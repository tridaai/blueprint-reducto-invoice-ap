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


class StatementLine(BaseModel):
    """One open-item line on a supplier statement: the invoice the
    supplier believes is open, and for how much. A statement line
    is a claim by the supplier, not a fact — reconciliation is
    where it gets checked against AP's own records."""

    invoice_number: str
    invoice_date: str = ""
    amount: float = 0.0
    currency: str = "USD"


class ExtractedDocument(BaseModel):
    """The extraction of one (sub-)document, as the port returns it."""

    doc_class: DocClass = "unknown"
    fields: dict[str, ExtractedField] = Field(default_factory=dict)
    lines: list[LineItem] = Field(default_factory=list)
    # Populated when the document is a statement: its open-item
    # lines, in the order the supplier listed them (order matters —
    # a repeated number later in the list is the duplicate break).
    statement_lines: list[StatementLine] = Field(default_factory=list)
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


ReconClass = Literal[
    "matched",
    "amount_disagrees",
    "missing_in_ap",
    "missing_on_statement",
    "duplicates_within_statement",
]

ReconStatus = Literal["AWAITING_CONTROLLER", "ACCEPTED", "FLAGGED_BACK"]


class ReconLine(BaseModel):
    """One reconciled item: a statement line, or an AP record the
    statement never mentioned. Both figures ride along whenever
    both exist — a break a human must judge is shown as the two
    numbers, never collapsed into a verdict alone."""

    invoice_number: str
    classification: ReconClass
    statement_amount: float | None = None
    ap_amount: float | None = None
    currency: str = "USD"
    ap_record_id: str | None = None
    invoice_date: str = ""
    note: str = ""


class ControllerDecision(BaseModel):
    """The controller's word on a reconciliation. ``flag_back``
    requires a note — the AP team acts on it with the supplier."""

    decision: Literal["accept", "flag_back"]
    actor: str
    note: str = ""
    at: str = ""


class Reconciliation(BaseModel):
    """A stored reconciliation of one supplier statement against
    AP's records: every line classified, totals tied or the break
    named, and a controller decision on top. Evidence for humans —
    computing one never changes any document record."""

    reconciliation_id: str
    statement_record_id: str
    submission_id: str
    supplier_id: str
    supplier_name: str = ""
    period: str = ""
    currency: str = "USD"
    status: ReconStatus = "AWAITING_CONTROLLER"
    lines: list[ReconLine] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    totals: dict[str, float] = Field(default_factory=dict)
    statement_total_stated: float | None = None
    statement_total_lines: float = 0.0
    totals_tie: bool = False
    flags: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    decision: ControllerDecision | None = None
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
