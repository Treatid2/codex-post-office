# Migration authority matrix

“Post-cut-over” below is a target rule, not a current authority claim. No cut-over occurred in P0/P1.

| Fact class | Pre-cut-over authority | Evidence imported in P1 | Post-cut-over target | Conflict rule |
|---|---|---|---|---|
| Native message transport state | Schema-12 Hub rows plus hash-chained Hub events | Message status, generation, event headers and row hashes | vNext transport events/index | Preserve current IDs; never create a semantic message to repair transport |
| Semantic message meaning | Exact immutable message fields and linked author/browser evidence | IDs, type, parties, related/root cycle, subject, hashes of prose | `SemanticMessage` events | Prose or delivery cannot create authority |
| Message and payload bytes | Existing CAS plus verified delivery/custody receipts | Paths, sizes, SHA-256, file inventory | `MessageBundle` plus `StorageCopy` events | Hash disagreement is quarantine/review only |
| Transport attempts | Outbox, delivery, wake, courier and custody rows | Outboxes, Drive receipts, poke/wake records | `TransportAttempt` events | Retry keeps semantic message and semantic cycle unchanged |
| Mailbox generation | Schema-12 mailbox row and exact-generation message address | Full mailbox inventory | vNext mailbox events with compatibility projection | Never guess or renumber an occupied generation |
| Endpoint identity | Legacy mailbox/thread binding; no separate schema-12 endpoint aggregate | Explicit legacy mailbox-bound actor projection | vNext `Endpoint` aggregate | Do not fabricate an endpoint ID during import |
| Caller authentication capability | Schema-12 capability record and external bearer token | Capability metadata only; no bearer token or stored secret hash | Existing authentication layer or compatible projection | Never place secret material in events or snapshots |
| Semantic authority grant | Exact author action and historically attributed browser decision | No native `AuthorityGrant`; gap is explicit | vNext `AuthorityGrant` events | Default deny; transport facts never mint a grant |
| Browser project decisions | Exact browser/author evidence and current project register until reconciled | Binding, message, receipt, and supplied assertion references | Attributed vNext decision events | Ambiguity requires author review; no automatic repair |
| Semantic-cycle state | Exact author decision plus reconciled project evidence | Structured legacy cycle membership; semantic state unresolved | `SemanticCycle` events | Only exact author authority closes; reconciliation cannot reopen |
| Browser-bridge transfer | Existing Drive delivery/custody evidence | File ID, URL, parent, verification level and hash | Transient bridge-transfer event and receipt; durable bytes live in the local CAS | Drive is never retention or authority; successful ingest must not require routine rereads |
| Project identity | Historical project codes/domains and attested project records | Derived project-code inventory labelled non-native | vNext `Project` catalogue | Do not infer a new immutable project ID from display text |
| Task/package/interface | External project records; absent from schema 12 | Empty native inventories and explicit migration gap | vNext catalogues | Import only from attested sources with exact identity |
| Automatic review | Schema-10 automatic-review tables/service | Review inventory and receipt fields | Preserved service or compatible projection | Review result grants report authority only |
| Browser observer | Schema-1 advisory observer | Transactional DB copy, thread watermarks and event summary | Advisory projection only | Observer state never suppresses required authoritative reads |
| Human projections | Current Sheets/Drive/register evidence by fact class | Projection references and file inventory used only for legacy migration evidence | Database-backed views derived from the vNext event journal/index | External projections never become retention or authority; a stale view never overrides an event |

## Enforcement levels

P0/P1 can claim `CONTROL_PLANE_ENFORCED` for contract shape and `AUDITED_WRITE_SET` for its own test
fixtures. It cannot claim `HOST_PATH_ENFORCED`: the current host profile has broad filesystem access.
