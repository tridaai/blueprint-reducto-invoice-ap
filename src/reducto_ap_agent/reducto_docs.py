"""The Reducto adapter: the document engine, for real.

Implements :class:`~.ports.DocumentPort` against Reducto's
public API, as documented at docs.reducto.ai:

- ``POST /upload`` (multipart) → a ``reducto://`` document URL
- ``POST /classify`` → the document's type
- ``POST /split`` → the bundle's sub-documents, by page range
- ``POST /extract`` → schema-shaped fields, with citations
  (bounding boxes) requested on every call

The same adapter serves both deployment profiles — only the
base URL changes (hosted API in ``cloud``, an internal endpoint
in ``vpc``); see :mod:`.profiles`. All response-shape mapping
lives in this module, so an API revision is a one-file change.

Failure posture, by design: a missing API key raises
:class:`DocumentNotConfigured` at construction; a network or
HTTP failure raises :class:`DocumentBackendUnavailable`. The
adapter NEVER substitutes another engine's results — a document
the engine could not read is an error the operator sees, not a
quiet local guess.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .errors import DocumentBackendUnavailable, DocumentNotConfigured
from .ports import Classification, SubDocument, UploadedDocument
from .schemas import (
    Citation,
    ExtractedDocument,
    ExtractedField,
    LineItem,
    RemittanceLine,
    StatementLine,
)

log = logging.getLogger(__name__)

# The extraction schema sent with every extract call. Field
# names are the pipeline's vocabulary (schemas.py); the engine
# is asked for exactly these, nothing more. One schema covers
# payable documents (fields + lines), statements (the statement
# fields + open-item lines), and remittance advices (the run
# reference + paid lines) — the classifier has already said
# which shape the document is, and the fields that don't apply
# simply come back empty.
INVOICE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "supplier_name": {"type": "string"},
        "invoice_number": {"type": "string"},
        "invoice_date": {"type": "string"},
        "due_date": {"type": "string"},
        "po_reference": {"type": "string"},
        "currency": {"type": "string"},
        "subtotal": {"type": "number"},
        "tax": {"type": "number"},
        "total": {"type": "number"},
        "bank_account": {"type": "string"},
        "statement_date": {"type": "string"},
        "period": {"type": "string"},
        "payment_run_id": {"type": "string"},
        "advice_date": {"type": "string"},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sku": {"type": "string"},
                    "description": {"type": "string"},
                    "quantity": {"type": "number"},
                    "unit_price": {"type": "number"},
                    "amount": {"type": "number"},
                },
            },
        },
        "statement_lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "invoice_number": {"type": "string"},
                    "invoice_date": {"type": "string"},
                    "amount": {"type": "number"},
                    "currency": {"type": "string"},
                },
            },
        },
        "remittance_lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "invoice_number": {"type": "string"},
                    "amount_paid": {"type": "number"},
                    "currency": {"type": "string"},
                    "value_date": {"type": "string"},
                    "bank_reference": {"type": "string"},
                    "payment_run_id": {"type": "string"},
                },
            },
        },
    },
}


def _bbox_of(node: Any) -> tuple[float, float, float, float] | None:
    """Pull a bounding box out of an engine citation node, in the
    shapes the API returns (``bbox`` list, or citation objects)."""
    if isinstance(node, dict):
        box = node.get("bbox") or node.get("bounding_box")
        if isinstance(box, (list, tuple)) and len(box) == 4:
            return tuple(float(v) for v in box)  # type: ignore[return-value]
        for key in ("citations", "citation"):
            inner = node.get(key)
            if isinstance(inner, list) and inner:
                found = _bbox_of(inner[0])
                if found:
                    return found
            elif isinstance(inner, dict):
                found = _bbox_of(inner)
                if found:
                    return found
    return None


def _page_of(node: Any) -> int:
    if isinstance(node, dict):
        for key in ("page", "page_number"):
            value = node.get(key)
            if isinstance(value, int):
                return value
        for key in ("citations", "citation"):
            inner = node.get(key)
            if isinstance(inner, list) and inner and isinstance(inner[0], dict):
                page = _page_of(inner[0])
                if page:
                    return page
    return 1


class ReductoDocumentAdapter:
    """DocumentPort over Reducto's HTTP API (cloud or in-VPC)."""

    name = "reducto"

    def __init__(
        self,
        api_key: str | None,
        base_url: str,
        *,
        timeout_seconds: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise DocumentNotConfigured(
                "DOCUMENT_BACKEND=reducto but REDUCTO_API_KEY is not set. "
                "Set the key, or select the labelled offline backend "
                "(DOCUMENT_BACKEND=offline) for tests and smoke runs."
            )
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
            transport=transport,
        )

    # -- transport -------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise DocumentBackendUnavailable(
                f"Reducto at {self._base_url} could not be reached: {exc}"
            ) from exc
        if response.status_code >= 400:
            raise DocumentBackendUnavailable(
                f"Reducto {method} {path} failed: HTTP {response.status_code} "
                f"— {response.text[:300]}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise DocumentBackendUnavailable(
                f"Reducto {method} {path} returned a non-JSON body"
            ) from exc
        return body if isinstance(body, dict) else {"result": body}

    # -- port ------------------------------------------------------

    def upload(self, filename: str, data: bytes, content_type: str) -> UploadedDocument:
        body = self._request(
            "POST",
            "/upload",
            files={"file": (filename, data, content_type or "application/octet-stream")},
        )
        ref = (
            body.get("reducto_url")
            or body.get("url")
            or (f"reducto://{body['file_id']}" if body.get("file_id") else None)
        )
        if not ref:
            raise DocumentBackendUnavailable(
                "Reducto /upload response carried no document reference"
            )
        return UploadedDocument(engine_ref=str(ref), filename=filename, byte_size=len(data))

    def classify(self, doc: UploadedDocument | SubDocument) -> Classification:
        body = self._request("POST", "/classify", json={"input": doc.engine_ref})
        result = body.get("result") or {}
        doc_class = (
            result.get("type") or result.get("doc_type") or result.get("classification")
            if isinstance(result, dict)
            else None
        )
        confidence = result.get("confidence") if isinstance(result, dict) else None
        return Classification(
            doc_class=str(doc_class or "unknown"),
            confidence=float(confidence) if isinstance(confidence, (int, float)) else None,
        )

    def split(self, doc: UploadedDocument) -> list[SubDocument]:
        body = self._request("POST", "/split", json={"input": doc.engine_ref})
        result = body.get("result") or []
        subs: list[SubDocument] = []
        if isinstance(result, list):
            for item in result:
                if not isinstance(item, dict):
                    continue
                pages = item.get("pages") or item.get("page_range") or []
                start = int(pages[0]) if pages else int(item.get("start_page", 1))
                end = int(pages[-1]) if pages else int(item.get("end_page", start))
                subs.append(
                    SubDocument(
                        engine_ref=str(item.get("url") or item.get("input") or doc.engine_ref),
                        page_start=start,
                        page_end=end,
                        label=str(item.get("name") or item.get("type") or ""),
                    )
                )
        if not subs:
            subs = [SubDocument(engine_ref=doc.engine_ref, page_start=1, page_end=1)]
        return subs

    def extract(self, doc: UploadedDocument | SubDocument) -> ExtractedDocument:
        body = self._request(
            "POST",
            "/extract",
            json={
                "input": doc.engine_ref,
                "instructions": {"schema": INVOICE_SCHEMA},
                "settings": {
                    "citations": {"enabled": True, "numerical_confidence": True},
                },
            },
        )
        result = body.get("result")
        if isinstance(result, list):  # chunked responses: one dict per chunk
            merged: dict[str, Any] = {}
            for chunk in result:
                if isinstance(chunk, dict):
                    merged.update(chunk)
            result = merged
        if not isinstance(result, dict):
            raise DocumentBackendUnavailable("Reducto /extract returned no result object")
        fields: dict[str, ExtractedField] = {}
        lines: list[LineItem] = []
        statement_lines: list[StatementLine] = []
        remittance_lines: list[RemittanceLine] = []
        for key, raw in result.items():
            if key == "lines" and isinstance(raw, list):
                for row in raw:
                    if isinstance(row, dict):
                        lines.append(
                            LineItem(
                                sku=str(row.get("sku") or ""),
                                description=str(row.get("description") or ""),
                                quantity=float(row.get("quantity") or 0),
                                unit_price=float(row.get("unit_price") or 0),
                                amount=float(row.get("amount") or 0),
                            )
                        )
                continue
            if key == "statement_lines" and isinstance(raw, list):
                for row in raw:
                    if isinstance(row, dict):
                        statement_lines.append(
                            StatementLine(
                                invoice_number=str(row.get("invoice_number") or ""),
                                invoice_date=str(row.get("invoice_date") or ""),
                                amount=float(row.get("amount") or 0),
                                currency=str(row.get("currency") or "USD"),
                            )
                        )
                continue
            if key == "remittance_lines" and isinstance(raw, list):
                for row in raw:
                    if isinstance(row, dict):
                        remittance_lines.append(
                            RemittanceLine(
                                invoice_number=str(row.get("invoice_number") or ""),
                                amount_paid=float(row.get("amount_paid") or 0),
                                currency=str(row.get("currency") or "USD"),
                                value_date=str(row.get("value_date") or ""),
                                bank_reference=str(row.get("bank_reference") or ""),
                                payment_run_id=str(row.get("payment_run_id") or ""),
                            )
                        )
                continue
            value, citation, confidence = raw, None, None
            if isinstance(raw, dict) and "value" in raw:
                value = raw.get("value")
                box = _bbox_of(raw)
                if box:
                    citation = Citation(page=_page_of(raw), bbox=box, source="engine")
                conf = raw.get("confidence")
                confidence = float(conf) if isinstance(conf, (int, float)) else None
            fields[key] = ExtractedField(value=value, citation=citation, confidence=confidence)
        return ExtractedDocument(
            fields=fields,
            lines=lines,
            statement_lines=statement_lines,
            remittance_lines=remittance_lines,
            currency=str(fields["currency"].value) if "currency" in fields and fields["currency"].value else "USD",
        )
