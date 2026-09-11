# P3.6 shadow validation and one-time cutover

P3.6 is the release-control boundary. It does not infer correctness from elapsed time, an empty
queue, or a browser response. It retains exact observations and allows production authority only
from a clean, immutable dossier.

## Shadow evidence

`shadow observe` derives `MATCH`, `MISMATCH`, `ABSENT`, or `AMBIGUOUS` from supplied roots; callers
cannot choose a favourable classification. Observations are retained locally. A matching
observation may be resolved by an exact reconciliation plan. Any other classification requires an
explicit human-author decision through `shadow decide`; accepting an exception is visible and is
not represented as a match.

`hub.snapshot` is a secret-free database projection. `hub.reconcile.preview` is deliberately a
journaled mutation because it creates the durable plan that `hub.reconcile.apply` later consumes.
Apply requires the exact plan ID and root and refuses every `REVIEW_REQUIRED` action.

## Rehearsal and dossier

`shadow record-rehearsal` retains the final capture, delta, import, replay, backup, restore and
performance roots. Import and replay roots must match and every named rehearsal check must be
explicitly true.

`shadow dossier` records and exports the cutover decision boundary. Readiness requires:

- a valid `SHADOW` kernel with all 64 contract operations implemented and current;
- a passed rehearsal and an unchanged guarded state;
- no open shadow observations or attention items;
- drained newly-created vNext dispatches, plus explicit final-rehearsal proof that every open
  semantic message/cycle, nonterminal automatic review, and retained legacy continuation packet
  was preserved exactly;
- no unexpected Drive access; and
- at least one active courier/system actor.

The dossier is useful even when it fails: its checks say exactly what remains. Only a ready dossier
is retained as activation authority.

## Cutover transaction

`cutover preflight` rechecks the exact dossier root and state guard without changing authority.
`cutover activate` creates a retained `PREPARED` transfer, publishes a local activation pointer,
then commits the transfer and changes the kernel authority state to `AUTHORITATIVE`. Consumers must
accept the pointer only when its transfer is also retained as `COMMITTED`.

If a failure occurs before the pointer exists, `cutover rollback-pre-authority` retains a rollback
event. Once the pointer exists, rollback is refused: `cutover finish-prepared` validates the exact
pointer, retained action and still-preview kernel, then idempotently finishes that transfer. After
the first authoritative mutation, returning to the legacy database is not a toggle; recovery is
forward-only.

The old database and payload archive remain immutable evidence. Google Drive is not an activation
pointer, database, archive or backup.

## Command outline

```powershell
./scripts/Invoke-PostOfficeNext.ps1 shadow observe --path <db> --credential <credential> --source-kind LIVE_PROJECTION --source-reference <ref> --expected-root <sha256> --observed-root <sha256> --evidence <json>
./scripts/Invoke-PostOfficeNext.ps1 shadow record-rehearsal --path <db> --credential <author> --evidence <json>
./scripts/Invoke-PostOfficeNext.ps1 shadow dossier --path <db> --credential <author> --legacy-final-root <sha256> --output <json>
./scripts/Invoke-PostOfficeNext.ps1 cutover preflight --path <db> --credential <author> --dossier-id <id> --dossier-root <sha256>
./scripts/Invoke-PostOfficeNext.ps1 cutover activate --path <db> --credential <author> --dossier-id <id> --dossier-root <sha256> --legacy-state-root <path> --pointer-output <new-json> --author-action-id <id>
./scripts/Invoke-PostOfficeNext.ps1 cutover finish-prepared --path <db> --credential <author> --transfer-id <id>
```
