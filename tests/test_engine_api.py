"""The VPC stand-in engine: the Reducto API shape, offline inside.

Pins the contract the Reducto adapter depends on — upload
returns a reference, classify/split/extract answer in the
documented shapes — served by the engine the compose VPC stack
runs internally.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from reducto_ap_agent.datasets import build_world
from reducto_ap_agent.engine_api import app


@pytest.fixture()
def engine():
    with TestClient(app) as client:
        yield client


def test_engine_round_trip(engine):
    world = build_world()
    bundle = next(s for s in world.submissions if s.submission_id == "SUB-010")
    uploaded = engine.post(
        "/upload",
        files={"file": (bundle.filename, bundle.text.encode(), "text/plain")},
    )
    assert uploaded.status_code == 200
    ref = uploaded.json()["reducto_url"]

    split = engine.post("/split", json={"input": ref}).json()
    assert len(split["result"]) == 2

    first = split["result"][0]["input"]
    classified = engine.post("/classify", json={"input": first}).json()
    assert classified["result"]["type"] == "invoice"

    extracted = engine.post("/extract", json={"input": first}).json()
    result = extracted["result"]
    assert result["invoice_number"] == "INV-1010"
    assert result["total"] == 600.00
    assert len(result["lines"]) == 1


def test_engine_health_names_the_stand_in(engine):
    body = engine.get("/health").json()
    assert body["engine"] == "offline-stand-in"
