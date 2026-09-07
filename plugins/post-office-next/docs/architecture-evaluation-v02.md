# Post Office Next architecture evaluation v02

> Superseded in part by `architecture-direction-v03.md`. In particular, Google Drive is now a
> transient browser bridge rather than a retention/projection layer, and the proposed dual
> projection rollout is replaced by isolated implementation, optional non-authoritative shadow
> validation, and one controlled production switchover.

## Decision

The supplied architecture is directionally correct, but the first implementation should not add
modules or migrations to the online `codex-comms` source. P0/P1 is safer and easier to verify as a
separate sidecar with no mutation surface. The present transport kernel remains the compatibility
target and evidence source, not a Python dependency.

The resulting architecture is:

```text
online codex-comms kernel (unchanged)
    -> read-only SQLite backup + exact file inventory
    -> frozen legacy capture
    -> deterministic vNext snapshot
    -> read-only reconciliation preview

post-office-next contracts
    -> future shared dispatcher
    -> future human client and LLM client
```

## Improvements over the supplied plan

1. **Clean sidecar boundary for P0/P1.** The plan proposed incremental modules inside the old plugin.
   That creates accidental coupling before the entity model is stable. This build instead consumes
   consistent copies through an explicit legacy adapter and contains no live-state writer.
2. **Preserve contracts, not implementation internals.** Later compatibility work should preserve
   IDs, hashes, receipts, state transitions, and wrapper behaviour. It should not require the new
   domain model to import the old monolithic Python module.
3. **Typed authority evidence.** The operation envelope distinguishes caller capability,
   `AuthorityGrant`, and exact author action. The phrase “grant or author action” is no longer an
   untyped string field.
4. **Per-aggregate concurrency is executable.** Every mutation contract carries an aggregate ID and
   either an expected version or expected root. The global snapshot root is reserved for audit and
   reconciliation.
5. **Determinism excludes observation time.** Capture metadata records immutable database and file
   identities. Snapshot roots omit capture timestamps and output paths, so repeated reads of one
   frozen capture are byte-identical.
6. **Secrets are structurally excluded.** `caller-secrets/` is never copied or inventoried. Caller
   capability rows are projected without the stored secret hash. Message prose is represented by
   hashes where its content is not required for inventory.
7. **Legacy endpoint ambiguity is explicit.** Schema 12 conflates actor/endpoint identity with a
   mailbox record. The snapshot labels those rows as legacy mailbox-bound actors instead of
   fabricating endpoint IDs.
8. **Cycle facts fail closed.** Structured cycle columns, immutable labels, browser guidance,
   retained assertions, and exact author actions remain separate evidence. A mismatch creates a
   review item; it never creates a repair or a semantic state transition.
9. **Duplicate bytes are candidates, not defects.** Repeated hashes across CAS, staging, archive,
   and review projections are reported as content-reuse candidates. They are not automatically
   tombstoned without semantic and custody evidence.
10. **P0/P1 has no hidden P2.** All 64 intended operations (55 FGPM wishlist operations and nine
    separately labelled Post Office Next extensions) have schemas, but only `hub.snapshot` and
    `hub.reconcile.preview` are implemented. Mutation contracts are design gates, not callable
    writers.
11. **Human/LLM parity starts at the dispatcher.** A dashboard is deferred until the shared
    dispatcher exists. Building rich UI pages against provisional tables would create a second
    contract and premature migration pressure.
12. **Revision control should cover the project, not one plugin.** The least disruptive Git root is
    an operator-selected immutable capture root, containing the online source, sidecar source, design
    inputs, and receipts. This build does not initialise or publish that repository without a
    separate author decision.

## Retained plan decisions

- default-deny authority and author-only semantic-cycle closure;
- exact mailbox generations and immutable semantic message identity;
- one mutable package per package-development task;
- zero-package-write integration tasks;
- per-aggregate optimistic concurrency;
- explicit message/bundle/transport/storage layers;
- provisioning as a durable saga with a commit barrier;
- manual browser pokes and author-gated Codex execution;
- side-by-side isolated construction, optional non-authoritative shadow validation, and one
  explicitly authorised production switchover; no prolonged dual write or dual authority.

## P2 decision gate

After P0/P1 remediation, the next safe step is **P2 database foundation**: create an isolated vNext
transactional database, append-only event journal, materialized indexes, and local
content-addressed payload store; then import a frozen legacy capture deterministically. It must
remain read-only with respect to production and replay to the same snapshot root. Google Drive is
used only by later browser-bridge tests. Production migration and the one-time switchover remain
separate, expressly authorised decisions.
