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


class RemittanceLine(BaseModel):
    """One paid line on a remittance advice: the bank's report of
    money that actually left, against one invoice of one payment
    run. An advice line is the bank's claim about the past, not
    an instruction — confirmation reconciliation is where it
    gets checked against the run that proposed the payment."""

    invoice_number: str
    amount_paid: float = 0.0
    currency: str = "USD"
    value_date: str = ""
    bank_reference: str = ""
    # The payment run the line names, when the advice carries
    # the reference per line rather than in its header (or both).
    payment_run_id: str = ""


class ExtractedDocument(BaseModel):
    """The extraction of one (sub-)document, as the port returns it."""

    doc_class: DocClass = "unknown"
    fields: dict[str, ExtractedField] = Field(default_factory=dict)
    lines: list[LineItem] = Field(default_factory=list)
    # Populated when the document is a statement: its open-item
    # lines, in the order the supplier listed them (order matters —
    # a repeated number later in the list is the duplicate break).
    statement_lines: list[StatementLine] = Field(default_factory=list)
    # Populated when the document is a remittance advice: the
    # paid lines the bank reports, in the order it listed them.
    remittance_lines: list[RemittanceLine] = Field(default_factory=list)
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


# A record's payment-run membership. ``IN_RUN``: held by an open
# run awaiting the treasurer. ``PAID_PROPOSED``: the run was
# released — the record sits in a bank-instruction export as a
# proposal. Terminal: a paid-proposed record never joins another
# run. This is deliberately NOT a RecordStatus: the record stays
# APPROVED, because the approver's decision and the treasurer's
# batch are two different humans' acts and neither rewrites the
# other.
PaymentState = Literal["IN_RUN", "PAID_PROPOSED"]

# A member record's remittance-confirmation state: what the
# bank's advice said about this member when its run was last
# reconciled against an advice. An overlay beside
# ``payment_state``, never a replacement for it — PAID_PROPOSED
# is what the run proposed; this is what the bank answered.
# Written by the confirmation path only, and only ever about a
# released run's members.
PaymentConfirmationState = Literal[
    "CONFIRMED", "SHORT_PAID", "OVER_PAID", "MISSING",
]

# A credit note's application state. ``UNAPPLIED``: the credit
# is on file — extracted, matched, and (once the approver's
# gate passes it) genuine — but no controller has applied it
# against the invoice it names, so it has reduced nothing.
# ``APPLIED``: a named controller applied it; the application
# is recorded on the credited invoice's ledger and the credit
# carries the invoice's record id. Only credit-note records
# carry a state; every other class leaves it None.
CreditState = Literal["UNAPPLIED", "APPLIED"]


class CreditApplication(BaseModel):
    """One credit note applied against one invoice, by a named
    controller. ``amount`` is positive — the size of the credit
    — and the invoice's open payable falls by exactly this much.
    The entry lives on the INVOICE's ledger (``credits_applied``)
    because that is where the money question is asked: what is
    still owed on this invoice. The credit note's own record
    points back via ``applied_invoice_record_id``."""

    credit_record_id: str
    credit_number: str
    invoice_record_id: str
    amount: float
    currency: str = "USD"
    actor: str
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
    payment_run_id: str | None = None
    payment_state: PaymentState | None = None
    payment_confirmation: PaymentConfirmationState | None = None
    payment_confirmation_id: str | None = None
    # Credit application. Credit-note records carry
    # ``credit_state`` (UNAPPLIED from intake) and, once applied,
    # ``applied_invoice_record_id``. Invoice records carry the
    # ledger ``credits_applied`` — the applications that have
    # reduced what is still owed on them. The invoice's approved
    # (extraction) total never changes: it is history. The OPEN
    # amount — face minus this ledger — is the derived figure
    # the packet and the payment runs read (credits.py).
    credit_state: CreditState | None = None
    credits_applied: list[CreditApplication] = Field(default_factory=list)
    applied_invoice_record_id: str | None = None
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
    """One proposed payment: an approved invoice at its OPEN
    amount — the approved face (``gross_amount``) minus the
    credits applied against it, which ride the line so the
    reader sees why the figure differs from the invoice's
    face. A fully credited invoice appears at 0.00 with its
    credits listed: the packet is the AP position, and a known
    invoice silently vanishing from it would hide exactly the
    fact the credits explain."""

    record_id: str
    supplier_name: str
    invoice_number: str
    currency: str
    amount: float  # the open amount — what would actually be paid
    gross_amount: float = 0.0  # the approved face, before credits
    credits_applied: list[CreditApplication] = Field(default_factory=list)


