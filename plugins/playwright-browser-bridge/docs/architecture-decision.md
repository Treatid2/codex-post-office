# ADR-001: Playwright is a browser-transport companion

## Decision

Run Playwright MCP as an independently packaged, singleton browser-transport companion. Post Office
and automatic review consume its loopback HTTP endpoint through a narrower service boundary. Do not
start a raw Playwright MCP instance in every Codex task.

The default browser is ordinary Chrome launched and owned by the bridge with a dedicated persistent
profile and an ephemeral loopback CDP listener. Playwright MCP attaches to that exact listener and
shares the existing browser context. This split avoids the observed early termination of a Chrome
process launched under Playwright automation while retaining exact lifecycle ownership. It requires
one interactive ChatGPT sign-in and runs headed for the reliable ChatGPT path without touching the
user's normal Chrome cursor, installing another extension, or using Google Drive reads. Headless
remains optional where the target site accepts it.

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
- One service owner and one long-lived broker session for MCP delivery. Collection uses one bounded,
  short-lived Playwright client attached to the same bridge-owned Chrome listener so it can capture
  the signed attachment request and retain exact bytes without exposing CDP to callers.
- Fail-closed ownership of the broker, Playwright MCP, and dedicated Chrome using listener PID,
  creation time, command line, profile, and port.
- Serialized lifecycle mutation, interrupted-start recovery, and PID-reuse refusal.
- Bounded transient output; retained review evidence moves into Post Office custody.
- Approved production transport for cursor-free, manifest-bound local ChatGPT attachment delivery,
  and for manifest-bound collection when the direct task API cannot expose the file. The delivery
  marker, generation-bound collection packet, exact attachment identities, and local custody
  receipt make both directions replay-safe without a browser heartbeat.
- Collection verifies the exact source-turn UUID and correlation markers, captures only the signed
  request for the exact attachment name, retrieves it through Chrome's authenticated request
  context, and requires the declared byte count and SHA-256 before returning a receipt.
- Headed bootstrap verifies the isolated ChatGPT profile without returning page content and retains
  at most one login session. Headless bootstrap is a bounded authentication check; a site
  interstitial selects headed operation rather than automation-fingerprint evasion.
