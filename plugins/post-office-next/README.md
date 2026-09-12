# Post Office Next

> **Post-cutover implementation:** this is an unofficial Treatid2 plugin. It is not endorsed by
> OpenAI, Google, Microsoft, or the Playwright project. The Treatid2 deployment is authoritative;
> every independent deployment still requires a clean P3.6 dossier and explicit author action.

Post Office Next is a ground-up control-plane implementation. The one-time Treatid2 cutover is
complete; the former `codex-comms` implementation and transport shim are retained only as frozen
migration evidence and are not polled or used for new work.

The current target architecture is defined by
[`docs/architecture-direction-v03.md`](docs/architecture-direction-v03.md). Google Drive is a
transient browser bridge only, never an authority, archive, backup, or long-term retention store.
Post Office Next will use a new transactional database and local content-addressed payload store.
The old and new systems will not operate as dual authorities: the old Post Office remains online
unchanged until a fully implemented vNext passes rehearsal and optional shadow validation, followed
by one separately authorised production switchover.

The exact meanings of P0, P0.1, P1, P2, P2.1, P2.2, P2.3, P2.4, and P3.1 through P3.6 are fixed in
[`docs/delivery-phases.md`](docs/delivery-phases.md); they are engineering gates, not severity labels.
The complete implementation-to-cutover sequence and post-cutover operational roadmap are in
[`docs/complete-roadmap-v01.md`](docs/complete-roadmap-v01.md).

The current delivery spans the P0/P0.1 contract and isolation gates, P1 evidence tooling, the
isolated P2/P2.1 database and migration foundation, and the complete P3 operational kernel:

- versioned entity, operation-request, operation-result, and diagnostic JSON Schemas;
- a complete operation catalogue with per-aggregate concurrency rules;
- immutable custody manifests for the legacy source;
- consistent SQLite backups of the live state made through read-only connections;
- deterministic inventory snapshots from those copies;
- semantic-cycle/transport-cycle mapping and read-only reconciliation previews;
- deterministic legacy import with exact typed raw-row preservation and normalized projections;
- local content-addressed payload/evidence custody with byte-for-byte verification;
- a hash-chained import journal that rebuilds the database and CAS to identical logical roots; and
- verified database backup and restore with immutable receipts; and
- authenticated, default-deny contract dispatch with durable request replay and conflict detection;
- reusable aggregate-CAS and hash-chained mutation-event primitives;
- kernel-routed `hub.status` and authority inspection;
- scoped authority grant/revocation with exact author-action consumption;
- endpoint allocation/revocation and mailbox allocation/generation rotation;
- secure project/task provisioning and semantic package/message workflows;
- recoverable local/native/Playwright transport plus automatic-review return wakes;
- retained shadow reconciliation, rehearsal and cutover dossiers; and
- stable, machine-readable diagnostics and receipts.

The implementation writes only to explicitly selected local paths. `cutover activate` remains a
one-time deployment command: it validates an exact ready dossier and publishes a locally
verifiable pointer. Normal retention and routing do not use Google Drive.

Automatic code review is deliberately a companion tool, not part of this control-plane plugin.
Its project-independent interface can use Post Office custody, transient browser transport,
reviewer queues, monitoring, and verified result return without granting callers postal operations.
See [`docs/automatic-review-boundary.md`](docs/automatic-review-boundary.md).

## Commands