class UnappliedCredit(BaseModel):
    """An approved credit note no controller has applied yet,
    with the reason it currently cannot be applied (or that it
    is simply awaiting application). It sits on the packet
    because it is real money the supplier owes back — leaving
    it off would overstate what AP is about to pay out across
    the position, and hide the controller's worklist."""

    record_id: str
    credit_number: str
    supplier_name: str = ""
    amount: float  # positive — the size of the credit
    currency: str = "USD"
    invoice_number: str = ""  # the invoice the document names, when it names one
    reason: str = ""


class PaymentPacket(BaseModel):
    """The proposed payment list. A proposal only: generating it
    moves no money, and this type has no field that could."""

    packet_id: str
    lines: list[PaymentLine] = Field(default_factory=list)
    unapplied_credits: list[UnappliedCredit] = Field(default_factory=list)
    totals_by_currency: dict[str, float] = Field(default_factory=dict)
    generated_at: str = ""
    note: str = "Proposed payments for the customer's AP system of record. Nothing here has been paid."


PositionBucket = Literal["CURRENT", "1-30", "31-60", "61-90", "90+", "UNDATED"]
PositionState = Literal["OPEN_FREE", "IN_RUN", "PAID_PROPOSED"]
TieStatus = Literal["TIED", "POSITION_TIE_BREAK"]


class PositionLine(BaseModel):
    """One payable in the AP position (position.py): an APPROVED
    invoice with a positive OPEN amount, aged off its due date.
    ``state`` is where the money stands — free, held by an open
    run, or released as PAID_PROPOSED — a second dimension over
    the same open figure, so the state split and the ageing
    split are two readings of one total and must agree."""

    record_id: str
    supplier_id: str | None = None
    supplier_name: str = ""
    invoice_number: str = ""
    currency: str = "USD"
    face: float = 0.0
    credits: float = 0.0  # applied credits against the face
    open: float = 0.0
    invoice_date: str = ""
    due_date: str = ""  # "" when the record carries none → UNDATED
    days_past_due: int | None = None  # None when UNDATED
    bucket: PositionBucket = "UNDATED"
    state: PositionState = "OPEN_FREE"
    payment_run_id: str | None = None


class SupplierPosition(BaseModel):
    """One supplier's slice of the position, in one currency:
    the records under the row, and the row's own splits. The
    console expands a row into ``lines``; the totals are the
    sums of those lines, computed in the same pass."""

    supplier_id: str | None = None
    supplier_name: str = ""
    currency: str = "USD"
    lines: list[PositionLine] = Field(default_factory=list)
    bucket_totals: dict[str, float] = Field(default_factory=dict)
    state_totals: dict[str, float] = Field(default_factory=dict)
    face_total: float = 0.0
    credits_total: float = 0.0
    open_total: float = 0.0


class PositionAnomaly(BaseModel):
    """A record the position cannot age and will not hide: an
    APPROVED invoice whose applied-credit ledger exceeds its
    face, so its open amount is negative. Application refuses
    over-credit in code (credits.py), so this state is only
    reachable by a corrupted record — the position names it,
    keeps it out of the buckets, and lets it break the tie."""

    record_id: str
    supplier_name: str = ""
    invoice_number: str = ""
    currency: str = "USD"
    face: float = 0.0
    credits: float = 0.0
    open: float = 0.0
    code: str = "NEGATIVE_OPEN"
    detail: str = ""


