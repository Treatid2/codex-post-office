---
name: post-office-next
description: "Operate Post Office Next evidence capture, deterministic migration, authenticated P3 workflows, production preparation, shadow validation, and an explicitly authorised one-time cutover. Do not use it for raw browser control or to infer authority to send, accept, close, or cut over."
---

# Post Office Next

Use this skill for the Post Office Next local control plane. It implements contracts, read-only
legacy capture, deterministic import/replay/restore, the complete authenticated P3 semantic and
transport runtime, production-root preparation, shadow evidence, and guarded one-time authority
transfer. Browser interaction remains the responsibility of the separate Playwright bridge, and
automatic review remains a separate least-authority companion.

## Safety boundary

- Invoke operations through `scripts/Invoke-PostOfficeNext.ps1`.
- Keep source state read-only and write every capture, snapshot, report, rehearsal, and prepared
  vNext root to a new disjoint path until the exact cutover transaction.
- Never point capture, migration, restore, or preparation at the current Post Office state, its
  parent, or an alias of either.
- `POST_OFFICE_PROTECTED_ROOTS` may add operator-defined protected roots using the platform path
  separator. Do not remove a protected root to make a command succeed.
- Do not treat Google Drive as storage, authority, backup, or migration evidence.
- Do not infer permission to cut over, repair production state, close a cycle, or send mail. A
  cutover requires an explicit author request plus the exact ready dossier named by preflight.

## Supported work

Start by validating the public contract baseline:

```powershell
./scripts/Invoke-PostOfficeNext.ps1 contracts validate
```

Use `source-manifest` and `state-capture` to record immutable evidence from a legacy installation.
Use `snapshot` and `reconcile-preview` to compare that evidence without changing either system.
Use `migration import` only with a frozen capture and its complete baseline/delta payload manifests;
use `migration replay` only against a verified isolated import root. Use `database initialize`,
`database inspect`, `database backup`, and `database restore` only against isolated vNext databases
and new destinations. Read `docs/deterministic-migration-v01.md` before a P2.1 rehearsal.

Use `production prepare` only from a verified immutable import into an absent disjoint root. It
proves the copy before mutation, bootstraps `SHADOW`, and creates locally retained author, courier,
and task-bound credentials without emitting their values. Use `scripts/post_office_task.py` for an
authenticated task's inbox and lifecycle operations. Read `docs/operational-kernel-v01.md`,
`docs/authority-provisioning-kernel-v01.md`, and `docs/shadow-cutover-v01.md` first. `SHADOW` is
non-authoritative and never permits external side effects.

Every non-help operation returns one versioned JSON result or diagnostic. Preserve it as the
operation receipt. A successful rehearsal is evidence, not authority. After authority transfer,
the activation pointer and retained committed transfer must agree before any client treats the
database as production.

Read `docs/delivery-phases.md` before describing P0, P0.1, P1, P2, P2.1, or P3.1 through P3.6. Those names are delivery
gates, not defect priorities or runtime states.
