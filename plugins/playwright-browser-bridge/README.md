# Playwright Browser Bridge

> **Development preview:** this is an unofficial Treatid2 plugin. It is not endorsed by OpenAI,
> Google, Microsoft, or the Playwright project, and its interfaces may change before 1.0.

Playwright Browser Bridge is a project-independent companion to Post Office Next. It provides one
singleton, pinned Playwright MCP runtime for structured, cursor-free access to a dedicated Chrome
profile. It does not own review queues, packages, results, authority, or retained Post Office data.

The runtime supports three browser modes:

- `DedicatedChrome` is the normal default. Playwright owns a persistent review-only profile,
  which needs one interactive ChatGPT sign-in and can then run headless.
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

On its first headed start, sign in to ChatGPT in the dedicated Chrome window. Stop the bridge and
restart with `-Headless` for unattended operation; the authenticated profile is retained under the
runtime state root. The profile is browser state, not review evidence or long-term Post Office data.

Diagnostic `ExistingChrome` validates both lines of Chrome's `DevToolsActivePort` file and connects
to its exact, ephemeral WebSocket endpoint. It does not assume that Chrome's `/json/version`
discovery endpoint is available. Because Chrome consent is connection-scoped, this mode is not the
normal review transport.

The default endpoint is `http://127.0.0.1:8931/mcp`. The wrapper refuses an occupied port, binds
only IPv4 loopback, allowlists the exact loopback host and port, records the exact listener identity, and refuses to
stop a reused or mismatched process ID. Browser images are omitted from MCP responses by default to
limit transport size. Output is capped at 256 MiB in the runtime state directory.

`start` uses a hidden process and returns immediately after an MCP protocol probe. Add
`-BrowserCheck` to require a real `browser_tabs` call before success. There is deliberately no
heartbeat; callers probe at the point of use and normal MCP traffic provides liveness.

For an isolated disposable browser:

```powershell
./scripts/Invoke-PlaywrightBrowserBridge.ps1 start -BrowserMode ManagedChrome -Headless -BrowserCheck
```

Runtime state defaults to `%LOCALAPPDATA%\Treatid2\CodexPostOffice\playwright-browser-bridge`. It is
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
