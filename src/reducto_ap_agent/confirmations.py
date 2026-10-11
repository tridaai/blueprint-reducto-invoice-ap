"""Remittance confirmation, computed in code.

A released payment run is a proposal: ``PAID_PROPOSED`` says
what the treasurer released, not what the bank did. What the
bank did comes back as a remittance advice — paid lines against
the run's invoices — and confirmation is the discipline of
answering it line by line against the run, the way statement
reconciliation answers a supplier:

- ``paid_as_proposed`` — the advice names the member and the
  money agrees **exactly**;
- ``short_paid`` — less arrived than proposed (or the money
  arrived in a different currency, in which case nothing
  arrived in the currency that was proposed);
- ``over_paid`` — more arrived than proposed;
- ``missing`` — the run was released and no advice line names
  the member;
- ``unexpected_line`` — an advice line names an invoice that
  is not a member of the run. Shown, never absorbed: the bank
  answered a question this run never asked, and a human
  decides what that means.

THE EXACT-MATCH RULE, and why it lives here and not in the
match policy: the three-way match tolerates drift because it
compares *documents* — an invoice a supplier typed against an
order AP placed — and documents carry reading noise. A
confirmation compares *money*: the amount on the released
instruction against the amount the bank says left the
account. Money that left either matches the instruction to
the cent or it is a break someone must explain — a one-cent
shortfall waved through by a tolerance is a one-cent loss
nobody ever looks at. So this module compares amounts exactly
(rounded to cents, then equal), and deliberately never calls
the match policy's tolerance helpers.

An advice is tied to a run only by a reference the advice
itself carries — its header's payment-run field, or one run
named unanimously by its lines. An advice naming no run, or
several, or a run that is not on file, or a run the treasurer
has not released is held UNMATCHED with the reason on the
record. It is never guessed onto a run: attaching the bank's
answer to the wrong batch would corrupt two reconciliations
at once.

A confirmation is evidence over the run. Computing one reads
the run and the advice and writes its own record, plus the
confirmation overlay on each member record it classifies —
and nothing else. The run, its decision, and its export bytes
are untouched; approvals and memberships are untouched; no
bank data or supplier master is ever written from an advice.
"""

from __future__ import annotations

from .schemas import (
    ConfirmationLine,
    ConfirmationTotals,
    DocumentRecord,
    ExtractedDocument,
    PaymentConfirmation,
    PaymentConfirmationState,
    PaymentRun,
)

CONFIRM_CLASSES = (
    "paid_as_proposed",
    "short_paid",
    "over_paid",
    "missing",
    "unexpected_line",
)

# The overlay a classified member carries on its record, beside
# payment_state. unexpected_line names no member, so it writes
# no overlay.
OVERLAY_BY_CLASS: dict[str, PaymentConfirmationState | None] = {
    "paid_as_proposed": "CONFIRMED",
    "short_paid": "SHORT_PAID",
    "over_paid": "OVER_PAID",
    "missing": "MISSING",
    "unexpected_line": None,
}


def _normalise(number: str) -> str:
    return str(number).strip().upper()


def _cents(amount: float) -> float:
    return round(float(amount), 2)


def resolve_run_reference(extraction: ExtractedDocument) -> tuple[str | None, str]:
    """The run an advice answers, as the advice itself names it.

    Returns ``(run_id, how)`` — or ``(None, why_not)`` when the
    advice names no single run. Header first: the advice's own
    payment-run field is its statement of intent. Failing that,
    one run named unanimously by the paid lines. Anything else
    is not a reference; it is a guess, and this function does
    not guess."""
    header = extraction.value("payment_run_id")
    if header and str(header).strip():
        return str(header).strip(), "named in the advice's header"
    named = {
        line.payment_run_id.strip()
        for line in extraction.remittance_lines
        if line.payment_run_id and line.payment_run_id.strip()
    }
    if len(named) == 1:
        return named.pop(), "named unanimously by the advice's lines"
    if len(named) > 1:
        return None, (
            "the advice's lines name more than one payment run "
            f"({', '.join(sorted(named))}) — there is no single run "
            "this advice answers"
        )
    return None, "the advice names no payment run, in its header or its lines"


