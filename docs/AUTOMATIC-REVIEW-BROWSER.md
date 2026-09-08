# Automatic-review browser initialization and retirement

## Component map

An automatic-review browser consists of four separately recorded objects:

1. a dedicated ChatGPT review conversation;
2. a generation-bound REVIEW_BROWSER mailbox;
3. a reviewer-registry record used by the FIFO scheduler; and
4. a dedicated Chrome profile owned by one Playwright browser-bridge process.

None of these identifiers is interchangeable. The browser profile is authenticated browser state,
not review evidence or long-term Post Office data.

The public repository supplies the browser bridge and requester client. Registration, scheduling
and result custody use the separately deployed compatible Post Office backend.

## Lifecycle states

Normal state progression is:

~~~text
PROVISIONING -> WARMING -> ACTIVE -> DRAINING -> RETIRED
~~~

Exceptional states are:

- QUARANTINED: integrity, identity or behavioural evidence makes new work unsafe.
- RECOVERY_REQUIRED: nonterminal work or endpoint evidence must be reconciled before transition.

Only ACTIVE reviewers accept new assignments. DRAINING, QUARANTINED, RECOVERY_REQUIRED and RETIRED
reviewers are excluded from selection.

## Initialization

1. Allocate a REVIEW_BROWSER mailbox and retain its generation.
2. Create one dedicated ChatGPT review conversation. Do not reuse a project-manager mailbox or an
   historical/manual review conversation.
3. Start the bridge in DedicatedChrome mode without Headless.
4. Run `bootstrap`; sign in interactively to ChatGPT in the dedicated Chrome window when it reports
   `AUTHENTICATION_REQUIRED`, then rerun `bootstrap` until it reports `AUTHENTICATED`.
5. Keep the bridge headed for the reliable ChatGPT path. Headless is optional only after a
   headless bootstrap proves the site accepts it.
6. Verify that the endpoint is loopback-only and that the bridge reports the expected process,
   profile and browser identity.
7. Bind the mailbox generation to the exact browser conversation and review guidance.
8. Grant only the browser operations required by the narrow gateway.
9. Register the reviewer with its stable reviewer ID and FIFO order.
10. Run an empty/read-only health transaction, then move WARMING to ACTIVE.

Bridge commands:

~~~powershell
./plugins/playwright-browser-bridge/scripts/Invoke-PlaywrightBrowserBridge.ps1 preflight -BrowserMode DedicatedChrome
./plugins/playwright-browser-bridge/scripts/Invoke-PlaywrightBrowserBridge.ps1 start -BrowserMode DedicatedChrome -BrowserCheck
./plugins/playwright-browser-bridge/scripts/Invoke-PlaywrightBrowserBridge.ps1 bootstrap
./plugins/playwright-browser-bridge/scripts/Invoke-PlaywrightBrowserBridge.ps1 status -BrowserCheck
~~~

Headed bootstrap retains at most one login tab and retires that session when authentication
succeeds. In headless service mode the command is a bounded authentication check only; an
interstitial means the reviewer must remain headed.

The raw bridge remains internal to the Post Office/review gateway. Review requesters never receive
Playwright or CDP tools.

## Review activation and return

- Validate and retain the exact review package before browser activation.
- Select one ACTIVE reviewer durably and bind the transaction to it.
- Each reviewer runs at most one active review and owns its own FIFO.
- A returned turn is only a scheduling signal until Review ID, activation dispatch, attachment
  identity, size and SHA-256 are verified.
- If the direct task API cannot expose the exact attachment path, use the bridge's
  `collect-attachment` command with the retained reviewer conversation UUID, result name, byte
  count, SHA-256, Review ID, and Activation Dispatch ID. Import the returned bytes immediately.
- Preserve results in local custody and return them only to the retained requester.
- Review output has REPORT authority; it grants no implementation or publication authority.
- A normal return wakes the collector through the completed-turn event. There is no polling
  heartbeat.

Google Drive is not the normal request or return path. Use local MCP publication and local
attachment collection. An explicit Drive compatibility fallback requires a recorded primary-path
failure and verified readback.

## Graceful retirement

1. Change ACTIVE to DRAINING before stopping any process.
2. Stop assigning new transactions.
3. Allow the current review and queued transactions already bound to that reviewer to finish, or
   move the reviewer to RECOVERY_REQUIRED and perform an explicit evidence-preserving recovery.
4. Confirm that no REVIEW_ACTIVE or returned-but-uncollected transaction remains bound to it.
5. Revoke browser capabilities and disable completed-turn wake creation.
6. Stop the bridge using its identity-verifying stop command.
7. Rotate the mailbox generation and remove it from scheduler and observer coverage.
8. Mark RETIRED with a reason and references to all final transaction and process receipts.

Retirement never deletes the ChatGPT conversation, profile, mailbox history or review evidence
automatically. Deletion, if ever desired, is a separate user-authorised retention decision.

## Emergency quarantine

Use QUARANTINED when browser identity, profile integrity, guidance binding or returned evidence is
suspect. Stop new assignments immediately, preserve the running process and logs when safe, and
move bound work to RECOVERY_REQUIRED. Do not silently activate another browser against the same
transaction.

## Diagnostic attachment to everyday Chrome

ExistingChrome is diagnostic-only. It requires Chrome remote debugging and may produce a fresh
consent prompt for each CDP connection. It can expose cookies, saved data and open tabs from the
user's everyday profile. It is not a substitute for DedicatedChrome and must not be used for normal
review scheduling.
