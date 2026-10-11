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
    "PAID_DUPLICATE_HOLD",
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


class DuplicateHoldResolution(BaseModel):
    """A named controller's word on a paid-duplicate hold.
    ``CONFIRMED_DUPLICATE``: the arrival is the paid invoice
    again — the record is refused permanently, and nothing is
    deleted. ``RELEASED_NOT_DUPLICATE``: the arrival is a
    different bill that happens to answer to a paid invoice's
    number at a paid figure — the hold lifts and the record
    returns to the state its own match earned. Either way the
    resolution rides the record permanently: actor, reason,
    and the moment are the evidence the next arrival is
    judged against."""

    action: Literal["CONFIRMED_DUPLICATE", "RELEASED_NOT_DUPLICATE"]
    actor: str
    reason: str
    at: str = ""


class PaidDuplicateHold(BaseModel):
    """The paid-duplicate watch's evidence against one arrival
    (duplicates.py): this invoice-class document states the
    number of an invoice that was already released for payment,
    at an amount that is that invoice's paid story — its face,
    or the net figure a released run actually instructed after
    credits were applied (``amount_basis`` names which).

    The evidence names the payment, never just the twin: the
    original record, the run that released it, who released it
    and when, and the amount the export instructed
    (``amount_paid`` — the run line, not a recomputation). The
    hold is a state of THIS record; the original record and
    its run are never written by the watch."""

    original_record_id: str
    invoice_number: str  # the number as this arrival states it
    supplier_name: str = ""
    payment_run_id: str
    released_at: str = ""
    released_by: str = ""
    amount_paid: float = 0.0  # the run line's instructed (net) amount
    paid_face_amount: float = 0.0  # the original's approved face
    currency: str = "USD"
    arrived_amount: float = 0.0  # this arrival's stated total
    amount_basis: Literal["FACE", "NET"] = "FACE"
    held_at: str = ""
    resolution: DuplicateHoldResolution | None = None


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
    # The paid-duplicate watch (duplicates.py). Set at intake
    # when an invoice-class arrival answers to an invoice that
    # was already released for payment, at that invoice's face
    # or its net paid figure; the record's status is then
    # PAID_DUPLICATE_HOLD until a named controller resolves it.
    # The payload stays on the record after resolution — it is
    # the permanent evidence of why the document was stopped,
    # and of who let it through or confirmed it, and why.
    duplicate_hold: PaidDuplicateHold | None = None
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


class LedgerDocument(BaseModel):
    """One document on a supplier's ledger (ledger.py): every
    record the intake joined to this supplier, whatever its
    class or state — the relationship's paper trail, before
    any money reading is taken of it."""

    record_id: str
    submission_id: str
    doc_class: DocClass = "unknown"
    status: RecordStatus = "RECEIVED"
    document_number: str = ""  # as extracted; credit notes carry theirs here too
    total: float | None = None  # the extraction total, as read (credits negative)
    refusal_reason: str | None = None
    fraud_flags: list[str] = Field(default_factory=list)
    created_at: str = ""


class LedgerInvoice(BaseModel):
    """One invoice on the ledger, with the whole of its money
    story: the approved face, the credits applied against it
    (the ledger entries themselves, so each figure traces to
    its record), the open amount the packet and the runs read,
    and where the money stands — its run membership, the
    bank's confirmation answer, and its ageing bucket in the
    position at the ledger's as-of."""

    record_id: str
    invoice_number: str = ""
    status: RecordStatus = "RECEIVED"
    currency: str = "USD"
    face: float = 0.0
    credits_applied: list[CreditApplication] = Field(default_factory=list)
    credits_total: float = 0.0
    open: float = 0.0
    match_status: str | None = None
    exception_codes: list[str] = Field(default_factory=list)
    payment_state: PaymentState | None = None
    payment_run_id: str | None = None
    payment_confirmation: PaymentConfirmationState | None = None
    payment_confirmation_id: str | None = None
    # The paid-duplicate watch's hold on this arrival, when it
    # carries one — the evidence (the payment it answers to)
    # and, once a controller has ruled, the resolution. An
    # invoice with no hold leaves it None; the ledger shows
    # the hold so a supplier call sees why the document stopped.
    duplicate_hold: PaidDuplicateHold | None = None
    due_date: str = ""
    bucket: PositionBucket | None = None  # the position's reading at as_of
    fully_credited: bool = False  # open exactly zero, covered by credits


class LedgerCredit(BaseModel):
    """One credit note on the ledger: its size, the invoice its
    own document names, and its application state. While
    UNAPPLIED it carries the reason it sits unapplied, in the
    same words the packet and the application path use
    (credits.unapplied_reason) — the ledger never invents a
    second explanation."""

    record_id: str
    credit_number: str = ""
    status: RecordStatus = "RECEIVED"
    currency: str = "USD"
    amount: float = 0.0  # positive — the size of the credit
    credit_state: CreditState | None = None
    credited_invoice_number: str = ""
    applied_invoice_record_id: str | None = None
    unapplied_reason: str | None = None  # None once APPLIED


