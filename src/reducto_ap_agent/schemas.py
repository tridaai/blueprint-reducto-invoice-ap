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
    "NEW_SUPPLIER_REVIEW",
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


class BankVersion(BaseModel):
    """One version of a supplier's bank details on the master
    (bankmaster.py): the details, the date they take effect,
    and the provenance of the recording — who recorded them,
    when, and what they were recorded FROM. Bank details are
    verified out of band; this record is the evidence that the
    verification happened, and it is append-only: a later
    version never rewrites an earlier one, so what the master
    said on any past date stays sayable.

    Version 1 is the seed — the details the deployment loaded
    the master with. It carries no ``effective_from`` (""),
    because it predates the record: it is effective for every
    date before the first dated version supersedes it. Every
    recorded version names its effective date, and effective
    dates are monotonic per supplier — a version takes effect
    after every version already on file — so the version in
    force on a date is never ambiguous."""

    version: int  # 1-based, per supplier, in recording order
    bank_account: str
    bank_name: str = ""
    effective_from: str = ""  # ISO date; "" only on the seed
    recorded_by: str = ""
    recorded_at: str = ""  # ISO timestamp (UTC) of the recording
    source: str = ""  # what the details were recorded from


class BankHistory(BaseModel):
    """One supplier's bank timeline (bankmaster.py): every
    version on file in recording order, and the version in
    force at ``as_of`` named as ``current_version`` — the one
    the ledger shows and a payment released at that date would
    pay. A read model: building it writes nothing."""

    supplier_id: str
    supplier_name: str
    as_of: str
    versions: list[BankVersion] = Field(default_factory=list)
    current_version: int = 1
    note: str = (
        "Versions are append-only and effective-dated: the "
        "fraud screen judges an invoice against the version in "
        "force on the invoice's own date, and history is never "
        "rewritten by a later recording."
    )


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
# run awaiting the treasurer. ``PAID_PROPOSED``: released runs
# have instructed the record's whole open amount — it sits in
# bank-instruction exports as a proposal, fully committed.
# Terminal: a paid-proposed record never joins another run.
# ``PART_PAID``: released runs have instructed part of the open
# amount; the remainder is still owed and may join a later run.
# A part-paid record that joins one reads IN_RUN again while
# held, and returns to PART_PAID if the run is returned. This
# is deliberately NOT a RecordStatus: the record stays
# APPROVED, because the approver's decision and the treasurer's
# batch are two different humans' acts and neither rewrites the
# other.
PaymentState = Literal["IN_RUN", "PAID_PROPOSED", "PART_PAID"]

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

# A purchase order's coverage state (coverage.py): how the
# cumulative billing of APPROVED invoices against the order
# stands against the order's total. ``WITHIN`` below the
# watch threshold, ``WATCH`` at or above it, ``EXCEEDED``
# billed past the total, ``UNMEASURED`` when the order is not
# on the register and no total exists to measure against —
# its billed sum is still listed, under no verdict.
CoverageState = Literal["WITHIN", "WATCH", "EXCEEDED", "UNMEASURED"]

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


