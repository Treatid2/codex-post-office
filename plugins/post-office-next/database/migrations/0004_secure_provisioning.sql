-- SPDX-License-Identifier: MPL-2.0

ALTER TABLE projects ADD COLUMN policy_root TEXT
    CHECK (policy_root IS NULL OR length(policy_root) = 64);

CREATE TABLE IF NOT EXISTS provisioning_plans (
    plan_id TEXT PRIMARY KEY,
    aggregate_type TEXT NOT NULL CHECK (aggregate_type IN ('Project','Task')),
    aggregate_id TEXT NOT NULL,
    authority_id TEXT NOT NULL,
    requested_resources_json TEXT NOT NULL CHECK (json_valid(requested_resources_json)),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    plan_root TEXT NOT NULL CHECK (length(plan_root) = 64),
    stage TEXT NOT NULL CHECK (stage IN (
        'PLAN','AUTHORISED','IDS_RESERVED','EXTERNAL_PENDING','VERIFIED','COMMITTED',
        'READY','COMPENSATING','ROLLED_BACK','ORPHANED_REVIEW'
    )),
    aggregate_version INTEGER NOT NULL CHECK (aggregate_version > 0),
    aggregate_root TEXT NOT NULL CHECK (length(aggregate_root) = 64),
    created_event_id TEXT,
    committed_event_id TEXT,
    created_at TEXT NOT NULL,
    committed_at TEXT,
    UNIQUE (aggregate_type, aggregate_id),
    UNIQUE (plan_root)
) STRICT;

CREATE TABLE IF NOT EXISTS task_endpoint_bindings (
    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
    endpoint_id TEXT NOT NULL UNIQUE REFERENCES endpoints(endpoint_id),
    mailbox_id TEXT NOT NULL,
    mailbox_generation INTEGER NOT NULL CHECK (mailbox_generation > 0),
    bound_event_id TEXT NOT NULL,
    bound_at TEXT NOT NULL,
    FOREIGN KEY (mailbox_id, mailbox_generation)
        REFERENCES mailboxes(mailbox_id, generation)
) STRICT;

CREATE TABLE IF NOT EXISTS task_responses (
    response_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    message_id TEXT NOT NULL,
    evidence_roots_json TEXT NOT NULL CHECK (json_valid(evidence_roots_json)),
    recorded_event_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (task_id, message_id)
) STRICT;

CREATE TABLE IF NOT EXISTS task_reviews (
    review_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    decision TEXT NOT NULL CHECK (decision IN ('ACCEPTED','CORRECTION_REQUIRED','REJECTED')),
    rationale TEXT NOT NULL,
    recorded_event_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_provisioning_plans_target
    ON provisioning_plans(aggregate_type, aggregate_id, stage);
CREATE INDEX IF NOT EXISTS idx_task_responses_task
    ON task_responses(task_id, recorded_at);
CREATE INDEX IF NOT EXISTS idx_task_reviews_task
    ON task_reviews(task_id, recorded_at);
