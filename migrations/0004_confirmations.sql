-- 0004: remittance confirmations — one row per reconciliation
-- of one remittance advice against the payment run it answers.
-- Same discipline as 0001–0003: the full PaymentConfirmation
-- (classified lines, the per-currency summary, controller
-- decision, audit trail) as JSONB; columns exist for identity,
-- ordering, and filtering. A confirmation is evidence over a
-- run: this table is written by the confirmation path only,
-- and nothing in it feeds back into the records it classifies
-- beyond the confirmation overlay the service stamps there.
-- run_id is nullable: an advice that names no run on file is
-- held UNMATCHED on its confirmation record, not dropped.
CREATE TABLE IF NOT EXISTS confirmations (
    confirmation_id TEXT PRIMARY KEY,
    advice_record_id TEXT NOT NULL,
    run_id TEXT,
    seq BIGINT GENERATED ALWAYS AS IDENTITY,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'AWAITING_CONTROLLER',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS confirmations_run_idx ON confirmations (run_id, seq);
CREATE INDEX IF NOT EXISTS confirmations_advice_idx ON confirmations (advice_record_id, seq);
