"""The supplier ledger: one supplier, the whole relationship.

Every earlier round followed the money along one axis — a
document through the pipeline, a statement against the records,
a batch to the bank and the bank's answer back, a credit against
its invoice, the whole position at close. The page an AP lead
actually opens during a supplier call is none of those: it is
the supplier's page. This module is that page, as a read model:

- **Identity is the master's, joined the intake's way.** A
  record belongs to a supplier because the pipeline joined it
  there — the extracted supplier name matched the master
  exactly, or the referenced purchase order's supplier resolved
  it (graph.py states the discipline). The ledger invents no
  second matching pass. A document that joined to nothing is
  not attached by resemblance and not dropped: it is listed on
  the index under UNMATCHED, with the name as the document
  wrote it, so a supplier trading under three spellings shows
  up as a data problem instead of a missing third of the story.
  Only the classes the intake resolves identity for can be
  unmatched — invoices, credit notes, statements. Attachments
  (remittance advices, delivery notes) are filed with their
  submission and resolve their payment run, not a supplier;
  listing them as unmatched suppliers would manufacture a
  problem the pipeline never claimed to solve.
- **Every figure traces to its record, computed once.** Face
  is the extraction total; credits are the invoice's own
  ``credits_applied`` ledger; open is ``credits.open_amount``
  — the one computation the packet, the runs, and the position
  already read. The ledger recomputes nothing in its own way.
- **The totals tie to the position.** The ledger is all-time —
  the whole relationship has no as-of — but its payable totals
  are taken in the position's own universe: every APPROVED
  invoice at its open amount. An open figure does not depend
  on the as-of (only its ageing does), so the ledger's total
  per currency must equal the position's supplier row at any
  as-of, to the cent — and the ledger carries the position's
  figure beside its own and names the difference when there
  is one. There can be one, honestly: a record whose applied
  credits exceed its face is corruption the credit path
  refuses to write, and the position keeps it out of its
  ageing as a named NEGATIVE_OPEN anomaly while the ledger's
  sum still counts it. That difference is reported, per
  currency, as POSITION_TIE_BREAK — the two views disagreeing
  is itself the finding, and neither view edits itself to hide
  it. Each invoice also carries its bucket from the position
  at the ledger's stated as-of — read out of the position
  itself, so the ageing on the ledger is the ageing at close.
- **Bank details come from the master, only.** The ledger
  shows the master's approved account and bank. A document
  that presented different details was held at intake with the
  guardrail's flag; that flag, in the guardrail's words, is
  the ledger's bank warning, with the records that raised it.
  No document's bank details are ever shown as the supplier's.
- **Open breaks are the unaccepted ones.** A statement break
  is open while its reconciliation has not been ACCEPTED:
  awaiting a controller, or flagged back — flagged back means
  the AP team is taking it to the supplier, which is the
  opposite of settled. The break lines ride verbatim from the
  reconciliation; the ledger classifies nothing itself.
- **A held duplicate shows with its evidence.** An invoice
  the paid-duplicate watch stopped carries its hold on the
  ledger line — the payment it answers to and, once ruled
  on, the resolution — and the index counts a supplier's
  standing holds on its row. The ledger reads the hold off
  the record; it computes nothing about duplicates itself.
"""

from __future__ import annotations

from datetime import date

from .credits import (
    credit_amount,
    credited_invoice_number,
    open_amount,
    unapplied_reason,
)
from .position import build_position
from .schemas import (
    DocumentRecord,
    LedgerBank,
    LedgerBreak,
    LedgerCredit,
    LedgerCurrencyTotals,
    LedgerDocument,
    LedgerInvoice,
    Reconciliation,
    Supplier,
    SupplierIndex,
    SupplierIndexEntry,
    SupplierLedger,
    UnmatchedDocument,
)

# The classes the intake resolves supplier identity for (see
# the module docstring): only these can fail to join, so only
# these can be unmatched.
IDENTITY_CLASSES = ("invoice", "credit_note", "statement")


def _cents(amount: float) -> float:
    return round(float(amount), 2)


def _document_number(record: DocumentRecord) -> str:
    if record.extraction is None:
        return ""
    return str(record.extraction.value("invoice_number") or "")


