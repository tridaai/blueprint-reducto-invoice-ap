"""The service facade: submissions, the gate, the payment packet.

The API, CLI, demo, and evals all drive the same service, so the
behaviour a test pins is the behaviour the console shows. The
service owns the pipeline's threads (one per submission) and the
gate discipline:

- a decision names its approver — anonymous approvals don't exist;
- fraud-held and exception records cannot be decided at the gate
  (fraud is resolved in the supplier master, out of band;
  exceptions go back to the sender with their codes);
- the payment packet is a read model over APPROVED records — a
  proposal, computed fresh, that executes nothing;
- statement reconciliation lives here too: computed over
  records, decided by a named controller, and strictly
  read-only over the documents it reconciles;
- payment runs assemble approved records into a stored batch
  for one value date — one open run per record, every refusal
  named — and stop at a treasurer's gate. Release generates
  the bank-instruction export once and locks the batch;
  return frees every member. Neither pays anything.
- remittance confirmation closes the loop: a bank advice is
  reconciled against the released run it names, line by line,
  in code — exactly, never within a tolerance — and stops at
  a controller's gate. An advice that names no run on file is
  held UNMATCHED, never guessed onto one. Held advices land
  on the worklist (worklist.py), where the arithmetic suggests
  what each might answer — and only a named controller's
  link, or their not-ours mark, takes one off the pile.
- the supplier ledger gathers the whole relationship per
  supplier — documents, invoices with their credit ledgers,
  open statement breaks, the master's bank details and any
  bank-change warning — as a read model whose payable totals
  tie to the AP position; documents that joined to no supplier
  are listed as unmatched, never attached by resemblance;
- credit application sits between the gate and the packet:
  an approved credit note is applied by a named controller
  against the invoice its own document names, in code — to
  the cent, never partially, never by amount-matching — and
  from that moment the invoice's OPEN amount (face minus
  applied credits) is what the packet proposes and the runs
  pay. The approved face never changes; a credit against an
  invoice a run already holds or has released is refused
  with the state named, and stays on file for the next cycle.
- the paid-duplicate watch stands at intake: an invoice that
  answers to one already released for payment — same supplier,
  same number, the paid face or the net figure the run
  instructed — is held on the paid evidence, the gate cannot
  decide it, and only a named controller resolves the hold,
  with a reason, one of two ways (duplicates.py). A record
  released from a hold becomes decidable again even though
  its submission's gate closed long before; ``decide``
  states how that decision lands.
- PO coverage accumulates what the match cannot see: the
  approved billing against each order, read against the
  register's total (coverage.py). It is a read model beside
  the position and the ledger — and one deliberate exception
  to pure reading: when a gate decision lands on an invoice
  whose order stands EXCEEDED counting that invoice, the
  coverage fact is written onto the record's audit trail.
  Coverage never blocks the gate; the record simply shows,
  permanently, that the approver decided with the fact in
  front of them.
- the supplier master's bank details are versioned
  (bankmaster.py): a legitimate bank change is recorded here,
  by a named actor, with the provenance it was verified from
  and the date it takes effect — append-only, so the master
  can say what it held on any past date. The fraud screen
  judges each invoice against the version in force on the
  invoice's own date; a decided record's stamped flags are
  history and are never re-judged when a new version lands.
- unknown suppliers stop at the door (enrolment.py): a
  payable document whose supplier resolves to no master
  record is held in NEW_SUPPLIER_REVIEW — unmatched,
  unscreened, undecidable, unpaid — until a named controller
  enrols the supplier or refuses the document. Enrolment is
  a bank-details recording and inherits the master's
  governance whole: actor, details, and the source they were
  verified from, or it does not happen; it creates exactly
  one master record and releases every held record of the
  identity back into the pipeline's own adjudication.
"""

from __future__ import annotations

import base64
import logging
from datetime import date, datetime, timezone

from langgraph.types import Command

