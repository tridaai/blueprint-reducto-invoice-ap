"""The traced demo: the whole synthetic AP world, end to end.

Runs every bundled submission through the pipeline on the
offline adapter, has a named approver decide the records that
reach the gate, and prints what an operator would see: the
matches, the exceptions with their codes, the fraud hold, the
refusal — then the two supplier statements reconciled line by
line with their breaks itemised and a controller's decision on
each — then the credit notes: a controller applies them
against the invoices they name (one nets, one covers its
invoice in full, two are refused by name — an over-credit and
one against the fraud hold) — the payment packet that
proposes open amounts, and executes nothing — and finally a
payment run: the approved records
assembled for a value date, the fraud hold and the exception
refused by name when they are (mistakenly) asked for, and a
treasurer releasing the batch into a bank-instruction export
that still, itself, pays no one — and finally the bank's
answer: a remittance advice naming the released run, reconciled
line by line in code, with the shorts, the overpayment, the
unaccounted members, and one line for an invoice the run never
contained, all itemised for a controller.
"""

from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver

from .datasets import build_world, remittance_advice_doc
from .errors import GateError
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

    print("\nCredit notes — a controller applies them against the invoices they name:")
    controller = "Marta Chen (controller)"
    for credit_id in ("REC-SUB-007-0", "REC-SUB-024-0"):
        applied = service.apply_credit(credit_id, controller)
        entry = next(
            c for c in service.get_record(
                applied.applied_invoice_record_id or ""
            ).credits_applied
            if c.credit_record_id == credit_id
        )
        print(f"  {entry.credit_number:<9} applied by {controller}: "
              f"{entry.amount:>7.2f} {entry.currency} against "
              f"{applied.applied_invoice_record_id}")
    for credit_id in ("REC-SUB-025-0", "REC-SUB-028-0"):
        try:
            service.apply_credit(credit_id, controller)
            print(f"  {credit_id}: applied (unexpected)")
        except GateError as exc:
            print(f"  REFUSED {credit_id}: {exc}")

    packet = service.payment_packet()
    print(f"\nPayment packet {packet.packet_id} — proposed only, nothing executed:")
    for line in packet.lines:
        detail = ""
        if line.credits_applied:
            credits = ", ".join(
                f"{c.credit_number} −{c.amount:.2f}" for c in line.credits_applied
            )
            detail = f"  (face {line.gross_amount:.2f} − credits {credits})"
        print(f"  {line.supplier_name:<28} {line.invoice_number:<10} {line.currency} {line.amount:>10.2f}{detail}")
    for currency, total in packet.totals_by_currency.items():
        print(f"  {'TOTAL':<42} {currency} {total:>10.2f}")
    if packet.unapplied_credits:
        print("  Unapplied credits (approved, on file, reducing nothing yet):")
        for credit in packet.unapplied_credits:
            print(f"    {credit.credit_number:<9} {credit.supplier_name:<28} "
                  f"{credit.currency} {credit.amount:>7.2f} — {credit.reason}")
    print(f"\n{packet.note}")

    print("\nPayment run — the batch a treasurer can release or return:")
    approved_ids = [line.record_id for line in packet.lines]
    run = service.create_payment_run(
        "2026-10-16",
        # The fraud hold and an exception are named too — the run
        # refuses them by name rather than quietly leaving them out.
        record_ids=approved_ids + ["REC-SUB-008-0", "REC-SUB-003-0"],
    )
    totals = ", ".join(
        f"{currency} {total:,.2f}" for currency, total in run.totals_by_currency.items()
    )
    print(f"\n{run.run_id} — value date {run.value_date} — {run.status} · totals: {totals}")
    for line in run.lines:
        detail = ""
        if line.credits_applied:
            credits = ", ".join(
                f"{c.credit_number} −{c.amount:.2f}" for c in line.credits_applied
            )
            detail = f"  (face {line.gross_amount:.2f} − credits {credits})"
        print(f"  MEMBER  {line.record_id:<16} {line.supplier_name:<28} "
              f"{line.invoice_number:<10} {line.currency} {line.amount:>10.2f}{detail}")
    for refusal in run.refusals:
        print(f"  REFUSED {refusal.record_id:<16} {refusal.code}: {refusal.reason}")
    released = service.decide_payment_run(
        run.run_id, "release", "Jonas Weber (treasurer)", "demo: batch reviewed"
    )
    print(f"  → {released.status} by Jonas Weber (treasurer)")
    print(f"  export: {released.export_key} · sha256 {released.export_digest[:16]}…")
    export_text = service.payment_run_export(run.run_id).decode("utf-8")
    for row in export_text.splitlines()[:4]:
        print(f"    {row}")
    print("    …")
    print("\nThe export is a file for the customer's payment systems. "
          "This system has still paid nothing.")

    print("\nRemittance confirmation — the bank answers the run:")
    advice_text = remittance_advice_doc(run.run_id, [
        ("INV-1001", 1200.00, "USD", "2026-10-16", "BNK-2026-3101"),  # exact — the net figure
        ("INV-1002", 1000.00, "USD", "2026-10-16", "BNK-2026-3102"),  # short 10.00
        ("INV-1010", 650.00, "USD", "2026-10-17", "BNK-2026-3104"),   # over 50.00
        ("INV-1013", 1080.00, "EUR", "2026-10-17", "BNK-2026-3105"),  # exact
        ("INV-9999", 123.00, "USD", "2026-10-17", "BNK-2026-3106"),   # not a member
        # INV-1014, INV-2001, INV-2002, INV-3001: no line — missing.
        # INV-2003 is not missing: it was fully credited and never
        # became a member — the run refused it by name above.
    ])
    (advice_record,) = service.submit(
        "SUB-DEMO-ADVICE", "sub-demo-advice.pdf",
        advice_text.encode("utf-8"), "text/plain",
    )
    confirmation = service.confirm_remittance(advice_record.record_id)
    counts = ", ".join(
        f"{name} {n}" for name, n in confirmation.counts.items() if n
    )
    print(f"\n{confirmation.confirmation_id} — answers {confirmation.run_id} "
          f"via advice {advice_record.record_id} — {confirmation.status}")
    print(f"  totals tie: {'yes' if confirmation.totals_tie else 'NO'} · {counts}")
    for line in confirmation.lines:
        proposed = f"{line.proposed_amount:.2f}" if line.proposed_amount is not None else "—"
        paid = f"{line.paid_amount:.2f}" if line.paid_amount is not None else "—"
        print(f"  {line.invoice_number:<9} {line.classification:<18} "
              f"proposed {proposed:>9} · paid {paid:>9} {line.currency}")
    for row in confirmation.totals:
        print(f"  {row.currency}: run {row.run_total:.2f} · confirmed "
              f"{row.confirmed_paid:.2f} · shortfall {row.shortfall:.2f} · "
              f"excess {row.excess:.2f} · missing {row.missing_proposed:.2f} · "
              f"unexpected {row.unexpected_paid:.2f}")
    decided = service.decide_confirmation(
        confirmation.confirmation_id, "accept", "Marta Chen (controller)",
        "demo: breaks noted for follow-up with the bank",
    )
    print(f"  → {decided.status} by Marta Chen (controller)")
    print("\nThe confirmation is evidence over the run. The run, its "
          "export, and every approval above are exactly as they were.")

    print("\nA late credit — CN-1003 names INV-1001, which the run above "
          "already released:")
    try:
        service.apply_credit("REC-SUB-029-0", controller)
        print("  applied (unexpected)")
    except GateError as exc:
        print(f"  REFUSED REC-SUB-029-0: {exc}")
    print("  The credit stays on file, unapplied, with its reason on the "
          "packet — next cycle's business, settled against the supplier's "
          "future invoices. It does not rewrite a released run.")
    return service


def main() -> None:
    run_demo()


if __name__ == "__main__":
    main()
