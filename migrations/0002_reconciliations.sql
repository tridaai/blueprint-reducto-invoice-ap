-- 0002: supplier statement reconciliations — one row per
-- reconciliation of one statement against AP's records.
-- Same discipline as 0001: the full Reconciliation (lines with
-- their classifications, summary, controller decision, audit
-- trail) as JSONB; columns exist for identity, ordering, and
-- filtering. Reconciliations are evidence: this table is written
-- by the reconciliation path only, and nothing in it feeds back
-- into the records table or the payment packet.
CREATE TABLE IF NOT EXISTS reconciliations (
    reconciliation_id TEXT PRIMARY KEY,
    statement_record_id TEXT NOT NULL,
    seq BIGINT GENERATED ALWAYS AS IDENTITY,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'AWAITING_CONTROLLER',
    supplier_id TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS reconciliations_supplier_idx ON reconciliations (supplier_id, seq);
CREATE INDEX IF NOT EXISTS reconciliations_statement_idx ON reconciliations (statement_record_id, seq);
