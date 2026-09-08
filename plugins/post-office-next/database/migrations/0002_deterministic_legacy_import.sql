-- SPDX-License-Identifier: MPL-2.0

CREATE TABLE IF NOT EXISTS migration_sources (
    migration_run_id TEXT PRIMARY KEY REFERENCES migration_runs(migration_run_id),
    capture_manifest_sha256 TEXT NOT NULL CHECK (length(capture_manifest_sha256) = 64),
    capture_root TEXT NOT NULL CHECK (length(capture_root) = 64),
    baseline_payload_manifest_sha256 TEXT NOT NULL CHECK (length(baseline_payload_manifest_sha256) = 64),
    delta_payload_manifest_sha256 TEXT NOT NULL CHECK (length(delta_payload_manifest_sha256) = 64),
    source_database_roots_json TEXT NOT NULL CHECK (json_valid(source_database_roots_json)),
    source_event_boundary_json TEXT NOT NULL CHECK (json_valid(source_event_boundary_json)),
    source_identity_root TEXT NOT NULL CHECK (length(source_identity_root) = 64),
    evidence_only INTEGER NOT NULL CHECK (evidence_only = 1),
    imported_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS migration_source_artifacts (
    migration_run_id TEXT NOT NULL REFERENCES migration_runs(migration_run_id),
    artifact_kind TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    source_name TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    storage_copy_id TEXT NOT NULL,
    PRIMARY KEY (migration_run_id, artifact_kind, ordinal),
    FOREIGN KEY (storage_copy_id, sha256) REFERENCES storage_copies(storage_copy_id, content_sha256)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS legacy_import_tables (
    migration_run_id TEXT NOT NULL REFERENCES migration_runs(migration_run_id),
    source_database TEXT NOT NULL CHECK (source_database IN ('hub','observer')),
    table_name TEXT NOT NULL,
    columns_json TEXT NOT NULL CHECK (json_valid(columns_json)),
    row_count INTEGER NOT NULL CHECK (row_count >= 0),
    rows_root TEXT NOT NULL CHECK (length(rows_root) = 64),
    PRIMARY KEY (migration_run_id, source_database, table_name)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS legacy_import_rows (
    migration_run_id TEXT NOT NULL,
    source_database TEXT NOT NULL,
    table_name TEXT NOT NULL,
    row_ordinal INTEGER NOT NULL CHECK (row_ordinal >= 0),
    primary_key_json TEXT NOT NULL CHECK (json_valid(primary_key_json)),
    row_json TEXT NOT NULL CHECK (json_valid(row_json)),
    row_sha256 TEXT NOT NULL CHECK (length(row_sha256) = 64),
    PRIMARY KEY (migration_run_id, source_database, table_name, row_ordinal),
    FOREIGN KEY (migration_run_id, source_database, table_name)
        REFERENCES legacy_import_tables(migration_run_id, source_database, table_name)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS migration_payloads (
    migration_run_id TEXT NOT NULL REFERENCES migration_runs(migration_run_id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    source_manifest_kind TEXT NOT NULL CHECK (source_manifest_kind IN ('BASELINE','DELTA')),
    source_path TEXT NOT NULL,
    captured_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    storage_copy_id TEXT NOT NULL,
    PRIMARY KEY (migration_run_id, ordinal),
    UNIQUE (migration_run_id, sha256),
    FOREIGN KEY (storage_copy_id, sha256) REFERENCES storage_copies(storage_copy_id, content_sha256)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS migration_evidence_items (
    migration_run_id TEXT NOT NULL REFERENCES migration_runs(migration_run_id),
    logical_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    related_entity_type TEXT,
    related_entity_id TEXT,
    reference TEXT,
    size_bytes INTEGER,
    sha256 TEXT,
    storage_copy_id TEXT,
    evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
    PRIMARY KEY (migration_run_id, logical_id),
    CHECK (sha256 IS NULL OR length(sha256) = 64),
    CHECK (size_bytes IS NULL OR size_bytes >= 0),
    FOREIGN KEY (storage_copy_id, sha256) REFERENCES storage_copies(storage_copy_id, content_sha256)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS legacy_cycle_mappings (
    migration_run_id TEXT NOT NULL REFERENCES migration_runs(migration_run_id),
    legacy_cycle_id TEXT NOT NULL,
    target_cycle_id TEXT NOT NULL REFERENCES semantic_cycles(cycle_id),
    evidence_root TEXT NOT NULL CHECK (length(evidence_root) = 64),
    semantic_state TEXT NOT NULL CHECK (semantic_state IN ('ACTIVE','AWAITING_REVIEW')),
    confidence TEXT NOT NULL CHECK (confidence = 'STRUCTURAL_ONLY_NO_AUTHOR_ACCEPTANCE_INFERRED'),
    PRIMARY KEY (migration_run_id, legacy_cycle_id),
    UNIQUE (migration_run_id, target_cycle_id)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS migration_projection_coverage (
    migration_run_id TEXT NOT NULL REFERENCES migration_runs(migration_run_id),
    source_database TEXT NOT NULL,
    table_name TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN ('NORMALIZED','NORMALIZED_AND_RAW','RAW_ONLY')),
    target_tables_json TEXT NOT NULL CHECK (json_valid(target_tables_json)),
    reason TEXT NOT NULL,
    PRIMARY KEY (migration_run_id, source_database, table_name)
) STRICT, WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS migration_replay_receipts (
    replay_id TEXT PRIMARY KEY,
    migration_run_id TEXT NOT NULL REFERENCES migration_runs(migration_run_id),
    source_logical_state_root TEXT NOT NULL CHECK (length(source_logical_state_root) = 64),
    replay_logical_state_root TEXT NOT NULL CHECK (length(replay_logical_state_root) = 64),
    source_event_chain_root TEXT NOT NULL CHECK (length(source_event_chain_root) = 64),
    replay_event_chain_root TEXT NOT NULL CHECK (length(replay_event_chain_root) = 64),
    verified_at TEXT NOT NULL,
    CHECK (source_logical_state_root = replay_logical_state_root),
    CHECK (source_event_chain_root = replay_event_chain_root)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_legacy_rows_identity
    ON legacy_import_rows(source_database, table_name, row_sha256);
CREATE INDEX IF NOT EXISTS idx_migration_payload_hash
    ON migration_payloads(sha256);
CREATE INDEX IF NOT EXISTS idx_migration_anomaly_run
    ON migration_anomalies(migration_run_id, resolution_state, classification);

CREATE TRIGGER IF NOT EXISTS legacy_import_rows_no_update
BEFORE UPDATE ON legacy_import_rows BEGIN
    SELECT RAISE(ABORT, 'legacy_import_rows is immutable');
END;

CREATE TRIGGER IF NOT EXISTS legacy_import_rows_no_delete
BEFORE DELETE ON legacy_import_rows BEGIN
    SELECT RAISE(ABORT, 'legacy_import_rows is immutable');
END;

CREATE TRIGGER IF NOT EXISTS migration_sources_no_update
BEFORE UPDATE ON migration_sources BEGIN
    SELECT RAISE(ABORT, 'migration_sources is immutable');
END;

CREATE TRIGGER IF NOT EXISTS migration_sources_no_delete
BEFORE DELETE ON migration_sources BEGIN
    SELECT RAISE(ABORT, 'migration_sources is immutable');
END;
