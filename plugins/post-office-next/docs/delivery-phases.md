# Post Office Next delivery phases

These labels describe engineering gates, not priority severities and not production releases.

- **P0 — architecture and public-contract baseline.** Restore the complete 55-operation FGPM
  wishlist, retain nine explicitly labelled vNext extensions, define authority/concurrency/result
  contracts, and establish the rule that Google Drive is only a transient browser bridge.
- **P0.1 — P0 hardening and independent-review remediation.** Correct defects found while proving
  P0's contracts and isolation boundaries. The current P0.1 increment covers path topology,
  read-only database identity preflight, exact migration/schema identity, coherent diagnostics and
  result polarity, Draft-07 parity, capture-root verification, cycle-mapping non-inference, and
  local-storage/bridge binding. It also requires production-root exclusion at every write entry,
  content-complete database identities, database-aware capture consistency, strict local-evidence
  custody, and rejection of links in capture/source boundaries. P0.1 does not add production
  authority or constitute cut-over.
- **P1 — exact legacy evidence capture and reconciliation.** Freeze consistent read-only copies of
  the old Post Office database and required project-register/archive evidence, verify custody and
  hashes, prove a stable observation boundary across both databases and inventoried files, produce
  deterministic snapshots, and classify discrepancies without applying repairs.
  The tooling exists; P1 remains open until the actual register/archive evidence set is captured.
- **P2 — isolated vNext durable substrate and deterministic migration rehearsal.** Build the new
  transactional schema, append-only journal, local content-addressed retention, backup/restore,
  deterministic legacy importer, and replay verifier in non-authoritative isolated databases. The
  schema and backup foundation has started; importer, replay, restore, and full workflows remain.

Later gates implement the complete dispatcher/workflows, perform non-authoritative shadow
validation, rehearse the final import, and finally execute one separately authorised production
switchover. None of P0 through P2 authorises dual-write or production cut-over.
