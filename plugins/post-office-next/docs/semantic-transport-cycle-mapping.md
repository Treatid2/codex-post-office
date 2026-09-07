# Semantic-cycle and transport-cycle mapping

## Mapping rule

Legacy `messages.cycle_id` and `messages.root_cycle_id` are preserved exactly but imported as
`legacyCycleId`, not automatically as a vNext semantic cycle. Each mapping records:

- the legacy cycle ID;
- exact member message IDs and row hashes;
- structured message states;
- external semantic-state assertions and their source hashes;
- classification (`TRANSPORT_ONLY`, `EXACT`, or `AMBIGUOUS`); and
- decision (`PRESERVE` only for an exact semantic-cycle mapping, otherwise `REVIEW_REQUIRED`).

State assertions, including agreeing assertions, never create semantic identity. A mapping receives
`semanticCycleId`, classification `EXACT`, and decision `PRESERVE` only when one verified
`EXACT_AUTHOR_ACTION` assertion explicitly supplies the `semanticCycleId` fact. Transport-only,
assertion-only, unverified, and ambiguous evidence remains unmapped and requires review.

No mapping code can close, accept, reopen, or replace a cycle. The P0/P1 CLI contains no apply path.

## Mandatory discrepancy fixture

`FGPM-CYCLE-000013` is `AMBIGUOUS` and `REVIEW_REQUIRED`.

Exact retained facts in the frozen snapshot:

- structured legacy member: `FGPM-C2C-000014`;
- structured `cycle_id`: `FGPM-CYCLE-000013`;
- structured `root_cycle_id`: `FGPM-CYCLE-000013`;
- transport status: `STALE_GENERATION`;
- authorization class: `REPORT`;
- immutable subject names `FGPM-CYCLE-000012` as the response target;
- schema-12 row evidence hash:
  `1a02a3c69cce78eb71e9f4823bd0fa4d08533297e465ef35e5d2a1ab0ff2043c`;
- supplied browser guidance, SHA-256
  `96c32bbd35e10fbc54efa69920052f3b8d329f49c05cb638aca102dd00b07152`, says cycle 13 is
  `CLOSED`;
- supplied evaluation, SHA-256
  `2a39c0ba4f4ce09ee08796d6e1e2f1bf589f5c5be5f7a9b31f262e3b100d0829`, reports retained
  assertions that it is `OPEN`.

Neither supplied document is an exact author-action receipt. The preview therefore proposes no
authoritative state, performs no repair, and marks both the state conflict and the structured/label
cycle mismatch for exact-evidence review.