class PaymentEntry(BaseModel):
    """One released instruction against one invoice: the part
    of it a payment run's export told the customer's payment
    systems to pay, recorded on the invoice when the treasurer
    released that run. ``amount`` is positive — the instructed
    part — and the invoice's open payable falls by exactly
    this much. The entry lives on the INVOICE's ledger
    (``payments_applied``) beside the credit applications,
    because that is where the money question is asked: what is
    still owed on this invoice. The run's own line stays the
    instruction's primary record; this entry is the invoice's
    view of the same act, written in the same step, at the
    line's amount — the two cannot disagree without one of
    them having been written outside the release path."""

    run_id: str
    amount: float
    currency: str = "USD"
    actor: str  # the treasurer who released the run
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
    or the total the released runs actually instructed for it
    across every part payment (``amount_basis`` names which).

    The evidence names the payment, never just the twin: the
    original record, the run that paid it most recently, who
    released it and when, and the amount the exports instructed
    (``amount_paid`` — what the released runs instructed in
    total, not a recomputation). The hold is a state of THIS
    record; the original record and its runs are never
    written by the watch."""

    original_record_id: str
    invoice_number: str  # the number as this arrival states it
    supplier_name: str = ""
    payment_run_id: str  # the most recent run that paid the original
    released_at: str = ""
    released_by: str = ""
    amount_paid: float = 0.0  # cumulative instructed across released runs
    paid_face_amount: float = 0.0  # the original's approved face
    currency: str = "USD"
    arrived_amount: float = 0.0  # this arrival's stated total
    amount_basis: Literal["FACE", "NET"] = "FACE"
    held_at: str = ""
    resolution: DuplicateHoldResolution | None = None


class SupplierHoldResolution(BaseModel):
    """A named controller's word on a new-supplier hold.
    ``ENROLLED``: the supplier was enrolled on the master —
    ``supplier_id`` is the id it was enrolled under, and
    ``detail`` is the provenance the bank details were
    recorded from (the enrolment's source statement, the
    bankmaster doctrine's own requirement). ``REFUSED``: the
    document was refused as not a known supplier, and
    ``detail`` is the controller's reason. Either way the
    resolution rides the record permanently, beside the
    hold that stopped it."""

    action: Literal["ENROLLED", "REFUSED"]
    actor: str
    detail: str
    supplier_id: str | None = None
    at: str = ""


class SupplierHold(BaseModel):
    """The new-supplier hold's evidence on one arrival
    (enrolment.py): the supplier name exactly as the document
    wrote it, its normalised form (the intake's own key — the
    group an enrolment resolves), and the near-miss: the
    master supplier the name most resembles, when one stands
    within the stated edit distance. A near-miss is evidence
    for the controller, never a join — resemblance enrols
    nobody, and the hold names it precisely so a misspelling
    of a governed supplier is refused as a misspelling, not
    enrolled as a second payee."""

    name_as_written: str
    normalised_name: str
    near_miss_supplier_id: str | None = None
    near_miss_supplier_name: str | None = None
    held_at: str = ""
    resolution: SupplierHoldResolution | None = None


class NewSupplierGroup(BaseModel):
    """One identity on the new-supplier review queue
    (enrolment.py): every held record that wrote its supplier
    as this name, grouped under the intake's own normalisation
    — so a group is exactly what one enrolment resolves. The
    grouping is a read model over held records; it stores
    nothing, and enrolling or refusing writes the records,
    never the group."""

    name_as_written: str
    normalised_name: str
    record_ids: list[str] = Field(default_factory=list)
    document_numbers: list[str] = Field(default_factory=list)
    totals_by_currency: dict[str, float] = Field(default_factory=dict)
    held_since: str = ""  # the earliest hold in the group
    near_miss_supplier_id: str | None = None
    near_miss_supplier_name: str | None = None


class NewSupplierReview(BaseModel):
    """The new-supplier review queue (enrolment.py): every
    payable document currently stopped at the door because
    its supplier is not on the master, grouped by the identity
    it stated. Two exits per group or record, both a named
    controller's act: enrol the supplier (which releases every
    held record of the identity back into the pipeline), or
    refuse a record as not a known supplier."""

    groups: list[NewSupplierGroup] = Field(default_factory=list)
    held_count: int = 0
    note: str = (
        "A held document has not been matched and has not "
        "been fraud-screened — there is no master record to "
        "screen it against. It is not a payable, and no gate "
        "can decide it. Enrolment is a bank-details recording "
        "and carries that governance whole: a named controller, "
        "the details, and what they were verified from."
    )


class Enrolment(BaseModel):
    """The result of enrolling one new supplier (service.py):
    the master record created, the version-1 bank record its
    details stand as, and every held record the enrolment
    released back into the pipeline."""

    supplier: Supplier
    bank_version: BankVersion
    released_record_ids: list[str] = Field(default_factory=list)


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
    # Released payment instructions against this invoice
    # (payments.py): one entry per released run that took a
    # part of it, at that run's line amount. Written only by
    # the release path. The OPEN amount — face minus the
    # credits above minus this ledger — is the derived figure
    # the packet, the runs, the position, and the ledger all
    # read (credits.py computes it; the paid total beside it).
    payments_applied: list[PaymentEntry] = Field(default_factory=list)
    # The paid-duplicate watch (duplicates.py). Set at intake
    # when an invoice-class arrival answers to an invoice that
    # was already released for payment, at that invoice's face
    # or its net paid figure; the record's status is then
    # PAID_DUPLICATE_HOLD until a named controller resolves it.
    # The payload stays on the record after resolution — it is
    # the permanent evidence of why the document was stopped,
    # and of who let it through or confirmed it, and why.
    duplicate_hold: PaidDuplicateHold | None = None
    # The new-supplier hold (enrolment.py). Set at intake when
    # a payable document's supplier resolves to no master
    # record — by neither the name it states nor the purchase
    # order it references; the record's status is then
    # NEW_SUPPLIER_REVIEW until a named controller enrols the
    # supplier (releasing the record back into the pipeline)
    # or refuses the document. The payload stays on the record
    # after either act — the permanent evidence of why the
    # document stopped at the door, and of who opened it.
    supplier_hold: SupplierHold | None = None
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
    credits applied against it and the payments already
    released against it, both ledgers riding the line so the
    reader sees why the figure differs from the invoice's
    face. A fully credited or fully paid invoice appears at
    0.00 with its ledgers listed: the packet is the AP
    position, and a known invoice silently vanishing from it
    would hide exactly the fact the ledgers explain."""

    record_id: str
    supplier_name: str
    invoice_number: str
    currency: str
    amount: float  # the open amount — what would actually be paid
    gross_amount: float = 0.0  # the approved face, before credits
    credits_applied: list[CreditApplication] = Field(default_factory=list)
    payments_applied: list[PaymentEntry] = Field(default_factory=list)


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
PositionState = Literal["OPEN_FREE", "IN_RUN", "PAID_PROPOSED", "PART_PAID"]
TieStatus = Literal["TIED", "POSITION_TIE_BREAK"]


