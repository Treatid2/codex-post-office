---
name: browser-bridge
description: "Operate the pinned, loopback Playwright browser bridge used by Codex Post Office companions: preflight, start, probe, inspect, and stop a dedicated or diagnostic Chrome session. Do not expose or tunnel the raw browser endpoint."
---

# Playwright Browser Bridge

This companion owns one pinned Playwright MCP process, one ordinary Chrome process, and one browser
profile. Chrome exposes an ephemeral loopback CDP listener and Playwright attaches to the existing
context. It supplies browser transport; it does not own mailboxes, reviews, packages, results,
authority, or retained evidence.

## Normal operation

Use `scripts/Invoke-PlaywrightBrowserBridge.ps1` for every lifecycle operation:

```powershell
./scripts/Invoke-PlaywrightBrowserBridge.ps1 preflight
./scripts/Invoke-PlaywrightBrowserBridge.ps1 start -BrowserMode DedicatedChrome -BrowserCheck
./scripts/Invoke-PlaywrightBrowserBridge.ps1 bootstrap
./scripts/Invoke-PlaywrightBrowserBridge.ps1 status -BrowserCheck
./scripts/Invoke-PlaywrightBrowserBridge.ps1 probe -BrowserCheck
./scripts/Invoke-PlaywrightBrowserBridge.ps1 stop
```

`DedicatedChrome` is the normal mode. Its first start is headed so a human can authenticate the
review-only account. Run `bootstrap`; when it reports `AUTHENTICATION_REQUIRED`, let the human sign
in and rerun it until it reports `AUTHENTICATED`. Keep it headed for reliable ChatGPT operation.
Use `-Headless` only after a headless `bootstrap` proves the
target site accepts it; an authentication interstitial requires headed operation. `ManagedChrome`
is disposable test/recovery state. `ExistingChrome` is diagnostic-only because attaching to a live
personal Chrome session has broad authority and may require consent for every connection.

Keep the listener on `127.0.0.1`. Never expose it through a public tunnel, change it to a wildcard
bind, or grant arbitrary filesystem access. A remote or cross-task caller must use a narrow,
authenticated application gateway that exposes only its own domain operations.

## Lifecycle and retirement

Treat a dedicated browser as `PROVISIONING`, `WARMING`, `ACTIVE`, `DRAINING`, or `RETIRED`.
`QUARANTINED` and `RECOVERY_REQUIRED` are exceptional states. Stop assigning new work before
draining. Retire only after outstanding work has returned or been explicitly recovered, then revoke
its mailbox or reviewer binding before removing its profile. Do not delete a profile merely because
a tab or extension was refreshed.

The bridge has no heartbeat. Probe at the point of use and treat normal MCP traffic as liveness.
Import any result that must be retained into Post Office custody before clearing browser output.

Use `deliver-message` only with an immutable Post Office `delivery-manifest.json`. Do not construct
the prompt or attachment list in the browser task or place either on the command line. A successful
result must contain the exact `playwright-chatgpt:<dispatch-id>:<thread-id>` receipt; record that
through the Post Office receipt operation before treating the message as delivered. Reusing the
same manifest is the supported recovery path: the exact visible dispatch marker suppresses a
duplicate send. Composer discovery is capability-based: prefer an enabled editable accessibility
textbox, then a native textarea or editable surface; prefer the composer's own form submission,
then an accessible send action, then the stable ChatGPT test hook. Never add localized placeholder
text as a selector. If the outcome is ambiguous, have the Post Office caller record
`RECONCILIATION_REQUIRED` before yielding. A genuinely non-addressable recipient is
`WAITING_FOR_RECIPIENT`; an obsolete unreceipted dispatch must be durably superseded before a new
manifest is issued.

Use `deliver-review-activation` only with an immutable, exactly-one-package
`activation-manifest.json` issued by the automatic-review backend. This is deliberately distinct
from ordinary mail: it binds the exact review, activation dispatch, reviewer conversation, mailbox
generation, prompt, and prompt hash. Require the exact browser receipt and user-turn UUID, then
record both atomically through the review backend. On failure, record the bounded retryable failure;
do not mark the activation sent, invent a native connector receipt, or resubmit the review.

Use `collect-attachment` only with the immutable `collection-manifest.json` issued after an
authoritative task read. The manifest binds the exact ChatGPT conversation UUID, browser mailbox
generation, sweep, source turn, attachment reference, filename, byte count, SHA-256, and required
correlation markers. The command attaches a bounded pinned Playwright client to the bridge-owned
Chrome listener, verifies the exact source turn, and prefers the exact rendered attachment control.
When ChatGPT omits that control, it may use the authenticated ChatGPT Library only after matching
the exact filename, byte count, conversation and originating message (or the source message's exact
turn-exchange correlation), then invoke Library's own Download action. It returns only newly
retrieved, hash-matching bytes plus its collection receipt. The Post Office caller must rehash and
retain those bytes before clearing the transient file. The bridge does not interpret the attachment
or grant mail/review authority. Preserve and record any returned collection error code; do not
collapse it to a generic pending state.
