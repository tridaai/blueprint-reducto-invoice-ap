"""Credit notes: applied in code, against the invoice they name.

An invoice says what AP owes. A credit note is the supplier
saying some of that is owed back — damaged goods, a price
correction, a rebate. The credit only means anything attached
to the invoice it reduces, so this module owns the attachment:

- **A credit names its invoice, or it waits.** The document
  itself carries the reference (the ``credited_invoice_number``
  extraction field). A credit naming no invoice, or an invoice
  that is not on file, is held UNAPPLIED with the reason named.
  It is never matched to an invoice by amount similarity:
  amount-matching is exactly how double-crediting happens — two
  credits for the same amount against one invoice both "fit",
  and the payable falls twice for one debt.
- **Application is a controller's act, in code.** A named
  controller applies an approved credit against its approved
  invoice. The application is recorded on the invoice's ledger
  (``credits_applied``) and the credit flips to APPLIED,
  pointing at the invoice's record. Partial coverage is the
  normal case and tracked cumulatively: several credits may
  land on one invoice, each in full, until the open amount
  reaches zero.
- **Credits apply to the cent, in full, or not at all.** No
  tolerance — the match policy's tolerances absorb document
  reading noise, and a credit is not a reading, it is money
  (the rule round 4 set for confirmations, applied here). A
  credit larger than the invoice's open amount is refused
  OVER_CREDIT with both figures; a credit note is never split
  across applications, because a half-applied credit note is
  an invitation to apply the other half twice.
- **The approved amount never changes.** The invoice's
  extraction total is history — the approver approved that
  figure. What changes is the derived OPEN amount: face minus
  the credit ledger minus the payments already released
  against it (payments.py records those on the invoice at
  release). The packet and the payment runs read the open
  amount; the export instructs it. One amount rule, computed
  here, reused everywhere — the way the packet's amounts were
  reused by the runs before.
- **A credit never joins a payment run.** Runs pay invoices at
  their open amounts. A credit note riding a run as a negative
  instruction would pay the supplier a refund as a separate
  bank movement while the invoice it credits could still be
  paid in full in another batch — the netting would depend on
  which records a treasurer happened to batch together. Applied
  against the invoice, the reduction holds wherever the invoice
  goes.
- **Late credits are refused by name, not forced in.** A credit
  against an invoice held by an open run is refused
  TARGET_IN_RUN — the batch was assembled on the open amount
  at assembly time, and it is returned or released as
  assembled. A credit against a PAID_PROPOSED invoice is
  refused TARGET_PAID_PROPOSED, terminally: that money's story
  belongs to the released run and its remittance confirmation,
  and rewriting the invoice's payable now would falsify both.
  The credit stays on file, UNAPPLIED, visible on the packet
  with its reason — in a live deployment it is next cycle's
  business, settled against the supplier's future invoices;
  carrying a supplier credit balance forward is a ledger this
  blueprint deliberately does not build.
- **A part-paid invoice is a live target for its remainder.**
  PART_PAID is not PAID_PROPOSED: part of the payable is
  still open, and a credit reduces what is actually still
  owed. It applies against the OPEN remainder — the same
  derived figure — so a credit larger than the remainder is
  OVER_CREDIT against the remainder's figures, never against
  the face the supplier originally billed. The released part
  is untouched: the credit reduces the future, not the past.
"""

from __future__ import annotations

from .schemas import CreditApplication, DocumentRecord

# Record states that can never become the target of an
# application. A duplicate copy (EXCEPTION), a rejected record,
# a refused document: none of them is a payable anyone will
# settle, so when the live copy of an invoice exists beside a
# dead one, the credit names the live one.
_DEAD_STATES = ("EXCEPTION", "REJECTED", "REFUSED")


def _cents(amount: float) -> float:
    return round(float(amount), 2)


def _normalise(number: str) -> str:
    return str(number).strip().upper()


def credit_amount(credit: DocumentRecord) -> float:
    """The size of a credit note, as a positive figure.

    Credit notes carry negative totals (that is what makes
    them credits); the ledger and every reason quote the
    magnitude, so a human reads "credit of 200.00 against an
    open 1400.00" instead of juggling signs."""
    if credit.extraction is None:
        return 0.0
    return _cents(abs(float(credit.extraction.value("total") or 0.0)))


def credited_invoice_number(credit: DocumentRecord) -> str:
    """The invoice number the credit's own document names —
    the extraction field, verbatim. Empty when the document
    names none, which is a held state with its own reason,
    never a gap another field (the PO, an amount) fills in."""
    if credit.extraction is None:
        return ""
    value = credit.extraction.value("credited_invoice_number")
    return str(value).strip() if value else ""


