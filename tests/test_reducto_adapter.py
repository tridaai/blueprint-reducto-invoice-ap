"""The Reducto adapter against a stubbed transport.

No live call is made anywhere in this suite: the adapter's
request shapes and response mapping are pinned against
httpx.MockTransport, and its failure posture (never fall back,
never guess) is asserted directly.
"""

from __future__ import annotations

import json

import httpx
import pytest

from reducto_ap_agent.errors import (
    DocumentBackendUnavailable,
    DocumentNotConfigured,
)
from reducto_ap_agent.reducto_docs import ReductoDocumentAdapter


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _ok_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/upload":
        return httpx.Response(200, json={"file_id": "abc123"})
    if path == "/classify":
        return httpx.Response(200, json={"result": {"type": "invoice", "confidence": 0.98}})
    if path == "/split":
        return httpx.Response(200, json={"result": [
            {"input": "reducto://abc123", "pages": [1, 1], "name": "Invoice"},
        ]})
    if path == "/extract":
        body = json.loads(request.content)
        assert body["settings"]["citations"]["enabled"] is True
        assert "schema" in body["instructions"]
        return httpx.Response(200, json={"result": {
            "supplier_name": "Brightpath Components Inc",
            "invoice_number": {
                "value": "INV-1001",
                "bbox": [40, 100, 300, 130],
                "page": 1,
                "confidence": 0.97,
            },
            "total": 1400.0,
            "currency": "USD",
            "lines": [
                {"sku": "SKU-A1", "description": "Industrial valves",
                 "quantity": 10, "unit_price": 120.0, "amount": 1200.0},
            ],
        }})
    return httpx.Response(404, json={"detail": "nope"})


def test_adapter_full_flow_maps_fields_and_citations():
    adapter = ReductoDocumentAdapter(
        "test-key", "https://platform.reducto.ai", transport=_transport(_ok_handler)
    )
    uploaded = adapter.upload("inv.pdf", b"%PDF-fake", "application/pdf")
    assert uploaded.engine_ref == "reducto://abc123"
    assert adapter.classify(uploaded).doc_class == "invoice"
    subs = adapter.split(uploaded)
    assert len(subs) == 1 and subs[0].page_start == 1
    extraction = adapter.extract(subs[0])
    assert extraction.value("invoice_number") == "INV-1001"
    field = extraction.fields["invoice_number"]
    assert field.citation is not None
    assert field.citation.bbox == (40.0, 100.0, 300.0, 130.0)
    assert field.citation.source == "engine"
    assert extraction.lines[0].sku == "SKU-A1"


def test_missing_key_is_not_configured_never_a_fallback():
    with pytest.raises(DocumentNotConfigured):
        ReductoDocumentAdapter(None, "https://platform.reducto.ai")
    with pytest.raises(DocumentNotConfigured):
        ReductoDocumentAdapter("", "https://platform.reducto.ai")


def test_http_failure_is_unavailable_never_a_fallback():
    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="engine down")

    adapter = ReductoDocumentAdapter(
        "test-key", "https://platform.reducto.ai", transport=_transport(boom)
    )
    with pytest.raises(DocumentBackendUnavailable):
        adapter.upload("inv.pdf", b"%PDF-fake", "application/pdf")


def test_network_failure_is_unavailable():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    adapter = ReductoDocumentAdapter(
        "test-key", "http://reducto-engine:8080", transport=_transport(boom)
    )
    with pytest.raises(DocumentBackendUnavailable):
        adapter.classify(type("D", (), {"engine_ref": "reducto://x"})())
