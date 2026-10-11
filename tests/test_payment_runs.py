"""Payment runs: assembly in code, named refusals, the
treasurer's gate, and the bank export that carries only the
supplier master's details."""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from reducto_ap_agent.api import create_app
from reducto_ap_agent.errors import GateError, PaymentRunError, RecordNotFound
from reducto_ap_agent.graph import ApPipeline
from reducto_ap_agent.object_store import InMemoryObjectStore
from reducto_ap_agent.offline_docs import OfflineDocumentAdapter
from reducto_ap_agent.service import ApService
from reducto_ap_agent.store import InMemoryRecordStore

APPROVER = "Priya Nair (AP lead)"
TREASURER = "Jonas Weber (treasurer)"


def approve(service: ApService, *record_ids: str) -> None:
    for record_id in record_ids:
        service.decide(record_id, "approve", APPROVER, "matched")


def test_run_assembles_approved_records_at_their_open_amounts(seeded):
    approve(seeded, "REC-SUB-001-0", "REC-SUB-007-0", "REC-SUB-017-0")
    seeded.apply_credit("REC-SUB-007-0", "Marta Chen (controller)")
    run = seeded.create_payment_run(
        "2026-10-16",
        record_ids=["REC-SUB-001-0", "REC-SUB-007-0", "REC-SUB-017-0"],
    )
    assert run.status == "AWAITING_TREASURER"
    assert run.value_date == "2026-10-16"
    # The credit note is not a member: its reduction already
    # stands in the invoice's member line — 1400.00 face, 200.00
    # of CN-1001 applied, 1200.00 to pay — beside the untouched
    # 900.00 invoice.
    assert [line.record_id for line in run.lines] == [
        "REC-SUB-001-0", "REC-SUB-017-0",
    ]
    member_line = run.lines[0]
    assert member_line.amount == 1200.00
    assert member_line.gross_amount == 1400.00
    assert [c.credit_number for c in member_line.credits_applied] == ["CN-1001"]
    assert run.totals_by_currency == {"USD": 2100.00}
    assert run.totals_by_currency["USD"] == round(
        sum(line.amount for line in run.lines), 2
    )
    (refusal,) = run.refusals
    assert refusal.record_id == "REC-SUB-007-0"
    assert refusal.code == "CREDIT_NOTE"
    assert "REC-SUB-001-0" in refusal.reason  # where the credit landed
    member = seeded.get_record("REC-SUB-001-0")
    assert member.payment_state == "IN_RUN"
    assert member.payment_run_id == run.run_id
    assert member.status == "APPROVED"  # the approval itself is untouched
    # The credit note itself is held by nothing.
    assert seeded.get_record("REC-SUB-007-0").payment_state is None


def test_auto_assembly_sweeps_the_approved_universe(seeded):
    approve(seeded, "REC-SUB-001-0", "REC-SUB-013-0")
    run = seeded.create_payment_run("2026-10-16")
    assert {line.record_id for line in run.lines} == {
        "REC-SUB-001-0", "REC-SUB-013-0",
    }
    assert run.totals_by_currency == {"USD": 1400.00, "EUR": 1080.00}


def test_run_refuses_fraud_and_exception_by_name(seeded):
    approve(seeded, "REC-SUB-001-0")
    run = seeded.create_payment_run(
        "2026-10-16",
        record_ids=["REC-SUB-001-0", "REC-SUB-008-0", "REC-SUB-003-0"],
    )
    assert [line.record_id for line in run.lines] == ["REC-SUB-001-0"]
    refusals = {r.record_id: r for r in run.refusals}
    assert refusals["REC-SUB-008-0"].code == "FRAUD_HOLD"
    assert "BANK_DETAILS_CHANGED" in refusals["REC-SUB-008-0"].reason
    assert refusals["REC-SUB-003-0"].code == "MATCH_EXCEPTION"
    assert "PRICE_VARIANCE" in refusals["REC-SUB-003-0"].reason
    # Refused records are not held, marked, or moved.
    assert seeded.get_record("REC-SUB-008-0").payment_state is None
    assert seeded.get_record("REC-SUB-003-0").status == "EXCEPTION"