from .bankmaster import build_history, version_at
from .confirmations import (
    OVERLAY_BY_CLASS,
    reconcile_confirmation,
    reconcile_linked_confirmation,
    resolve_run_reference,
    unmatched_confirmation,
)
from .credits import (
    application_blocker,
    build_application,
    credit_amount,
    credited_invoice_number,
    open_amount,
    paid_amount,
    resolve_target,
    unapplied_reason,
)
from .duplicates import evidence_summary, watch_entry
from .enrolment import build_review_queue, hold_summary
from .errors import (
    ConfirmationError,
    CoverageError,
    CreditError,
    GateError,
    MasterError,
    PaymentRunError,
    ReconciliationError,
    RecordNotFound,
)
from .coverage import build_coverage, po_reference_of
from .coverage import record_coverage as _coverage_fact
from .graph import ApPipeline, apply_gate_decision
from .guardrails import normalise_account
from .ledger import build_index, build_ledger
from .payments import (
    PAYABLE_CLASSES,
    assemble_run,
    build_export_csv,
    export_digest,
    export_key_for,
)
from .position import build_position, export_key_for_position, position_csv
from .reconciliation import reconcile_statement
from .schemas import (
    AdviceWorklist,
    BankHistory,
    BankVersion,
    ControllerDecision,
    CoverageReport,
    DocumentRecord,
    DuplicateHoldResolution,
    Enrolment,
    NewSupplierReview,
    NotOursMark,
    PaymentConfirmation,
    PaymentEntry,
    PaymentLine,
    PaymentPacket,
    PaymentRun,
    PositionReport,
    RecordCoverage,
    Reconciliation,
    Supplier,
    SupplierHoldResolution,
    SupplierIndex,
    SupplierLedger,
    TreasurerDecision,
    UnappliedCredit,
)
from .worklist import build_worklist

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ApService:
    def __init__(self, pipeline: ApPipeline, store) -> None:
        self.pipeline = pipeline
        self.store = store
        self._pending: dict[str, dict[str, dict]] = {}  # submission → record → decision

    # -- submissions -------------------------------------------------

    def submit(self, submission_id: str, filename: str, data: bytes,
               content_type: str = "application/octet-stream") -> list[DocumentRecord]:
        config = {"configurable": {"thread_id": submission_id}}
        state = {
            "submission_id": submission_id,
            "filename": filename,
            "content_type": content_type,
            "data_b64": base64.b64encode(data).decode(),
        }
        self.pipeline.graph.invoke(state, config=config)
        return [
            rec for rec in self.store.list() if rec.submission_id == submission_id
        ]

    def list_records(self, status: str | None = None) -> list[DocumentRecord]:
        return self.store.list(status=status)

    def get_record(self, record_id: str) -> DocumentRecord:
        record = self.store.get(record_id)
        if record is None:
            raise RecordNotFound(f"no record {record_id}")
        return record

    # -- the gate ------------------------------------------------------

    def decide(self, record_id: str, decision: str, actor: str, reason: str = "") -> DocumentRecord:
        if decision not in ("approve", "reject"):
            raise GateError(f"decision must be 'approve' or 'reject', got {decision!r}")
        if not actor or not actor.strip():
            raise GateError("a gate decision must name its approver")
        record = self.get_record(record_id)
        if record.status == "FRAUD_REVIEW":
            raise GateError(
                "record is on fraud hold: bank details are verified out of band "
                "and corrected in the supplier master — the gate cannot clear it"
            )
        if record.status == "PAID_DUPLICATE_HOLD":
            hold = record.duplicate_hold
            evidence = (
                evidence_summary(hold) if hold is not None
                else "the record answers to an invoice already "
                     "released for payment"
            )
            raise GateError(
                f"record is held by the paid-duplicate watch: {evidence} "
                "— the gate cannot decide a held duplicate; a "
                "controller resolves the hold on the record "
                "(confirm it, or release it as not a duplicate) "
                "with a reason"
            )
        if record.status == "NEW_SUPPLIER_REVIEW":
            hold = record.supplier_hold
            evidence = (
                hold_summary(hold) if hold is not None
                else "the record's supplier is not on the supplier master"
            )
            raise GateError(
                f"record is held at the door for a new supplier: "
                f"{evidence} — the gate cannot decide it; a "
                "controller enrols the supplier (which releases "
                "the record into the pipeline) or refuses the "
                "document as not a known supplier"
            )
        if record.status != "AWAITING_APPROVER":
            raise GateError(
                f"record {record_id} is {record.status}; only AWAITING_APPROVER records can be decided"
            )
        pending = self._pending.setdefault(record.submission_id, {})
        pending[record_id] = {"decision": decision, "actor": actor, "reason": reason}

        awaiting = [
            rec.record_id for rec in self.store.list()
            if rec.submission_id == record.submission_id and rec.status == "AWAITING_APPROVER"
        ]
        decided_before = {
            rec.record_id
            for rec in self.store.list()
            if rec.submission_id == record.submission_id
            and rec.decision is not None
        }
        decisions: dict[str, dict] = {}
        if all(rid in pending for rid in awaiting):
            decisions = dict(pending)
            config = {"configurable": {"thread_id": record.submission_id}}
            self.pipeline.graph.invoke(Command(resume=decisions), config=config)
            self._pending.pop(record.submission_id, None)
            # A record can become AWAITING_APPROVER after its
            # submission's gate already closed — a paid-duplicate
            # hold a controller released, weeks after the
            # submission ran. That thread has no interrupt left
            # to resume, so the resume above is a no-op for it
            # and the decision would silently never land. Any
            # record still awaiting with a decision recorded
            # against it gets that decision applied directly,
            # in the same write the graph's apply step makes.
            for rid, raw in decisions.items():
                rec = self.store.get(rid)
                if rec is not None and rec.status == "AWAITING_APPROVER" \
                        and rec.decision is None:
                    apply_gate_decision(rec, raw, at=_now())
                    self.store.save(rec)
        # Decisions this call landed (by either path) on
        # invoices whose order stands EXCEEDED counting them
        # carry the coverage fact onto the record's audit
        # trail — the approver's context, made permanent.
        for rid in decisions:
            if rid in decided_before:
                continue
            rec = self.store.get(rid)
            if rec is not None and rec.decision is not None:
                self._note_coverage_at_decision(rec)
        return self.get_record(record_id)

    def _note_coverage_at_decision(self, record: DocumentRecord) -> None:
        """Write the PO coverage fact onto a just-decided
        invoice's audit trail, when its order stands EXCEEDED
        counting this invoice. Coverage never blocks the gate
        (coverage.py states why — an exceeded order is a fact
        for a human, not a fraud signal); what this write does
        is keep the fact from evaporating with the screen the
        approver read: the record shows the decision was taken
        with the order's over-billing in front of them, figures
        quoted in the fact's own sentence."""
        if record.doc_class != "invoice" or record.extraction is None:
            return
        if not po_reference_of(record):
            return
        fact = _coverage_fact(
            record, self.store.list(),
            self.store.reference.purchase_orders,
        )
        if fact.state != "EXCEEDED":
            return
        record.audit.append({
            "event": "po_coverage_at_decision", "at": _now(),
            "detail": fact.fact,
        })
        record.updated_at = _now()
        self.store.save(record)

    # -- the paid-duplicate watch --------------------------------

    def list_duplicate_watch(self, as_of: str) -> list[dict]:
        """The watch list: every record currently held by the
        paid-duplicate watch, with the paid evidence on its
        face and its hold's age at ``as_of`` (an ISO date —
        explicit, like the position's, so an age can be
        reproduced at audit). A read over the store: building
        it writes nothing, and a resolved hold leaves the list
        (its evidence stays on the record and the ledger)."""
        return [
            watch_entry(record, as_of)
            for record in self.store.list(status="PAID_DUPLICATE_HOLD")
        ]

    def resolve_duplicate_hold(
        self, record_id: str, action: str, actor: str, reason: str = ""
    ) -> DocumentRecord:
        """A named controller's word on a paid-duplicate hold.

        Two exits, both recorded on the record forever.
        CONFIRMED_DUPLICATE: the arrival is the paid invoice
        again — the record is refused permanently, with the
        evidence and the resolution as its refusal reason;
        nothing is deleted, and the original payment is not
        touched (there is no clawback here — recovery, if any,
        is a conversation with the supplier, not a state this
        system writes). RELEASED_NOT_DUPLICATE: the hold lifts
        and the record returns to the state its own match
        earned — AWAITING_APPROVER, decidable at the gate like
        any arrival, or EXCEPTION with its own codes standing.
        The release adjudicates this document only: a second
        arrival of the same document is a new record and is
        held afresh against the same paid history."""
        if action not in ("CONFIRMED_DUPLICATE", "RELEASED_NOT_DUPLICATE"):
            raise GateError(
                f"hold resolution must be 'CONFIRMED_DUPLICATE' or "
                f"'RELEASED_NOT_DUPLICATE', got {action!r}"
            )
        if not actor or not actor.strip():
            raise GateError("a hold resolution must name its controller")
        if not reason or not reason.strip():
            raise GateError(
                "resolving a paid-duplicate hold requires a reason — "
                "it is the evidence the next arrival of this invoice "
                "will be judged against"
            )
        record = self.get_record(record_id)
        if record.duplicate_hold is None:
            raise GateError(
                f"record {record_id} carries no paid-duplicate hold "
                "to resolve"
            )
        if record.status != "PAID_DUPLICATE_HOLD":
            prior = record.duplicate_hold.resolution
            prior_text = (
                f" — it was resolved {prior.action} by {prior.actor} "
                f"at {prior.at}"
                if prior is not None else ""
            )
            raise GateError(
                f"record {record_id} is {record.status}; only a "
                f"PAID_DUPLICATE_HOLD can be resolved{prior_text}"
            )
        hold = record.duplicate_hold
        hold.resolution = DuplicateHoldResolution(
            action=action, actor=actor, reason=reason, at=_now(),  # type: ignore[arg-type]
        )
        evidence = evidence_summary(hold)
        if action == "CONFIRMED_DUPLICATE":
            record.status = "REFUSED"
            record.refusal_reason = (
                f"Confirmed duplicate: {evidence}. This arrival "
                f"({record.record_id}) will never be paid. Confirmed "
                f"by {actor}: {reason}"
            )
            record.audit.append({
                "event": "duplicate_confirmed", "at": _now(),
                "detail": f"confirmed duplicate by {actor} — {reason}",
            })
        else:
            record.status = (
                "EXCEPTION"
                if record.match is not None
                and record.match.status == "EXCEPTION"
                else "AWAITING_APPROVER"
            )
            record.audit.append({
                "event": "duplicate_hold_released", "at": _now(),
                "detail": (
                    f"released as not a duplicate by {actor} — "
                    f"{reason}; the record returns to "
                    f"{record.status}, the state its own match earned"
                ),
            })
        record.updated_at = _now()
        return self.store.save(record)

    # -- the new-supplier door -----------------------------------

    def new_supplier_review(self) -> NewSupplierReview:
        """The review queue: every record currently held at the
        door, grouped by the identity it stated (enrolment.py).
        A read over the store: building it writes nothing, and
        a group leaves the queue when its last held record is
        enrolled into the pipeline or refused."""
        return build_review_queue(self.store.list())

    def enrol_new_supplier(
        self,
        record_id: str,
        actor: str,
        bank_account: str,
        bank_name: str = "",
        source: str = "",
        supplier_id: str | None = None,
    ) -> Enrolment:
        """Enrol the supplier one held record arrived from:
        create its master record and release every held record
        of that identity back into the pipeline.

        Enrolment IS a bank-details recording — the new payee's
        details enter the master by this act and no other — so
        it carries the bankmaster governance whole (the
        refusals mirror ``record_bank_version``'s, by name): a
        named controller, the details themselves, and the
        source they were verified FROM — the onboarding pack,
        the verified callback, the evidence a human checked.
        An enrolment without any of the three is refused: an
        unattributed first payee is exactly the fraud pattern,
        wearing a master's clothes.

        The identity is the documents' own: the name as the
        held records wrote it, and the currency of the record
        the controller acted on. The id is the next free
        ``SUP-<n>`` unless the controller states one — the
        buyer's ERP often assigned the vendor number when it
        raised the supplier's first purchase order, before AP
        enrolled the payee, and the orders on file name that
        id; a stated id that is already on the master is
        refused, because enrolment creates exactly one record
        and never merges into a standing one.

        The act is whole or nothing: every check runs before
        the master is written, and only then are the records
        released — each through the pipeline's own adjudication
        under the enrolled supplier, so a released record is
        screened, watched, and matched exactly as it would
        have been had the master carried its supplier when it
        arrived. Enrolment adjudicates nobody's document
        clean: a released invoice whose stated details differ
        from the enrolled ones lands on FRAUD_REVIEW by the
        ordinary rule. The hold's resolution rides every
        released record permanently."""
        if not actor or not actor.strip():
            raise MasterError(
                "enrolling a supplier must name the controller "
                "who enrols it — a new payee with no name on its "
                "enrolment is not a master record, it is a rumour"
            )
        if not source or not source.strip():
            raise MasterError(
                "enrolling a supplier must state what its bank "
                "details were verified from — enrolment is a "
                "bank-details recording, the details are verified "
                "out of band, and the source is the record of "
                "that verification (the onboarding pack, the "
                "verified callback); without it the enrolment "
                "is unprovenanced and refused"
            )
        if not bank_account or not bank_account.strip():
            raise MasterError(
                "enrolling a supplier must state the bank "
                "account — the master never holds an empty "
                "account, and a first payee least of all"
            )
        record = self.get_record(record_id)
        if record.status != "NEW_SUPPLIER_REVIEW" \
                or record.supplier_hold is None:
            prior = (
                record.supplier_hold.resolution
                if record.supplier_hold is not None else None
            )
            prior_text = (
                f" — its hold was resolved {prior.action} by "
                f"{prior.actor} at {prior.at}"
                if prior is not None else ""
            )
            raise GateError(
                f"record {record_id} is {record.status}; only a "
                f"NEW_SUPPLIER_REVIEW record's supplier can be "
                f"enrolled{prior_text}"
            )
        hold = record.supplier_hold
        name = hold.name_as_written.strip()
        if not name:
            raise MasterError(
                f"record {record_id} names no supplier — there "
                "is no identity to enrol; refuse the document, "
                "or have the supplier resubmit naming itself"
            )
        if self.store.reference.supplier_named(name) is not None:
            raise MasterError(
                f"a supplier named {name!r} already stands on "
                "the master — enrolment creates a new record; "
                "it never merges a held identity into a standing "
                "one"
            )
        if supplier_id is not None:
            supplier_id = supplier_id.strip()
            if not supplier_id:
                raise MasterError(
                    "a stated supplier id must not be blank — "
                    "omit it and the master assigns the next id"
                )
            if supplier_id in self.store.reference.suppliers:
                raise MasterError(
                    f"supplier id {supplier_id} already stands "
                    "on the master — enrolment creates exactly "
                    "one new record and never reuses a standing "
                    "supplier's id"
                )
        else:
            supplier_id = self.store.reference.next_supplier_id()
        group = [
            rec for rec in self.store.list(status="NEW_SUPPLIER_REVIEW")
            if rec.supplier_hold is not None
            and rec.supplier_hold.normalised_name
            == hold.normalised_name
        ]
        supplier = Supplier(
            supplier_id=supplier_id,
            name=name,
            bank_account=bank_account.strip(),
            bank_name=(bank_name or "").strip(),
            currency=(
                record.extraction.currency
                if record.extraction is not None else "USD"
            ),
        )
        version = BankVersion(
            version=1,
            bank_account=supplier.bank_account,
            bank_name=supplier.bank_name,
            effective_from="",
            recorded_by=actor.strip(),
            recorded_at=_now(),
            source=source.strip(),
        )
        self.store.reference.enrol_supplier(supplier, version)
        released: list[str] = []
        for member in group:
            member_hold = member.supplier_hold
            assert member_hold is not None
            member_hold.resolution = SupplierHoldResolution(
                action="ENROLLED",
                actor=actor.strip(),
                detail=source.strip(),
                supplier_id=supplier_id,
                at=_now(),
            )
            member.audit.append({
                "event": "supplier_enrolled", "at": _now(),
                "detail": (
                    f"supplier enrolled as {supplier_id} "
                    f"({supplier.name}) by {actor.strip()} — bank "
                    f"details recorded from: {source.strip()}; "
                    "this record is released back into the "
                    "pipeline and judged on its own contents"
                ),
            })
            self.pipeline.readjudicate(member, supplier)
            released.append(member.record_id)
        return Enrolment(
            supplier=supplier, bank_version=version,
            released_record_ids=released,
        )

    def refuse_new_supplier(
        self, record_id: str, actor: str, reason: str = ""
    ) -> DocumentRecord:
        """Refuse one held document as not a known supplier.

        The door's other exit, and deliberately per record:
        enrolment answers for an identity, but refusal answers
        for THIS document — a supplier's genuine first invoice
        is not condemned because a second document under its
        name was refused, and its other held records stay held
        for their own decision. The refusal is terminal and
        reasoned (a named controller, a stated reason — the
        duplicate-hold discipline), the record is retained
        read-only, and the resolution rides it permanently
        beside the hold that stopped it. Nothing is enrolled
        by a refusal: no master record, no bank version."""
        if not actor or not actor.strip():
            raise GateError(
                "refusing a held document must name its controller"
            )
        if not reason or not reason.strip():
            raise GateError(
                "refusing a held document requires a reason — "
                "it is the answer the sender gets, and the "
                "evidence the next arrival under this name "
                "will be read against"
            )
        record = self.get_record(record_id)
        if record.status != "NEW_SUPPLIER_REVIEW" \
                or record.supplier_hold is None:
            prior = (
                record.supplier_hold.resolution
                if record.supplier_hold is not None else None
            )
            prior_text = (
                f" — its hold was resolved {prior.action} by "
                f"{prior.actor} at {prior.at}"
                if prior is not None else ""
            )
            raise GateError(
                f"record {record_id} is {record.status}; only a "
                f"NEW_SUPPLIER_REVIEW record can be refused at "
                f"the door{prior_text}"
            )
        hold = record.supplier_hold
        hold.resolution = SupplierHoldResolution(
            action="REFUSED", actor=actor, detail=reason, at=_now(),
        )
        record.status = "REFUSED"
        record.refusal_reason = (
            f"NOT_A_KNOWN_SUPPLIER: supplier "
            f"{hold.name_as_written!r} is not on the supplier "
            f"master and enrolment was refused by {actor}: "
            f"{reason}"
        )
        record.audit.append({
            "event": "new_supplier_refused", "at": _now(),
            "detail": (
                f"refused as not a known supplier by {actor} — "
                f"{reason}"
            ),
        })
        record.updated_at = _now()
        return self.store.save(record)

    # -- statement reconciliation ------------------------------------

    def reconcile(self, statement_record_id: str) -> Reconciliation:
        """Reconcile one captured statement against AP's records.

        Read-only over document records: this writes exactly one
        new reconciliation record and touches nothing else — the
        packet cannot tell it happened (a test pins that)."""
        record = self.get_record(statement_record_id)
        if record.doc_class != "statement":
            raise ReconciliationError(
                f"record {statement_record_id} is a {record.doc_class}, not a "
                "statement — only statements reconcile"
            )
        if record.extraction is None:
            raise ReconciliationError(
                f"statement {statement_record_id} carries no captured "
                "extraction; its lines were never read"
            )
        if record.supplier_id is None:
            raise ReconciliationError(
                f"statement {statement_record_id} names a supplier that is "
                "not on file; there is nothing to reconcile it against"
            )
        existing = [
            r for r in self.store.list_reconciliations()
            if r.statement_record_id == statement_record_id
        ]
        reconciliation_id = f"RECON-{statement_record_id}"
        if existing:
            reconciliation_id = f"{reconciliation_id}-{len(existing) + 1}"
        ap_records = [
            r for r in self.store.list() if r.supplier_id == record.supplier_id
        ]
        recon = reconcile_statement(
            reconciliation_id, record, ap_records,
            policy=self.pipeline.policy, created_at=_now(),
        )
        return self.store.save_reconciliation(recon)

    def get_reconciliation(self, reconciliation_id: str) -> Reconciliation:
        recon = self.store.get_reconciliation(reconciliation_id)
        if recon is None:
            raise RecordNotFound(f"no reconciliation {reconciliation_id}")
        return recon

    def list_reconciliations(
        self, supplier_id: str | None = None
    ) -> list[Reconciliation]:
        return self.store.list_reconciliations(supplier_id=supplier_id)

    def decide_reconciliation(
        self, reconciliation_id: str, action: str, actor: str, note: str = ""
    ) -> Reconciliation:
        """The controller's word: accept the reconciliation, or
        flag it back with a note the AP team acts on. Decisions
        are final — a corrected statement is a new submission and
        reconciles afresh — and every decision joins the audit
        trail; nothing is ever edited in place."""
        if action not in ("accept", "flag_back"):
            raise GateError(
                f"controller action must be 'accept' or 'flag_back', got {action!r}"
            )
        if not actor or not actor.strip():
            raise GateError("a controller decision must name its controller")
        if action == "flag_back" and not (note and note.strip()):
            raise GateError(
                "flagging a reconciliation back requires a note — the AP "
                "team takes it to the supplier"
            )
        recon = self.get_reconciliation(reconciliation_id)
        if recon.status != "AWAITING_CONTROLLER":
            raise GateError(
                f"reconciliation {reconciliation_id} is {recon.status}; only "
                "AWAITING_CONTROLLER reconciliations can be decided"
            )
        recon.decision = ControllerDecision(
            decision=action, actor=actor, note=note, at=_now()  # type: ignore[arg-type]
        )
        recon.status = "ACCEPTED" if action == "accept" else "FLAGGED_BACK"
        recon.audit.append({
            "event": "controller_decision", "at": _now(),
            "detail": f"{action} by {actor}" + (f" — {note}" if note else ""),
        })
        recon.updated_at = _now()
        return self.store.save_reconciliation(recon)

    # -- credit application ------------------------------------------

    def apply_credit(self, credit_record_id: str, actor: str) -> DocumentRecord:
        """A controller applies one approved credit note against
        the invoice its own document names.

        The blocker discipline lives in credits.py — one
        computation, so this refusal, the packet's unapplied
        list, and the API all speak the same words. A blocked
        application changes nothing and raises with the
        blocker's code in the message; a clear one writes the
        ledger entry on the invoice and flips the credit to
        APPLIED in the same act, both sides audited. The
        invoice's decision and approved total are never touched
        — application moves the derived open amount, not the
        history."""
        if not actor or not actor.strip():
            raise GateError("a credit application must name its controller")
        credit = self.get_record(credit_record_id)
        if credit.doc_class != "credit_note":
            raise CreditError(
                f"record {credit_record_id} is a {credit.doc_class}, not a "
                "credit note — only credit notes are applied"
            )
        if credit.extraction is None:
            raise CreditError(
                f"credit note {credit_record_id} carries no captured "
                "extraction; the invoice it names was never read"
            )
        records = self.store.list()
        blocker = application_blocker(credit, records)
        if blocker is not None:
            code, reason = blocker
            raise GateError(f"{code}: {reason}")
        target = resolve_target(credit, records)
        assert target is not None  # the blocker cleared, so a target exists
        entry = build_application(credit, target, actor, at=_now())
        target.credits_applied.append(entry)
        target.audit.append({
            "event": "credit_applied", "at": _now(),
            "detail": (
                f"credit {entry.credit_number} ({credit.record_id}) "
                f"applied by {actor}: {entry.amount:.2f} "
                f"{entry.currency} — open amount now "
                f"{open_amount(target):.2f}"
            ),
        })
        target.updated_at = _now()
        self.store.save(target)
        credit.credit_state = "APPLIED"
        credit.applied_invoice_record_id = target.record_id
        credit.audit.append({
            "event": "credit_applied", "at": _now(),
            "detail": (
                f"applied by {actor} against invoice "
                f"{credited_invoice_number(credit)} ({target.record_id}) "
                f"for {entry.amount:.2f} {entry.currency}"
            ),
        })
        credit.updated_at = _now()
        return self.store.save(credit)

    def list_credits(self) -> list[dict]:
        """Every credit note on file, enriched with the figures
        the surfaces show: its size, the invoice its document
        names, and — while unapplied — the reason it sits
        unapplied, computed by the same blocker the application
        path enforces."""
        records = self.store.list()
        views: list[dict] = []
        for record in records:
            if record.doc_class != "credit_note":
                continue
            view = record.model_dump()
            view["amount"] = credit_amount(record)
            view["credited_invoice_number"] = credited_invoice_number(record)
            view["unapplied_reason"] = (
                None if record.credit_state == "APPLIED"
                else unapplied_reason(record, records)
            )
            views.append(view)
        return views

    # -- the packet ------------------------------------------------------

    def payment_packet(self) -> PaymentPacket:
        """The AP position, as a payment proposal: every approved
        invoice at its OPEN amount (credits listed on the line),
        plus the approved credits still unapplied, with the
        reason each one waits. Credit notes are never lines —
        their money already stands inside the invoice lines
        they reduced, or in the unapplied list when it doesn't
        yet."""
        records = self.store.list()
        lines: list[PaymentLine] = []
        totals: dict[str, float] = {}
        for record in records:
            if record.status != "APPROVED" or record.doc_class != "invoice":
                continue
            if record.extraction is None:
                continue
            amount = open_amount(record)
            currency = record.extraction.currency
            lines.append(PaymentLine(
                record_id=record.record_id,
                supplier_name=record.supplier_name,
                invoice_number=str(record.extraction.value("invoice_number") or ""),
                currency=currency,
                amount=amount,
                gross_amount=round(
                    float(record.extraction.value("total") or 0.0), 2
                ),
                credits_applied=list(record.credits_applied),
                payments_applied=list(record.payments_applied),
            ))
            totals[currency] = round(totals.get(currency, 0.0) + amount, 2)
        unapplied: list[UnappliedCredit] = []
        for record in records:
            if record.doc_class != "credit_note" \
                    or record.status != "APPROVED" \
                    or record.credit_state == "APPLIED" \
                    or record.extraction is None:
                continue
            unapplied.append(UnappliedCredit(
                record_id=record.record_id,
                credit_number=str(
                    record.extraction.value("invoice_number") or ""
                ),
                supplier_name=record.supplier_name,
                amount=credit_amount(record),
                currency=record.extraction.currency,
                invoice_number=credited_invoice_number(record),
                reason=unapplied_reason(record, records),
            ))
        return PaymentPacket(
            packet_id=f"PKT-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
            lines=lines,
            unapplied_credits=unapplied,
            totals_by_currency=totals,
            generated_at=_now(),
        )

    # -- the position ----------------------------------------------------

    def ap_position(self, as_of: str) -> PositionReport:
        """The AP position at close, aged as of ``as_of`` (an ISO
        date): every payable by supplier, bucketed by days past
        its due date, split by the state of its money, and tied
        to the packet's open total in the same pass (position.py
        states the construction). A read over the store —
        building it writes nothing and moves no money."""
        return build_position(self.store.list(), as_of)

    def position_export(self, as_of: str) -> tuple[str, str, bytes]:
        """The position CSV, written to the object store in the
        export idiom: a versioned key, the SHA-256 of the bytes
        returned beside them so a caller can record the digest
        of exactly what was filed. Returns (key, digest, bytes).
        Rendering is deterministic — the same store state at the
        same as-of renders the same bytes."""
        report = self.ap_position(as_of)
        data = position_csv(report)
        key = export_key_for_position(report.as_of)
        self.pipeline.objects.put(key, data, "text/csv")
        return key, export_digest(data), data

    # -- PO coverage -------------------------------------------------

    def po_coverage(self) -> CoverageReport:
        """PO coverage over the whole store (coverage.py):
        every order's cumulative APPROVED billing against the
        total the order register states, ranked EXCEEDED first
        and UNMEASURED last. A read over the store and the
        reference data — building it writes nothing, and it
        changes no match outcome: the match is per-invoice
        arithmetic, coverage is the accumulation beside it."""
        return build_coverage(
            self.store.list(), self.store.reference.purchase_orders
        )

    def record_coverage(self, record_id: str) -> RecordCoverage:
        """The coverage fact for one invoice, counting itself —
        what the approver's surfaces show before the gate
        decides. A record that is not an invoice, or an invoice
        naming no purchase order, has no coverage fact, and
        CoverageError names why. An invoice naming an order
        that is not on the register is NOT an error: its fact
        reads UNMEASURED, billed sum shown, no verdict."""
        record = self.get_record(record_id)
        if record.doc_class != "invoice":
            raise CoverageError(
                f"record {record_id} is a {record.doc_class}, not an "
                "invoice — coverage is a fact about an invoice "
                "billing against an order"
            )
        if record.extraction is None:
            raise CoverageError(
                f"invoice {record_id} carries no captured extraction; "
                "the order it names was never read"
            )
        if not po_reference_of(record):
            raise CoverageError(
                f"invoice {record_id} names no purchase order — "
                "coverage measures billing against an order, and "
                "this invoice stands outside any order"
            )
        return _coverage_fact(
            record, self.store.list(),
            self.store.reference.purchase_orders,
        )

    # -- the supplier master's bank history ----------------------

    def record_bank_version(
        self,
        supplier_id: str,
        bank_account: str,
        bank_name: str,
        effective_from: str,
        actor: str,
        source: str,
    ) -> BankVersion:
        """Record a legitimate bank change on the supplier
        master: a new version of one supplier's bank details,
        effective from a stated date, appended to the series
        and never editing what stands.

        This is the act the fraud screen's doctrine points at —
        legitimate changes are verified OUT OF BAND and recorded
        in the master; they never ride in on an invoice. So the
        recording must carry its provenance: who recorded it
        (``actor``) and what it was recorded FROM (``source`` —
        the change notice, the verified callback, the evidence
        a human checked). A recording without either is refused
        by name: an unattributed bank change is exactly the
        fraud pattern, wearing a master's clothes.

        Effective dates move forward only. A version takes
        effect after every version already on file, so the
        version in force on any date is unambiguous and no
        recording can silently rewrite what the screen said
        about the past; a correction to a wrongly recorded
        version is a NEW version, not an edit. Details identical
        to the latest version change nothing and are refused as
        the no-op they are. Recording a version re-judges no
        record: the screen's verdicts stand stamped on the
        records that earned them."""
        supplier = self.store.reference.suppliers.get(supplier_id)
        if supplier is None:
            raise RecordNotFound(
                f"no supplier {supplier_id} on the supplier master"
            )
        if not actor or not actor.strip():
            raise MasterError(
                "recording a bank version must name who recorded "
                "it — a bank change with no name on it is not a "
                "master record, it is a rumour"
            )
        if not source or not source.strip():
            raise MasterError(
                "recording a bank version must state what it was "
                "recorded from — bank details are verified out of "
                "band, and the source is the record of that "
                "verification (the supplier's change notice, the "
                "verified callback); without it the change is "
                "unprovenanced and refused"
            )
        if not bank_account or not bank_account.strip():
            raise MasterError(
                "a bank version must state the account — the "
                "master never holds an empty account"
            )
        effective_from = (effective_from or "").strip()
        try:
            effective_from = date.fromisoformat(effective_from).isoformat()
        except ValueError:
            raise MasterError(
                "a bank version must state the date it takes "
                f"effect, as an ISO date — got {effective_from!r}; "
                "the fraud screen judges invoices as of their own "
                "dates, so an undated change could bless or damn "
                "documents it was never about"
            ) from None
        series = self.store.reference.bank_versions_for(supplier_id)
        latest = series[-1]
        dated = [v.effective_from for v in series if v.effective_from]
        if dated and effective_from <= max(dated):
            raise MasterError(
                f"effective date {effective_from} does not move "
                f"supplier {supplier_id}'s bank history forward — "
                f"version {latest.version} already stands "
                f"effective {max(dated)}; a new version takes "
                "effect after it, and a correction is recorded "
                "as a new version, never as a rewrite of a date "
                "the screen has already judged by"
            )
        if normalise_account(bank_account) == normalise_account(
            latest.bank_account
        ) and (bank_name or "").strip() == latest.bank_name:
            raise MasterError(
                f"the stated details are identical to version "
                f"{latest.version} of supplier {supplier_id}'s "
                "bank details — a version that changes nothing "
                "records nothing"
            )
        version = BankVersion(
            version=latest.version + 1,
            bank_account=bank_account.strip(),
            bank_name=(bank_name or "").strip(),
            effective_from=effective_from,
            recorded_by=actor.strip(),
            recorded_at=_now(),
            source=source.strip(),
        )
        return self.store.reference.record_bank_version(
            supplier_id, version)

    def bank_history(self, supplier_id: str, as_of: str) -> BankHistory:
        """One supplier's bank timeline as of ``as_of``: every
        version in recording order — details, effective date,
        provenance — and the version in force at that date
        named. A read over the reference data; building it
        writes nothing. An id that is not on the master is a
        404, the ledger's own rule."""
        supplier = self.store.reference.suppliers.get(supplier_id)
        if supplier is None:
            raise RecordNotFound(
                f"no supplier {supplier_id} on the supplier master"
            )
        return build_history(
            supplier,
            self.store.reference.bank_versions_for(supplier_id),
            as_of,
        )

    # -- the supplier ledger -------------------------------------

    def supplier_index(self, as_of: str) -> SupplierIndex:
        """The ledger index: every supplier on the master with
        its counts and open totals, and the documents that
        joined to no supplier, named as their own section. A
        read over the store — building it writes nothing."""
        return build_index(
            list(self.store.reference.suppliers.values()),
            self.store.list(),
            self.store.list_reconciliations(),
            as_of,
            self.store.reference.purchase_orders,
            self.store.reference.bank_versions,
        )

    def supplier_ledger(
        self, supplier_id: str, as_of: str
    ) -> SupplierLedger:
        """One supplier's ledger: the whole relationship with
        every figure tracing to its record, its payable totals
        tied to the position at ``as_of`` (ledger.py states the
        construction). An id that is not on the master is a
        404 — there is no ledger for a supplier AP does not
        govern, and resemblance to a master name is never a
        join."""
        supplier = self.store.reference.suppliers.get(supplier_id)
        if supplier is None:
            raise RecordNotFound(
                f"no supplier {supplier_id} on the supplier master"
            )
        return build_ledger(
            supplier,
            self.store.list(),
            self.store.list_reconciliations(),
            as_of,
            self.store.reference.purchase_orders,
            self.store.reference.bank_versions,
        )

    # -- payment runs ----------------------------------------------

    def _next_run_id(self, value_date: str) -> str:
        n = len(self.store.list_payment_runs()) + 1
        while True:
            run_id = f"RUN-{value_date}-{n:02d}"
            if self.store.get_payment_run(run_id) is None:
                return run_id
            n += 1

    def create_payment_run(
        self,
        value_date: str,
        record_ids: list[str] | None = None,
        currencies: list[str] | None = None,
        amounts: dict[str, float] | None = None,
    ) -> PaymentRun:
        """Assemble a payment run and hold its members.

        Candidates are the named records, in the order named,
        or — unnamed — every APPROVED payable record, the
        packet's universe. Ineligible candidates ride the run
        as named refusals; an unknown named id fails loudly
        (a mistyped id must never silently shrink a batch);
        zero eligible records fails the assembly outright.
        Members are marked IN_RUN on their records, so a second
        assembly can only refuse them by name. ``amounts``
        names the part each member joins at, per record id;
        members it does not name join at their whole open
        amount.
        """
        if not value_date or not value_date.strip():
            raise PaymentRunError("a payment run needs a value date")
        value_date = value_date.strip()
        if record_ids is not None:
            candidates: list[DocumentRecord] = []
            seen: set[str] = set()
            for record_id in record_ids:
                if record_id in seen:
                    continue
                seen.add(record_id)
                candidates.append(self.get_record(record_id))
        else:
            candidates = [
                rec for rec in self.store.list()
                if rec.status == "APPROVED" and rec.doc_class in PAYABLE_CLASSES
            ]
        run = assemble_run(
            self._next_run_id(value_date), value_date, candidates,
            currencies=currencies, amounts=amounts, created_at=_now(),
        )
        for line in run.lines:
            record = self.get_record(line.record_id)
            record.payment_run_id = run.run_id
            record.payment_state = "IN_RUN"
            record.audit.append({
                "event": "payment_run_joined", "at": _now(),
                "detail": f"member of payment run {run.run_id} "
                          f"(value date {run.value_date})"
                          + (f" at a part of {line.amount:.2f} of its "
                             f"open {line.open_amount:.2f}"
                             if line.amount != line.open_amount else ""),
            })
            record.updated_at = _now()
            self.store.save(record)
        return self.store.save_payment_run(run)

    def get_payment_run(self, run_id: str) -> PaymentRun:
        run = self.store.get_payment_run(run_id)
        if run is None:
            raise RecordNotFound(f"no payment run {run_id}")
        return run

    def list_payment_runs(self, status: str | None = None) -> list[PaymentRun]:
        return self.store.list_payment_runs(status=status)

    def decide_payment_run(
        self, run_id: str, action: str, actor: str, reason: str = ""
    ) -> PaymentRun:
        """The treasurer's word: release the batch, or return
        it for rework. Release generates the bank-instruction
        export first — if the export cannot be built (a member
        supplier not on file), nothing mutates — then records
        each member's instructed part on its payments ledger
        and stands the member at its new state: PAID_PROPOSED
        when this release completes the invoice, PART_PAID
        with the remainder still open when it does not. Return
        frees every member to join a later run — a member with
        payments already released goes back to standing
        PART_PAID, never to unpaid. The records' APPROVED
        status — the approver's decision — is never touched:
        the run is a proposal state layered over it."""
        if action not in ("release", "return"):
            raise GateError(
                f"treasurer action must be 'release' or 'return', got {action!r}"
            )
        if not actor or not actor.strip():
            raise GateError("a treasurer decision must name its treasurer")
        if action == "return" and not (reason and reason.strip()):
            raise GateError(
                "returning a payment run requires a reason — the AP "
                "team reworks the batch on it"
            )
        run = self.get_payment_run(run_id)
        if run.status != "AWAITING_TREASURER":
            raise GateError(
                f"payment run {run_id} is {run.status}; only "
                "AWAITING_TREASURER runs can be decided"
            )
        if action == "release":
            # Build and store the export BEFORE any state moves:
            # a release that cannot produce its artifact has not
            # happened, and must not half-happen. The export pays
            # the bank version in force today — the master's
            # present word for money moving now (payments.py).
            data = build_export_csv(
                run, self.store.reference.suppliers,
                self.store.reference.bank_versions,
                datetime.now(timezone.utc).date().isoformat(),
            )
            key = export_key_for(run.run_id)
            self.pipeline.objects.put(key, data, "text/csv")
            run.export_key = key
            run.export_digest = export_digest(data)
            run.export_generated_at = _now()
            run.status = "RELEASED"
        else:
            run.status = "RETURNED"
        run.decision = TreasurerDecision(
            decision=action, actor=actor, reason=reason, at=_now()  # type: ignore[arg-type]
        )
        run.audit.append({
            "event": "treasurer_decision", "at": _now(),
            "detail": f"{action} by {actor}" + (f" — {reason}" if reason else ""),
        })
        run.updated_at = _now()
        saved = self.store.save_payment_run(run)
        for line in run.lines:
            record = self.get_record(line.record_id)
            if action == "release":
                record.payments_applied.append(PaymentEntry(
                    run_id=run.run_id, amount=line.amount,
                    currency=line.currency, actor=actor, at=_now(),
                ))
                remaining = open_amount(record)
                if remaining == 0:
                    record.payment_state = "PAID_PROPOSED"
                    record.audit.append({
                        "event": "payment_run_paid_proposed", "at": _now(),
                        "detail": f"released in payment run {run.run_id}; the "
                                  "export proposes this payment — nothing has "
                                  "been paid by this system",
                    })
                else:
                    record.payment_state = "PART_PAID"
                    record.audit.append({
                        "event": "payment_run_part_paid", "at": _now(),
                        "detail": f"released in payment run {run.run_id} at "
                                  f"{line.amount:.2f}; "
                                  f"{paid_amount(record):.2f} now instructed "
                                  f"across released runs and {remaining:.2f} "
                                  "still open — the export proposes this "
                                  "part payment; nothing has been paid by "
                                  "this system",
                    })
            else:
                if record.payments_applied:
                    record.payment_state = "PART_PAID"
                    record.payment_run_id = \
                        record.payments_applied[-1].run_id
                    freed_detail = (
                        f"payment run {run.run_id} was returned by "
                        f"{actor}; the invoice stands part-paid — "
                        f"{paid_amount(record):.2f} instructed across "
                        f"released runs, {open_amount(record):.2f} open "
                        "for a later run"
                    )
                else:
                    record.payment_run_id = None
                    record.payment_state = None
                    freed_detail = (
                        f"payment run {run.run_id} was returned by "
                        f"{actor}; free to join a later run"
                    )
                record.audit.append({
                    "event": "payment_run_freed", "at": _now(),
                    "detail": freed_detail,
                })
            record.updated_at = _now()
            self.store.save(record)
        return saved

    def payment_run_export(self, run_id: str) -> bytes:
        """The released run's bank-instruction CSV, byte for
        byte as generated at release — served from the object
        store, never recomputed, so what the treasurer released
        is what anyone can ever download."""
        run = self.get_payment_run(run_id)
        if run.status != "RELEASED" or run.export_key is None:
            raise GateError(
                f"payment run {run_id} is {run.status}; its export exists "
                "only once the treasurer releases it"
            )
        try:
            return self.pipeline.objects.get(run.export_key)
        except KeyError:
            raise PaymentRunError(
                f"export artifact {run.export_key} for run {run_id} is "
                "missing from the object store"
            ) from None

    # -- remittance confirmation ---------------------------------

    def confirm_remittance(self, advice_record_id: str) -> PaymentConfirmation:
        """Reconcile one remittance advice against the payment
        run it answers.

        The run is the one the advice names — its header's
        reference, or one run named unanimously by its lines.
        An advice naming no run, a run that is not on file, or
        a run the treasurer has not released is held UNMATCHED
        on its confirmation record with the reason named; it is
        never guessed onto a run. A matched confirmation writes
        its own record and the confirmation overlay on each
        member it classifies — the run, its decision, its
        export, and every approval are left exactly as they
        were."""
        record = self.get_record(advice_record_id)
        if record.doc_class != "remittance_advice":
            raise ConfirmationError(
                f"record {advice_record_id} is a {record.doc_class}, not a "
                "remittance advice — only advices confirm payment runs"
            )
        if record.extraction is None:
            raise ConfirmationError(
                f"remittance advice {advice_record_id} carries no captured "
                "extraction; its paid lines were never read"
            )
        existing = [
            c for c in self.store.list_confirmations()
            if c.advice_record_id == advice_record_id
        ]
        confirmation_id = f"CONF-{advice_record_id}"
        if existing:
            confirmation_id = f"{confirmation_id}-{len(existing) + 1}"

        run_id, why_not = resolve_run_reference(record.extraction)
        if run_id is None:
            return self.store.save_confirmation(unmatched_confirmation(
                confirmation_id, record, why_not, created_at=_now(),
            ))
        run = self.store.get_payment_run(run_id)
        if run is None:
            return self.store.save_confirmation(unmatched_confirmation(
                confirmation_id, record,
                f"the advice names payment run {run_id}, which is not on "
                "file — an advice is never guessed onto a run it did "
                "not name",
                created_at=_now(),
            ))
        if run.status != "RELEASED":
            return self.store.save_confirmation(unmatched_confirmation(
                confirmation_id, record,
                f"the advice names payment run {run_id}, which is "
                f"{run.status} — confirmations answer released runs, "
                "because only a released run has proposed payments "
                "that could have left the bank",
                created_at=_now(),
            ))

        confirmation = reconcile_confirmation(
            confirmation_id, record, run, created_at=_now(),
        )
        saved = self.store.save_confirmation(confirmation)
        self._record_confirmation_overlays(saved)
        return saved

    def _record_confirmation_overlays(
        self, confirmation: PaymentConfirmation
    ) -> None:
        """The one write a confirmation makes beyond its own
        record: the confirmation overlay on each member record
        it classifies, with the audit entry that explains it.
        Shared by the named-run reconciliation and the
        worklist's linked reconciliation, so a member's overlay
        means the same thing whichever path classified it."""
        for line in confirmation.lines:
            overlay = OVERLAY_BY_CLASS[line.classification]
            if overlay is None or line.member_record_id is None:
                continue
            member = self.get_record(line.member_record_id)
            member.payment_confirmation = overlay
            member.payment_confirmation_id = confirmation.confirmation_id
            member.audit.append({
                "event": "payment_confirmation_recorded", "at": _now(),
                "detail": (
                    f"remittance confirmation {confirmation.confirmation_id} "
                    f"(advice {confirmation.advice_record_id}) classifies "
                    f"this member {line.classification} against run "
                    f"{confirmation.run_id}"
                ),
            })
            member.updated_at = _now()
            self.store.save(member)

    def get_confirmation(self, confirmation_id: str) -> PaymentConfirmation:
        confirmation = self.store.get_confirmation(confirmation_id)
        if confirmation is None:
            raise RecordNotFound(f"no confirmation {confirmation_id}")
        return confirmation

    def list_confirmations(
        self, run_id: str | None = None
    ) -> list[PaymentConfirmation]:
        return self.store.list_confirmations(run_id=run_id)

    def decide_confirmation(
        self, confirmation_id: str, action: str, actor: str, note: str = ""
    ) -> PaymentConfirmation:
        """The controller's word on a confirmation: accept it,
        or flag it back with a note the AP team works from.
        An UNMATCHED confirmation cannot be decided — there is
        nothing to accept until an advice names a released run
        on file and is reconciled against it."""
        if action not in ("accept", "flag_back"):
            raise GateError(
                f"controller action must be 'accept' or 'flag_back', got {action!r}"
            )
        if not actor or not actor.strip():
            raise GateError("a controller decision must name its controller")
        if action == "flag_back" and not (note and note.strip()):
            raise GateError(
                "flagging a confirmation back requires a note — the AP "
                "team takes it to the bank"
            )
        confirmation = self.get_confirmation(confirmation_id)
        if confirmation.status == "UNMATCHED":
            raise GateError(
                f"confirmation {confirmation_id} is UNMATCHED — "
                f"{confirmation.unmatched_reason}; there is nothing to "
                "decide until an advice names a released run on file"
            )
        if confirmation.status != "AWAITING_CONTROLLER":
            raise GateError(
                f"confirmation {confirmation_id} is {confirmation.status}; "
                "only AWAITING_CONTROLLER confirmations can be decided"
            )
        confirmation.decision = ControllerDecision(
            decision=action, actor=actor, note=note, at=_now()  # type: ignore[arg-type]
        )
        confirmation.status = (
            "ACCEPTED" if action == "accept" else "FLAGGED_BACK"
        )
        confirmation.audit.append({
            "event": "controller_decision", "at": _now(),
            "detail": f"{action} by {actor}" + (f" — {note}" if note else ""),
        })
        confirmation.updated_at = _now()
        return self.store.save_confirmation(confirmation)

    # -- the unmatched-advice worklist --------------------------

    def advice_worklist(self) -> AdviceWorklist:
        """The pile of held advices with the candidates
        computed for each (worklist.py): a pure read that
        confirms nothing. An advice leaves the pile only when
        a controller links it or marks it not ours."""
        return build_worklist(self.store)

    def link_confirmation(
        self,
        confirmation_id: str,
        actor: str,
        links: list[dict],
        rule: str = "manual",
    ) -> PaymentConfirmation:
        """A controller ties a held advice to a released run by
        hand: the worklist's act.

        ``links`` pairs advice lines with run members —
        ``{"advice_line": index, "run_id": ..., "record_ids":
        [...]}`` — all against ONE run, because a confirmation
        answers one run (the advice's remaining business, if
        any, is a second act against a fresh held record).
        The pairing is the controller's: it may follow a
        worklist candidate or be the controller's own choice,
        and ``rule`` names which — a candidate's rule, or
        "manual". Classification then runs under round 4's
        exact rule with the pairing standing in for the number
        match: a single member classifies exact, short, or
        over on the money as it stands; a multi-member lump
        settles only when it equals the members' total to the
        cent — any other lump is refused here, by name, with
        the remedy, because round 4 has no honest way to split
        one bank line across members it does not cover."""
        if not actor or not actor.strip():
            raise GateError("linking an advice must name its controller")
        held = self.get_confirmation(confirmation_id)
        if held.status == "NOT_OURS":
            mark = held.not_ours
            raise GateError(
                f"confirmation {confirmation_id} was marked NOT_OURS "
                f"by {mark.actor if mark else 'a controller'}"
                + (f" — {mark.reason}" if mark else "")
                + "; a not-ours advice is closed, and the mark is "
                "the end of its worklist life"
            )
        if held.status != "UNMATCHED":
            raise GateError(
                f"confirmation {confirmation_id} is {held.status} and "
                f"already answers run {held.run_id} — only a held "
                "UNMATCHED advice can be linked"
            )
        already = [
            c for c in self.store.list_confirmations()
            if c.linked_from == confirmation_id
        ]
        if already:
            raise GateError(
                f"confirmation {confirmation_id} was already linked "
                f"as {already[0].confirmation_id} against run "
                f"{already[0].run_id}; any lines it did not cover "
                "answer in a fresh confirmation of the advice"
            )
        if not links:
            raise ConfirmationError(
                "a link names no lines — pair at least one advice "
                "line with the run members it answers"
            )
        advice = self.get_record(held.advice_record_id)
        if advice.extraction is None:
            raise ConfirmationError(
                f"remittance advice {advice.record_id} carries no "
                "captured extraction; its paid lines were never read"
            )
        advice_lines = advice.extraction.remittance_lines

        run_ids = {str(link.get("run_id") or "") for link in links}
        if len(run_ids) != 1 or not next(iter(run_ids)):
            raise GateError(
                "one link act ties an advice to ONE run — the links "
                f"named {', '.join(sorted(run_ids)) or 'no run'}; "
                "link each run's members in its own act"
            )
        run_id = next(iter(run_ids))
        run = self.store.get_payment_run(run_id)
        if run is None:
            raise GateError(
                f"payment run {run_id} is not on file — a link can "
                "only tie an advice to a run that exists"
            )
        if run.status != "RELEASED":
            raise GateError(
                f"payment run {run_id} is {run.status} — links answer "
                "released runs, because only a released run has "
                "proposed payments that could have left the bank"
            )
        members_by_id = {line.record_id: (i, line)
                         for i, line in enumerate(run.lines)}
        settled = {
            (c.run_id, line.member_record_id)
            for c in self.store.list_confirmations()
            for line in c.lines
            if c.run_id is not None
            and line.classification == "paid_as_proposed"
            and line.member_record_id is not None
        }

        pairings: list[tuple[int, list[int]]] = []
        seen_advice: set[int] = set()
        seen_members: set[str] = set()
        for link in links:
            advice_index = link.get("advice_line")
            if not isinstance(advice_index, int) \
                    or not 0 <= advice_index < len(advice_lines):
                raise ConfirmationError(
                    f"advice line {advice_index!r} is not a line of "
                    f"advice {advice.record_id}, which carries "
                    f"{len(advice_lines)} paid line(s)"
                )
            if advice_index in seen_advice:
                raise ConfirmationError(
                    f"advice line {advice_index} is paired twice — "
                    "one advice line answers one set of members"
                )
            seen_advice.add(advice_index)
            record_ids = [str(r) for r in link.get("record_ids") or []]
            if not record_ids:
                raise ConfirmationError(
                    f"advice line {advice_index} is paired with no "
                    "run members — name the members it answers"
                )
            member_indices: list[int] = []
            for record_id in record_ids:
                if record_id in seen_members:
                    raise GateError(
                        f"run member {record_id} is paired twice — a "
                        "member is answered once in one confirmation"
                    )
                seen_members.add(record_id)
                found = members_by_id.get(record_id)
                if found is None:
                    raise GateError(
                        f"record {record_id} is not a member of "
                        f"payment run {run_id} — a link pairs an "
                        "advice line with members of the run it ties to"
                    )
                member_indices.append(found[0])
                if (run_id, record_id) in settled:
                    raise GateError(
                        f"run member {record_id} "
                        f"({found[1].invoice_number}) was already "
                        "confirmed paid as proposed for this run — "
                        "settled money is never linked twice"
                    )
            aline = advice_lines[advice_index]
            if len(member_indices) > 1:
                members = [run.lines[i] for i in member_indices]
                suppliers = {m.supplier_id for m in members}
                if len(suppliers) > 1:
                    raise GateError(
                        "a combined link covers one supplier's lines "
                        "of one run — the members named span "
                        f"{len(suppliers)} suppliers; link each "
                        "supplier's members separately"
                    )
                currencies = {m.currency for m in members}
                if currencies != {aline.currency}:
                    raise GateError(
                        "a combined link settles in the advice "
                        f"line's currency ({aline.currency}) — the "
                        "members named stand in "
                        f"{', '.join(sorted(currencies))}; link the "
                        "members singly, where a currency break "
                        "classifies short in round 4's own words"
                    )
                total = round(sum(m.amount for m in members), 2)
                paid = round(float(aline.amount_paid), 2)
                if total != paid:
                    raise GateError(
                        f"advice line {advice_index} pays {paid:.2f} "
                        f"{aline.currency} against members totalling "
                        f"{total:.2f} — a difference of "
                        f"{paid - total:+.2f}; a combined link "
                        "settles only when the lump equals the "
                        "members' total to the cent. Link the lump "
                        "against a single member instead (it will "
                        "classify short or over, in round 4's own "
                        "words), or hold the advice for a corrected "
                        "one from the bank"
                    )
            pairings.append((advice_index, member_indices))

        existing = [
            c for c in self.store.list_confirmations()
            if c.advice_record_id == advice.record_id
        ]
        new_id = f"CONF-{advice.record_id}"
        if existing:
            new_id = f"{new_id}-{len(existing) + 1}"
        confirmation = reconcile_linked_confirmation(
            new_id, advice, run, pairings,
            linked_by=actor, linked_rule=rule,
            linked_from=confirmation_id, created_at=_now(),
        )
        saved = self.store.save_confirmation(confirmation)
        self._record_confirmation_overlays(saved)
        held.audit.append({
            "event": "linked", "at": _now(),
            "detail": (
                f"linked by {actor} under {rule} against run "
                f"{run.run_id} as {saved.confirmation_id}"
            ),
        })
        held.updated_at = _now()
        self.store.save_confirmation(held)
        return saved

    def mark_advice_not_ours(
        self, confirmation_id: str, actor: str, reason: str
    ) -> PaymentConfirmation:
        """A controller closes a held advice as not this
        company's money: the bank sent another customer's
        advice, or the payment belongs to another company's
        batch. The mark names its controller and its reason,
        the advice leaves the worklist, and the record stands —
        a closed item is still evidence of what arrived."""
        if not actor or not actor.strip():
            raise GateError(
                "marking an advice not ours must name its controller"
            )
        if not reason or not reason.strip():
            raise GateError(
                "marking an advice not ours requires a reason — "
                "the next person to meet this advice in the pile "
                "reads why it was closed"
            )
        held = self.get_confirmation(confirmation_id)
        if held.status == "NOT_OURS":
            mark = held.not_ours
            raise GateError(
                f"confirmation {confirmation_id} is already NOT_OURS"
                + (f" — marked by {mark.actor}: {mark.reason}"
                   if mark else "")
            )
        if held.status != "UNMATCHED":
            raise GateError(
                f"confirmation {confirmation_id} is {held.status} and "
                f"answers run {held.run_id} — only a held UNMATCHED "
                "advice can be marked not ours"
            )
        already = [
            c for c in self.store.list_confirmations()
            if c.linked_from == confirmation_id
        ]
        if already:
            raise GateError(
                f"confirmation {confirmation_id} was already linked "
                f"as {already[0].confirmation_id} — a linked advice "
                "has been answered for; it cannot be marked not ours"
            )
        held.status = "NOT_OURS"
        held.not_ours = NotOursMark(actor=actor, reason=reason, at=_now())
        held.audit.append({
            "event": "marked_not_ours", "at": _now(),
            "detail": f"marked not ours by {actor} — {reason}",
        })
        held.updated_at = _now()
        return self.store.save_confirmation(held)