def paid_amount(record: DocumentRecord) -> float:
    """What released runs have already instructed against an
    invoice record: the sum of its payments ledger, written at
    release from the runs' own lines (payments.py). Zero for
    an invoice no released run has taken. This is the memo
    figure beside the open amount — the paid half of the one
    computation below."""
    return _cents(sum(entry.amount for entry in record.payments_applied))


def open_amount(record: DocumentRecord) -> float:
    """What is still owed on an invoice record: its approved
    face minus the credits applied against it minus the
    payments already released against it. The face is the
    extraction total and never moves; this is the one derived
    figure the packet, the runs, the position, the ledger, and
    the export all read."""
    if record.extraction is None:
        return 0.0
    face = float(record.extraction.value("total") or 0.0)
    applied = sum(entry.amount for entry in record.credits_applied)
    return _cents(face - applied - paid_amount(record))


def billable_amount(record: DocumentRecord) -> float:
    """What an invoice record bills against the purchase order
    it names: its approved face minus the credits applied
    against it — and NOT minus payments. A credit reduces the
    bill itself (the supplier took money back off what was
    charged); a payment only settles a bill that stands, so
    paying an invoice, in part or in full, never un-bills it.
    This is the figure PO coverage accumulates (coverage.py):
    ``open_amount`` answers "what is still owed", this answers
    "what has been charged against the order", and the two
    differ by exactly the payments already released."""
    if record.extraction is None:
        return 0.0
    face = float(record.extraction.value("total") or 0.0)
    applied = sum(entry.amount for entry in record.credits_applied)
    return _cents(face - applied)


def _resolve_target(
    credit: DocumentRecord, records: list[DocumentRecord]
) -> tuple[DocumentRecord | None, tuple[str, str] | None]:
    """The invoice record a credit names, or the named reason
    no single record answers to the name.

    Resolution is by the document's own reference, within the
    credit's own supplier — a credit from one supplier against
    another supplier's invoice is a filing error, named as
    such, not a match."""
    number = credited_invoice_number(credit)
    named = [
        r for r in records
        if r.doc_class == "invoice" and r.extraction is not None
        and _normalise(str(r.extraction.value("invoice_number") or ""))
        == _normalise(number)
    ]
    if not named:
        return None, (
            "INVOICE_NOT_ON_FILE",
            f"the credit names invoice {number}, which is not on file — "
            "a credit is applied only against an invoice AP holds",
        )
    own = [r for r in named if r.supplier_id == credit.supplier_id]
    if not own:
        other = named[0]
        return None, (
            "INVOICE_NOT_ON_FILE",
            f"invoice {number} is on file for {other.supplier_name}, not "
            f"for this credit's supplier ({credit.supplier_name}) — a "
            "credit reduces only its own supplier's invoices",
        )
    live = [r for r in own if r.status not in _DEAD_STATES]
    if len(live) == 1:
        return live[0], None
    if len(live) > 1:
        approved = [r for r in live if r.status == "APPROVED"]
        if len(approved) == 1:
            return approved[0], None
        return None, (
            "AMBIGUOUS_INVOICE_REFERENCE",
            f"invoice {number} answers to {len(live)} live records "
            f"({', '.join(r.record_id for r in live)}) — the credit "
            "cannot be applied until the duplication is resolved",
        )
    # Only dead copies answer to the name. The first of them is
    # the target in name only — its state is the blocker, and
    # naming it beats reporting the invoice as missing.
    return own[0], None


