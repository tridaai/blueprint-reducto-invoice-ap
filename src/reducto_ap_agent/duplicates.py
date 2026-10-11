"""The paid-duplicate watch: the duplicate that arrives second.

The pipeline's duplicate check (graph.py) compares an arrival
against the documents in the case with it: same supplier, same
invoice number — a co-pending copy is branded DUPLICATE_INVOICE
and goes back to the sender as an exception. That check is
amount-blind and payment-blind, and an exception is a dead
end: nothing about it says whether the twin was ever paid,
and nothing lets a human rule on it.

The dangerous duplicate is a different animal. It arrives
weeks after the original was approved, assembled into a
payment run, and released to the bank: a re-sent PDF, a
"corrected" reissue, the same bill keyed twice by the
supplier's own AP. It extracts cleanly and matches its PO —
nothing pending resembles it, because its twin is no longer
pending; it is paid. Left alone it would be approved and
paid a second time, and the first anyone would know is the
supplier's next statement.

This module is the watch against that arrival, run at intake
(graph.py calls it the moment an invoice's extraction
completes, before the record can reach the gate):

- **The keys are the repo's own identity discipline.** Same
  supplier — the intake's resolved ``supplier_id``, never a
  resemblance. Same invoice number, normalised the way
  credits and confirmations normalise (strip, upper-case) —
  the older pipeline check compares raw strings, so a resend
  whose number was read with different case or spacing slips
  it; the watch does not. And an amount relationship, below.
- **The history it checks is paid history.** A twin counts
  when RELEASED runs have instructed money for it — in full
  (PAID_PROPOSED) or in part (PART_PAID): a part-paid
  original is paid history too, because the parts the runs
  instructed are exactly the money a resend would take
  twice. A twin that is merely approved, or held in an open
  run, is the co-pending case the pipeline's own check
  already owns.
- **The amount must be the paid invoice's own figure, to the
  cent.** Exact equality — no tolerance anywhere near this:
  the match policy's tolerances absorb document-reading
  noise, and a duplicate judgement is not a reading, it is
  an accusation (the rule confirmations set in round 4).
  Two bases, named on the hold: FACE — the arrival states the
  original's approved face; NET — the arrival states the
  total the released runs actually instructed across every
  part payment, each run's line summed (for one whole
  payment, the net figure that run instructed after the
  credits applied before release). A supplier who was
  credited and re-bills at the net figure is the classic
  second arrival, and the hold says which figure it matched.
  The evidence is read from the runs' lines themselves —
  what was instructed, when, is history the invoice's later
  credits (which may land on a part-paid remainder) cannot
  rewrite. An arrival at any other amount is NOT held: it is
  surfaced by the pipeline's number rule as a DUPLICATE_INVOICE
  exception, exactly as before — a different amount is a
  different claim, and it is a human's exception, not this
  watch's hold.
- **A hit is a hold, never a silent drop and never a gate
  decision.** The record stops at PAID_DUPLICATE_HOLD with
  the paid evidence on its face — the original record, the
  run, who released it and when, the amount instructed. The
  approver's gate refuses to decide it (409, the FRAUD_REVIEW
  precedent), and a payment run refuses it by name. Only a
  named controller resolves it, with a reason, in one of two
  ways: CONFIRMED_DUPLICATE — the record is refused
  permanently; RELEASED_NOT_DUPLICATE — the hold lifts and
  the record returns to the state its own match earned. The
  resolution rides the record permanently.
- **Identity is the record, not the number.** A release
  adjudicates ONE document. A second arrival of the same
  document is a new record, checked afresh against the same
  paid history, and held again — a release cannot be
  laundered by resending. Nor does a release bless the
  number: the next legitimate bill under it is judged on its
  own arrival.
- **What the watch never does.** No clawback, no recovery
  workflow, no touching the released run, its export, or the
  original payment: the original record and the run are
  byte-identical across any hold and resolution (a test pins
  it). The watch is also only about invoice-class arrivals —
  the paid invoice's own credit notes and remittance advices
  keep behaving exactly as credits.py and confirmations.py
  built them; they reduce and answer the payment, they never
  re-present it. And a bank-change resend is a fraud case
  first: the intake computes this check only for arrivals
  carrying no fraud flags, so the watch never competes with
  the fraud hold for a record.
"""

from __future__ import annotations

from datetime import date

