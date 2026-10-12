"""PO coverage: what has been billed against an order, in total.

The three-way match is per-invoice arithmetic: this invoice,
against this order, within these tolerances. It cannot see
accumulation. Three honest invoices, each matching cleanly,
can together bill an order past its total — the classic slow
leak, invisible to every per-invoice check. This module is
the cumulative view the match does not have:

- **Billed is a property of APPROVED invoices.** A record
  bills its order when the approver has approved it — refused,
  rejected, exception, fraud-held, and still-awaiting money
  bills nothing, however plausible the document. The billed
  figure per invoice is ``credits.billable_amount``: the
  approved face minus the credits applied against it. A
  credit reduces the bill itself, so it reduces billed; a
  payment only settles a bill that stands, so a part-paid or
  fully paid invoice bills in full, exactly as charged.
- **The total is the register's, and says so.** An order's
  total comes from the order register (``PurchaseOrder.total``
  — reference data, the customer's ERP mirror in a real
  deployment), and the row names that source. An order an
  approved invoice names that is NOT on the register gets no
  verdict: it reads UNMEASURED, its billed sum listed, its
  total None. Coverage never invents a total from the
  invoices billing against the order — that would grade the
  leak against the leak.
- **Currencies are never converted.** An invoice billing in
  a currency other than the order's is not summed into it;
  it is listed on the row as excluded, with the reason. A
  quiet conversion is how a coverage figure becomes a
  fiction.
- **Coverage informs; it never blocks, and never rewrites.**
  An order standing EXCEEDED is not fraud and not a match
  failure — change orders and blanket orders over-bill
  legitimately, and the register's total is reference data
  that can lag the paperwork. So the gate stays the
  approver's: the coverage fact rides the record's surfaces
  before they decide (``record_coverage``), and a decision
  taken on an EXCEEDED order is recorded as taken with the
  fact in front of them (service.decide). The match outcome
  of every contributing invoice is untouched — coverage is
  a second fact, on a second axis.

A pure read model: building it writes nothing, like the
position and the ledger it stands beside. The position asks
what is still OWED; coverage asks what has been BILLED —
different ledgers, deliberately never merged.
"""

from __future__ import annotations

from .credits import billable_amount
from .policy import DEFAULT_COVERAGE, CoveragePolicy
from .schemas import (
    CoverageExcluded,
    CoverageLine,
    CoverageReport,
    DocumentRecord,
    OrderCoverage,
    PurchaseOrder,
    RecordCoverage,
)

_STATE_RANK = {"EXCEEDED": 0, "WATCH": 1, "WITHIN": 2, "UNMEASURED": 3}


def _cents(amount: float) -> float:
    return round(float(amount), 2)


def po_reference_of(record: DocumentRecord) -> str:
    """The purchase order reference as the invoice carries it
    — the extraction field, verbatim but for surrounding
    whitespace. Empty when the document names no order, which
    is its own match outcome (NO_PO_REFERENCE) and no coverage
    question at all."""
    if record.extraction is None:
        return ""
    value = record.extraction.value("po_reference")
    return str(value).strip() if value else ""


def _bills(record: DocumentRecord) -> bool:
    """Whether a record bills its order: an invoice, approved,
    with an extraction and an order named. Every other state
    of money — awaiting, exception, rejected, refused,
    fraud-held — has charged the order nothing."""
    return (
        record.doc_class == "invoice"
        and record.status == "APPROVED"
        and record.extraction is not None
        and bool(po_reference_of(record))
    )


def _pct(billed: float, total: float | None) -> float | None:
    """Coverage as a percentage of the order total, one
    decimal. None when there is no usable total to measure
    against — the percentage would be a fiction."""
    if total is None or total == 0:
        return None
    return round(100.0 * billed / total, 1)


def _state_for(
    billed: float, total: float | None, policy: CoveragePolicy
) -> str:
    """The state a billed figure stands in. EXCEEDED is a
    comparison to the cent — billed past the total — never a
    percentage judgement: exactly 100% is a fully billed
    order, and it reads WATCH like any figure at or over the
    watch threshold."""
    if total is None:
        return "UNMEASURED"
    if billed > total:
        return "EXCEEDED"
    pct = _pct(billed, total)
    if pct is not None and pct >= policy.watch_pct:
        return "WATCH"
    return "WITHIN"


