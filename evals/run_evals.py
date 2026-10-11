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
  member lines (the invoice member at its net open amount —
  its credit applied in code before assembly), refuses the
  fraud hold and the exception by name, refuses a held member
  with the holding run named, frees its members on return,
  and on release locks them as paid-proposed behind an export
  that is byte-stable and carries only the supplier master's
  bank details.
- remittance confirmation — the bank's answer to a released
  run classifies line by line (one member exact, one short,
  one over, one missing, one line for an invoice the run
  never contained), the summary ties to the run's totals,
  advices naming no run or a run not on file are held
  UNMATCHED with their reasons, and a full confirmation
  cycle leaves the run, its export, and its members'
  approvals byte-identical.
- credit application — a credit lands only against the
  invoice its own document names: applied by a controller in
  code it nets the invoice's open amount (the approved face
  and the approval itself untouched, the credit listed on
  the packet line and the run member), a full cover excludes
  its invoice from a run by name (FULLY_CREDITED), and the
  refusals are named — an over-credit with both figures, a
  credit naming no invoice, one naming an invoice not on
  file, one against the fraud-held invoice, and the late
  credit against an invoice already paid-proposed. The
  released export instructs the net figure and carries no
  negative credit line.
- AP position — the close read over a planted world (its own
  service, records planted directly so the due dates are
  exact): bucket placement is exact including the boundary
  days (30 days past due is 1–30, 31 is 31–60), an invoice
  with no due date is UNDATED and named, the aged total ties
  to the packet's open total computed in the same pass, the
  state split (free / in-run / paid-proposed) foots to the
  same figure, and a corrupted record — a credit ledger
  exceeding its invoice's face, which application refuses in
  code — breaks the tie by name (POSITION_TIE_BREAK) with
  both figures.

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

