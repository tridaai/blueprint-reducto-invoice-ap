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
- supplier ledger — one supplier's whole relationship as a
  read model over the main service's end state: Harborlight's
  totals (face, credits, paid, open) tie to its position to
  the cent, with its two released invoices disclosed on the
  position's fully-paid list, and every supplier's ledger
  ties; a stray invoice from a supplier spelling the master
  has never seen lands under Unmatched — counted, named as
  written, and present on no supplier's ledger; Harborlight's
  story renders whole (a fully credited invoice with its
  credit, a paid-proposed member the bank answered MISSING, a
  waiting credit with its reason, four open statement breaks);
  and the bank warning is present exactly where a document
  disagreed with the master — Granite Peak — and nowhere else.
- partial payments — an invoice paid in parts across two
  runs, on its own planted service: the first run takes
  1200.00 of a 2000.00 face and the member stands PART_PAID;
  a further 900.00 is refused as an overcommit with the
  figures named; a 200.00 credit lands on the remainder and
  a 900.00 credit is judged against the remainder, not the
  face; the position ages only the 800.00 remainder with the
  paid figure beside it; advices answer the part run's line
  (the part confirms, the full face is an over-payment on
  that run); the second run completes the paydown — 1200 +
  600 = 2000 − 200 — with each export instructing only its
  own part; and resends are held on the cumulative instructed
  figure, 1200.00 after the first run and 1800.00 after both.
