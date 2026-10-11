"""The supplier ledger: one supplier, the whole relationship,
every figure tracing to its record — joined the intake's way,
tied to the position, with the documents that joined to no
supplier named instead of attached or dropped."""

from __future__ import annotations

from fastapi.testclient import TestClient

from reducto_ap_agent.api import create_app
from reducto_ap_agent.datasets import _doc, remittance_advice_doc
from reducto_ap_agent.ledger import build_ledger
from reducto_ap_agent.schemas import LineItem, Supplier
from reducto_ap_agent.service import ApService

AS_OF = "2026-11-15"
HARBORLIGHT = "SUP-08"


def _approve_all_awaiting(service: ApService) -> None:
    for record in service.list_records(status="AWAITING_APPROVER"):
        service.decide(record.record_id, "approve", "Test Approver", "test")


def _stray_invoice(supplier_name: str, number: str, po: str | None,
                   total: float) -> bytes:
    fields = [
        ("Supplier", supplier_name),
        ("Invoice Number", number),
        ("Invoice Date", "2026-09-14"),
        ("Due Date", "2026-10-14"),
    ]
    if po:
        fields.append(("PO Reference", po))
    fields += [
        ("Currency", "USD"),
        ("Bank Account", "US00 HARB 6666 7777 8888"),
        ("Subtotal", f"{total:.2f}"), ("Tax", "0.00"),
        ("Total", f"{total:.2f}"),
    ]
    text = _doc("INVOICE", 1, fields, [
        LineItem(sku="SKU-N9", description="Mooring line", quantity=1,
                 unit_price=total, amount=total),
    ])
    return text.encode("utf-8")


def test_ledger_totals_tie_to_position_for_every_supplier(seeded):
    _approve_all_awaiting(seeded)
    seeded.apply_credit("REC-SUB-007-0", "Test Controller")   # CN-1001 → INV-1001
    seeded.apply_credit("REC-SUB-024-0", "Test Controller")   # CN-2003 → INV-2003
    position = seeded.ap_position(AS_OF)
    rows = {(r.supplier_id, r.currency): r for r in position.suppliers}
    index = seeded.supplier_index(AS_OF)
    for entry in index.suppliers:
        ledger = seeded.supplier_ledger(entry.supplier_id, AS_OF)
        assert ledger.tie_status == "TIED", entry.supplier_id
        for totals in ledger.totals:
            row = rows[(entry.supplier_id, totals.currency)]
            assert totals.open_total == row.open_total
            assert totals.position_open_total == row.open_total
    harbor = seeded.supplier_ledger(HARBORLIGHT, AS_OF)
    (usd,) = harbor.totals
    assert (usd.face_total, usd.credits_total, usd.open_total) == (
        1500.00, 200.00, 1300.00)
    assert usd.approved_invoices == 3


def test_ledger_is_a_pure_read(seeded):
    _approve_all_awaiting(seeded)
    seeded.reconcile("REC-SUB-020-0")
    before = [r.model_dump() for r in seeded.list_records()]
    seeded.supplier_index(AS_OF)
    seeded.supplier_ledger(HARBORLIGHT, AS_OF)
    assert [r.model_dump() for r in seeded.list_records()] == before


def test_unmatched_documents_are_named_and_attached_nowhere(seeded):
    (stray,) = seeded.submit(
        "SUB-TEST-STRAY", "stray.pdf",
        _stray_invoice("Harbor Light Marine Supply", "INV-2099",
                       "PO-9998", 150.00),
        "text/plain",
    )
    assert stray.supplier_id is None
    index = seeded.supplier_index(AS_OF)
    assert index.unmatched_count == 1
    (unmatched,) = index.unmatched
    assert unmatched.record_id == stray.record_id
    assert unmatched.name_as_written == "Harbor Light Marine Supply"
    assert unmatched.document_number == "INV-2099"
    # Resemblance is not a join: Harborlight's ledger does not
    # contain it, and neither does any other supplier's.
    for entry in index.suppliers:
        ledger = seeded.supplier_ledger(entry.supplier_id, AS_OF)
        assert stray.record_id not in [d.record_id for d in ledger.documents]


