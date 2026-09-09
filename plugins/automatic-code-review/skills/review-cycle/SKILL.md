---
name: review-cycle
description: "Use the project-independent automatic code-review companion to inspect access, submit a PR-scale package, follow its Post Office-backed browser review, evaluate findings, and continue explicitly authorised correction cycles. Do not use for tiny checks or as implementation authority."
---

# Automatic Review Cycle

This is a project-independent review client. It uses Post Office transport and custody internally,
but callers do not need a postal mailbox or postal operations.

## Access

Run `scripts/Invoke-AutomaticCodeReview.ps1 access` from this plugin. `CODEX_THREAD_ID` is required;
the registered service does not support a session-only fallback. `CODEX_HOST_ID` defaults to
`local`. Never add a caller-selectable command-line requester identity.

The runtime, backend, and service root are selected once by a trusted administrator, recorded in the
fixed local deployment lock, and hash-attested on every use. The public plugin does not include or
guess a production backend. Never add or use a caller-provided backend path, PATH lookup,
hub-root/capability override, or arbitrary adapter. If configuration or attestation fails, stop with the client diagnostic;
do not bypass it or invoke the backend directly as a substitute for this companion workflow.

If the result is `PROVISION_REQUIRED`, find the trusted task titled `ChatGPT Post Office` with the
Codex task tools and ask it to provision or rotate a review-only capability for the exact requester
task and host. The capability must:

- use the courier's active mailbox as credential subject, so no requester postal mailbox is needed;
- contain only `review-submit`, `review-status`, `review-complete`, and `review-withdraw`;
- be bound to the exact requester task and host;
- be written only to the protected `caller-secrets/<task-id>.token` path; and
- never be included in chat, packages, reports, logs, or command output.

Do this service enrolment automatically as part of review use. Do not ask the author to apply for
postal permission. The courier uses its bounded `issue-review-capability` operation; it must not use
general capability issuance. Wait for the courier's confirmation, then rerun `access`.

`CREDENTIAL_PRESENT` proves only that a protected credential file exists. Within the local Windows
account, the bearer capability is the authentication principal; task/host values are routing and
transaction-binding metadata, not a separately authenticated OS identity. The backend validates the
recorded task/host values and exact operations on first use. If submission reports `REVIEW_ACCESS_REQUIRED`,
request the same bounded courier rotation once; never expose or reuse another task's credential.

## Package and submit

Use the contract template and package rules supplied by the registered review service. A package
must be PR-scale, substantially integrated, under 1 MiB compressed, hash-complete, and contain one
`REVIEW_CONTRACT.md` and `PACKAGE_MANIFEST.json`. Reviewer modification, execution, network,
publication, and project authority remain prohibited.

Run `inspect`, then prefer `ensure` with `PR_SCALE_NEAR_COMPLETE`, a unique idempotency key, and no
reviewer override. `ensure` atomically checks the exact idempotency key, package digest, current open
review and cooldown before creating anything. Interpret its result as follows:

- `ACTIVE_DO_NOT_RESUBMIT`: retain and monitor the returned Review ID.
- `RETURNED_COMPLETE_REQUIRED`: evaluate the retained return and complete that Review ID.
- `TERMINAL`: do not recreate the same review; start a hash-distinct authorised cycle if needed.
- `ABSENT_SUBMIT_CREATED`: a new durable request was created.
- `CONFLICT`: do not submit; reconcile the returned `conflict_reason` and retained Review ID.

The original `submit` command remains available as a strict creation/replay primitive. The backend
supplies registered browser and guidance endpoints. Google Drive, where a legacy adapter still
requires it, is a transient exceptional bridge only.

When the author or requesting task explicitly withdraws obsolete work before reviewer activation,
run `withdraw --review-id <id> --reason <reason> --idempotency-key <key>`. It succeeds only from
`PENDING_DRIVE_DELIVERY`, `QUEUED`, or `READY_TO_ACTIVATE`; it records terminal `WITHDRAWN` while
preserving every custody and journal record. Never delete the package or Drive object to simulate
withdrawal. Once the review is `REVIEW_ACTIVE`, it cannot be truthfully recalled: retain the eventual
result as superseded evidence and complete it normally. A replacement must use a new Review ID and
hash-distinct package. Withdrawing `READY_TO_ACTIVATE` atomically advances that reviewer's oldest
valid queued item only to the send-safe `READY_TO_ACTIVATE` boundary; the courier must still perform
the one-use activation and receipt sequence.

## Coordination and return

The trusted courier retains exact bytes, activates the assigned reviewer through the local MCP
browser path, monitors the browser, verifies the returned Markdown attachment, and returns it
durably. Google Drive is an exceptional compatibility bridge only; it is never primary transport or
retained storage. When a connector exposes only an opaque attachment reference, the courier issues
an immutable review-bound collection manifest and uses the separate Playwright bridge; successful
collection means the exact result has been rehashed and retained in Post Office custody, not merely
downloaded. The caller
may inspect only its own status and complete only its own returned review. Never request coordinator
operations or use another task's identity.

Treat a review result as REPORT authority only. Evaluate findings against the original request.
Apply corrections only when that request already authorizes them. Resubmit a hash-distinct package
only when the author explicitly requested another cycle or a continue-until-pass loop. A PASS or
PASS_WITH_FINDINGS ends the loop after accepted findings are evaluated and the transaction is
completed. CHANGES_REQUIRED continues only under that explicit authority. BLOCKED_BY_EVIDENCE is
not a pass and requires the missing evidence or author direction.
