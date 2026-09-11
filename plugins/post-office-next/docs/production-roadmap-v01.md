# Post Office Next production roadmap

This roadmap turns the public contract baseline into one authoritative local Post Office. Phase
names are engineering gates, not defect priorities or release numbers. A later gate may not weaken
the authentication, exact-author-action, idempotency, compare-and-swap, event-chain, local-custody,
or Google-Drive-exclusion rules established by an earlier gate.

## Operating principles

- SQLite plus the local content-addressed store are authoritative. Google Drive is never retained
  state; it is an exceptional, measurable, transient browser bridge only.
- Every state-changing operation enters through the authenticated kernel dispatcher and commits
  one immediate transaction containing its domain mutation, authority consumption, event, and
  idempotency receipt.
- Browser observations, task status, and transport markers are evidence. They are reconciled with
  retained intent before state changes; absence of an observation is not proof of failure.
- Normal delivery is event-driven. Automatic review requesters do not poll or run heartbeats.
- The old Post Office remains authoritative until the cutover transaction completes. Shadow mode
  cannot send, acknowledge, close, retry, revoke, or otherwise affect external state.
- Cutover is a one-time, fail-closed authority transfer after a final quiesced capture. It is not a
  dual-write period.

## Completed foundation

- **P0 / P0.1:** complete contract and architecture baseline plus hardening.
- **P1:** frozen legacy capture, post-baseline delta, and reconciliation evidence.
- **P2 / P2.1:** isolated transactional schema, local CAS, deterministic import/replay, and
  backup/restore rehearsal.
- **P2.2–P2.4:** browser collection, automatic-review activation, and bounded courier
  reconciliation/recovery.
- **P3.1:** authenticated operational dispatcher and `hub.status`.
- **P3.2:** semantic authority, endpoint, and mailbox-generation operations.
- **P3.3:** secure project/task planning, creation, binding, and lifecycle.
- **P3.4:** semantic package, interface, cycle, bundle, message, and integration workflow.
- **P3.5:** recoverable transport and project-independent automatic review runtime.
- **P3.6:** retained shadow reconciliation, rehearsal evidence, immutable dossier, and guarded
  authority transfer.

## P3.3 — secure project and task provisioning

Implement controlled caller-credential binding and the project/task provisioning workflow through
the P3 dispatcher. This gate owns `project.planCreate`, `project.create`, `project.read`,
`project.update`, `project.pause`, `project.archive`, `task.planCreate`, `task.create`, `task.read`,
`task.bindEndpoint`, `task.activate`, `task.block`, `task.moveProject`, `task.recordResponse`,
`task.review`, `task.close`, and `provisioning.inspect`.

Exit criteria:

1. Plans are durable aggregates with canonical roots and explicit requested resources; create calls
   consume only the exact approved plan/root pair.
2. Caller credentials are written only to a new operator-selected local path, never returned in a
   receipt, and are bound to an existing actor with an explicit operation allowlist and expiry.
3. Project roots, packages, mail domains, tasks, endpoints, and grants are scope-consistent.
4. One-package task mutation, task movement, endpoint binding, and lifecycle transitions fail
   closed and are concurrency tested.
5. No Codex task, filesystem project, browser, message, or external service is created in this gate.

## P3.4 — semantic mail and package workflow

Implement the remaining local domain operations: package and interface registration/lifecycle,
semantic-cycle lifecycle, capability-request/change-set/context/integration records, message
planning/registration/routing/acknowledgement/review/closure, bundle verification/supersession, and
their task correlations.

Exit criteria:

1. Every operation in these categories is routed by the shared dispatcher and contract-valid.
2. Message registration requires verified local bundle custody and exact destination generation.
3. Author acceptance is distinct from delivery, acknowledgement, task completion, and review.
4. A correction stays in its governing semantic cycle; courier forwarding cannot accidentally
   create an unrelated authority cycle.
5. Duplicate, stale-generation, invalid-manifest, and changed-idempotency paths are tested with zero
   partial mutation.

## P3.5 — transport, review, and recovery runtime

Implement `transport.inspect`, `transport.retry`, `transport.quarantine`,
`transport.tombstoneDuplicate`, attention projection, local CAS ingress/egress, bounded Playwright
delivery and collection manifests, native task wakes, execution leases and automatic lease
recovery, and the project-independent automatic-review FIFO/return wake.

Exit criteria:

1. The normal path uses local retained bytes and direct browser/task transport; Drive access is
   absent from successful acceptance tests and measured when exceptional fallback is injected.
2. Multi-attachment transport is atomic at message completion while retaining per-payload receipts.
3. Crash points between intent, send, observation, receipt, and return are exactly replayable without
   duplicate user turns or packages.
4. Long-running work recovers from lease expiry without becoming author-blocked; only terminal
   evidence failures enter `BLOCKED` or quarantine.
5. Review requesters may retain multiple distinct reviews, are rate-limited only by the configured
   submission interval, and are woken on verified return without heartbeat polling.

## P3.6 — shadow validation and cutover readiness

Run vNext as a non-authoritative decision engine against a frozen baseline plus a bounded stream of
legacy events. Produce deterministic comparisons, an anomaly ledger, performance and Drive-access
budgets, backup/restore evidence, operational documentation, and a signed cutover dossier.

Exit criteria:

1. Every one of the 64 public operations is implemented or explicitly retired by a new reviewed
   contract version; no `CONTRACT_ONLY` operation remains silently callable.
2. Shadow decisions match the retained legacy intent or have an accepted, documented vNext reason.
3. No unexplained open transport, lease, outbox, review, wake, or generation-stale state remains.
4. Final migration rehearsal, journal replay, database restore, and credential continuity pass from
   the same evidence boundary.
5. The cutover command supports `plan`, `preflight`, `activate`, `finish-prepared`, and
   `rollback-pre-authority` modes,
   requires the exact author-approved dossier root, and cannot activate from `ISOLATED` or an
   unverified shadow instance.

## One-time cutover

After P3.6 passes and the author has explicitly requested cutover:

1. Quiesce the old Post Office and reject new legacy mutations.
2. Drain or explicitly classify every open delivery, review, wake, outbox, and lease.
3. Capture the final database and payload delta at one stable boundary.
4. Import into a new vNext production root; replay into a second root; compare logical, event, CAS,
   authority, mailbox-generation, and open-work roots; take and restore a verified backup.
5. Create the cutover dossier from those immutable receipts and run cutover preflight.
6. Atomically seal the legacy authority marker and activate vNext with that exact dossier root.
7. Rebind the courier and client entry points, retaining credential hashes or issuing explicitly
   controlled replacements without exposing secrets.
8. Run read, local delivery, browser delivery, automatic-review, recovery, and backup smoke tests.
9. Keep the old database and CAS read-only as rollback evidence. Do not resume legacy writes after
   vNext has accepted the first authoritative mutation; any later rollback is a separately designed
   forward recovery, not a database toggle.

## Release discipline

Each phase is complete only when contracts validate, the full regression suite passes, a fresh
isolated rehearsal produces retained receipts, documentation names the exact non-capabilities, and
independent review findings have been evaluated and incorporated where they improve the design.
Review reports are evidence and advice, not implementation authority or blockers.
