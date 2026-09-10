# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .benchmark import performance_baseline
from .contracts import generate_contracts, validate_contracts
from .database import backup_database, initialize_database, inspect_database, restore_database
from .diagnostics import PostOfficeError
from .legacy import capture_state, source_manifest
from .kernel import (
    bootstrap_kernel,
    create_kernel_credential,
    execute_operation,
    inspect_kernel,
)
from .migration import import_legacy_capture, replay_migration
from .reconciliation import reconcile_preview
from .snapshot import create_snapshot


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise PostOfficeError(
            "PON_INPUT_INVALID",
            "Command-line arguments are invalid",
            {"parserMessage": message},
        )


def _parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(description="Post Office Next isolated control-plane preview")
    sub = parser.add_subparsers(dest="command", required=True)
    contracts = sub.add_parser("contracts")
    contracts.add_argument("action", choices=["generate", "validate"])
    source = sub.add_parser("source-manifest")
    source.add_argument("--source-root", required=True)
    source.add_argument("--snapshot-archive", required=True)
    source.add_argument("--output", required=True)
    capture = sub.add_parser("state-capture")
    capture.add_argument("--source-state-root", required=True)
    capture.add_argument("--capture-root", required=True)
    capture.add_argument("--external-evidence-manifest")
    snapshot = sub.add_parser("snapshot")
    snapshot.add_argument("--capture-root", required=True)
    snapshot.add_argument("--output", required=True)
    reconcile = sub.add_parser("reconcile-preview")
    reconcile.add_argument("--snapshot", required=True)
    reconcile.add_argument("--assertions")
    reconcile.add_argument("--output", required=True)
    benchmark = sub.add_parser("performance-baseline")
    benchmark.add_argument("--output", required=True)
    benchmark.add_argument("--iterations", type=int, default=5)
    benchmark.add_argument("--capture-root")
    database = sub.add_parser("database")
    database.add_argument("action", choices=["initialize", "inspect", "backup", "restore"])
    database.add_argument("--path", required=True)
    database.add_argument("--destination")
    database.add_argument("--receipt")
    database.add_argument("--backup-receipt")
    migration = sub.add_parser("migration")
    migration.add_argument("action", choices=["import", "replay"])
    migration.add_argument("--capture-root")
    migration.add_argument("--baseline-payload-manifest")
    migration.add_argument("--payload-delta-manifest")
    migration.add_argument("--source-root")
    migration.add_argument("--output-root", required=True)
    kernel = sub.add_parser("kernel")
    kernel.add_argument("action", choices=["credential-create", "bootstrap", "inspect", "execute"])
    kernel.add_argument("--path")
    kernel.add_argument("--credential")
    kernel.add_argument("--output")
    kernel.add_argument("--capability-id")
    kernel.add_argument("--actor-id")
    kernel.add_argument("--actor-kind", choices=["HUMAN", "BROWSER", "COURIER", "ENDPOINT", "SYSTEM"])
    kernel.add_argument("--actor-role")
    kernel.add_argument("--mode", choices=["ISOLATED", "SHADOW"], default="ISOLATED")
    kernel.add_argument("--request")
    return parser


