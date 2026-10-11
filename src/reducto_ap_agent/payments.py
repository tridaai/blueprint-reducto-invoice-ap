"""Payment runs, assembled in code.

The packet is a view: every approved record, proposed. A
payment run is a commitment device built on top of it — a
named batch for one value date that a treasurer can release
or return as a unit. Assembly is where the control lives:

- a run takes only APPROVED, payable records (invoices and
  credit notes; a credit note's negative total nets against
  the batch exactly as it does in the packet — one amount
  rule, reused, not reinvented);
- a record belongs to at most one open run. Assembly that
  names a record already held refuses it *by name*, with the
  holding run's id in the reason; a record from a released
  run (PAID_PROPOSED) is refused forever — it is already in
  a bank export, and paying it twice is the failure this
  whole control exists to prevent;
- a fraud-held or exception record can never enter a run,
  and asking for one produces a named refusal, not a silent
  skip — the run assembles from the eligible remainder and
  reports everything it refused and why;
- totals are computed from the member lines and tied back
  against them before the run exists. If the arithmetic
  does not tie, assembly fails loudly and no run is stored.

The export is a payment-instruction CSV generated once, at
release. Its bank details come from the SUPPLIER MASTER —
never from the invoice. An invoice is a claim by the sender;
the master is the verified record. (The fraud pattern this
blueprint screens for is precisely an invoice asking to be
paid somewhere new — a member line can therefore only exist
for a record whose invoice agreed with the master or was
approved despite no master change, and the export reads the
master regardless, so there is no path by which an invoice's
account reaches a bank file.)
"""

from __future__ import annotations

import csv
import hashlib
import io

from .errors import PaymentRunError
from .schemas import (
    DocumentRecord,
    PaymentRefusal,
    PaymentRun,
    PaymentRunLine,
    Supplier,
)

PAYABLE_CLASSES = ("invoice", "credit_note")

EXPORT_COLUMNS = [
    "payment_run_id",
    "value_date",
    "record_id",
    "invoice_number",
    "supplier_id",
    "supplier",
    "bank_name",
    "bank_account",
    "currency",
    "amount",
]


def _refusal(record: DocumentRecord, currencies: list[str]) -> PaymentRefusal | None:
    """Why this record cannot join a run, or None when it can.

    Check order matters: the refusals a human acts on first
    (already committed elsewhere, held, excepted) are named
    before the generic not-approved."""
    rid = record.record_id
    if record.doc_class not in PAYABLE_CLASSES:
        return PaymentRefusal(
            record_id=rid, code="NOT_PAYABLE_CLASS",
            reason=(
                f"classified as {record.doc_class!r} — only invoices and "
                "credit notes are payable"
            ),
        )
    if record.payment_state == "PAID_PROPOSED":
        return PaymentRefusal(
            record_id=rid, code="ALREADY_PAID_PROPOSED",
            reason=(
                f"already released in payment run {record.payment_run_id} — "
                "it is in a bank export and never joins another run"
            ),
        )
    if record.payment_state == "IN_RUN":
        return PaymentRefusal(
            record_id=rid, code="ALREADY_IN_RUN",
            reason=(
                f"already a member of open payment run {record.payment_run_id} — "
                "a record belongs to at most one open run"
            ),
        )
    if record.status == "FRAUD_REVIEW":
        return PaymentRefusal(
            record_id=rid, code="FRAUD_HOLD",
            reason=(
                "on fraud hold: "
                + ("; ".join(record.fraud_flags) or "bank details under review")
                + " — fraud holds are cleared in the supplier master, "
                "never by a payment run"
            ),
        )
    if record.status == "EXCEPTION":
        codes = record.match.exception_codes if record.match else []
        return PaymentRefusal(
            record_id=rid, code="MATCH_EXCEPTION",
            reason=(
                "carries an open match exception"
                + (f" [{', '.join(codes)}]" if codes else "")
                + " — exceptions go back to the sender, not into a run"
            ),
        )
    if record.status != "APPROVED":
        return PaymentRefusal(
            record_id=rid, code="NOT_APPROVED",
            reason=(
                f"record is {record.status} — only APPROVED records "
                "enter a payment run"
            ),
        )
    if currencies and record.extraction is not None \
            and record.extraction.currency not in currencies:
        return PaymentRefusal(
            record_id=rid, code="OUT_OF_CURRENCY_SCOPE",
            reason=(
                f"record is in {record.extraction.currency}; this run's "
                f"scope is {', '.join(currencies)}"
            ),
        )
    return None


