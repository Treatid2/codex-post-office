# P3.4 semantic workflow kernel

P3.4 makes the local semantic record authoritative inside an isolated or shadow kernel. It does not
perform browser or native-task delivery.

Implemented records include software packages and accepted versions, versioned interfaces,
capability requests, change sets, context bundles, integration candidates, author-owned semantic
cycles, semantic messages, message plans, bundles, acknowledgements, decisions and closures.

Message planning is a durable mutation. Its canonical plan binds the complete message and bundle;
registration must reproduce that message exactly and name the planned bundle. Registration fails
unless the bundle SHA-256 already has a verified `LOCAL_CAS` or `DATABASE_BLOB` storage copy. It
then creates the message, bundle and payload rows in one transaction. Routing records a pending
transport attempt and advances the message only to `CUSTODY_RECORDED`; P3.4 never claims external
delivery. A planned, unregistered bundle may be superseded without mutating a registered message.

Package versions and integration package roots likewise require verified local custody. Interfaces
are versioned and bind their steward, providers and consumers to registered packages. Capability,
change-set and task relations are checked to remain within one project. Context construction rejects
secret or credential references.

Cycle acceptance and closure remain distinct exact-author actions. Review output is evidence, not
authority, and cannot itself accept a cycle.

All operations use authenticated capabilities, one semantic authority basis, canonical
idempotency, compare-and-swap aggregate positions, and the chained event journal. The supported
kernel modes remain `ISOLATED` and non-authoritative `SHADOW`.
