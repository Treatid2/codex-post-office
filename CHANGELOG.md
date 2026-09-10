# Changelog

All notable changes to this repository will be documented here.

## Unreleased

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
