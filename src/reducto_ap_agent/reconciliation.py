"""Supplier statement reconciliation, computed in code.

A supplier statement is a claim: *these are the invoices we
believe you still owe us for.* Reconciliation is the discipline
of answering it line by line from AP's own records — no model,
no impression, just the join and the arithmetic:

- ``matched`` — the invoice is on file and the amounts agree
  within the match policy's total tolerance (policy.py's
  tolerances, reused — a reconciled amount and a matched amount
  are the same judgement, so they share the one policy);
- ``amount_disagrees`` — on file, but the figures differ beyond
  tolerance. Both figures ride the line; the break is the two
  numbers, not a verdict;
- ``missing_in_ap`` — the statement lists an invoice AP has no
  live record of: never received, never scanned, or filed under
  another number. The classic break, and the one that stops a
  dunning letter from becoming a dispute;
- ``missing_on_statement`` — AP holds a live invoice the
  statement never mentions. Shown because AP may be about to
  pay something the supplier no longer claims — credited,
  cancelled, or simply their error; either way a human should
  see it before the money moves;
- ``duplicates_within_statement`` — the same invoice listed
  twice on one statement. The first occurrence reconciles
  normally; the repeats are the break.

The statement's own arithmetic is checked too: its stated total
must agree with the sum of its lines (same total tolerance),
or the break is named on the reconciliation as
``STATEMENT_TOTAL_MISMATCH``.

A reconciliation is evidence, not action: computing one reads
document records and writes only its own record. It never
approves, alters, or excludes an invoice — the payment packet
cannot tell that a reconciliation happened.
"""

from __future__ import annotations

from .matching import _total_within_tolerance
from .policy import DEFAULT_POLICY, MatchPolicy
from .schemas import DocumentRecord, ReconLine, Reconciliation

RECON_CLASSES = (
    "matched",
    "amount_disagrees",
    "missing_in_ap",
    "missing_on_statement",
    "duplicates_within_statement",
)

# AP records that count as "held by AP" for reconciliation: the
# invoice arrived and AP has not finally disposed of it. A
# REJECTED invoice is disposed — the supplier was told no — so
# it does not make a statement line "present", and its absence
# from a statement is not a break. Everything else (awaiting the
# gate, approved into the packet, exception, fraud hold) is a
# live amount AP is acting on.
LIVE_AP_STATUSES = frozenset(
    {"RECEIVED", "AWAITING_APPROVER", "APPROVED", "EXCEPTION", "FRAUD_REVIEW"}
)


def _normalise(number: str) -> str:
    return str(number).strip().upper()


def live_invoice_records(records: list[DocumentRecord]) -> list[DocumentRecord]:
    """The AP side of the join: live invoice records, in store
    order, so 'the record' for a duplicated number is the first
    one AP processed."""
    return [
        rec
        for rec in records
        if rec.doc_class == "invoice"
        and rec.status in LIVE_AP_STATUSES
        and rec.extraction is not None
    ]