def test_po_reference_still_joins_when_the_name_does_not(seeded):
    # The intake's second path: the name matches nothing, but
    # the document references Harborlight's PO — the PO's
    # supplier resolves it, exactly as the pipeline has always
    # done. The ledger invents no third path and removes none.
    (record,) = seeded.submit(
        "SUB-TEST-POJOIN", "pojoin.pdf",
        _stray_invoice("Harborlight Marine", "INV-2098", "PO-6020", 90.00),
        "text/plain",
    )
    assert record.supplier_id == HARBORLIGHT
    ledger = seeded.supplier_ledger(HARBORLIGHT, AS_OF)
    assert record.record_id in [d.record_id for d in ledger.documents]
    assert seeded.supplier_index(AS_OF).unmatched_count == 0


def test_ledger_renders_the_credit_confirmation_and_break_story(seeded):
    for record_id in ("REC-SUB-017-0", "REC-SUB-018-0", "REC-SUB-019-0",
                      "REC-SUB-024-0"):
        seeded.decide(record_id, "approve", "Test Approver", "test")
    seeded.apply_credit("REC-SUB-024-0", "Test Controller")
    run = seeded.create_payment_run(
        "2026-10-16", record_ids=["REC-SUB-017-0", "REC-SUB-018-0"])
    seeded.decide_payment_run(run.run_id, "release", "Test Treasurer", "test")
    advice = remittance_advice_doc(run.run_id, [
        ("INV-2001", 900.00, "USD", "2026-10-16", "BNK-TEST-01"),
    ])
    (advice_record,) = seeded.submit(
        "SUB-TEST-ADVICE", "advice.pdf", advice.encode("utf-8"), "text/plain")
    seeded.confirm_remittance(advice_record.record_id)
    recon = seeded.reconcile("REC-SUB-020-0")

    ledger = seeded.supplier_ledger(HARBORLIGHT, AS_OF)
    invoices = {i.invoice_number: i for i in ledger.invoices}
    assert invoices["INV-2001"].payment_state == "PAID_PROPOSED"
    assert invoices["INV-2001"].payment_confirmation == "CONFIRMED"
    assert invoices["INV-2002"].payment_confirmation == "MISSING"
    covered = invoices["INV-2003"]
    assert covered.fully_credited and covered.open == 0.00
    assert [c.credit_number for c in covered.credits_applied] == ["CN-2003"]
    credits = {c.credit_number: c for c in ledger.credits}
    assert credits["CN-2003"].credit_state == "APPLIED"
    assert credits["CN-2003"].applied_invoice_record_id == "REC-SUB-019-0"
    assert credits["CN-2002"].credit_state == "UNAPPLIED"
    assert credits["CN-2002"].unapplied_reason  # the reason rides the ledger

    breaks = {(b.invoice_number, b.classification)
              for b in ledger.open_breaks}
    assert breaks == {
        ("INV-2002", "amount_disagrees"),
        ("INV-2999", "missing_in_ap"),
        ("INV-2001", "duplicates_within_statement"),
        ("INV-2003", "missing_on_statement"),
    }
    assert seeded.supplier_index(AS_OF).suppliers[
        [e.supplier_id for e in seeded.supplier_index(AS_OF).suppliers]
        .index(HARBORLIGHT)].open_break_count == 4
    # A controller accepting the reconciliation closes the
    # breaks; the ledger follows the record, not its own memory.
    seeded.decide_reconciliation(
        recon.reconciliation_id, "accept", "Test Controller", "test")
    assert seeded.supplier_ledger(HARBORLIGHT, AS_OF).open_breaks == []