from .schemas import DocumentRecord, PaidDuplicateHold, PaymentRun


def _normalise(number: str) -> str:
    """The repo's invoice-number normalisation — strip and
    upper-case, the same two operations credits.py and
    confirmations.py apply before comparing numbers."""
    return str(number).strip().upper()


def _cents(amount: float) -> float:
    return round(float(amount), 2)


def find_paid_duplicate(
    record: DocumentRecord,
    records: list[DocumentRecord],
    runs: list[PaymentRun],
    *,
    at: str = "",
) -> PaidDuplicateHold | None:
    """The paid evidence against ``record``, or None.

    Pure: reads the store snapshot the caller passes, writes
    nothing. The caller (the pipeline) owns the consequences —
    attaching the hold and stopping the record. Twins are
    scanned in store order and the first paid twin is the
    evidence: the earliest payment is the one a second payment
    would duplicate."""
    if record.doc_class != "invoice" or record.extraction is None:
        return None
    if record.supplier_id is None:
        return None
    number = record.extraction.value("invoice_number")
    if not number:
        return None
    arrived = _cents(float(record.extraction.value("total") or 0.0))
    for other in records:
        if other.record_id == record.record_id:
            continue
        if other.doc_class != "invoice" or other.extraction is None:
            continue
        if other.supplier_id != record.supplier_id:
            continue
        if _normalise(str(other.extraction.value("invoice_number") or "")) \
                != _normalise(str(number)):
            continue
        # The twin's paid history, read off the runs themselves:
        # every RELEASED run whose lines instruct money for this
        # record, and the total they instructed. The runs are
        # scanned in store order, so the last paying run is the
        # most recent instruction — the one the evidence names.
        paying: list[tuple[PaymentRun, float, str]] = []
        for run in runs:
            if run.status != "RELEASED":
                continue
            for line in run.lines:
                if line.record_id == other.record_id:
                    paying.append(
                        (run, _cents(line.amount), line.currency))
        if not paying:
            continue
        run, _, currency = paying[-1]
        paid = _cents(sum(amount for _, amount, _ in paying))
        face = _cents(float(other.extraction.value("total") or 0.0))
        if arrived == face:
            basis = "FACE"
        elif arrived == paid and paid != face:
            basis = "NET"
        else:
            continue
        return PaidDuplicateHold(
            original_record_id=other.record_id,
            invoice_number=str(number),
            supplier_name=other.supplier_name,
            payment_run_id=run.run_id,
            released_at=run.decision.at if run.decision else "",
            released_by=run.decision.actor if run.decision else "",
            amount_paid=paid,
            paid_face_amount=face,
            currency=currency,
            arrived_amount=arrived,
            amount_basis=basis,  # type: ignore[arg-type]
            held_at=at,
        )
    return None


def evidence_summary(hold: PaidDuplicateHold) -> str:
    """The hold's evidence in one sentence — the words the
    audit trail, the gate's refusal, and the console all use,
    so a human meets the same account of the payment
    everywhere."""
    return (
        f"invoice {hold.invoice_number} was already released for "
        f"payment as {hold.original_record_id} — "
        f"{hold.amount_paid:.2f} {hold.currency} instructed "
        f"across released runs, most recently payment run "
        f"{hold.payment_run_id} (released {hold.released_at} by "
        f"{hold.released_by}); this arrival states "
        f"{hold.arrived_amount:.2f} — the {hold.amount_basis} figure"
    )


def hold_age_days(hold: PaidDuplicateHold, as_of: str) -> int | None:
    """Whole days the hold has stood, as of ``as_of`` (an ISO
    date) — the age a watch list shows. None when the hold
    carries no timestamp to age from; never negative (an
    as-of before the hold date is a clock artefact, not an
    age)."""
    if not hold.held_at:
        return None
    try:
        held = date.fromisoformat(hold.held_at[:10])
        aged = date.fromisoformat(str(as_of).strip()[:10])
    except ValueError:
        return None
    return max((aged - held).days, 0)


def watch_entry(record: DocumentRecord, as_of: str) -> dict:
    """One held record as the watch list serves it: the record
    itself, plus its hold's age at ``as_of`` — the enrichment
    idiom ``list_credits`` set for read surfaces."""
    view = record.model_dump()
    view["hold_age_days"] = (
        hold_age_days(record.duplicate_hold, as_of)
        if record.duplicate_hold is not None else None
    )
    return view
