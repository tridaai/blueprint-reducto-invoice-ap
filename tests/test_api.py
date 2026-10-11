"""The HTTP surface: health, readiness honesty, the gate flow."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from reducto_ap_agent.api import create_app
from reducto_ap_agent.datasets import build_world
from reducto_ap_agent.graph import ApPipeline
from reducto_ap_agent.object_store import InMemoryObjectStore
from reducto_ap_agent.offline_docs import OfflineDocumentAdapter
from reducto_ap_agent.service import ApService
from reducto_ap_agent.store import InMemoryRecordStore


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    world = build_world()
    store = InMemoryRecordStore()
    store.reference.load(world.suppliers, world.purchase_orders, world.receipts)
    pipeline = ApPipeline(
        documents=OfflineDocumentAdapter(), store=store,
        objects=InMemoryObjectStore(), checkpointer=MemorySaver(),
    )
    service = ApService(pipeline=pipeline, store=store)
    with TestClient(create_app(service)) as test_client:
        yield test_client


def test_health_and_readiness_name_the_wiring(client):
    assert client.get("/health").json() == {"status": "ok"}
    readiness = client.get("/readiness").json()
    assert readiness["deployment_profile"] in ("cloud", "vpc")
    assert readiness["store"] == "memory"  # honest: the double is live
    assert readiness["checkpointer"] == "memory"


def test_console_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Reducto Invoice AP" in response.text
    assert "View all blueprints" in response.text


def test_seed_decide_packet_flow(client):
    seeded = client.post("/demo/seed")
    assert seeded.status_code == 201
    records = client.get("/records").json()
    assert len(records) == 24
    fraud = [r for r in records if r["status"] == "FRAUD_REVIEW"]
    assert len(fraud) == 1 and fraud[0]["record_id"] == "REC-SUB-008-0"

    decided = client.post(
        "/records/REC-SUB-001-0/decision",
        json={"decision": "approve", "actor": "Priya Nair (AP lead)", "reason": "matched"},
    )
    assert decided.status_code == 200
    assert decided.json()["status"] == "APPROVED"

    blocked = client.post(
        "/records/REC-SUB-008-0/decision",
        json={"decision": "approve", "actor": "Priya Nair (AP lead)"},
    )
    assert blocked.status_code == 409  # the gate cannot clear a fraud hold

    packet = client.get("/payments/packet").json()
    assert packet["totals_by_currency"]["USD"] == 1400.00
    assert packet["lines"][0]["invoice_number"] == "INV-1001"


def test_unknown_record_is_404(client):
    assert client.get("/records/REC-NOPE-0").status_code == 404


def test_request_id_header_is_returned(client):
    response = client.get("/health", headers={"X-Request-ID": "trace-123"})
    assert response.headers["X-Request-ID"] == "trace-123"
