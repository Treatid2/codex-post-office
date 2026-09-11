-- SPDX-License-Identifier: MPL-2.0

CREATE TABLE IF NOT EXISTS reviewer_instances (
    reviewer_thread_id TEXT PRIMARY KEY,
    endpoint_id TEXT NOT NULL REFERENCES endpoints(endpoint_id),
    label TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE','RETIRED')),
    rotation_order INTEGER NOT NULL UNIQUE,
    guidance_url TEXT,
    minimum_interval_minutes INTEGER NOT NULL DEFAULT 30 CHECK (minimum_interval_minutes >= 0),
    last_submission_at TEXT,
    registered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    retired_at TEXT
) STRICT;

ALTER TABLE automatic_reviews ADD COLUMN requester_thread_id TEXT;
ALTER TABLE automatic_reviews ADD COLUMN reviewer_thread_id TEXT REFERENCES reviewer_instances(reviewer_thread_id);
ALTER TABLE automatic_reviews ADD COLUMN package_name TEXT;
ALTER TABLE automatic_reviews ADD COLUMN package_size_bytes INTEGER CHECK (package_size_bytes IS NULL OR package_size_bytes >= 0);
ALTER TABLE automatic_reviews ADD COLUMN legacy_status TEXT;

CREATE TABLE IF NOT EXISTS continuation_items (
    continuation_id TEXT PRIMARY KEY,
    source_table TEXT NOT NULL,
    source_id TEXT NOT NULL,
    continuation_kind TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('READY','LEASED','COMPLETED','RETIRED','FAILED_FINAL')),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    evidence_root TEXT NOT NULL CHECK (length(evidence_root) = 64),
    lease_owner_actor_id TEXT REFERENCES actors(actor_id),
    lease_token_sha256 TEXT CHECK (lease_token_sha256 IS NULL OR length(lease_token_sha256) = 64),
    lease_expires_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE (source_table, source_id)
) STRICT;

CREATE TABLE IF NOT EXISTS continuation_receipts (
    receipt_id TEXT PRIMARY KEY,
    continuation_id TEXT NOT NULL REFERENCES continuation_items(continuation_id),
    receipt_kind TEXT NOT NULL CHECK (receipt_kind IN ('LEASED','COMPLETED','RECOVERED','FAILED')),
    evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
    evidence_root TEXT NOT NULL CHECK (length(evidence_root) = 64),
    recorded_at TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_reviewer_instances_rotation
    ON reviewer_instances(status, rotation_order, reviewer_thread_id);
CREATE INDEX IF NOT EXISTS idx_continuation_ready
    ON continuation_items(state, continuation_kind, created_at, continuation_id);

CREATE TRIGGER IF NOT EXISTS continuation_receipts_no_update
BEFORE UPDATE ON continuation_receipts BEGIN
    SELECT RAISE(ABORT, 'continuation_receipts is append-only');
END;

CREATE TRIGGER IF NOT EXISTS continuation_receipts_no_delete
BEFORE DELETE ON continuation_receipts BEGIN
    SELECT RAISE(ABORT, 'continuation_receipts is append-only');
END;
