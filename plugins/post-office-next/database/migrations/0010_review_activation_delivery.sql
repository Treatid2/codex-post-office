-- SPDX-License-Identifier: MPL-2.0

CREATE TABLE IF NOT EXISTS automatic_review_activations (
    activation_id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL UNIQUE REFERENCES automatic_reviews(review_id),
    activation_dispatch_id TEXT NOT NULL UNIQUE,
    reviewer_thread_id TEXT NOT NULL,
    mailbox_id TEXT NOT NULL,
    mailbox_generation INTEGER NOT NULL CHECK (mailbox_generation >= 1),
    prompt_sha256 TEXT NOT NULL CHECK (length(prompt_sha256) = 64),
    manifest_path TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    state TEXT NOT NULL CHECK (state IN ('PACKET_ISSUED','SENT')),
    idempotency_key TEXT NOT NULL UNIQUE,
    request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64),
    source_message_id TEXT,
    receipt_reference TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (state = 'PACKET_ISSUED' AND source_message_id IS NULL AND receipt_reference IS NULL)
        OR
        (state = 'SENT' AND source_message_id IS NOT NULL AND receipt_reference IS NOT NULL)
    )
) STRICT;

CREATE INDEX IF NOT EXISTS idx_review_activations_state
    ON automatic_review_activations(state, created_at);
