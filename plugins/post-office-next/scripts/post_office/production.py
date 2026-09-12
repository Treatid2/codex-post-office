# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from .canonical import require_new_output_file, sha256_file, sha256_json, write_json
from .database import inspect_connection, inspect_database
from .diagnostics import PostOfficeError
from .kernel import (
    bind_kernel_credential,
    bootstrap_kernel,
    create_kernel_actor,
    create_kernel_credential,
    execute_operation,
)


DATABASE_NAME = "post-office-next.sqlite3"
TASK_OPERATIONS = [
    "attention.list",
    "hub.status",
    "task.activate",
    "task.block",
    "task.close",
    "task.read",
    "task.recordResponse",
    "task.review",
    "transport.inspect",
]
COURIER_OPERATIONS = [
    "attention.list",
    "bundle.verify",
    "hub.snapshot",
    "hub.status",
    "message.acknowledge",
    "message.close",
    "message.review",
    "task.read",
    "task.recordResponse",
    "transport.inspect",
    "transport.quarantine",
    "transport.retry",
    "transport.tombstoneDuplicate",
]


def production_status(database: Path, plugin_root: Path) -> dict[str, Any]:
    """Return a secret-free operational snapshot suitable for bounded sweeps."""
    database = database.resolve(strict=False)
    con = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute("BEGIN")
        inspection = inspect_connection(con, database, plugin_root)
        kernel = con.execute(
            "SELECT mode,status,authority_state,contract_root,bootstrapped_at "
            "FROM kernel_instances WHERE instance_id='PON-KERNEL'"
        ).fetchone()

        def grouped(table: str, column: str) -> dict[str, int]:
            return {
                str(row[0]): int(row[1])
                for row in con.execute(
                    f"SELECT {column},COUNT(*) FROM {table} GROUP BY {column} ORDER BY {column}"
                )
            }

        oldest = con.execute(
            """SELECT continuation_id,continuation_kind,state,created_at,lease_expires_at
               FROM continuation_items
               WHERE state IN ('READY','LEASED')
               ORDER BY created_at,continuation_id LIMIT 1"""
        ).fetchone()
        transfer = con.execute(
            """SELECT transfer_id,state,committed_at,pointer_path
               FROM authority_transfers ORDER BY COALESCE(committed_at,''),transfer_id DESC LIMIT 1"""
        ).fetchone()
        reviewers = [
            {
                "threadId": str(row["reviewer_thread_id"]),
                "label": str(row["label"]),
                "state": str(row["status"]),
                "lastSubmissionAt": row["last_submission_at"],
            }
            for row in con.execute(
                "SELECT reviewer_thread_id,label,status,last_submission_at "
                "FROM reviewer_instances ORDER BY rotation_order"
            )
        ]
        historical_classifications = {
            "MESSAGE_STATE_MAPPING",
            "RAW_ONLY_NOT_PROJECTED_P2_1",
        }
        migration_rows = [
            {"classification": str(row[0]), "state": str(row[1]), "count": int(row[2])}
            for row in con.execute(
                "SELECT classification,resolution_state,COUNT(*) FROM migration_anomalies "
                "GROUP BY classification,resolution_state ORDER BY classification,resolution_state"
            )
        ]
        open_historical = sum(
            row["count"] for row in migration_rows
            if row["state"] == "OPEN" and row["classification"] in historical_classifications
        )
        open_actionable = sum(
            row["count"] for row in migration_rows
            if row["state"] == "OPEN" and row["classification"] not in historical_classifications
        )
        return {
            "ok": True,
            "database": str(database.resolve()),
            "kernel": dict(kernel) if kernel else None,
            "authorityTransfer": dict(transfer) if transfer else None,
            "queues": {
                "messages": grouped("semantic_messages", "state"),
                "dispatches": grouped("transport_dispatches", "state"),
                "reviews": grouped("automatic_reviews", "state"),
                "continuations": grouped("continuation_items", "state"),
                "attention": grouped("attention_items", "state"),
                "migrationAnomalies": grouped("migration_anomalies", "resolution_state"),
            },
            "oldestContinuityWork": dict(oldest) if oldest else None,
            "migrationEvidence": {
                "actionableOpenCount": open_actionable,
                "historicalOpenCount": open_historical,
                "knownHistoricalClassifications": sorted(historical_classifications),
                "byClassificationAndState": migration_rows,
            },
            "reviewers": reviewers,
            "backupReceiptCount": int(
                con.execute("SELECT COUNT(*) FROM backup_receipts").fetchone()[0]
            ),
            "unexpectedDriveAccessCount": int(
                con.execute(
                    "SELECT COALESCE(SUM(access_count),0) FROM drive_access_metrics WHERE purpose='UNEXPECTED'"
                ).fetchone()[0]
            ),
            "latestEvent": inspection["database"]["eventBoundary"],
            "logicalStateRoot": inspection["logicalStateRoot"],
            "secretsIncluded": False,
        }
    finally:
        if con.in_transaction:
            con.rollback()
        con.close()


