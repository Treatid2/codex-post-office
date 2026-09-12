# Post Office Next delivery phases

These labels describe engineering gates, not priority severities and not production releases.

- **P0 — architecture and public-contract baseline.** Restore the complete 55-operation FGPM
  wishlist, retain nine explicitly labelled vNext extensions, define authority/concurrency/result
  contracts, and establish the rule that Google Drive is only a transient browser bridge.
- **P0.1 — P0 hardening and independent-review remediation.** Correct defects found while proving
  P0's contracts and isolation boundaries. The current P0.1 increment covers path topology,
  read-only database identity preflight, exact migration/schema identity, coherent diagnostics and
  result polarity, JSON Schema Draft-07 parity, capture-root verification, cycle-mapping non-inference, and
  local-storage/bridge binding. It also requires production-root exclusion at every write entry,
  content-complete database identities, database-aware capture consistency, strict local-evidence
  custody, and rejection of links in capture/source boundaries. P0.1 does not add production
  authority or constitute cut-over.
- **P1 — exact legacy evidence capture and reconciliation.** Freeze consistent read-only copies of
  the old Post Office database and required project-register/archive evidence, verify custody and
  hashes, prove a stable observation boundary across both databases and inventoried files, produce
  deterministic snapshots, and classify discrepancies without applying repairs.
  Completed for the transition baseline on 2026-09-06: the frozen capture includes both legacy
  databases, retained register/archive evidence, and the baseline plus post-baseline payload delta.
- **P2 — isolated vNext durable substrate and deterministic migration rehearsal.** Build the new
  transactional schema, append-only journal, local content-addressed retention, backup/restore,
  deterministic legacy importer, and replay verifier in non-authoritative isolated databases.
- **P2.1 — deterministic import, replay, and recovery increment within P2.** Import the frozen P1
  capture and both payload manifests into a new isolated database and local CAS; retain every
  legacy table and typed row exactly; build conservative normalized projections without inferring
  author acceptance or other missing semantics; verify every retained byte; replay the hash-chained
  import journal into a second root with identical logical, event, CAS, and verification roots; and
  prove a receipted backup/restore round trip. P2.1 is implemented and rehearsed. It does not make
  vNext authoritative and does not provide the live dispatcher or complete workflows.
- **P2.2 — browser-result collection increment.** When an authoritative ChatGPT turn identifies a
  result attachment but exposes no readable local path, issue an immutable collection manifest and
  use the dedicated Playwright profile to retrieve the exact in-thread or Library artifact. Verify
  conversation, turn, filename, byte count, SHA-256, and correlation text before importing it into
  Post Office custody. The authoritative runtime now carries collected ordinary browser responses
  through local CAS and the normal receipted transport queue to another browser without Drive or a
  legacy shim. This is implemented; it does not make browser metadata authoritative.
- **P2.3 — automatic-review activation increment.** Replace the unreliable native reviewer-task
  dispatch dependency with an immutable, zero-attachment Playwright activation manifest bound to
  the Review ID, Activation Dispatch ID, reviewer conversation, browser mailbox generation, exact
  prompt, and prompt SHA-256. Record `SENT` only after the exact visible marker and resulting user
  turn UUID are observed. Failed attempts remain durably `PENDING_SEND` and idempotently retryable.
  This increment does not broaden review authority or resubmit a review transaction.
- **P2.4 — courier reconciliation and recovery increment.** Reconcile retained ledger identity
  with bounded observations of the actual browser or Codex task before changing transport state.
  Addressable `notLoaded` requester tasks receive queued review returns instead of waiting for an
  `idle` observation; genuinely busy or unavailable recipients are recorded as
  `WAITING_FOR_RECIPIENT`. Ambiguous sends become `RECONCILIATION_REQUIRED`; exhausted bounded
  attempts become `TERMINAL_FAILURE`. The courier can custody-preservingly withdraw or supersede
  unreceipted ordinary messages and retire obsolete unreceipted Playwright dispatches. Exact
  manifest replay plus visible dispatch markers recover crashes between packet issue, browser
  submission, observed turn and receipt recording without blind resubmission. Composer discovery
  uses editable/accessibility capabilities with tested native and stable-hook fallbacks, not
  localized placeholder text. Wakes remain event-driven and bounded: requesters need no heartbeat.
  The acceptance gate injects a stop at each transition and proves no duplicate delivery, no lost
  package, exact hash/destination-generation binding, return to an unloaded task, safe obsolete
  dispatch retirement, and clean journal replay/verification. P2.4 does not authorize production
  cut-over.

