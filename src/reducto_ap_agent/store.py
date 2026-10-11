"""The record store: processed documents and their decisions.

Two implementations of one small surface — ``save``, ``get``,
``list`` — because the deployment gets PostgreSQL and the test
suite gets a double with identical behaviour:

- :class:`PostgresRecordStore` — the production store (JSONB
  payload in the ``records`` table; migration ``0001``).
- :class:`InMemoryRecordStore` — the double. When it is live,
  ``/readiness`` says so: nothing persists past the process.

The store also carries the reference data the match reads
(suppliers, purchase orders, receipts) — seeded from the world
the deployment configures (the synthetic dataset here; the
customer's ERP mirrors in a real one) and held behind the same
surface so the pipeline never cares where they came from.
"""

from __future__ import annotations

import json
import threading

from .db import database_url, ensure_migrated
from .schemas import DocumentRecord, GoodsReceipt, PurchaseOrder, Supplier


class ReferenceData:
    """Suppliers, POs, receipts — read-only for the pipeline."""

    def __init__(self) -> None:
        self.suppliers: dict[str, Supplier] = {}
        self.suppliers_by_name: dict[str, Supplier] = {}
        self.purchase_orders: dict[str, PurchaseOrder] = {}
        self.receipts_by_po: dict[str, GoodsReceipt] = {}

    def load(self, suppliers: list[Supplier], pos: list[PurchaseOrder],
             receipts: list[GoodsReceipt]) -> None:
        for supplier in suppliers:
            self.suppliers[supplier.supplier_id] = supplier
            self.suppliers_by_name[supplier.name.strip().lower()] = supplier
        for po in pos:
            self.purchase_orders[po.po_number] = po
        for receipt in receipts:
            self.receipts_by_po[receipt.po_number] = receipt

    def supplier_named(self, name: str | None) -> Supplier | None:
        if not name:
            return None
        return self.suppliers_by_name.get(name.strip().lower())


class InMemoryRecordStore:
    name = "memory"

    def __init__(self) -> None:
        self._records: dict[str, DocumentRecord] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self.reference = ReferenceData()

    def save(self, record: DocumentRecord) -> DocumentRecord:
        with self._lock:
            if record.record_id not in self._records:
                self._order.append(record.record_id)
            self._records[record.record_id] = record
        return record

    def get(self, record_id: str) -> DocumentRecord | None:
        return self._records.get(record_id)

    def list(self, status: str | None = None) -> list[DocumentRecord]:
        records = [self._records[rid] for rid in self._order]
        if status:
            records = [r for r in records if r.status == status]
        return records


class PostgresRecordStore:
    name = "postgres"

    def __init__(self, url: str) -> None:
        ensure_migrated(url)
        self._url = url
        self.reference = ReferenceData()

    def _connect(self):
        from .db import connect

        return connect(self._url)

    def save(self, record: DocumentRecord) -> DocumentRecord:
        from psycopg.types.json import Jsonb

        with self._connect() as conn:
            conn.execute(
                "INSERT INTO records (record_id, submission_id, payload, status, supplier_id) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (record_id) DO UPDATE SET payload = EXCLUDED.payload, "
                "status = EXCLUDED.status, supplier_id = EXCLUDED.supplier_id, "
                "updated_at = now()",
                (
                    record.record_id,
                    record.submission_id,
                    Jsonb(json.loads(record.model_dump_json())),
                    record.status,
                    record.supplier_id,
                ),
            )
            conn.commit()
        return record

    def get(self, record_id: str) -> DocumentRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM records WHERE record_id = %s", (record_id,)
            ).fetchone()
        return DocumentRecord.model_validate(row["payload"]) if row else None

    def list(self, status: str | None = None) -> list[DocumentRecord]:
        query = "SELECT payload FROM records"
        params: tuple = ()
        if status:
            query += " WHERE status = %s"
            params = (status,)
        query += " ORDER BY seq"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [DocumentRecord.model_validate(row["payload"]) for row in rows]


def default_store() -> InMemoryRecordStore | PostgresRecordStore:
    """Postgres when DATABASE_URL is set, else the double."""
    url = database_url()
    if url:
        return PostgresRecordStore(url)
    return InMemoryRecordStore()
