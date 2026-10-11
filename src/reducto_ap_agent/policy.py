"""Match and screening policy — every tolerance in one module.

The three-way match is a business decision before it is a
computation: how far a unit price may drift before a human looks,
whether a short shipment is an exception or an accepted partial.
Those numbers live here, in one place, so a deployment tunes its
policy in the open and the evals can state exactly which policy
produced a result. Nothing in the match engine hardcodes a
tolerance of its own.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MatchPolicy:
    """Tolerances for the three-way match (invoice × PO × receipt)."""

    # A line's unit price may differ from the PO's by at most this
    # fraction and still match without a human.
    unit_price_tolerance: float = 0.02
    # …or by at most this absolute amount per unit, whichever the
    # deployment's currency makes more forgiving is NOT the rule:
    # both bounds must hold (the stricter reading wins).
    unit_price_abs_tolerance: float = 0.50
    # Invoiced quantity must equal the received quantity. A short
    # receipt is an exception, never silently matched.
    allow_partial_receipt: bool = False
    # Header totals: invoice total vs the sum of its own lines and
    # vs the PO total. A total difference is within tolerance when
    # it falls inside either bound (relative or absolute) — money
    # totals accumulate rounding across many lines, so the total
    # check takes the looser reading; line prices take the stricter
    # one above.
    total_tolerance: float = 0.01
    total_abs_tolerance: float = 1.00
    # An invoice naming no purchase order cannot three-way match;
    # it always routes to a human as NO_PO_REFERENCE.
    require_po_reference: bool = True


DEFAULT_POLICY = MatchPolicy()

# Document classes the payment path accepts. Everything else the
# classifier returns — statements, letters, delivery notes standing
# alone — is refused by the payment path with the reason recorded.
PAYABLE_CLASSES = frozenset({"invoice", "credit_note"})

# Classes a split may legitimately produce alongside a payable one;
# they are stored as attachments, never processed for payment.
ATTACHMENT_CLASSES = frozenset({"remittance_advice", "delivery_note", "packing_slip"})

# Words that, appearing in free-text fields bound for a model call,
# mark the field for fencing: the text is data, never instructions.
INJECTION_MARKERS = (
    "ignore previous instructions",
    "ignore all instructions",
    "disregard the above",
    "you are now",
    "system prompt",
    "approve this invoice",
    "pay immediately",
    "override the match",
)
