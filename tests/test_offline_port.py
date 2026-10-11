"""The offline adapter, held to the port contract."""

from __future__ import annotations

from reducto_ap_agent.offline_docs import OfflineDocumentAdapter


def test_upload_split_classify_extract_invoice(world):
    adapter = OfflineDocumentAdapter()
    sub = world.submissions[0]
    uploaded = adapter.upload(sub.filename, sub.text.encode(), "text/plain")
    parts = adapter.split(uploaded)
    assert len(parts) == 1
    assert adapter.classify(parts[0]).doc_class == "invoice"
    extraction = adapter.extract(parts[0])
    assert extraction.value("invoice_number") == "INV-1001"
    assert extraction.value("total") == 1400.00
    assert extraction.value("po_reference") == "PO-6001"
    assert len(extraction.lines) == 2
    # Every extracted field carries a text-layer citation.
    for field in extraction.fields.values():
        assert field.citation is not None
        assert field.citation.source == "text_layer"


def test_split_bundle_into_invoice_and_attachment(world):
    adapter = OfflineDocumentAdapter()
    bundle = next(s for s in world.submissions if s.submission_id == "SUB-010")
    uploaded = adapter.upload(bundle.filename, bundle.text.encode(), "text/plain")
    parts = adapter.split(uploaded)
    assert len(parts) == 2
    classes = [adapter.classify(p).doc_class for p in parts]
    assert classes == ["invoice", "remittance_advice"]
    assert parts[1].page_start == 2


def test_extract_credit_note_keeps_its_sign(world):
    adapter = OfflineDocumentAdapter()
    sub = next(s for s in world.submissions if s.submission_id == "SUB-007")
    uploaded = adapter.upload(sub.filename, sub.text.encode(), "text/plain")
    extraction = adapter.extract(adapter.split(uploaded)[0])
    assert extraction.value("invoice_number") == "CN-1001"
    assert extraction.value("total") == -200.00
