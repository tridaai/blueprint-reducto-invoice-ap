"""The traced demo: the whole synthetic AP world, end to end.

Runs every bundled submission through the pipeline on the
offline adapter, has a named approver decide the records that
reach the gate, and prints what an operator would see: the
matches, the exceptions with their codes, the fraud hold, the
refusal — then the two supplier statements reconciled line by
line with their breaks itemised and a controller's decision on
each — and the payment packet that proposes, and executes
nothing.
"""

from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver

from .datasets import build_world
from .graph import ApPipeline
from .object_store import InMemoryObjectStore
from .offline_docs import OfflineDocumentAdapter
from .service import ApService
from .store import InMemoryRecordStore

APPROVER = "Priya Nair (AP lead)"


def run_demo() -> ApService:
    world = build_world()
    store = InMemoryRecordStore()
    store.reference.load(world.suppliers, world.purchase_orders, world.receipts)
    pipeline = ApPipeline(
        documents=OfflineDocumentAdapter(),
        store=store,
        objects=InMemoryObjectStore(),
        checkpointer=MemorySaver(),
    )
    service = ApService(pipeline=pipeline, store=store)

    print("Trida Blueprint — Reducto Invoice AP, deployed in-VPC (offline adapter)\n")
    for submission in world.submissions:
        records = service.submit(
            submission.submission_id, submission.filename,
            submission.text.encode("utf-8"), "text/plain",
        )
        for record in records:
            match = record.match
            detail = ""
            if match:
                detail = f" match={match.status}"
                if match.exception_codes:
                    detail += f" [{', '.join(match.exception_codes)}]"
            if record.fraud_flags:
                detail += f" FRAUD: {record.fraud_flags[0]}"
            if record.refusal_reason:
                detail += f" — {record.refusal_reason}"
            if record.injection_flags:
                detail += f" injection-screen: {record.injection_flags[0]}"
            print(f"{record.record_id:<16} {record.doc_class:<18} {record.status:<20}{detail}")
            if record.status == "AWAITING_APPROVER":
                service.decide(record.record_id, "approve", APPROVER, "demo: matched, approved")
                print(f"{'':<16} → approved by {APPROVER}")

    print("\nSupplier statement reconciliation — the statements answer back:")
    for statement_id in ("REC-SUB-020-0", "REC-SUB-021-0"):
        recon = service.reconcile(statement_id)
        counts = ", ".join(
            f"{name} {n}" for name, n in recon.counts.items() if n
        )
        print(f"\n{recon.reconciliation_id} — {recon.supplier_name}, period "
              f"{recon.period} — {recon.status}")
        print(f"  totals tie: {'yes' if recon.totals_tie else 'NO'} "
              f"(stated {recon.statement_total_stated:.2f} vs lines "
              f"{recon.statement_total_lines:.2f}) · {counts}")
        for line in recon.lines:
            if line.classification == "matched":
                continue
            stmt = f"{line.statement_amount:.2f}" if line.statement_amount is not None else "—"
            ap = f"{line.ap_amount:.2f}" if line.ap_amount is not None else "—"
            print(f"  BREAK {line.invoice_number:<9} {line.classification:<28} "
                  f"statement {stmt:>8} · AP {ap:>8}")
        if all(n == 0 for name, n in recon.counts.items()
               if name not in ("matched",)):
            decided = service.decide_reconciliation(
                recon.reconciliation_id, "accept", "Marta Chen (controller)",
                "demo: statement agrees with AP",
            )
            print(f"  → {decided.status} by Marta Chen (controller)")
        else:
            decided = service.decide_reconciliation(
                recon.reconciliation_id, "flag_back", "Marta Chen (controller)",
                "demo: breaks itemised above — AP team to take them to the supplier",
            )
            print(f"  → {decided.status} by Marta Chen (controller)")

    packet = service.payment_packet()
    print(f"\nPayment packet {packet.packet_id} — proposed only, nothing executed:")
    for line in packet.lines:
        print(f"  {line.supplier_name:<28} {line.invoice_number:<10} {line.currency} {line.amount:>10.2f}")
    for currency, total in packet.totals_by_currency.items():
        print(f"  {'TOTAL':<42} {currency} {total:>10.2f}")
    print(f"\n{packet.note}")
    return service


def main() -> None:
    run_demo()


if __name__ == "__main__":
    main()
