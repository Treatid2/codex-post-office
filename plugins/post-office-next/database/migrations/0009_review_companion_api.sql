-- SPDX-License-Identifier: MPL-2.0

ALTER TABLE automatic_reviews ADD COLUMN requester_host_id TEXT;
ALTER TABLE automatic_reviews ADD COLUMN subject TEXT;
ALTER TABLE automatic_reviews ADD COLUMN readiness TEXT;
ALTER TABLE automatic_reviews ADD COLUMN idempotency_key TEXT;
ALTER TABLE automatic_reviews ADD COLUMN completed_summary TEXT;
ALTER TABLE automatic_reviews ADD COLUMN withdrawn_at TEXT;
ALTER TABLE automatic_reviews ADD COLUMN withdrawal_reason TEXT;
ALTER TABLE automatic_reviews ADD COLUMN withdrawal_idempotency_key TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_reviews_idempotency_key
    ON automatic_reviews(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_reviews_requester_interval
    ON automatic_reviews(requester_thread_id, queued_at DESC);

CREATE TABLE IF NOT EXISTS review_requester_credentials (
    requester_thread_id TEXT PRIMARY KEY,
    requester_host_id TEXT NOT NULL,
    secret_sha256 TEXT NOT NULL CHECK (length(secret_sha256) = 64),
    status TEXT NOT NULL CHECK (status IN ('ACTIVE','REVOKED')),
    registered_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL
) STRICT;
