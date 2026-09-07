---
name: browser-bridge
description: "Operate the pinned, loopback Playwright browser bridge used by Codex Post Office companions: preflight, start, probe, inspect, and stop a dedicated or diagnostic Chrome session. Do not expose or tunnel the raw browser endpoint."
---

# Playwright Browser Bridge

This companion owns one pinned Playwright MCP process and one browser profile. It supplies browser
transport; it does not own mailboxes, reviews, packages, results, authority, or retained evidence.

## Normal operation

Use `scripts/Invoke-PlaywrightBrowserBridge.ps1` for every lifecycle operation:

```powershell
./scripts/Invoke-PlaywrightBrowserBridge.ps1 preflight
./scripts/Invoke-PlaywrightBrowserBridge.ps1 start -BrowserMode DedicatedChrome -BrowserCheck
./scripts/Invoke-PlaywrightBrowserBridge.ps1 status -BrowserCheck
./scripts/Invoke-PlaywrightBrowserBridge.ps1 probe -BrowserCheck
./scripts/Invoke-PlaywrightBrowserBridge.ps1 stop
```

`DedicatedChrome` is the normal mode. Its first start is headed so a human can authenticate the
review-only account. After authentication, stop it and restart with `-Headless`. `ManagedChrome` is
disposable test/recovery state. `ExistingChrome` is diagnostic-only because attaching to a live
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
