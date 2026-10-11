"""The record store double and the migration discipline."""

from __future__ import annotations

from reducto_ap_agent import db
from reducto_ap_agent.schemas import DocumentRecord, Reconciliation
from reducto_ap_agent.store import InMemoryRecordStore


def test_in_memory_store_round_trip_and_order():
    store = InMemoryRecordStore()
    first = DocumentRecord(record_id="REC-1", submission_id="SUB-1", status="RECEIVED")
    second = DocumentRecord(record_id="REC-2", submission_id="SUB-1", status="APPROVED")
    store.save(first)
    store.save(second)
    store.save(DocumentRecord(record_id="REC-1", submission_id="SUB-1", status="APPROVED"))
    assert [r.record_id for r in store.list()] == ["REC-1", "REC-2"]  # re-save keeps position
    assert store.get("REC-1").status == "APPROVED"
    assert [r.record_id for r in store.list(status="APPROVED")] == ["REC-1", "REC-2"]
    assert store.get("REC-MISSING") is None


def test_in_memory_store_reconciliation_round_trip():
    store = InMemoryRecordStore()
    recon = Reconciliation(
        reconciliation_id="RECON-REC-1", statement_record_id="REC-1",
        submission_id="SUB-1", supplier_id="SUP-01",
    )
    store.save_reconciliation(recon)
    store.save_reconciliation(
        Reconciliation(
            reconciliation_id="RECON-REC-2", statement_record_id="REC-2",
            submission_id="SUB-2", supplier_id="SUP-02",
        )
    )
    assert store.get_reconciliation("RECON-REC-1") == recon
    assert store.get_reconciliation("RECON-MISSING") is None
    assert [r.reconciliation_id for r in store.list_reconciliations()] == [
        "RECON-REC-1", "RECON-REC-2",
    ]
    assert [
        r.reconciliation_id
        for r in store.list_reconciliations(supplier_id="SUP-02")
    ] == ["RECON-REC-2"]
    # Re-saving (a controller decision) keeps position, updates payload.
    decided = recon.model_copy(update={"status": "ACCEPTED"})
    store.save_reconciliation(decided)
    assert [r.reconciliation_id for r in store.list_reconciliations()] == [
        "RECON-REC-1", "RECON-REC-2",
    ]
    assert store.get_reconciliation("RECON-REC-1").status == "ACCEPTED"


def test_migrations_are_numbered_and_runner_is_safe_without_a_database(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    files = db.migration_files()
    assert files, "at least one migration ships"
    names = [f.name for f in files]
    assert names == sorted(names)
    assert names[0].startswith("0001")
    assert db.run_migrations() == []  # no DATABASE_URL → nothing applied, no error


def test_reference_data_lookup_is_case_insensitive(world):
    from reducto_ap_agent.store import ReferenceData

    reference = ReferenceData()
    reference.load(world.suppliers, world.purchase_orders, world.receipts)
    supplier = reference.supplier_named("brightpath components inc")
    assert supplier is not None and supplier.supplier_id == "SUP-02"
    assert reference.supplier_named("Nobody GmbH") is None
    assert reference.receipts_by_po.get("PO-6016") is None  # planted: no receipt
