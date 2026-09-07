# Automatic Code Review

> **Development preview:** this is an unofficial Treatid2 plugin. It is not endorsed by OpenAI,
> Google, Microsoft, or the Playwright project, and its interfaces may change before 1.0.

Automatic Code Review is a project-independent companion to Post Office. Its public interface is
review-specific; it does not expose mailboxes, routing, handoffs, or cycle control. Post Office may
provide retained package custody, local MCP browser transport, reviewer queues, monitoring, and
verified result return behind that interface. Google Drive is an exceptional compatibility path,
not the normal review transport.

Every Codex task may request review access without obtaining a postal mailbox or asking the author
for a separate permission step. On first use, the companion asks the trusted courier to issue a
least-authority bearer capability recorded against the task/host routing identity with only
`review-submit`, `review-status`, and `review-complete`. The capability is stored in the normal
protected caller-secret location and is never printed or copied into a package.

The public client does not bundle a production Post Office backend. A trusted administrator runs
`Configure-AutomaticCodeReview.ps1` once to select the installed Python runtime, registered
`auto_review.py` service entry point, and Post Office state root. The script writes a deployment
lock beneath `%LOCALAPPDATA%\Treatid2\CodexPostOffice\automatic-code-review`; it contains paths and
hashes, not credentials. The client invokes that exact Python runtime in isolated mode and attests
the runtime, client, and security-relevant backend modules before and after every invocation. It
never searches `PATH`, accepts a caller capability override, or inherits a caller-selected hub root.

Example administrator setup:

```powershell
./scripts/Configure-AutomaticCodeReview.ps1 `
  -PythonExecutable <python.exe> `
  -BackendEntrypoint <auto_review.py> `
  -StateRoot <post-office-state>
```

Package admission is byte-snapshot based end to end. The client selects one stable source ZIP and
passes a private copy; the registered service then stable-reads that copy once, performs all ZIP and
manifest inspection in memory, calculates the whole-package identity from the same bytes, and uses
`retain_bytes` for durable custody. Contract identity, package digest, retained payload, idempotency
fingerprint, and transaction metadata therefore cannot come from different pathname versions.

```powershell
./scripts/Invoke-AutomaticCodeReview.ps1 access
./scripts/Invoke-AutomaticCodeReview.ps1 inspect --package <review.zip>
./scripts/Invoke-AutomaticCodeReview.ps1 submit --package <review.zip> --subject <subject> --readiness PR_SCALE_NEAR_COMPLETE --idempotency-key <key>
./scripts/Invoke-AutomaticCodeReview.ps1 ensure --package <review.zip> --subject <subject> --readiness PR_SCALE_NEAR_COMPLETE --idempotency-key <key>
./scripts/Invoke-AutomaticCodeReview.ps1 status --review-id <id>
./scripts/Invoke-AutomaticCodeReview.ps1 complete --review-id <id> --summary <summary>
```

`CODEX_THREAD_ID` is required because that is the only identity form supported end to end by the
registered service. `CODEX_SESSION_ID` alone is explicitly unsupported. `CODEX_HOST_ID` defaults to
`local`. These task/host fields bind routing and transaction records; within one local Windows
account, the review-only bearer capability is the authentication principal. The client does not
claim per-task operating-system isolation or a second independent identity factor.

`access` checks only whether the protected task credential exists; it never reads or prints the
secret and does not query Post Office tables. The backend validates exact operations on first use.
All non-help paths, including wrapper bootstrap failures, return one versioned JSON result or
diagnostic. Backend execution is the direct Python child, has no service-operation descendant, and
has a bounded wait. A timeout is reported as an unknown durable outcome with no automatic retry and
the original idempotency key preserved for reconciliation.

`ensure` is the crash-safe submission boundary. In one backend transaction it reconciles the
supplied package and idempotency identity with the requester's retained reviews and returns exactly
one of `ACTIVE_DO_NOT_RESUBMIT`, `RETURNED_COMPLETE_REQUIRED`, `TERMINAL`,
`ABSENT_SUBMIT_CREATED`, or `CONFLICT`. It never creates a second standard review while another
review from that requester is open.

Normal delivery and monitoring use the local MCP browser path. Google Drive is an exceptional
compatibility bridge only and is never retained storage.

See [the architecture decision](docs/architecture-decision.md) for the boundary and alternatives.
This plugin is distributed under the Mozilla Public License 2.0; see the repository `LICENSE`.
