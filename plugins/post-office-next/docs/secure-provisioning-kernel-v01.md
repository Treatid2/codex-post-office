# P3.3 secure provisioning kernel

P3.3 extends the isolated/shadow operational kernel with durable project and task provisioning. It
does not create a filesystem project, a Codex task, a browser conversation, or any external
resource.

## Operational surface

The dispatcher now implements project plan/create/read/update/pause/archive, task
plan/create/read/bind/activate/block/move/record-response/review/close, and provisioning inspection.
Every mutation uses the same capability authentication, one exact authority basis, idempotency
record, compare-and-swap check, event chain, and immediate transaction as P3.1 and P3.2.

`project.planCreate` and `task.planCreate` are durable mutations. The caller supplies the plan ID as
the request aggregate ID and the future project/task ID in the parameters. A plan root binds the
canonical target, requested resources, and full payload. `project.create` and `task.create` accept
only the retained plan ID and exact plan root, then mark that plan committed. A consumed plan cannot
be used again.

Project plans explicitly name local project, CAS, and backup roots plus unique mail domains. The
kernel records these paths but does not create or inspect them in P3.3. Project archival requires a
prior pause and no open tasks.

Package-development tasks have exactly one mutable package; the package must belong to the task's
project. Other task kinds have none. Task authority must cover the project or the sole package.
Endpoint binding requires an active task-specific endpoint and an active mailbox generation from
the same project. The normal state path is `PROVISIONING -> READY -> ACTIVE -> RESPONSE_RETURNED ->
ACCEPTED|CORRECTION_REQUIRED|REJECTED -> CLOSED` (only accepted, rejected, or cancelled work can
close). Blocking and safe pre-activation project movement are explicit transitions.

## Caller credential binding

`kernel credential-bind` is a deployment lifecycle command rather than a public semantic mail
operation. It requires the authenticated human-author credential and an exact, unique author-action
ID. It binds a new capability to an existing active actor, a subject identity/generation, an
explicit subset of currently implemented operations, and an optional expiry. The secret is written
only to a new operator-selected local credential file; command output contains its digest but never
the secret. Failed database admission removes only the newly created credential file.

## Boundary

The only supported kernel modes remain `ISOLATED` and non-authoritative `SHADOW`. P3.3 does not
authorize production delivery or cutover.
