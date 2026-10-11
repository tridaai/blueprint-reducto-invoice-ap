"""Statement reconciliation: intake, the five break classes,
the controller gate, and the packet invariance pin."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from reducto_ap_agent.api import create_app
from reducto_ap_agent.errors import GateError, ReconciliationError
from reducto_ap_agent.graph import ApPipeline
from reducto_ap_agent.object_store import InMemoryObjectStore
from reducto_ap_agent.offline_docs import OfflineDocumentAdapter
from reducto_ap_agent.service import ApService
from reducto_ap_agent.store import InMemoryRecordStore


def test_statement_record_is_refused_but_carries_its_lines(seeded):
    record = seeded.get_record("REC-SUB-020-0")
    # The payment path's refusal is unchanged from v1…
    assert record.status == "REFUSED"
    assert record.refusal_reason is not None
    assert "NOT_PAYABLE_DOCUMENT" in record.refusal_reason
    # …and the statement's lines ride the record for reconciliation.
    assert record.supplier_id == "SUP-08"
    assert record.extraction is not None
    lines = record.extraction.statement_lines
    assert [ln.invoice_number for ln in lines] == [
        "INV-2001", "INV-2002", "INV-2999", "INV-2001",
    ]
    assert record.extraction.value("total") == 2515.00
    assert record.extraction.value("period") == "2026-09"
    events = [a["event"] for a in record.audit]
    assert events.index("statement_captured") < events.index("refused")


def test_every_planted_break_lands_in_its_exact_class(seeded):
    recon = seeded.reconcile("REC-SUB-020-0")
    assert recon.status == "AWAITING_CONTROLLER"
    assert recon.supplier_id == "SUP-08"
    assert recon.period == "2026-09"
    assert [
        (ln.invoice_number, ln.classification) for ln in recon.lines
    ] == [
        ("INV-2001", "matched"),
        ("INV-2002", "amount_disagrees"),
        ("INV-2999", "missing_in_ap"),
        ("INV-2001", "duplicates_within_statement"),
        ("INV-2003", "missing_on_statement"),
    ]
    assert recon.counts == {
        "matched": 1, "amount_disagrees": 1, "missing_in_ap": 1,
        "missing_on_statement": 1, "duplicates_within_statement": 1,
    }
    disagrees = recon.lines[1]
    assert disagrees.statement_amount == 440.00  # both figures, side by side
    assert disagrees.ap_amount == 400.00
    assert disagrees.ap_record_id == "REC-SUB-018-0"
    assert recon.totals_tie and recon.flags == []
    assert recon.statement_total_stated == 2515.00
    assert recon.statement_total_lines == 2515.00


def test_clean_statement_reconciles_fully(seeded):
    recon = seeded.reconcile("REC-SUB-021-0")
    assert len(recon.lines) == 2
    assert all(ln.classification == "matched" for ln in recon.lines)
    assert recon.counts["matched"] == 2
    assert sum(recon.counts.values()) == 2
    assert recon.totals_tie


def test_statement_total_mismatch_is_named_not_passed(seeded):
    text = (
        "=== PAGE 1 ===\n[DOCUMENT: STATEMENT]\n"
        "Supplier: Cedarline Packaging Co\n"
        "Statement Date: 2026-09-30\nPeriod: 2026-09\nCurrency: USD\n"
        "Lines:\nInvoice | Date | Currency | Amount\n"
        "INV-1002 | 2026-09-14 | USD | 1010.00\n"
        "Total: 5000.00\n"
    )
    (record,) = seeded.submit("SUB-T1", "stmt.pdf", text.encode(), "text/plain")
    recon = seeded.reconcile(record.record_id)
    assert recon.totals_tie is False
    assert recon.flags == ["STATEMENT_TOTAL_MISMATCH"]
    assert "5000.00" in recon.notes[0] and "1010.00" in recon.notes[0]
    # The line itself still reconciles on its own merits.
    assert recon.lines[0].classification == "matched"


def test_statement_without_a_total_names_that_too(seeded):
    text = (
        "=== PAGE 1 ===\n[DOCUMENT: STATEMENT]\n"
        "Supplier: Cedarline Packaging Co\n"
        "Statement Date: 2026-09-30\nPeriod: 2026-09\nCurrency: USD\n"
        "Lines:\nInvoice | Date | Currency | Amount\n"
        "INV-1002 | 2026-09-14 | USD | 1010.00\n"
    )
    (record,) = seeded.submit("SUB-T2", "stmt.pdf", text.encode(), "text/plain")
    recon = seeded.reconcile(record.record_id)
    assert recon.totals_tie is False
    assert recon.flags == ["STATEMENT_TOTAL_MISSING"]


def test_reconcile_refuses_what_is_not_a_statement(seeded):
    with pytest.raises(ReconciliationError):
        seeded.reconcile("REC-SUB-001-0")  # an invoice
    with pytest.raises(ReconciliationError):
        seeded.reconcile("REC-SUB-010-1")  # a remittance advice


def test_rejected_invoice_leaves_the_ap_side_of_the_join(seeded):
    seeded.decide("REC-SUB-019-0", "reject", "Priya Nair (AP lead)", "sent in error")
    recon = seeded.reconcile("REC-SUB-020-0")
    # AP finally disposed of INV-2003, so its absence from the
    # statement is no longer a break in either direction.
    assert recon.counts["missing_on_statement"] == 0
    assert all(ln.invoice_number != "INV-2003" for ln in recon.lines)


def test_second_reconcile_of_one_statement_is_a_fresh_record(seeded):
    first = seeded.reconcile("REC-SUB-020-0")
    second = seeded.reconcile("REC-SUB-020-0")
    assert first.reconciliation_id == "RECON-REC-SUB-020-0"
    assert second.reconciliation_id == "RECON-REC-SUB-020-0-2"
    mine = seeded.list_reconciliations(supplier_id="SUP-08")
    assert [r.reconciliation_id for r in mine] == [
        "RECON-REC-SUB-020-0", "RECON-REC-SUB-020-0-2",
    ]


def test_controller_gate_discipline(seeded):
    recon = seeded.reconcile("REC-SUB-021-0")
    with pytest.raises(GateError):
        seeded.decide_reconciliation(recon.reconciliation_id, "accept", "  ")
    with pytest.raises(GateError):
        seeded.decide_reconciliation(recon.reconciliation_id, "maybe", "Marta Chen")
    with pytest.raises(GateError):
        # Flagging back without a note gives the AP team nothing to act on.
        seeded.decide_reconciliation(recon.reconciliation_id, "flag_back", "Marta Chen")
    decided = seeded.decide_reconciliation(
        recon.reconciliation_id, "accept", "Marta Chen (controller)", "agrees with AP"
    )
    assert decided.status == "ACCEPTED"
    assert decided.decision is not None
    assert decided.decision.actor == "Marta Chen (controller)"
    assert [a["event"] for a in decided.audit] == ["reconciled", "controller_decision"]
    with pytest.raises(GateError):
        seeded.decide_reconciliation(recon.reconciliation_id, "accept", "Marta Chen")


def test_flag_back_records_the_note(seeded):
    recon = seeded.reconcile("REC-SUB-020-0")
    decided = seeded.decide_reconciliation(
        recon.reconciliation_id, "flag_back", "Marta Chen (controller)",
        "INV-2999 never arrived — ask Harborlight for the copy",
    )
    assert decided.status == "FLAGGED_BACK"
    assert decided.decision is not None
    assert "INV-2999" in decided.decision.note


def test_reconciliation_never_changes_what_is_payable(seeded):
    """The pin: reconciling — and deciding — moves no record and
    no packet line. Reconciliation is evidence, not action."""
    seeded.decide("REC-SUB-001-0", "approve", "Priya Nair (AP lead)", "matched")
    seeded.decide("REC-SUB-017-0", "approve", "Priya Nair (AP lead)", "matched")
    packet_before = seeded.payment_packet()
    snapshot = {
        r.record_id: (r.status, r.decision, r.match) for r in seeded.list_records()
    }

    breaks = seeded.reconcile("REC-SUB-020-0")
    clean = seeded.reconcile("REC-SUB-021-0")
    seeded.decide_reconciliation(clean.reconciliation_id, "accept", "Marta Chen")
    seeded.decide_reconciliation(
        breaks.reconciliation_id, "flag_back", "Marta Chen", "breaks to the supplier"
    )

    assert {
        r.record_id: (r.status, r.decision, r.match) for r in seeded.list_records()
    } == snapshot
    packet_after = seeded.payment_packet()
    assert packet_after.lines == packet_before.lines
    assert packet_after.totals_by_currency == packet_before.totals_by_currency


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


def test_reconciliation_over_http(client):
    assert client.post("/demo/seed").status_code == 201
    created = client.post("/statements/REC-SUB-020-0/reconcile")
    assert created.status_code == 201
    recon = created.json()
    assert recon["status"] == "AWAITING_CONTROLLER"
    assert recon["counts"]["missing_in_ap"] == 1

    listed = client.get("/reconciliations", params={"supplier_id": "SUP-08"})
    assert [r["reconciliation_id"] for r in listed.json()] == [recon["reconciliation_id"]]

    no_note = client.post(
        f"/reconciliations/{recon['reconciliation_id']}/decision",
        json={"action": "flag_back", "actor": "Marta Chen (controller)"},
    )
    assert no_note.status_code == 409
    decided = client.post(
        f"/reconciliations/{recon['reconciliation_id']}/decision",
        json={
            "action": "flag_back", "actor": "Marta Chen (controller)",
            "note": "INV-2999 never arrived",
        },
    )
    assert decided.status_code == 200
    assert decided.json()["status"] == "FLAGGED_BACK"


def test_reconcile_an_invoice_over_http_is_422(client):
    client.post("/demo/seed")
    response = client.post("/statements/REC-SUB-001-0/reconcile")
    assert response.status_code == 422
    assert client.post("/statements/REC-NOPE-0/reconcile").status_code == 404
    assert client.get("/reconciliations/RECON-NOPE").status_code == 404
