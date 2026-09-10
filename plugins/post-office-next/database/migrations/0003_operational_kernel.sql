-- SPDX-License-Identifier: MPL-2.0

CREATE TABLE IF NOT EXISTS kernel_instances (
    instance_id TEXT PRIMARY KEY CHECK (instance_id = 'PON-KERNEL'),
    mode TEXT NOT NULL CHECK (mode IN ('ISOLATED','SHADOW')),
    status TEXT NOT NULL CHECK (status IN ('READY','SEALED')),
    contract_root TEXT NOT NULL CHECK (length(contract_root) = 64),
    bootstrap_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    bootstrap_capability_id TEXT NOT NULL UNIQUE REFERENCES caller_capabilities(capability_id),
    bootstrapped_at TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_idempotency_operation
ON idempotency_records(operation, recorded_at);
