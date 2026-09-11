# SPDX-License-Identifier: MPL-2.0

"""Small authenticated task-facing adapter for an authoritative Post Office Next root."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from post_office.canonical import sha256_bytes  # noqa: E402
from post_office.diagnostics import PostOfficeError  # noqa: E402
from post_office.kernel import execute_operation  # noqa: E402


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise PostOfficeError("PON_CREDENTIAL_INVALID", "Task credential is not an object", {})
    return value


def _open(database: Path, credential_path: Path) -> tuple[sqlite3.Connection, sqlite3.Row, sqlite3.Row]:
    credential = _json(credential_path.resolve(strict=True))
    con = sqlite3.connect(database.resolve(strict=True).as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    capability = con.execute(
        "SELECT * FROM caller_capabilities WHERE capability_id=?", (credential.get("capabilityId"),)
    ).fetchone()
    supplied = sha256_bytes(str(credential.get("secret", "")).encode("utf-8"))
    if (
        not capability
        or capability["status"] != "ACTIVE"
        or capability["subject_kind"] != "TASK"
        or not hmac.compare_digest(str(capability["secret_sha256"]), supplied)
    ):
        con.close()
        raise PostOfficeError("PON_AUTHENTICATION_FAILED", "Task credential is not active or task-bound", {})
    actor = con.execute("SELECT * FROM actors WHERE actor_id=?", (capability["actor_id"],)).fetchone()
    task = con.execute("SELECT * FROM tasks WHERE task_id=?", (capability["subject_id"],)).fetchone()
    if not actor or actor["status"] != "ACTIVE" or not task:
        con.close()
        raise PostOfficeError("PON_AUTHENTICATION_FAILED", "Task identity is not active", {})
    return con, capability, task


def _grant(con: sqlite3.Connection, actor_id: str, task_id: str, operation: str) -> str:
    for row in con.execute(
        "SELECT * FROM authority_grants WHERE recipient_actor_id=? AND status='ACTIVE' ORDER BY created_at DESC",
        (actor_id,),
    ):
        if operation not in json.loads(row["allowed_operations_json"]):
            continue
        scope = json.loads(row["scope_json"])
        if task_id in scope.get("taskIds", []):
            return str(row["grant_id"])
    raise PostOfficeError("PON_AUTHORIZATION_DENIED", "No active task authority covers this operation", {"operation": operation})


def _mutate(database: Path, credential: Path, plugin_root: Path, operation: str, parameters: dict[str, Any]) -> dict[str, Any]:
    con, capability, task = _open(database, credential)
    try:
        actor = con.execute("SELECT * FROM actors WHERE actor_id=?", (capability["actor_id"],)).fetchone()
        grant_id = _grant(con, str(actor["actor_id"]), str(task["task_id"]), operation)
        request = {
            "schemaVersion": "1",
            "operation": operation,
            "requestId": "PON-TASK-REQUEST-" + uuid.uuid4().hex,
            "actor": {"id": actor["actor_id"], "kind": actor["actor_kind"], "role": actor["role"]},
            "authority": {"capabilityId": capability["capability_id"], "authorityGrantId": grant_id},
            "aggregate": {"type": "Task", "id": task["task_id"], "expectedVersion": int(task["aggregate_version"])},
            "parameters": {"taskId": task["task_id"], **parameters},
        }
    finally:
        con.close()
    request_path: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix="pon-task-request-", suffix=".json")
        request_path = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(request, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
        return execute_operation(database, request_path, credential, plugin_root)
    finally:
        if request_path and request_path.exists():
            request_path.unlink()


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    database = Path(args.path)
    credential = Path(args.credential)
    plugin_root = Path(__file__).resolve().parent.parent
    con, capability, task = _open(database, credential)
    try:
        if args.command == "task":
            return {"ok": True, "task": dict(task)}
        if args.command == "inbox":
            binding = con.execute("SELECT * FROM task_endpoint_bindings WHERE task_id=?", (task["task_id"],)).fetchone()
            if not binding:
                return {"ok": True, "taskId": task["task_id"], "messages": [], "count": 0}
            rows = []
            for message in con.execute(
                """SELECT * FROM semantic_messages WHERE recipient_mailbox_id=? AND recipient_generation=?
                   AND state NOT IN ('CLOSED','CANCELLED') ORDER BY created_at,message_id""",
                (binding["mailbox_id"], binding["mailbox_generation"]),
            ):
                bundles = [dict(item) for item in con.execute(
                    "SELECT * FROM message_bundles WHERE message_id=? ORDER BY created_at,bundle_id", (message["message_id"],)
                )]
                rows.append({"message": dict(message), "bundles": bundles})
            return {"ok": True, "taskId": task["task_id"], "messages": rows, "count": len(rows)}
    finally:
        con.close()
    if args.command == "activate":
        return _mutate(database, credential, plugin_root, "task.activate", {})
    if args.command == "block":
        return _mutate(database, credential, plugin_root, "task.block", {"reason": args.reason})
    if args.command == "respond":
        return _mutate(
            database, credential, plugin_root, "task.recordResponse",
            {"messageId": args.message_id, "evidenceRoots": args.evidence_root},
        )
    if args.command == "review":
        return _mutate(database, credential, plugin_root, "task.review", {"decision": args.decision, "rationale": args.reason})
    return _mutate(database, credential, plugin_root, "task.close", {"summary": args.summary})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["task", "inbox", "activate", "block", "respond", "review", "close"])
    parser.add_argument("--path", required=True)
    parser.add_argument("--credential", required=True)
    parser.add_argument("--reason")
    parser.add_argument("--message-id")
    parser.add_argument("--evidence-root", action="append", default=[])
    parser.add_argument("--decision", choices=["ACCEPT", "CORRECTION_REQUIRED"])
    parser.add_argument("--summary")
    args = parser.parse_args()
    if args.command == "block" and not args.reason:
        parser.error("block requires --reason")
    if args.command == "respond" and (not args.message_id or not args.evidence_root):
        parser.error("respond requires --message-id and at least one --evidence-root")
    if args.command == "review" and (not args.decision or not args.reason):
        parser.error("review requires --decision and --reason")
    if args.command == "close" and not args.summary:
        parser.error("close requires --summary")
    try:
        print(json.dumps(dispatch(args), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    except PostOfficeError as exc:
        print(json.dumps(exc.as_result(), separators=(",", ":")), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
