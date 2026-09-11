-- SPDX-License-Identifier: MPL-2.0

ALTER TABLE software_packages ADD COLUMN source_location TEXT;
ALTER TABLE software_packages ADD COLUMN licence TEXT;

CREATE TABLE IF NOT EXISTS package_interfaces (
    package_id TEXT NOT NULL REFERENCES software_packages(package_id),
    interface_id TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('PROVIDES','REQUIRES')),
    PRIMARY KEY (package_id, interface_id, direction)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS interface_participants (
    interface_id TEXT NOT NULL,
    version TEXT NOT NULL,
    package_id TEXT NOT NULL REFERENCES software_packages(package_id),
    relation TEXT NOT NULL CHECK (relation IN ('PROVIDER','CONSUMER')),
    PRIMARY KEY (interface_id, version, package_id, relation),
    FOREIGN KEY (interface_id, version) REFERENCES interfaces(interface_id, version)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS workflow_aggregates (
    aggregate_type TEXT NOT NULL CHECK (aggregate_type IN (
        'CapabilityRequest','ChangeSet','ContextBundle','IntegrationCandidate'
    )),
    aggregate_id TEXT NOT NULL,
    project_id TEXT REFERENCES projects(project_id),
    task_id TEXT REFERENCES tasks(task_id),
    state TEXT NOT NULL,
    state_json TEXT NOT NULL CHECK (json_valid(state_json)),
    aggregate_version INTEGER NOT NULL CHECK (aggregate_version > 0),
    aggregate_root TEXT NOT NULL CHECK (length(aggregate_root) = 64),
    created_event_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (aggregate_type, aggregate_id)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS message_plans (
    plan_id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL UNIQUE,
    bundle_id TEXT NOT NULL UNIQUE,
    semantic_message_json TEXT NOT NULL CHECK (json_valid(semantic_message_json)),
    bundle_json TEXT NOT NULL CHECK (json_valid(bundle_json)),
    plan_root TEXT NOT NULL CHECK (length(plan_root) = 64),
    state TEXT NOT NULL CHECK (state IN ('PLANNED','REGISTERED','SUPERSEDED')),
    aggregate_version INTEGER NOT NULL CHECK (aggregate_version > 0),
    aggregate_root TEXT NOT NULL CHECK (length(aggregate_root) = 64),
    created_event_id TEXT,
    registered_event_id TEXT,
    created_at TEXT NOT NULL,
    registered_at TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS message_decisions (
    decision_id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES semantic_messages(message_id),
    decision TEXT NOT NULL CHECK (decision IN ('ACCEPTED','CORRECTION_REQUIRED','REJECTED')),
    rationale TEXT NOT NULL,
    event_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS message_closures (
    message_id TEXT PRIMARY KEY REFERENCES semantic_messages(message_id),
    summary TEXT NOT NULL,
    event_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS cycle_notes (
    cycle_id TEXT NOT NULL REFERENCES semantic_cycles(cycle_id),
    note_kind TEXT NOT NULL,
    note TEXT NOT NULL,
    event_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (cycle_id, note_kind, event_id)
) STRICT, WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_workflow_project_state
    ON workflow_aggregates(project_id, aggregate_type, state);
CREATE INDEX IF NOT EXISTS idx_workflow_task_state
    ON workflow_aggregates(task_id, aggregate_type, state);
CREATE INDEX IF NOT EXISTS idx_message_plans_state
    ON message_plans(state, message_id);
