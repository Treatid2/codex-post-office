---
name: post-office-next
description: "Inspect and exercise the isolated Post Office Next development preview: validate contracts, capture legacy state read-only, build reconciliation evidence, initialize an isolated vNext database, perform deterministic import/replay/restore rehearsals, or exercise the P3.1 operational kernel. Do not use it for production mail delivery, browser control, automatic review, or cutover."
---

# Post Office Next

Use this skill only for the isolated control-plane preview in this plugin. The current plugin
implements contracts, read-only legacy capture, snapshots, reconciliation previews, performance
baselines, the vNext database foundation, isolated deterministic import/replay/restore, and the P3.1
authenticated operational kernel. It does not implement production routing, mailbox mutation,
reviewer orchestration, production migration, or switchover.

## Safety boundary

- Invoke operations through `scripts/Invoke-PostOfficeNext.ps1`.
- Keep source state read-only and write every capture, snapshot, report, and vNext database to a
  new disjoint path.
- Never point a write command at the current Post Office state, its parent, or an alias of either.
- `POST_OFFICE_PROTECTED_ROOTS` may add operator-defined protected roots using the platform path
  separator. Do not remove a protected root to make a command succeed.
- Do not treat Google Drive as storage, authority, backup, or migration evidence.
- Do not infer permission to cut over, repair production state, close a cycle, or send mail.

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

Use `kernel credential-create`, `kernel bootstrap`, `kernel inspect`, and `kernel execute` only with
an exact isolated vNext database and a credential path outside source control. P3.1 implements only
the `hub.status` operation; every catalogue mutation remains default-denied. Read
`docs/operational-kernel-v01.md` before exercising it. `SHADOW` is non-authoritative and never
permits external side effects.

Every non-help operation returns one versioned JSON result or diagnostic. Preserve that output as
the operation receipt. A successful preview is evidence, not authority to apply the proposed
change.

Read `docs/delivery-phases.md` before describing P0, P0.1, P1, P2, P2.1, or P3.1. Those names are delivery
gates, not defect priorities or runtime states.
