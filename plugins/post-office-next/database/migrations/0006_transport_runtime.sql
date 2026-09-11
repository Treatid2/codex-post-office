-- SPDX-License-Identifier: MPL-2.0

CREATE TABLE IF NOT EXISTS transport_dispatches (
    dispatch_id TEXT PRIMARY KEY,
    transport_attempt_id TEXT NOT NULL UNIQUE REFERENCES transport_attempts(transport_attempt_id),
    channel TEXT NOT NULL CHECK (channel IN ('NATIVE_TASK','PLAYWRIGHT_BROWSER')),
    destination_endpoint_id TEXT NOT NULL REFERENCES endpoints(endpoint_id),
    destination_mailbox_id TEXT NOT NULL,
    destination_generation INTEGER NOT NULL CHECK (destination_generation > 0),
    state TEXT NOT NULL CHECK (state IN (
        'READY','LEASED','SENT','RECEIPTED','WAITING_FOR_RECIPIENT',
        'RECONCILIATION_REQUIRED','TERMINAL_FAILURE','CANCELLED'
    )),
    lease_owner_actor_id TEXT REFERENCES actors(actor_id),
    lease_token_sha256 TEXT CHECK (lease_token_sha256 IS NULL OR length(lease_token_sha256) = 64),
    lease_expires_at TEXT,
    observable_marker TEXT NOT NULL,
    observed_receipt_id TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at TEXT NOT NULL,
    last_error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (destination_mailbox_id, destination_generation)
        REFERENCES mailboxes(mailbox_id, generation)
) STRICT;

CREATE TABLE IF NOT EXISTS runtime_receipts (
    receipt_id TEXT PRIMARY KEY,
    dispatch_id TEXT NOT NULL REFERENCES transport_dispatches(dispatch_id),
    receipt_kind TEXT NOT NULL CHECK (receipt_kind IN (
        'LEASED','OBSERVED_SENT','DELIVERED','RECOVERED','FAILED','CANCELLED'
    )),
    evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
    evidence_root TEXT NOT NULL CHECK (length(evidence_root) = 64),
    recorded_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS attention_items (
    attention_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('INFO','WARNING','ERROR','CRITICAL')),
    reason_code TEXT NOT NULL,
    details_json TEXT NOT NULL CHECK (json_valid(details_json)),
    state TEXT NOT NULL CHECK (state IN ('OPEN','RESOLVED')),
    opened_at TEXT NOT NULL,
    resolved_at TEXT,
    UNIQUE (entity_type, entity_id, reason_code, state)
) STRICT;

CREATE TABLE IF NOT EXISTS automatic_reviews (
    review_id TEXT PRIMARY KEY,
    semantic_message_id TEXT NOT NULL UNIQUE REFERENCES semantic_messages(message_id),
    requester_task_id TEXT NOT NULL REFERENCES tasks(task_id),
    reviewer_endpoint_id TEXT NOT NULL REFERENCES endpoints(endpoint_id),
    package_sha256 TEXT NOT NULL CHECK (length(package_sha256) = 64),
    state TEXT NOT NULL CHECK (state IN (
        'QUEUED','ACTIVE','RETURNED','EVALUATED','WITHDRAWN','SUPERSEDED','FAILED_FINAL'
    )),
    queued_at TEXT NOT NULL,
    activated_at TEXT,
    returned_at TEXT,
    evaluated_at TEXT,
    result_message_id TEXT UNIQUE REFERENCES semantic_messages(message_id),
    wake_dispatch_id TEXT REFERENCES transport_dispatches(dispatch_id),
    UNIQUE (requester_task_id, package_sha256)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_transport_dispatch_ready
    ON transport_dispatches(state, next_attempt_at, created_at);
CREATE INDEX IF NOT EXISTS idx_attention_open
    ON attention_items(state, severity, opened_at);
CREATE INDEX IF NOT EXISTS idx_reviews_fifo
    ON automatic_reviews(state, queued_at, review_id);

CREATE TRIGGER IF NOT EXISTS runtime_receipts_no_update
BEFORE UPDATE ON runtime_receipts BEGIN
    SELECT RAISE(ABORT, 'runtime_receipts is append-only');
END;

CREATE TRIGGER IF NOT EXISTS runtime_receipts_no_delete
BEFORE DELETE ON runtime_receipts BEGIN
    SELECT RAISE(ABORT, 'runtime_receipts is append-only');
END;