def _order_row(
    po_number: str,
    candidates: list[DocumentRecord],
    po: PurchaseOrder | None,
    policy: CoveragePolicy,
) -> OrderCoverage:
    """One order's row over its billing candidates (APPROVED
    invoices naming it, in billing — store — order)."""
    if po is not None:
        currency = po.currency
        total: float | None = _cents(po.total)
        supplier_id: str | None = po.supplier_id
        supplier_name = po.supplier_name
        source = "ORDER_REGISTER"
    else:
        total = None
        source = "NOT_ON_FILE"
        supplier_id = candidates[0].supplier_id if candidates else None
        supplier_name = candidates[0].supplier_name if candidates else ""
        currency = (
            candidates[0].extraction.currency  # type: ignore[union-attr]
            if candidates else "USD"
        )
    lines: list[CoverageLine] = []
    excluded: list[CoverageExcluded] = []
    billed = 0.0
    for record in candidates:
        assert record.extraction is not None
        billable = billable_amount(record)
        if record.extraction.currency != currency:
            excluded.append(CoverageExcluded(
                record_id=record.record_id,
                invoice_number=str(
                    record.extraction.value("invoice_number") or ""),
                amount=billable,
                currency=record.extraction.currency,
                reason=(
                    f"bills in {record.extraction.currency} against a "
                    f"{currency} order — coverage never converts "
                    "currencies, so this invoice is listed, not summed"
                ),
            ))
            continue
        billed = _cents(billed + billable)
        lines.append(CoverageLine(
            record_id=record.record_id,
            invoice_number=str(
                record.extraction.value("invoice_number") or ""),
            supplier_name=record.supplier_name,
            invoice_date=str(
                record.extraction.value("invoice_date") or ""),
            face=_cents(float(record.extraction.value("total") or 0.0)),
            credits=_cents(sum(
                entry.amount for entry in record.credits_applied)),
            billable=billable,
            cumulative_billed=billed,
            cumulative_pct=_pct(billed, total),
        ))
    return OrderCoverage(
        po_number=po_number,
        supplier_id=supplier_id,
        supplier_name=supplier_name,
        currency=currency,
        total=total,
        total_source=source,  # type: ignore[arg-type]
        billed=billed,
        pct=_pct(billed, total),
        state=_state_for(billed, total, policy),  # type: ignore[arg-type]
        lines=lines,
        excluded=excluded,
    )


def build_coverage(
    records: list[DocumentRecord],
    purchase_orders: dict[str, PurchaseOrder],
    policy: CoveragePolicy = DEFAULT_COVERAGE,
) -> CoverageReport:
    """PO coverage over a store snapshot: every order on the
    register (billed or not — an untouched order reads WITHIN
    at 0.0), plus every order an approved invoice names that
    the register does not carry. Pure: reads records and
    reference data, writes nothing."""
    candidates: dict[str, list[DocumentRecord]] = {}
    for record in records:
        if _bills(record):
            candidates.setdefault(po_reference_of(record), []).append(record)
    rows = [
        _order_row(po_number, candidates.get(po_number, []),
                   purchase_orders.get(po_number), policy)
        for po_number in set(purchase_orders) | set(candidates)
    ]

    def rank(row: OrderCoverage) -> tuple:
        measure = row.pct if row.pct is not None else row.billed
        return (_STATE_RANK[row.state], -measure, row.po_number)

    rows.sort(key=rank)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.state] = counts.get(row.state, 0) + 1
    return CoverageReport(watch_pct=policy.watch_pct, orders=rows,
                          counts=counts)


def record_coverage(
    record: DocumentRecord,
    records: list[DocumentRecord],
    purchase_orders: dict[str, PurchaseOrder],
    policy: CoveragePolicy = DEFAULT_COVERAGE,
) -> RecordCoverage:
    """The coverage fact for one invoice, COUNTING this
    invoice: where its order stands with this record's own
    billable amount in the sum. An approved record is already
    inside the sum (``counted`` says so); an awaiting one is
    added for the reading, so the approver sees the figure
    their approval would produce — "billed at 104% including
    this invoice" — before they decide, not after."""
    po_number = po_reference_of(record)
    candidates = [
        r for r in records
        if _bills(r) and po_reference_of(r) == po_number
    ]
    row = _order_row(
        po_number, candidates, purchase_orders.get(po_number), policy)
    counted = any(r.record_id == record.record_id for r in candidates)
    assert record.extraction is not None
    own_currency = record.extraction.currency
    if counted:
        including = row.billed
    elif own_currency == row.currency:
        including = _cents(row.billed + billable_amount(record))
    else:
        including = row.billed
    pct = _pct(including, row.total)
    state = _state_for(including, row.total, policy)
    invoice_number = str(record.extraction.value("invoice_number") or "")
    if row.total is None:
        fact = (
            f"Order {po_number} is not on the order register — no "
            f"total to measure against. Approved invoices have "
            f"billed {row.billed:.2f} {row.currency} against it; "
            f"counting this invoice, {including:.2f} — UNMEASURED."
        )
    else:
        fact = (
            f"Order {po_number} stands billed at {pct:.1f}% "
            f"({including:.2f} of {row.total:.2f} {row.currency}) "
            f"including this invoice — {state}."
        )
    if not counted and own_currency != row.currency:
        fact += (
            f" This invoice bills in {own_currency}, not the "
            f"order's {row.currency}, so coverage does not sum it."
        )
    return RecordCoverage(
        record_id=record.record_id,
        invoice_number=invoice_number,
        po_number=po_number,
        currency=row.currency,
        total=row.total,
        total_source=row.total_source,
        billed=row.billed,
        billed_including=including,
        pct=pct,
        state=state,  # type: ignore[arg-type]
        counted=counted,
        fact=fact,
    )
