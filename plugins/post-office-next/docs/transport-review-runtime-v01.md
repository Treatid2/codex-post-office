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

`runtime ingest-browser-return` is the bounded recovery path when a human has already carried a
manifest-backed browser response to its intended browser recipient. It verifies the observed outer
size and SHA-256, ZIP integrity, every non-manifest member, the source-message correlation, and both
retained browser-thread bindings. In one transaction it retains the original archive in local CAS,
allocates the next project message identity, registers the response relation, and records the
already-visible destination turn as a receipted transport. It never sends a duplicate and does not
convert reception into acknowledgement, acceptance, integration, further authority, or closure.

For an ordinary browser return that exists only in its sender chat, the courier uses the complete
collection and delivery path:

1. `runtime issue-browser-return-collection` validates the delivered source message and exact
   active browser-mailbox generation, then issues one immutable Playwright collection manifest.
2. The Playwright bridge retrieves only that source turn's exact attachment and returns a
   conversation-, collection- and SHA-256-bound receipt.
3. `runtime ingest-collected-browser-return` revalidates the receipt, outer bytes, ZIP integrity,
   source-message correlation and every JSON- or Markdown-manifested member. It imports the exact
   archive into local CAS and creates one pending RESPONSE addressed back to the source sender.
4. `runtime reconcile` materializes the transport. `runtime claim --dispatch-id ...` allows the
   courier to lease that exact dispatch without disturbing unrelated queue entries.
5. `runtime issue-delivery-manifest` verifies the active lease and destination generation, stages
   a hash-identical attachment under its canonical filename, and issues the immutable Playwright
   delivery manifest. The normal bridge delivery and `runtime complete` receipt finish transport.

Neither collection nor delivery uses Drive. Collection, custody, delivery, acknowledgement,
acceptance, integration and closure remain distinct states.

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
marker and lease. For browser delivery it also issues the immutable, lease-bound manifest consumed
by the dedicated Playwright companion. Raw CDP is
loopback-only and no raw browser or MCP endpoint is tunnelled publicly. Google Drive is not used on
the successful path.
