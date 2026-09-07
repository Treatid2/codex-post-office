# Post Office Next architecture direction v03

Status: author-directed amendment, 2026-09-04.

This document supersedes the Google Drive retention, dual-projection, and rollout assumptions in
the supplied architecture plan and in `architecture-evaluation-v02.md`. It does not modify the
historical handoff documents, which remain source evidence.

## Governing deployment decision

The existing Post Office remains the sole online authority and is left unchanged until Post Office
Next is fully implemented and has passed migration rehearsal, shadow validation, acceptance, and
cut-over-readiness checks.

There is one production authority switch:

1. build and test Post Office Next in isolation;
2. rehearse deterministic imports from consistent legacy snapshots;
3. optionally run Post Office Next in non-authoritative shadow mode;
4. quiesce legacy writes for the final migration window;
5. take and verify a final consistent legacy capture;
6. import that capture into the new schema and verify identities, hashes, counts, authority facts,
   cycle facts, and replay roots;
7. switch all clients and endpoints to Post Office Next as one controlled activation;
8. retain the old Post Office read-only as migration evidence, not as a second live authority.

There is no prolonged dual-write or dual-authority period. Once Post Office Next accepts its first
production write, the old Post Office cannot resume as the writer without an expressly designed and
authorised reverse migration.

## Google Drive boundary

Google Drive is a browser bridge only. It is not:

- an authoritative database;
- a durable archive or backup;
- a project register;
- a general projection target;
- a source for routine reads after a package has been ingested; or
- part of the post-cut-over retention model.

A Drive object is a transient transport copy. The durable record is created locally before an
outbound upload or immediately after an inbound download. The database records the transport
attempt, remote object identity, expected digest, observed digest or receipt, timestamps, retry
state, and cleanup state. Retention and recovery never depend on the Drive object remaining
available.

Drive access must be minimised by design:

- address known object IDs instead of scanning folders;
- use explicit browser pokes/events rather than polling;
- batch metadata calls where the connector permits;
- deduplicate before upload and immediately after download;
- avoid downloading an outbound object solely for readback when a trustworthy server-side digest
  is available; otherwise perform at most the integrity read required by policy;
- cache no authority in Drive and never reread a successfully ingested package for normal work;
- expire or remove bridge copies after terminal receipt according to a bounded cleanup policy; and
- measure Drive calls and latency per transport attempt so regressions are visible.

## Durable data and retention model

Post Office Next owns a new transactional relational schema. The initial single-host implementation
may use SQLite with WAL mode, strict tables, foreign keys, explicit migrations, transactional
outbox/inbox processing, and integrity checks. The logical schema must avoid SQLite-specific domain
coupling so a later move to PostgreSQL remains possible if measured concurrency requires it.

The authoritative store consists of:

- a normalized operational database for projects, tasks, packages, interfaces, endpoints,
  mailboxes and generations, semantic messages and cycles, authority grants, transport attempts,
  acknowledgements, leases, idempotency keys, migration state, and browser-bridge transfers;
- an append-only `hub_events` journal with aggregate identity, aggregate version, operation,
  authority evidence, timestamp, payload digest, previous-event digest, and event digest;
- materialized/index tables rebuilt and verified from the event journal where replay is required;
- a local content-addressed payload store for large immutable bytes, with size and SHA-256 bound in
  the database; and
- verified local backups containing the database capture, payload manifest, schema version, and
  replay/integrity roots.

Small structured envelopes may be retained directly in the database. Large or opaque attachments
belong in the local content-addressed store, never solely in a database path field or Drive object.
The authoritative permanent root is an operator-configured local or network filesystem; backups and disaster-recovery copies must be
independently verifiable and must not depend on Google Drive.

## Shadow operation

Shadow operation is a testing technique, not a transition of authority. A shadow instance may:

- import consistent legacy captures or consume a read-only mirrored feed;
- replay legacy events into the new model;
- compute the decisions and transport actions it would have taken; and
- compare snapshots, state transitions, diagnostics, latency, and intended side effects.

It must not route real messages, poke browsers, allocate live resources, mutate the legacy Post
Office, or become an authority source. Shadow output is test evidence only.

## Revised delivery interpretation

- **P0/P1 remediation:** correct the full FGPM contract catalogue and authority model; define the
  new database, event, payload-retention, browser-bridge, migration, and cut-over contracts; capture
  all legacy evidence needed for migration without treating Drive as future retention.
- **P0.1 hardening:** independently review and correct P0's isolation, path-safety, exact database
  identity, schema parity, diagnostic/result, capture-integrity, and non-inference guarantees before
  advancing dependent migration work. P0.1 is a correction gate, not a production release.
- **P2 database foundation:** create the isolated Post Office Next database, event journal,
  materialized indexes, local content-addressed payload store, migrations, backup/restore, and
  deterministic legacy importer. P2 remains non-authoritative.
- **Subsequent implementation:** complete every required catalogue, authority, provisioning,
  messaging, transport, review, reconciliation, recovery, dashboard, and browser-bridge workflow
  against the new store. Google Drive is exercised only at the browser boundary.
- **Migration rehearsal and shadow validation:** repeat full imports and simulated operations until
  all acceptance, integrity, performance, and failure-recovery gates pass.
- **One-time cut-over:** perform the final quiesce, capture, import, verification, and activation
  only after the entire new Post Office is ready and the author separately approves the switch.

P2 is therefore not permission to migrate production or to begin a dual-running replacement. It is
permission only to build and prove the new durable substrate in isolation.