class PositionLine(BaseModel):
    """One payable in the AP position (position.py): an APPROVED
    invoice with a positive OPEN amount, aged off its due date.
    ``state`` is where the open money stands — free, held by an
    open run, or part paid with the remainder still owed — a
    second dimension over the same open figure, so the state
    split and the ageing split are two readings of one total
    and must agree. ``paid`` is the cumulative amount released
    runs have already instructed against this invoice: a memo
    figure beside the open amount, never part of it."""

    record_id: str
    supplier_id: str | None = None
    supplier_name: str = ""
    invoice_number: str = ""
    currency: str = "USD"
    face: float = 0.0
    credits: float = 0.0  # applied credits against the face
    paid: float = 0.0  # cumulative released against this invoice
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
    paid_total: float = 0.0  # paid cumulative over the row's aged lines
    open_total: float = 0.0


class PositionAnomaly(BaseModel):
    """A record the position cannot age and will not hide: an
    APPROVED invoice whose applied ledgers (credits, and any
    payments recorded against it) exceed its face, so its open
    amount is negative. Application refuses over-credit and
    run assembly refuses an over-committed part in code, so
    this state is only reachable by a corrupted record — the
    position names it, keeps it out of the buckets, and lets
    it break the tie."""

    record_id: str
    supplier_name: str = ""
    invoice_number: str = ""
    currency: str = "USD"
    face: float = 0.0
    credits: float = 0.0
    paid: float = 0.0
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


