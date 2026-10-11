-- 0003: payment runs — approved records assembled into one
-- controlled batch, decided by a named treasurer, and (on
-- release) exported as a bank-instruction artifact. Same
-- discipline as 0001/0002: the full PaymentRun (member lines,
-- named refusals, per-currency totals, treasurer decision,
-- export key + digest, audit trail) as JSONB; columns exist
-- for identity, ordering, and filtering. A payment run moves
-- no money: it is a proposal with a gate on it, and this
-- table is written by the payment-run path only.
CREATE TABLE IF NOT EXISTS payment_runs (
    run_id TEXT PRIMARY KEY,
    value_date TEXT NOT NULL,
    seq BIGINT GENERATED ALWAYS AS IDENTITY,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'AWAITING_TREASURER',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS payment_runs_status_idx ON payment_runs (status, seq);
