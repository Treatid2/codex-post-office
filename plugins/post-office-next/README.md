# Post Office Next

> **Development preview:** this is an unofficial Treatid2 plugin. It is not endorsed by OpenAI,
> Google, Microsoft, or the Playwright project. It is not a production replacement yet.

Post Office Next is a side-by-side, ground-up control-plane implementation. The current
`codex-comms` plugin remains the online transport kernel; this tree neither imports its Python
modules nor mutates its state.

The current target architecture is defined by
[`docs/architecture-direction-v03.md`](docs/architecture-direction-v03.md). Google Drive is a
transient browser bridge only, never an authority, archive, backup, or long-term retention store.
Post Office Next will use a new transactional database and local content-addressed payload store.
The old and new systems will not operate as dual authorities: the old Post Office remains online
unchanged until a fully implemented vNext passes rehearsal and optional shadow validation, followed
by one separately authorised production switchover.

The exact meanings of P0, P0.1, P1, and P2 are fixed in
[`docs/delivery-phases.md`](docs/delivery-phases.md); they are engineering gates, not severity labels.

The current delivery spans the P0/P0.1 contract and isolation gates, P1 evidence tooling, and the
initial isolated P2 database foundation:

- versioned entity, operation-request, operation-result, and diagnostic JSON Schemas;
- a complete operation catalogue with per-aggregate concurrency rules;
- immutable custody manifests for the legacy source;
- consistent SQLite backups of the live state made through read-only connections;
- deterministic inventory snapshots from those copies;
- semantic-cycle/transport-cycle mapping and read-only reconciliation previews; and
- stable, machine-readable diagnostics and receipts.

No mutation, migration, repair, routing, wake, browser poke, automatic review, or cut-over command
exists in this plugin.

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
./scripts/Invoke-PostOfficeNext.ps1 snapshot --capture-root <path> --output <json>
./scripts/Invoke-PostOfficeNext.ps1 reconcile-preview --snapshot <json> --assertions <json> --output <json>
./scripts/Invoke-PostOfficeNext.ps1 performance-baseline --output <json> --iterations 5 [--capture-root <path>]
./scripts/Invoke-PostOfficeNext.ps1 database initialize --path <isolated-vnext.sqlite3>
./scripts/Invoke-PostOfficeNext.ps1 database inspect --path <isolated-vnext.sqlite3>
./scripts/Invoke-PostOfficeNext.ps1 database backup --path <isolated-vnext.sqlite3> --destination <backup.sqlite3> --receipt <receipt.json>
```

Every operational command emits one JSON result on stdout and returns a non-zero exit code with a
stable, versioned diagnostic on failure, including command-line parse failures. `--help` is the sole
human-readable successful control path. Write commands require new, disjoint outputs; state capture
requires an absent capture directory so it can publish a fully verified staged capture atomically.
Every write entry point rejects the protected production state root before creating directories or
staging content. Capture rejects linked source members, checks both legacy databases for commits and
content changes across the complete capture interval, and records exact copied database content and
event boundaries. Local migration evidence counts as present only when its retained bytes, size,
digest, and contained member path all validate.

This plugin is distributed under the Mozilla Public License 2.0; see the repository `LICENSE`.
