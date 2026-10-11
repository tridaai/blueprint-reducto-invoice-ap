"""The pipeline: a LangGraph graph with a human gate in the middle.

    intake → split & classify → extract → screen → match → gate → apply

The graph earns its place at the gate: a submission whose
documents matched stops mid-graph (a LangGraph ``interrupt``,
checkpointed) until a named approver decides, and resumes
exactly where it stopped. Everything before the gate is the
deterministic work — the document engine behind its port, the
guardrails, the match in code. Everything after it is the
application of a human's recorded decision.

Records, not state, are the system of record: the graph's state
carries ids and the submission bytes; every fact about a document
lives on its :class:`DocumentRecord` in the store.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from . import guardrails
from .matching import match_invoice
from .policy import ATTACHMENT_CLASSES, DEFAULT_POLICY, MatchPolicy
from .ports import DocumentPort, ObjectStorePort
from .schemas import DocumentRecord, GateDecision

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PipelineState(TypedDict, total=False):
    submission_id: str
    filename: str
    content_type: str
    data_b64: str
    object_key: str
    injection_flags: list[str]
    record_ids: list[str]
    decisions: dict[str, dict[str, Any]]


class ApPipeline:
    """The compiled pipeline plus the collaborators it runs on."""

    def __init__(
        self,
        documents: DocumentPort,
        store,
        objects: ObjectStorePort,
        *,
        policy: MatchPolicy = DEFAULT_POLICY,
        checkpointer=None,
    ) -> None:
        self.documents = documents
        self.store = store
        self.objects = objects
        self.policy = policy
        builder = StateGraph(PipelineState)
        builder.add_node("intake", self._intake)
        builder.add_node("process", self._process)
        builder.add_node("gate", self._gate)
        builder.add_node("apply", self._apply)
        builder.add_edge(START, "intake")
        builder.add_edge("intake", "process")
        builder.add_edge("process", "gate")
        builder.add_edge("gate", "apply")
        builder.add_edge("apply", END)
        self.graph = builder.compile(checkpointer=checkpointer)

    # -- nodes -----------------------------------------------------

    def _intake(self, state: PipelineState) -> dict:
        data = base64.b64decode(state["data_b64"])
        key = f"submissions/{state['submission_id']}/{state['filename']}"
        self.objects.put(key, data, state.get("content_type", "application/octet-stream"))
        # Injection screen runs on the text layer BEFORE the bytes
        # travel onward to the engine: document text is untrusted.
        text = data.decode("utf-8", errors="replace")
        flags, _fenced = guardrails.screen_injection(text)
        return {"object_key": key, "injection_flags": flags}

    def _process(self, state: PipelineState) -> dict:
        data = base64.b64decode(state["data_b64"])
        uploaded = self.documents.upload(
            state["filename"], data, state.get("content_type", "application/octet-stream")
        )
        record_ids: list[str] = []
        for index, sub in enumerate(self.documents.split(uploaded)):
            record = self._process_one(state, sub, index)
            record_ids.append(record.record_id)
        return {"record_ids": record_ids}

    def _process_one(self, state: PipelineState, sub, index: int) -> DocumentRecord:
        ref = self.store.reference
        record = DocumentRecord(
            record_id=f"REC-{state['submission_id']}-{index}",
            submission_id=state["submission_id"],
            created_at=_now(), updated_at=_now(),
            injection_flags=list(state.get("injection_flags") or []),
            audit=[{"event": "received", "at": _now()}],
        )
        classification = self.documents.classify(sub)
        record.doc_class = classification.doc_class  # type: ignore[assignment]

        if record.doc_class in ATTACHMENT_CLASSES:
            record.status = "RECEIVED"
            record.audit.append({
                "event": "attachment_stored",
                "at": _now(),
                "detail": f"classified {record.doc_class}; stored with the submission, not processed for payment",
            })
            return self.store.save(record)

        refusal = guardrails.payable_refusal(record.doc_class)
        if refusal:
            if record.doc_class == "statement":
                # The payment path refuses a statement, as it must —
                # but the statement's open-item lines are the input
                # to supplier reconciliation, a separate path off
                # this same intake. Capture them on the record
                # before the refusal lands, so reconciliation reads
                # records, not raw documents.
                extraction = self.documents.extract(sub)
                extraction.doc_class = record.doc_class
                record.extraction = extraction
                supplier = ref.supplier_named(extraction.value("supplier_name"))
                if supplier is not None:
                    record.supplier_id = supplier.supplier_id
                    record.supplier_name = supplier.name
                record.audit.append({
                    "event": "statement_captured", "at": _now(),
                    "detail": (
                        f"{len(extraction.statement_lines)} open-item lines captured "
                        "for supplier reconciliation; the payment path still refuses "
                        "this document"
                    ),
                })
            record.status = "REFUSED"
            record.refusal_reason = refusal
            record.audit.append({"event": "refused", "at": _now(), "detail": refusal})
            record.updated_at = _now()
            return self.store.save(record)

        extraction = self.documents.extract(sub)
        extraction.doc_class = record.doc_class
        record.extraction = extraction
        supplier = ref.supplier_named(extraction.value("supplier_name"))
        po = ref.purchase_orders.get(str(extraction.value("po_reference") or ""))
        if supplier is None and po is not None:
            supplier = ref.suppliers.get(po.supplier_id)
        if supplier is not None:
            record.supplier_id = supplier.supplier_id
            record.supplier_name = supplier.name

        # Fraud screen — before the match outcome means anything.
        seen_bank = extraction.value("bank_account")
        record.bank_account_seen = str(seen_bank) if seen_bank else None
        record.fraud_flags = guardrails.screen_bank_details(record.bank_account_seen, supplier)

        receipt = ref.receipts_by_po.get(po.po_number) if po else None
        result = match_invoice(extraction, po, receipt, policy=self.policy)
        if extraction.doc_class == "invoice":
            result = self._duplicate_check(record, result)
        record.match = result

        if record.fraud_flags:
            record.status = "FRAUD_REVIEW"
            record.audit.append({
                "event": "fraud_hold", "at": _now(), "detail": "; ".join(record.fraud_flags),
            })
        elif result.status == "EXCEPTION":
            record.status = "EXCEPTION"
            record.audit.append({
                "event": "exception", "at": _now(),
                "detail": ", ".join(result.exception_codes),
            })
        else:
            record.status = "AWAITING_APPROVER"
            record.audit.append({"event": "awaiting_approver", "at": _now()})
        record.updated_at = _now()
        return self.store.save(record)

    def _duplicate_check(self, record: DocumentRecord, result):
        """A second live record for the same supplier + invoice
        number is a duplicate submission — an exception a human
        resolves, never a silent drop of either copy."""
        number = record.extraction.value("invoice_number") if record.extraction else None
        if not number or record.supplier_id is None:
            return result
        for other in self.store.list():
            if other.record_id == record.record_id or other.doc_class != "invoice":
                continue
            if other.supplier_id != record.supplier_id or other.extraction is None:
                continue
            if other.extraction.value("invoice_number") == number:
                result.status = "EXCEPTION"
                if "DUPLICATE_INVOICE" not in result.exception_codes:
                    result.exception_codes.append("DUPLICATE_INVOICE")
                result.notes.append(
                    f"Invoice number {number} already processed as {other.record_id} "
                    f"for this supplier — duplicate submission."
                )
                return result
        return result

    def _gate(self, state: PipelineState) -> dict:
        awaiting = [
            rid for rid in state.get("record_ids", [])
            if (rec := self.store.get(rid)) and rec.status == "AWAITING_APPROVER"
        ]
        if not awaiting:
            return {"decisions": {}}
        decisions = interrupt({
            "awaiting_record_ids": awaiting,
            "submission_id": state["submission_id"],
        })
        return {"decisions": decisions or {}}

    def _apply(self, state: PipelineState) -> dict:
        for record_id, raw in (state.get("decisions") or {}).items():
            record = self.store.get(record_id)
            if record is None or record.status != "AWAITING_APPROVER":
                continue
            decision = GateDecision(
                decision=raw["decision"], actor=raw["actor"],
                reason=raw.get("reason", ""), at=_now(),
            )
            record.decision = decision
            record.status = "APPROVED" if decision.decision == "approve" else "REJECTED"
            record.audit.append({
                "event": "gate_decision", "at": _now(),
                "detail": f"{decision.decision} by {decision.actor}",
            })
            record.updated_at = _now()
            self.store.save(record)
        return {}