```powershell
./scripts/Invoke-PostOfficeNext.ps1 contracts generate
./scripts/Invoke-PostOfficeNext.ps1 contracts validate
./scripts/Invoke-PostOfficeNext.ps1 source-manifest --source-root <path> --snapshot-archive <zip> --output <json>
./scripts/Invoke-PostOfficeNext.ps1 state-capture --source-state-root <path> --capture-root <path> [--external-evidence-manifest <json>]
./scripts/Invoke-PostOfficeNext.ps1 payload-delta --capture-root <frozen-capture> --baseline-payload-manifest <manifest.json> --source-state-root <retired-read-only-state> --output-root <new-delta-root>
./scripts/Invoke-PostOfficeNext.ps1 snapshot --capture-root <path> --output <json>
./scripts/Invoke-PostOfficeNext.ps1 reconcile-preview --snapshot <json> --assertions <json> --output <json>
./scripts/Invoke-PostOfficeNext.ps1 performance-baseline --output <json> --iterations 5 [--capture-root <path>]
./scripts/Invoke-PostOfficeNext.ps1 database initialize --path <isolated-vnext.sqlite3>
./scripts/Invoke-PostOfficeNext.ps1 database inspect --path <isolated-vnext.sqlite3>
./scripts/Invoke-PostOfficeNext.ps1 database backup --path <isolated-vnext.sqlite3> --destination <backup.sqlite3> --receipt <receipt.json>
./scripts/Invoke-PostOfficeNext.ps1 database restore --path <backup.sqlite3> --backup-receipt <backup-receipt.json> --destination <restored.sqlite3> --receipt <restore-receipt.json>
./scripts/Invoke-PostOfficeNext.ps1 database reattest-migration-history --path <vnext.sqlite3> --destination <pre-repair.sqlite3> --receipt <receipt.json> --credential <author.json> --exact-author-action-id <id>
./scripts/Invoke-PostOfficeNext.ps1 migration import --capture-root <frozen-capture> --baseline-payload-manifest <manifest.json> --payload-delta-manifest <manifest.json> --output-root <new-rehearsal-root>
./scripts/Invoke-PostOfficeNext.ps1 migration replay --source-root <imported-rehearsal-root> --output-root <new-replay-root>
./scripts/Invoke-PostOfficeNext.ps1 production prepare --source-root <verified-import-root> --output-root <new-shadow-root> --receipt <new-receipt.json>
./scripts/Invoke-PostOfficeNext.ps1 production status --path <authoritative-vnext.sqlite3>
./scripts/Invoke-PostOfficeNext.ps1 kernel credential-create --output <credential.json> --capability-id <id>
./scripts/Invoke-PostOfficeNext.ps1 kernel bootstrap --path <isolated-vnext.sqlite3> --credential <credential.json> --actor-id <id> --actor-kind HUMAN --actor-role authenticated-reader --mode ISOLATED
./scripts/Invoke-PostOfficeNext.ps1 kernel inspect --path <isolated-vnext.sqlite3>
./scripts/Invoke-PostOfficeNext.ps1 kernel execute --path <isolated-vnext.sqlite3> --request <request.json> --credential <credential.json>
./scripts/Invoke-PostOfficeNext.ps1 runtime reconcile --path <vnext.sqlite3> --credential <courier.json>
./scripts/Invoke-PostOfficeNext.ps1 runtime retire-review-transport --path <vnext.sqlite3> --credential <courier.json>
./scripts/Invoke-PostOfficeNext.ps1 runtime issue-review-result-collection --path <vnext.sqlite3> --credential <courier.json> --review-id <id> --activation-dispatch-id <id> --verdict <verdict> --source-thread-id <uuid> --source-turn-id <uuid> --attachment-reference <reference> --attachment-name <name> --expected-sha256 <sha256> --expected-size-bytes <bytes> --observed-at <timestamp>
./scripts/Invoke-PostOfficeNext.ps1 continuation reconcile --path <vnext.sqlite3> --credential <courier.json> [--observations <evidence.json>]
./scripts/Invoke-PostOfficeNext.ps1 runtime record-recovered --path <vnext.sqlite3> --credential <courier.json> --message-id <id> --bundle-id <id> --channel PLAYWRIGHT_BROWSER --observable-marker <marker> --observed-receipt-id <receipt>
./scripts/Invoke-PostOfficeNext.ps1 reviews ensure --path <vnext.sqlite3> --credential <courier.json> --review-id <id> --semantic-message-id <id> --requester-task-id <id> --reviewer-endpoint-id <id> --package-sha256 <sha256>
./scripts/Invoke-PostOfficeNext.ps1 shadow dossier --path <shadow.sqlite3> --credential <author.json> --legacy-final-root <sha256> --output <dossier.json>
./scripts/Invoke-PostOfficeNext.ps1 cutover preflight --path <shadow.sqlite3> --credential <author.json> --dossier-id <id> --dossier-root <sha256>
```

