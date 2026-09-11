# P3.5 transport and automatic-review runtime

P3.5 turns retained `message.route` intent into a recoverable dispatch queue. Before cutover it may
be exercised in an isolated or non-authoritative shadow kernel; after a verified P3.6 transfer the
same runtime operates against an `AUTHORITATIVE` kernel.

## Dispatch and recovery

`runtime reconcile` materializes one dispatch for each pending transport attempt after rechecking
the exact destination mailbox generation and verified local bundle custody. The selected channel is
`NATIVE_TASK` for local Codex endpoints or `PLAYWRIGHT_BROWSER` for browser endpoints. Each dispatch
has a unique visible marker.

`runtime claim` uses a short 10-minute lease by default (accepted range 30 seconds to 30 minutes).
The lease is internal crash detection, not a task state and never a reason to wait for a person. On
expiry, one bounded reconciliation does exactly one of three things:

- an exact marker and receipt complete the delivery;
- explicit proof that the marker is absent safely requeues it;
- ambiguous evidence opens an attention item and records `RECONCILIATION_REQUIRED` without a blind
  resend.

`runtime complete` requires the secret lease token, exact visible marker, and exact destination
receipt. It atomically receipts the dispatch and attempt, advances the semantic message to
`DELIVERED`, appends the message event and runtime receipt, and resolves prior attention.

The authenticated kernel also implements transport inspection, explicit retry, quarantine,
duplicate tombstoning and attention listing. Retry creates a new numbered attempt; historical
attempts remain retained.

Migrated continuation work uses the same bounded lease pattern. `continuation claim` returns the
resolved native or browser thread, host, URL, mailbox, endpoint and generation when a retained
binding exists. `continuation complete` requires an exact external or custody receipt.
`continuation retire` is deliberately separate: it closes only a proven-obsolete or superseded
continuation and retains the exact supersession receipt and disposition; it never claims the
original collection, wake or delivery occurred.

## Automatic review

The project-independent review companion uses the same retained messages and local custody. Its
runtime offers atomic `ensure`, FIFO `claim`, exact `return`, and `withdraw` actions. `ensure` is
idempotent for the same Review ID and package hash. Different reviews may coexist without an open
review-count cap. Browser submission is throttled by a per-reviewer minimum interval (30 minutes by
default), not by queue depth.

A return is accepted only when its retained result message is addressed to the original requester
task and already has a materialized wake dispatch. The return records that dispatch and explicitly
reports `heartbeatRequired: false`: the return dispatch wakes the requester. The Post Office courier
is the component performing that wake and therefore is the sole operational exception.

Withdrawal is a state transition, never deletion of the review package or custody history.

## Browser boundary

The runtime returns the selected channel, local retained location, destination identity, exact
marker and lease. The dedicated Playwright companion consumes that bounded work item. Raw CDP is
loopback-only and no raw browser or MCP endpoint is tunnelled publicly. Google Drive is not used on
the successful path.