def test_run_refuses_unapproved_and_unpayable_records(seeded):
    approve(seeded, "REC-SUB-001-0")
    run = seeded.create_payment_run(
        "2026-10-16",
        record_ids=[
            "REC-SUB-001-0",   # approved — member
            "REC-SUB-002-0",   # still awaiting the gate
            "REC-SUB-009-0",   # a statement — never payable
            "REC-SUB-010-1",   # a remittance advice — not a payable class
        ],
    )
    refusals = {r.record_id: r.code for r in run.refusals}
    assert refusals == {
        "REC-SUB-002-0": "NOT_APPROVED",
        "REC-SUB-009-0": "NOT_PAYABLE_CLASS",
        "REC-SUB-010-1": "NOT_PAYABLE_CLASS",
    }


def test_second_run_refuses_a_held_member_with_the_holding_run_named(seeded):
    approve(seeded, "REC-SUB-001-0", "REC-SUB-017-0")
    first = seeded.create_payment_run("2026-10-16", record_ids=["REC-SUB-001-0"])
    second = seeded.create_payment_run(
        "2026-10-23", record_ids=["REC-SUB-001-0", "REC-SUB-017-0"]
    )
    (refusal,) = second.refusals
    assert refusal.record_id == "REC-SUB-001-0"
    assert refusal.code == "ALREADY_IN_RUN"
    assert first.run_id in refusal.reason  # the holding run is named
    assert [line.record_id for line in second.lines] == ["REC-SUB-017-0"]


def test_return_frees_every_member_to_join_a_later_run(seeded):
    approve(seeded, "REC-SUB-001-0", "REC-SUB-017-0")
    run = seeded.create_payment_run(
        "2026-10-16", record_ids=["REC-SUB-001-0", "REC-SUB-017-0"]
    )
    returned = seeded.decide_payment_run(
        run.run_id, "return", TREASURER, "wrong value date — rework the batch"
    )
    assert returned.status == "RETURNED"
    for record_id in ("REC-SUB-001-0", "REC-SUB-017-0"):
        record = seeded.get_record(record_id)
        assert record.payment_state is None
        assert record.payment_run_id is None
        assert record.status == "APPROVED"
    later = seeded.create_payment_run(
        "2026-10-23", record_ids=["REC-SUB-001-0", "REC-SUB-017-0"]
    )
    assert len(later.lines) == 2 and later.refusals == []


def test_release_locks_members_as_paid_proposed_forever(seeded):
    approve(seeded, "REC-SUB-001-0", "REC-SUB-017-0")
    run = seeded.create_payment_run("2026-10-16", record_ids=["REC-SUB-001-0"])
    seeded.decide_payment_run(run.run_id, "release", TREASURER, "batch reviewed")
    member = seeded.get_record("REC-SUB-001-0")
    assert member.payment_state == "PAID_PROPOSED"
    assert member.payment_run_id == run.run_id
    assert member.status == "APPROVED"  # still the approver's word underneath
    later = seeded.create_payment_run(
        "2026-10-23", record_ids=["REC-SUB-001-0", "REC-SUB-017-0"]
    )
    (refusal,) = later.refusals
    assert refusal.code == "ALREADY_PAID_PROPOSED"
    assert run.run_id in refusal.reason


def test_treasurer_gate_discipline(seeded):
    approve(seeded, "REC-SUB-001-0")
    run = seeded.create_payment_run("2026-10-16", record_ids=["REC-SUB-001-0"])
    with pytest.raises(GateError):
        seeded.decide_payment_run(run.run_id, "release", "  ")
    with pytest.raises(GateError):
        seeded.decide_payment_run(run.run_id, "maybe", TREASURER)
    with pytest.raises(GateError):
        # Returning without a reason gives the AP team nothing to rework on.
        seeded.decide_payment_run(run.run_id, "return", TREASURER)
    with pytest.raises(GateError):
        seeded.payment_run_export(run.run_id)  # no export before release
    decided = seeded.decide_payment_run(run.run_id, "release", TREASURER, "ok")
    assert decided.decision is not None
    assert decided.decision.actor == TREASURER
    assert [a["event"] for a in decided.audit] == ["assembled", "treasurer_decision"]
    with pytest.raises(GateError):
        seeded.decide_payment_run(run.run_id, "release", TREASURER)


