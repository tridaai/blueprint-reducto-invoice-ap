"""The record model: documents, extractions, matches, decisions.

One vocabulary runs from the DocumentPort through the match engine
to the gate, so a field's citation means the same thing at every
layer: a value a human can trace back to the exact box on the exact
page it was read from.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

DocClass = Literal[
    "invoice",
    "credit_note",
    "remittance_advice",
    "delivery_note",
    "packing_slip",
    "statement",
    "letter",
    "unknown",
]

RecordStatus = Literal[
    "RECEIVED",
    "AWAITING_APPROVER",
    "APPROVED",
    "REJECTED",
    "EXCEPTION",
    "FRAUD_REVIEW",
    "REFUSED",
]


class Citation(BaseModel):
    """Where a value was read: page + bounding box, as the
    document engine reported it. The offline adapter reports
    text-layer citations (line-derived boxes) and labels them so."""

    page: int
    bbox: tuple[float, float, float, float]  # x0, y0, x1, y1, 0..1000 space
    source: Literal["engine", "text_layer"] = "engine"


class ExtractedField(BaseModel):
    """One extracted value with its citation and confidence."""

    value: Any = None
    citation: Citation | None = None
    confidence: float | None = None


class LineItem(BaseModel):
    sku: str = ""
    description: str = ""
    quantity: float = 0.0
    unit_price: float = 0.0
    amount: float = 0.0


class ExtractedDocument(BaseModel):
    """The extraction of one (sub-)document, as the port returns it."""

    doc_class: DocClass = "unknown"
    fields: dict[str, ExtractedField] = Field(default_factory=dict)
    lines: list[LineItem] = Field(default_factory=list)
    currency: str = "USD"
    raw_text: str = ""  # the text layer, where the engine exposes one

    def value(self, name: str) -> Any:
        field = self.fields.get(name)
        return field.value if field else None


class PurchaseOrder(BaseModel):
    po_number: str
    supplier_id: str
    supplier_name: str
    currency: str = "USD"
    lines: list[LineItem] = Field(default_factory=list)
    total: float = 0.0


class GoodsReceipt(BaseModel):
    receipt_id: str
    po_number: str
    supplier_id: str
    lines: list[LineItem] = Field(default_factory=list)  # quantities received


class Supplier(BaseModel):
    supplier_id: str
    name: str
    bank_account: str  # the master record's approved account
    bank_name: str = ""
    currency: str = "USD"


class LineMatch(BaseModel):
    sku: str
    verdict: Literal["matched", "price_variance", "quantity_variance", "missing"]
    invoiced_qty: float = 0.0
    received_qty: float = 0.0
    po_unit_price: float = 0.0
    invoice_unit_price: float = 0.0
    note: str = ""


class MatchResult(BaseModel):
    status: Literal["MATCHED", "MATCHED_WITH_TOLERANCE", "EXCEPTION"]
    po_number: str | None = None
    line_matches: list[LineMatch] = Field(default_factory=list)
    exception_codes: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    invoice_total: float = 0.0
    po_total: float = 0.0


class GateDecision(BaseModel):
    decision: Literal["approve", "reject"]
    actor: str
    reason: str = ""
    at: str = ""


class DocumentRecord(BaseModel):
    """One processed (sub-)document and everything decided about it."""

    record_id: str
    submission_id: str
    supplier_id: str | None = None
    supplier_name: str = ""
    status: RecordStatus = "RECEIVED"
    doc_class: DocClass = "unknown"
    extraction: ExtractedDocument | None = None
    match: MatchResult | None = None
    refusal_reason: str | None = None
    fraud_flags: list[str] = Field(default_factory=list)
    injection_flags: list[str] = Field(default_factory=list)
    bank_account_seen: str | None = None
    decision: GateDecision | None = None
    audit: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


class PaymentLine(BaseModel):
    record_id: str
    supplier_name: str
    invoice_number: str
    currency: str
    amount: float


class PaymentPacket(BaseModel):
    """The proposed payment list. A proposal only: generating it
    moves no money, and this type has no field that could."""

    packet_id: str
    lines: list[PaymentLine] = Field(default_factory=list)
    totals_by_currency: dict[str, float] = Field(default_factory=dict)
    generated_at: str = ""
    note: str = "Proposed payments for the customer's AP system of record. Nothing here has been paid."