- **P3.1 — isolated operational kernel.** Add the shared contract dispatcher, exact capability and
  actor authentication, default-deny operation authorization, durable canonical idempotency,
  immediate write transactions, per-aggregate compare-and-swap primitives, and canonical
  hash-chained event commits. Route `hub.status` as the first fully operational contract while all
  catalogue mutations remain unavailable. Kernel instances may be `ISOLATED` or non-authoritative
  `SHADOW`; no production mode exists. P3.1 is implemented and tested.

- **P3.2 — isolated authority and mailbox-provisioning kernel.** Route `authority.inspect`,
  `authority.grant`, `authority.revoke`, `endpoint.allocate`, `endpoint.revoke`,
  `mailbox.allocate`, and `mailbox.rotateGeneration` through the P3.1 dispatcher. Exact human
  author actions are authenticated, durably recorded, and consumed once; delegated grants are
  scope-checked and bounded uses are consumed through their own chained events. Endpoint and
  mailbox identities are caller-selected aggregate IDs protected by version/root compare-and-swap.
  Mailbox rotation retires the previous generation atomically, and endpoint revocation records
  child mailbox-revocation events before the endpoint event. P3.2 is implemented and tested only
  in `ISOLATED` or non-authoritative `SHADOW` mode. It does not provision additional caller-secret
  bindings, create projects or tasks, route messages, invoke browsers, or authorize cut-over.

- **P3.3 — secure project and task provisioning.** Add controlled caller-credential binding,
  durable project/task provisioning plans, project lifecycle, task lifecycle, endpoint binding and
  provisioning inspection through the authenticated dispatcher. This gate is implemented and
  tested in isolated/shadow mode. It mutates only catalogue state; it does not create external Codex
  tasks or filesystem projects. See [`secure-provisioning-kernel-v01.md`](secure-provisioning-kernel-v01.md).

- **P3.4 — semantic mail and package workflow.** Route package, interface, semantic-cycle, message,
  bundle, capability-request, change-set, context and integration operations through the shared
  kernel. Require verified local custody and exact destination generations, and keep delivery,
  acknowledgement, review, acceptance and closure separate. This gate is implemented and tested in
  isolated/shadow mode; routing stops at retained transport intent. See
  [`semantic-workflow-kernel-v01.md`](semantic-workflow-kernel-v01.md).

- **P3.5 — transport, review and recovery runtime.** Add local-CAS transport, bounded Playwright and
  native-task adapters, leases and automatic recovery, attention projection, and automatic-review
  FIFO/return wakes. This gate is implemented with short internal leases, exact-evidence recovery,
  atomic review ensure/withdraw and return-triggered wakes. Successful normal paths do not use
  Drive; exceptional fallback remains explicit, measured and transient. See
  [`transport-review-runtime-v01.md`](transport-review-runtime-v01.md).

- **P3.6 — non-authoritative shadow validation and cutover readiness.** Compare vNext decisions with
  a bounded legacy event feed, resolve or accept every anomaly, prove restore/replay/performance and
  credential continuity, and emit an immutable cutover dossier. P3.6 adds a fail-closed cutover
  preflight and guarded authority-transfer mechanism. It is implemented and tested; activation
  still requires a clean real-world rehearsal, an exact dossier root and an explicit author action.
  See [`shadow-cutover-v01.md`](shadow-cutover-v01.md).

The complete gates and exit criteria are defined in
[`production-roadmap-v01.md`](production-roadmap-v01.md). A separately authorised one-time cut-over
follows only after P3.6 passes. None of P0 through P3.6 authorises dual-write.
