# Post Office Next delivery phases

These labels describe engineering gates, not priority severities and not production releases.

- **P0 — architecture and public-contract baseline.** Restore the complete 55-operation FGPM
  wishlist, retain nine explicitly labelled vNext extensions, define authority/concurrency/result
  contracts, and establish the rule that Google Drive is only a transient browser bridge.
- **P0.1 — P0 hardening and independent-review remediation.** Correct defects found while proving
  P0's contracts and isolation boundaries. The current P0.1 increment covers path topology,
  read-only database identity preflight, exact migration/schema identity, coherent diagnostics and
  result polarity, Draft-07 parity, capture-root verification, cycle-mapping non-inference, and
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
  Post Office custody. This is implemented; it does not make browser metadata authoritative.
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

The remaining P3 work is to implement the authority, provisioning, message, transport and recovery
handlers through this kernel, then run non-authoritative shadow validation. A separately authorised
cut-over follows only after those gates pass. None of P0 through P3.1 authorises dual-write or
production cut-over.
