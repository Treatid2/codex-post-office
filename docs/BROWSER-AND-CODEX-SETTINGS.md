# Browser and Codex settings

Several unrelated settings use similar developer/CDP terminology. Enable only the setting required
for the selected component.

| Setting | Normal production value | Required when |
|---|---|---|
| Chrome Extensions Developer mode | Off unless using an unpacked observer | Loading a locally developed Manifest V3 observer |
| Chrome remote debugging | Off | Diagnostic ExistingChrome mode only |
| Codex/ChatGPT desktop developer mode | Off unless adding an unverified local connector | Registering an unverified connector or WebMCP integration |
| Enforce CSP in developer mode | On when developer mode is enabled | Restricting unverified connector network behaviour |
| Codex desktop full CDP access | Off | A separately authorised Codex connected-browser diagnostic |
| Site tools / WebMCP discovery | Off unless the browser mailbox uses it | Discovering tools exposed by a configured website |
| Secure MCP Tunnel | Optional, narrow gateway only | A cloud browser must reach a local read-only Post Office gateway |

## Dedicated review browser

DedicatedChrome is the normal mode. Playwright owns a separate persistent profile and does not need
Chrome remote debugging, Chrome Extensions Developer mode, Codex full CDP access, or the user's live
cursor. One headed start is required for sign-in; later starts may be headless.

Treat the profile as a credential-bearing secret. Do not commit, share, package or place it under
the repository. Retire it through the reviewer lifecycle before deleting it.

## Existing Chrome diagnostic

ExistingChrome attaches to the user's current Chrome instance. Before use:

1. Open chrome://inspect/#remote-debugging.
2. Enable Allow remote debugging for this browser instance.
3. Accept Chrome's explicit full-control prompt.
4. Run only the bounded diagnostic.
5. Disable remote debugging afterward.

This mode can access saved data, cookies, site data and arbitrary URLs. Repeated prompts are expected
because consent can be connection-scoped. Do not make it the automatic-review default.

## Optional browser observer extension

A compatible Post Office deployment may use a privacy-bounded Manifest V3 observer to schedule
authoritative reads. Loading an unpacked extension requires Chrome Extensions Developer mode.

The observer may record conversation IDs, request timing/status, completion state, character counts
and attachment counts. It must not record message text, request/response bodies, cookies,
authorisation headers, prompts or arbitrary headers. Service-worker state belongs in
chrome.storage, because extension service workers are ephemeral.

After reloading an unpacked extension, reload each observed ChatGPT tab once. An Extension context
invalidated error means an old content script survived an extension reload; reloading the affected
tab attaches the new extension context. If observation remains incomplete or stale, fall back to an
authoritative browser read.

The observer is an optimisation and wake signal, never the mailbox, review result or delivery
authority. It may use a local tab-presence heartbeat for coverage but must not poll ChatGPT or the
Post Office.

## MCP and tunnels

- Raw Playwright listens only on 127.0.0.1.
- Never publish raw Playwright with ngrok, localtunnel or an unrestricted public listener.
- ChatGPT cannot directly reach a loopback endpoint.
- If Secure MCP Tunnel is used, terminate it at a narrow Post Office gateway exposing only the
  required mailbox operations.
- Store the generation-bound capability on the server side. Never embed it in a URL, connector
  manifest, repository or conversation.
- A successful loopback health probe proves local readiness only. Until the account-owned tunnel is
  provisioned and connected, report LOCAL_READY_TUNNEL_PENDING rather than a transport failure.

## Downloads, uploads and Google Drive

Allow browser downloads or uploads only where the chosen return path requires them. Prefer local
attachment paths and MCP payload transfer. Google Drive must not be used for routine retention,
queue state, backup or archive storage. Slow Drive access is not a reason to poll harder; it is a
reason to use the local primary path.

## Ports

The browser bridge defaults to 127.0.0.1:8931. A compatible Post Office MCP gateway may use a
different loopback port. Verify that each port is unoccupied before startup and that process
identity still matches before stop or retirement.
