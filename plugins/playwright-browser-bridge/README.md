# Playwright Browser Bridge

> **Development preview:** this is an unofficial Treatid2 plugin. It is not endorsed by OpenAI,
> Google, Microsoft, or the Playwright project, and its interfaces may change before 1.0.

Playwright Browser Bridge is a project-independent companion to Post Office Next. It provides one
singleton, pinned Playwright MCP runtime for structured, cursor-free access to a dedicated Chrome
profile. It does not own review queues, packages, results, authority, or retained Post Office data.

The runtime supports three browser modes:

- `DedicatedChrome` is the normal default. Playwright owns a persistent review-only profile,
  which needs one interactive ChatGPT sign-in. Headed operation is the reliable ChatGPT transport;
  headless is optional only where the site accepts it.
- `ManagedChrome` is an isolated, disposable browser for tests and recovery.
- `ExistingChrome` attaches to the user's live Chrome through CDP and is diagnostic-only because
  current Chrome versions may request consent for every new CDP connection.

The plugin intentionally does not register a per-task raw MCP server. Per-task processes would
reconnect to Chrome, repeat consent prompts, contend for the persistent profile, and expose broad
browser authority. The automatic-review companion is the project-independent caller-facing tool;
its broker owns one long-lived MCP session to this loopback service.

VS Code is not a runtime dependency. It may be used as an independent diagnostic MCP client.

## Prerequisites

- Windows PowerShell 7 or newer.
- Node.js 20 or newer. The runtime is pinned to `@playwright/mcp@0.0.80`.
- Google Chrome.
- Only for diagnostic `ExistingChrome`, open `chrome://inspect/#remote-debugging` in Chrome and enable **Allow
  remote debugging for this browser instance**. This is separate from ChatGPT developer mode and
  the Codex desktop full-CDP permission.

## Managed HTTP runtime

```powershell
./scripts/Invoke-PlaywrightBrowserBridge.ps1 preflight
./scripts/Invoke-PlaywrightBrowserBridge.ps1 start -BrowserMode DedicatedChrome -BrowserCheck
./scripts/Invoke-PlaywrightBrowserBridge.ps1 status -BrowserCheck
./scripts/Invoke-PlaywrightBrowserBridge.ps1 stop
```

On its first headed start, sign in to ChatGPT in the dedicated Chrome window. Keep the service
headed for reliable ChatGPT operation; it uses its own profile and does not take the user's normal
Chrome cursor. `-Headless` is an optional deployment mode only after `bootstrap` proves the target
site accepts it. The authenticated profile is retained under the runtime state root. The profile is
browser state, not review evidence or long-term Post Office data.

Diagnostic `ExistingChrome` validates both lines of Chrome's `DevToolsActivePort` file and connects
to its exact, ephemeral WebSocket endpoint. It does not assume that Chrome's `/json/version`
discovery endpoint is available. Because Chrome consent is connection-scoped, this mode is not the
normal review transport.

The default endpoint is `http://127.0.0.1:8931/mcp`. The wrapper refuses an occupied port, binds
only IPv4 loopback, allowlists the exact loopback host and port, records the exact listener identity,
and refuses to stop a reused or mismatched process ID. It bounds incomplete request bodies, queue
depth and bytes, request deadlines, and upstream responses. Browser images are omitted from MCP
responses by default. Output is capped at 256 MiB in the runtime state directory.

`start` uses hidden service processes and returns immediately after an MCP protocol probe. The
headed Chrome window remains visible during first-use authentication. Add
`-BrowserCheck` to require a real `browser_tabs` call before success. There is deliberately no
heartbeat; callers probe at the point of use and normal MCP traffic provides liveness.

After the first headed start, use `bootstrap` to open ChatGPT through the bridge and determine
whether the isolated profile is authenticated without returning page text:

```powershell
./scripts/Invoke-PlaywrightBrowserBridge.ps1 bootstrap
```

`bootstrap` is deliberately limited to `https://chatgpt.com/`; it is not a general navigation
wrapper and refuses managed or attached sessions. In headed mode it retains at most one login tab
while authentication is required. In headless mode it performs an authentication check and closes
its MCP session; if login is required or the site presents an interstitial, operate headed.

When the direct task API identifies a completed ChatGPT attachment but cannot expose its local
path, the trusted courier can collect that one exact file through the dedicated profile:

```powershell
./scripts/Invoke-PlaywrightBrowserBridge.ps1 collect-attachment `
  -ThreadId '<conversation-uuid>' `
  -AttachmentName '<exact-name>' `
  -ExpectedBytes <exact-byte-count> `
  -ExpectedSha256 '<exact-sha256>' `
  -RequiredText @('<review-id>', '<activation-dispatch-id>')
```

The collector is restricted to `https://chatgpt.com/c/<uuid>`, performs a bounded conversation
scan for exact-named actionable attachment controls, rejects stale output files, and returns only
a newly downloaded file whose size and SHA-256 match. Repeated exact controls are safe because the
declared content identity remains authoritative. The caller must then import that file into Post
Office custody before clearing the transient bridge output.

For an isolated disposable browser:

```powershell
./scripts/Invoke-PlaywrightBrowserBridge.ps1 start -BrowserMode ManagedChrome -Headless -BrowserCheck
```

Runtime state defaults to `%LOCALAPPDATA%\Codex\PostOfficeNext\playwright-browser-bridge`. It is
operational state, not repository content. Review artifacts must be imported into Post Office's
content-addressed custody before bridge output is cleared or expires.

## Security boundary

Browser automation has authority over the dedicated review session. Keep the HTTP listener on
loopback and do not enable unrestricted file access. The
bridge does not expose `0.0.0.0`, install a browser extension, use Google Drive for retention, or
grant postal authority.

This plugin is distributed under the Mozilla Public License 2.0. See the repository `LICENSE` and
`THIRD_PARTY_NOTICES.md` files.

The Secure MCP Tunnel must target the narrow Post Office review gateway, not this raw Playwright
endpoint. The gateway calls the bridge internally and exposes only review-specific operations.

## Current state

The dedicated bridge is the active local browser-automation path for Post Office collection when
the direct task API cannot expose an attachment path. Direct task reads and sends remain preferred
when they are sufficient. The user's everyday Chrome and `ExistingChrome` are not part of normal
operation. Secure MCP Tunnel and raw public exposure remain out of scope.

The verified local deployment is headed because ChatGPT held this profile at an interstitial in
headless mode.

Pinned Playwright MCP `0.0.80` injects Chrome's
`--disable-blink-features=AutomationControlled` launch argument, so Chrome displays an unsupported
flag banner. The bridge does not add stealth plugins or modify browser fingerprints; removing that
banner requires an upstream opt-out or a later pinned version that no longer injects the argument.

## Tests

`npm test` exercises broker admission, queue, session-retirement, and response-size boundaries. It
also verifies authentication detection, bounded retained login sessions, exact-thread navigation,
mandatory correlation markers, stale-download rejection, and exact attachment hashing. The
PowerShell integration test proves managed-browser operation, protected-root refusal, lifecycle
serialization, interrupted start recovery, and changed-process-identity refusal.