class NewSupplierHeldLine(BaseModel):
    """An invoice stopped at the door because its supplier is
    not on the master, disclosed beside the position: a payee
    nobody has enrolled is not a payable — it stands outside
    the packet's universe (the packet reads APPROVED records
    only) and therefore outside the tie — shown with the name
    exactly as the document wrote it, so the close sees the
    money that is waiting on an enrolment decision."""

    record_id: str
    supplier_name: str = ""  # as the document wrote it — no master name exists
    invoice_number: str = ""
    currency: str = "USD"
    face: float = 0.0
    open: float = 0.0


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


class FullyPaidLine(BaseModel):
    """An APPROVED invoice whose released payments (with any
    credits) bring its open amount to exactly zero. It owes
    nothing and ages nothing; it is listed so the close can
    see the payment happened — the counterpart of the fully
    credited line, for money that left by instruction instead
    of by netting. ``paid`` is the cumulative the released
    runs instructed, in one part or several."""

    record_id: str
    supplier_name: str = ""
    invoice_number: str = ""
    currency: str = "USD"
    face: float = 0.0
    credits: float = 0.0
    paid: float = 0.0


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
    paid_totals_by_currency: dict[str, float] = Field(default_factory=dict)
    ties: list[CurrencyTie] = Field(default_factory=list)
    tie_status: TieStatus = "TIED"
    fraud_held: list[FraudHeldLine] = Field(default_factory=list)
    fraud_held_totals_by_currency: dict[str, float] = Field(default_factory=dict)
    new_supplier_held: list[NewSupplierHeldLine] = Field(default_factory=list)
    new_supplier_held_totals_by_currency: dict[str, float] = Field(
        default_factory=dict)
    fully_credited: list[FullyCreditedLine] = Field(default_factory=list)
    fully_paid: list[FullyPaidLine] = Field(default_factory=list)
    anomalies: list[PositionAnomaly] = Field(default_factory=list)
    splits_agree: bool = True  # bucket split and state split both foot to the open totals
    note: str = (
        "Ageing runs off due dates as of the stated date. Fraud-held "
        "money and money held at the door for an unenrolled supplier "
        "are disclosed beside the position, outside the tie."
    )


RunStatus = Literal["AWAITING_TREASURER", "RELEASED", "RETURNED"]


class PaymentRunLine(BaseModel):
    """One member of a payment run: an approved invoice at the
    amount this run will instruct — its OPEN amount
    (``open_amount``: the approved face ``gross_amount`` minus
    the credits applied against it and the payments already
    released), or a stated part of it when the treasurer's
    batch pays the invoice in parts. The credits ride the line
    so the treasurer sees why the figure that will leave the
    bank differs from the invoice's face. Credit notes are
    never members: a credit is applied against an invoice, not
    paid as a negative instruction (credits.py states the
    rule). The line is the instructed truth: once assembled it
    is never recomputed, and on release the invoice's paid
    ledger grows by exactly ``amount``."""

    record_id: str
    supplier_id: str | None = None
    supplier_name: str = ""
    invoice_number: str = ""
    doc_class: DocClass = "invoice"
    currency: str = "USD"
    amount: float = 0.0  # the instructed amount — what the export instructs
    open_amount: float = 0.0  # the invoice's full open amount at assembly
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
    "NOT_OURS",
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
    payments_applied: list[PaymentEntry] = Field(default_factory=list)
    paid_total: float = 0.0
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
    # The purchase order this invoice names, and that order's
    # coverage state (coverage.py), read out of the coverage
    # view built over the same records — display only: the
    # ledger's totals and its tie never read these fields.
    # ``po_reference`` is "" when the invoice names no order;
    # the state is then None, there being no order to stand
    # in any state.
    po_reference: str = ""
    po_coverage_state: CoverageState | None = None


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
    # The bank version in force at the ledger's as-of (the
    # version whose details are shown above), and how many
    # versions the master holds for this supplier in total —
    # the count is the signal that the details have a past;
    # the timeline itself is served by the bank-history read.
    bank_version: int = 1
    bank_version_count: int = 1
    effective_from: str = ""  # the shown version's; "" = the seed
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
    paid_total: float = 0.0
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
    # Of the unmatched documents, how many are currently
    # stopped at the door in NEW_SUPPLIER_REVIEW — the subset
    # awaiting a controller's enrol-or-refuse word, as opposed
    # to documents that were decided and simply joined nowhere.
    new_supplier_hold_count: int = 0
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


