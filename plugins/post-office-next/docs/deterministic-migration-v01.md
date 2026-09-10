# P2.1 deterministic migration and recovery

P2.1 is a bounded increment inside the P2 engineering gate. It proves that the frozen P1 evidence
can be converted into Post Office Next's isolated durable model and rebuilt deterministically. It
does not authorize production writes, live routing, dual authority, or switchover.

## Inputs and custody

`migration import` requires three explicit, read-only inputs:

1. the frozen P1 capture root, containing consistent copies of the legacy hub and observer
   databases plus retained external evidence;
2. the baseline payload-capture manifest; and
3. the frozen post-baseline payload-delta manifest.

The importer verifies the capture identity, database content roots and event boundaries, manifest
coverage, every payload and evidence size and SHA-256 digest, and link/path safety before publishing
an output. Baseline and delta entries must provide exact, non-conflicting coverage for the payload
inventory. Google Drive is not read and is not a retention target.

The output is a new directory containing:

- `post-office-next.sqlite3`, using the complete contiguous migration set (currently 0001–0003);
- `cas/`, a local content-addressed store for payload and evidence bytes;
- `cas-manifest.json`, binding each logical input to its CAS object; and
- `migration-receipt.json`, binding source identity, projection counts, database state, event chain,
  CAS, and verification roots.

Publication is all-or-nothing. Existing output roots are rejected.

## Preservation and projection rules

Every source table and every row is retained in immutable typed form. SQLite values preserve their
type; blobs are base64 encoded and floating-point values use their exact hexadecimal representation.
Table-level column declarations, row ordinals, primary-key evidence, row hashes, row counts, and
Merkle roots make omissions or alterations detectable.

Normalized vNext projections are derived conservatively for projects, mail domains, actors,
capabilities, endpoints, mailboxes and generations, semantic cycles, messages and parents,
bundles, payloads, retained evidence, transport attempts, and historical browser-bridge facts.
Where a source fact cannot be mapped safely, the importer records projection coverage or an anomaly
and retains the exact raw row. It never turns delivery, acknowledgement, or a transport state into
author acceptance, and it never fabricates missing semantic-cycle identity.

The imported database remains explicitly marked
`NON_AUTHORITATIVE_ISOLATED_REHEARSAL`. The old Post Office remains authoritative.

## Event replay

The importer records one append-only, hash-chained event for the source identity, every source
table, every source row, every payload, every retained evidence item, and final verification. Event
bodies contain the complete portable reconstruction data.

`migration replay` verifies the source receipt, database and CAS, then reconstructs a new database
and CAS from that journal. It succeeds only if the rebuilt output has the same:

- logical-state root;
- event count and terminal event-chain root;
- CAS object count, byte count, and CAS root; and
- source identity and verification roots.

Replay writes a new `migration-replay-receipt.json`; it does not alter the source import.

## Backup and restore

`database backup` takes one consistent read-only SQLite snapshot and binds its physical digest,
logical contents, logical state, event boundary, schema identity, integrity, and foreign keys into a
receipt. `database restore` requires that receipt and rejects any physical or logical mismatch. It
restores through SQLite's backup API into a new destination, re-verifies the result, and publishes
the database and restore receipt atomically.

## Frozen-capture rehearsal

The 2026-09-08 P2.1 rehearsal imported the 2026-09-06 transition baseline without reading or
changing the live Post Office:

| Measure | Verified value |
|---|---:|
| Source databases / tables / rows | 2 / 27 / 30,558 |
| Projects / mailboxes / messages | 12 / 33 / 146 |
| Semantic cycles / bundles | 111 / 134 |
| Payloads / payload bytes | 342 / 849,787,228 |
| Retained evidence items | 33 |
| CAS objects / CAS bytes | 357 / 869,104,324 |
| Import events | 30,968 |

The direct import and journal-only replay produced identical roots:

- source identity: `da5b9b8f74815563aaef6e6ccdc4521568911c7acbfd17ec0ed7ce44995cc621`;
- verification: `3e5d9e77e48305e60f780759dbb452402a5e3dc0b6c173d416ba49308f268f0e`;
- logical state: `4976a0115c12642391b5ceae7f8000d333ec0936b119388dd6a01e16ec641a9f`;
- terminal event chain: `050d17e6f7fca1e4ffb8c8f63c4b251680544b12aa74d4c000fc4e01ed4e6e4d`;
  and
- CAS: `75f84f8a098922efb25cf35eab20735411068cc28b0cef2e061ea6318d8afd6f`.

SQLite `quick_check` returned `ok`, foreign-key checking returned no errors, and the backup/restore
round trip retained the same logical-state and event-boundary roots.

## What remains

P2.1 does not implement the live operation dispatcher, mailbox and automatic-review state machines,
browser delivery orchestration, monitoring/dashboard, shadow comparison, or production cutover.
Those require later, separately reviewed increments and one explicit switchover authorization.
