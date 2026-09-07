# Post Office Next database foundation v01

The initial vNext schema is implemented by `database/migrations/0001_initial.sql`. It is an isolated
schema: initialization accepts an explicit database path and never discovers, opens, upgrades, or
writes the production Codex Comms database.

## Durable authority

The durable store is split into:

- normalized relational state for projects, packages, interfaces, tasks, endpoints, mailbox
  generations, messages, cycles, authority, transport, leases, migration, and backups;
- append-only `hub_events`, protected against update and delete by database triggers;
- local `storage_copies`, restricted to `LOCAL_CAS`, `LOCAL_BACKUP`, and `DATABASE_BLOB`, using
  traversal-safe relative locations beneath separately governed local roots; and
- migration and backup receipts with exact hashes and roots.

Every event requires a caller capability and exactly one of an authority grant or exact author
action. Per-aggregate version uniqueness prevents two events from committing the same aggregate
version.

## Google Drive boundary

The only schema object allowed to name Google Drive is `browser_bridge_transfers`. Every bridge row
must point to an already durable `storage_copies` row whose content digest equals the transfer's
expected digest, and records access count, optional latency,
expected and observed hashes, terminal state, and cleanup state. No project, message, event,
retention, archive, or backup table contains a Drive location.

## Initialization and inspection

New initialization is built under a same-directory staging name, fully verified, and published to
an absent destination atomically. Existing files receive a read-only exact-identity preflight before
any writable open. The identity includes the application ID, WAL mode, exact contiguous migration
set and checksums, `user_version`, complete table/index/trigger definition root, `quick_check`, and
`foreign_key_check`. Its logical-state root also covers every value in every application table as a
typed, order-independent row set and records the exact final event sequence, event ID, and event
hash. Reinitialization performs no write and succeeds only for that exact identity;
unrelated, empty, partial, extra-migration, or schema-drifted databases fail closed without changing
the file or creating sidecars. Protected legacy database names and the production Codex Comms state
root are never valid vNext creation destinations.

Backups hold one read-only source snapshot, verify exact logical identity and event boundary against
the staged destination, and publish the database and immutable receipt only after all checks pass.
Source, destination, and receipt paths must be pairwise disjoint and new. Every write entry point
shares the same canonical production-root exclusion before it creates a parent, staging file, or
final output.

This schema is a P2 foundation, not a complete Post Office. The legacy importer, event replay,
backup/restore, operation dispatcher, browser bridge, dashboard, and one-time production migration
remain to be implemented and verified before switchover.