class FraudHeldLine(BaseModel):
    """An invoice on fraud hold, disclosed beside the position:
    real money in dispute, excluded from the packet's universe
    (the packet reads APPROVED records only) and therefore from
    the tie — shown with its flags so the close sees it."""

    record_id: str
    supplier_name: str = ""
    invoice_number: str = ""
    currency: str = "USD"
    face: float = 0.0
    open: float = 0.0
    fraud_flags: list[str] = Field(default_factory=list)


class FullyCreditedLine(BaseModel):
    """An APPROVED invoice whose applied credits bring its open
    amount to exactly zero. It owes nothing and ages nothing;
    it is listed so the close can see the cover happened."""

    record_id: str
    supplier_name: str = ""
    invoice_number: str = ""
    currency: str = "USD"
    face: float = 0.0
    credits: float = 0.0


class CurrencyTie(BaseModel):
    """The tie-out for one currency: the packet's open total
    against the position's aged total, and the difference when
    they are not the same number."""

    currency: str = "USD"
    packet_total: float = 0.0
    position_total: float = 0.0
    difference: float = 0.0  # packet_total - position_total
    tied: bool = True


class PositionReport(BaseModel):
    """The AP position at close (position.py): every payable
    aged by due date as of ``as_of``, split by the state of its
    money, and tied to the packet's open total in the same
    pass. A read model — building it moves no money and writes
    no record. When the tie breaks the report says so in
    ``tie_status`` and names the break per currency; it never
    presents itself as tied when it is not."""

    as_of: str
    bucket_edges: list[int] = Field(default_factory=list)
    suppliers: list[SupplierPosition] = Field(default_factory=list)
    bucket_totals_by_currency: dict[str, dict[str, float]] = Field(
        default_factory=dict)
    state_totals_by_currency: dict[str, dict[str, float]] = Field(
        default_factory=dict)
    open_totals_by_currency: dict[str, float] = Field(default_factory=dict)
    ties: list[CurrencyTie] = Field(default_factory=list)
    tie_status: TieStatus = "TIED"
    fraud_held: list[FraudHeldLine] = Field(default_factory=list)
    fraud_held_totals_by_currency: dict[str, float] = Field(default_factory=dict)
    fully_credited: list[FullyCreditedLine] = Field(default_factory=list)
    anomalies: list[PositionAnomaly] = Field(default_factory=list)
    splits_agree: bool = True  # bucket split and state split both foot to the open totals
    note: str = (
        "Ageing runs off due dates as of the stated date. Fraud-held "
        "money is disclosed beside the position, outside the tie."
    )


RunStatus = Literal["AWAITING_TREASURER", "RELEASED", "RETURNED"]


class PaymentRunLine(BaseModel):
    """One member of a payment run: an approved invoice at its
    OPEN amount — the approved face (``gross_amount``) minus
    the credits applied against it, listed on the line so the
    treasurer sees why the figure that will leave the bank
    differs from the invoice's face. Credit notes are never
    members: a credit is applied against an invoice, not paid
    as a negative instruction (credits.py states the rule)."""

    record_id: str
    supplier_id: str | None = None
    supplier_name: str = ""
    invoice_number: str = ""
    doc_class: DocClass = "invoice"
    currency: str = "USD"
    amount: float = 0.0  # the open (net) amount — what the export instructs
    gross_amount: float = 0.0  # the approved face, before credits
    credits_applied: list[CreditApplication] = Field(default_factory=list)


class PaymentRefusal(BaseModel):
    """A record a run was asked to take and refused, with the
    reason in words. A run assembles from the eligible remainder
    and reports every refusal — nothing is silently dropped."""

    record_id: str
    code: str
    reason: str


