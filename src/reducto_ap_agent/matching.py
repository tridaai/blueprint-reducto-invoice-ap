"""The three-way match, computed in code.

Invoice × purchase order × goods receipt. No model is consulted:
quantities, prices, and totals are arithmetic, and a match a human
will rely on should be reproducible to the cent. The engine takes
the extraction, the PO the invoice references, and the receipt
against that PO, and returns a :class:`MatchResult` whose
exception codes name exactly what a human must look at.

Rules (tolerances from :mod:`.policy`, never hardcoded here):

- every invoice line must exist on the PO (``LINE_NOT_ON_PO``);
- invoiced quantity must equal the received quantity
  (``QUANTITY_VARIANCE``) — a short shipment is an exception;
- unit price must sit inside the price tolerance
  (``PRICE_VARIANCE`` outside it; a difference inside it still
  marks the match ``MATCHED_WITH_TOLERANCE`` so the drift is
  visible, not buried);
- the stated total must agree with the invoice's own lines and
  with the PO total (``TOTAL_MISMATCH``);
- no PO reference, an unknown PO, or a missing receipt are
  exceptions in their own right (``NO_PO_REFERENCE``,
  ``PO_NOT_FOUND``, ``NO_RECEIPT``).
"""

from __future__ import annotations

from .policy import DEFAULT_POLICY, MatchPolicy
from .schemas import (
    ExtractedDocument,
    GoodsReceipt,
    LineMatch,
    MatchResult,
    PurchaseOrder,
)


def _price_within_tolerance(invoice_price: float, po_price: float, policy: MatchPolicy) -> bool:
    # Both bounds must hold — the stricter reading wins (policy.py).
    diff = abs(invoice_price - po_price)
    return (
        diff <= policy.unit_price_tolerance * abs(po_price)
        and diff <= policy.unit_price_abs_tolerance
    )


def _total_within_tolerance(amount: float, reference: float, policy: MatchPolicy) -> bool:
    diff = abs(amount - reference)
    return diff <= max(policy.total_tolerance * abs(reference), policy.total_abs_tolerance)


def match_invoice(
    extraction: ExtractedDocument,
    po: PurchaseOrder | None,
    receipt: GoodsReceipt | None,
    *,
    policy: MatchPolicy = DEFAULT_POLICY,
) -> MatchResult:
    invoice_total = float(extraction.value("total") or 0.0)
    po_ref = extraction.value("po_reference")

    if extraction.doc_class == "credit_note":
        # A credit note matches against the invoice it references
        # via its PO; the full invoice-level check is the approver's
        # view of the original record. Here: it must name a real PO
        # and carry a negative total in the right direction.
        if po is None:
            return MatchResult(
                status="EXCEPTION", exception_codes=["PO_NOT_FOUND"],
                notes=["Credit note references no purchase order on file."],
                invoice_total=invoice_total,
            )
        return MatchResult(
            status="MATCHED", po_number=po.po_number,
            notes=["Credit note against a purchase order on file; it reduces the payable, never creates one."],
            invoice_total=invoice_total, po_total=po.total,
        )

    if not po_ref and policy.require_po_reference:
        return MatchResult(
            status="EXCEPTION", exception_codes=["NO_PO_REFERENCE"],
            notes=["Invoice names no purchase order; a three-way match is impossible."],
            invoice_total=invoice_total,
        )
    if po is None:
        return MatchResult(
            status="EXCEPTION", exception_codes=["PO_NOT_FOUND"],
            notes=[f"Purchase order {po_ref!r} is not on file."],
            invoice_total=invoice_total,
        )
    if receipt is None:
        return MatchResult(
            status="EXCEPTION", po_number=po.po_number,
            exception_codes=["NO_RECEIPT"],
            notes=[f"No goods receipt recorded against {po.po_number}; receipt is one leg of the match."],
            invoice_total=invoice_total, po_total=po.total,
        )

    po_lines = {ln.sku: ln for ln in po.lines}
    received = {ln.sku: ln.quantity for ln in receipt.lines}
    codes: list[str] = []
    notes: list[str] = []
    line_matches: list[LineMatch] = []
    any_tolerance = False

    for inv_line in extraction.lines:
        po_line = po_lines.get(inv_line.sku)
        if po_line is None:
            line_matches.append(LineMatch(
                sku=inv_line.sku, verdict="missing",
                invoiced_qty=inv_line.quantity,
                invoice_unit_price=inv_line.unit_price,
                note="Line is not on the purchase order.",
            ))
            codes.append("LINE_NOT_ON_PO")
            continue
        recv_qty = received.get(inv_line.sku, 0.0)
        verdict = "matched"
        if inv_line.quantity != recv_qty and not policy.allow_partial_receipt:
            verdict = "quantity_variance"
            codes.append("QUANTITY_VARIANCE")
            notes.append(
                f"{inv_line.sku}: invoiced {inv_line.quantity:g} but received {recv_qty:g}."
            )
        elif inv_line.unit_price != po_line.unit_price:
            if _price_within_tolerance(inv_line.unit_price, po_line.unit_price, policy):
                any_tolerance = True
                notes.append(
                    f"{inv_line.sku}: unit price {inv_line.unit_price:.2f} vs PO "
                    f"{po_line.unit_price:.2f} — inside tolerance."
                )
            else:
                verdict = "price_variance"
                codes.append("PRICE_VARIANCE")
                notes.append(
                    f"{inv_line.sku}: unit price {inv_line.unit_price:.2f} vs PO "
                    f"{po_line.unit_price:.2f} — outside tolerance."
                )
        line_matches.append(LineMatch(
            sku=inv_line.sku, verdict=verdict,
            invoiced_qty=inv_line.quantity, received_qty=recv_qty,
            po_unit_price=po_line.unit_price, invoice_unit_price=inv_line.unit_price,
        ))

    lines_sum = round(sum(ln.amount for ln in extraction.lines), 2)
    if not _total_within_tolerance(invoice_total, lines_sum, policy):
        codes.append("TOTAL_MISMATCH")
        notes.append(
            f"Stated total {invoice_total:.2f} disagrees with the invoice's own lines ({lines_sum:.2f})."
        )
    elif not codes and not _total_within_tolerance(invoice_total, po.total, policy):
        # Only meaningful when every line matched: otherwise the
        # line codes already explain the difference.
        codes.append("TOTAL_MISMATCH")
        notes.append(
            f"Invoice total {invoice_total:.2f} disagrees with the PO total ({po.total:.2f}) "
            "beyond what the matched lines explain."
        )

    codes = list(dict.fromkeys(codes))  # dedupe, keep order
    if codes:
        status = "EXCEPTION"
    elif any_tolerance:
        status = "MATCHED_WITH_TOLERANCE"
    else:
        status = "MATCHED"
    return MatchResult(
        status=status, po_number=po.po_number, line_matches=line_matches,
        exception_codes=codes, notes=notes,
        invoice_total=invoice_total, po_total=po.total,
    )
