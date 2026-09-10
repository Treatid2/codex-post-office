# Codex Post Office

Codex Post Office is an unofficial, community-maintained collection of local Codex plugins for
durable task communication, independent automatic code review, and cursor-free review-browser
access.

This project is not affiliated with, endorsed by, certified by, or supported by OpenAI. It is
published as source code under the [Mozilla Public License 2.0](LICENSE). Downstream installation,
environment compatibility, operation and upgrades remain the responsibility of each user.

The repository is a Codex-compatible marketplace containing three separately installable plugins:

- [post-office-next](plugins/post-office-next) — versioned contracts, legacy capture and
  reconciliation evidence, an isolated transactional database, and the P3.1 operational kernel.
- [automatic-code-review](plugins/automatic-code-review) — a least-authority review requester
  that can use a compatible Post Office backend without granting postal operations.
- [playwright-browser-bridge](plugins/playwright-browser-bridge) — a singleton, loopback-only
  Playwright MCP companion for manifest-bound browser delivery and exact attachment collection.

The separation is intentional. Review requesters do not receive mailbox administration or raw
browser/CDP authority, and the browser bridge owns no mail, review, or authorisation state.

## Current maturity

This is a **development preview**, not a complete replacement Post Office distribution.

- The post-office-next plugin implements the P0 contract gate, P0.1 isolation gate, P1 legacy
  evidence capture, the P2 database foundation, P2.1 deterministic migration rehearsal, and the
  isolated P3.1 authenticated operation kernel.
- It does not yet implement the complete production mailbox, routing, migration, or cutover
  runtime.
- automatic-code-review is a portable, fail-closed client for a separately deployed compatible
  backend. Its trusted administrator must generate a local deployment lock before use.
- playwright-browser-bridge is operational on supported Windows hosts for idempotent ChatGPT
  delivery, P2.3 automatic-review activation, P2.4 capability-based composer discovery, and exact
  attachment collection, but its raw MCP endpoint remains local-only.

Functionality will be added in response to demonstrated operational needs. The repository does not
promise speculative features, perpetual maintenance, compatibility with every Codex release, or
support for downstream forks.

The delivery-gate names P0, P0.1, P1, P2, P2.1, P2.2, P2.3, P2.4 and P3.1 are engineering milestones, not
defect severities. The numbered P2.x items are bounded implementation increments within P2, not
production releases.
Their exact definitions are in
[delivery-phases.md](plugins/post-office-next/docs/delivery-phases.md).

## Installation

Clone the repository, add its root as a local Codex marketplace, and install only the components
needed on that host:

~~~powershell
git clone https://github.com/Treatid2/codex-post-office.git
cd codex-post-office
codex plugin marketplace add .
codex plugin add post-office-next@treatid2
codex plugin add automatic-code-review@treatid2
codex plugin add playwright-browser-bridge@treatid2
~~~

The marketplace entries are AVAILABLE and ON_INSTALL; nothing is installed by default. This
repository is not submitted for inclusion in an official or curated Codex marketplace.

Read [installation and setup](docs/INSTALLATION.md) before enabling any browser or stateful
component.

## Operational principles

- SQLite and local content-addressed storage are the durable authority.
- Google Drive is not a database, archive, backup, or long-term retention layer. It is an
  exceptional compatibility fallback after a recorded primary-transport failure.
- Mailbox IDs are routing identities; capabilities are the authentication authority.
- Browser and mailbox generations are explicit. Rebinding invalidates stale-generation authority.
- The default review browser is a dedicated profile, not the user's everyday Chrome profile.
- Raw Playwright/CDP endpoints remain loopback-only and are never exposed through a public tunnel.
- Author acceptance and production cutover always require explicit authority.

## Documentation

- [Installation, dependencies and Codex setup](docs/INSTALLATION.md)
- [Mailbox construction and lifecycle](docs/MAILBOX-LIFECYCLE.md)
- [Automatic-review browser initialization and retirement](docs/AUTOMATIC-REVIEW-BROWSER.md)
- [Browser and desktop settings](docs/BROWSER-AND-CODEX-SETTINGS.md)
- [Security policy](SECURITY.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)
- [Change history](CHANGELOG.md)

## Licence

Unless a file states otherwise, this repository is licensed under the
[Mozilla Public License 2.0](LICENSE). MPL-2.0 applies at the file level: when modified covered
files are distributed, their source remains available under MPL-2.0, while independent surrounding
components may use other licences.