def test_export_carries_the_supplier_masters_bank_details_only(seeded):
    """The fraud-screen property of the export: rows pay the
    account on the supplier master — and the fraud record,
    whose invoice named a different account, is not in the
    file at all, under either account number."""
    approve(seeded, "REC-SUB-001-0", "REC-SUB-017-0")
    run = seeded.create_payment_run(
        "2026-10-16",
        record_ids=[
            "REC-SUB-001-0", "REC-SUB-017-0", "REC-SUB-008-0",
        ],
    )
    seeded.decide_payment_run(run.run_id, "release", TREASURER, "batch reviewed")
    data = seeded.payment_run_export(run.run_id)
    text = data.decode("utf-8")
    reference = seeded.store.reference
    for line in run.lines:
        master = reference.suppliers[line.supplier_id or ""]
        row = next(r for r in text.splitlines() if line.record_id in r)
        assert master.bank_account in row
    # The account the fraudulent invoice asked for, and the
    # master account of the supplier it impersonated (the
    # record never became a member, so neither appears).
    assert "US00 ATTK 9999 0000 1111" not in text
    assert "US00 GRAN 5555 6666 7777" not in text
    assert "REC-SUB-008-0" not in text


def test_export_is_byte_stable_and_matches_its_digest(seeded):
    approve(seeded, "REC-SUB-001-0")
    run = seeded.create_payment_run("2026-10-16", record_ids=["REC-SUB-001-0"])
    released = seeded.decide_payment_run(run.run_id, "release", TREASURER, "ok")
    first = seeded.payment_run_export(run.run_id)
    second = seeded.payment_run_export(run.run_id)
    assert first == second
    assert released.export_digest == hashlib.sha256(first).hexdigest()
    assert released.export_key == (
        f"payment-runs/{run.run_id}/payment-instructions-v1.csv"
    )
    stored = seeded.pipeline.objects.get(released.export_key or "")
    assert stored == first


def test_currency_scope_refuses_out_of_scope_records(seeded):
    approve(seeded, "REC-SUB-001-0", "REC-SUB-013-0")
    run = seeded.create_payment_run(
        "2026-10-16",
        record_ids=["REC-SUB-001-0", "REC-SUB-013-0"],
        currencies=["usd"],
    )
    assert run.currencies == ["USD"]
    assert [line.record_id for line in run.lines] == ["REC-SUB-001-0"]
    (refusal,) = run.refusals
    assert refusal.record_id == "REC-SUB-013-0"
    assert refusal.code == "OUT_OF_CURRENCY_SCOPE"


def test_assembly_with_nothing_eligible_fails_loudly(seeded):
    with pytest.raises(PaymentRunError):
        seeded.create_payment_run("2026-10-16")  # nothing approved yet
    with pytest.raises(PaymentRunError):
        seeded.create_payment_run("2026-10-16", record_ids=["REC-SUB-008-0"])
    with pytest.raises(RecordNotFound):
        seeded.create_payment_run("2026-10-16", record_ids=["REC-NOPE-0"])
    assert seeded.list_payment_runs() == []  # no half-built runs stored


def test_a_run_never_rewrites_the_approval(seeded):
    approve(seeded, "REC-SUB-001-0")
    before = seeded.get_record("REC-SUB-001-0")
    decision_before = before.decision.model_copy() if before.decision else None
    run = seeded.create_payment_run("2026-10-16", record_ids=["REC-SUB-001-0"])
    seeded.decide_payment_run(run.run_id, "release", TREASURER, "ok")
    after = seeded.get_record("REC-SUB-001-0")
    assert after.status == "APPROVED"
    assert after.decision == decision_before
    events = [a["event"] for a in after.audit]
    assert "payment_run_joined" in events
    assert "payment_run_paid_proposed" in events


