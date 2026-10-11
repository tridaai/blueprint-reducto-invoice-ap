"""The record store: processed documents and their decisions.

Two implementations of one small surface — ``save``, ``get``,
``list``, plus the same trio for statement reconciliations
and for payment runs — because the deployment gets PostgreSQL
and the test suite gets a double with identical behaviour:

- :class:`PostgresRecordStore` — the production store (JSONB
  payloads in the ``records``, ``reconciliations``, and
  ``payment_runs`` tables; migrations ``0001``–``0003``).
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
from .schemas import (
    DocumentRecord,
    GoodsReceipt,
    PaymentRun,
    PurchaseOrder,
    Reconciliation,
    Supplier,
)


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
        self._recons: dict[str, Reconciliation] = {}
        self._recon_order: list[str] = []
        self._runs: dict[str, PaymentRun] = {}
        self._run_order: list[str] = []
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

    def save_reconciliation(self, recon: Reconciliation) -> Reconciliation:
        with self._lock:
            if recon.reconciliation_id not in self._recons:
                self._recon_order.append(recon.reconciliation_id)
            self._recons[recon.reconciliation_id] = recon
        return recon

    def get_reconciliation(self, reconciliation_id: str) -> Reconciliation | None:
        return self._recons.get(reconciliation_id)

    def list_reconciliations(
        self, supplier_id: str | None = None
    ) -> list[Reconciliation]:
        recons = [self._recons[rid] for rid in self._recon_order]
        if supplier_id:
            recons = [r for r in recons if r.supplier_id == supplier_id]
        return recons

    def save_payment_run(self, run: PaymentRun) -> PaymentRun:
        with self._lock:
            if run.run_id not in self._runs:
                self._run_order.append(run.run_id)
            self._runs[run.run_id] = run
        return run

    def get_payment_run(self, run_id: str) -> PaymentRun | None:
        return self._runs.get(run_id)

    def list_payment_runs(
        self, status: str | None = None
    ) -> list[PaymentRun]:
        runs = [self._runs[rid] for rid in self._run_order]
        if status:
            runs = [r for r in runs if r.status == status]
        return runs


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

    def save_reconciliation(self, recon: Reconciliation) -> Reconciliation:
        from psycopg.types.json import Jsonb

        with self._connect() as conn:
            conn.execute(
                "INSERT INTO reconciliations "
                "(reconciliation_id, statement_record_id, payload, status, supplier_id) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (reconciliation_id) DO UPDATE SET payload = EXCLUDED.payload, "
                "status = EXCLUDED.status, supplier_id = EXCLUDED.supplier_id, "
                "updated_at = now()",
                (
                    recon.reconciliation_id,
                    recon.statement_record_id,
                    Jsonb(json.loads(recon.model_dump_json())),
                    recon.status,
                    recon.supplier_id,
                ),
            )
            conn.commit()
        return recon

    def get_reconciliation(self, reconciliation_id: str) -> Reconciliation | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM reconciliations WHERE reconciliation_id = %s",
                (reconciliation_id,),
            ).fetchone()
        return Reconciliation.model_validate(row["payload"]) if row else None

    def list_reconciliations(
        self, supplier_id: str | None = None
    ) -> list[Reconciliation]:
        query = "SELECT payload FROM reconciliations"
        params: tuple = ()
        if supplier_id:
            query += " WHERE supplier_id = %s"
            params = (supplier_id,)
        query += " ORDER BY seq"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [Reconciliation.model_validate(row["payload"]) for row in rows]

    def save_payment_run(self, run: PaymentRun) -> PaymentRun:
        from psycopg.types.json import Jsonb

        with self._connect() as conn:
            conn.execute(
                "INSERT INTO payment_runs (run_id, value_date, payload, status) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (run_id) DO UPDATE SET payload = EXCLUDED.payload, "
                "status = EXCLUDED.status, updated_at = now()",
                (
                    run.run_id,
                    run.value_date,
                    Jsonb(json.loads(run.model_dump_json())),
                    run.status,
                ),
            )
            conn.commit()
        return run

    def get_payment_run(self, run_id: str) -> PaymentRun | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM payment_runs WHERE run_id = %s", (run_id,)
            ).fetchone()
        return PaymentRun.model_validate(row["payload"]) if row else None

    def list_payment_runs(
        self, status: str | None = None
    ) -> list[PaymentRun]:
        query = "SELECT payload FROM payment_runs"
        params: tuple = ()
        if status:
            query += " WHERE status = %s"
            params = (status,)
        query += " ORDER BY seq"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [PaymentRun.model_validate(row["payload"]) for row in rows]


def default_store() -> InMemoryRecordStore | PostgresRecordStore:
    """Postgres when DATABASE_URL is set, else the double."""
    url = database_url()
    if url:
        return PostgresRecordStore(url)
    return InMemoryRecordStore()
