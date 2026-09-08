# Installation and setup

## Scope

This release is a Windows development preview. It packages three independent Codex plugins but does
not yet provide a complete production Post Office backend. Install only the components needed on a
given host.

The project is unofficial and is not affiliated with or endorsed by OpenAI.

## Required software

| Component | Required version or role | Needed by |
|---|---|---|
| Codex desktop or CLI | A version supporting local plugin marketplaces | All plugins |
| Git | Current supported release | Cloning and updating |
| PowerShell | 7 or newer | All Windows wrappers |
| Python | 3.12 or newer | Post Office Next and automatic review |
| Node.js | 20 or newer, including npm/npx | Playwright browser bridge |
| Google Chrome | Current supported release | Dedicated or attached browser operation |

VS Code is optional and may be used as an independent MCP diagnostic client. It is not a runtime
dependency.

An OpenAI API key is not required for the local loopback browser bridge. A separately provisioned
OpenAI Platform API key and tunnel identifier are required only if an account owner elects to use
OpenAI Secure MCP Tunnel. A ChatGPT subscription should not be assumed to supply those credentials.

## Add the marketplace

~~~powershell
git clone https://github.com/Treatid2/codex-post-office.git
Set-Location codex-post-office
codex plugin marketplace add .
~~~

Install only the required plugins:

~~~powershell
codex plugin add post-office-next@treatid2
codex plugin add automatic-code-review@treatid2
codex plugin add playwright-browser-bridge@treatid2
codex plugin list
~~~

Start a new Codex task after installation or update so newly installed skills are loaded.

## Post Office Next

The current plugin provides contracts, capture, reconciliation-preview, isolated database
foundation, and deterministic migration/replay/recovery commands. It must not be pointed at
production state for mutation or cutover.

~~~powershell
./plugins/post-office-next/scripts/Invoke-PostOfficeNext.ps1 contracts validate
~~~

Set `CODEX_PYTHON` to the absolute path of a trusted Python 3 entry point before invoking the
wrapper. Codex-managed installations should use their stable shared Python entry point rather than
a runtime-cache copy. The wrapper's JSON output identifies success or a stable diagnostic.

## Automatic code review

The public preview contains a portable client, not a production review backend. A trusted
administrator must already operate a compatible backend whose entry point is auto_review.py, whose
adjacent dependency is codex_comms.py, and whose state root contains hub.sqlite3.

Create the machine-local deployment lock from a trusted administrator context:

~~~powershell
& ./plugins/automatic-code-review/scripts/Configure-AutomaticCodeReview.ps1 `
  -PythonExecutable <absolute-python-path> `
  -BackendEntrypoint <absolute-auto-review-backend-path> `
  -StateRoot <absolute-post-office-state-root>
~~~

The command records resolved paths and SHA-256 attestations in:

~~~text
%LOCALAPPDATA%\Treatid2\CodexPostOffice\automatic-code-review\runtime-lock.json
~~~

This file is deployment state. Do not commit, package or send it. Normal review tasks cannot select
another backend, state root, Python executable or capability through arguments or environment
variables. When the backend is deliberately updated, rerun configuration from the trusted
administrator context before making the new version available.

After configuration:

~~~powershell
./plugins/automatic-code-review/scripts/Invoke-AutomaticCodeReview.ps1 access
~~~

A requester receives only review-submit, review-status and review-complete authority. It does not
need or receive a postal mailbox.

## Playwright browser bridge

Run the preflight before installation-dependent work:

~~~powershell
./plugins/playwright-browser-bridge/scripts/Invoke-PlaywrightBrowserBridge.ps1 preflight
~~~

The wrapper pins the Playwright MCP package version. First use may require npm network access to
obtain that exact package. The package lock records the expected transitive versions and integrity
values.

For the normal review browser, perform one headed initialization:

~~~powershell
./plugins/playwright-browser-bridge/scripts/Invoke-PlaywrightBrowserBridge.ps1 start -BrowserMode DedicatedChrome -BrowserCheck
~~~

Sign in to ChatGPT only in the dedicated Chrome window. Keep the dedicated service headed for the
reliable ChatGPT path:

~~~powershell
./plugins/playwright-browser-bridge/scripts/Invoke-PlaywrightBrowserBridge.ps1 bootstrap
~~~

Headed bootstrap retains at most one authentication tab while login is required and retires it
after authentication succeeds. `-Headless` is optional only after a headless bootstrap proves the
site accepts it. If login is required or an interstitial appears, operate headed.

The default endpoint is loopback-only. Never expose the raw Playwright endpoint through ngrok,
localtunnel, port forwarding or Secure MCP Tunnel. A tunnel, when required, terminates at a narrow
Post Office gateway.

The delivery command consumes only an immutable manifest issued by a compatible Post Office
backend. It verifies each local attachment and suppresses duplicate sends by checking the exact
dispatch marker in the bound ChatGPT conversation:

~~~powershell
./plugins/playwright-browser-bridge/scripts/Invoke-PlaywrightBrowserBridge.ps1 deliver-message `
  -ManifestPath <absolute-delivery-manifest-path>
~~~

Do not hand-author this manifest. The backend must bind its message, mailbox generation,
conversation UUID, payload order, byte sizes and SHA-256 values before invoking the bridge.

## Data and credentials

- Runtime databases, payloads, evidence and browser profiles live outside the repository.
- Capability values never belong in chat, command arguments, URLs, logs or source control.
- Google Drive is not required for normal retention or browser transfer.
- Any compatibility fallback to Drive requires a recorded primary-transport failure and verified
  readback; it must not become an archive.
- Back up the authoritative database and content-addressed payload store together.

See [browser and Codex settings](BROWSER-AND-CODEX-SETTINGS.md) before enabling developer or CDP
features.