def _document(record: DocumentRecord) -> LedgerDocument:
    return LedgerDocument(
        record_id=record.record_id,
        submission_id=record.submission_id,
        doc_class=record.doc_class,
        status=record.status,
        document_number=_document_number(record),
        total=(
            _cents(float(record.extraction.value("total") or 0.0))
            if record.extraction is not None else None
        ),
        refusal_reason=record.refusal_reason,
        fraud_flags=list(record.fraud_flags),
        created_at=record.created_at,
    )


def _bank(supplier: Supplier, records: list[DocumentRecord]) -> LedgerBank:
    flags: list[str] = []
    flagged_ids: list[str] = []
    for record in records:
        if record.fraud_flags:
            flagged_ids.append(record.record_id)
            for flag in record.fraud_flags:
                if flag not in flags:
                    flags.append(flag)
    return LedgerBank(
        bank_name=supplier.bank_name,
        bank_account=supplier.bank_account,
        warning="; ".join(flags) if flags else None,
        flags=flags,
        flagged_record_ids=flagged_ids,
    )


def _open_breaks(
    supplier_id: str, recons: list[Reconciliation]
) -> list[LedgerBreak]:
    breaks: list[LedgerBreak] = []
    for recon in recons:
        if recon.supplier_id != supplier_id or recon.status == "ACCEPTED":
            continue
        for line in recon.lines:
            if line.classification == "matched":
                continue
            breaks.append(LedgerBreak(
                reconciliation_id=recon.reconciliation_id,
                statement_record_id=recon.statement_record_id,
                period=recon.period,
                recon_status=recon.status,
                invoice_number=line.invoice_number,
                classification=line.classification,
                statement_amount=line.statement_amount,
                ap_amount=line.ap_amount,
                currency=line.currency,
                note=line.note,
            ))
    return breaks


def build_ledger(
    supplier: Supplier,
    records: list[DocumentRecord],
    recons: list[Reconciliation],
    as_of: str,
) -> SupplierLedger:
    """Supplier ``supplier``'s ledger over the whole store
    snapshot. Pure: reads records and reconciliations, writes
    nothing. ``as_of`` ages the position the totals tie to."""
    own = [r for r in records if r.supplier_id == supplier.supplier_id]
    position = build_position(records, as_of)
    aged = {
        line.record_id: line
        for row in position.suppliers for line in row.lines
    }
    fully_credited_ids = {f.record_id for f in position.fully_credited}
    position_rows = {
        row.currency: row for row in position.suppliers
        if row.supplier_id == supplier.supplier_id
    }

    invoices: list[LedgerInvoice] = []
    totals: dict[str, LedgerCurrencyTotals] = {}
    for record in own:
        if record.doc_class != "invoice" or record.extraction is None:
            continue
        currency = record.extraction.currency
        face = _cents(float(record.extraction.value("total") or 0.0))
        credits_total = _cents(
            sum(entry.amount for entry in record.credits_applied))
        open_now = open_amount(record)
        line = aged.get(record.record_id)
        invoices.append(LedgerInvoice(
            record_id=record.record_id,
            invoice_number=_document_number(record),
            status=record.status,
            currency=currency,
            face=face,
            credits_applied=list(record.credits_applied),
            credits_total=credits_total,
            open=open_now,
            match_status=record.match.status if record.match else None,
            exception_codes=(
                list(record.match.exception_codes) if record.match else []
            ),
            payment_state=record.payment_state,
            payment_run_id=record.payment_run_id,
            payment_confirmation=record.payment_confirmation,
            payment_confirmation_id=record.payment_confirmation_id,
            duplicate_hold=record.duplicate_hold,
            due_date=str(record.extraction.value("due_date") or ""),
            bucket=line.bucket if line else None,
            fully_credited=record.record_id in fully_credited_ids,
        ))
        if record.status == "APPROVED":
            bucket_totals = totals.setdefault(
                currency, LedgerCurrencyTotals(currency=currency))
            bucket_totals.approved_invoices += 1
            bucket_totals.face_total = _cents(
                bucket_totals.face_total + face)
            bucket_totals.credits_total = _cents(
                bucket_totals.credits_total + credits_total)
            bucket_totals.open_total = _cents(
                bucket_totals.open_total + open_now)

    for currency, entry in totals.items():
        row = position_rows.get(currency)
        entry.position_open_total = row.open_total if row else 0.0
        entry.difference = _cents(
            entry.open_total - entry.position_open_total)
        entry.tied = entry.difference == 0

    credits: list[LedgerCredit] = []
    for record in own:
        if record.doc_class != "credit_note":
            continue
        credits.append(LedgerCredit(
            record_id=record.record_id,
            credit_number=_document_number(record),
            status=record.status,
            currency=(
                record.extraction.currency if record.extraction else "USD"
            ),
            amount=credit_amount(record),
            credit_state=record.credit_state,
            credited_invoice_number=credited_invoice_number(record),
            applied_invoice_record_id=record.applied_invoice_record_id,
            unapplied_reason=(
                None if record.credit_state == "APPLIED"
                else unapplied_reason(record, records)
            ),
        ))

    totals_list = [totals[c] for c in sorted(totals)]
    return SupplierLedger(
        supplier_id=supplier.supplier_id,
        supplier_name=supplier.name,
        master_currency=supplier.currency,
        as_of=position.as_of,
        bank=_bank(supplier, own),
        documents=[_document(r) for r in own],
        invoices=invoices,
        credits=credits,
        open_breaks=_open_breaks(supplier.supplier_id, recons),
        totals=totals_list,
        tie_status=(
            "TIED" if all(t.tied for t in totals_list)
            else "POSITION_TIE_BREAK"
        ),
    )


