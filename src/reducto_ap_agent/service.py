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
  proposal, computed fresh, that executes nothing.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone

from langgraph.types import Command

from .errors import GateError, RecordNotFound
from .graph import ApPipeline
from .schemas import DocumentRecord, PaymentLine, PaymentPacket

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