`database reattest-migration-history` is an exceptional, backup-first repair for digest-only
migration-history drift. It refuses version/name drift, schema drift, integrity failures, and any
drifted migration containing data-changing or destructive SQL. It is not a general way to bypass
database identity checks and requires the active bootstrap author credential. `runtime
record-recovered` records an idempotent supplemental receipt
when an exactly retained, already-delivered migrated message is later recovered through a direct
transport; it does not rewrite the original fallback history or acknowledge the message for its
recipient.

Automatic-review request packages use `STORED` custody attempts linked by their automatic-review
record because reviewer activation has its own manifest and receipt protocol. Ordinary `runtime
reconcile` will not materialize those attempts. `runtime retire-review-transport` is an idempotent
migration repair: it cancels only review-owned ordinary dispatches that are still READY, unleased,
and unreceipted, records a journal event and runtime receipt for each, and preserves the review and
package custody.

Use `runtime issue-review-result-collection` only after an authoritative task read identifies an
exact result attached to the active review's retained reviewer conversation. It produces a
Playwright collection manifest bound to the active review, activation dispatch, exact result turn,
filename, size, hash, and verdict. Collection still grants no result or implementation authority;
run `reviews ingest-result` only after the bridge returns exact matching bytes.

After preparation, authenticated tasks use `scripts/post_office_task.py` for `task`, `inbox`,
`activate`, `block`, `respond`, `review`, and `close`. Preparation writes replacement credentials
only beneath the new root's `caller-secrets` directory and records hashes—not secrets—in its
receipt. Automatic review uses `scripts/auto_review.py` as the attested backend.

`production status` is read-only and secret-free. It reports the authority transfer, journal
boundary, queue states, reviewer lifecycle, attention, continuation work, migration evidence, and
unexpected Drive access. Migration findings are split into actionable open findings and known
historical import evidence; historical classifications remain append-only evidence rather than an
operational blockage. Continuation reconciliation accepts positive `performedReceiptId`
evidence or explicit `actionAbsent: true` evidence. With neither, the expired lease remains held
and an attention item is recorded; it is never blindly replayed.

Every operational command emits one JSON result on stdout and returns a non-zero exit code with a
stable, versioned diagnostic on failure, including command-line parse failures. `--help` is the sole
human-readable successful control path. Write commands require new, disjoint outputs; state capture
requires an absent capture directory so it can publish a fully verified staged capture atomically.
Every write entry point rejects the protected production state root before creating directories or
staging content. Capture rejects linked source members, checks both legacy databases for commits and
content changes across the complete capture interval, and records exact copied database content and
event boundaries. Local migration evidence counts as present only when its retained bytes, size,
digest, and contained member path all validate.

See [`docs/deterministic-migration-v01.md`](docs/deterministic-migration-v01.md) for P2.1's
preservation rules, replay proof, and current limitations.
Read [`docs/operational-kernel-v01.md`](docs/operational-kernel-v01.md) before using the P3.1
commands.
Read [`docs/authority-provisioning-kernel-v01.md`](docs/authority-provisioning-kernel-v01.md) before
using the P3.2 authority, endpoint, or mailbox operations.
Read [`docs/shadow-cutover-v01.md`](docs/shadow-cutover-v01.md) before any authority transfer.

This plugin is distributed under the Mozilla Public License 2.0; see the repository `LICENSE`.
