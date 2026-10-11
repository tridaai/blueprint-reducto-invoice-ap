"""Remittance confirmation: the bank's answer to a released
run, reconciled line by line — exactly, never within a
tolerance — behind a controller's gate, with unmatched
advices held rather than guessed."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from reducto_ap_agent.api import create_app
from reducto_ap_agent.datasets import remittance_advice_doc
from reducto_ap_agent.errors import ConfirmationError, GateError, RecordNotFound
from reducto_ap_agent.graph import ApPipeline
from reducto_ap_agent.object_store import InMemoryObjectStore
from reducto_ap_agent.offline_docs import OfflineDocumentAdapter
from reducto_ap_agent.schemas import PaymentConfirmation
from reducto_ap_agent.service import ApService
from reducto_ap_agent.store import InMemoryRecordStore

APPROVER = "Priya Nair (AP lead)"
TREASURER = "Jonas Weber (treasurer)"
CONTROLLER = "Marta Chen (controller)"

MEMBERS = ["REC-SUB-002-0", "REC-SUB-010-0", "REC-SUB-014-0", "REC-SUB-018-0"]

ADVICE_ROWS = [
    ("INV-1002", 1010.00, "USD", "2026-10-30", "BNK-2026-4102"),  # exact
    ("INV-1010", 550.00, "USD", "2026-10-30", "BNK-2026-4110"),   # short 50.00
    ("INV-1014", 325.00, "USD", "2026-10-30", "BNK-2026-4114"),   # over 25.00
    ("INV-9999", 123.00, "USD", "2026-10-30", "BNK-2026-4199"),   # not a member
    # INV-2002 (REC-SUB-018-0, 400.00) has no line — missing.
]


def approve(service: ApService, *record_ids: str) -> None:
    for record_id in record_ids:
        service.decide(record_id, "approve", APPROVER, "matched")


def released_run(service: ApService, record_ids=MEMBERS, value_date="2026-10-30"):
    approve(service, *record_ids)
    run = service.create_payment_run(value_date, record_ids=list(record_ids))
    service.decide_payment_run(run.run_id, "release", TREASURER, "batch reviewed")
    return service.get_payment_run(run.run_id)


def submit_advice(service: ApService, run_id, rows=ADVICE_ROWS, sid="SUB-900"):
    text = remittance_advice_doc(run_id, rows)
    (record,) = service.submit(
        sid, f"{sid.lower()}.pdf", text.encode("utf-8"), "text/plain"
    )
    return record


def test_advice_lines_are_captured_at_intake(seeded):
    record = submit_advice(seeded, "RUN-2099-01-99")
    assert record.doc_class == "remittance_advice"
    assert record.status == "RECEIVED"
    assert record.extraction is not None
    lines = record.extraction.remittance_lines
    assert [line.invoice_number for line in lines] == [
        "INV-1002", "INV-1010", "INV-1014", "INV-9999",
    ]
    assert lines[1].amount_paid == 550.00
    assert lines[1].bank_reference == "BNK-2026-4110"
    assert record.extraction.value("payment_run_id") == "RUN-2099-01-99"
    events = [a["event"] for a in record.audit]
    assert "remittance_captured" in events
    # The v1 world's advice (bundled with an invoice) is captured
    # too — it simply carries no paid lines.
    bundled = seeded.get_record("REC-SUB-010-1")
    assert bundled.extraction is not None
    assert bundled.extraction.remittance_lines == []


def test_confirmation_classifies_every_class_exactly(seeded):
    run = released_run(seeded)
    advice = submit_advice(seeded, run.run_id)
    conf = seeded.confirm_remittance(advice.record_id)

    assert conf.status == "AWAITING_CONTROLLER"
    assert conf.run_id == run.run_id
    by_number = {line.invoice_number: line for line in conf.lines}
    assert by_number["INV-1002"].classification == "paid_as_proposed"
    assert by_number["INV-1002"].difference == 0.0
    short = by_number["INV-1010"]
    assert short.classification == "short_paid"
    assert short.proposed_amount == 600.00
    assert short.paid_amount == 550.00
    assert short.difference == -50.00
    over = by_number["INV-1014"]
    assert over.classification == "over_paid"
    assert over.difference == 25.00
    missing = by_number["INV-2002"]
    assert missing.classification == "missing"
    assert missing.proposed_amount == 400.00
    assert missing.paid_amount is None
    assert missing.member_record_id == "REC-SUB-018-0"
    unexpected = by_number["INV-9999"]
    assert unexpected.classification == "unexpected_line"
    assert unexpected.member_record_id is None

    assert conf.counts == {
        "paid_as_proposed": 1, "short_paid": 1, "over_paid": 1,
        "missing": 1, "unexpected_line": 1,
    }
    assert conf.totals_tie
    (totals,) = conf.totals
    assert totals.currency == "USD"
    assert totals.run_total == 2310.00  # 1010 + 600 + 300 + 400
    assert totals.confirmed_proposed == 1010.00
    assert totals.confirmed_paid == 1010.00
    assert totals.short_proposed == 600.00
    assert totals.short_paid == 550.00
    assert totals.shortfall == 50.00
    assert totals.over_proposed == 300.00
    assert totals.over_paid == 325.00
    assert totals.excess == 25.00
    assert totals.missing_proposed == 400.00
    # The unexpected line's money is reported, never absorbed.
    assert totals.unexpected_paid == 123.00


def test_the_exact_match_rule_has_no_tolerance(seeded):
    """One cent under the proposal is well inside the match
    policy's total tolerance (1% + 1.00) — and a confirmation
    break anyway: money that left the bank matches the
    instruction to the cent, or a human explains it."""
    run = released_run(seeded, record_ids=["REC-SUB-002-0"])
    advice = submit_advice(seeded, run.run_id, rows=[
        ("INV-1002", 1009.99, "USD", "2026-10-30", "BNK-2026-4200"),
    ])
    conf = seeded.confirm_remittance(advice.record_id)
    (line,) = conf.lines
    assert line.classification == "short_paid"
    assert line.difference == -0.01
    assert conf.totals[0].shortfall == 0.01


def test_member_overlays_land_beside_the_proposal(seeded):
    run = released_run(seeded)
    advice = submit_advice(seeded, run.run_id)
    conf = seeded.confirm_remittance(advice.record_id)
    expected = {
        "REC-SUB-002-0": "CONFIRMED",
        "REC-SUB-010-0": "SHORT_PAID",
        "REC-SUB-014-0": "OVER_PAID",
        "REC-SUB-018-0": "MISSING",
    }
    for record_id, state in expected.items():
        member = seeded.get_record(record_id)
        assert member.payment_confirmation == state
        assert member.payment_confirmation_id == conf.confirmation_id
        # The proposal underneath is untouched by the answer.
        assert member.payment_state == "PAID_PROPOSED"
        assert member.status == "APPROVED"
        assert member.decision is not None and member.decision.decision == "approve"
        events = [a["event"] for a in member.audit]
        assert "payment_confirmation_recorded" in events


def test_confirmation_leaves_the_run_and_its_export_untouched(seeded):
    run = released_run(seeded)
    export_before = seeded.payment_run_export(run.run_id)
    decision_before = run.decision.model_copy() if run.decision else None
    advice = submit_advice(seeded, run.run_id)
    conf = seeded.confirm_remittance(advice.record_id)
    seeded.decide_confirmation(
        conf.confirmation_id, "accept", CONTROLLER, "breaks noted"
    )
    after = seeded.get_payment_run(run.run_id)
    assert after.status == "RELEASED"
    assert after.decision == decision_before
    assert after.export_digest == run.export_digest
    assert seeded.payment_run_export(run.run_id) == export_before


def test_advice_naming_no_run_is_held_unmatched(seeded):
    conf = seeded.confirm_remittance("REC-SUB-022-0")
    assert conf.status == "UNMATCHED"
    assert conf.run_id is None
    assert "names no payment run" in conf.unmatched_reason
    assert conf.lines == []
    assert all(n == 0 for n in conf.counts.values())
    # Nothing was classified onto anyone.
    assert all(
        r.payment_confirmation is None for r in seeded.list_records()
    )
    # And there is nothing for a controller to decide.
    with pytest.raises(GateError):
        seeded.decide_confirmation(conf.confirmation_id, "accept", CONTROLLER)


def test_advice_naming_an_unknown_run_is_held_unmatched(seeded):
    conf = seeded.confirm_remittance("REC-SUB-023-0")
    assert conf.status == "UNMATCHED"
    assert "RUN-2099-01-99" in conf.unmatched_reason
    assert "not on file" in conf.unmatched_reason


def test_advice_naming_an_unreleased_run_is_held_unmatched(seeded):
    approve(seeded, "REC-SUB-002-0")
    run = seeded.create_payment_run(
        "2026-10-30", record_ids=["REC-SUB-002-0"]
    )
    advice = submit_advice(seeded, run.run_id, rows=[
        ("INV-1002", 1010.00, "USD", "2026-10-30", "BNK-2026-4300"),
    ])
    conf = seeded.confirm_remittance(advice.record_id)
    assert conf.status == "UNMATCHED"
    assert "AWAITING_TREASURER" in conf.unmatched_reason
    assert seeded.get_record("REC-SUB-002-0").payment_confirmation is None


def test_currency_mismatch_is_short_and_the_figure_stays_on_the_line(seeded):
    run = released_run(seeded, record_ids=["REC-SUB-013-0"])  # EUR 1080.00
    advice = submit_advice(seeded, run.run_id, rows=[
        ("INV-1013", 1080.00, "USD", "2026-10-30", "BNK-2026-4400"),
    ])
    conf = seeded.confirm_remittance(advice.record_id)
    (line,) = conf.lines
    assert line.classification == "short_paid"
    assert line.difference is None  # not comparable across currencies
    assert line.paid_amount == 1080.00  # rides the line, in USD
    assert "Currency differs" in line.note
    (totals,) = conf.totals
    assert totals.currency == "EUR"
    assert totals.shortfall == 1080.00  # the full proposed amount
    assert totals.short_paid == 0.0     # nothing arrived in EUR
    assert conf.totals_tie


def test_a_second_line_for_one_member_is_unexpected(seeded):
    run = released_run(seeded, record_ids=["REC-SUB-002-0"])
    advice = submit_advice(seeded, run.run_id, rows=[
        ("INV-1002", 1010.00, "USD", "2026-10-30", "BNK-2026-4501"),
        ("INV-1002", 1010.00, "USD", "2026-10-30", "BNK-2026-4502"),
    ])
    conf = seeded.confirm_remittance(advice.record_id)
    assert [line.classification for line in conf.lines] == [
        "paid_as_proposed", "unexpected_line",
    ]
    assert conf.lines[1].member_record_id == "REC-SUB-002-0"
    assert conf.counts["unexpected_line"] == 1
    assert conf.totals_tie


def test_controller_gate_discipline(seeded):
    run = released_run(seeded)
    advice = submit_advice(seeded, run.run_id)
    conf = seeded.confirm_remittance(advice.record_id)
    with pytest.raises(GateError):
        seeded.decide_confirmation(conf.confirmation_id, "accept", "  ")
    with pytest.raises(GateError):
        seeded.decide_confirmation(conf.confirmation_id, "maybe", CONTROLLER)
    with pytest.raises(GateError):
        # Flagging back without a note gives the AP team nothing.
        seeded.decide_confirmation(conf.confirmation_id, "flag_back", CONTROLLER)
    with pytest.raises(RecordNotFound):
        seeded.decide_confirmation("CONF-NOPE", "accept", CONTROLLER)
    flagged = seeded.decide_confirmation(
        conf.confirmation_id, "flag_back", CONTROLLER,
        "INV-1010 short by 50.00 — take it to the bank",
    )
    assert flagged.status == "FLAGGED_BACK"
    assert flagged.decision is not None
    assert flagged.decision.actor == CONTROLLER
    with pytest.raises(GateError):
        seeded.decide_confirmation(conf.confirmation_id, "accept", CONTROLLER)
    # Confirmation is asked of advices, not of anything else.
    with pytest.raises(ConfirmationError):
        seeded.confirm_remittance("REC-SUB-020-0")  # a statement
    with pytest.raises(RecordNotFound):
        seeded.confirm_remittance("REC-NOPE-0")


def test_store_confirmation_round_trip():
    store = InMemoryRecordStore()
    conf = PaymentConfirmation(
        confirmation_id="CONF-REC-1", advice_record_id="REC-1",
        submission_id="SUB-1", run_id="RUN-1",
    )
    store.save_confirmation(conf)
    held = PaymentConfirmation(
        confirmation_id="CONF-REC-2", advice_record_id="REC-2",
        submission_id="SUB-2", run_id=None, status="UNMATCHED",
        unmatched_reason="the advice names no payment run",
    )
    store.save_confirmation(held)
    assert store.get_confirmation("CONF-REC-1") == conf
    assert store.get_confirmation("CONF-MISSING") is None
    assert [c.confirmation_id for c in store.list_confirmations()] == [
        "CONF-REC-1", "CONF-REC-2",
    ]
    assert [
        c.confirmation_id for c in store.list_confirmations(run_id="RUN-1")
    ] == ["CONF-REC-1"]


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


def test_confirmation_over_http(client):
    assert client.post("/demo/seed").status_code == 201
    for record_id in MEMBERS:
        response = client.post(
            f"/records/{record_id}/decision",
            json={"decision": "approve", "actor": APPROVER, "reason": "matched"},
        )
        assert response.status_code == 200
    run = client.post(
        "/payments/runs",
        json={"value_date": "2026-10-30", "record_ids": MEMBERS},
    ).json()
    client.post(
        f"/payments/runs/{run['run_id']}/decision",
        json={"action": "release", "actor": TREASURER, "reason": "reviewed"},
    )
    advice_text = remittance_advice_doc(run["run_id"], ADVICE_ROWS)
    submitted = client.post("/submissions", json={
        "text": advice_text, "filename": "advice.pdf",
        "submission_id": "SUB-HTTP-ADV",
    })
    assert submitted.status_code == 201
    advice_id = submitted.json()[0]["record_id"]

    created = client.post(f"/remittances/{advice_id}/confirm")
    assert created.status_code == 201
    conf = created.json()
    assert conf["status"] == "AWAITING_CONTROLLER"
    assert conf["counts"]["short_paid"] == 1
    assert conf["totals_tie"] is True

    listed = client.get(f"/confirmations?run_id={run['run_id']}")
    assert [c["confirmation_id"] for c in listed.json()] == [conf["confirmation_id"]]

    decided = client.post(
        f"/confirmations/{conf['confirmation_id']}/decision",
        json={"action": "accept", "actor": CONTROLLER, "note": "breaks noted"},
    )
    assert decided.status_code == 200
    assert decided.json()["status"] == "ACCEPTED"

    assert client.get("/confirmations/CONF-NOPE").status_code == 404
    not_advice = client.post("/remittances/REC-SUB-020-0/confirm")
    assert not_advice.status_code == 422
