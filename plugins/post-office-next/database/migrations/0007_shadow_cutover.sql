-- SPDX-License-Identifier: MPL-2.0

ALTER TABLE kernel_instances ADD COLUMN authority_state TEXT NOT NULL DEFAULT 'PREVIEW'
    CHECK (authority_state IN ('PREVIEW','AUTHORITATIVE','RETIRED'));

CREATE TABLE IF NOT EXISTS shadow_observations (
    observation_id TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('LEGACY_CAPTURE','POST_BASELINE_DELTA','LIVE_PROJECTION','TRANSPORT','REVIEW','PERFORMANCE','DRIVE_ACCESS')),
    source_reference TEXT NOT NULL,
    expected_root TEXT CHECK (expected_root IS NULL OR length(expected_root) = 64),
    observed_root TEXT CHECK (observed_root IS NULL OR length(observed_root) = 64),
    classification TEXT NOT NULL CHECK (classification IN ('MATCH','MISMATCH','ABSENT','AMBIGUOUS')),
    evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
    evidence_root TEXT NOT NULL CHECK (length(evidence_root) = 64),
    state TEXT NOT NULL CHECK (state IN ('OPEN','RESOLVED','ACCEPTED_EXCEPTION')),
    recorded_at TEXT NOT NULL,
    resolved_at TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS reconciliation_plans (
    plan_id TEXT PRIMARY KEY,
    snapshot_root TEXT NOT NULL CHECK (length(snapshot_root) = 64),
    assertion_set_root TEXT NOT NULL CHECK (length(assertion_set_root) = 64),
    observation_set_root TEXT NOT NULL CHECK (length(observation_set_root) = 64),
    actions_json TEXT NOT NULL CHECK (json_valid(actions_json)),
    plan_root TEXT NOT NULL UNIQUE CHECK (length(plan_root) = 64),
    state TEXT NOT NULL CHECK (state IN ('PREVIEWED','APPLIED','SUPERSEDED')),
    aggregate_version INTEGER NOT NULL DEFAULT 0 CHECK (aggregate_version >= 0),
    aggregate_root TEXT NOT NULL CHECK (length(aggregate_root) = 64),
    created_at TEXT NOT NULL,
    applied_at TEXT,
    applied_event_id TEXT REFERENCES hub_events(event_id)
) STRICT;

CREATE TABLE IF NOT EXISTS cutover_rehearsals (
    rehearsal_id TEXT PRIMARY KEY,
    capture_root TEXT NOT NULL CHECK (length(capture_root) = 64),
    delta_root TEXT NOT NULL CHECK (length(delta_root) = 64),
    import_root TEXT NOT NULL CHECK (length(import_root) = 64),
    replay_root TEXT NOT NULL CHECK (length(replay_root) = 64),
    backup_root TEXT NOT NULL CHECK (length(backup_root) = 64),
    restore_root TEXT NOT NULL CHECK (length(restore_root) = 64),
    performance_root TEXT NOT NULL CHECK (length(performance_root) = 64),
    passed INTEGER NOT NULL CHECK (passed IN (0,1)),
    evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
    evidence_root TEXT NOT NULL CHECK (length(evidence_root) = 64),
    recorded_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS cutover_dossiers (
    dossier_id TEXT PRIMARY KEY,
    rehearsal_id TEXT NOT NULL REFERENCES cutover_rehearsals(rehearsal_id),
    contract_root TEXT NOT NULL CHECK (length(contract_root) = 64),
    database_root TEXT NOT NULL CHECK (length(database_root) = 64),
    legacy_final_root TEXT NOT NULL CHECK (length(legacy_final_root) = 64),
    open_anomaly_count INTEGER NOT NULL CHECK (open_anomaly_count >= 0),
    open_attention_count INTEGER NOT NULL CHECK (open_attention_count >= 0),
    nonterminal_dispatch_count INTEGER NOT NULL CHECK (nonterminal_dispatch_count >= 0),
    nonterminal_review_count INTEGER NOT NULL CHECK (nonterminal_review_count >= 0),
    drive_access_count INTEGER NOT NULL CHECK (drive_access_count >= 0),
    ready INTEGER NOT NULL CHECK (ready IN (0,1)),
    checks_json TEXT NOT NULL CHECK (json_valid(checks_json)),
    dossier_root TEXT NOT NULL UNIQUE CHECK (length(dossier_root) = 64),
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS authority_transfers (
    transfer_id TEXT PRIMARY KEY,
    dossier_id TEXT NOT NULL UNIQUE REFERENCES cutover_dossiers(dossier_id),
    dossier_root TEXT NOT NULL CHECK (length(dossier_root) = 64),
    legacy_state_root TEXT NOT NULL,
    vnext_database_path TEXT NOT NULL,
    pointer_path TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('PREPARED','COMMITTED','ROLLED_BACK')),
    exact_author_action_id TEXT NOT NULL UNIQUE,
    committed_at TEXT,
    rollback_reason TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS drive_access_metrics (
    metric_id TEXT PRIMARY KEY,
    purpose TEXT NOT NULL CHECK (purpose IN ('BROWSER_ACCESS','MIGRATION_EVIDENCE','UNEXPECTED')),
    access_count INTEGER NOT NULL CHECK (access_count >= 0),
    interval_start TEXT NOT NULL,
    interval_end TEXT NOT NULL,
    evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
    recorded_at TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_shadow_observations_state
    ON shadow_observations(state, classification, recorded_at);
CREATE INDEX IF NOT EXISTS idx_cutover_dossiers_ready
    ON cutover_dossiers(ready, created_at);

CREATE TRIGGER IF NOT EXISTS cutover_rehearsals_no_update
BEFORE UPDATE ON cutover_rehearsals BEGIN
    SELECT RAISE(ABORT, 'cutover_rehearsals is append-only');
END;

CREATE TRIGGER IF NOT EXISTS cutover_rehearsals_no_delete
BEFORE DELETE ON cutover_rehearsals BEGIN
    SELECT RAISE(ABORT, 'cutover_rehearsals is append-only');
END;

CREATE TRIGGER IF NOT EXISTS cutover_dossiers_no_update
BEFORE UPDATE ON cutover_dossiers BEGIN
    SELECT RAISE(ABORT, 'cutover_dossiers is append-only');
END;

CREATE TRIGGER IF NOT EXISTS cutover_dossiers_no_delete
BEFORE DELETE ON cutover_dossiers BEGIN
    SELECT RAISE(ABORT, 'cutover_dossiers is append-only');
END;
