# Mailbox construction and lifecycle

## Current implementation status

This document defines the required construction and retirement contract. The development-preview
Post Office Next CLI does not yet implement the complete production mailbox lifecycle. Its schemas
already describe mailbox, endpoint, task and authority entities and their intended operations.
Do not invent deployment commands beyond those actually present in a compatible backend.

## Four identities that must remain separate

| Identity | Purpose | Security meaning |
|---|---|---|
| Mailbox ID | Durable routing identity | Not a credential |
| Generation | Current incarnation of a mailbox endpoint | Invalidates stale bindings and capabilities |
| Endpoint/task binding | Delivery destination | Routing metadata |
| Capability | High-entropy bearer bound to operations and generation | Authentication authority |

A project or role narrows scope but is not a substitute for authentication. A task ID or mailbox ID
must never be accepted as proof of authority.

## Mailbox roles

- COURIER: cross-project routing and evidence coordination; cannot infer author acceptance.
- BROWSER_PROXY: project-manager browser stage; may carry manager guidance within its project.
- CODEX: project-bound worker endpoint with bounded execution authority.
- REVIEW_BROWSER: dedicated automatic-review endpoint; read-only review work with no postal,
  implementation, publication or author-decision authority.

## Construction transaction

Mailbox construction should be one recoverable transaction with a durable receipt:

1. Establish the project and confirm its authoritative local root.
2. Allocate an immutable mailbox ID and initial generation.
3. Allocate or register the concrete endpoint.
4. Bind the mailbox to the exact endpoint, task or browser conversation.
5. Grant a least-authority capability bound to mailbox ID, generation, task/host where applicable,
   expiry and an explicit operation set.
6. Verify database constraints, endpoint reachability and capability scope.
7. Record the construction receipt and only then mark the mailbox ACTIVE.

The corresponding versioned design operations are project.create, mailbox.allocate,
endpoint.allocate, task.bindEndpoint and authority.grant. These are operation-contract names, not
promises that the preview CLI already exposes matching commands.

No secret value belongs in the receipt. Record only the capability identifier, subject, permitted
operations, expiry and stored hash metadata.

## Role-specific capability rules

### Codex worker

Bind the capability to the exact mailbox generation, Codex task, host and project. Grant only the
operations required for inbox, acknowledgement, claim, lease, response and completion.

### Browser proxy

Use a generation-bound browser capability. Grant only the required subset of browser inbox,
payload-fetch, acknowledgement and optional resumable upload operations. Store the secret on the
server side of the browser gateway; do not place it in a connector URL or conversation.

### Automatic-review requester

Do not allocate a postal mailbox. Issue a separate task-bound capability containing only
review-submit, review-status and review-complete. Store it in protected caller-secret storage.

### Courier

Keep ordinary routing authority separate from author-decision authority. Acceptance, rejection,
reopening and production cutover require a distinct author-courier capability and an explicit
author instruction.

## Generation rotation

Rebinding increments the generation. Rotation must atomically:

- revoke earlier-generation capabilities;
- prevent stale messages and leases from executing;
- invalidate pending wake and browser-delivery authority;
- preserve all prior evidence;
- require explicit recovery or continuation for unfinished work; and
- issue a new capability only after the new binding is verified.

Never modify an existing generation in place.

## Retirement

Retirement is evidence-preserving:

1. Move the endpoint to DRAINING so it receives no new assignments.
2. Complete, return, or explicitly recover every bound nonterminal transaction.
3. Revoke all capabilities.
4. Stop endpoint-specific runtime processes after verifying their exact identities.
5. Rotate or close the active generation so old credentials cannot revive it.
6. Remove it from scheduling and observer coverage.
7. Record RETIRED with reason, timestamp and retained transaction references.

Do not delete mailbox rows, reuse mailbox IDs, erase generations, or silently reassign in-flight
reviews.

## Verification checklist

- Mailbox ID is unique and immutable.
- Only one generation is active.
- Binding identity and generation agree across mailbox, endpoint and capability records.
- Capability operations are a least-authority subset.
- No token or token hash is present in chat or distributable artifacts.
- Nonterminal work has an owner or explicit blocked/recovery state.
- The event journal and retained payload hashes verify after construction, rotation or retirement.
