"""PO coverage: the accumulation the match cannot see.

Three honest invoices can over-bill one order; each matches on
its own. These tests plant the accumulation directly (the
position scenario's idiom) so the cumulatives are exact, and
then prove the gate behaviour on the seeded world: coverage
informs the approver — it never blocks, never rewrites a
match — and a decision taken on an EXCEEDED order is recorded
as taken with the fact in front of them.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from reducto_ap_agent.api import create_app
from reducto_ap_agent.credits import billable_amount, open_amount
from reducto_ap_agent.errors import CoverageError
from reducto_ap_agent.schemas import (
    CreditApplication,
    DocumentRecord,
    ExtractedDocument,
    ExtractedField,
    PaymentEntry,
)
from reducto_ap_agent.service import ApService

import pytest

# Register totals in the synthetic world: PO-6003 (Dunmore)
# 1000.00 USD, PO-6002 (Cedarline) 1000.00 USD, PO-6004
# (Eastvale) 300.00 USD.
PO = "PO-6003"


def _invoice(
    record_id: str,
    number: str,
    po: str | None,
    total: float,
    *,
    status: str = "APPROVED",
    currency: str = "USD",
    supplier: str = "Dunmore Electrical Ltd",
    doc_class: str = "invoice",
    credits: tuple[float, ...] = (),
    payments: tuple[float, ...] = (),
) -> DocumentRecord:
    fields = {
        "invoice_number": ExtractedField(value=number),
        "invoice_date": ExtractedField(value="2026-09-01"),
        "total": ExtractedField(value=total),
    }
    if po is not None:
        fields["po_reference"] = ExtractedField(value=po)
    return DocumentRecord(
        record_id=record_id,
        submission_id="SUB-TEST-COV",
        supplier_id="SUP-04",
        supplier_name=supplier,
        status=status,  # type: ignore[arg-type]
        doc_class=doc_class,  # type: ignore[arg-type]
        extraction=ExtractedDocument(
            doc_class=doc_class, fields=fields, currency=currency),
        credits_applied=[
            CreditApplication(
                credit_record_id=f"{record_id}-CR{i}",
                credit_number=f"CN-{record_id}-{i}",
                invoice_record_id=record_id, amount=amount,
                currency=currency, actor="Test Controller",
            ) for i, amount in enumerate(credits)
        ],
        payments_applied=[
            PaymentEntry(
                run_id=f"RUN-{record_id}-{i}", amount=amount,
                currency=currency, actor="Test Treasurer",
            ) for i, amount in enumerate(payments)
        ],
    )


def _plant(service: ApService, *records: DocumentRecord) -> None:
    for record in records:
        service.store.save(record)


def _row(service: ApService, po_number: str):
    report = service.po_coverage()
    return next(r for r in report.orders if r.po_number == po_number)


# -- the billable figure --------------------------------------


def test_billable_is_face_minus_credits_never_minus_payments():
    record = _invoice("REC-CB-1", "INV-CB-1", PO, 1000.00,
                      credits=(200.00, 100.00), payments=(500.00,))
    assert billable_amount(record) == 700.00  # what it charges the order
    assert open_amount(record) == 200.00      # what is still owed on it


# -- the accumulation ------------------------------------------


def test_three_invoices_cross_watch_then_exceeded(service):
    _plant(service, _invoice("REC-C1", "INV-C1", PO, 500.00))
    row = _row(service, PO)
    assert (row.billed, row.pct, row.state) == (500.00, 50.0, "WITHIN")
    assert row.total == 1000.00 and row.total_source == "ORDER_REGISTER"

    _plant(service, _invoice("REC-C2", "INV-C2", PO, 350.00))
    row = _row(service, PO)
    assert (row.billed, row.pct, row.state) == (850.00, 85.0, "WATCH")
    assert [line.cumulative_billed for line in row.lines] == [500.00, 850.00]
    assert [line.cumulative_pct for line in row.lines] == [50.0, 85.0]
    assert [line.invoice_number for line in row.lines] == ["INV-C1", "INV-C2"]

    _plant(service, _invoice("REC-C3", "INV-C3", PO, 300.00))
    row = _row(service, PO)
    assert (row.billed, row.pct, row.state) == (1150.00, 115.0, "EXCEEDED")
    assert [line.cumulative_billed for line in row.lines] == [
        500.00, 850.00, 1150.00]


def test_threshold_boundaries_are_exact(service):
    _plant(service, _invoice("REC-B1", "INV-B1", "PO-6002", 800.00))
    row = _row(service, "PO-6002")
    assert (row.pct, row.state) == (80.0, "WATCH")  # at the threshold: WATCH

    _plant(service, _invoice("REC-B2", "INV-B2", "PO-6004", 300.00))
    row = _row(service, "PO-6004")
    # Exactly the total is a fully billed order, not an exceeded one.
    assert (row.pct, row.state) == (100.0, "WATCH")

    _plant(service, _invoice("REC-B3", "INV-B3", "PO-6004", 0.01))
    row = _row(service, "PO-6004")
    assert (row.billed, row.state) == (300.01, "EXCEEDED")  # one cent past


def test_credits_reduce_billed_and_part_payment_does_not(service):
    record = _invoice("REC-CP-1", "INV-CP-1", PO, 600.00,
                      credits=(150.00,), payments=(450.00,))
    _plant(service, record)
    row = _row(service, PO)
    # The credit took 150.00 off the bill; the released 450.00
    # settled the rest of it — the order was still billed 450.00.
    assert row.billed == 450.00
    (line,) = row.lines
    assert (line.face, line.credits, line.billable) == (600.00, 150.00, 450.00)
    assert open_amount(record) == 0.00  # owed nothing, billed 450.00


def test_unapproved_money_bills_nothing(service):
    _plant(
        service,
        _invoice("REC-U1", "INV-U1", PO, 500.00, status="AWAITING_APPROVER"),
        _invoice("REC-U2", "INV-U2", PO, 500.00, status="EXCEPTION"),
        _invoice("REC-U3", "INV-U3", PO, 500.00, status="REJECTED"),
        _invoice("REC-U4", "INV-U4", PO, 500.00, status="FRAUD_REVIEW"),
        _invoice("REC-U5", "CN-U5", PO, -500.00, doc_class="credit_note"),
    )
    row = _row(service, PO)
    assert row.billed == 0.00 and row.lines == [] and row.state == "WITHIN"


def test_invoice_without_a_po_affects_nothing(service):
    _plant(service, _invoice("REC-N1", "INV-N1", None, 9999.00))
    report = service.po_coverage()
    assert {r.po_number for r in report.orders} == set(
        service.store.reference.purchase_orders)
    assert all(r.billed == 0.00 for r in report.orders)


def test_unknown_order_is_unmeasured_with_its_billed_sum(service):
    _plant(service, _invoice("REC-M1", "INV-M1", "PO-9997", 275.00))
    report = service.po_coverage()
    row = next(r for r in report.orders if r.po_number == "PO-9997")
    assert row.state == "UNMEASURED"
    assert row.total is None and row.pct is None
    assert row.total_source == "NOT_ON_FILE"
    assert row.billed == 275.00
    assert report.orders[-1].po_number == "PO-9997"  # unmeasured last


def test_untouched_register_order_reads_within_at_zero(service):
    report = service.po_coverage()
    row = next(r for r in report.orders if r.po_number == "PO-6004")
    assert (row.billed, row.pct, row.state) == (0.00, 0.0, "WITHIN")
    assert report.watch_pct == 80.0
    assert sum(report.counts.values()) == len(report.orders)


def test_ranking_is_exceeded_watch_within_unmeasured(service):
    _plant(
        service,
        _invoice("REC-R1", "INV-R1", PO, 1150.00),               # EXCEEDED
        _invoice("REC-R2", "INV-R2", "PO-6002", 850.00),         # WATCH
        _invoice("REC-R3", "INV-R3", "PO-6004", 100.00),         # WITHIN
        _invoice("REC-R4", "INV-R4", "PO-9997", 50.00),          # UNMEASURED
    )
    order = [r.po_number for r in service.po_coverage().orders]
    assert order.index(PO) < order.index("PO-6002") \
        < order.index("PO-6004") < order.index("PO-9997")


def test_foreign_currency_is_excluded_never_converted(service):
    _plant(service, _invoice("REC-X1", "INV-X1", PO, 500.00, currency="EUR"))
    row = _row(service, PO)
    assert row.billed == 0.00 and row.lines == []
    (excluded,) = row.excluded
    assert excluded.record_id == "REC-X1" and excluded.amount == 500.00
    assert "never converts" in excluded.reason


def test_coverage_is_a_pure_read(service):
    _plant(service, _invoice("REC-P1", "INV-P1", PO, 900.00))
    before = [r.model_dump() for r in service.list_records()]
    service.po_coverage()
    service.record_coverage("REC-P1")
    assert [r.model_dump() for r in service.list_records()] == before


# -- the per-record fact ---------------------------------------


def test_record_coverage_counts_the_invoice_in_front_of_the_approver(service):
    _plant(service, _invoice("REC-F1", "INV-F1", PO, 850.00))
    awaiting = _invoice("REC-F2", "INV-F2", PO, 300.00,
                        status="AWAITING_APPROVER")
    _plant(service, awaiting)
    fact = service.record_coverage("REC-F2")
    assert fact.counted is False
    assert (fact.billed, fact.billed_including) == (850.00, 1150.00)
    assert (fact.pct, fact.state) == (115.0, "EXCEEDED")
    assert "115.0%" in fact.fact and "including this invoice" in fact.fact

    approved = service.record_coverage("REC-F1")
    assert approved.counted is True
    assert approved.billed_including == approved.billed == 850.00
    assert approved.state == "WATCH"


def test_record_coverage_unmeasured_fact(service):
    _plant(service, _invoice("REC-F3", "INV-F3", "PO-9997", 200.00))
    fact = service.record_coverage("REC-F3")
    assert fact.state == "UNMEASURED" and fact.total is None
    assert "not on the order register" in fact.fact


def test_record_coverage_refuses_records_with_no_fact(service):
    _plant(service, _invoice("REC-F4", "INV-F4", None, 100.00,
                             status="EXCEPTION"))
    with pytest.raises(CoverageError, match="names no purchase order"):
        service.record_coverage("REC-F4")


# -- the gate: informed, never blocked --------------------------


def test_crossing_invoice_stays_decidable_and_the_record_shows_it(seeded):
    seeded.decide("REC-SUB-010-0", "approve", "Test Approver", "test")
    first = seeded.get_record("REC-SUB-010-0")
    # 600.00 of PO-6010's 600.00 — fully billed (WATCH), so no
    # coverage note belongs on this decision.
    assert not any(
        a["event"] == "po_coverage_at_decision" for a in first.audit)

    fact = seeded.record_coverage("REC-SUB-030-0")
    assert fact.state == "EXCEEDED" and fact.pct == 200.0
    assert fact.billed_including == 1200.00 and fact.total == 600.00

    decided = seeded.decide("REC-SUB-030-0", "approve", "Test Approver", "test")
    assert decided.status == "APPROVED"  # coverage informed; it did not block
    assert decided.match is not None and decided.match.status == "MATCHED"
    notes = [
        a for a in decided.audit if a["event"] == "po_coverage_at_decision"
    ]
    (note,) = notes
    assert "PO-6010" in note["detail"] and "200.0%" in note["detail"]


def test_decision_under_the_threshold_writes_no_coverage_note(seeded):
    decided = seeded.decide("REC-SUB-001-0", "approve", "Test Approver", "test")
    assert decided.status == "APPROVED"
    assert not any(
        a["event"] == "po_coverage_at_decision" for a in decided.audit)


# -- the ledger rows --------------------------------------------


def test_ledger_rows_carry_the_order_and_its_coverage_state(seeded):
    seeded.decide("REC-SUB-010-0", "approve", "Test Approver", "test")
    seeded.decide("REC-SUB-030-0", "approve", "Test Approver", "test")
    ledger = seeded.supplier_ledger("SUP-09", "2026-11-15")
    rows = {i.invoice_number: i for i in ledger.invoices}
    assert rows["INV-1010"].po_reference == "PO-6010"
    assert rows["INV-1010"].po_coverage_state == "EXCEEDED"
    assert rows["INV-3001"].po_coverage_state == "EXCEEDED"
    # Display only: the money story is the one the ledger
    # always told — face 1200.00, open 1200.00, still tied.
    (usd,) = ledger.totals
    assert (usd.face_total, usd.open_total) == (1200.00, 1200.00)
    assert ledger.tie_status == "TIED"

    no_po = seeded.supplier_ledger("SUP-06", "2026-11-15")
    (row,) = no_po.invoices
    assert row.po_reference == "" and row.po_coverage_state is None


# -- over HTTP ---------------------------------------------------


def test_coverage_over_http(seeded):
    client = TestClient(create_app(seeded))
    response = client.get("/po-coverage")
    assert response.status_code == 200
    body = response.json()
    assert body["watch_pct"] == 80.0
    assert any(o["po_number"] == "PO-6010" for o in body["orders"])

    seeded.decide("REC-SUB-010-0", "approve", "Test Approver", "test")
    fact = client.get("/records/REC-SUB-030-0/po-coverage")
    assert fact.status_code == 200
    assert fact.json()["state"] == "EXCEEDED"
    assert fact.json()["counted"] is False

    no_po = client.get("/records/REC-SUB-005-0/po-coverage")
    assert no_po.status_code == 422
    statement = client.get("/records/REC-SUB-020-0/po-coverage")
    assert statement.status_code == 422
