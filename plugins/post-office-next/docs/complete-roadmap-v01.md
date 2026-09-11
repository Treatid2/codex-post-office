# Complete Post Office Next roadmap

This roadmap separates software capability gates from deployment state. Installing the plugin does
not activate a Post Office. A deployment becomes authoritative only through the one-time cutover
transaction described below.

## Phase meanings and current software status

| Gate | Meaning | Software status |
| --- | --- | --- |
| P0 | Architecture and public-contract baseline: the complete FGPM wishlist, vNext extensions, authority/concurrency/result contracts, and the rule that Google Drive is only a transient browser bridge. | Complete |
| P0.1 | P0 hardening: review-driven corrections to path isolation, capture identity, schema parity, diagnostics, evidence custody, and production-root exclusion. It is not a separate product release. | Complete |
| P1 | Exact read-only legacy capture, post-baseline payload delta, stable event boundary, and discrepancy classification. | Complete |
| P2 | New transactional SQLite substrate, append-only journal, local content-addressed store, backup/restore, and deterministic migration. | Complete |
| P2.1 | Exact import/replay/recovery rehearsal, including typed legacy preservation and byte-for-byte CAS verification. | Complete |
| P2.2 | Manifest-bound browser result collection with exact conversation, turn, filename, size, digest, and correlation verification. | Complete |
| P2.3 | Idempotent automatic-review activation through an immutable zero-attachment browser manifest. | Complete |
| P2.4 | Bounded courier reconciliation, unloaded-task return, ambiguous-send recovery, obsolete-dispatch retirement, and event-driven wakes. | Complete |
| P3.1 | Authenticated default-deny dispatcher, capability checks, durable idempotency, aggregate compare-and-swap, and hash-chained events. | Complete |
| P3.2 | Exact author actions, delegated grants, endpoint lifecycle, mailbox allocation, and atomic generation rotation. | Complete |
| P3.3 | Secure project/task plans and lifecycle, caller-credential binding, endpoint binding, and provisioning inspection. | Complete |
| P3.4 | Package, interface, cycle, message, bundle, capability-request, change-set, context, and integration workflow. | Complete |
| P3.5 | Local/native/Playwright transport, short execution leases with automatic recovery, attention projection, review FIFO, ensure/withdraw, and return wakes. | Complete |
| P3.6 | Shadow comparison, anomaly decisions, rehearsal evidence, immutable dossier, preflight, and guarded authority transfer. | Complete |

`mode=ISOLATED` or `mode=SHADOW` records how a kernel was prepared. Production authority is a
separate `authorityState`. A verified P3.6 transfer changes that state to `AUTHORITATIVE`; it does
not erase the retained preparation provenance.

## One-time deployment cutover

Cutover is a deployment action after P3.6, not another implementation phase:

1. quiesce legacy writes and classify every open transport, review, wake, outbox, and lease;
2. freeze the final database boundary and copy every post-baseline payload into local custody;
3. import and replay independently, compare logical/event/CAS/open-work roots, and verify a restored
   backup;
4. provision replacement task and courier credentials without exporting secrets;
5. produce a ready dossier from the same boundary and require the exact author-approved root;
6. commit the authority transfer and local activation pointer atomically;
7. rebind task, courier, review, and browser entry points;
8. run read, native delivery, browser delivery, review, lease recovery, and backup smoke tests; and
9. retain the legacy database and CAS as immutable evidence. After the first vNext mutation,
   recovery is forward-only rather than a database toggle.

## P4 — post-cutover operation

P4 is driven by observed operational needs and does not reopen or weaken the cutover.

### P4.1 — continuity drain and operator ergonomics

- Drain migrated continuation items through exact destination bindings and external receipts.
- Expose normalized destination thread, host, URL, mailbox, and generation on courier claims.
- Keep leases short and automatically recover expired work; a lease expiry is retryable work, not
  an author blocker.
- Provide secret-free queue, attention, and continuity status suitable for a bounded operator
  sweep.

Delivered increments: migrated claims expose normalized destinations; obsolete work can be retired
without fabricating completion; and an exact, already-delivered migrated message can receive an
idempotent supplemental direct-transport receipt without rewriting its original fallback history.
The operator status command now reports this state without credentials or secrets. Expired
continuation leases require performed-or-absent evidence; ambiguous expiry is held for attention.

### P4.2 — transport reliability proof

- Prove the normal native and Playwright paths perform no Google Drive access.
- Retain per-attempt timing, browser receipt, payload digest, and fallback reason.
- Treat connector denial, browser ambiguity, and missing attachment evidence as distinct states.
- Exercise crash recovery at intent, send, observation, custody, receipt, and return boundaries.

### P4.3 — upgrades, backup, and retirement

- Add an upgrade rehearsal that clones the authoritative database, applies migrations, verifies the
  event chain and logical roots, and then performs a guarded in-place upgrade.
- Schedule and verify local database/CAS backups without storing secrets in receipts.
- Retire old browser instances, task bindings, credentials, and compatibility shims only after
  outstanding work is drained or explicitly superseded.
- Preserve a documented forward-recovery procedure for an authoritative deployment.

Delivered increment: a backup-first migration-history re-attestation command repairs only
digest-only drift when versions, names, the complete schema, integrity checks, and foreign keys all
match. It refuses drifted migrations containing data-changing or destructive SQL and retains the
pre-repair database plus an immutable receipt. Full forward upgrade rehearsal remains outstanding.

The authoritative Treatid2 deployment also retains a verified local database backup and a
separately restored, root-matching copy. General forward schema migration remains deliberately
unimplemented until a real migration exists to rehearse; current-version backup/restore is not
mislabelled as an upgrade.

### P4.4 — public release hardening

- Keep the three plugins independently installable and least-authority.
- Run contract generation/validation, full tests, migration rehearsal, licence checks, and secret
  scanning before each tagged release.
- Publish source and release notes under MPL-2.0 without claiming OpenAI endorsement or official
  marketplace inclusion.
- Evaluate independent review findings as evidence and improve the implementation where useful;
  review does not supply implementation authority and does not block unrelated operation.

No later feature phase is specified until an actual operational requirement exists. This avoids
turning plausible conveniences into unsupported promises.

## Interruption policy

Courier work and implementation share one durable queue. A bounded incoming wake or return is
handled, receipted, and closed before engineering resumes. Unrelated long-running work remains
leased and recoverable; it is not converted to `BLOCKED` merely because another task arrived.
Automatic-review requesters do not need heartbeats because a verified return creates their wake.
The Post Office operator is the exceptional component responsible for issuing those wakes.