def application_blocker(
    credit: DocumentRecord, records: list[DocumentRecord]
) -> tuple[str, str] | None:
    """Why this credit cannot be applied right now, as
    ``(code, reason)`` — or None when it can. Every refusal in
    the credit path is computed here, so the service, the
    packet, the API, and the console all refuse in the same
    words. Check order matters: the credit's own state first,
    then its reference, then the target's state, then the
    money."""
    if credit.credit_state == "APPLIED":
        return (
            "ALREADY_APPLIED",
            f"already applied against invoice record "
            f"{credit.applied_invoice_record_id} — a credit applies once",
        )
    if credit.status == "FRAUD_REVIEW":
        return (
            "CREDIT_NOT_APPROVED",
            "the credit note is on fraud hold — a held document is "
            "evidence in a fraud review, not money to net, and the "
            "gate cannot clear it",
        )
    if credit.status != "APPROVED":
        return (
            "CREDIT_NOT_APPROVED",
            f"the credit note is {credit.status} — only an APPROVED "
            "credit can be applied; an unverified document moves no money",
        )
    if credit.extraction is not None \
            and float(credit.extraction.value("total") or 0.0) >= 0:
        return (
            "CREDIT_NOT_NEGATIVE",
            f"the credit note carries a total of "
            f"{float(credit.extraction.value('total') or 0.0):.2f} — a "
            "credit must carry a negative total; this document reduces "
            "nothing and is not applied as one",
        )
    number = credited_invoice_number(credit)
    if not number:
        return (
            "NO_INVOICE_REFERENCE",
            "the credit note names no invoice — a credit is applied "
            "only against the invoice its own document names, and is "
            "never matched to one by amount",
        )
    target, problem = _resolve_target(credit, records)
    if problem is not None:
        return problem
    assert target is not None
    if target.status == "FRAUD_REVIEW":
        flags = "; ".join(target.fraud_flags) or "bank details under review"
        return (
            "TARGET_FRAUD_HOLD",
            f"invoice {number} ({target.record_id}) is on fraud hold "
            f"(FRAUD_REVIEW: {flags}) — nothing is applied against a "
            "held record; the hold is cleared in the supplier master, "
            "never by netting",
        )
    if target.status != "APPROVED":
        return (
            "TARGET_NOT_APPROVED",
            f"invoice {number} ({target.record_id}) is {target.status} "
            "— credits apply against APPROVED invoices only",
        )
    if credit.extraction is not None \
            and credit.extraction.currency != target.extraction.currency:
        return (
            "CURRENCY_MISMATCH",
            f"the credit is in {credit.extraction.currency} but invoice "
            f"{number} is in {target.extraction.currency} — a credit "
            "reduces a payable in the payable's own currency, to the cent",
        )
    if target.payment_state == "IN_RUN":
        return (
            "TARGET_IN_RUN",
            f"invoice {number} is held by open payment run "
            f"{target.payment_run_id}, which was assembled on its open "
            f"amount — return the run, or let it release, before "
            "applying a credit against it",
        )
    # PART_PAID deliberately falls through: the invoice is not
    # fully paid, so its remainder is a live target and the
    # money check below judges the credit against that
    # remainder — the released part is never the credit's
    # business.
    if target.payment_state == "PAID_PROPOSED":
        return (
            "TARGET_PAID_PROPOSED",
            f"invoice {number} was released in payment run "
            f"{target.payment_run_id} and is PAID_PROPOSED — that "
            "money's story belongs to the run and its remittance "
            "confirmation, and the credit cannot rewrite it. The "
            "credit stays on file for the supplier's next cycle",
        )
    amount = credit_amount(credit)
    open_now = open_amount(target)
    if amount > open_now:
        less = []
        if target.credits_applied:
            less.append("credits already applied")
        if target.payments_applied:
            less.append("payments already released")
        return (
            "OVER_CREDIT",
            f"credit of {amount:.2f} exceeds the open amount of "
            f"invoice {number} ({open_now:.2f}"
            + (f", face {float(target.extraction.value('total') or 0.0):.2f}"
               f" less {' and '.join(less)}" if less
               else "")
            + ") — a credit never reduces a payable below zero, and "
            "it is never partially applied",
        )
    return None


def unapplied_reason(
    credit: DocumentRecord, records: list[DocumentRecord]
) -> str:
    """The reason an unapplied credit currently sits unapplied,
    in the words every surface shows. A credit that could be
    applied says so — its state is a worklist entry, not a
    failure."""
    blocker = application_blocker(credit, records)
    if blocker is None:
        return (
            "approved and applicable — awaiting a controller's "
            "application against the invoice it names"
        )
    return blocker[1]


def build_application(
    credit: DocumentRecord,
    target: DocumentRecord,
    actor: str,
    *,
    at: str = "",
) -> CreditApplication:
    """The ledger entry for applying ``credit`` to ``target``.
    Pure: the service checks the blocker, then records this on
    the invoice and flips the credit's state — the two writes
    are one act and this entry is what both sides agree on."""
    assert credit.extraction is not None and target.extraction is not None
    return CreditApplication(
        credit_record_id=credit.record_id,
        credit_number=str(credit.extraction.value("invoice_number") or ""),
        invoice_record_id=target.record_id,
        amount=credit_amount(credit),
        currency=credit.extraction.currency,
        actor=actor,
        at=at,
    )


def resolve_target(
    credit: DocumentRecord, records: list[DocumentRecord]
) -> DocumentRecord | None:
    """The invoice record a credit names (the service calls
    this once the blocker has cleared, so a target exists)."""
    target, _ = _resolve_target(credit, records)
    return target
