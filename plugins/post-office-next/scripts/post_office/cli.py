# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .benchmark import performance_baseline
from .contracts import generate_contracts, validate_contracts
from .database import backup_database, initialize_database, inspect_database, reattest_migration_history, restore_database
from .diagnostics import PostOfficeError
from .legacy import capture_state, source_manifest
from .kernel import (
    bind_kernel_credential,
    bootstrap_kernel,
    create_kernel_credential,
    create_kernel_actor,
    execute_operation,
    inspect_kernel,
)
from .runtime import (
    claim_next_review,
    claim_next_continuation,
    claim_next_transport,
    complete_automatic_review,
    complete_continuation,
    complete_transport,
    ensure_automatic_review,
    ingest_collected_browser_return,
    ingest_recovered_browser_return,
    ingest_automatic_review_result,
    issue_browser_return_collection_manifest,
    issue_transport_delivery_manifest,
    reconcile_transport,
    record_recovered_transport_receipt,
    reconcile_continuations,
    retire_continuation,
    return_automatic_review,
    status_automatic_review,
    withdraw_automatic_review,
)
from .migration import import_legacy_capture, replay_migration
from .production import prepare_production_root, production_status
from .payloads import capture_payload_delta
from .reconciliation import reconcile_preview
from .snapshot import create_snapshot
from .shadow import (
    create_cutover_dossier,
    cutover_preflight,
    decide_shadow_observation,
    execute_cutover,
    finish_prepared_cutover,
    record_cutover_rehearsal,
    record_shadow_observation,
    rollback_pre_authority,
)


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
    payload_delta = sub.add_parser("payload-delta")
    payload_delta.add_argument("--capture-root", required=True)
    payload_delta.add_argument("--baseline-payload-manifest", required=True)
    payload_delta.add_argument("--source-state-root", required=True)
    payload_delta.add_argument("--output-root", required=True)
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
    database.add_argument("action", choices=["initialize", "inspect", "backup", "restore", "reattest-migration-history"])
    database.add_argument("--path", required=True)
    database.add_argument("--destination")
    database.add_argument("--receipt")
    database.add_argument("--backup-receipt")
    database.add_argument("--credential")
    database.add_argument("--exact-author-action-id")
    migration = sub.add_parser("migration")
    migration.add_argument("action", choices=["import", "replay"])
    migration.add_argument("--capture-root")
    migration.add_argument("--baseline-payload-manifest")
    migration.add_argument("--payload-delta-manifest")
    migration.add_argument("--source-root")
    migration.add_argument("--output-root", required=True)
    production = sub.add_parser("production")
    production.add_argument("action", choices=["prepare", "status"])
    production.add_argument("--path")
    production.add_argument("--source-root")
    production.add_argument("--output-root")
    production.add_argument("--receipt")
    kernel = sub.add_parser("kernel")
    kernel.add_argument("action", choices=["credential-create", "credential-bind", "actor-create", "bootstrap", "inspect", "execute"])
    kernel.add_argument("--path")
    kernel.add_argument("--credential")
    kernel.add_argument("--output")
    kernel.add_argument("--capability-id")
    kernel.add_argument("--actor-id")
    kernel.add_argument("--actor-kind", choices=["HUMAN", "BROWSER", "COURIER", "ENDPOINT", "SYSTEM"])
    kernel.add_argument("--actor-role")
    kernel.add_argument("--mode", choices=["ISOLATED", "SHADOW"], default="ISOLATED")
    kernel.add_argument("--request")
    kernel.add_argument("--author-action-id")
    kernel.add_argument("--subject-kind")
    kernel.add_argument("--subject-id")
    kernel.add_argument("--subject-generation", type=int)
    kernel.add_argument("--allow-operation", action="append", dest="allowed_operations")
    kernel.add_argument("--expires-at")
    runtime = sub.add_parser("runtime")
    runtime.add_argument(
        "action",
        choices=[
            "reconcile", "claim", "complete", "record-recovered", "ingest-browser-return",
            "issue-browser-return-collection", "ingest-collected-browser-return",
            "issue-delivery-manifest",
        ],
    )
    runtime.add_argument("--path", required=True)
    runtime.add_argument("--credential", required=True)
    runtime.add_argument("--observations")
    runtime.add_argument("--lease-seconds", type=int, default=600)
    runtime.add_argument("--dispatch-id")
    runtime.add_argument("--lease-token")
    runtime.add_argument("--observable-marker")
    runtime.add_argument("--observed-receipt-id")
    runtime.add_argument("--message-id")
    runtime.add_argument("--bundle-id")
    runtime.add_argument("--channel", choices=["NATIVE_TASK", "PLAYWRIGHT_BROWSER"])
    runtime.add_argument("--source-message-id")
    runtime.add_argument("--result-path")
    runtime.add_argument("--expected-sha256")
    runtime.add_argument("--expected-size-bytes", type=int)
    runtime.add_argument("--source-thread-id")
    runtime.add_argument("--source-turn-id")
    runtime.add_argument("--destination-thread-id")
    runtime.add_argument("--destination-turn-id")
    runtime.add_argument("--attachment-reference")
    runtime.add_argument("--attachment-name")
    runtime.add_argument("--observed-at")
    runtime.add_argument("--required-text", action="append", default=[])
    runtime.add_argument("--collection-manifest")
    runtime.add_argument("--collection-receipt")
    reviews = sub.add_parser("reviews")
    reviews.add_argument("action", choices=["ensure", "claim", "ingest-result", "return", "status", "complete", "withdraw"])
    reviews.add_argument("--path", required=True)
    reviews.add_argument("--credential", required=True)
    reviews.add_argument("--review-id")
    reviews.add_argument("--semantic-message-id")
    reviews.add_argument("--requester-task-id")
    reviews.add_argument("--reviewer-endpoint-id")
    reviews.add_argument("--package-sha256")
    reviews.add_argument("--minimum-interval-minutes", type=int, default=30)
    reviews.add_argument("--result-message-id")
    reviews.add_argument("--result-path")
    reviews.add_argument("--source-thread-id")
    reviews.add_argument("--source-message-id")
    reviews.add_argument("--activation-dispatch-id")
    reviews.add_argument("--verdict")
    reviews.add_argument("--reason")
    reviews.add_argument("--summary")
    continuation = sub.add_parser("continuation")
    continuation.add_argument("action", choices=["reconcile", "claim", "complete", "retire"])
    continuation.add_argument("--path", required=True)
    continuation.add_argument("--credential", required=True)
    continuation.add_argument("--kind")
    continuation.add_argument("--lease-seconds", type=int, default=600)
    continuation.add_argument("--continuation-id")
    continuation.add_argument("--lease-token")
    continuation.add_argument("--outcome")
    continuation.add_argument("--observations")
    shadow = sub.add_parser("shadow")
    shadow.add_argument("action", choices=["observe", "decide", "record-rehearsal", "dossier"])
    shadow.add_argument("--path", required=True)
    shadow.add_argument("--credential", required=True)
    shadow.add_argument("--source-kind")
    shadow.add_argument("--source-reference")
    shadow.add_argument("--expected-root")
    shadow.add_argument("--observed-root")
    shadow.add_argument("--evidence")
    shadow.add_argument("--observation-id")
    shadow.add_argument("--decision", choices=["RESOLVE", "ACCEPT_EXCEPTION"])
    shadow.add_argument("--author-action-id")
    shadow.add_argument("--reason")
    shadow.add_argument("--legacy-final-root")
    shadow.add_argument("--output")
    cutover = sub.add_parser("cutover")
    cutover.add_argument("action", choices=["plan", "preflight", "activate", "finish-prepared", "rollback-pre-authority"])
    cutover.add_argument("--path", required=True)
    cutover.add_argument("--credential", required=True)
    cutover.add_argument("--dossier-id")
    cutover.add_argument("--dossier-root")
    cutover.add_argument("--legacy-final-root")
    cutover.add_argument("--output")
    cutover.add_argument("--legacy-state-root")
    cutover.add_argument("--pointer-output")
    cutover.add_argument("--transfer-id")
    cutover.add_argument("--reason")
    cutover.add_argument("--author-action-id")
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
    if args.command == "payload-delta":
        return capture_payload_delta(
            Path(args.capture_root), Path(args.baseline_payload_manifest),
            Path(args.source_state_root), Path(args.output_root)
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
        if args.action == "reattest-migration-history":
            if not args.destination or not args.receipt or not args.credential or not args.exact_author_action_id:
                raise PostOfficeError(
                    "PON_INPUT_INVALID",
                    "database reattest-migration-history requires --destination, --receipt, --credential and --exact-author-action-id",
                )
            return reattest_migration_history(
                Path(args.path),
                Path(args.destination),
                Path(args.receipt),
                Path(args.credential),
                args.exact_author_action_id,
                plugin_root,
            )
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
    if args.command == "production":
        if args.action == "status":
            if not args.path:
                raise PostOfficeError("PON_INPUT_INVALID", "production status requires --path", {})
            return production_status(Path(args.path), plugin_root)
        if not all((args.source_root, args.output_root, args.receipt)):
            raise PostOfficeError(
                "PON_INPUT_INVALID",
                "production prepare requires --source-root, --output-root and --receipt",
                {},
            )
        return prepare_production_root(
            Path(args.source_root), Path(args.output_root), Path(args.receipt), plugin_root
        )
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
        if args.action == "actor-create":
            if not all((args.credential, args.author_action_id, args.actor_id, args.actor_kind, args.actor_role)):
                raise PostOfficeError(
                    "PON_INPUT_INVALID",
                    "kernel actor-create requires --credential, --author-action-id, --actor-id, --actor-kind and --actor-role",
                )
            return create_kernel_actor(
                Path(args.path), Path(args.credential), action_id=args.author_action_id,
                actor_id=args.actor_id, actor_kind=args.actor_kind, actor_role=args.actor_role,
                plugin_root=plugin_root,
            )
        if args.action == "credential-bind":
            if not all((args.credential, args.output, args.author_action_id, args.actor_id,
                        args.capability_id, args.subject_kind, args.subject_id, args.allowed_operations)):
                raise PostOfficeError(
                    "PON_INPUT_INVALID",
                    "kernel credential-bind requires --credential, --output, --author-action-id, --actor-id, --capability-id, --subject-kind, --subject-id and at least one --allow-operation",
                )
            return bind_kernel_credential(
                Path(args.path), Path(args.credential), Path(args.output),
                action_id=args.author_action_id, actor_id=args.actor_id,
                capability_id=args.capability_id, subject_kind=args.subject_kind,
                subject_id=args.subject_id, subject_generation=args.subject_generation,
                allowed_operations=args.allowed_operations, expires_at=args.expires_at,
                plugin_root=plugin_root,
            )
        if not args.request or not args.credential:
            raise PostOfficeError(
                "PON_INPUT_INVALID", "kernel execute requires --request and --credential"
            )
        return execute_operation(
            Path(args.path), Path(args.request), Path(args.credential), plugin_root
        )
    if args.command == "runtime":
        if args.action == "reconcile":
            observations = None
            if args.observations:
                observations = json.loads(Path(args.observations).read_text(encoding="utf-8"))
            return reconcile_transport(Path(args.path), Path(args.credential), plugin_root, observations=observations)
        if args.action == "claim":
            return claim_next_transport(
                Path(args.path), Path(args.credential), plugin_root,
                lease_seconds=args.lease_seconds, dispatch_id=args.dispatch_id,
            )
        if args.action == "issue-browser-return-collection":
            if not all((args.source_message_id, args.source_thread_id, args.source_turn_id,
                        args.attachment_reference, args.attachment_name, args.expected_sha256,
                        args.expected_size_bytes, args.observed_at)):
                raise PostOfficeError(
                    "PON_INPUT_INVALID",
                    "runtime issue-browser-return-collection requires source message/thread/turn, "
                    "attachment reference/name, expected identity and observed time",
                )
            return issue_browser_return_collection_manifest(
                Path(args.path), Path(args.credential), plugin_root,
                source_message_id=args.source_message_id, source_thread_id=args.source_thread_id,
                source_turn_id=args.source_turn_id, attachment_reference=args.attachment_reference,
                attachment_name=args.attachment_name, expected_sha256=args.expected_sha256,
                expected_size_bytes=args.expected_size_bytes, observed_at=args.observed_at,
                required_text=args.required_text,
            )
        if args.action == "ingest-collected-browser-return":
            if not all((args.collection_manifest, args.result_path, args.collection_receipt)):
                raise PostOfficeError(
                    "PON_INPUT_INVALID",
                    "runtime ingest-collected-browser-return requires collection manifest, result path and receipt",
                )
            return ingest_collected_browser_return(
                Path(args.path), Path(args.credential), plugin_root,
                collection_manifest_path=Path(args.collection_manifest), result_path=Path(args.result_path),
                collection_receipt=args.collection_receipt,
            )
        if args.action == "issue-delivery-manifest":
            if not all((args.dispatch_id, args.lease_token)):
                raise PostOfficeError(
                    "PON_INPUT_INVALID",
                    "runtime issue-delivery-manifest requires dispatch ID and lease token",
                )
            return issue_transport_delivery_manifest(
                Path(args.path), Path(args.credential), plugin_root,
                dispatch_id=args.dispatch_id, lease_token=args.lease_token,
            )
        if args.action == "record-recovered":
            if not all((args.message_id, args.bundle_id, args.channel, args.observable_marker, args.observed_receipt_id)):
                raise PostOfficeError(
                    "PON_INPUT_INVALID",
                    "runtime record-recovered requires --message-id, --bundle-id, --channel, --observable-marker and --observed-receipt-id",
                )
            return record_recovered_transport_receipt(
                Path(args.path),
                Path(args.credential),
                plugin_root,
                message_id=args.message_id,
                bundle_id=args.bundle_id,
                channel=args.channel,
                observable_marker=args.observable_marker,
                observed_receipt_id=args.observed_receipt_id,
            )
        if args.action == "ingest-browser-return":
            if not all((args.source_message_id, args.result_path, args.expected_sha256,
                        args.expected_size_bytes, args.source_thread_id, args.source_turn_id,
                        args.destination_thread_id, args.destination_turn_id)):
                raise PostOfficeError(
                    "PON_INPUT_INVALID",
                    "runtime ingest-browser-return requires --source-message-id, --result-path, "
                    "--expected-sha256, --expected-size-bytes, --source-thread-id, --source-turn-id, "
                    "--destination-thread-id and --destination-turn-id",
                )
            return ingest_recovered_browser_return(
                Path(args.path), Path(args.credential), plugin_root,
                source_message_id=args.source_message_id, result_path=Path(args.result_path),
                expected_sha256=args.expected_sha256, expected_size_bytes=args.expected_size_bytes,
                source_thread_id=args.source_thread_id, source_turn_id=args.source_turn_id,
                destination_thread_id=args.destination_thread_id,
                destination_turn_id=args.destination_turn_id,
            )
        if not all((args.dispatch_id, args.lease_token, args.observable_marker, args.observed_receipt_id)):
            raise PostOfficeError(
                "PON_INPUT_INVALID",
                "runtime complete requires --dispatch-id, --lease-token, --observable-marker and --observed-receipt-id",
            )
        return complete_transport(
            Path(args.path), Path(args.credential), plugin_root,
            dispatch_id=args.dispatch_id, lease_token=args.lease_token,
            observable_marker=args.observable_marker, observed_receipt_id=args.observed_receipt_id,
        )
    if args.command == "reviews":
        if args.action == "claim":
            return claim_next_review(
                Path(args.path), Path(args.credential), plugin_root,
                reviewer_endpoint_id=args.reviewer_endpoint_id,
            )
        if args.action == "ingest-result":
            if not all((args.review_id, args.result_path, args.source_thread_id, args.source_message_id,
                        args.activation_dispatch_id, args.verdict)):
                raise PostOfficeError(
                    "PON_INPUT_INVALID",
                    "reviews ingest-result requires --review-id, --result-path, --source-thread-id, "
                    "--source-message-id, --activation-dispatch-id and --verdict",
                )
            return ingest_automatic_review_result(
                Path(args.path), Path(args.credential), plugin_root,
                review_id=args.review_id, result_path=Path(args.result_path),
                source_thread_id=args.source_thread_id, source_message_id=args.source_message_id,
                activation_dispatch_id=args.activation_dispatch_id, verdict=args.verdict,
            )
        if not args.review_id:
            raise PostOfficeError("PON_INPUT_INVALID", f"reviews {args.action} requires --review-id")
        if args.action == "ensure":
            if not all((args.semantic_message_id, args.requester_task_id, args.reviewer_endpoint_id, args.package_sha256)):
                raise PostOfficeError(
                    "PON_INPUT_INVALID",
                    "reviews ensure requires --semantic-message-id, --requester-task-id, --reviewer-endpoint-id and --package-sha256",
                )
            return ensure_automatic_review(
                Path(args.path), Path(args.credential), plugin_root,
                review_id=args.review_id, semantic_message_id=args.semantic_message_id,
                requester_task_id=args.requester_task_id, reviewer_endpoint_id=args.reviewer_endpoint_id,
                package_sha256=args.package_sha256,
                minimum_interval_minutes=args.minimum_interval_minutes,
            )
        if args.action == "return":
            if not args.result_message_id:
                raise PostOfficeError("PON_INPUT_INVALID", "reviews return requires --result-message-id")
            return return_automatic_review(
                Path(args.path), Path(args.credential), plugin_root,
                review_id=args.review_id, result_message_id=args.result_message_id,
            )
        if args.action == "status":
            return status_automatic_review(
                Path(args.path), Path(args.credential), plugin_root, review_id=args.review_id,
            )
        if args.action == "complete":
            if not args.summary:
                raise PostOfficeError("PON_INPUT_INVALID", "reviews complete requires --summary")
            return complete_automatic_review(
                Path(args.path), Path(args.credential), plugin_root,
                review_id=args.review_id, summary=args.summary,
            )
        if not args.reason:
            raise PostOfficeError("PON_INPUT_INVALID", "reviews withdraw requires --reason")
        return withdraw_automatic_review(
            Path(args.path), Path(args.credential), plugin_root,
            review_id=args.review_id, reason=args.reason,
        )
    if args.command == "continuation":
        if args.action == "reconcile":
            observations = None
            if args.observations:
                observations = json.loads(Path(args.observations).read_text(encoding="utf-8"))
            return reconcile_continuations(
                Path(args.path), Path(args.credential), plugin_root, observations=observations
            )
        if args.action == "claim":
            return claim_next_continuation(
                Path(args.path), Path(args.credential), plugin_root,
                continuation_kind=args.kind, lease_seconds=args.lease_seconds,
            )
        if not all((args.continuation_id, args.lease_token, args.outcome)):
            raise PostOfficeError(
                "PON_INPUT_INVALID",
                f"continuation {args.action} requires --continuation-id, --lease-token and --outcome",
            )
        outcome = json.loads(Path(args.outcome).read_text(encoding="utf-8"))
        if args.action == "retire":
            return retire_continuation(
                Path(args.path), Path(args.credential), plugin_root,
                continuation_id=args.continuation_id, lease_token=args.lease_token,
                outcome=outcome,
            )
        return complete_continuation(
            Path(args.path), Path(args.credential), plugin_root,
            continuation_id=args.continuation_id, lease_token=args.lease_token, outcome=outcome,
        )
    if args.command == "shadow":
        if args.action == "observe":
            if not all((args.source_kind, args.source_reference, args.evidence)):
                raise PostOfficeError("PON_INPUT_INVALID", "shadow observe requires --source-kind, --source-reference and --evidence")
            evidence = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
            return record_shadow_observation(
                Path(args.path), Path(args.credential), plugin_root,
                source_kind=args.source_kind, source_reference=args.source_reference,
                expected_root=args.expected_root, observed_root=args.observed_root, evidence=evidence,
            )
        if args.action == "decide":
            if not all((args.observation_id, args.decision, args.author_action_id, args.reason)):
                raise PostOfficeError("PON_INPUT_INVALID", "shadow decide requires --observation-id, --decision, --author-action-id and --reason")
            return decide_shadow_observation(
                Path(args.path), Path(args.credential), plugin_root,
                observation_id=args.observation_id, decision=args.decision,
                action_id=args.author_action_id, rationale=args.reason,
            )
        if args.action == "record-rehearsal":
            if not args.evidence:
                raise PostOfficeError("PON_INPUT_INVALID", "shadow record-rehearsal requires --evidence")
            evidence = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
            return record_cutover_rehearsal(
                Path(args.path), Path(args.credential), plugin_root, evidence=evidence,
            )
        if not args.legacy_final_root or not args.output:
            raise PostOfficeError("PON_INPUT_INVALID", "shadow dossier requires --legacy-final-root and --output")
        return create_cutover_dossier(
            Path(args.path), Path(args.credential), plugin_root,
            legacy_final_root=args.legacy_final_root, output=Path(args.output),
        )
    if args.command == "cutover":
        if args.action == "plan":
            if not args.legacy_final_root or not args.output:
                raise PostOfficeError("PON_INPUT_INVALID", "cutover plan requires --legacy-final-root and --output")
            return create_cutover_dossier(
                Path(args.path), Path(args.credential), plugin_root,
                legacy_final_root=args.legacy_final_root, output=Path(args.output),
            )
        if args.action == "preflight":
            if not args.dossier_id or not args.dossier_root:
                raise PostOfficeError("PON_INPUT_INVALID", "cutover preflight requires --dossier-id and --dossier-root")
            return cutover_preflight(
                Path(args.path), Path(args.credential), plugin_root,
                dossier_id=args.dossier_id, dossier_root=args.dossier_root,
            )
        if args.action == "finish-prepared":
            if not args.transfer_id:
                raise PostOfficeError("PON_INPUT_INVALID", "cutover finish-prepared requires --transfer-id")
            return finish_prepared_cutover(
                Path(args.path), Path(args.credential), plugin_root, transfer_id=args.transfer_id,
            )
        if args.action == "rollback-pre-authority":
            if not args.transfer_id or not args.author_action_id or not args.reason:
                raise PostOfficeError("PON_INPUT_INVALID", "cutover rollback-pre-authority requires --transfer-id, --author-action-id and --reason")
            return rollback_pre_authority(
                Path(args.path), Path(args.credential), plugin_root,
                transfer_id=args.transfer_id, action_id=args.author_action_id, reason=args.reason,
            )
        if not all((args.dossier_id, args.dossier_root, args.legacy_state_root, args.pointer_output, args.author_action_id)):
            raise PostOfficeError("PON_INPUT_INVALID", "cutover activate requires --dossier-id, --dossier-root, --legacy-state-root, --pointer-output and --author-action-id")
        return execute_cutover(
            Path(args.path), Path(args.credential), plugin_root,
            dossier_id=args.dossier_id, dossier_root=args.dossier_root,
            legacy_state_root=Path(args.legacy_state_root), pointer_output=Path(args.pointer_output),
            action_id=args.author_action_id,
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