- PO coverage — the accumulation the match cannot see, on a
  planted service (the position scenario's idiom): three
  approved invoices against one order cross WITHIN → WATCH →
  EXCEEDED with hand-derived cumulatives; a credit applied
  to a member reduces billed (1150.00 back to exactly the
  1000.00 total — WATCH, not EXCEEDED), a released part
  payment against a member reduces it not at all; an
  unapproved invoice and one naming no order contribute
  nothing, and an order no register carries reads UNMEASURED
  with its billed sum, ranked last; the per-record fact
  counts an awaiting invoice in ("billed at 120.0% including
  this invoice"); and on the main service the world's own
  pair — INV-1010 and INV-3001, each billing PO-6010's whole
  600.00 — stands EXCEEDED at 200.0%, the second invoice's
  approval unblocked, its match untouched, and its record
  carrying the decision-time coverage note.
- paid-duplicate watch — the duplicate that arrives second:
  on the main service, an invoice released in a run is
  re-submitted verbatim and held on the paid evidence (run,
  treasurer, the instructed amount), the gate's refusal names
  that evidence, and a controller's release makes the record
  decidable again at a gate that closed when it was
  submitted. In a planted universe (a paid record and its
  released run planted directly, the position scenario's
  idiom), a re-bill at the net figure is held on the NET
  basis, a different amount is surfaced by the number rule
  and not held, a different number at the paid amount is not
  held, resolutions without actor or reason are refused, a
  confirmed hold refuses permanently, a second arrival
  re-holds after a release, and the paid record and its run
  are byte-identical throughout.

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

from reducto_ap_agent.credits import open_amount  # noqa: E402
from reducto_ap_agent.datasets import _doc, _inv_fields, build_world, remittance_advice_doc  # noqa: E402
from reducto_ap_agent.errors import GateError  # noqa: E402
from reducto_ap_agent.graph import ApPipeline  # noqa: E402
from reducto_ap_agent.object_store import InMemoryObjectStore  # noqa: E402
from reducto_ap_agent.offline_docs import OfflineDocumentAdapter  # noqa: E402
from reducto_ap_agent.schemas import (  # noqa: E402
    CreditApplication,
    DocumentRecord,
    ExtractedDocument,
    ExtractedField,
    LineItem,
    PaymentEntry,
    PaymentRun,
    PaymentRunLine,
    RemittanceLine,
    TreasurerDecision,
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

    # -- supplier ledger ---------------------------------------
    # Harborlight's whole relationship, over the main service's
    # end state: three approved invoices — one released in the
    # batch, one released in the confirmation run and answered
    # MISSING, one fully covered by CN-2003 — a second credit
    # still waiting, and the statement whose four breaks no
    # controller has accepted. Plus one stray document from a
    # spelling the master has never seen.
    stray_text = _doc("INVOICE", 1, [
        ("Supplier", "Harbor Light Marine Supply"),
        ("Invoice Number", "INV-2099"),
        ("Invoice Date", "2026-09-14"),
        ("Due Date", "2026-10-14"),
        ("PO Reference", "PO-9998"),
        ("Currency", "USD"),
        ("Bank Account", "US00 HARB 6666 7777 8888"),
        ("Subtotal", "150.00"), ("Tax", "0.00"), ("Total", "150.00"),
    ], [LineItem(sku="SKU-N9", description="Mooring line", quantity=1,
                 unit_price=150.00, amount=150.00)])
    (stray,) = service.submit(
        "SUB-EVAL-STRAY", "sub-eval-stray.pdf",
        stray_text.encode("utf-8"), "text/plain",
    )
    harbor_ledger = service.supplier_ledger("SUP-08", "2026-11-15")
    ledger_index = service.supplier_index("2026-11-15")
    end_position = service.ap_position("2026-11-15")
    (harbor_usd,) = harbor_ledger.totals
    # Harborlight's money is settled by the end state: INV-2001
    # (900.00) and INV-2002 (400.00) were released in runs, so
    # they read as PAID, not open — face 1500.00, credits
    # 200.00 (CN-2003's cover), paid 1300.00, open 0.00. The
    # position discloses them on its fully-paid list; nothing
    # ages, and the ledger's open ties to the position's zero.
    ledger_ties = (
        harbor_ledger.tie_status == "TIED"
        and (harbor_usd.face_total, harbor_usd.credits_total,
             harbor_usd.paid_total, harbor_usd.open_total)
        == (1500.00, 200.00, 1300.00, 0.00)
        and harbor_usd.position_open_total == 0.00
        and {(f.invoice_number, f.paid) for f in end_position.fully_paid
             if f.supplier_name == "Harborlight Marine Supply"}
        == {("INV-2001", 900.00), ("INV-2002", 400.00)}
        and all(
            totals.tied
            for entry in ledger_index.suppliers
            for totals in service.supplier_ledger(
                entry.supplier_id, "2026-11-15").totals
        )
    )
    stray_unmatched = (
        stray.supplier_id is None
        and ledger_index.unmatched_count == 1
        and ledger_index.unmatched[0].record_id == stray.record_id
        and ledger_index.unmatched[0].name_as_written
        == "Harbor Light Marine Supply"
        and all(
            stray.record_id not in [
                d.record_id
                for d in service.supplier_ledger(
                    entry.supplier_id, "2026-11-15").documents
            ]
            for entry in ledger_index.suppliers
        )
    )
    harbor_invoices = {i.invoice_number: i for i in harbor_ledger.invoices}
    harbor_credits = {c.credit_number: c for c in harbor_ledger.credits}
    ledger_story = (
        harbor_invoices["INV-2001"].payment_state == "PAID_PROPOSED"
        and harbor_invoices["INV-2002"].payment_confirmation == "MISSING"
        and harbor_invoices["INV-2003"].fully_credited
        and harbor_invoices["INV-2003"].open == 0.00
        and [c.credit_number for c in
             harbor_invoices["INV-2003"].credits_applied] == ["CN-2003"]
        and harbor_credits["CN-2003"].credit_state == "APPLIED"
        and harbor_credits["CN-2003"].applied_invoice_record_id
        == "REC-SUB-019-0"
        and harbor_credits["CN-2002"].credit_state == "UNAPPLIED"
        and [(b.invoice_number, b.classification)
             for b in harbor_ledger.open_breaks] == [
            ("INV-2002", "amount_disagrees"),
            ("INV-2999", "missing_in_ap"),
            ("INV-2001", "duplicates_within_statement"),
            ("INV-2003", "missing_on_statement"),
        ]
    )
    granite_ledger = service.supplier_ledger("SUP-07", "2026-11-15")
    ledger_bank = (
        granite_ledger.bank.warning is not None
        and "BANK_DETAILS_CHANGED" in granite_ledger.bank.warning
        and granite_ledger.bank.flagged_record_ids == ["REC-SUB-008-0"]
        and granite_ledger.bank.bank_account
        == "US00 GRAN 5555 6666 7777"  # the master's account, only
        and harbor_ledger.bank.warning is None
        and {e.supplier_id: e.bank_warning
             for e in ledger_index.suppliers}["SUP-07"] is True
        and all(
            not e.bank_warning for e in ledger_index.suppliers
            if e.supplier_id != "SUP-07"
        )
    )
    ledger_results = {
        "totals_tie_to_position": ledger_ties,
        "unmatched_listed_nowhere_else": stray_unmatched,
        "story_complete": ledger_story,
        "bank_warning_matches_master": ledger_bank,
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
            "PART_PAID": 0.0,
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

    # -- paid-duplicate watch ----------------------------------
    # Runs last on the main service: INV-1013 (EUR, Larkspur)
    # was approved in the payment-run section and freed by
    # every run that held it, so it can be released on its own
    # here without disturbing any earlier result.
    dup_run = service.create_payment_run(
        "2026-11-13", record_ids=["REC-SUB-013-0"])
    service.decide_payment_run(
        dup_run.run_id, "release", "Eval Treasurer", "eval: batch reviewed")
    sub013_text = next(
        s.text for s in world.submissions if s.submission_id == "SUB-013")
    (dup_resend,) = service.submit(
        "SUB-EVAL-DUP", "sub-eval-dup.pdf",
        sub013_text.encode("utf-8"), "text/plain")
    dup_hold = dup_resend.duplicate_hold
    exact_held = (
        dup_resend.status == "PAID_DUPLICATE_HOLD"
        and dup_hold is not None
        and dup_hold.original_record_id == "REC-SUB-013-0"
        and dup_hold.payment_run_id == dup_run.run_id
        and dup_hold.released_by == "Eval Treasurer"
        and dup_hold.amount_paid == 1080.00
        and dup_hold.paid_face_amount == 1080.00
        and dup_hold.arrived_amount == 1080.00
        and dup_hold.amount_basis == "FACE"
        and dup_hold.currency == "EUR"
    )
    try:
        service.decide(dup_resend.record_id, "approve",
                       "Eval Approver", "eval: pay it again")
        gate_refusal = ""
    except GateError as exc:
        gate_refusal = str(exc)
    service.resolve_duplicate_hold(
        dup_resend.record_id, "RELEASED_NOT_DUPLICATE",
        "Eval Controller",
        "eval: a second site billed under the same number; "
        "this copy is the plant's own bill")
    decided = service.decide(
        dup_resend.record_id, "approve", "Eval Approver",
        "eval: adjudicated not a duplicate")
    released_decidable = (
        "paid-duplicate watch" in gate_refusal
        and dup_run.run_id in gate_refusal
        and decided.status == "APPROVED"
        and decided.decision is not None
        and decided.decision.actor == "Eval Approver"
    )

    # The planted universe: a paid record and its released
    # run, planted directly (the position scenario's idiom),
    # so the face, the applied credit, and the instructed net
    # are exact: face 1000.00, CN-EV-D1 applied 300.00, the
    # run instructed 700.00.
    watch_service = build_offline_service()
    s02 = world.supplier("SUP-02")
    credit_entry = CreditApplication(
        credit_record_id="REC-EV-D1-CR", credit_number="CN-EV-D1",
        invoice_record_id="REC-EV-D1", amount=300.00, currency="USD",
        actor="Eval Controller", at="2026-10-15T10:00:00+00:00")
    watch_service.store.save(DocumentRecord(
        record_id="REC-EV-D1", submission_id="SUB-EV-DUP",
        supplier_id="SUP-02", supplier_name=s02.name,
        status="APPROVED", doc_class="invoice",
        extraction=ExtractedDocument(doc_class="invoice", fields={
            "invoice_number": ExtractedField(value="INV-EV-D1"),
            "total": ExtractedField(value=1000.00),
        }, currency="USD"),
        payment_run_id="RUN-EV-DUP", payment_state="PAID_PROPOSED",
        credits_applied=[credit_entry],
    ))
    watch_service.store.save_payment_run(PaymentRun(
        run_id="RUN-EV-DUP", value_date="2026-10-16", status="RELEASED",
        lines=[PaymentRunLine(
            record_id="REC-EV-D1", supplier_id="SUP-02",
            supplier_name=s02.name, invoice_number="INV-EV-D1",
            currency="USD", amount=700.00, gross_amount=1000.00,
            credits_applied=[credit_entry])],
        totals_by_currency={"USD": 700.00},
        decision=TreasurerDecision(
            decision="release", actor="Eval Treasurer",
            reason="eval: batch reviewed", at="2026-10-16T09:30:00+00:00"),
        created_at="2026-10-16T09:00:00+00:00",
        updated_at="2026-10-16T09:30:00+00:00",
    ))
    twin_before = watch_service.get_record("REC-EV-D1").model_dump_json()
    run_before = watch_service.get_payment_run("RUN-EV-DUP").model_dump_json()

    def dup_text(number: str, total: float) -> str:
        return _doc("INVOICE", 1, _inv_fields(
            s02, number, "PO-6001", total), [
            LineItem(sku="SKU-A1", description="Industrial valves",
                     quantity=10, unit_price=120.00, amount=1200.00),
            LineItem(sku="SKU-A2", description="Gasket sets",
                     quantity=5, unit_price=40.00, amount=200.00),
        ])

    def dup_submit(submission_id: str, number: str, total: float):
        (record,) = watch_service.submit(
            submission_id, f"{submission_id.lower()}.pdf",
            dup_text(number, total).encode("utf-8"), "text/plain")
        return record

    net_resend = dup_submit("SUB-EV-NET", "INV-EV-D1", 700.00)
    net_basis_named = (
        net_resend.status == "PAID_DUPLICATE_HOLD"
        and net_resend.duplicate_hold is not None
        and net_resend.duplicate_hold.amount_basis == "NET"
        and net_resend.duplicate_hold.amount_paid == 700.00
        and net_resend.duplicate_hold.paid_face_amount == 1000.00
        and net_resend.duplicate_hold.released_at
        == "2026-10-16T09:30:00+00:00"
    )
    face_resend = dup_submit("SUB-EV-FACE", "INV-EV-D1", 1000.00)
    diff_resend = dup_submit("SUB-EV-DIFF", "INV-EV-D1", 850.00)
    new_number = dup_submit("SUB-EV-NEWNUM", "INV-EV-D9", 700.00)
    boundaries_hold = (
        face_resend.status == "PAID_DUPLICATE_HOLD"
        and face_resend.duplicate_hold.amount_basis == "FACE"
        and diff_resend.status == "EXCEPTION"
        and "DUPLICATE_INVOICE" in diff_resend.match.exception_codes
        and diff_resend.duplicate_hold is None
        and new_number.status != "PAID_DUPLICATE_HOLD"
        and new_number.duplicate_hold is None
    )

    def resolution_refusal(record_id: str, action: str,
                           actor: str, reason: str) -> str:
        try:
            watch_service.resolve_duplicate_hold(
                record_id, action, actor, reason)
        except GateError as exc:
            return str(exc)
        return ""

    discipline = (
        "requires a reason" in resolution_refusal(
            net_resend.record_id, "CONFIRMED_DUPLICATE",
            "Eval Controller", "")
        and "must name its controller" in resolution_refusal(
            net_resend.record_id, "CONFIRMED_DUPLICATE", "", "a reason")
    )
    confirmed = watch_service.resolve_duplicate_hold(
        net_resend.record_id, "CONFIRMED_DUPLICATE", "Eval Controller",
        "eval: the supplier confirmed the original was paid")
    re_arrival = dup_submit("SUB-EV-NET-2", "INV-EV-D1", 700.00)
    face_released = watch_service.resolve_duplicate_hold(
        face_resend.record_id, "RELEASED_NOT_DUPLICATE",
        "Eval Controller", "eval: adjudicated a different bill")
    resolution_discipline = (
        discipline
        and confirmed.status == "REFUSED"
        and confirmed.duplicate_hold.resolution.action
        == "CONFIRMED_DUPLICATE"
        and re_arrival.status == "PAID_DUPLICATE_HOLD"
        and re_arrival.duplicate_hold.resolution is None
        # A release restores the record's own verdict, it does
        # not launder it: the face resend's stated total never
        # agreed with its own lines, and that exception stands.
        # (The released-and-then-decided path is proven on the
        # main service above, where the resend matched cleanly.)
        and face_released.status == "EXCEPTION"
        and "TOTAL_MISMATCH" in face_released.match.exception_codes
        # The payment's own records never moved.
        and watch_service.get_record("REC-EV-D1").model_dump_json()
        == twin_before
        and watch_service.get_payment_run("RUN-EV-DUP").model_dump_json()
        == run_before
    )
    watch_results = {
        "exact_resend_held_with_evidence": exact_held,
        "released_record_decidable": released_decidable,
        "net_basis_named": net_basis_named,
        "boundaries_hold": boundaries_hold,
        "resolution_discipline": resolution_discipline,
    }

    # -- partial payments --------------------------------------
    # Its own service and planted records (the position
    # scenario's idiom), one invoice paid in parts end to end:
    # INV-EV-P1 (face 2000.00) is taken at 1200.00 by a first
    # run, a 200.00 credit lands on the 800.00 remainder, and a
    # second run takes the 600.00 that is left — 1200 + 600 =
    # 2000 − 200. Every reader must land on those figures.
    part_service = build_offline_service()
    s02 = world.supplier("SUP-02")

    def plant_invoice(record_id, number, total):
        part_service.store.save(DocumentRecord(
            record_id=record_id, submission_id="SUB-EV-PART",
            supplier_id="SUP-02", supplier_name=s02.name,
            status="APPROVED", doc_class="invoice",
            extraction=ExtractedDocument(
                doc_class="invoice", currency="USD",
                fields={
                    "invoice_number": ExtractedField(value=number),
                    "invoice_date": ExtractedField(value="2026-09-01"),
                    "due_date": ExtractedField(value="2026-11-15"),
                    "total": ExtractedField(value=total),
                }),
        ))

    def plant_credit(record_id, number, total, credited):
        part_service.store.save(DocumentRecord(
            record_id=record_id, submission_id="SUB-EV-PART",
            supplier_id="SUP-02", supplier_name=s02.name,
            status="APPROVED", doc_class="credit_note",
            credit_state="UNAPPLIED",
            extraction=ExtractedDocument(
                doc_class="credit_note", currency="USD",
                fields={
                    "invoice_number": ExtractedField(value=number),
                    "total": ExtractedField(value=total),
                    "credited_invoice_number": ExtractedField(
                        value=credited),
                }),
        ))

    def plant_advice(record_id, run_id, rows):
        part_service.store.save(DocumentRecord(
            record_id=record_id, submission_id="SUB-EV-PART",
            supplier_id="SUP-02", supplier_name=s02.name,
            status="APPROVED", doc_class="remittance_advice",
            extraction=ExtractedDocument(
                doc_class="remittance_advice", currency="USD",
                fields={
                    "payment_run_id": ExtractedField(value=run_id),
                },
                remittance_lines=[
                    RemittanceLine(
                        invoice_number=number, amount_paid=amount,
                        currency="USD", value_date="2026-11-20",
                        bank_reference=ref)
                    for number, amount, ref in rows
                ]),
        ))

    plant_invoice("REC-EV-P1", "INV-EV-P1", 2000.00)
    plant_invoice("REC-EV-P2", "INV-EV-P2", 500.00)
    plant_invoice("REC-EV-P3", "INV-EV-P3", 100.00)
    plant_credit("REC-EV-PC1", "CN-EV-P1", -200.00, "INV-EV-P1")
    plant_credit("REC-EV-PC2", "CN-EV-P2", -900.00, "INV-EV-P1")

    part_run1 = part_service.create_payment_run(
        "2026-11-20", record_ids=["REC-EV-P1", "REC-EV-P2"],
        amounts={"REC-EV-P1": 1200.00})
    part_lines = {l.record_id: l for l in part_run1.lines}
    part_service.decide_payment_run(
        part_run1.run_id, "release", "Eval Treasurer", "eval: first part")
    p1_after_part = part_service.get_record("REC-EV-P1")
    first_part_stands = (
        part_lines["REC-EV-P1"].amount == 1200.00
        and part_lines["REC-EV-P1"].open_amount == 2000.00
        and part_lines["REC-EV-P2"].amount == 500.00
        and part_run1.totals_by_currency == {"USD": 1700.00}
        and p1_after_part.payment_state == "PART_PAID"
        and [e.amount for e in p1_after_part.payments_applied] == [1200.00]
        and part_service.get_record("REC-EV-P2").payment_state
        == "PAID_PROPOSED"
    )

    mid_position = part_service.ap_position("2026-11-25")
    (mid_row,) = [r for r in mid_position.suppliers
                  if r.supplier_id == "SUP-02"]
    (mid_line,) = [l for l in mid_row.lines
                   if l.record_id == "REC-EV-P1"]
    remainder_ages = (
        mid_line.open == 800.00
        and mid_line.paid == 1200.00
        and mid_line.state == "PART_PAID"
        and mid_line.bucket == "1-30"  # due 11-15, as of 11-25
        and mid_position.paid_totals_by_currency["USD"] == 1700.00
        and [f.invoice_number for f in mid_position.fully_paid]
        == ["INV-EV-P2"]
        and mid_position.tie_status == "TIED"
        and mid_position.splits_agree
    )

    # A resend at the part figure, while only the part is paid:
    # held on the cumulative evidence of one run.
    def part_resend(submission_id: str, total: float):
        text = _doc("INVOICE", 1, _inv_fields(
            s02, "INV-EV-P1", "PO-6001", total), [
            LineItem(sku="SKU-A1", description="Industrial valves",
                     quantity=10, unit_price=120.00, amount=1200.00),
            LineItem(sku="SKU-A2", description="Gasket sets",
                     quantity=5, unit_price=40.00, amount=200.00),
        ])
        (record,) = part_service.submit(
            submission_id, f"{submission_id.lower()}.pdf",
            text.encode("utf-8"), "text/plain")
        return record

    part_resend_rec = part_resend("SUB-EV-PART-RESEND", 1200.00)
    held_on_the_part = (
        part_resend_rec.status == "PAID_DUPLICATE_HOLD"
        and part_resend_rec.duplicate_hold.amount_basis == "NET"
        and part_resend_rec.duplicate_hold.amount_paid == 1200.00
        and part_resend_rec.duplicate_hold.payment_run_id
        == part_run1.run_id
    )

    # The cumulative overcommit: 900.00 more against an 800.00
    # remainder is refused by name, committed figure quoted.
    greedy = part_service.create_payment_run(
        "2026-11-27", record_ids=["REC-EV-P1", "REC-EV-P3"],
        amounts={"REC-EV-P1": 900.00})
    greedy_refusal = {r.record_id: r for r in greedy.refusals}["REC-EV-P1"]
    overcommit_refused = (
        greedy_refusal.code == "PART_EXCEEDS_OPEN"
        and "900.00" in greedy_refusal.reason
        and "800.00" in greedy_refusal.reason
        and "1200.00" in greedy_refusal.reason
        and [l.record_id for l in greedy.lines] == ["REC-EV-P3"]
    )
    part_service.decide_payment_run(
        greedy.run_id, "return", "Eval Treasurer", "eval: reset")

    # The credit lands on the remainder; the bigger one is
    # judged against the remainder's figures, not the face.
    part_service.apply_credit("REC-EV-PC1", "Eval Controller")
    credit_on_remainder = (
        open_amount(part_service.get_record("REC-EV-P1")) == 600.00
    )
    try:
        part_service.apply_credit("REC-EV-PC2", "Eval Controller")
        over_credit_message = ""
    except GateError as exc:
        over_credit_message = str(exc)
    over_credit_on_remainder = (
        over_credit_message.startswith("OVER_CREDIT")
        and "900.00" in over_credit_message
        and "600.00" in over_credit_message
        and part_service.get_record("REC-EV-PC2").credit_state
        == "UNAPPLIED"
    )

    # Confirmations answer the first run's line: the part
    # confirms exactly; the full face is an over-payment break
    # on that run and confirms nothing about the remainder.
    plant_advice("REC-EV-ADV-P1", part_run1.run_id,
                 [("INV-EV-P1", 1200.00, "BNK-EV-P1")])
    part_conf = part_service.confirm_remittance("REC-EV-ADV-P1")
    plant_advice("REC-EV-ADV-P2", part_run1.run_id,
                 [("INV-EV-P1", 2000.00, "BNK-EV-P2")])
    face_conf = part_service.confirm_remittance("REC-EV-ADV-P2")
    part_classes = {l.invoice_number: l.classification
                    for l in part_conf.lines}
    face_classes = {l.invoice_number: l.classification
                    for l in face_conf.lines}
    confirmation_answers_the_line = (
        part_classes["INV-EV-P1"] == "paid_as_proposed"
        and face_classes["INV-EV-P1"] == "over_paid"
        and open_amount(part_service.get_record("REC-EV-P1")) == 600.00
    )

    part_run2 = part_service.create_payment_run(
        "2026-12-04", record_ids=["REC-EV-P1"],
        amounts={"REC-EV-P1": 600.00})
    part_service.decide_payment_run(
        part_run2.run_id, "release", "Eval Treasurer", "eval: the rest")
    p1_done = part_service.get_record("REC-EV-P1")
    export1 = part_service.payment_run_export(part_run1.run_id).decode()
    export2 = part_service.payment_run_export(part_run2.run_id).decode()
    row1 = next(r for r in export1.splitlines() if "REC-EV-P1" in r)
    row2 = next(r for r in export2.splitlines() if "REC-EV-P1" in r)
    paydown_ties = (
        first_part_stands
        and p1_done.payment_state == "PAID_PROPOSED"
        and [e.amount for e in p1_done.payments_applied]
        == [1200.00, 600.00]
        and open_amount(p1_done) == 0.00
        and row1.rstrip().endswith(",1200.00")
        and row2.rstrip().endswith(",600.00")
    )

    end_position = part_service.ap_position("2026-12-10")
    end_ledger = part_service.supplier_ledger("SUP-02", "2026-12-10")
    end_invoice = next(
        i for i in end_ledger.invoices if i.record_id == "REC-EV-P1")
    readers_agree = (
        remainder_ages
        and [(f.invoice_number, f.face, f.credits, f.paid)
             for f in end_position.fully_paid
             if f.record_id == "REC-EV-P1"]
        == [("INV-EV-P1", 2000.00, 200.00, 1800.00)]
        and end_position.paid_totals_by_currency["USD"] == 2300.00
        and end_position.tie_status == "TIED"
        and (end_invoice.face, end_invoice.credits_total,
             end_invoice.paid_total, end_invoice.open)
        == (2000.00, 200.00, 1800.00, 0.00)
        and end_ledger.tie_status == "TIED"
    )

    # After both runs, the watch's evidence is the two runs
    # summed, named by the most recent.
    net_resend_rec = part_resend("SUB-EV-PART-RESEND-NET", 1800.00)
    face_resend_rec = part_resend("SUB-EV-PART-RESEND-FACE", 2000.00)
    watch_cumulative = (
        held_on_the_part
        and net_resend_rec.status == "PAID_DUPLICATE_HOLD"
        and net_resend_rec.duplicate_hold.amount_basis == "NET"
        and net_resend_rec.duplicate_hold.amount_paid == 1800.00
        and net_resend_rec.duplicate_hold.payment_run_id
        == part_run2.run_id
        and face_resend_rec.status == "PAID_DUPLICATE_HOLD"
        and face_resend_rec.duplicate_hold.amount_basis == "FACE"
        and face_resend_rec.duplicate_hold.amount_paid == 1800.00
    )
    partial_results = {
        "two_run_paydown_ties": paydown_ties,
        "overcommit_refused_with_figures": overcommit_refused,
        "credit_lands_on_remainder": credit_on_remainder,
        "over_credit_judged_on_remainder": over_credit_on_remainder,
        "position_remainder_ages": remainder_ages,
        "readers_agree_at_the_end": readers_agree,
        "confirmation_answers_the_part_line":
            confirmation_answers_the_line,
        "watch_holds_on_cumulative_evidence": watch_cumulative,
    }

    # -- PO coverage ------------------------------------------
    # Its own service and planted records (the position
    # scenario's idiom), so the cumulatives against PO-6003
    # (Dunmore, register total 1000.00) are exact: 500.00
    # WITHIN, +350.00 WATCH at 85%, +300.00 EXCEEDED at 115%.
    coverage_service = build_offline_service()
    s04 = world.supplier("SUP-04")

    def cov_plant(record_id, number, po, total, *,
                  status="APPROVED", currency="USD"):
        fields = {
            "invoice_number": ExtractedField(value=number),
            "invoice_date": ExtractedField(value="2026-09-01"),
            "total": ExtractedField(value=total),
        }
        if po is not None:
            fields["po_reference"] = ExtractedField(value=po)
        coverage_service.store.save(DocumentRecord(
            record_id=record_id, submission_id="SUB-EV-COV",
            supplier_id="SUP-04", supplier_name=s04.name,
            status=status, doc_class="invoice",
            extraction=ExtractedDocument(
                doc_class="invoice", fields=fields, currency=currency),
        ))

    def cov_row(po_number):
        return next(
            r for r in coverage_service.po_coverage().orders
            if r.po_number == po_number
        )

    cov_plant("REC-EV-K1", "INV-EV-K1", "PO-6003", 500.00)
    stage_within = cov_row("PO-6003")
    cov_plant("REC-EV-K2", "INV-EV-K2", "PO-6003", 350.00)
    stage_watch = cov_row("PO-6003")
    cov_plant("REC-EV-K3", "INV-EV-K3", "PO-6003", 300.00)
    stage_exceeded = cov_row("PO-6003")
    cumulative_states = (
        (stage_within.billed, stage_within.pct, stage_within.state)
        == (500.00, 50.0, "WITHIN")
        and (stage_watch.billed, stage_watch.pct, stage_watch.state)
        == (850.00, 85.0, "WATCH")
        and (stage_exceeded.billed, stage_exceeded.pct,
             stage_exceeded.state) == (1150.00, 115.0, "EXCEEDED")
        and [l.cumulative_billed for l in stage_exceeded.lines]
        == [500.00, 850.00, 1150.00]
        and [l.invoice_number for l in stage_exceeded.lines]
        == ["INV-EV-K1", "INV-EV-K2", "INV-EV-K3"]
        and stage_exceeded.total == 1000.00
        and stage_exceeded.total_source == "ORDER_REGISTER"
    )

    # A credit lands on the second invoice: billed falls by
    # exactly the credit, back to the order's total — fully
    # billed (WATCH at 100.0%), no longer exceeded. A released
    # part payment against the first invoice settles money
    # without un-billing any of it.
    k2 = coverage_service.get_record("REC-EV-K2")
    k2.credits_applied.append(CreditApplication(
        credit_record_id="REC-EV-K2-CR", credit_number="CN-EV-K2",
        invoice_record_id="REC-EV-K2", amount=150.00,
        currency="USD", actor="Eval Controller"))
    coverage_service.store.save(k2)
    after_credit = cov_row("PO-6003")
    k1 = coverage_service.get_record("REC-EV-K1")
    k1.payments_applied.append(PaymentEntry(
        run_id="RUN-EV-K1", amount=400.00, currency="USD",
        actor="Eval Treasurer"))
    coverage_service.store.save(k1)
    after_payment = cov_row("PO-6003")
    credits_reduce_payments_dont = (
        (after_credit.billed, after_credit.pct, after_credit.state)
        == (1000.00, 100.0, "WATCH")
        and after_payment.billed == 1000.00
        and [l.billable for l in after_payment.lines]
        == [500.00, 200.00, 300.00]
    )

    # Money that is not approved bills nothing, an invoice
    # naming no order bills nothing anywhere, and an order
    # the register does not carry is measured against
    # nothing: UNMEASURED, its billed sum listed, ranked last.
    cov_plant("REC-EV-K4", "INV-EV-K4", "PO-6003", 200.00,
              status="AWAITING_APPROVER")
    cov_plant("REC-EV-K5", "INV-EV-K5", None, 9999.00)
    cov_plant("REC-EV-K6", "INV-EV-K6", "PO-9997", 275.00)
    end_report = coverage_service.po_coverage()
    unmeasured = cov_row("PO-9997")
    unapproved_and_unmeasured = (
        cov_row("PO-6003").billed == 1000.00
        and unmeasured.state == "UNMEASURED"
        and unmeasured.total is None and unmeasured.pct is None
        and unmeasured.billed == 275.00
        and end_report.orders[-1].po_number == "PO-9997"
    )

    # The fact for the awaiting invoice counts it in: the
    # approver reads 120.0% — EXCEEDED — before deciding.
    k4_fact = coverage_service.record_coverage("REC-EV-K4")
    record_fact = (
        k4_fact.counted is False
        and (k4_fact.billed, k4_fact.billed_including)
        == (1000.00, 1200.00)
        and (k4_fact.pct, k4_fact.state) == (120.0, "EXCEEDED")
        and "120.0%" in k4_fact.fact
        and "including this invoice" in k4_fact.fact
    )

    # Ranking: an order billed to four times its total leads;
    # the unmeasured order closes the list.
    cov_plant("REC-EV-K7", "INV-EV-K7", "PO-6004", 1200.00)
    ranked = coverage_service.po_coverage().orders
    ranking_holds = (
        ranked[0].po_number == "PO-6004"
        and ranked[0].state == "EXCEEDED"
        and ranked[0].pct == 400.0
        and ranked[-1].po_number == "PO-9997"
        and next(i for i, r in enumerate(ranked)
                 if r.po_number == "PO-6003")
        < next(i for i, r in enumerate(ranked)
               if r.state == "WITHIN")
    )

    # On the main service, the world's own pair: INV-1010 and
    # INV-3001 each bill PO-6010's whole 600.00. The second
    # approval (recorded in the credit section above) was
    # taken with the order at 200.0% — unblocked, its match
    # untouched, and the fact on its record.
    main_row = next(
        r for r in service.po_coverage().orders
        if r.po_number == "PO-6010"
    )
    inv3001 = service.get_record("REC-SUB-030-0")
    crossing_informs = (
        (main_row.billed, main_row.pct, main_row.state)
        == (1200.00, 200.0, "EXCEEDED")
        and [(l.record_id, l.billable, l.cumulative_billed)
             for l in main_row.lines]
        == [("REC-SUB-010-0", 600.00, 600.00),
            ("REC-SUB-030-0", 600.00, 1200.00)]
        and inv3001.status == "APPROVED"
        and inv3001.match is not None
        and inv3001.match.status == "MATCHED"
        and any(
            a["event"] == "po_coverage_at_decision"
            and "PO-6010" in a["detail"] and "200.0%" in a["detail"]
            for a in inv3001.audit
        )
    )
    coverage_results = {
        "cumulative_states_exact": cumulative_states,
        "credits_reduce_payments_do_not": credits_reduce_payments_dont,
        "unapproved_excluded_unmeasured_listed":
            unapproved_and_unmeasured,
        "record_fact_counts_the_invoice": record_fact,
        "ranking_worst_first_unmeasured_last": ranking_holds,
        "crossing_informs_never_blocks": crossing_informs,
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
        "supplier_ledger": ledger_results,
        "paid_duplicate_watch": watch_results,
        "partial_payments": partial_results,
        "po_coverage": coverage_results,
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
        "ledger_totals_tie_to_position":
            ledger_results["totals_tie_to_position"],
        "ledger_unmatched_listed_nowhere_else":
            ledger_results["unmatched_listed_nowhere_else"],
        "ledger_story_complete": ledger_results["story_complete"],
        "ledger_bank_warning_matches_master":
            ledger_results["bank_warning_matches_master"],
        "duplicate_watch_holds_the_paid_resend":
            watch_results["exact_resend_held_with_evidence"],
        "duplicate_watch_released_record_decidable":
            watch_results["released_record_decidable"],
        "duplicate_watch_net_basis_and_boundaries": (
            watch_results["net_basis_named"]
            and watch_results["boundaries_hold"]
        ),
        "duplicate_watch_resolution_discipline":
            watch_results["resolution_discipline"],
        "partial_payment_paydown_ties":
            partial_results["two_run_paydown_ties"],
        "partial_payment_overcommit_refused":
            partial_results["overcommit_refused_with_figures"],
        "partial_payment_credit_on_remainder": (
            partial_results["credit_lands_on_remainder"]
            and partial_results["over_credit_judged_on_remainder"]
        ),
        "partial_payment_position_and_ledger_agree": (
            partial_results["position_remainder_ages"]
            and partial_results["readers_agree_at_the_end"]
        ),
        "partial_payment_confirmation_and_watch": (
            partial_results["confirmation_answers_the_part_line"]
            and partial_results["watch_holds_on_cumulative_evidence"]
        ),
        "coverage_cumulative_states_exact":
            coverage_results["cumulative_states_exact"],
        "coverage_credits_reduce_payments_do_not":
            coverage_results["credits_reduce_payments_do_not"],
        "coverage_unapproved_excluded_unmeasured_listed":
            coverage_results["unapproved_excluded_unmeasured_listed"],
        "coverage_record_fact_counts_the_invoice":
            coverage_results["record_fact_counts_the_invoice"],
        "coverage_ranking_worst_first_unmeasured_last":
            coverage_results["ranking_worst_first_unmeasured_last"],
        "coverage_crossing_informs_never_blocks":
            coverage_results["crossing_informs_never_blocks"],
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