def _copy_import(source_root: Path, output_root: Path) -> Path:
    source_root = source_root.resolve(strict=True)
    output_root = output_root.resolve(strict=False)
    if output_root.exists() or os.path.lexists(output_root):
        raise PostOfficeError("PON_OUTPUT_EXISTS", "Production staging root must be absent", {"path": str(output_root)})
    if output_root == source_root or source_root in output_root.parents or output_root in source_root.parents:
        raise PostOfficeError("PON_PATH_CONFLICT", "Production staging root must be disjoint from import evidence", {})
    for member in source_root.rglob("*"):
        if member.is_symlink():
            raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Import evidence contains a linked member", {"path": str(member)})
    try:
        shutil.copytree(source_root, output_root, copy_function=shutil.copy2)
    except Exception:
        if output_root.exists():
            shutil.rmtree(output_root)
        raise
    database = output_root / DATABASE_NAME
    if not database.is_file():
        raise PostOfficeError("PON_PATH_NOT_FOUND", "Imported vNext database is absent", {"path": str(database)})
    return database


def _task_bindings(database: Path) -> list[dict[str, str]]:
    con = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        bindings: list[dict[str, str]] = []
        for row in con.execute(
            """SELECT t.task_id,t.project_id,e.actor_id,e.access_scope_json
               FROM tasks t JOIN task_endpoint_bindings b ON b.task_id=t.task_id
               JOIN endpoints e ON e.endpoint_id=b.endpoint_id
               JOIN actors a ON a.actor_id=e.actor_id
               WHERE t.state NOT IN ('CLOSED','CANCELLED') AND e.status='ACTIVE' AND a.status='ACTIVE'
               ORDER BY t.task_id"""
        ):
            scope = json.loads(row["access_scope_json"])
            thread_id = scope.get("threadId")
            if isinstance(thread_id, str) and thread_id:
                bindings.append({
                    "taskId": str(row["task_id"]),
                    "projectId": str(row["project_id"]),
                    "actorId": str(row["actor_id"]),
                    "threadId": thread_id,
                })
        return bindings
    finally:
        con.close()


