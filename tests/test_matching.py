"""The three-way match, case by case."""

from __future__ import annotations

from reducto_ap_agent.datasets import build_world
from reducto_ap_agent.matching import match_invoice
from reducto_ap_agent.offline_docs import OfflineDocumentAdapter


def _extraction(submission_id: str):
    world = build_world()
    adapter = OfflineDocumentAdapter()
    sub = next(s for s in world.submissions if s.submission_id == submission_id)
    uploaded = adapter.upload(sub.filename, sub.text.encode(), "text/plain")
    return world, adapter.extract(adapter.split(uploaded)[0])


def _po_receipt(world, po_number: str):
    po = next(p for p in world.purchase_orders if p.po_number == po_number)
    receipt = next((r for r in world.receipts if r.po_number == po_number), None)
    return po, receipt


def test_clean_match():
    world, ext = _extraction("SUB-001")
    po, rcv = _po_receipt(world, "PO-6001")
    result = match_invoice(ext, po, rcv)
    assert result.status == "MATCHED"
    assert result.exception_codes == []


def test_price_inside_tolerance_is_shown_not_buried():
    world, ext = _extraction("SUB-002")
    po, rcv = _po_receipt(world, "PO-6002")
    result = match_invoice(ext, po, rcv)
    assert result.status == "MATCHED_WITH_TOLERANCE"
    assert any("inside tolerance" in n for n in result.notes)


def test_price_outside_tolerance_is_an_exception():
    world, ext = _extraction("SUB-003")
    po, rcv = _po_receipt(world, "PO-6003")
    result = match_invoice(ext, po, rcv)
    assert result.status == "EXCEPTION"
    assert result.exception_codes == ["PRICE_VARIANCE"]


def test_short_shipment_is_a_quantity_exception():
    world, ext = _extraction("SUB-004")
    po, rcv = _po_receipt(world, "PO-6004")
    result = match_invoice(ext, po, rcv)
    assert result.exception_codes == ["QUANTITY_VARIANCE"]
    line = result.line_matches[0]
    assert line.invoiced_qty == 20 and line.received_qty == 14


def test_no_po_reference_and_unknown_po_and_no_receipt():
    world, ext = _extraction("SUB-005")
    assert match_invoice(ext, None, None).exception_codes == ["NO_PO_REFERENCE"]

    world, ext = _extraction("SUB-012")
    assert match_invoice(ext, None, None).exception_codes == ["PO_NOT_FOUND"]

    world, ext = _extraction("SUB-016")
    po, rcv = _po_receipt(world, "PO-6016")
    assert rcv is None
    assert match_invoice(ext, po, rcv).exception_codes == ["NO_RECEIPT"]


def test_stated_total_disagreeing_with_lines_is_total_mismatch():
    world, ext = _extraction("SUB-011")
    po, rcv = _po_receipt(world, "PO-6011")
    result = match_invoice(ext, po, rcv)
    assert result.exception_codes == ["TOTAL_MISMATCH"]


def test_unlisted_line_is_named_not_absorbed():
    world, ext = _extraction("SUB-015")
    po, rcv = _po_receipt(world, "PO-6002")
    result = match_invoice(ext, po, rcv)
    assert result.exception_codes == ["LINE_NOT_ON_PO"]
    missing = [lm for lm in result.line_matches if lm.verdict == "missing"]
    assert missing and missing[0].sku == "SKU-Z9"