# -- the HTTP surface ---------------------------------------------------


@pytest.fixture()
def client(world):
    store = InMemoryRecordStore()
    store.reference.load(world.suppliers, world.purchase_orders, world.receipts)
    pipeline = ApPipeline(
        documents=OfflineDocumentAdapter(), store=store,
        objects=InMemoryObjectStore(), checkpointer=MemorySaver(),
    )
    service = ApService(pipeline=pipeline, store=store)
    with TestClient(create_app(service)) as test_client:
        yield test_client


def _approve_over_http(client, record_id: str) -> None:
    response = client.post(
        f"/records/{record_id}/decision",
        json={"decision": "approve", "actor": APPROVER, "reason": "matched"},
    )
    assert response.status_code == 200


def test_payment_run_over_http(client):
    assert client.post("/demo/seed").status_code == 201
    _approve_over_http(client, "REC-SUB-001-0")
    _approve_over_http(client, "REC-SUB-007-0")
    applied = client.post(
        "/credits/REC-SUB-007-0/apply",
        json={"actor": "Marta Chen (controller)"},
    )
    assert applied.status_code == 200
    assert applied.json()["credit_state"] == "APPLIED"

    created = client.post(
        "/payments/runs",
        json={
            "value_date": "2026-10-16",
            "record_ids": ["REC-SUB-001-0", "REC-SUB-007-0", "REC-SUB-008-0"],
        },
    )
    assert created.status_code == 201
    run = created.json()
    assert run["status"] == "AWAITING_TREASURER"
    # One member, at its net open amount: 1400.00 − CN-1001's 200.00.
    assert run["totals_by_currency"] == {"USD": 1200.00}
    assert run["lines"][0]["amount"] == 1200.00
    assert run["lines"][0]["credits_applied"][0]["credit_number"] == "CN-1001"
    refusals = {r["record_id"]: r["code"] for r in run["refusals"]}
    assert refusals == {"REC-SUB-007-0": "CREDIT_NOTE", "REC-SUB-008-0": "FRAUD_HOLD"}

    listed = client.get("/payments/runs")
    assert [r["run_id"] for r in listed.json()] == [run["run_id"]]

    blocked = client.get(f"/payments/runs/{run['run_id']}/export")
    assert blocked.status_code == 409  # nothing to download before release

    decided = client.post(
        f"/payments/runs/{run['run_id']}/decision",
        json={"action": "release", "actor": TREASURER, "reason": "reviewed"},
    )
    assert decided.status_code == 200
    assert decided.json()["status"] == "RELEASED"
    assert decided.json()["export_digest"]

    download = client.get(f"/payments/runs/{run['run_id']}/export")
    assert download.status_code == 200
    assert "attachment" in download.headers["content-disposition"]
    assert "US00 BPAY 1111 2222 3333" in download.text  # the master account
    assert "ATTK" not in download.text
    again = client.get(f"/payments/runs/{run['run_id']}/export")
    assert again.content == download.content


def test_payment_run_errors_over_http(client):
    client.post("/demo/seed")
    assert client.get("/payments/runs/RUN-NOPE").status_code == 404
    empty = client.post("/payments/runs", json={"value_date": "2026-10-16"})
    assert empty.status_code == 422  # nothing approved — assembly fails loudly

    _approve_over_http(client, "REC-SUB-001-0")
    run = client.post(
        "/payments/runs",
        json={"value_date": "2026-10-16", "record_ids": ["REC-SUB-001-0"]},
    ).json()
    no_reason = client.post(
        f"/payments/runs/{run['run_id']}/decision",
        json={"action": "return", "actor": TREASURER},
    )
    assert no_reason.status_code == 409
    returned = client.post(
        f"/payments/runs/{run['run_id']}/decision",
        json={"action": "return", "actor": TREASURER, "reason": "rework"},
    )
    assert returned.status_code == 200
    assert returned.json()["status"] == "RETURNED"