class TreasurerDecision(BaseModel):
    """The treasurer's word on a run. ``return`` requires a
    reason — the AP team reworks the batch on it."""

    decision: Literal["release", "return"]
    actor: str
    reason: str = ""
    at: str = ""


class PaymentRun(BaseModel):
    """A stored payment run: approved payable records assembled
    for one value date, every refusal named, totals tied to the
    member lines, and a treasurer's gate on top. On release the
    bank-instruction export is generated once, stored in the
    object store, and pinned here by key + SHA-256 digest.

    A run is still a proposal. ``RELEASED`` means a human
    released the batch for the customer's payment systems to
    execute — nothing in this system executes a payment."""

    run_id: str
    value_date: str
    currencies: list[str] = Field(default_factory=list)  # empty = all currencies
    status: RunStatus = "AWAITING_TREASURER"
    lines: list[PaymentRunLine] = Field(default_factory=list)
    refusals: list[PaymentRefusal] = Field(default_factory=list)
    totals_by_currency: dict[str, float] = Field(default_factory=dict)
    decision: TreasurerDecision | None = None
    export_key: str | None = None
    export_digest: str | None = None
    export_generated_at: str | None = None
    audit: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    note: str = (
        "A controlled proposal batch: released means handed to the "
        "customer's payment systems, not paid by this system."
    )


ConfirmClass = Literal[
    "paid_as_proposed",
    "short_paid",
    "over_paid",
    "missing",
    "unexpected_line",
]

ConfirmStatus = Literal[
    "UNMATCHED", "AWAITING_CONTROLLER", "ACCEPTED", "FLAGGED_BACK",
]


class ConfirmationLine(BaseModel):
    """One reconciled item of a remittance confirmation: a member
    of the run, answered by the bank's advice — or an advice line
    that names no member. Both figures ride along whenever both
    exist; ``difference`` is paid − proposed, signed, and is
    None whenever the two sides are not comparable (a side is
    missing, or the money arrived in another currency)."""

    invoice_number: str
    classification: ConfirmClass
    proposed_amount: float | None = None
    paid_amount: float | None = None
    currency: str = "USD"
    member_record_id: str | None = None
    value_date: str = ""
    bank_reference: str = ""
    difference: float | None = None
    note: str = ""


class ConfirmationTotals(BaseModel):
    """The confirmation summary for one currency. The proposed
    side partitions the run's members — confirmed + short +
    over + missing must recompose the run's total for the
    currency exactly, or the confirmation names the arithmetic
    break. Unexpected lines are the bank's answers to questions
    the run never asked; their money is reported here and is
    never part of the tie."""

    currency: str
    run_total: float = 0.0
    confirmed_proposed: float = 0.0
    confirmed_paid: float = 0.0
    short_proposed: float = 0.0
    short_paid: float = 0.0
    shortfall: float = 0.0
    over_proposed: float = 0.0
    over_paid: float = 0.0
    excess: float = 0.0
    missing_proposed: float = 0.0
    unexpected_paid: float = 0.0


class PaymentConfirmation(BaseModel):
    """A stored reconciliation of one remittance advice against
    the payment run it answers: every member classified, the
    summary tied to the run's totals or the break named, and a
    controller decision on top. Evidence over the run — a
    confirmation never mutates the run, its export, the member
    records' approvals, or any bank data; the only writes it
    makes beyond its own record are the confirmation overlays
    on the member records it classifies.

    An advice that names no run on file (or no run at all) is
    held ``UNMATCHED`` with the reason on the record: it is
    never guessed onto a run, and there is nothing for a
    controller to decide until it is tied to one."""

    confirmation_id: str
    advice_record_id: str
    submission_id: str
    run_id: str | None = None
    status: ConfirmStatus = "AWAITING_CONTROLLER"
    unmatched_reason: str = ""
    lines: list[ConfirmationLine] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    totals: list[ConfirmationTotals] = Field(default_factory=list)
    totals_tie: bool = False
    flags: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    decision: ControllerDecision | None = None
    audit: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
