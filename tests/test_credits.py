"""Credit notes: application in code against the invoice the
document names — the netting, the ledger, and every refusal
with its reason. Synthetic credit documents are submitted
through the real pipeline where a scenario needs one the
planted world does not carry (a credit against a held or
released invoice, a currency mismatch)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from reducto_ap_agent.api import create_app
from reducto_ap_agent.datasets import _doc
from reducto_ap_agent.errors import CreditError, GateError
from reducto_ap_agent.graph import ApPipeline
from reducto_ap_agent.object_store import InMemoryObjectStore
from reducto_ap_agent.offline_docs import OfflineDocumentAdapter
from reducto_ap_agent.schemas import LineItem
from reducto_ap_agent.service import ApService
from reducto_ap_agent.store import InMemoryRecordStore

APPROVER = "Priya Nair (AP lead)"
CONTROLLER = "Marta Chen (controller)"
TREASURER = "Jonas Weber (treasurer)"
HARBORLIGHT_BANK = "US00 HARB 6666 7777 8888"


def approve(service: ApService, *record_ids: str) -> None:
    for record_id in record_ids:
        service.decide(record_id, "approve", APPROVER, "matched")


def _credit_text(number: str, credits: str | None, total: float,
                 currency: str = "USD") -> str:
    fields = [
        ("Supplier", "Harborlight Marine Supply"),
        ("Credit Note Number", number),
        ("Issue Date", "2026-10-01"),
        ("PO Reference", "PO-6020"),
    ]
    if credits:
        fields.append(("Credits Invoice", credits))
    fields += [
        ("Currency", currency), ("Bank Account", HARBORLIGHT_BANK),
        ("Subtotal", f"{total:.2f}"), ("Tax", "0.00"),
        ("Total", f"{total:.2f}"),
    ]
    line = LineItem(
        sku="SKU-N1", description="Rebate", quantity=1,
        unit_price=total, amount=total,
    )
    return _doc("CREDIT NOTE", 1, fields, [line])


def _submit_credit(service: ApService, submission_id: str, text: str) -> str:
    (record,) = service.submit(
        submission_id, f"{submission_id.lower()}.pdf",
        text.encode("utf-8"), "text/plain",
    )
    assert record.doc_class == "credit_note"
    assert record.status == "AWAITING_APPROVER"
    return record.record_id


# -- intake and the happy path --------------------------------------


def test_credit_notes_are_born_unapplied_and_name_their_invoice(seeded):
    credit = seeded.get_record("REC-SUB-007-0")
    assert credit.credit_state == "UNAPPLIED"
    assert credit.extraction.value("credited_invoice_number") == "INV-1001"
    assert credit.applied_invoice_record_id is None
    invoice = seeded.get_record("REC-SUB-001-0")
    assert invoice.credits_applied == []
    assert invoice.credit_state is None  # only credit notes carry one


def test_application_is_cumulative_and_the_face_never_moves(seeded):
    approve(seeded, "REC-SUB-001-0", "REC-SUB-007-0", "REC-SUB-029-0")
    seeded.apply_credit("REC-SUB-007-0", CONTROLLER)
    seeded.apply_credit("REC-SUB-029-0", CONTROLLER)
    invoice = seeded.get_record("REC-SUB-001-0")
    assert [c.credit_number for c in invoice.credits_applied] == [
        "CN-1001", "CN-1003",
    ]
    assert [c.amount for c in invoice.credits_applied] == [200.00, 100.00]
    assert all(c.actor == CONTROLLER for c in invoice.credits_applied)
    packet = seeded.payment_packet()
    (line,) = [l for l in packet.lines if l.record_id == "REC-SUB-001-0"]
    assert line.gross_amount == 1400.00
    assert line.amount == 1100.00  # 1400.00 − 200.00 − 100.00
    events = [a["event"] for a in invoice.audit]
    assert events.count("credit_applied") == 2


def test_application_requires_a_named_controller_and_a_credit_note(seeded):
    approve(seeded, "REC-SUB-001-0", "REC-SUB-007-0")
    with pytest.raises(GateError):
        seeded.apply_credit("REC-SUB-007-0", "  ")
    with pytest.raises(CreditError):
        seeded.apply_credit("REC-SUB-001-0", CONTROLLER)  # an invoice
    with pytest.raises(GateError) as excinfo:
        seeded.apply_credit("REC-SUB-007-0", CONTROLLER)
        seeded.apply_credit("REC-SUB-007-0", CONTROLLER)
    assert "ALREADY_APPLIED" in str(excinfo.value)


def test_unapproved_credit_and_unapproved_target_are_refused(seeded):
    # Neither side approved yet.
    with pytest.raises(GateError) as excinfo:
        seeded.apply_credit("REC-SUB-007-0", CONTROLLER)
    assert "CREDIT_NOT_APPROVED" in str(excinfo.value)
    # Credit approved, its invoice still at the gate.
    approve(seeded, "REC-SUB-007-0")
    with pytest.raises(GateError) as excinfo:
        seeded.apply_credit("REC-SUB-007-0", CONTROLLER)
    assert "TARGET_NOT_APPROVED" in str(excinfo.value)
    assert "AWAITING_APPROVER" in str(excinfo.value)


# -- the named refusals ----------------------------------------------


def test_over_credit_is_refused_with_both_figures(seeded):
    approve(seeded, "REC-SUB-018-0", "REC-SUB-025-0")
    with pytest.raises(GateError) as excinfo:
        seeded.apply_credit("REC-SUB-025-0", CONTROLLER)
    message = str(excinfo.value)
    assert message.startswith("OVER_CREDIT")
    assert "450.00" in message and "400.00" in message
    # Nothing moved: the credit waits, the invoice's ledger is empty.
    assert seeded.get_record("REC-SUB-025-0").credit_state == "UNAPPLIED"
    assert seeded.get_record("REC-SUB-018-0").credits_applied == []


def test_unreferenced_and_unknown_invoice_credits_are_held(seeded):
    approve(seeded, "REC-SUB-026-0", "REC-SUB-027-0")
    with pytest.raises(GateError) as excinfo:
        seeded.apply_credit("REC-SUB-026-0", CONTROLLER)
    assert "NO_INVOICE_REFERENCE" in str(excinfo.value)
    with pytest.raises(GateError) as excinfo:
        seeded.apply_credit("REC-SUB-027-0", CONTROLLER)
    message = str(excinfo.value)
    assert "INVOICE_NOT_ON_FILE" in message and "INV-9999" in message
    # Both sit on the packet's unapplied list, each with its reason.
    waiting = {
        c.credit_number: c for c in seeded.payment_packet().unapplied_credits
    }
    assert "no invoice" in waiting["CN-9001"].reason
    assert "INV-9999" in waiting["CN-9002"].reason


def test_credit_against_the_fraud_hold_is_refused_by_name(seeded):
    approve(seeded, "REC-SUB-028-0")
    with pytest.raises(GateError) as excinfo:
        seeded.apply_credit("REC-SUB-028-0", CONTROLLER)
    message = str(excinfo.value)
    assert "TARGET_FRAUD_HOLD" in message
    assert "FRAUD_REVIEW" in message
    assert seeded.get_record("REC-SUB-028-0").credit_state == "UNAPPLIED"


def test_credit_against_a_held_invoice_waits_for_the_run(seeded):
    approve(seeded, "REC-SUB-017-0")
    run = seeded.create_payment_run(
        "2026-10-16", record_ids=["REC-SUB-017-0"]
    )
    credit_id = _submit_credit(
        seeded, "SUB-T-INRUN", _credit_text("CN-T1", "INV-2001", -50.00)
    )
    approve(seeded, credit_id)
    with pytest.raises(GateError) as excinfo:
        seeded.apply_credit(credit_id, CONTROLLER)
    message = str(excinfo.value)
    assert "TARGET_IN_RUN" in message and run.run_id in message
    # Return the run and the same application succeeds.
    seeded.decide_payment_run(run.run_id, "return", TREASURER, "rework")
    applied = seeded.apply_credit(credit_id, CONTROLLER)
    assert applied.credit_state == "APPLIED"
    (line,) = [
        l for l in seeded.payment_packet().lines
        if l.record_id == "REC-SUB-017-0"
    ]
    assert line.amount == 850.00  # 900.00 − 50.00


def test_late_credit_against_a_released_invoice_is_refused_forever(seeded):
    approve(seeded, "REC-SUB-017-0")
    run = seeded.create_payment_run(
        "2026-10-16", record_ids=["REC-SUB-017-0"]
    )
    seeded.decide_payment_run(run.run_id, "release", TREASURER, "reviewed")
    export_before = seeded.payment_run_export(run.run_id)
    credit_id = _submit_credit(
        seeded, "SUB-T-LATE", _credit_text("CN-T2", "INV-2001", -50.00)
    )
    approve(seeded, credit_id)
    with pytest.raises(GateError) as excinfo:
        seeded.apply_credit(credit_id, CONTROLLER)
    message = str(excinfo.value)
    assert "TARGET_PAID_PROPOSED" in message and run.run_id in message
    # The refusal changed nothing: export bytes, run, and the
    # invoice's ledger are exactly as released.
    assert seeded.payment_run_export(run.run_id) == export_before
    assert seeded.get_record("REC-SUB-017-0").credits_applied == []
    assert seeded.get_record(credit_id).credit_state == "UNAPPLIED"
    (waiting,) = seeded.payment_packet().unapplied_credits
    assert waiting.credit_number == "CN-T2"
    assert "PAID_PROPOSED" in waiting.reason


def test_currency_mismatch_is_refused(seeded):
    approve(seeded, "REC-SUB-017-0")
    credit_id = _submit_credit(
        seeded, "SUB-T-FX",
        _credit_text("CN-T3", "INV-2001", -50.00, currency="EUR"),
    )
    approve(seeded, credit_id)
    with pytest.raises(GateError) as excinfo:
        seeded.apply_credit(credit_id, CONTROLLER)
    message = str(excinfo.value)
    assert "CURRENCY_MISMATCH" in message
    assert "EUR" in message and "USD" in message


# -- runs and the packet ----------------------------------------------


def test_fully_credited_invoice_is_refused_by_the_run_with_its_reason(seeded):
    approve(seeded, "REC-SUB-001-0", "REC-SUB-019-0", "REC-SUB-024-0")
    seeded.apply_credit("REC-SUB-024-0", CONTROLLER)
    # The packet still shows the invoice — at zero, credits listed.
    (line,) = [
        l for l in seeded.payment_packet().lines
        if l.record_id == "REC-SUB-019-0"
    ]
    assert line.amount == 0.00 and line.gross_amount == 200.00
    run = seeded.create_payment_run(
        "2026-10-16", record_ids=["REC-SUB-019-0", "REC-SUB-001-0"]
    )
    assert [l.record_id for l in run.lines] == ["REC-SUB-001-0"]
    (refusal,) = run.refusals
    assert refusal.record_id == "REC-SUB-019-0"
    assert refusal.code == "FULLY_CREDITED"
    assert "CN-2003" in refusal.reason


def test_unapplied_credit_offered_to_a_run_is_refused_in_its_own_words(seeded):
    approve(seeded, "REC-SUB-001-0", "REC-SUB-007-0")
    run = seeded.create_payment_run(
        "2026-10-16", record_ids=["REC-SUB-001-0", "REC-SUB-007-0"]
    )
    (refusal,) = run.refusals
    assert refusal.code == "CREDIT_NOTE"
    assert "UNAPPLIED" in refusal.reason
    assert "INV-1001" in refusal.reason  # the invoice it must be applied against


def test_credits_view_names_every_state(seeded):
    approve(seeded, "REC-SUB-001-0", "REC-SUB-007-0", "REC-SUB-026-0")
    seeded.apply_credit("REC-SUB-007-0", CONTROLLER)
    views = {v["record_id"]: v for v in seeded.list_credits()}
    assert views["REC-SUB-007-0"]["credit_state"] == "APPLIED"
    assert views["REC-SUB-007-0"]["unapplied_reason"] is None
    assert views["REC-SUB-007-0"]["amount"] == 200.00
    assert views["REC-SUB-007-0"]["credited_invoice_number"] == "INV-1001"
    assert "names no invoice" in views["REC-SUB-026-0"]["unapplied_reason"]


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


def test_credits_over_http(client):
    assert client.post("/demo/seed").status_code == 201
    for record_id in ("REC-SUB-001-0", "REC-SUB-007-0", "REC-SUB-026-0"):
        response = client.post(
            f"/records/{record_id}/decision",
            json={"decision": "approve", "actor": APPROVER, "reason": "matched"},
        )
        assert response.status_code == 200

    credits = client.get("/credits").json()
    by_number = {c["extraction"]["fields"]["invoice_number"]["value"]: c
                 for c in credits}
    assert by_number["CN-1001"]["credit_state"] == "UNAPPLIED"
    assert by_number["CN-1001"]["unapplied_reason"].startswith(
        "approved and applicable"
    )
    assert "names no invoice" in by_number["CN-9001"]["unapplied_reason"]

    blocked = client.post(
        "/credits/REC-SUB-026-0/apply", json={"actor": CONTROLLER}
    )
    assert blocked.status_code == 409
    assert "NO_INVOICE_REFERENCE" in blocked.json()["detail"]

    anonymous = client.post(
        "/credits/REC-SUB-007-0/apply", json={"actor": "  "}
    )
    assert anonymous.status_code == 409

    not_a_credit = client.post(
        "/credits/REC-SUB-001-0/apply", json={"actor": CONTROLLER}
    )
    assert not_a_credit.status_code == 422

    applied = client.post(
        "/credits/REC-SUB-007-0/apply", json={"actor": CONTROLLER}
    )
    assert applied.status_code == 200
    assert applied.json()["credit_state"] == "APPLIED"

    packet = client.get("/payments/packet").json()
    (line,) = [l for l in packet["lines"] if l["invoice_number"] == "INV-1001"]
    assert line["amount"] == 1200.00 and line["gross_amount"] == 1400.00
    assert line["credits_applied"][0]["credit_number"] == "CN-1001"
    waiting = {c["credit_number"] for c in packet["unapplied_credits"]}
    assert waiting == {"CN-9001"}