def unmatched_confirmation(
    confirmation_id: str,
    advice: DocumentRecord,
    reason: str,
    *,
    created_at: str = "",
) -> PaymentConfirmation:
    """A held confirmation: the advice is on record, the reason
    it answers no run is named, and nothing was classified.
    The advice's lines stay on the advice's own record — this
    record points at it, it does not copy them into classes
    they were never checked against."""
    n = len(advice.extraction.remittance_lines) if advice.extraction else 0
    return PaymentConfirmation(
        confirmation_id=confirmation_id,
        advice_record_id=advice.record_id,
        submission_id=advice.submission_id,
        run_id=None,
        status="UNMATCHED",
        unmatched_reason=reason,
        counts={name: 0 for name in CONFIRM_CLASSES},
        totals=[],
        totals_tie=False,
        notes=[
            f"Held unmatched: {reason}. The advice carries {n} paid "
            "line(s) on its document record; none were classified, "
            "because classification is always against a named run."
        ],
        audit=[{"event": "held_unmatched", "at": created_at}],
        created_at=created_at,
        updated_at=created_at,
    )


def reconcile_confirmation(
    confirmation_id: str,
    advice: DocumentRecord,
    run: PaymentRun,
    *,
    created_at: str = "",
) -> PaymentConfirmation:
    """Reconcile one advice against the released run it answers.

    ``run`` is resolved and known RELEASED — the service checks
    both before calling; this function owns the line-by-line
    classification and the summary arithmetic only."""
    extraction = advice.extraction
    assert extraction is not None  # the service validates before calling
    advice_lines = extraction.remittance_lines

    members_by_number: dict[str, int] = {}
    for index, member in enumerate(run.lines):
        number = _normalise(member.invoice_number)
        if number and number not in members_by_number:
            members_by_number[number] = index

    lines: list[ConfirmationLine] = []
    answered: set[int] = set()
    for aline in advice_lines:
        number = _normalise(aline.invoice_number)
        member_index = members_by_number.get(number)
        if member_index is None:
            lines.append(ConfirmationLine(
                invoice_number=aline.invoice_number,
                classification="unexpected_line",
                paid_amount=_cents(aline.amount_paid),
                currency=aline.currency,
                value_date=aline.value_date,
                bank_reference=aline.bank_reference,
                note=(
                    f"Names {aline.invoice_number}, which is not a member "
                    f"of payment run {run.run_id} — shown, never absorbed "
                    "into the run's totals."
                ),
            ))
            continue
        member = run.lines[member_index]
        if member_index in answered:
            lines.append(ConfirmationLine(
                invoice_number=aline.invoice_number,
                classification="unexpected_line",
                paid_amount=_cents(aline.amount_paid),
                currency=aline.currency,
                member_record_id=member.record_id,
                value_date=aline.value_date,
                bank_reference=aline.bank_reference,
                note=(
                    "A second line for this invoice in the same advice — "
                    "the first line carries the member's confirmation."
                ),
            ))
            continue
        answered.add(member_index)
        proposed = _cents(member.amount)
        paid = _cents(aline.amount_paid)
        if aline.currency != member.currency:
            lines.append(ConfirmationLine(
                invoice_number=aline.invoice_number,
                classification="short_paid",
                proposed_amount=proposed,
                paid_amount=paid,
                currency=member.currency,
                member_record_id=member.record_id,
                value_date=aline.value_date,
                bank_reference=aline.bank_reference,
                note=(
                    f"Currency differs: the run proposed {member.currency} "
                    f"but the advice reports {aline.currency} — nothing "
                    f"arrived in the proposed currency, so the member is "
                    f"short by the full {proposed:.2f} {member.currency} "
                    "until the currency break is resolved. The paid figure "
                    "rides this line in its own currency and stays out of "
                    "the paid totals."
                ),
            ))
        elif paid == proposed:
            lines.append(ConfirmationLine(
                invoice_number=aline.invoice_number,
                classification="paid_as_proposed",
                proposed_amount=proposed,
                paid_amount=paid,
                currency=member.currency,
                member_record_id=member.record_id,
                value_date=aline.value_date,
                bank_reference=aline.bank_reference,
                difference=0.0,
                note="Paid exactly as proposed, to the cent.",
            ))
        elif paid < proposed:
            lines.append(ConfirmationLine(
                invoice_number=aline.invoice_number,
                classification="short_paid",
                proposed_amount=proposed,
                paid_amount=paid,
                currency=member.currency,
                member_record_id=member.record_id,
                value_date=aline.value_date,
                bank_reference=aline.bank_reference,
                difference=_cents(paid - proposed),
                note=(
                    f"Proposed {proposed:.2f} but {paid:.2f} arrived — "
                    f"short {_cents(proposed - paid):.2f} {member.currency}. "
                    "Bank charges, a partial payment, or an error: a human "
                    "finds out which."
                ),
            ))
        else:
            lines.append(ConfirmationLine(
                invoice_number=aline.invoice_number,
                classification="over_paid",
                proposed_amount=proposed,
                paid_amount=paid,
                currency=member.currency,
                member_record_id=member.record_id,
                value_date=aline.value_date,
                bank_reference=aline.bank_reference,
                difference=_cents(paid - proposed),
                note=(
                    f"Proposed {proposed:.2f} but {paid:.2f} arrived — "
                    f"over by {_cents(paid - proposed):.2f} {member.currency}. "
                    "Money the run never instructed is still a break."
                ),
            ))

    for index, member in enumerate(run.lines):
        if index in answered:
            continue
        lines.append(ConfirmationLine(
            invoice_number=member.invoice_number,
            classification="missing",
            proposed_amount=_cents(member.amount),
            currency=member.currency,
            member_record_id=member.record_id,
            note=(
                f"The run was released for value date {run.value_date} and "
                "no advice line names this invoice — the payment is "
                "unaccounted for until the bank answers for it."
            ),
        ))

    counts = {name: 0 for name in CONFIRM_CLASSES}
    for line in lines:
        counts[line.classification] += 1

    # The summary, per currency. The tie is computed against the
    # run's own member lines, recomputed here from the run —
    # not from the classes — so a classification bug cannot make
    # its own arithmetic look right.
    currencies: list[str] = []
    for member in run.lines:
        if member.currency not in currencies:
            currencies.append(member.currency)
    for line in lines:
        if line.classification == "unexpected_line" \
                and line.currency not in currencies:
            currencies.append(line.currency)
    totals: list[ConfirmationTotals] = []
    for currency in currencies:
        row = ConfirmationTotals(
            currency=currency,
            run_total=_cents(sum(
                m.amount for m in run.lines if m.currency == currency
            )),
        )
        for line in lines:
            if line.currency != currency:
                continue
            paid = line.paid_amount or 0.0
            proposed = line.proposed_amount or 0.0
            if line.classification == "paid_as_proposed":
                row.confirmed_proposed = _cents(row.confirmed_proposed + proposed)
                row.confirmed_paid = _cents(row.confirmed_paid + paid)
            elif line.classification == "short_paid":
                row.short_proposed = _cents(row.short_proposed + proposed)
                if line.difference is not None:
                    # Same currency: the gap is proposed minus paid,
                    # and the paid figure joins this currency's totals.
                    row.short_paid = _cents(row.short_paid + paid)
                    row.shortfall = _cents(row.shortfall + proposed - paid)
                else:
                    # Another currency arrived: in the proposed
                    # currency the member is short by the whole
                    # amount, and the foreign figure stays on its
                    # line, out of these totals.
                    row.shortfall = _cents(row.shortfall + proposed)
            elif line.classification == "over_paid":
                row.over_proposed = _cents(row.over_proposed + proposed)
                row.over_paid = _cents(row.over_paid + paid)
                row.excess = _cents(row.excess + paid - proposed)
            elif line.classification == "missing":
                row.missing_proposed = _cents(row.missing_proposed + proposed)
            elif line.classification == "unexpected_line":
                # Only lines whose member (if any) is in this
                # currency row, or memberless lines paid in it —
                # line.currency carries the advice's currency for
                # those, which is the row they were bucketed by.
                row.unexpected_paid = _cents(row.unexpected_paid + paid)
        totals.append(row)

    totals_tie = all(
        _cents(
            row.confirmed_proposed + row.short_proposed
            + row.over_proposed + row.missing_proposed
        ) == row.run_total
        for row in totals
    )
    flags: list[str] = []
    notes: list[str] = []
    if not totals_tie:
        flags.append("CONFIRMATION_ARITHMETIC_BREAK")
        notes.append(
            "The classification does not recompose the run's totals — "
            "the break is in this confirmation's arithmetic, and it is "
            "named here rather than smoothed over."
        )
    foreign = [
        line for line in lines
        if line.classification == "short_paid" and line.difference is None
    ]
    if foreign:
        notes.append(
            f"{len(foreign)} line(s) arrived in a different currency "
            "than proposed; their paid figures ride their lines in "
            "their own currency and are excluded from the paid totals."
        )

    return PaymentConfirmation(
        confirmation_id=confirmation_id,
        advice_record_id=advice.record_id,
        submission_id=advice.submission_id,
        run_id=run.run_id,
        status="AWAITING_CONTROLLER",
        lines=lines,
        counts=counts,
        totals=totals,
        totals_tie=totals_tie,
        flags=flags,
        notes=notes,
        audit=[{"event": "confirmed", "at": created_at}],
        created_at=created_at,
        updated_at=created_at,
    )
