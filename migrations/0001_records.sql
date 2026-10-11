-- 0001: the record store — one row per processed (sub-)document.
-- The payload is the full DocumentRecord (extraction with
-- citations, match result, guardrail flags, gate decision, audit
-- trail) as JSONB; the columns exist for identity, ordering, and
-- filtering. `seq` preserves insertion order across re-saves.
CREATE TABLE IF NOT EXISTS records (
    record_id TEXT PRIMARY KEY,
    submission_id TEXT NOT NULL,
    seq BIGINT GENERATED ALWAYS AS IDENTITY,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'RECEIVED',
    supplier_id TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS records_submission_idx ON records (submission_id, seq);
CREATE INDEX IF NOT EXISTS records_status_idx ON records (status, seq);
