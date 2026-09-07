# Security policy

Codex Post Office is an unofficial development preview. It is not endorsed by OpenAI, Google,
Microsoft, or the Playwright project and is not ready for production mail or review custody.

Do not commit or attach Post Office runtime databases, retained payloads, browser transcripts,
Google Drive objects, review packages/results, task capabilities, caller-secret files, captures,
snapshots, reconciliation evidence, or other operational data to this repository.

Report suspected vulnerabilities through
[GitHub private vulnerability reporting](https://github.com/Treatid2/codex-post-office/security/advisories/new).
Do not include live credentials or private runtime data in a public issue.

The current code is pre-release and must not be pointed at a production Post Office state root.
Keep raw browser endpoints on loopback, use dedicated browser profiles, and never commit deployment
locks or generated browser profiles.