def unmatched_documents(
    records: list[DocumentRecord],
) -> list[UnmatchedDocument]:
    """The documents that joined to no supplier: identity-class
    records whose ``supplier_id`` the intake left unset. The
    name rides as the document wrote it — resemblance to a
    master name is the reader's observation to make, never
    the ledger's join to perform."""
    unmatched: list[UnmatchedDocument] = []
    for record in records:
        if record.supplier_id is not None:
            continue
        if record.doc_class not in IDENTITY_CLASSES:
            continue
        written = (
            record.extraction.value("supplier_name")
            if record.extraction is not None else None
        )
        unmatched.append(UnmatchedDocument(
            record_id=record.record_id,
            submission_id=record.submission_id,
            doc_class=record.doc_class,
            status=record.status,
            name_as_written=str(written) if written else "",
            document_number=_document_number(record),
            total=(
                _cents(float(record.extraction.value("total") or 0.0))
                if record.extraction is not None else None
            ),
            refusal_reason=record.refusal_reason,
        ))
    return unmatched


def build_index(
    suppliers: list[Supplier],
    records: list[DocumentRecord],
    recons: list[Reconciliation],
    as_of: str,
) -> SupplierIndex:
    """The ledger index: every master supplier with its counts,
    and the unmatched section. Pure, like the ledger itself."""
    entries: list[SupplierIndexEntry] = []
    for supplier in sorted(suppliers, key=lambda s: s.name):
        ledger = build_ledger(supplier, records, recons, as_of)
        counts: dict[str, int] = {}
        for doc in ledger.documents:
            counts[doc.doc_class] = counts.get(doc.doc_class, 0) + 1
        entries.append(SupplierIndexEntry(
            supplier_id=supplier.supplier_id,
            supplier_name=supplier.name,
            currency=supplier.currency,
            document_counts=counts,
            open_totals_by_currency={
                t.currency: t.open_total for t in ledger.totals
            },
            open_break_count=len(ledger.open_breaks),
            bank_warning=ledger.bank.warning is not None,
            duplicate_hold_count=sum(
                1 for doc in ledger.documents
                if doc.status == "PAID_DUPLICATE_HOLD"
            ),
        ))
    unmatched = unmatched_documents(records)
    return SupplierIndex(
        as_of=date.fromisoformat(str(as_of).strip()[:10]).isoformat(),
        suppliers=entries,
        unmatched_count=len(unmatched),
        unmatched=unmatched,
    )