def dispatch(args: argparse.Namespace, plugin_root: Path) -> dict[str, Any]:
    if args.command == "contracts":
        return generate_contracts(plugin_root) if args.action == "generate" else validate_contracts(plugin_root)
    if args.command == "source-manifest":
        return source_manifest(Path(args.source_root), Path(args.snapshot_archive), Path(args.output))
    if args.command == "state-capture":
        return capture_state(
            Path(args.source_state_root),
            Path(args.capture_root),
            Path(args.external_evidence_manifest) if args.external_evidence_manifest else None,
        )
    if args.command == "snapshot":
        return create_snapshot(Path(args.capture_root), Path(args.output))
    if args.command == "reconcile-preview":
        return reconcile_preview(Path(args.snapshot), Path(args.assertions) if args.assertions else None, Path(args.output))
    if args.command == "performance-baseline":
        return performance_baseline(
            plugin_root,
            Path(args.output),
            args.iterations,
            Path(args.capture_root) if args.capture_root else None,
        )
    if args.command == "database":
        if args.action == "initialize":
            return initialize_database(plugin_root, Path(args.path))
        if args.action == "inspect":
            return {"ok": True, **inspect_database(Path(args.path), plugin_root)}
        if args.action == "backup":
            if not args.destination or not args.receipt:
                raise PostOfficeError("PON_INPUT_INVALID", "database backup requires --destination and --receipt")
            return backup_database(Path(args.path), Path(args.destination), Path(args.receipt), plugin_root)
        if not args.destination or not args.receipt or not args.backup_receipt:
            raise PostOfficeError(
                "PON_INPUT_INVALID",
                "database restore requires --path, --backup-receipt, --destination and --receipt",
            )
        return restore_database(
            Path(args.path),
            Path(args.backup_receipt),
            Path(args.destination),
            Path(args.receipt),
            plugin_root,
        )
    if args.command == "migration":
        if args.action == "import":
            if not args.capture_root or not args.baseline_payload_manifest or not args.payload_delta_manifest:
                raise PostOfficeError(
                    "PON_INPUT_INVALID",
                    "migration import requires --capture-root, --baseline-payload-manifest and --payload-delta-manifest",
                )
            return import_legacy_capture(
                Path(args.capture_root),
                Path(args.baseline_payload_manifest),
                Path(args.payload_delta_manifest),
                Path(args.output_root),
                plugin_root,
            )
        if not args.source_root:
            raise PostOfficeError("PON_INPUT_INVALID", "migration replay requires --source-root")
        return replay_migration(Path(args.source_root), Path(args.output_root), plugin_root)
    if args.command == "kernel":
        if args.action == "credential-create":
            if not args.output or not args.capability_id:
                raise PostOfficeError(
                    "PON_INPUT_INVALID",
                    "kernel credential-create requires --output and --capability-id",
                )
            return create_kernel_credential(Path(args.output), args.capability_id)
        if not args.path:
            raise PostOfficeError("PON_INPUT_INVALID", f"kernel {args.action} requires --path")
        if args.action == "inspect":
            return inspect_kernel(Path(args.path), plugin_root)
        if args.action == "bootstrap":
            if not all((args.credential, args.actor_id, args.actor_kind, args.actor_role)):
                raise PostOfficeError(
                    "PON_INPUT_INVALID",
                    "kernel bootstrap requires --credential, --actor-id, --actor-kind and --actor-role",
                )
            return bootstrap_kernel(
                Path(args.path), Path(args.credential), actor_id=args.actor_id,
                actor_kind=args.actor_kind, actor_role=args.actor_role,
                mode=args.mode, plugin_root=plugin_root,
            )
        if not args.request or not args.credential:
            raise PostOfficeError(
                "PON_INPUT_INVALID", "kernel execute requires --request and --credential"
            )
        return execute_operation(
            Path(args.path), Path(args.request), Path(args.credential), plugin_root
        )
    raise PostOfficeError("PON_INPUT_INVALID", "Unsupported command")


def main(argv: list[str] | None = None) -> int:
    try:
        parser = _parser()
        args = parser.parse_args(argv)
        plugin_root = Path(__file__).resolve().parents[2]
        result = dispatch(args, plugin_root)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except PostOfficeError as exc:
        print(json.dumps(exc.as_result(), ensure_ascii=False, sort_keys=True))
        return 2
    except Exception as exc:  # fail closed at the command boundary
        error = PostOfficeError("PON_INTERNAL_ERROR", str(exc), {"exceptionType": type(exc).__name__})
        print(json.dumps(error.as_result(), ensure_ascii=False, sort_keys=True))
        return 3


if __name__ == "__main__":
    sys.exit(main())