from reducto_ap_agent.datasets import build_world, remittance_advice_doc  # noqa: E402
from reducto_ap_agent.errors import GateError  # noqa: E402
from reducto_ap_agent.graph import ApPipeline  # noqa: E402
from reducto_ap_agent.object_store import InMemoryObjectStore  # noqa: E402
from reducto_ap_agent.offline_docs import OfflineDocumentAdapter  # noqa: E402
from reducto_ap_agent.schemas import (  # noqa: E402
    CreditApplication,
    DocumentRecord,
    ExtractedDocument,
    ExtractedField,
)
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
    # the planted set is two invoices + the credit note that
    # credits one of them, the fraud hold, an exception, and a
    # member already held by an open run.
    for record_id in (
        "REC-SUB-001-0", "REC-SUB-007-0", "REC-SUB-017-0", "REC-SUB-013-0",
    ):
        service.decide(record_id, "approve", "Eval Approver", "eval: matched")

    # The credit is applied before any run is assembled: a
    # controller nets CN-1001 against INV-1001 in code, and
    # from here on every figure the packet and the runs carry
    # for that invoice is its open amount — 1200.00, with the
    # credit listed and the 1400.00 face untouched.
    service.apply_credit("REC-SUB-007-0", "Eval Controller")
    credited_invoice = service.get_record("REC-SUB-001-0")
    applied_credit = service.get_record("REC-SUB-007-0")
    packet_line = next(
        line for line in service.payment_packet().lines
        if line.record_id == "REC-SUB-001-0"
    )
    application_nets = (
        applied_credit.credit_state == "APPLIED"
        and applied_credit.applied_invoice_record_id == "REC-SUB-001-0"
        and credited_invoice.decision is not None
        and float(credited_invoice.extraction.value("total")) == 1400.00
        and packet_line.amount == 1200.00
        and packet_line.gross_amount == 1400.00
        and [c.credit_number for c in packet_line.credits_applied]
        == ["CN-1001"]
    )

    first = service.create_payment_run(
        "2026-10-16", record_ids=["REC-SUB-001-0", "REC-SUB-007-0"]
    )
    first_ties = (
        [line.record_id for line in first.lines] == ["REC-SUB-001-0"]
        and first.lines[0].amount == 1200.00  # the net open amount
        and first.lines[0].gross_amount == 1400.00
        and [c.credit_number for c in first.lines[0].credits_applied]
        == ["CN-1001"]
        and {r.record_id: r.code for r in first.refusals}.get(
            "REC-SUB-007-0") == "CREDIT_NOTE"
        and first.totals_by_currency == {"USD": 1200.00}
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

    # -- remittance confirmation scenario ------------------------
    # A fresh released run, then the bank's answer to it: one
    # member paid exactly, one short, one over, one never
    # answered for, and one line for an invoice the run never
    # contained. The confirmation's summary must tie to the
    # run's total, and the whole cycle — reconcile plus the
    # controller's accept — must leave the run, its export,
    # and its members' approvals exactly as released.
    conf_members = [
        "REC-SUB-002-0",  # INV-1002, USD 1010.00 → exact
        "REC-SUB-010-0",  # INV-1010, USD 600.00  → short 50.00
        "REC-SUB-014-0",  # INV-1014, USD 300.00  → over 25.00
        "REC-SUB-018-0",  # INV-2002, USD 400.00  → no line: missing
    ]
    for record_id in conf_members:
        service.decide(record_id, "approve", "Eval Approver", "eval: matched")
    conf_run = service.create_payment_run(
        "2026-10-30", record_ids=list(conf_members)
    )
    service.decide_payment_run(
        conf_run.run_id, "release", "Eval Treasurer", "eval: batch reviewed"
    )
    conf_run = service.get_payment_run(conf_run.run_id)
    export_before = service.payment_run_export(conf_run.run_id)
    member_before = {
        rid: (
            service.get_record(rid).status,
            service.get_record(rid).decision,
            service.get_record(rid).payment_state,
        )
        for rid in conf_members
    }
    advice_text = remittance_advice_doc(conf_run.run_id, [
        ("INV-1002", 1010.00, "USD", "2026-10-30", "BNK-EVAL-01"),
        ("INV-1010", 550.00, "USD", "2026-10-30", "BNK-EVAL-02"),
        ("INV-1014", 325.00, "USD", "2026-10-30", "BNK-EVAL-03"),
        ("INV-9999", 123.00, "USD", "2026-10-30", "BNK-EVAL-04"),
    ])
    (advice_record,) = service.submit(
        "SUB-EVAL-CONF", "sub-eval-conf.pdf",
        advice_text.encode("utf-8"), "text/plain",
    )
    conf = service.confirm_remittance(advice_record.record_id)
    classes = {line.invoice_number: line.classification for line in conf.lines}
    classes_exact = (
        conf.status == "AWAITING_CONTROLLER"
        and classes == {
            "INV-1002": "paid_as_proposed",
            "INV-1010": "short_paid",
            "INV-1014": "over_paid",
            "INV-2002": "missing",
            "INV-9999": "unexpected_line",
        }
        and service.get_record("REC-SUB-002-0").payment_confirmation == "CONFIRMED"
        and service.get_record("REC-SUB-010-0").payment_confirmation == "SHORT_PAID"
        and service.get_record("REC-SUB-014-0").payment_confirmation == "OVER_PAID"
        and service.get_record("REC-SUB-018-0").payment_confirmation == "MISSING"
    )
    (conf_totals,) = conf.totals
    summary_ties = (
        conf.totals_tie
        and conf_totals.run_total == 2310.00
        and conf_totals.confirmed_paid == 1010.00
        and conf_totals.shortfall == 50.00
        and conf_totals.excess == 25.00
        and conf_totals.missing_proposed == 400.00
        and conf_totals.unexpected_paid == 123.00
    )
    decided = service.decide_confirmation(
        conf.confirmation_id, "accept", "Eval Controller", "eval: breaks noted"
    )
    conf_run_after = service.get_payment_run(conf_run.run_id)
    member_after = {
        rid: (
            service.get_record(rid).status,
            service.get_record(rid).decision,
            service.get_record(rid).payment_state,
        )
        for rid in conf_members
    }
    run_and_export_untouched = (
        decided.status == "ACCEPTED"
        and service.payment_run_export(conf_run.run_id) == export_before
        and conf_run_after.decision == conf_run.decision
        and conf_run_after.export_digest == conf_run.export_digest
        and member_after == member_before
    )
    unmatched_no_run = service.confirm_remittance("REC-SUB-022-0")
    unmatched_unknown = service.confirm_remittance("REC-SUB-023-0")
    unmatched_held = (
        unmatched_no_run.status == "UNMATCHED"
        and unmatched_no_run.run_id is None
        and "names no payment run" in unmatched_no_run.unmatched_reason
        and unmatched_unknown.status == "UNMATCHED"
        and "RUN-2099-01-99" in unmatched_unknown.unmatched_reason
        and "not on file" in unmatched_unknown.unmatched_reason
    )
    confirmation_results = {
        "classes_exact": classes_exact,
        "summary_ties": summary_ties,
        "unmatched_held": unmatched_held,
        "run_and_export_untouched": run_and_export_untouched,
    }

    # -- credit application ------------------------------------
    # The discipline around the application already made in
    # the payment-run section: a full cover, and every named
    # refusal. The follow-up run from that section still holds
    # REC-SUB-013-0; return it first so the cover check can
    # assemble on that invoice.
    service.decide_payment_run(
        follow_up.run_id, "return", "Eval Treasurer",
        "eval: free the sweep member",
    )

    def apply_refusal(record_id: str) -> str:
        try:
            service.apply_credit(record_id, "Eval Controller")
        except GateError as exc:
            return str(exc)
        return ""

    # Full cover: CN-2003 (200.00) against INV-2003 (200.00).
    service.decide("REC-SUB-019-0", "approve", "Eval Approver", "eval: matched")
    service.decide("REC-SUB-024-0", "approve", "Eval Approver", "eval: matched")
    service.apply_credit("REC-SUB-024-0", "Eval Controller")
    cover_run = service.create_payment_run(
        "2026-11-06", record_ids=["REC-SUB-019-0", "REC-SUB-013-0"]
    )
    cover_refusal = {
        r.record_id: r for r in cover_run.refusals
    }.get("REC-SUB-019-0")
    full_cover_excludes = (
        bool(service.get_record("REC-SUB-019-0").credits_applied)
        and cover_refusal is not None
        and cover_refusal.code == "FULLY_CREDITED"
        and "CN-2003" in cover_refusal.reason
        and [line.record_id for line in cover_run.lines]
        == ["REC-SUB-013-0"]
    )
    service.decide_payment_run(
        cover_run.run_id, "return", "Eval Treasurer", "eval: cover checked"
    )

    # Over-credit: CN-3001 is 650.00 against INV-3001's 600.00.
    # (The CN-2002/INV-2002 pair plants the same case, but the
    # confirmation scenario above has already paid-proposed
    # INV-2002 — its refusal there would be the late one.)
    service.decide("REC-SUB-030-0", "approve", "Eval Approver", "eval: matched")
    service.decide("REC-SUB-031-0", "approve", "Eval Approver", "eval: matched")
    over = apply_refusal("REC-SUB-031-0")
    over_credit_refused = (
        over.startswith("OVER_CREDIT")
        and "650.00" in over and "600.00" in over
        and service.get_record("REC-SUB-031-0").credit_state == "UNAPPLIED"
        and service.get_record("REC-SUB-030-0").credits_applied == []
    )

    # No reference, an unknown invoice, the fraud-held target.
    service.decide("REC-SUB-026-0", "approve", "Eval Approver", "eval: matched")
    no_reference = apply_refusal("REC-SUB-026-0")
    service.decide("REC-SUB-027-0", "approve", "Eval Approver", "eval: matched")
    unknown_invoice = apply_refusal("REC-SUB-027-0")
    service.decide("REC-SUB-028-0", "approve", "Eval Approver", "eval: matched")
    fraud_target = apply_refusal("REC-SUB-028-0")
    references_held = (
        no_reference.startswith("NO_INVOICE_REFERENCE")
        and unknown_invoice.startswith("INVOICE_NOT_ON_FILE")
        and "INV-9999" in unknown_invoice
        and fraud_target.startswith("TARGET_FRAUD_HOLD")
    )

    # Late: CN-1003 names INV-1001, released in the batch above.
    service.decide("REC-SUB-029-0", "approve", "Eval Approver", "eval: matched")
    late = apply_refusal("REC-SUB-029-0")
    late_refused = (
        late.startswith("TARGET_PAID_PROPOSED")
        and batch.run_id in late
        and service.get_record("REC-SUB-029-0").credit_state == "UNAPPLIED"
    )

    # The released batch's export instructs the net figure and
    # carries no negative credit line of its own.
    batch_export = service.payment_run_export(batch.run_id).decode("utf-8")
    net_row = next(
        row for row in batch_export.splitlines() if "REC-SUB-001-0" in row
    )
    export_net = (
        net_row.rstrip().endswith(",1200.00")
        and "REC-SUB-007-0" not in batch_export
    )
    credit_results = {
        "application_nets_invoice": application_nets,
        "full_cover_excludes": full_cover_excludes,
        "over_credit_refused": over_credit_refused,
        "references_held": references_held,
        "late_refused": late_refused,
        "refusals_named": (
            over_credit_refused and references_held and late_refused
        ),
        "export_net": export_net,
    }

    # -- AP position scenario ------------------------------------
    # Its own service and planted records, so the due dates are
    # exact to the day and nothing above is disturbed.
    position_service = build_offline_service()

    def plant(record_id, supplier, number, total, *, due="2026-11-15",
              status="APPROVED", payment_state=None, run_id=None,
              credits=(), fraud_flags=()):
        fields = {
            "invoice_number": ExtractedField(value=number),
            "invoice_date": ExtractedField(value="2026-09-01"),
            "total": ExtractedField(value=total),
        }
        if due is not None:
            fields["due_date"] = ExtractedField(value=due)
        position_service.store.save(DocumentRecord(
            record_id=record_id, submission_id="SUB-EVAL",
            supplier_name=supplier, status=status, doc_class="invoice",
            extraction=ExtractedDocument(
                doc_class="invoice", fields=fields, currency="USD"),
            payment_state=payment_state, payment_run_id=run_id,
            credits_applied=[
                CreditApplication(
                    credit_record_id=f"{record_id}-CR{i}",
                    credit_number=f"CN-EV-{i}",
                    invoice_record_id=record_id, amount=amount,
                    currency="USD", actor="Eval Controller",
                ) for i, amount in enumerate(credits)
            ],
            fraud_flags=list(fraud_flags),
        ))

    plant("REC-EV-A1", "Acme", "INV-EV-1", 100.0, due="2026-10-16")
    plant("REC-EV-A2", "Acme", "INV-EV-2", 200.0, due="2026-10-15")
    plant("REC-EV-A3", "Acme", "INV-EV-3", 300.0, due=None)
    plant("REC-EV-B1", "Beta", "INV-EV-4", 400.0, due="2026-11-20",
          payment_state="IN_RUN", run_id="RUN-EV-01")
    plant("REC-EV-B2", "Beta", "INV-EV-5", 500.0, credits=(500.0,))
    plant("REC-EV-F1", "Granite", "INV-EV-6", 900.0,
          status="FRAUD_REVIEW", fraud_flags=("bank-change pattern",))

    position = position_service.ap_position("2026-11-15")
    planted_lines = {
        line.record_id: line
        for s in position.suppliers for line in s.lines
    }
    buckets_exact = (
        planted_lines["REC-EV-A1"].bucket == "1-30"
        and planted_lines["REC-EV-A1"].days_past_due == 30
        and planted_lines["REC-EV-A2"].bucket == "31-60"
        and planted_lines["REC-EV-A2"].days_past_due == 31
        and planted_lines["REC-EV-B1"].bucket == "CURRENT"
        and position.bucket_totals_by_currency["USD"] == {
            "CURRENT": 400.0, "1-30": 100.0, "31-60": 200.0,
            "61-90": 0.0, "90+": 0.0, "UNDATED": 300.0,
        }
    )
    undated_named = (
        planted_lines["REC-EV-A3"].bucket == "UNDATED"
        and planted_lines["REC-EV-A3"].days_past_due is None
    )
    (usd_tie,) = [t for t in position.ties if t.currency == "USD"]
    tie_holds = (
        position.tie_status == "TIED"
        and usd_tie.packet_total == usd_tie.position_total == 1000.0
        and usd_tie.packet_total
        == position_service.payment_packet().totals_by_currency["USD"]
        and [f.invoice_number for f in position.fully_credited]
        == ["INV-EV-5"]
        and [h.invoice_number for h in position.fraud_held] == ["INV-EV-6"]
    )
    state_split_sums = (
        position.state_totals_by_currency["USD"] == {
            "OPEN_FREE": 600.0, "IN_RUN": 400.0, "PAID_PROPOSED": 0.0,
        }
        and position.splits_agree
    )
    # The corrupted record: a ledger exceeding its face. The
    # credit path refuses this in code, so only corruption
    # writes it — and the position must refuse to tie over it.
    plant("REC-EV-N1", "Acme", "INV-EV-7", 400.0, credits=(450.0,))
    broken = position_service.ap_position("2026-11-15")
    (broken_tie,) = [t for t in broken.ties if t.currency == "USD"]
    tie_break_named = (
        broken.tie_status == "POSITION_TIE_BREAK"
        and broken_tie.packet_total == 950.0
        and broken_tie.position_total == 1000.0
        and broken_tie.difference == -50.0
        and [a.code for a in broken.anomalies] == ["NEGATIVE_OPEN"]
        and broken.anomalies[0].invoice_number == "INV-EV-7"
    )
    position_results = {
        "buckets_exact": buckets_exact,
        "undated_named": undated_named,
        "tie_holds": tie_holds,
        "state_split_sums": state_split_sums,
        "tie_break_named": tie_break_named,
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
        "remittance_confirmation": confirmation_results,
        "credit_notes": credit_results,
        "ap_position": position_results,
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
        "confirmation_classes_exact": confirmation_results["classes_exact"],
        "confirmation_summary_ties": confirmation_results["summary_ties"],
        "confirmation_unmatched_held": confirmation_results["unmatched_held"],
        "confirmation_leaves_run_and_export_untouched":
            confirmation_results["run_and_export_untouched"],
        "credit_application_nets_the_invoice":
            credit_results["application_nets_invoice"],
        "credit_full_cover_excludes_from_run":
            credit_results["full_cover_excludes"],
        "credit_refusals_named": credit_results["refusals_named"],
        "credit_export_carries_net_amounts": credit_results["export_net"],
        "position_buckets_exact": position_results["buckets_exact"],
        "position_undated_named": position_results["undated_named"],
        "position_ties_to_packet": position_results["tie_holds"],
        "position_state_split_sums": position_results["state_split_sums"],
        "position_tie_break_named": position_results["tie_break_named"],
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
