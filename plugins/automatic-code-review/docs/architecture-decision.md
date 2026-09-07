# ADR-001: automatic review is a Post Office companion

## Decision

Automatic review is a companion tool with a project-independent public interface. It uses Post
Office facilities for exact-byte custody, local MCP browser transport, reviewer selection and FIFO
scheduling, browser monitoring, result verification, and durable return. Google Drive is retained
only as an exceptional compatibility path for legacy adapters.

Tasks do not need postal mailboxes or postal operations. On first use, the trusted courier may issue
an exact task/host-bound capability containing only `review-submit`, `review-status`, and
`review-complete`, using its active courier mailbox as the credential subject. No token is returned
through chat. This is automatic service enrolment, not permission to send or receive mail.

## Why this boundary

An entirely independent review service would duplicate custody, queue, browser, scheduling, and recovery
machinery and create two sources of truth. Keeping review as a Post Office function would force all
callers to understand postal identities and would couple review evolution to mail semantics. The
companion boundary keeps one trusted transport implementation while allowing a small, stable,
review-specific client contract in every project.

## Security properties

- Caller identity is derived from the Codex runtime; no requester-task override is accepted.
- Review access grants no mailbox, handoff, author-transition, coordinator, or publication operation.
- Coordinator operations remain restricted to the trusted courier.
- Google Drive is never retained storage or the primary transport. Any legacy fallback is recorded,
  bounded and followed by verified import into local custody.
- The companion never reads or writes Post Office tables directly. It talks through the registered
  review service adapter or its future broker interface.
- Review reports have REPORT authority only. They cannot authorize edits, merges, pushes,
  publication, or project-cycle closure.

## Deployment transition

The public client is bound by a trusted-administrator deployment lock that records and attests the
exact Python runtime, backend modules and state root without containing credentials. The first
backend implementation is a legacy adapter so current review transactions continue unchanged.
Post Office Next should expose the same client contract through a local broker with authenticated
runtime identity and operation-scoped service credentials. The old adapter can then be retired after
shadow comparison and the separately authorised one-time switchover.

Browser execution is supplied by the separate singleton `playwright-browser-bridge` companion. Its
normal mode owns a dedicated persistent Chrome profile; the review broker keeps one long-lived
MCP session to its loopback endpoint. Review callers never receive raw Playwright tools or CDP
authority through the review-specific client contract.
