# Changelog

All notable changes to this repository will be documented here.

## Unreleased

- Added `runtime issue-review-result-collection`, an immutable manifest issuer that binds an active
  review, activation dispatch, reviewer thread, result turn, exact attachment identity, and verdict
  without pretending the review request was ordinary delivered mail.
- Separated automatic-review package custody from ordinary transport: its retained attempt is
  `STORED` and ownership is explicit in the automatic-review relation. Ordinary reconciliation
  now excludes review-owned requests, and `runtime retire-review-transport` auditably cancels only
  unsent, unleased legacy dispatches without changing review state or deleting custody evidence.
- Added the complete vNext browser-return courier path: authenticated immutable collection
  manifests, exact Playwright collection receipts, JSON or Markdown manifest/member validation,
  local-CAS ingestion as a pending RESPONSE, target-specific dispatch claims, and lease-bound
  Playwright delivery manifests. Browser returns can now travel browser-to-browser without Drive or
  the retired transport shim.
- Added an authenticated, idempotent `runtime ingest-browser-return` recovery command for exact
  manifest-backed browser returns that a human has already delivered to the intended browser. The
  command validates archive/member custody and endpoint bindings, imports the original bytes into
  local CAS, registers the response, and records the observed shortcut delivery without resending.
- Corrected the P3 cutover review findings: all secondary message-state changes now receive
  aggregate metadata and journal events; prepared cutover writes are durably fenced; revoked
  destinations are not materialized or claimed; continuation lease recovery is evidence-based;
  privileged author paths reject expired credentials; and automatic-review requests use the
  retained task capability and standing grant without manufacturing human authority.
- Added secret-free production status and retained a verified post-cutover backup/restore proof.
- Hardened direct Playwright upload and submission with native-file-input and exact-marker-gated
  Enter fallbacks plus regression tests.

- Built the P3.1 isolated operational kernel with fail-closed capability authentication, exact
  actor binding, default-deny dispatch, durable request-id idempotency, aggregate compare-and-swap
  and hash-chained event primitives, plus the first callable `hub.status` operation.
- Replaced the one-open-review-per-requester gate with a 30-minute standard submission interval:
  requesters may now retain unlimited hash-distinct concurrent reviews while every reviewer browser
  keeps its one-active-review FIFO. Verified results return independently instead of being
  suppressed merely because a newer requester review exists.
- Implemented the P2.4 courier-recovery contract: explicit waiting, reconciliation, supersession
  and terminal-failure states; custody-preserving withdrawal/retirement; addressable `notLoaded`
  requester returns; exact replay recovery; and crash-boundary acceptance criteria.
- Replaced localized ChatGPT composer placeholder matching with accessibility/editability
  capability discovery plus tested native-textarea, editable-surface, composer-form, accessible
  action and stable-hook fallbacks.
- Implemented P2.3 manifest-bound automatic-review activation through the dedicated Playwright
  profile, with exact reviewer-thread routing, visible replay suppression, user-turn receipts, and
  durable retryable failures that leave the review `PENDING_SEND`.
- Implemented P2.2 native browser-result collection through authenticated ChatGPT Library when the
  in-thread renderer omits a generated-file download control.
- Added exact Library origin correlation, byte/hash verification, bounded retryable failure codes,
  and focused regression tests without retaining browser/session metadata.
- Implemented the Post Office Next P2.1 deterministic legacy importer.
- Added exact typed retention for every legacy table and row plus conservative normalized
  projections.
- Added verified local content-addressed custody for baseline/delta payloads and retained evidence.
- Added hash-chained event replay with identical logical-state, event, CAS, and verification roots.
- Added receipted, fail-closed database restore and completed a full frozen-capture rehearsal.
- Added manifest-bound Playwright delivery to exact ChatGPT conversations.
- Added SHA-256 and size validation for every uploaded attachment.
- Added visible dispatch-marker replay suppression and a stable delivery receipt.
- Expanded transport tests to cover delivery, duplicate retry and tampered attachment rejection.
- Added manifest-bound Playwright collection with generation, sweep, source-turn and attachment
  correlation plus an exact custody receipt.
- Added requester-owned, idempotent pre-activation review withdrawal with immutable custody history.

## 0.1.0 - 2026-09-07

- Created the public Treatid2 Codex marketplace without submitting it to an official or curated
  marketplace.
- Separated Post Office Next, Automatic Code Review and Playwright Browser Bridge plugins.
- Added MPL-2.0 licensing and third-party notices.
- Removed deployment-specific paths and hashes from the public automatic-review client.
- Added a trusted, machine-local deployment-lock configuration flow.
- Documented mailbox construction, reviewer initialization and retirement, dependencies, browser
  settings, data retention and the current development-preview boundary.