def prepare_production_root(
    source_root: Path,
    output_root: Path,
    receipt_path: Path,
    plugin_root: Path,
) -> dict[str, Any]:
    """Copy a verified import into a disjoint shadow root and provision replacement credentials.

    Secret values are written only beneath caller-secrets and never returned in the receipt.
    The source import is never modified.
    """
    receipt_path = require_new_output_file(receipt_path, inputs=[source_root])
    source_database = source_root.resolve(strict=True) / DATABASE_NAME
    source_inspection = inspect_database(source_database, plugin_root)
    database = _copy_import(source_root, output_root)
    copied_inspection = inspect_database(database, plugin_root)
    if source_inspection["logicalStateRoot"] != copied_inspection["logicalStateRoot"]:
        shutil.rmtree(output_root)
        raise PostOfficeError("PON_MIGRATION_MISMATCH", "Copied production root differs from the verified import", {})

    secrets_root = output_root / "caller-secrets"
    secrets_root.mkdir(parents=True, exist_ok=False)
    author_credential = secrets_root / "author.token"
    create_kernel_credential(author_credential, "PON-CAPABILITY-PRODUCTION-AUTHOR")
    bootstrap = bootstrap_kernel(
        database,
        author_credential,
        actor_id="PON-ACTOR-PRODUCTION-AUTHOR",
        actor_kind="HUMAN",
        actor_role="author",
        mode="SHADOW",
        plugin_root=plugin_root,
    )
    create_kernel_actor(
        database,
        author_credential,
        action_id="PON-AUTHOR-ACTION-CREATE-COURIER",
        actor_id="PON-ACTOR-PRODUCTION-COURIER",
        actor_kind="COURIER",
        actor_role="courier",
        plugin_root=plugin_root,
    )
    courier_credential = secrets_root / "courier.token"
    courier = bind_kernel_credential(
        database,
        author_credential,
        courier_credential,
        action_id="PON-AUTHOR-ACTION-BIND-COURIER",
        actor_id="PON-ACTOR-PRODUCTION-COURIER",
        capability_id="PON-CAPABILITY-PRODUCTION-COURIER",
        subject_kind="COURIER",
        subject_id="PON-ACTOR-PRODUCTION-COURIER",
        subject_generation=None,
        allowed_operations=COURIER_OPERATIONS,
        expires_at=None,
        plugin_root=plugin_root,
    )

    task_receipts: list[dict[str, str]] = []
    seen_threads: set[str] = set()
    for binding in _task_bindings(database):
        thread_id = binding["threadId"]
        if thread_id in seen_threads:
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Multiple active tasks share one caller-secret path", {"threadId": thread_id})
        seen_threads.add(thread_id)
        suffix = sha256_json(binding).removeprefix("sha256:")[:24]
        action_id = f"PON-AUTHOR-ACTION-GRANT-TASK-{suffix}"
        grant_id = f"PON-GRANT-TASK-{suffix}"
        grant_request = {
            "schemaVersion": "1",
            "operation": "authority.grant",
            "requestId": f"PON-REQUEST-GRANT-TASK-{suffix}",
            "actor": {"id": "PON-ACTOR-PRODUCTION-AUTHOR", "kind": "HUMAN", "role": "author"},
            "authority": {
                "capabilityId": "PON-CAPABILITY-PRODUCTION-AUTHOR",
                "exactAuthorActionId": action_id,
            },
            "aggregate": {"type": "AuthorityGrant", "id": grant_id, "expectedVersion": 0},
            "parameters": {
                "grant": {
                    "grantorId": "PON-ACTOR-PRODUCTION-AUTHOR",
                    "recipientId": binding["actorId"],
                    "allowedOperations": [
                        "task.activate", "task.block", "task.close", "task.recordResponse", "task.review"
                    ],
                    "scope": {"taskIds": [binding["taskId"]]},
                    "classification": "STANDING",
                    "rationale": "Continue the exact migrated task after the one-time authority transfer",
                    "sourceDecisionId": action_id,
                }
            },
        }
        request_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(prefix=".task-grant-", suffix=".json", dir=output_root)
            request_path = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(grant_request, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            execute_operation(database, request_path, author_credential, plugin_root)
        finally:
            if request_path and request_path.exists():
                request_path.unlink()
        credential = secrets_root / f"{thread_id}.token"
        bound = bind_kernel_credential(
            database,
            author_credential,
            credential,
            action_id=f"PON-AUTHOR-ACTION-BIND-TASK-{suffix}",
            actor_id=binding["actorId"],
            capability_id=f"PON-CAPABILITY-TASK-{suffix}",
            subject_kind="TASK",
            subject_id=binding["taskId"],
            subject_generation=None,
            allowed_operations=TASK_OPERATIONS,
            expires_at=None,
            plugin_root=plugin_root,
        )
        task_receipts.append({
            **binding,
            "capabilityId": bound["capabilityId"],
            "authorityGrantId": grant_id,
            "secretSha256": bound["secretSha256"],
        })

    final_inspection = inspect_database(database, plugin_root)
    receipt = {
        "schemaVersion": "1",
        "kind": "POST_OFFICE_NEXT_PRODUCTION_PREPARATION",
        "sourceRoot": str(source_root.resolve()),
        "productionRoot": str(output_root.resolve()),
        "database": str(database.resolve()),
        "sourceLogicalStateRoot": source_inspection["logicalStateRoot"],
        "copiedLogicalStateRoot": copied_inspection["logicalStateRoot"],
        "preparedLogicalStateRoot": final_inspection["logicalStateRoot"],
        "databaseSha256": sha256_file(database),
        "bootstrap": {
            "instanceId": bootstrap["instanceId"],
            "contractRoot": bootstrap["contractRoot"],
            "mode": bootstrap["mode"],
            "authorityState": "PREVIEW",
        },
        "courier": {
            "actorId": courier["actorId"],
            "capabilityId": courier["capabilityId"],
            "secretSha256": courier["secretSha256"],
        },
        "taskCredentials": task_receipts,
        "taskCredentialCount": len(task_receipts),
        "secretsExcluded": True,
    }
    receipt["receiptRoot"] = sha256_json(receipt)
    write_json(receipt_path, receipt)
    return {"ok": True, **receipt, "receipt": str(receipt_path.resolve())}