def test_bank_warning_comes_from_the_master_and_the_flags(seeded):
    granite = seeded.supplier_ledger("SUP-07", AS_OF)
    master = seeded.store.reference.suppliers["SUP-07"]
    assert granite.bank.bank_account == master.bank_account
    assert granite.bank.bank_name == master.bank_name
    assert granite.bank.warning is not None
    assert "BANK_DETAILS_CHANGED" in granite.bank.warning
    assert granite.bank.flagged_record_ids == ["REC-SUB-008-0"]
    # The account a document ASKED for never appears as the
    # supplier's bank details — the master is the only source.
    assert "ATTK" not in granite.bank.bank_account

    clean = seeded.supplier_ledger("SUP-02", AS_OF)
    assert clean.bank.warning is None
    index = seeded.supplier_index(AS_OF)
    warnings = {e.supplier_id: e.bank_warning for e in index.suppliers}
    assert warnings["SUP-07"] is True
    assert all(not v for k, v in warnings.items() if k != "SUP-07")


def test_negative_open_breaks_the_ledger_tie_by_name(service):
    # Planted directly, the way the position's own eval plants
    # it: a credit ledger exceeding the face is corruption the
    # credit path refuses to write — and the two views must
    # disagree out loud, not agree quietly.
    from reducto_ap_agent.schemas import (
        CreditApplication, DocumentRecord, ExtractedDocument, ExtractedField,
    )
    service.store.save(DocumentRecord(
        record_id="REC-TEST-NEG", submission_id="SUB-TEST",
        supplier_id="SUP-02", supplier_name="Brightpath Components Inc",
        status="APPROVED", doc_class="invoice",
        extraction=ExtractedDocument(
            doc_class="invoice", currency="USD",
            fields={
                "invoice_number": ExtractedField(value="INV-NEG"),
                "total": ExtractedField(value=400.0),
                "due_date": ExtractedField(value="2026-10-14"),
            }),
        credits_applied=[CreditApplication(
            credit_record_id="REC-TEST-CN", credit_number="CN-NEG",
            invoice_record_id="REC-TEST-NEG", amount=450.0,
            currency="USD", actor="Test Controller")],
    ))
    ledger = service.supplier_ledger("SUP-02", AS_OF)
    (usd,) = ledger.totals
    assert usd.open_total == -50.00
    assert usd.position_open_total == 0.00  # the position excludes it as an anomaly
    assert usd.difference == -50.00 and not usd.tied
    assert ledger.tie_status == "POSITION_TIE_BREAK"


def test_master_supplier_with_no_documents_still_lists(service):
    index = service.supplier_index(AS_OF)
    assert len(index.suppliers) == 12
    empty = next(e for e in index.suppliers if e.supplier_id == "SUP-06")
    assert empty.document_counts == {}
    assert empty.open_totals_by_currency == {}
    ledger = service.supplier_ledger("SUP-06", AS_OF)
    assert ledger.documents == [] and ledger.totals == []
    assert ledger.tie_status == "TIED"


def test_ledger_api(service, seeded):
    client = TestClient(create_app(service))
    index = client.get("/suppliers/index", params={"as_of": AS_OF})
    assert index.status_code == 200
    assert index.json()["unmatched_count"] == 0
    assert len(index.json()["suppliers"]) == 12
    missing = client.get("/suppliers/SUP-99/ledger", params={"as_of": AS_OF})
    assert missing.status_code == 404

    seeded_client = TestClient(create_app(seeded))
    _approve_all_awaiting(seeded)
    ledger = seeded_client.get(
        f"/suppliers/{HARBORLIGHT}/ledger", params={"as_of": AS_OF})
    assert ledger.status_code == 200
    body = ledger.json()
    assert body["supplier_name"] == "Harborlight Marine Supply"
    assert body["tie_status"] == "TIED"
    assert {i["invoice_number"] for i in body["invoices"]} >= {
        "INV-2001", "INV-2002", "INV-2003"}


def test_build_ledger_needs_no_reference_registration(service):
    # build_ledger takes the master entry itself; a supplier is
    # its master record, not a lookup side-effect.
    supplier = Supplier(supplier_id="SUP-XX", name="Nowhere Trading",
                        bank_account="XX00 NONE 0000")
    ledger = build_ledger(supplier, [], [], AS_OF)
    assert ledger.supplier_name == "Nowhere Trading"
    assert ledger.bank.bank_account == "XX00 NONE 0000"
    assert ledger.bank.warning is None
