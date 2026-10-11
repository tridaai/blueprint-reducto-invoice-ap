"""The golden eval pack: the synthetic world, measured.

Runs every bundled submission through the real pipeline on the
offline adapter and compares each record against gold that is
true by construction (datasets.py plants the cases; it never
opines about them afterwards).

Gates (a failure exits non-zero):

- match-decision accuracy — pipeline status and match outcome
  per planted case: 100% required on this gold set;
- exception precision — the codes named are exactly the codes
  planted, per case;
- fraud recall — ZERO misses tolerated: the planted bank-change
  case must land on FRAUD_REVIEW;
- citation presence — every extracted field on a payable record
  carries a citation;
- statement reconciliation — every planted break lands in its
  exact class, the clean statement reconciles fully, statement
  totals tie, and reconciling leaves every document record —
  and therefore the payment packet — untouched.
- payment runs — an assembled batch ties its totals to its
  member lines (credit note netting included), refuses the
  fraud hold and the exception by name, refuses a held member
  with the holding run named, frees its members on return,
  and on release locks them as paid-proposed behind an export
  that is byte-stable and carries only the supplier master's
  bank details.

Reported, not gated: per-field extraction accuracy, review-queue
precision. A live run (EVAL_LIVE=1, DOCUMENT_BACKEND=reducto,
key in env) is likewise reported, never gated — it measures the
engine, not this code.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reducto_ap_agent.datasets import build_world  # noqa: E402
from reducto_ap_agent.graph import ApPipeline  # noqa: E402
from reducto_ap_agent.object_store import InMemoryObjectStore  # noqa: E402
from reducto_ap_agent.offline_docs import OfflineDocumentAdapter  # noqa: E402
from reducto_ap_agent.service import ApService  # noqa: E402
from reducto_ap_agent.store import InMemoryRecordStore  # noqa: E402


def build_offline_service() -> ApService:
    from langgraph.checkpoint.memory import MemorySaver

    world = build_world()
    store = InMemoryRecordStore()
    store.reference.load(world.suppliers, world.purchase_orders, world.receipts)
    pipeline = ApPipeline(
        documents=OfflineDocumentAdapter(), store=store,
        objects=InMemoryObjectStore(), checkpointer=MemorySaver(),
    )
    return ApService(pipeline=pipeline, store=store)


def evaluate(service: ApService) -> dict:
    world = build_world()
    cases: list[dict] = []
    field_hits: dict[str, list[bool]] = {"invoice_number": [], "total": []}
    citation_total = 0
    citation_present = 0
    fraud_total = 0
    fraud_caught = 0
    queue_should = 0
    queue_actual_correct = 0

    for submission in world.submissions:
        records = service.submit(
            submission.submission_id, submission.filename,
            submission.text.encode("utf-8"), "text/plain",
        )
        for record, gold in zip(records, submission.expected):
            failures: list[str] = []
            if record.doc_class != gold.doc_class:
                failures.append(f"class {record.doc_class} != gold {gold.doc_class}")
            if record.status != gold.status:
                failures.append(f"status {record.status} != gold {gold.status}")
            gold_match = gold.match_status
            actual_match = record.match.status if record.match else None
            if gold_match is not None and actual_match != gold_match:
                failures.append(f"match {actual_match} != gold {gold_match}")
            actual_codes = record.match.exception_codes if record.match else []
            if sorted(actual_codes) != sorted(gold.exception_codes):
                failures.append(f"codes {actual_codes} != gold {gold.exception_codes}")
            if gold.fraud:
                fraud_total += 1
                if record.status == "FRAUD_REVIEW" and record.fraud_flags:
                    fraud_caught += 1
                else:
                    failures.append("planted fraud case was not held")
            if gold.injection and not record.injection_flags:
                failures.append("planted injection payload was not flagged")
            if gold.status == "AWAITING_APPROVER":
                queue_should += 1
                if record.status == "AWAITING_APPROVER":
                    queue_actual_correct += 1
            if record.extraction is not None and gold.doc_class in ("invoice", "credit_note"):
                number = record.extraction.value("invoice_number")
                field_hits["invoice_number"].append(str(number) == gold.invoice_number)
                total = record.extraction.value("total")
                field_hits["total"].append(
                    total is not None and gold.total is not None
                    and abs(float(total) - gold.total) < 0.005
                )
                for field in record.extraction.fields.values():
                    citation_total += 1
                    if field.citation is not None:
                        citation_present += 1
            cases.append({
                "record_id": record.record_id,
                "submission_id": submission.submission_id,
                "passed": not failures,
                "failures": failures,
            })

    # -- statement reconciliation --------------------------------
    # Runs after every submission is processed, so the statements
    # reconcile against the whole AP picture. Gold: SUB-020's
    # statement plants one break of each class, in order; SUB-021's
    # agrees with AP to the cent.
    snapshot = {
        r.record_id: (r.status, r.decision, r.match) for r in service.list_records()
    }
    breaks = service.reconcile("REC-SUB-020-0")
    clean = service.reconcile("REC-SUB-021-0")
    after = {
        r.record_id: (r.status, r.decision, r.match) for r in service.list_records()
    }
    expected_sequence = [
        ("INV-2001", "matched"),
        ("INV-2002", "amount_disagrees"),
        ("INV-2999", "missing_in_ap"),
        ("INV-2001", "duplicates_within_statement"),
        ("INV-2003", "missing_on_statement"),
    ]
    actual_sequence = [
        (line.invoice_number, line.classification) for line in breaks.lines
    ]
    disagrees = next(
        (l for l in breaks.lines if l.classification == "amount_disagrees"), None
    )
    breaks_exact = (
        actual_sequence == expected_sequence
        and disagrees is not None
        and disagrees.statement_amount == 440.00
        and disagrees.ap_amount == 400.00
        and breaks.counts == {
            "matched": 1, "amount_disagrees": 1, "missing_in_ap": 1,
            "missing_on_statement": 1, "duplicates_within_statement": 1,
        }
    )
    clean_full = (
        len(clean.lines) == 2
        and all(l.classification == "matched" for l in clean.lines)
        and clean.counts["matched"] == 2
        and sum(clean.counts.values()) == 2
    )
    totals_tie = (
        breaks.totals_tie and clean.totals_tie
        and not breaks.flags and not clean.flags
    )
    records_untouched = snapshot == after
    statement_results = {
        "breaks_counts": breaks.counts,
        "clean_counts": clean.counts,
        "breaks_exact": breaks_exact,
        "clean_reconciles_fully": clean_full,
        "totals_tie": totals_tie,
        "records_untouched": records_untouched,
    }

    # -- payment runs ------------------------------------------
    # The controlled batch on top of the packet. Approvals are
    # recorded first (the run takes only APPROVED records);
    # the planted set is two invoices + the netting credit
    # note, the fraud hold, an exception, and a member already
    # held by an open run.
    for record_id in (
        "REC-SUB-001-0", "REC-SUB-007-0", "REC-SUB-017-0", "REC-SUB-013-0",
    ):
        service.decide(record_id, "approve", "Eval Approver", "eval: matched")

    first = service.create_payment_run(
        "2026-10-16", record_ids=["REC-SUB-001-0", "REC-SUB-007-0"]
    )
    first_ties = (
        [line.record_id for line in first.lines]
        == ["REC-SUB-001-0", "REC-SUB-007-0"]
        and first.totals_by_currency == {"USD": 1200.00}  # 1400.00 − 200.00 CN
    )
    service.decide_payment_run(
        first.run_id, "return", "Eval Treasurer", "eval: rework the batch"
    )
    return_frees = all(
        service.get_record(rid).payment_state is None
        for rid in ("REC-SUB-001-0", "REC-SUB-007-0")
    )

    batch = service.create_payment_run(
        "2026-10-16",
        record_ids=[
            "REC-SUB-001-0", "REC-SUB-007-0", "REC-SUB-017-0",
            "REC-SUB-008-0",  # the planted fraud hold
            "REC-SUB-003-0",  # a planted price-variance exception
        ],
    )
    batch_refusals = {r.record_id: r for r in batch.refusals}
    refusals_named = (
        batch_refusals["REC-SUB-008-0"].code == "FRAUD_HOLD"
        and batch_refusals["REC-SUB-003-0"].code == "MATCH_EXCEPTION"
    )
    run_totals_tie = (
        batch.totals_by_currency == {"USD": 2100.00}
        and round(sum(line.amount for line in batch.lines), 2) == 2100.00
    )

    sweep = service.create_payment_run(
        "2026-10-16", record_ids=["REC-SUB-001-0", "REC-SUB-013-0"]
    )
    held = {r.record_id: r for r in sweep.refusals}.get("REC-SUB-001-0")
    member_named = (
        held is not None
        and held.code == "ALREADY_IN_RUN"
        and batch.run_id in held.reason
    )
    service.decide_payment_run(
        sweep.run_id, "return", "Eval Treasurer", "eval: sweep done"
    )

    service.decide_payment_run(
        batch.run_id, "release", "Eval Treasurer", "eval: batch reviewed"
    )
    export_a = service.payment_run_export(batch.run_id)
    export_b = service.payment_run_export(batch.run_id)
    export_text = export_a.decode("utf-8")
    export_master_only = (
        "US00 BPAY 1111 2222 3333" in export_text  # SUP-02's master account
        and "US00 ATTK 9999 0000 1111" not in export_text  # the fraud invoice's ask
        and "US00 GRAN 5555 6666 7777" not in export_text  # no row for SUP-07 at all
        and "REC-SUB-008-0" not in export_text
    )
    export_stable = (
        export_a == export_b
        and service.get_payment_run(batch.run_id).export_digest
        == hashlib.sha256(export_a).hexdigest()
    )
    release_locks = (
        service.get_record("REC-SUB-001-0").payment_state == "PAID_PROPOSED"
    )
    follow_up = service.create_payment_run(
        "2026-10-23", record_ids=["REC-SUB-001-0", "REC-SUB-013-0"]
    )
    proposed_refused = (
        {r.record_id: r for r in follow_up.refusals}["REC-SUB-001-0"].code
        == "ALREADY_PAID_PROPOSED"
    )
    payment_run_results = {
        "first_run_nets_and_ties": first_ties,
        "return_frees_members": return_frees,
        "refusals_named": refusals_named,
        "totals_tie": run_totals_tie,
        "member_named_with_holding_run": member_named,
        "export_master_details_only": export_master_only,
        "export_byte_stable_and_digested": export_stable,
        "release_locks_members": release_locks,
        "paid_proposed_refused_forever": proposed_refused,
    }

    total_cases = len(cases)
    passed = sum(1 for c in cases if c["passed"])
    results = {
        "backend": "offline",
        "cases": total_cases,
        "passed": passed,
        "match_decision_accuracy": passed / total_cases if total_cases else 0.0,
        "extraction_accuracy_by_field": {
            name: (sum(hits) / len(hits) if hits else None)
            for name, hits in field_hits.items()
        },
        "citation_presence": citation_present / citation_total if citation_total else None,
        "fraud_cases": fraud_total,
        "fraud_caught": fraud_caught,
        "review_queue_precision": (
            queue_actual_correct / queue_should if queue_should else None
        ),
        "statement_reconciliation": statement_results,
        "payment_runs": payment_run_results,
        "failures": [c for c in cases if not c["passed"]],
    }
    results["gates"] = {
        "match_decision_accuracy == 1.0": results["match_decision_accuracy"] == 1.0,
        "fraud_misses == 0": fraud_caught == fraud_total,
        "citation_presence == 1.0": results["citation_presence"] == 1.0,
        "statement_breaks_classified_exactly": breaks_exact,
        "clean_statement_reconciles_fully": clean_full,
        "statement_totals_tie": totals_tie,
        "reconciliation_left_records_untouched": records_untouched,
        "payment_run_totals_tie": payment_run_results["totals_tie"],
        "payment_run_refusals_named": payment_run_results["refusals_named"],
        "payment_run_member_named_with_holding_run":
            payment_run_results["member_named_with_holding_run"],
        "payment_run_return_frees_members":
            payment_run_results["return_frees_members"],
        "payment_run_export_master_details_only":
            payment_run_results["export_master_details_only"],
        "payment_run_release_locks_and_export_stable": (
            payment_run_results["release_locks_members"]
            and payment_run_results["export_byte_stable_and_digested"]
            and payment_run_results["paid_proposed_refused_forever"]
        ),
    }
    results["gate_passed"] = all(results["gates"].values())
    return results


def main() -> int:
    results = evaluate(build_offline_service())
    out_paths = [
        Path(__file__).resolve().parent / "results.json",
        Path(__file__).resolve().parents[1] / "src" / "reducto_ap_agent" / "data" / "eval_results.json",
    ]
    for path in out_paths:
        path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in results.items() if k != "failures"}, indent=2))
    if results["failures"]:
        print("\nFAILURES:")
        for case in results["failures"]:
            print(f"  {case['record_id']}: {'; '.join(case['failures'])}")
    if os.environ.get("EVAL_LIVE") == "1":
        print("\nEVAL_LIVE=1: a live Reducto run is reported-not-gated; "
              "it requires REDUCTO_API_KEY and is not attempted by this runner "
              "in the offline gate environment.")
    print("\nGATE:", "PASS" if results["gate_passed"] else "FAIL")
    return 0 if results["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
