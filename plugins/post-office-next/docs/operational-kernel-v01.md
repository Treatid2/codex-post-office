# Post Office Next P3.1 operational kernel

P3.1 introduces the first callable operation dispatcher backed by the isolated vNext database. It
does not make vNext authoritative and cannot select a production mode. The only permitted instance
modes are `ISOLATED` and non-authoritative `SHADOW`.

## Delivered boundary

The operational kernel now provides:

- exact request and result validation against the generated operation contracts;
- a private credential file whose secret is never printed by a successful command;
- SHA-256 capability authentication and exact actor ID, kind, role and status binding;
- default-deny operation authorization;
- an immediate SQLite write transaction with exact application, migration and schema preflight;
- canonical request hashing and durable request-ID replay;
- rejection of a changed request that reuses a retained request ID;
- a shared per-aggregate version/root compare-and-swap gate for later mutation handlers;
- a canonical append-only event writer with aggregate versions and previous-event hash chaining;
- exact result-contract validation before a receipt commits; and
- `hub.status` as the first operation routed through the kernel.

`hub.status` durably records its idempotency receipt but does not change domain state or the event
journal. Its `beforeRoot` and `afterRoot` are therefore identical. The operational root deliberately
excludes `idempotency_records`; the exact database identity used by backup, restore and migration
verification still includes that table.

The shared mutation primitives are present and tested, but no catalogue mutation is callable in
P3.1. Later P3 slices must add handlers through this dispatcher rather than establishing parallel
authentication, concurrency, idempotency or event-writing paths.

## Isolated initialization

Choose a new credential path outside the repository and all protected legacy roots. The credential
is created before bootstrap, so an interrupted bootstrap can be replayed using the same credential
without inventing a replacement secret.

```powershell
./scripts/Invoke-PostOfficeNext.ps1 database initialize `
  --path <isolated-root>/post-office-next.sqlite3

./scripts/Invoke-PostOfficeNext.ps1 kernel credential-create `
  --output <isolated-root>/operator-credential.json `
  --capability-id PON-CAPABILITY-OPERATOR

./scripts/Invoke-PostOfficeNext.ps1 kernel bootstrap `
  --path <isolated-root>/post-office-next.sqlite3 `
  --credential <isolated-root>/operator-credential.json `
  --actor-id PON-ACTOR-OPERATOR `
  --actor-kind HUMAN `
  --actor-role authenticated-reader `
  --mode ISOLATED
```

Bootstrap creates exactly one kernel instance, binds the supplied actor and least-authority
capability, and is idempotent only when every retained field and the credential hash match. The
P3.1 bootstrap capability permits only `hub.status`.

Credential confidentiality is bounded to the current local operating-system account. Keep the file
outside source control, packages, chat prompts and receipts, and apply host ACLs appropriate to the
deployment. P3.1 does not claim a second hardware or service identity factor.

## Dispatch

Create a `hub.status` request that conforms to
`contracts/v1/operations/requests/hub.status.schema.json`, then call:

```powershell
./scripts/Invoke-PostOfficeNext.ps1 kernel execute `
  --path <isolated-root>/post-office-next.sqlite3 `
  --request <isolated-root>/hub-status-request.json `
  --credential <isolated-root>/operator-credential.json
```

The same request ID plus canonical request bytes returns the exact retained result. The same request
ID with any changed field fails with `PON_IDEMPOTENCY_CONFLICT`. Parallel identical submissions are
serialized by `BEGIN IMMEDIATE` and produce one receipt.

For a credential-free structural view of an already bootstrapped isolated instance:

```powershell
./scripts/Invoke-PostOfficeNext.ps1 kernel inspect `
  --path <isolated-root>/post-office-next.sqlite3
```

## Explicit non-capabilities

P3.1 does not route or collect mail, allocate mailboxes, create tasks, mutate projects, invoke a
browser, use Google Drive, run automatic reviews, consume legacy writes, activate a migration, or
switch authority. `SHADOW` is only a durable non-authoritative mode marker at this stage; shadow
feed ingestion and decision comparison are subsequent P3 work.
