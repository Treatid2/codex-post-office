# Playwright Browser Bridge

> **Development preview:** this is an unofficial Treatid2 plugin. It is not endorsed by OpenAI,
> Google, Microsoft, or the Playwright project, and its interfaces may change before 1.0.

Playwright Browser Bridge is a project-independent companion to Post Office Next. It provides one
singleton, pinned Playwright MCP runtime for structured, cursor-free access to a dedicated Chrome
profile. It does not own review queues, packages, results, authority, or retained Post Office data.

The runtime supports three browser modes:

- `DedicatedChrome` is the normal default. The bridge launches ordinary Chrome with a persistent
  review-only profile and an ephemeral loopback CDP listener, then makes Playwright attach to that
  exact listener. This avoids Chrome's unstable automation-launched session while retaining one
  bounded service owner. The profile needs one interactive ChatGPT sign-in. Headed operation is
  the reliable ChatGPT transport; headless is optional only where the site accepts it.
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
path, the compatible Post Office backend issues an immutable collection manifest. The trusted
courier can then collect that one exact file through the dedicated profile:

```powershell
./scripts/Invoke-PlaywrightBrowserBridge.ps1 collect-attachment `
  -ManifestPath '<post-office-state>/playwright-collections/<collection-id>/collection-manifest.json'
```

The manifest binds the collection ID, sweep, browser mailbox generation, ChatGPT conversation,
source turn, attachment reference, filename, byte count, SHA-256 and required correlation markers.
The collector is restricted to `https://chatgpt.com/c/<uuid>`, verifies the exact source-turn UUID
and its correlation markers, and first uses an exact-named attachment control when one is rendered.
If ChatGPT preserves a generated `sandbox:` reference but omits the in-thread control, the collector
uses the same authenticated browser session's Library. A Library candidate must match the filename,
byte count, conversation ID and originating assistant message; a user source message may correlate
only through the same immutable turn-exchange ID. The Library's own Download action then supplies
the bytes. Neither conversation metadata nor Library listings are retained. Both paths return only
a newly retrieved hash-matching file plus the exact
`playwright-chatgpt-collection:<collection-id>:<thread-id>:<sha256>` receipt. A compatible Post
Office backend must rehash and retain those bytes atomically before the transient bridge copy is
cleared. Replaying a retained collection returns its existing custody receipt; an interrupted
pre-retention attempt may redownload the same content, which deduplicates by mailbox and SHA-256.
Collection failures return stable error codes such as `ATTACHMENT_LIBRARY_UNRESOLVED`; compatible
Post Office wrappers record that retryable state and attempt count without weakening the manifest.

For Codex-to-browser delivery, Post Office first issues an immutable JSON dispatch manifest. The
bridge accepts only that manifest path on the command line; prompt text and attachment paths are
read from the retained packet:

```powershell
./scripts/Invoke-PlaywrightBrowserBridge.ps1 deliver-message `
  -ManifestPath '<post-office-state>/playwright-dispatches/<dispatch-id>/delivery-manifest.json'
```

The manifest binds one ChatGPT conversation UUID, mailbox generation, message ID, prompt, and up to
16 exact local attachments (maximum 256 MiB combined). Every attachment is size- and SHA-256-
verified before navigation. The bridge navigates only to `https://chatgpt.com/c/<uuid>`, uploads the
files, and prefixes the message with `POST-OFFICE-PLAYWRIGHT-DISPATCH <dispatch-id>`. On retry it
finds that exact marker and returns the retained receipt without sending a duplicate. Post Office
records the receipt only after the marker is visible in the bound conversation. Upload first uses
the page's native file input when present, with the visible upload menu/file chooser as a bounded
fallback. If a synthetic send-button click leaves the exact marker in the composer, one Enter-key
fallback is allowed; it is suppressed once the composer clears or the submitted marker appears.

P2.4 discovers the composer by enabled, editable textbox capability exposed through the browser's
accessibility semantics. Native `textarea` and editable surfaces are bounded fallbacks; submission
prefers the composer's own form action, then an accessible send action, then the stable ChatGPT
test hook. Localized placeholder strings are not selectors. The bridge still does not infer ledger
state: the courier records `WAITING_FOR_RECIPIENT`, `RECONCILIATION_REQUIRED`, `SUPERSEDED`, or
`TERMINAL_FAILURE`, and retries only the original immutable manifest.

Automatic-review activation uses a separate, exactly-one-package contract. The trusted courier asks
the review backend to issue an immutable activation manifest, then invokes:

```powershell
./scripts/Invoke-PlaywrightBrowserBridge.ps1 deliver-review-activation `
  -ManifestPath '<post-office-state>/playwright-activations/<activation-id>/activation-manifest.json'
```

The manifest binds the review ID, activation dispatch ID, reviewer conversation UUID, mailbox
generation, exact prompt and prompt SHA-256, plus the locally retained package name, size and
SHA-256. The bridge verifies and uploads that package before it prefixes the prompt with
`POST-OFFICE-REVIEW-ACTIVATION <review-id> <dispatch-id>`, uses the visible send control, and reads
the resulting user-turn UUID. A retry observes the marker and returns the same evidence instead of
sending again. Only the review backend may convert the exact
`playwright-chatgpt-review-activation:<review-id>:<dispatch-id>:<thread-id>:<source-message-id>`
receipt into `activation_state=SENT`; a failed attempt remains durably `PENDING_SEND` and retryable.

For an isolated disposable browser:

```powershell
./scripts/Invoke-PlaywrightBrowserBridge.ps1 start -BrowserMode ManagedChrome -Headless -BrowserCheck
```

Runtime state defaults to `%LOCALAPPDATA%\Codex\PostOfficeNext\playwright-browser-bridge`. It is
operational state, not repository content. Review artifacts must be imported into Post Office's
content-addressed custody before bridge output is cleared or expires.

The first collection installs the exact `playwright-core` version paired with the pinned MCP into
that operational state root. Its version is verified before use; it is not taken from a Codex
runtime cache or from `PATH`.

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

The dedicated bridge is the active local browser-automation path for exact Post Office delivery,
automatic-review activation, and collection when the direct task API cannot expose an attachment
path. Direct task reads remain
preferred when they are sufficient. The user's everyday Chrome and `ExistingChrome` are not part
of normal operation. Secure MCP Tunnel and raw public exposure remain out of scope.

The verified local deployment is headed because ChatGPT held this profile at an interstitial in
headless mode.

The normal dedicated path does not add stealth flags or modify browser fingerprints. Playwright's
disposable `ManagedChrome` test mode may show flags selected by the pinned upstream MCP package;
it is not the production ChatGPT transport.

## Tests

`npm test` exercises broker admission, queue, session-retirement, and response-size boundaries. It
also verifies authentication detection, bounded retained login sessions, exact-thread navigation,
mandatory correlation markers, stale-download rejection, exact attachment hashing, manifest-bound
uploads, review-activation syntax and manifest binding, Library origin correlation, exact Library
candidate selection, all composer/send discovery fallbacks, and delivery-marker replay
suppression. The
PowerShell integration test proves managed-browser operation, protected-root refusal, lifecycle
serialization, interrupted start recovery, and changed-process-identity refusal.
