"""The internal engine service: the VPC profile's stand-in.

In the VPC profile the document engine answers at an internal
endpoint speaking Reducto's API shape. A customer deployment
puts a licensed self-hosted engine there; this blueprint cannot
ship one, so the demo stack runs THIS service instead — clearly
labelled, backed by the offline text-layer adapter, implementing
the four endpoints the Reducto adapter calls (``/upload``,
``/classify``, ``/split``, ``/extract``) with the documented
response shapes.

It exists so the VPC topology is real in compose — an
internal-only service, no published port, document traffic that
never leaves the stack — not so anyone mistakes it for the
engine. Its extractions are the offline adapter's: deterministic
text-layer reads, not a document AI.
"""

from __future__ import annotations

from fastapi import FastAPI, UploadFile
from pydantic import BaseModel

from .offline_docs import OfflineDocumentAdapter
from .ports import SubDocument, UploadedDocument

_adapter = OfflineDocumentAdapter()
_refs: dict[str, str] = {}  # engine ref (reducto://…) → offline ref

app = FastAPI(title="Reducto AP — internal engine stand-in (offline adapter)")


def _offline_ref(engine_ref: str) -> tuple[str, int | None]:
    base, _, part = engine_ref.partition("#part")
    offline = _refs.get(base, base.replace("reducto://", "offline://", 1))
    return offline, (int(part) if part else None)


def _doc_for(engine_ref: str) -> UploadedDocument | SubDocument:
    offline, part = _offline_ref(engine_ref)
    if part is None:
        return UploadedDocument(engine_ref=offline, filename="", byte_size=0)
    return SubDocument(engine_ref=f"{offline}#part{part}", page_start=1, page_end=1)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "engine": "offline-stand-in"}


def main() -> None:
    """Serve the stand-in engine (compose's internal-only service)."""
    import os

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("ENGINE_PORT", "8080")))


@app.post("/upload")
async def upload(file: UploadFile) -> dict:
    data = await file.read()
    uploaded = _adapter.upload(file.filename or "document", data, file.content_type or "")
    engine_ref = uploaded.engine_ref.replace("offline://", "reducto://", 1)
    _refs[engine_ref] = uploaded.engine_ref
    return {"file_id": engine_ref.removeprefix("reducto://"), "reducto_url": engine_ref}


class InputRequest(BaseModel):
    input: str


@app.post("/classify")
def classify(request: InputRequest) -> dict:
    result = _adapter.classify(_doc_for(request.input))
    return {"result": {"type": result.doc_class, "confidence": result.confidence}}


@app.post("/split")
def split(request: InputRequest) -> dict:
    offline, _ = _offline_ref(request.input)
    subs = _adapter.split(UploadedDocument(engine_ref=offline, filename="", byte_size=0))
    items = []
    for sub in subs:
        part = sub.engine_ref.rsplit("#part", 1)[1]
        items.append({
            "input": f"{request.input.split('#', 1)[0]}#part{part}",
            "pages": [sub.page_start, sub.page_end],
            "name": sub.label,
        })
    return {"result": items}


@app.post("/extract")
def extract(request: InputRequest) -> dict:
    doc = _adapter.extract(_doc_for(request.input))
    result: dict = {name: field.value for name, field in doc.fields.items()}
    result["lines"] = [line.model_dump() for line in doc.lines]
    result["statement_lines"] = [line.model_dump() for line in doc.statement_lines]
    result["remittance_lines"] = [line.model_dump() for line in doc.remittance_lines]
    return {"result": result, "job_id": "offline-stand-in"}