def reconcile_statement(
    reconciliation_id: str,
    statement: DocumentRecord,
    ap_records: list[DocumentRecord],
    *,
    policy: MatchPolicy = DEFAULT_POLICY,
    created_at: str = "",
) -> Reconciliation:
    """Reconcile one statement record against AP records.

    ``ap_records`` is every record for the statement's supplier
    (any class, any status) — the live-invoice filter is applied
    here so callers cannot widen or narrow the join by accident.
    """
    extraction = statement.extraction
    assert extraction is not None  # the service validates before calling
    statement_lines = extraction.statement_lines
    currency = extraction.currency

    live = live_invoice_records(ap_records)
    ap_by_number: dict[str, DocumentRecord] = {}
    for rec in live:
        number = _normalise(str(rec.extraction.value("invoice_number") or ""))
        if number and number not in ap_by_number:
            ap_by_number[number] = rec

    lines: list[ReconLine] = []
    seen: set[str] = set()
    for sline in statement_lines:
        number = _normalise(sline.invoice_number)
        ap_rec = ap_by_number.get(number)
        ap_amount = (
            float(ap_rec.extraction.value("total") or 0.0) if ap_rec is not None else None
        )
        if number in seen:
            lines.append(ReconLine(
                invoice_number=sline.invoice_number,
                classification="duplicates_within_statement",
                statement_amount=sline.amount,
                ap_amount=ap_amount,
                currency=sline.currency,
                ap_record_id=ap_rec.record_id if ap_rec is not None else None,
                invoice_date=sline.invoice_date,
                note=(
                    f"{sline.invoice_number} appears more than once on this "
                    "statement; the first occurrence carries the reconciliation."
                ),
            ))
            continue
        seen.add(number)
        if ap_rec is None:
            lines.append(ReconLine(
                invoice_number=sline.invoice_number,
                classification="missing_in_ap",
                statement_amount=sline.amount,
                currency=sline.currency,
                invoice_date=sline.invoice_date,
                note=(
                    "On the statement but AP holds no live record of this "
                    "invoice — never received, or filed under another number."
                ),
            ))
        elif sline.currency != ap_rec.extraction.currency:
            lines.append(ReconLine(
                invoice_number=sline.invoice_number,
                classification="amount_disagrees",
                statement_amount=sline.amount,
                ap_amount=ap_amount,
                currency=sline.currency,
                ap_record_id=ap_rec.record_id,
                invoice_date=sline.invoice_date,
                note=(
                    f"Currency differs: statement says {sline.currency}, "
                    f"AP's record is in {ap_rec.extraction.currency} — "
                    "the amounts are not comparable until that is resolved."
                ),
            ))
        elif _total_within_tolerance(sline.amount, ap_amount or 0.0, policy):
            lines.append(ReconLine(
                invoice_number=sline.invoice_number,
                classification="matched",
                statement_amount=sline.amount,
                ap_amount=ap_amount,
                currency=sline.currency,
                ap_record_id=ap_rec.record_id,
                invoice_date=sline.invoice_date,
                note="Amounts agree within the match policy's total tolerance.",
            ))
        else:
            lines.append(ReconLine(
                invoice_number=sline.invoice_number,
                classification="amount_disagrees",
                statement_amount=sline.amount,
                ap_amount=ap_amount,
                currency=sline.currency,
                ap_record_id=ap_rec.record_id,
                invoice_date=sline.invoice_date,
                note=(
                    f"Statement claims {sline.amount:.2f} but AP's record "
                    f"totals {ap_amount:.2f} — outside the total tolerance."
                ),
            ))

    statement_numbers = {_normalise(sl.invoice_number) for sl in statement_lines}
    for rec in live:
        number = _normalise(str(rec.extraction.value("invoice_number") or ""))
        if number and number not in statement_numbers:
            lines.append(ReconLine(
                invoice_number=str(rec.extraction.value("invoice_number")),
                classification="missing_on_statement",
                ap_amount=float(rec.extraction.value("total") or 0.0),
                currency=rec.extraction.currency,
                ap_record_id=rec.record_id,
                invoice_date=str(rec.extraction.value("invoice_date") or ""),
                note=(
                    f"AP holds this invoice (status {rec.status}) but the "
                    "statement does not list it — the supplier may have "
                    "credited or cancelled it, or omitted it in error."
                ),
            ))

    counts = {name: 0 for name in RECON_CLASSES}
    totals = {name: 0.0 for name in RECON_CLASSES}
    for line in lines:
        counts[line.classification] += 1
        figure = (
            line.ap_amount
            if line.classification == "missing_on_statement"
            else line.statement_amount
        )
        totals[line.classification] = round(
            totals[line.classification] + (figure or 0.0), 2
        )

    flags: list[str] = []
    notes: list[str] = []
    lines_sum = round(sum(sl.amount for sl in statement_lines), 2)
    stated_raw = extraction.value("total")
    stated = float(stated_raw) if stated_raw is not None else None
    if stated is None:
        totals_tie = False
        flags.append("STATEMENT_TOTAL_MISSING")
        notes.append(
            "The statement states no total of its own; its arithmetic "
            "cannot be tied out."
        )
    elif _total_within_tolerance(lines_sum, stated, policy):
        totals_tie = True
    else:
        totals_tie = False
        flags.append("STATEMENT_TOTAL_MISMATCH")
        notes.append(
            f"The statement's stated total ({stated:.2f}) disagrees with "
            f"the sum of its own lines ({lines_sum:.2f}) — the break is "
            "in the statement's arithmetic, before anything is compared "
            "to AP."
        )

    period = str(extraction.value("period") or "")
    if not period:
        statement_date = str(extraction.value("statement_date") or "")
        period = statement_date[:7] if statement_date else ""

    return Reconciliation(
        reconciliation_id=reconciliation_id,
        statement_record_id=statement.record_id,
        submission_id=statement.submission_id,
        supplier_id=statement.supplier_id or "",
        supplier_name=statement.supplier_name,
        period=period,
        currency=currency,
        status="AWAITING_CONTROLLER",
        lines=lines,
        counts=counts,
        totals=totals,
        statement_total_stated=stated,
        statement_total_lines=lines_sum,
        totals_tie=totals_tie,
        flags=flags,
        notes=notes,
        audit=[{"event": "reconciled", "at": created_at}],
        created_at=created_at,
        updated_at=created_at,
    )
