# ADR-001: Playwright is a browser-transport companion

## Decision

Run Playwright MCP as an independently packaged, singleton browser-transport companion. Post Office
and automatic review consume its loopback HTTP endpoint through a narrower service boundary. Do not
start a raw Playwright MCP instance in every Codex task.

The default browser is a dedicated persistent Chrome profile launched and owned by Playwright. It
requires one interactive ChatGPT sign-in and runs headed for the reliable ChatGPT path without
touching the user's normal Chrome cursor, installing another extension, or using Google Drive
reads. Headless remains optional where the target site accepts it.

Attaching to the user's existing Chrome remains a diagnostic mode. It reads and validates both lines
of `DevToolsActivePort` and uses the exact WebSocket endpoint, but current Chrome consent is
connection-scoped and can produce repeated or delayed prompts. It is therefore unsuitable for the
normal unattended review path. A managed isolated mode remains available for tests and recovery.

## Rejected placements

- **Inside Post Office core:** rejected because raw browser actions are transport mechanics, not
  durable mail, custody, authority, or queue semantics.
- **Inside automatic-review caller API:** rejected because callers should request reviews, not
  acquire general browser control.
- **VS Code-managed server:** rejected because an editor session is not a reliable service owner and
  would make review availability depend on a particular interactive client.
- **Browser-extension bridge:** rejected as the primary route because it adds another privileged
  extension lifecycle and repeats the stale/ownership failures of the previous browser path.
- **Per-task Playwright stdio:** rejected because each task creates another browser/CDP connection,
  repeats consent, and would grant raw browser authority to review callers.

## Operational properties

- Exact Playwright MCP version pin.
- Loopback-only HTTP listener and exact host-header restriction.
- Bounded ingress bodies, queue depth and bytes, end-to-end request deadlines, and upstream
  responses.
- No heartbeat; protocol probes and calls establish point-of-use liveness.
- One service owner and one long-lived broker session; no reconnect per review caller.
- Fail-closed process ownership using listener PID, creation time, command line, and port.
- Serialized lifecycle mutation, interrupted-start recovery, and PID-reuse refusal.
- Bounded transient output; retained review evidence moves into Post Office custody.
- Approved production transport for cursor-free, manifest-bound local ChatGPT attachment delivery,
  and for collection when the direct task API cannot expose the file. The delivery marker and exact
  attachment identities make retries idempotent without a browser heartbeat.
- Headed bootstrap verifies the isolated ChatGPT profile without returning page content and retains
  at most one login session. Headless bootstrap is a bounded authentication check; a site
  interstitial selects headed operation rather than automation-fingerprint evasion.