class NotOursMark(BaseModel):
    """A controller's word that a held advice is not this
    company's money to work: the payment belongs to another
    company's batch, or the bank sent another customer's
    advice. The mark closes the worklist item; the advice and
    its held confirmation stay on record with the reason."""

    actor: str
    reason: str
    at: str = ""


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
    controller to decide until it is tied to one. The tie, when
    it comes, is a human act from the worklist (worklist.py):
    a NEW confirmation is built against the run the controller
    names, carrying ``linked_by`` / ``linked_rule`` /
    ``linked_from`` — the held record itself is never rewritten
    into a matched one. A held advice a controller marks
    NOT_OURS closes with the mark on this record instead."""

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
    # The worklist link, when this confirmation exists because
    # a controller tied a held advice to a run by hand: who
    # linked it, under which candidate rule (or "manual" when
    # the lines were the controller's own choice), and the
    # held confirmation it was linked from.
    linked_by: str = ""
    linked_rule: str = ""
    linked_from: str | None = None
    not_ours: NotOursMark | None = None
    audit: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


WorklistRule = Literal[
    "exact_line", "reference_near", "combined_sum",
    "exact_amount", "near_amount", "manual",
]


class WorklistCandidate(BaseModel):
    """One suggested link between one advice line and one or
    more unsettled lines of one released run (worklist.py):
    the rule that produced it, its strength under the printed
    ranking, and the numbers — so the controller sees WHY it
    is suggested before deciding anything. A suggestion is
    not a claim: the same run line may be suggested against
    several advices, and nothing is confirmed until a
    controller links."""

    rule: WorklistRule
    strength: int = 0
    run_id: str
    member_record_ids: list[str] = Field(default_factory=list)
    member_invoice_numbers: list[str] = Field(default_factory=list)
    member_amounts: list[float] = Field(default_factory=list)
    member_total: float = 0.0
    advice_amount: float = 0.0
    currency: str = "USD"
    # paid − proposed for single-line candidates; None when
    # the two sides are equal or not comparable singly (a
    # combined candidate's sides agree in sum by construction).
    delta: float | None = None
    note: str = ""


class WorklistAdviceLine(BaseModel):
    """One paid line of a held advice, as the bank wrote it,
    with the candidate links computed for it."""

    index: int = 0
    invoice_number: str = ""
    amount_paid: float = 0.0
    currency: str = "USD"
    value_date: str = ""
    bank_reference: str = ""
    candidates: list[WorklistCandidate] = Field(default_factory=list)


class WorklistEntry(BaseModel):
    """One held advice on the worklist: the held confirmation,
    why it was held, its lines as written, and every candidate
    the arithmetic found. An entry with no candidates is
    still an entry — the pile is the whole truth, ranked."""

    confirmation_id: str
    advice_record_id: str
    submission_id: str
    unmatched_reason: str = ""
    advice_lines: list[WorklistAdviceLine] = Field(default_factory=list)
    best_strength: int = 0
    created_at: str = ""


class AdviceWorklist(BaseModel):
    """The unmatched-advice worklist (worklist.py): every held
    advice not yet linked and not marked not-ours, ranked by
    its best candidate's strength (strongest first; an advice
    with no candidate stands last, in the order it was held),
    with the candidate rules and the ranking printed on the
    report — the arithmetic is inspectable, and it confirms
    nothing by itself."""

    entries: list[WorklistEntry] = Field(default_factory=list)
    ranking_rule: str = ""
    near_amount_abs: float = 0.0
    reference_max_edits: int = 0
    combined_max_lines: int = 0


class CoverageLine(BaseModel):
    """One invoice billing against an order, in billing order
    (coverage.py): the invoice's approved face, the credits
    applied against it, and its BILLABLE figure — face minus
    credits, the amount this invoice charges the order.
    Payments are not on the line: paying an invoice settles
    its bill, it does not shrink it, so a part-paid invoice
    bills in full. ``cumulative_billed`` and
    ``cumulative_pct`` are the running totals including this
    line — the point where an order crosses a threshold is
    visible on the line that crosses it. The pct is None
    only when the order's total is unknown (UNMEASURED)."""

    record_id: str
    invoice_number: str = ""
    supplier_name: str = ""
    invoice_date: str = ""
    face: float = 0.0
    credits: float = 0.0  # applied credits against the face
    billable: float = 0.0
    cumulative_billed: float = 0.0
    cumulative_pct: float | None = None


