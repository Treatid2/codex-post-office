# Post Office Next P3.2 authority and mailbox-provisioning kernel

P3.2 adds the first isolated catalogue mutations to the authenticated P3.1 dispatcher. It remains
a development preview: only `ISOLATED` and non-authoritative `SHADOW` kernel modes are accepted,
and no command can select production authority or perform cut-over.

## Implemented operations

- `authority.inspect` reports whether an actor exists, whether the proposed operation is implemented,
  and which active caller capabilities and semantic grants name it. Its decision is capability-layer
  evidence, not permission to omit the operation's required exact action or grant.
- `authority.grant` creates a scoped `ONE_SHOT` or `STANDING` semantic grant from an authenticated
  human author action. P3.2 grants may name only operations actually implemented by this kernel.
- `authority.revoke` revokes an existing grant through a different exact human author action.
- `endpoint.allocate` creates one endpoint and its deterministic `ENDPOINT` actor inside an existing
  active or provisioning project. The caller supplies the immutable endpoint aggregate ID.
- `mailbox.allocate` creates generation 1 for a caller-selected mailbox ID, bound to an active
  endpoint and a mail domain belonging to the same project.
- `mailbox.rotateGeneration` retires the single active generation and creates the next generation in
  one immediate transaction.
- `endpoint.revoke` revokes the endpoint actor and every active/provisional child mailbox. Each child
  mailbox transition receives its own hash-chained event before the endpoint's mutation event.

Every mutation uses the existing request-ID/canonical-request idempotency record, immediate SQLite
transaction, aggregate version/root compare-and-swap gate, exact result contract, and canonical
event chain. A successful replay returns the byte-equivalent retained result and never consumes an
authority basis twice.

## Authority model

An `author` bootstrap capability permits all P3.2 operations. Other bootstrap roles receive only
`hub.status` and `authority.inspect`.

For an exact-author mutation, the authenticated request itself is the confirmation. The kernel
creates the named `exact_author_actions` row from the canonical request only for an active `HUMAN`
actor whose exact role is `author`; the primary mutation event then consumes it. Reuse with a
different request, scope, operation, or request ID fails closed.

A delegated mutation still requires two independent facts:

1. an authenticated caller capability whose operation allowlist contains the operation; and
2. an active semantic authority grant to the same actor whose operation and retained scope cover the
   target project, task, package or mail domain.

For delegated `endpoint.allocate`, authority to create the endpoint and authority to assign its
`accessScope` are checked separately. Every referenced task, package, and mail domain must belong to
the endpoint's owning project; a task-bound endpoint may name only its own task. The retained grant
must cover every assigned resource either explicitly in the matching scope dimension or through an
owning-project grant. A task-only grant does not silently become project-wide, and a grant for one
package or mail domain does not authorize unrelated resources. Exact human-author actions remain
the only basis that may assign any contract-valid scope within the endpoint's owning project.

Bounded grant consumption is a separate `AuthorityGrant` aggregate event in the same transaction.
The event chain therefore records both the reduction of authority and the requested domain mutation.

Before each authenticated operation, the kernel verifies the complete installed contract manifest,
the canonical global event hash chain, per-aggregate event continuity, and the current
`AuthorityGrant`, `Endpoint`, and `Mailbox` projections against their latest mutation events. A
mismatch rejects the request before authority is consumed or domain state is changed.

P3.2 does not issue or bind a new caller secret for a delegated actor. A delegated grant is callable
only when a matching caller capability was already established by controlled bootstrap/import
evidence. General credential provisioning is a later P3 gate and must not be simulated by copying
the author's credential.

## Invocation

Bootstrap an isolated author kernel using the existing commands:

```powershell
./scripts/Invoke-PostOfficeNext.ps1 database initialize --path <isolated>/post-office-next.sqlite3
./scripts/Invoke-PostOfficeNext.ps1 kernel credential-create --output <isolated>/author.json --capability-id PON-CAPABILITY-AUTHOR
./scripts/Invoke-PostOfficeNext.ps1 kernel bootstrap --path <isolated>/post-office-next.sqlite3 --credential <isolated>/author.json --actor-id PON-ACTOR-AUTHOR --actor-kind HUMAN --actor-role author --mode ISOLATED
```

Submit a contract-valid JSON request with `kernel execute`. Creation requests use the desired entity
ID as `aggregate.id` and normally assert `expectedVersion: 0`. Later lifecycle requests use the same
aggregate ID and the version returned by the preceding event. Exact author mutations carry a fresh
`authority.exactAuthorActionId`; delegated mutations carry `authority.authorityGrantId`.

Mailbox generation rotation reports the stable mailbox aggregate ID in `createdIds`; the new numeric
generation is reported in the event result. Generation-qualified display strings are not entity IDs,
which keeps every contract-valid mailbox ID rotatable across generation digit-width changes.

## Explicit non-capabilities

P3.2 does not create or mutate projects, packages, tasks, interfaces, messages, bundles, transport
attempts, browser transfers, automatic reviews or production state. It does not send mail, create a
Codex task, attach to a browser, use Google Drive, ingest live legacy writes, or change authority
between the old and new Post Offices. Shadow results remain evidence only.
