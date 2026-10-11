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
  carries a citation.

Reported, not gated: per-field extraction accuracy, review-queue
precision. A live run (EVAL_LIVE=1, DOCUMENT_BACKEND=reducto,
key in env) is likewise reported, never gated — it measures the
engine, not this code.
"""

from __future__ import annotations

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
        "failures": [c for c in cases if not c["passed"]],
    }
    results["gates"] = {
        "match_decision_accuracy == 1.0": results["match_decision_accuracy"] == 1.0,
        "fraud_misses == 0": fraud_caught == fraud_total,
        "citation_presence == 1.0": results["citation_presence"] == 1.0,
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