class CoverageExcluded(BaseModel):
    """An approved invoice naming this order that coverage did
    NOT sum into it, with the reason in words — in practice a
    currency that is not the order's: coverage never converts
    currencies silently, so the invoice is listed here, out of
    the billed figure, where a human can see it was seen."""

    record_id: str
    invoice_number: str = ""
    amount: float = 0.0  # the invoice's billable figure, unsummed
    currency: str = "USD"
    reason: str = ""


class OrderCoverage(BaseModel):
    """One purchase order's coverage (coverage.py): the total
    the order register states (``total_source`` says where it
    came from — the register, or NOT_ON_FILE, in which case
    ``total`` is None and no verdict exists), the cumulative
    billed against it by APPROVED invoices, the coverage
    percentage (None when unmeasured), and the state the
    thresholds in policy.py name. The contributing invoices
    ride as lines, in billing order, each with its running
    cumulative."""

    po_number: str
    supplier_id: str | None = None
    supplier_name: str = ""
    currency: str = "USD"
    total: float | None = None
    total_source: Literal["ORDER_REGISTER", "NOT_ON_FILE"] = "ORDER_REGISTER"
    billed: float = 0.0
    pct: float | None = None
    state: CoverageState = "WITHIN"
    lines: list[CoverageLine] = Field(default_factory=list)
    excluded: list[CoverageExcluded] = Field(default_factory=list)


class CoverageReport(BaseModel):
    """PO coverage over the whole store (coverage.py): every
    order on the register plus every order an approved
    invoice names, ranked EXCEEDED first (fullest first),
    then WATCH, then WITHIN, UNMEASURED last — named, never
    hidden. A read model: building it writes nothing, moves
    no money, and changes no match outcome — the match is
    per-invoice arithmetic and coverage is a second,
    cumulative fact beside it."""

    watch_pct: float = 80.0
    orders: list[OrderCoverage] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    note: str = (
        "Billed is the sum over APPROVED invoices of face minus "
        "applied credits; payments never reduce it — paying "
        "settles a bill, it does not un-bill it. Coverage "
        "informs the approver's gate; it never blocks it, and "
        "it never rewrites a match outcome."
    )


class RecordCoverage(BaseModel):
    """The coverage fact for one invoice record, COUNTING this
    invoice: the order it names, the billed figure including
    this record's own billable amount (whether or not it is
    approved yet), and the state the order stands in on that
    figure. This is what rides the approver's surfaces before
    they decide — ``fact`` is the sentence, in the words every
    surface quotes. ``counted`` says whether the record is
    itself inside the billed sum already (an APPROVED record
    is; an awaiting one is added for the reading)."""

    record_id: str
    invoice_number: str = ""
    po_number: str
    currency: str = "USD"
    total: float | None = None
    total_source: Literal["ORDER_REGISTER", "NOT_ON_FILE"] = "ORDER_REGISTER"
    billed: float = 0.0  # approved billing, this record included if counted
    billed_including: float = 0.0  # billed, counting this record either way
    pct: float | None = None  # of billed_including, when a total exists
    state: CoverageState = "WITHIN"
    counted: bool = False
    fact: str = ""