def assemble_run(
    run_id: str,
    value_date: str,
    candidates: list[DocumentRecord],
    *,
    currencies: list[str] | None = None,
    created_at: str = "",
) -> PaymentRun:
    """Assemble a run from candidate records.

    Pure computation over the candidates the caller selected
    (named ids, or the approved-payable universe): the service
    persists the result and marks the members. Raises
    :class:`PaymentRunError` when nothing is eligible — an
    empty batch is an operator error, not a run."""
    scope = [c.strip().upper() for c in (currencies or []) if c and c.strip()]
    lines: list[PaymentRunLine] = []
    refusals: list[PaymentRefusal] = []
    for record in candidates:
        refusal = _refusal(record, scope)
        if refusal is not None:
            refusals.append(refusal)
            continue
        assert record.extraction is not None  # approved records carry one
        lines.append(PaymentRunLine(
            record_id=record.record_id,
            supplier_id=record.supplier_id,
            supplier_name=record.supplier_name,
            invoice_number=str(record.extraction.value("invoice_number") or ""),
            doc_class=record.doc_class,  # type: ignore[arg-type]
            currency=record.extraction.currency,
            amount=round(float(record.extraction.value("total") or 0.0), 2),
        ))
    if not lines:
        named = "; ".join(
            f"{r.record_id} ({r.code})" for r in refusals
        ) or "no candidates were offered"
        raise PaymentRunError(
            f"payment run for {value_date} has no eligible records — {named}"
        )
    totals: dict[str, float] = {}
    for line in lines:
        totals[line.currency] = round(totals.get(line.currency, 0.0) + line.amount, 2)
    # The tie: totals must be exactly the sum of the member
    # lines, recomputed independently, or the run does not
    # exist. Assembly fails loudly rather than storing a batch
    # whose header disagrees with its contents.
    for currency, total in totals.items():
        recomputed = round(
            sum(l.amount for l in lines if l.currency == currency), 2
        )
        if recomputed != total:
            raise PaymentRunError(
                f"run totals do not tie for {currency}: header {total:.2f} "
                f"vs member lines {recomputed:.2f}"
            )
    return PaymentRun(
        run_id=run_id,
        value_date=value_date,
        currencies=scope,
        status="AWAITING_TREASURER",
        lines=lines,
        refusals=refusals,
        totals_by_currency=totals,
        audit=[{"event": "assembled", "at": created_at}],
        created_at=created_at,
        updated_at=created_at,
    )


def export_key_for(run_id: str) -> str:
    """The versioned object-store key for a run's export. The
    version is in the key: a future export format is a new key,
    never a silent overwrite of the artifact a treasurer
    released against."""
    return f"payment-runs/{run_id}/payment-instructions-v1.csv"


def build_export_csv(run: PaymentRun, suppliers: dict[str, Supplier]) -> bytes:
    """The payment-instruction file for a released run.

    Deterministic: same run, same bytes — member order is the
    run's, amounts are fixed two-decimal, line endings are LF.

    Bank details are read from ``suppliers`` — the supplier
    master — on purpose, never from the member records (see
    the module docstring). A member whose supplier is not on
    file fails the release loudly, before anything is written."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(EXPORT_COLUMNS)
    for line in run.lines:
        supplier = suppliers.get(line.supplier_id or "")
        if supplier is None:
            raise PaymentRunError(
                f"cannot release {run.run_id}: member {line.record_id} names "
                f"supplier {line.supplier_id!r}, who is not on file — the "
                "export pays only accounts from the supplier master"
            )
        writer.writerow([
            run.run_id,
            run.value_date,
            line.record_id,
            line.invoice_number,
            supplier.supplier_id,
            supplier.name,
            supplier.bank_name,
            supplier.bank_account,
            line.currency,
            f"{line.amount:.2f}",
        ])
    return out.getvalue().encode("utf-8")


def export_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
