"""Partial payments: an invoice paid in parts, with every
reader agreeing on the arithmetic.

The story these tests pin, end to end on INV-1001 (face
1400.00): a run takes 840.00 of it (60%), a credit of 100.00
lands on the 560.00 remainder, and a second run takes the
460.00 that is left — 840 + 460 = 1400 − 100, to the cent, at
every surface that reads the money.

- assembly: a member joins at a stated part; the line carries
  the part beside the full open amount; a non-positive part
  and a part beyond the open amount are refused by name, the
  second with the committed figures in the reason;
- release: the invoice's payments ledger grows by the line
  amount, its open falls by exactly that, and it stands
  PART_PAID while a remainder survives — PAID_PROPOSED only
  when the release completes it. Returning a later run
  restores PART_PAID, never unpaid;
- the readers: the packet proposes the remainder, the position
  ages the remainder in the PART_PAID state with the paid
  figure beside it and discloses the invoice as fully paid at
  the end, the ledger ties to the position with the parts on
  the invoice's line;
- the disciplines around it: a confirmation answers the run's
  LINE (a part advice confirms exactly; a full-face advice is
  over_paid against the part and confirms nothing about the
  remainder); a credit lands on the remainder and an
  over-credit is judged against the remainder's figures; the
  duplicate watch holds a resend of a part-paid invoice on
  the cumulative instructed figure, and does not hold a
  resend at the remainder — the remainder was never paid.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from reducto_ap_agent.api import create_app
from reducto_ap_agent.credits import open_amount, paid_amount
from reducto_ap_agent.datasets import _doc, _inv_fields, build_world, remittance_advice_doc
from reducto_ap_agent.errors import GateError
from reducto_ap_agent.schemas import (
    DocumentRecord,
    ExtractedDocument,
    ExtractedField,
    LineItem,
    PaymentEntry,
)
from reducto_ap_agent.service import ApService

APPROVER = "Priya Nair (AP lead)"
TREASURER = "Jonas Weber (treasurer)"
CONTROLLER = "Marta Chen (controller)"

INV = "REC-SUB-001-0"       # INV-1001, Brightpath, face 1400.00
FILLER = "REC-SUB-017-0"    # a second approved invoice, 900.00


def approve(service: ApService, *record_ids: str) -> None:
    for record_id in record_ids:
        service.decide(record_id, "approve", APPROVER, "matched")


def part_one_released(service: ApService):
    """INV-1001 approved and paid 840.00 of its 1400.00 face in
    a released run. Returns the released run."""
    approve(service, INV)
    run = service.create_payment_run(
        "2026-10-16", record_ids=[INV], amounts={INV: 840.00})
    service.decide_payment_run(run.run_id, "release", TREASURER, "reviewed")
    return service.get_payment_run(run.run_id)


def pay_down_fully(service: ApService):
    """The whole story: 840.00 released, CN-1003 (100.00)
    applied to the remainder, 460.00 released. Returns
    (first run, second run)."""
    first = part_one_released(service)
    approve(service, "REC-SUB-029-0")  # CN-1003, 100.00 on INV-1001
    service.apply_credit("REC-SUB-029-0", CONTROLLER)
    second = service.create_payment_run(
        "2026-11-06", record_ids=[INV], amounts={INV: 460.00})
    service.decide_payment_run(second.run_id, "release", TREASURER, "reviewed")
    return first, service.get_payment_run(second.run_id)


# -- assembly ---------------------------------------------------


def test_member_joins_at_a_stated_part(seeded):
    approve(seeded, INV)
    run = seeded.create_payment_run(
        "2026-10-16", record_ids=[INV], amounts={INV: 840.00})
    (line,) = run.lines
    assert line.amount == 840.00
    assert line.open_amount == 1400.00  # the whole of what was owed
    assert line.gross_amount == 1400.00
    assert run.totals_by_currency == {"USD": 840.00}
    record = seeded.get_record(INV)
    assert record.payment_state == "IN_RUN"
    # Nothing is paid yet: holding is not instructing.
    assert record.payments_applied == []
    assert open_amount(record) == 1400.00


def test_part_equal_to_open_is_a_whole_member(seeded):
    approve(seeded, INV)
    run = seeded.create_payment_run(
        "2026-10-16", record_ids=[INV], amounts={INV: 1400.00})
    (line,) = run.lines
    assert line.amount == line.open_amount == 1400.00


def test_non_positive_part_is_refused_by_name(seeded):
    approve(seeded, INV, FILLER)
    for bad in (0.0, -250.0):
        run = seeded.create_payment_run(
            "2026-10-16", record_ids=[INV, FILLER], amounts={INV: bad})
        refusal = {r.record_id: r for r in run.refusals}[INV]
        assert refusal.code == "INVALID_PART_AMOUNT"
        assert "positive" in refusal.reason
        assert [line.record_id for line in run.lines] == [FILLER]
        seeded.decide_payment_run(run.run_id, "return", TREASURER, "reset")


def test_part_beyond_open_is_refused_with_the_figures(seeded):
    approve(seeded, INV, FILLER)
    run = seeded.create_payment_run(
        "2026-10-16", record_ids=[INV, FILLER], amounts={INV: 1500.00})
    refusal = {r.record_id: r for r in run.refusals}[INV]
    assert refusal.code == "PART_EXCEEDS_OPEN"
    assert "1500.00" in refusal.reason
    assert "1400.00" in refusal.reason


def test_overcommit_after_a_first_part_names_the_committed_figure(seeded):
    part_one_released(seeded)
    approve(seeded, FILLER)
    # 600.00 more would commit 840 + 600 = 1440 against a
    # 1400.00 face: refused, with the committed figure named.
    run = seeded.create_payment_run(
        "2026-11-06", record_ids=[INV, FILLER], amounts={INV: 600.00})
    refusal = {r.record_id: r for r in run.refusals}[INV]
    assert refusal.code == "PART_EXCEEDS_OPEN"
    assert "600.00" in refusal.reason
    assert "560.00" in refusal.reason  # the open remainder
    assert "840.00" in refusal.reason  # already committed
    assert [line.record_id for line in run.lines] == [FILLER]


def test_part_paid_member_in_an_open_run_is_refused_by_the_next(seeded):
    part_one_released(seeded)
    run = seeded.create_payment_run(
        "2026-11-06", record_ids=[INV], amounts={INV: 200.00})
    approve(seeded, FILLER)
    second = seeded.create_payment_run(
        "2026-11-13", record_ids=[INV, FILLER])
    refusal = {r.record_id: r for r in second.refusals}[INV]
    assert refusal.code == "ALREADY_IN_RUN"
    assert run.run_id in refusal.reason
    seeded.decide_payment_run(run.run_id, "return", TREASURER, "reset")
    seeded.decide_payment_run(second.run_id, "return", TREASURER, "reset")


# -- release and the ledger -------------------------------------


def test_release_grows_the_paid_ledger_and_stands_part_paid(seeded):
    run = part_one_released(seeded)
    record = seeded.get_record(INV)
    assert record.payment_state == "PART_PAID"
    assert record.payment_run_id == run.run_id
    (entry,) = record.payments_applied
    assert entry.run_id == run.run_id
    assert entry.amount == 840.00
    assert entry.actor == TREASURER
    assert paid_amount(record) == 840.00
    assert open_amount(record) == 560.00
    assert record.status == "APPROVED"  # the approval never moves
    events = [a["event"] for a in record.audit]
    assert "payment_run_part_paid" in events


def test_two_runs_pay_the_invoice_down_to_the_cent(seeded):
    first, second = pay_down_fully(seeded)
    record = seeded.get_record(INV)
    assert record.payment_state == "PAID_PROPOSED"
    assert [e.amount for e in record.payments_applied] == [840.00, 460.00]
    assert paid_amount(record) == 1300.00
    assert open_amount(record) == 0.00
    # 840 + 460 = 1400 (face) − 100 (CN-1003): nothing lost,
    # nothing invented, across two runs and a credit.
    assert 840.00 + 460.00 == 1400.00 - 100.00
    # Each export instructs only its own run's part.
    first_export = seeded.payment_run_export(first.run_id).decode()
    second_export = seeded.payment_run_export(second.run_id).decode()
    first_row = next(r for r in first_export.splitlines() if INV in r)
    second_row = next(r for r in second_export.splitlines() if INV in r)
    assert first_row.rstrip().endswith(",840.00")
    assert second_row.rstrip().endswith(",460.00")


def test_return_of_the_second_run_restores_part_paid(seeded):
    first = part_one_released(seeded)
    run = seeded.create_payment_run(
        "2026-11-06", record_ids=[INV], amounts={INV: 200.00})
    assert seeded.get_record(INV).payment_state == "IN_RUN"
    seeded.decide_payment_run(run.run_id, "return", TREASURER, "rework")
    record = seeded.get_record(INV)
    assert record.payment_state == "PART_PAID"  # never back to unpaid
    assert record.payment_run_id == first.run_id
    assert paid_amount(record) == 840.00
    assert open_amount(record) == 560.00


def test_fully_paid_invoice_never_joins_another_run(seeded):
    pay_down_fully(seeded)
    approve(seeded, FILLER)
    run = seeded.create_payment_run("2026-11-13", record_ids=[INV, FILLER])
    refusal = {r.record_id: r for r in run.refusals}[INV]
    assert refusal.code == "ALREADY_PAID_PROPOSED"


def test_partial_run_over_http(seeded):
    client = TestClient(create_app(seeded))
    approve(seeded, INV)
    response = client.post("/payments/runs", json={
        "value_date": "2026-10-16",
        "record_ids": [INV],
        "amounts": {INV: 840.00},
    })
    assert response.status_code == 201
    body = response.json()
    (line,) = body["lines"]
    assert line["amount"] == 840.00
    assert line["open_amount"] == 1400.00
    run_id = body["run_id"]
    decided = client.post(f"/payments/runs/{run_id}/decision", json={
        "action": "release", "actor": TREASURER, "reason": "reviewed",
    })
    assert decided.status_code == 200
    record = client.get(f"/records/{INV}").json()
    assert record["payment_state"] == "PART_PAID"
    assert [p["amount"] for p in record["payments_applied"]] == [840.00]


# -- credits against the remainder -------------------------------


def test_credit_lands_on_the_remainder(seeded):
    part_one_released(seeded)
    approve(seeded, "REC-SUB-029-0")  # CN-1003, 100.00
    seeded.apply_credit("REC-SUB-029-0", CONTROLLER)
    record = seeded.get_record(INV)
    assert [c.credit_number for c in record.credits_applied] == ["CN-1003"]
    assert open_amount(record) == 460.00
    assert paid_amount(record) == 840.00  # the released part is untouched


def test_over_credit_is_judged_against_the_remainder(seeded):
    part_one_released(seeded)
    approve(seeded, "REC-SUB-029-0")
    seeded.apply_credit("REC-SUB-029-0", CONTROLLER)  # open now 460.00
    # A planted credit of 500.00 naming INV-1001: larger than
    # the 460.00 remainder, smaller than the original face —
    # only the remainder's figures can refuse it honestly.
    seeded.store.save(DocumentRecord(
        record_id="REC-EV-BIGCN", submission_id="SUB-EV",
        supplier_id="SUP-02", supplier_name="Brightpath Components Inc",
        status="APPROVED", doc_class="credit_note",
        credit_state="UNAPPLIED",
        extraction=ExtractedDocument(
            doc_class="credit_note", currency="USD",
            fields={
                "invoice_number": ExtractedField(value="CN-EV-900"),
                "total": ExtractedField(value=-500.00),
                "credited_invoice_number": ExtractedField(value="INV-1001"),
            }),
    ))
    with pytest.raises(GateError) as excinfo:
        seeded.apply_credit("REC-EV-BIGCN", CONTROLLER)
    message = str(excinfo.value)
    assert message.startswith("OVER_CREDIT")
    assert "500.00" in message and "460.00" in message
    assert seeded.get_record("REC-EV-BIGCN").credit_state == "UNAPPLIED"
    assert open_amount(seeded.get_record(INV)) == 460.00


def test_credit_against_a_part_paid_invoice_in_an_open_run_is_held(seeded):
    part_one_released(seeded)
    seeded.create_payment_run(
        "2026-11-06", record_ids=[INV], amounts={INV: 200.00})
    approve(seeded, "REC-SUB-029-0")
    with pytest.raises(GateError) as excinfo:
        seeded.apply_credit("REC-SUB-029-0", CONTROLLER)
    assert str(excinfo.value).startswith("TARGET_IN_RUN")


# -- the readers ---------------------------------------------------


def test_packet_proposes_the_remainder_with_the_ledger(seeded):
    part_one_released(seeded)
    packet = seeded.payment_packet()
    line = next(l for l in packet.lines if l.record_id == INV)
    assert line.amount == 560.00
    assert line.gross_amount == 1400.00
    assert [p.amount for p in line.payments_applied] == [840.00]


def test_position_ages_the_remainder_and_discloses_the_end(seeded):
    part_one_released(seeded)
    report = seeded.ap_position("2026-11-15")
    (row,) = [r for r in report.suppliers if r.supplier_id == "SUP-02"]
    (line,) = [l for l in row.lines if l.record_id == INV]
    assert line.open == 560.00
    assert line.paid == 840.00
    assert line.state == "PART_PAID"
    assert row.paid_total == 840.00
    assert report.paid_totals_by_currency["USD"] >= 840.00
    assert report.state_totals_by_currency["USD"]["PART_PAID"] == 560.00
    assert report.splits_agree is True
    assert report.tie_status == "TIED"

    pay_down_rest = seeded.create_payment_run(
        "2026-11-20", record_ids=[INV], amounts={INV: 560.00})
    seeded.decide_payment_run(
        pay_down_rest.run_id, "release", TREASURER, "the rest")
    report = seeded.ap_position("2026-11-15")
    (paid_line,) = [f for f in report.fully_paid if f.record_id == INV]
    assert paid_line.face == 1400.00
    assert paid_line.paid == 1400.00
    rows = [r for r in report.suppliers if r.supplier_id == "SUP-02"]
    assert all(
        l.record_id != INV for r in rows for l in r.lines)
    assert report.tie_status == "TIED"


def test_ledger_shows_the_parts_and_ties(seeded):
    pay_down_fully(seeded)
    ledger = seeded.supplier_ledger("SUP-02", "2026-11-15")
    invoice = next(i for i in ledger.invoices if i.record_id == INV)
    assert invoice.face == 1400.00
    assert invoice.credits_total == 100.00
    assert invoice.paid_total == 1300.00
    assert [p.amount for p in invoice.payments_applied] == [840.00, 460.00]
    assert invoice.open == 0.00
    (totals,) = [t for t in ledger.totals if t.currency == "USD"]
    assert totals.paid_total >= 1300.00
    assert ledger.tie_status == "TIED"


def test_position_csv_carries_the_part_paid_column(service):
    service.store.save(DocumentRecord(
        record_id="REC-PP", submission_id="SUB-EV",
        supplier_id="SUP-02", supplier_name="Brightpath",
        status="APPROVED", doc_class="invoice",
        payment_state="PART_PAID", payment_run_id="RUN-EV-1",
        payments_applied=[PaymentEntry(
            run_id="RUN-EV-1", amount=600.00, currency="USD",
            actor=TREASURER, at="2026-10-16T09:30:00+00:00")],
        extraction=ExtractedDocument(
            doc_class="invoice", currency="USD",
            fields={
                "invoice_number": ExtractedField(value="INV-PP"),
                "invoice_date": ExtractedField(value="2026-09-01"),
                "due_date": ExtractedField(value="2026-11-15"),
                "total": ExtractedField(value=1000.00),
            }),
    ))
    report = service.ap_position("2026-11-15")
    (line,) = report.suppliers[0].lines
    assert (line.face, line.paid, line.open) == (1000.00, 600.00, 400.00)
    assert line.state == "PART_PAID"
    from reducto_ap_agent.position import position_csv
    text = position_csv(report).decode()
    assert "PART_PAID" in text
    assert "Payments released" in text


# -- confirmations answer the line ---------------------------------


def _advice(service: ApService, run_id: str, rows, submission_id: str):
    text = remittance_advice_doc(run_id, rows)
    (record,) = service.submit(
        submission_id, f"{submission_id.lower()}.pdf",
        text.encode("utf-8"), "text/plain")
    return record


def test_part_advice_confirms_the_line_exactly(seeded):
    run = part_one_released(seeded)
    before = seeded.get_record(INV).model_dump_json()
    advice = _advice(seeded, run.run_id, [
        ("INV-1001", 840.00, "USD", "2026-10-16", "BNK-PP-01"),
    ], "SUB-PP-ADV-1")
    conf = seeded.confirm_remittance(advice.record_id)
    (line,) = [l for l in conf.lines if l.invoice_number == "INV-1001"]
    assert line.classification == "paid_as_proposed"
    assert line.proposed_amount == 840.00
    record = seeded.get_record(INV)
    assert record.payment_confirmation == "CONFIRMED"
    assert record.payment_state == "PART_PAID"  # the overlay only
    assert open_amount(record) == 560.00
    assert record.model_dump_json() != before  # overlay written, nothing else


def test_full_face_advice_does_not_confirm_the_remainder(seeded):
    run = part_one_released(seeded)
    export_before = seeded.payment_run_export(run.run_id)
    advice = _advice(seeded, run.run_id, [
        ("INV-1001", 1400.00, "USD", "2026-10-16", "BNK-PP-02"),
    ], "SUB-PP-ADV-2")
    conf = seeded.confirm_remittance(advice.record_id)
    (line,) = [l for l in conf.lines if l.invoice_number == "INV-1001"]
    # The bank answering the full face against a run that
    # instructed 840.00 is an over-payment break on THIS run —
    # it says nothing about the 560.00 remainder, which stays
    # open, unproposed, and payable by a later run.
    assert line.classification == "over_paid"
    assert line.proposed_amount == 840.00
    assert line.paid_amount == 1400.00
    record = seeded.get_record(INV)
    assert record.payment_state == "PART_PAID"
    assert open_amount(record) == 560.00
    assert seeded.payment_run_export(run.run_id) == export_before


# -- the watch, on cumulative evidence -------------------------------


def _resend(service: ApService, submission_id: str, total: float):
    s02 = build_world().supplier("SUP-02")
    text = _doc("INVOICE", 1, _inv_fields(
        s02, "INV-1001", "PO-6001", total), [
        LineItem(sku="SKU-A1", description="Industrial valves",
                 quantity=10, unit_price=120.00, amount=1200.00),
        LineItem(sku="SKU-A2", description="Gasket sets",
                 quantity=5, unit_price=40.00, amount=200.00),
    ])
    (record,) = service.submit(
        submission_id, f"{submission_id.lower()}.pdf",
        text.encode("utf-8"), "text/plain")
    return record


def test_resend_of_a_part_paid_invoice_is_held_on_the_part(seeded):
    run = part_one_released(seeded)
    at_the_part = _resend(seeded, "SUB-PP-RESEND-PART", 840.00)
    assert at_the_part.status == "PAID_DUPLICATE_HOLD"
    hold = at_the_part.duplicate_hold
    assert hold.amount_basis == "NET"
    assert hold.amount_paid == 840.00  # the cumulative instructed figure
    assert hold.paid_face_amount == 1400.00
    assert hold.payment_run_id == run.run_id

    at_the_face = _resend(seeded, "SUB-PP-RESEND-FACE", 1400.00)
    assert at_the_face.status == "PAID_DUPLICATE_HOLD"
    assert at_the_face.duplicate_hold.amount_basis == "FACE"
    assert at_the_face.duplicate_hold.amount_paid == 840.00


def test_resend_at_the_remainder_is_not_paid_history(seeded):
    part_one_released(seeded)
    remainder = _resend(seeded, "SUB-PP-RESEND-REST", 560.00)
    # 560.00 was never instructed — it is a different claim,
    # and the number rule, not the watch, surfaces it.
    assert remainder.duplicate_hold is None
    assert remainder.status == "EXCEPTION"
    assert "DUPLICATE_INVOICE" in remainder.match.exception_codes


def test_watch_evidence_is_cumulative_across_both_runs(seeded):
    _, second = pay_down_fully(seeded)
    resend = _resend(seeded, "SUB-PP-RESEND-NET", 1300.00)
    assert resend.status == "PAID_DUPLICATE_HOLD"
    hold = resend.duplicate_hold
    assert hold.amount_basis == "NET"
    assert hold.amount_paid == 1300.00  # 840.00 + 460.00, two runs summed
    assert hold.payment_run_id == second.run_id  # the most recent
    assert hold.released_by == TREASURER
