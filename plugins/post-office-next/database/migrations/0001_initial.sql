-- SPDX-License-Identifier: MPL-2.0

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    name TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    applied_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    actor_kind TEXT NOT NULL CHECK (actor_kind IN ('HUMAN','BROWSER','COURIER','ENDPOINT','SYSTEM')),
    role TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE','REVOKED','RETIRED')),
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS exact_author_actions (
    action_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    operation TEXT NOT NULL,
    scope_json TEXT NOT NULL CHECK (json_valid(scope_json)),
    confirmation_sha256 TEXT NOT NULL CHECK (length(confirmation_sha256) = 64),
    recorded_at TEXT NOT NULL,
    consumed_event_id TEXT UNIQUE
) STRICT;

CREATE TABLE IF NOT EXISTS caller_capabilities (
    capability_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    subject_kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    subject_generation INTEGER,
    allowed_operations_json TEXT NOT NULL CHECK (json_valid(allowed_operations_json)),
    secret_sha256 TEXT NOT NULL CHECK (length(secret_sha256) = 64),
    status TEXT NOT NULL CHECK (status IN ('ACTIVE','EXPIRED','REVOKED')),
    expires_at TEXT,
    created_at TEXT NOT NULL,
    revoked_at TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS authority_grants (
    grant_id TEXT PRIMARY KEY,
    grantor_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    recipient_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    allowed_operations_json TEXT NOT NULL CHECK (json_valid(allowed_operations_json)),
    scope_json TEXT NOT NULL CHECK (json_valid(scope_json)),
    classification TEXT NOT NULL CHECK (classification IN ('ONE_SHOT','STANDING')),
    maximum_uses INTEGER CHECK (maximum_uses IS NULL OR maximum_uses > 0),
    remaining_uses INTEGER CHECK (remaining_uses IS NULL OR remaining_uses >= 0),
    status TEXT NOT NULL CHECK (status IN ('ACTIVE','EXHAUSTED','EXPIRED','REVOKED')),
    rationale TEXT NOT NULL,
    source_author_action_id TEXT REFERENCES exact_author_actions(action_id),
    created_event_id TEXT,
    revoked_event_id TEXT,
    expires_at TEXT,
    created_at TEXT NOT NULL,
    CHECK (classification = 'STANDING' OR (maximum_uses IS NOT NULL AND remaining_uses IS NOT NULL))
) STRICT;

CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PROPOSED','PROVISIONING','ACTIVE','PAUSED','PROVISIONING_FAILED','ARCHIVED')),
    local_project_root TEXT NOT NULL,
    local_cas_root TEXT NOT NULL,
    local_backup_root TEXT NOT NULL,
    aggregate_version INTEGER NOT NULL CHECK (aggregate_version > 0),
    aggregate_root TEXT NOT NULL CHECK (length(aggregate_root) = 64),
    created_event_id TEXT,
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS project_mail_domains (
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    mail_domain TEXT NOT NULL UNIQUE,
    PRIMARY KEY (project_id, mail_domain)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS software_packages (
    package_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    owning_project_id TEXT NOT NULL REFERENCES projects(project_id),
    status TEXT NOT NULL CHECK (status IN ('PROPOSED','IN_DEVELOPMENT','CANDIDATE','ACCEPTED','DEPRECATED','RETIRED')),
    aggregate_version INTEGER NOT NULL CHECK (aggregate_version > 0),
    aggregate_root TEXT NOT NULL CHECK (length(aggregate_root) = 64),
    created_event_id TEXT,
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS package_versions (
    package_id TEXT NOT NULL REFERENCES software_packages(package_id),
    version TEXT NOT NULL,
    content_root TEXT NOT NULL CHECK (length(content_root) = 64),
    state TEXT NOT NULL CHECK (state IN ('CANDIDATE','ACCEPTED','DEPRECATED','RETIRED')),
    accepted_event_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (package_id, version)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS interfaces (
    interface_id TEXT NOT NULL,
    version TEXT NOT NULL,
    steward_package_id TEXT NOT NULL REFERENCES software_packages(package_id),
    status TEXT NOT NULL CHECK (status IN ('EXPERIMENTAL','PROVISIONAL','SUPPORTED','DEPRECATED','RETIRED')),
    schema_root TEXT CHECK (schema_root IS NULL OR length(schema_root) = 64),
    fixture_root TEXT CHECK (fixture_root IS NULL OR length(fixture_root) = 64),
    human_guide TEXT NOT NULL,
    replacement_interface_id TEXT,
    created_event_id TEXT,
    PRIMARY KEY (interface_id, version)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    task_kind TEXT NOT NULL,
    objective TEXT NOT NULL,
    mutable_package_id TEXT REFERENCES software_packages(package_id),
    acceptance_contract_root TEXT NOT NULL CHECK (length(acceptance_contract_root) = 64),
    authority_grant_id TEXT NOT NULL REFERENCES authority_grants(grant_id),
    state TEXT NOT NULL CHECK (state IN ('PROPOSED','TRIAGED','AWAITING_AUTHORITY','PROVISIONING','READY','ACTIVE','BLOCKED','RESPONSE_RETURNED','UNDER_REVIEW','ACCEPTED','CORRECTION_REQUIRED','REJECTED','CLOSED','CANCELLED')),
    aggregate_version INTEGER NOT NULL CHECK (aggregate_version > 0),
    aggregate_root TEXT NOT NULL CHECK (length(aggregate_root) = 64),
    created_event_id TEXT,
    created_at TEXT NOT NULL,
    CHECK ((task_kind = 'PACKAGE_DEVELOPMENT' AND mutable_package_id IS NOT NULL)
        OR (task_kind IN ('PACKAGE_REVIEW','PROJECT_MANAGEMENT','INTEGRATION','EVALUATION') AND mutable_package_id IS NULL)
        OR task_kind NOT IN ('PACKAGE_DEVELOPMENT','PACKAGE_REVIEW','PROJECT_MANAGEMENT','INTEGRATION','EVALUATION'))
) STRICT;

CREATE TABLE IF NOT EXISTS endpoints (
    endpoint_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL UNIQUE REFERENCES actors(actor_id),
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    task_id TEXT REFERENCES tasks(task_id),
    role TEXT NOT NULL,
    access_scope_json TEXT NOT NULL CHECK (json_valid(access_scope_json)),
    status TEXT NOT NULL CHECK (status IN ('PROVISIONAL','ACTIVE','REVOKED','RETIRED')),
    startup_root TEXT NOT NULL CHECK (length(startup_root) = 64),
    created_event_id TEXT,
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS mailboxes (
    mailbox_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation > 0),
    mail_domain TEXT NOT NULL REFERENCES project_mail_domains(mail_domain),
    endpoint_id TEXT NOT NULL REFERENCES endpoints(endpoint_id),
    status TEXT NOT NULL CHECK (status IN ('PROVISIONAL','ACTIVE','RETIRED','REVOKED')),
    created_event_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (mailbox_id, generation)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS semantic_cycles (
    cycle_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    scope TEXT NOT NULL,
    opening_author_action_id TEXT NOT NULL REFERENCES exact_author_actions(action_id),
    state TEXT NOT NULL CHECK (state IN ('OPEN','ACTIVE','AWAITING_REVIEW','ACCEPTED','CLOSED')),
    acceptance_author_action_id TEXT REFERENCES exact_author_actions(action_id),
    closure_author_action_id TEXT REFERENCES exact_author_actions(action_id),
    aggregate_version INTEGER NOT NULL CHECK (aggregate_version > 0),
    aggregate_root TEXT NOT NULL CHECK (length(aggregate_root) = 64),
    created_at TEXT NOT NULL,
    CHECK (state <> 'CLOSED' OR closure_author_action_id IS NOT NULL)
) STRICT;

CREATE TABLE IF NOT EXISTS semantic_messages (
    message_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    mail_domain TEXT NOT NULL REFERENCES project_mail_domains(mail_domain),
    message_type TEXT NOT NULL,
    sender_endpoint_id TEXT NOT NULL REFERENCES endpoints(endpoint_id),
    recipient_mailbox_id TEXT NOT NULL,
    recipient_generation INTEGER NOT NULL,
    semantic_cycle_id TEXT NOT NULL REFERENCES semantic_cycles(cycle_id),
    authority_grant_id TEXT REFERENCES authority_grants(grant_id),
    exact_author_action_id TEXT REFERENCES exact_author_actions(action_id),
    requested_action TEXT NOT NULL,
    completion_criteria TEXT NOT NULL,
    content_root TEXT NOT NULL CHECK (length(content_root) = 64),
    state TEXT NOT NULL CHECK (state IN ('DRAFT','BUNDLED','CUSTODY_RECORDED','REGISTERED','DELIVERED','ACKNOWLEDGED','RESPONSE_RETURNED','REVIEWED','CLOSED','QUARANTINED','SUPERSEDED_BEFORE_REGISTRATION','CANCELLED_BEFORE_DELIVERY','BLOCKED')),
    aggregate_version INTEGER NOT NULL CHECK (aggregate_version > 0),
    aggregate_root TEXT NOT NULL CHECK (length(aggregate_root) = 64),
    registered_event_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (recipient_mailbox_id, recipient_generation) REFERENCES mailboxes(mailbox_id, generation),
    CHECK ((authority_grant_id IS NOT NULL) <> (exact_author_action_id IS NOT NULL))
) STRICT;

CREATE TABLE IF NOT EXISTS message_relations (
    message_id TEXT NOT NULL REFERENCES semantic_messages(message_id),
    related_message_id TEXT NOT NULL REFERENCES semantic_messages(message_id),
    relation_kind TEXT NOT NULL,
    PRIMARY KEY (message_id, related_message_id, relation_kind)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS message_bundles (
    bundle_id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES semantic_messages(message_id),
    canonical_filename TEXT NOT NULL,
    bundle_version INTEGER NOT NULL CHECK (bundle_version > 0),
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    sha256 TEXT NOT NULL UNIQUE CHECK (length(sha256) = 64),
    manifest_root TEXT NOT NULL CHECK (length(manifest_root) = 64),
    state TEXT NOT NULL CHECK (state IN ('CREATED','VERIFIED','SUPERSEDED_BEFORE_REGISTRATION','REGISTERED','QUARANTINED')),
    supersedes_bundle_id TEXT REFERENCES message_bundles(bundle_id),
    created_at TEXT NOT NULL,
    UNIQUE (message_id, bundle_version)
) STRICT;

CREATE TABLE IF NOT EXISTS bundle_payloads (
    bundle_id TEXT NOT NULL REFERENCES message_bundles(bundle_id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    relative_path TEXT NOT NULL CHECK (relative_path NOT LIKE '/%' AND relative_path NOT LIKE '%\%' AND relative_path NOT LIKE '%../%'),
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    PRIMARY KEY (bundle_id, ordinal),
    UNIQUE (bundle_id, relative_path)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS storage_copies (
    storage_copy_id TEXT PRIMARY KEY,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    location_kind TEXT NOT NULL CHECK (location_kind IN ('LOCAL_CAS','LOCAL_BACKUP','DATABASE_BLOB')),
    local_location TEXT NOT NULL CHECK (
        length(local_location) > 0
        AND local_location NOT LIKE '/%'
        AND instr(local_location, '\') = 0
        AND instr(local_location, ':') = 0
        AND local_location <> '..'
        AND local_location NOT LIKE '../%'
        AND local_location NOT LIKE '%/../%'
        AND local_location NOT LIKE '%/..'
    ),
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    retention_class TEXT NOT NULL CHECK (retention_class IN ('AUTHORITATIVE','BACKUP')),
    verification_level TEXT NOT NULL CHECK (verification_level IN ('SHA256_WRITE','SHA256_READBACK')),
    verified_at TEXT NOT NULL,
    UNIQUE (content_sha256, location_kind, local_location),
    UNIQUE (storage_copy_id, content_sha256)
) STRICT;

CREATE TABLE IF NOT EXISTS transport_attempts (
    transport_attempt_id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES semantic_messages(message_id),
    bundle_id TEXT NOT NULL REFERENCES message_bundles(bundle_id),
    source TEXT NOT NULL,
    destination TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('PENDING','COLLECTED','VALIDATED','STORED','DELIVERED','RECEIPTED','FAILED_RETRYABLE','FAILED_FINAL','QUARANTINED','TOMBSTONED_DUPLICATE','CANCELLED')),
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    canonical_attempt_id TEXT REFERENCES transport_attempts(transport_attempt_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (message_id, bundle_id, attempt_number)
) STRICT;

CREATE TABLE IF NOT EXISTS browser_bridge_transfers (
    bridge_transfer_id TEXT PRIMARY KEY,
    transport_attempt_id TEXT NOT NULL UNIQUE REFERENCES transport_attempts(transport_attempt_id),
    direction TEXT NOT NULL CHECK (direction IN ('TO_BROWSER','FROM_BROWSER')),
    provider TEXT NOT NULL CHECK (provider = 'GOOGLE_DRIVE'),
    remote_object_id TEXT NOT NULL,
    expected_sha256 TEXT NOT NULL CHECK (length(expected_sha256) = 64),
    observed_sha256 TEXT CHECK (observed_sha256 IS NULL OR length(observed_sha256) = 64),
    durable_storage_copy_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('PENDING','TRANSFERRED','INGESTED','RECEIPTED','CLEANUP_DUE','CLEANED','FAILED')),
    access_count INTEGER NOT NULL DEFAULT 0 CHECK (access_count >= 0),
    latency_ms INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),
    cleanup_due_at TEXT,
    terminal_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (durable_storage_copy_id, expected_sha256)
        REFERENCES storage_copies(storage_copy_id, content_sha256)
) STRICT;

CREATE TABLE IF NOT EXISTS acknowledgements (
    acknowledgement_id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES semantic_messages(message_id),
    recipient_endpoint_id TEXT NOT NULL REFERENCES endpoints(endpoint_id),
    payload_hashes_json TEXT NOT NULL CHECK (json_valid(payload_hashes_json)),
    evidence_root TEXT NOT NULL CHECK (length(evidence_root) = 64),
    acknowledged_at TEXT NOT NULL,
    UNIQUE (message_id, recipient_endpoint_id)
) STRICT;

CREATE TABLE IF NOT EXISTS idempotency_records (
    request_id TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    canonical_request_sha256 TEXT NOT NULL CHECK (length(canonical_request_sha256) = 64),
    result_json TEXT NOT NULL CHECK (json_valid(result_json)),
    event_id TEXT,
    recorded_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS leases (
    lease_id TEXT PRIMARY KEY,
    task_id TEXT REFERENCES tasks(task_id),
    message_id TEXT REFERENCES semantic_messages(message_id),
    owner_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    state TEXT NOT NULL CHECK (state IN ('ACTIVE','EXPIRED','RELEASED','COMPLETED')),
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    released_at TEXT,
    CHECK (task_id IS NOT NULL OR message_id IS NOT NULL)
) STRICT;

CREATE TABLE IF NOT EXISTS hub_events (
    sequence INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    operation TEXT NOT NULL,
    capability_id TEXT NOT NULL REFERENCES caller_capabilities(capability_id),
    authority_grant_id TEXT REFERENCES authority_grants(grant_id),
    exact_author_action_id TEXT REFERENCES exact_author_actions(action_id),
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    aggregate_version INTEGER NOT NULL CHECK (aggregate_version > 0),
    before_state_root TEXT NOT NULL CHECK (length(before_state_root) = 64),
    after_state_root TEXT NOT NULL CHECK (length(after_state_root) = 64),
    result_json TEXT NOT NULL CHECK (json_valid(result_json)),
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    previous_event_sha256 TEXT,
    event_sha256 TEXT NOT NULL UNIQUE CHECK (length(event_sha256) = 64),
    CHECK ((authority_grant_id IS NOT NULL) <> (exact_author_action_id IS NOT NULL)),
    CHECK (previous_event_sha256 IS NULL OR length(previous_event_sha256) = 64),
    UNIQUE (aggregate_type, aggregate_id, aggregate_version)
) STRICT;

CREATE TRIGGER IF NOT EXISTS hub_events_no_update
BEFORE UPDATE ON hub_events BEGIN
    SELECT RAISE(ABORT, 'hub_events is append-only');
END;

CREATE TRIGGER IF NOT EXISTS hub_events_no_delete
BEFORE DELETE ON hub_events BEGIN
    SELECT RAISE(ABORT, 'hub_events is append-only');
END;

CREATE TABLE IF NOT EXISTS projection_cursors (
    projection_id TEXT PRIMARY KEY,
    projection_kind TEXT NOT NULL CHECK (projection_kind IN ('DATABASE_VIEW','LOCAL_REPORT','HUMAN_DASHBOARD')),
    source_event_sequence INTEGER NOT NULL DEFAULT 0,
    source_root TEXT NOT NULL CHECK (length(source_root) = 64),
    projection_root TEXT NOT NULL CHECK (length(projection_root) = 64),
    stale INTEGER NOT NULL CHECK (stale IN (0,1)),
    last_successful_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS migration_runs (
    migration_run_id TEXT PRIMARY KEY,
    source_schema_version INTEGER NOT NULL,
    source_capture_root TEXT NOT NULL CHECK (length(source_capture_root) = 64),
    target_schema_version INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('PLANNED','IMPORTING','VERIFYING','VERIFIED','FAILED','ACTIVATED')),
    imported_counts_json TEXT NOT NULL CHECK (json_valid(imported_counts_json)),
    verification_root TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS migration_identity_map (
    migration_run_id TEXT NOT NULL REFERENCES migration_runs(migration_run_id),
    legacy_entity_type TEXT NOT NULL,
    legacy_entity_id TEXT NOT NULL,
    target_entity_type TEXT NOT NULL,
    target_entity_id TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL CHECK (length(evidence_sha256) = 64),
    PRIMARY KEY (migration_run_id, legacy_entity_type, legacy_entity_id),
    UNIQUE (migration_run_id, target_entity_type, target_entity_id)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS migration_anomalies (
    anomaly_id TEXT PRIMARY KEY,
    migration_run_id TEXT NOT NULL REFERENCES migration_runs(migration_run_id),
    classification TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
    resolution_state TEXT NOT NULL CHECK (resolution_state IN ('OPEN','REVIEWED','RESOLVED','ACCEPTED_AS_GAP')),
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS backup_receipts (
    backup_id TEXT PRIMARY KEY,
    database_sha256 TEXT NOT NULL CHECK (length(database_sha256) = 64),
    payload_manifest_root TEXT NOT NULL CHECK (length(payload_manifest_root) = 64),
    schema_version INTEGER NOT NULL,
    event_chain_root TEXT NOT NULL CHECK (length(event_chain_root) = 64),
    local_location TEXT NOT NULL,
    verified_at TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_messages_recipient ON semantic_messages(recipient_mailbox_id, recipient_generation, state);
CREATE INDEX IF NOT EXISTS idx_messages_cycle ON semantic_messages(semantic_cycle_id, state);
CREATE INDEX IF NOT EXISTS idx_transport_state ON transport_attempts(state, updated_at);
CREATE INDEX IF NOT EXISTS idx_bridge_state ON browser_bridge_transfers(state, cleanup_due_at);
CREATE INDEX IF NOT EXISTS idx_events_aggregate ON hub_events(aggregate_type, aggregate_id, aggregate_version);
CREATE INDEX IF NOT EXISTS idx_events_operation ON hub_events(operation, occurred_at);
CREATE INDEX IF NOT EXISTS idx_tasks_project_state ON tasks(project_id, state);
