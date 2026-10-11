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
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone

from langgraph.types import Command

from .errors import GateError, PaymentRunError, ReconciliationError, RecordNotFound
from .graph import ApPipeline
from .payments import (
    PAYABLE_CLASSES,
    assemble_run,
    build_export_csv,
    export_digest,
    export_key_for,
)
from .reconciliation import reconcile_statement
from .schemas import (
    ControllerDecision,
    DocumentRecord,
    PaymentLine,
    PaymentPacket,
    PaymentRun,
    Reconciliation,
    TreasurerDecision,
)

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
        if all(rid in pending for rid in awaiting):
            config = {"configurable": {"thread_id": record.submission_id}}
            self.pipeline.graph.invoke(Command(resume=dict(pending)), config=config)
            self._pending.pop(record.submission_id, None)
        return self.get_record(record_id)

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

    # -- the packet ------------------------------------------------------

    def payment_packet(self) -> PaymentPacket:
        lines: list[PaymentLine] = []
        totals: dict[str, float] = {}
        for record in self.store.list(status="APPROVED"):
            if record.extraction is None:
                continue
            amount = float(record.extraction.value("total") or 0.0)
            currency = record.extraction.currency
            lines.append(PaymentLine(
                record_id=record.record_id,
                supplier_name=record.supplier_name,
                invoice_number=str(record.extraction.value("invoice_number") or ""),
                currency=currency,
                amount=amount,
            ))
            totals[currency] = round(totals.get(currency, 0.0) + amount, 2)
        return PaymentPacket(
            packet_id=f"PKT-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
            lines=lines,
            totals_by_currency=totals,
            generated_at=_now(),
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
    ) -> PaymentRun:
        """Assemble a payment run and hold its members.

        Candidates are the named records, in the order named,
        or — unnamed — every APPROVED payable record, the
        packet's universe. Ineligible candidates ride the run
        as named refusals; an unknown named id fails loudly
        (a mistyped id must never silently shrink a batch);
        zero eligible records fails the assembly outright.
        Members are marked IN_RUN on their records, so a second
        assembly can only refuse them by name.
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
            currencies=currencies, created_at=_now(),
        )
        for line in run.lines:
            record = self.get_record(line.record_id)
            record.payment_run_id = run.run_id
            record.payment_state = "IN_RUN"
            record.audit.append({
                "event": "payment_run_joined", "at": _now(),
                "detail": f"member of payment run {run.run_id} "
                          f"(value date {run.value_date})",
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
        supplier not on file), nothing mutates — then locks
        every member as PAID_PROPOSED. Return frees every
        member to join a later run. The records' APPROVED
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
            # happened, and must not half-happen.
            data = build_export_csv(run, self.store.reference.suppliers)
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
                record.payment_state = "PAID_PROPOSED"
                record.audit.append({
                    "event": "payment_run_paid_proposed", "at": _now(),
                    "detail": f"released in payment run {run.run_id}; the "
                              "export proposes this payment — nothing has "
                              "been paid by this system",
                })
            else:
                record.payment_run_id = None
                record.payment_state = None
                record.audit.append({
                    "event": "payment_run_freed", "at": _now(),
                    "detail": f"payment run {run.run_id} was returned by "
                              f"{actor}; free to join a later run",
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
