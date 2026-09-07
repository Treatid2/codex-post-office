---
name: post-office-next
description: "Inspect and exercise the isolated Post Office Next development preview: validate contracts, capture legacy state read-only, build reconciliation evidence, and initialize or inspect an isolated vNext database. Do not use it for production mail delivery, browser control, automatic review, or cutover."
---

# Post Office Next

Use this skill only for the isolated control-plane preview in this plugin. The current plugin
implements contracts, read-only legacy capture, snapshots, reconciliation previews, performance
baselines, and the initial vNext database foundation. It does not implement production routing,
mailbox mutation, reviewer orchestration, migration execution, or switchover.

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
Use `database initialize`, `database inspect`, and `database backup` only against an isolated vNext
database.

Every non-help operation returns one versioned JSON result or diagnostic. Preserve that output as
the operation receipt. A successful preview is evidence, not authority to apply the proposed
change.

Read `docs/delivery-phases.md` before describing P0, P0.1, P1, or P2. Those names are delivery gates,
not defect priorities or runtime states.
