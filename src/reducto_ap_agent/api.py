"""The HTTP surface: console, submissions, the gate, the packet.

Thin by design — every behaviour lives in the service, so what
the tests pin is what the console shows. The API's own jobs are
request IDs, JSON logging, honest error mapping, and naming the
live wiring (backend, profile, store) on ``/readiness`` so an
operator can see at a glance which deployment they are looking
at — including when the answers are "offline" and "in-memory".
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from . import db
from .checkpoints import checkpointer_name
from .config import document_backend, load_dotenv
from .errors import ApError
from .observability import configure_json_logging, new_request_id, request_id_ctx
from .profiles import resolve_profile
from .service import ApService
from .wiring import build_service

log = logging.getLogger(__name__)
_STATIC = Path(__file__).resolve().parent / "static"


class SubmitRequest(BaseModel):
    text: str
    filename: str = "document.pdf"
    submission_id: str | None = None


class DecisionRequest(BaseModel):
    decision: str
    actor: str
    reason: str = ""


class ReconDecisionRequest(BaseModel):
    action: str  # "accept" | "flag_back"
    actor: str
    note: str = ""


class PaymentRunRequest(BaseModel):
    value_date: str
    record_ids: list[str] | None = None  # None = every approved payable record
    currencies: list[str] | None = None  # None/empty = all currencies


class TreasurerDecisionRequest(BaseModel):
    action: str  # "release" | "return"
    actor: str
    reason: str = ""


class ConfirmationDecisionRequest(BaseModel):
    action: str  # "accept" | "flag_back"
    actor: str
    note: str = ""


class ApplyCreditRequest(BaseModel):
    actor: str


def create_app(service: ApService | None = None) -> FastAPI:
    load_dotenv()
    configure_json_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.run_migrations()
        yield

    app = FastAPI(title="Trida Blueprint — Reducto Invoice AP (in-VPC)", lifespan=lifespan)
    state: dict[str, ApService] = {}

    def svc() -> ApService:
        if "service" not in state:
            state["service"] = service or build_service()
        return state["service"]

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or new_request_id()
        token = request_id_ctx.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            request_id_ctx.reset(token)
        response.headers["X-Request-ID"] = request_id
        log.info(
            "request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
        return response

    @app.exception_handler(ApError)
    def ap_error_handler(request: Request, exc: ApError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    @app.get("/favicon.svg", include_in_schema=False)
    def favicon_svg() -> FileResponse:
        return FileResponse(_STATIC / "brand" / "favicon.svg")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon_ico() -> FileResponse:
        return FileResponse(_STATIC / "brand" / "favicon.ico")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (_STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readiness")
    def readiness() -> JSONResponse:
        service = svc()
        profile = resolve_profile()
        payload = {
            "ready": True,
            "deployment_profile": profile.name,
            "document_backend": document_backend(),
            "document_engine_endpoint": profile.reducto_base_url,
            "store": service.store.name,
            "checkpointer": checkpointer_name(),
        }
        return JSONResponse(content=payload)

    @app.get("/evals/results")
    def eval_results() -> dict:
        path = Path(__file__).resolve().parent / "data" / "eval_results.json"
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        return {"status": "not run yet — `make evals` produces this file"}

    @app.post("/demo/seed", status_code=201)
    def seed_demo() -> list[dict]:
        from .datasets import build_world

        service = svc()
        out: list[dict] = []
        for submission in build_world().submissions:
            out += [
                rec.model_dump()
                for rec in service.submit(
                    submission.submission_id, submission.filename,
                    submission.text.encode("utf-8"), "text/plain",
                )
            ]
        return out

    @app.post("/submissions", status_code=201)
    def submit(request: SubmitRequest) -> list[dict]:
        submission_id = request.submission_id or f"SUB-{uuid.uuid4().hex[:8].upper()}"
        records = svc().submit(
            submission_id, request.filename, request.text.encode("utf-8"), "text/plain"
        )
        return [rec.model_dump() for rec in records]

    @app.get("/records")
    def list_records(status: str | None = None) -> list[dict]:
        return [rec.model_dump() for rec in svc().list_records(status=status)]

    @app.get("/records/{record_id}")
    def get_record(record_id: str) -> dict:
        return svc().get_record(record_id).model_dump()

    @app.post("/records/{record_id}/decision")
    def decide(record_id: str, request: DecisionRequest) -> dict:
        record = svc().decide(record_id, request.decision, request.actor, request.reason)
        return record.model_dump()

    @app.post("/statements/{record_id}/reconcile", status_code=201)
    def reconcile_statement(record_id: str) -> dict:
        return svc().reconcile(record_id).model_dump()

    @app.get("/reconciliations")
    def list_reconciliations(supplier_id: str | None = None) -> list[dict]:
        return [r.model_dump() for r in svc().list_reconciliations(supplier_id=supplier_id)]

    @app.get("/reconciliations/{reconciliation_id}")
    def get_reconciliation(reconciliation_id: str) -> dict:
        return svc().get_reconciliation(reconciliation_id).model_dump()

    @app.post("/reconciliations/{reconciliation_id}/decision")
    def decide_reconciliation(reconciliation_id: str, request: ReconDecisionRequest) -> dict:
        recon = svc().decide_reconciliation(
            reconciliation_id, request.action, request.actor, request.note
        )
        return recon.model_dump()

    @app.get("/credits")
    def list_credits() -> list[dict]:
        return svc().list_credits()

    @app.post("/credits/{record_id}/apply")
    def apply_credit(record_id: str, request: ApplyCreditRequest) -> dict:
        return svc().apply_credit(record_id, request.actor).model_dump()

    @app.get("/payments/packet")
    def payment_packet() -> dict:
        return svc().payment_packet().model_dump()

    @app.get("/position")
    def ap_position(as_of: str | None = None) -> dict:
        # The as-of is explicit — ageing against a hidden wall
        # clock could not be reproduced at audit. It defaults to
        # today only as a convenience; the report always states
        # the date it was aged at.
        return svc().ap_position(
            as_of or datetime.now(timezone.utc).date().isoformat()
        ).model_dump()

    @app.get("/position/export")
    def position_export(as_of: str | None = None) -> Response:
        as_of = as_of or datetime.now(timezone.utc).date().isoformat()
        key, digest, data = svc().position_export(as_of)
        return Response(
            content=data,
            media_type="text/csv",
            headers={
                "Content-Disposition":
                    f'attachment; filename="ap-position-{as_of}.csv"',
                "X-Export-Key": key,
                "X-Export-Sha256": digest,
            },
        )

    @app.post("/payments/runs", status_code=201)
    def create_payment_run(request: PaymentRunRequest) -> dict:
        run = svc().create_payment_run(
            request.value_date,
            record_ids=request.record_ids,
            currencies=request.currencies,
        )
        return run.model_dump()

    @app.get("/payments/runs")
    def list_payment_runs(status: str | None = None) -> list[dict]:
        return [r.model_dump() for r in svc().list_payment_runs(status=status)]

    @app.get("/payments/runs/{run_id}")
    def get_payment_run(run_id: str) -> dict:
        return svc().get_payment_run(run_id).model_dump()

    @app.post("/payments/runs/{run_id}/decision")
    def decide_payment_run(run_id: str, request: TreasurerDecisionRequest) -> dict:
        run = svc().decide_payment_run(
            run_id, request.action, request.actor, request.reason
        )
        return run.model_dump()

    @app.get("/payments/runs/{run_id}/export")
    def payment_run_export(run_id: str) -> Response:
        data = svc().payment_run_export(run_id)
        return Response(
            content=data,
            media_type="text/csv",
            headers={
                "Content-Disposition":
                    f'attachment; filename="{run_id}-payment-instructions.csv"'
            },
        )

    @app.post("/remittances/{record_id}/confirm", status_code=201)
    def confirm_remittance(record_id: str) -> dict:
        return svc().confirm_remittance(record_id).model_dump()

    @app.get("/confirmations")
    def list_confirmations(run_id: str | None = None) -> list[dict]:
        return [c.model_dump() for c in svc().list_confirmations(run_id=run_id)]

    @app.get("/confirmations/{confirmation_id}")
    def get_confirmation(confirmation_id: str) -> dict:
        return svc().get_confirmation(confirmation_id).model_dump()

    @app.post("/confirmations/{confirmation_id}/decision")
    def decide_confirmation(
        confirmation_id: str, request: ConfirmationDecisionRequest
    ) -> dict:
        confirmation = svc().decide_confirmation(
            confirmation_id, request.action, request.actor, request.note
        )
        return confirmation.model_dump()

    @app.get("/suppliers")
    def suppliers() -> list[dict]:
        reference = svc().store.reference
        return [s.model_dump() for s in reference.suppliers.values()]

    return app


app = create_app()