class LedgerBreak(BaseModel):
    """One open statement break against this supplier: a line a
    reconciliation classified as anything but matched, on a
    reconciliation no controller has accepted. Both figures
    ride along whenever both exist — the break is the two
    numbers, exactly as reconciliation left them."""

    reconciliation_id: str
    statement_record_id: str
    period: str = ""
    recon_status: ReconStatus = "AWAITING_CONTROLLER"
    invoice_number: str
    classification: ReconClass
    statement_amount: float | None = None
    ap_amount: float | None = None
    currency: str = "USD"
    note: str = ""


class LedgerBank(BaseModel):
    """The supplier's bank details AS THE MASTER RECORDS THEM —
    the only bank details a ledger ever shows. A document that
    presented different details put the supplier on fraud hold
    at intake; that warning rides here in the guardrail's own
    words, with the records that raised it. ``warning`` is
    None exactly when no document on file disagrees with the
    master."""

    bank_name: str = ""
    bank_account: str = ""  # the master's approved account
    warning: str | None = None
    flags: list[str] = Field(default_factory=list)
    flagged_record_ids: list[str] = Field(default_factory=list)


class LedgerCurrencyTotals(BaseModel):
    """The ledger's payable totals for one currency, and their
    tie to the position: the ledger sums every APPROVED invoice
    at its open amount; the position's supplier row sums the
    same universe aged at the same as-of — the open figure
    does not depend on the as-of, only its ageing does, so the
    two must agree to the cent. When they cannot — a corrupted
    record the position keeps out of its ageing as a named
    anomaly — the difference is carried here, not smoothed."""

    currency: str = "USD"
    approved_invoices: int = 0
    face_total: float = 0.0
    credits_total: float = 0.0
    open_total: float = 0.0
    position_open_total: float = 0.0
    difference: float = 0.0  # ledger open − position open
    tied: bool = True


class SupplierLedger(BaseModel):
    """One supplier, the whole relationship (ledger.py): the
    master identity and its bank, every document joined to
    it, every invoice's money story, every credit and its
    state, the statement breaks still open against it, and
    totals that tie to the AP position at ``as_of``. A read
    model — building it writes nothing and moves no money."""

    supplier_id: str
    supplier_name: str
    master_currency: str = "USD"
    as_of: str
    bank: LedgerBank
    documents: list[LedgerDocument] = Field(default_factory=list)
    invoices: list[LedgerInvoice] = Field(default_factory=list)
    credits: list[LedgerCredit] = Field(default_factory=list)
    open_breaks: list[LedgerBreak] = Field(default_factory=list)
    totals: list[LedgerCurrencyTotals] = Field(default_factory=list)
    tie_status: TieStatus = "TIED"
    note: str = (
        "Totals cover APPROVED invoices at their open amounts and tie "
        "to the AP position at the stated as-of. Fraud-held invoices "
        "are listed with their flags and stand outside both totals — "
        "the position discloses them beside its own tie the same way."
    )


class UnmatchedDocument(BaseModel):
    """A document the intake could not join to the supplier
    master: its extracted supplier name matched no master entry
    exactly, and no referenced purchase order resolved it. It
    belongs to no supplier's ledger; it is listed here, with
    the name as the document wrote it, so a supplier trading
    under three spellings shows as a data problem instead of a
    third of the story quietly going missing."""

    record_id: str
    submission_id: str
    doc_class: DocClass = "unknown"
    status: RecordStatus = "RECEIVED"
    name_as_written: str = ""  # "" when the document names no supplier
    document_number: str = ""
    total: float | None = None
    refusal_reason: str | None = None


class SupplierIndexEntry(BaseModel):
    """One master supplier on the ledger index: what the AP
    lead needs before opening the page — how much paper, how
    much is still owed, whether a statement break is open,
    and whether any document has disagreed with the master's
    bank details."""

    supplier_id: str
    supplier_name: str
    currency: str = "USD"
    document_counts: dict[str, int] = Field(default_factory=dict)
    open_totals_by_currency: dict[str, float] = Field(default_factory=dict)
    open_break_count: int = 0
    bank_warning: bool = False
    # Arrivals currently held by the paid-duplicate watch
    # against this supplier's paid history — a marker on the
    # row, because a held duplicate is the first thing an AP
    # lead needs to know before a supplier call.
    duplicate_hold_count: int = 0


class SupplierIndex(BaseModel):
    """The ledger index (ledger.py): every supplier on the
    master — including those with no documents yet, because
    the master is the governed list — and, named as its own
    section, the documents that joined to no supplier at all."""

    as_of: str
    suppliers: list[SupplierIndexEntry] = Field(default_factory=list)
    unmatched_count: int = 0
    unmatched: list[UnmatchedDocument] = Field(default_factory=list)
    note: str = (
        "Documents join a supplier by the intake's own discipline: "
        "the extracted supplier name, matched exactly against the "
        "master, or the supplier of the purchase order the document "
        "references. Anything else is listed under Unmatched — never "
        "attached by resemblance, never dropped."
    )


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
