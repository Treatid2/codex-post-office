# SPDX-License-Identifier: MPL-2.0
"""Task-facing compatibility adapter for the vNext automatic-review companion.

This intentionally exposes only inspect/submit/ensure/status/complete/withdraw. Courier review
activation, browser transport, collection, and return remain Post Office runtime operations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath


REVIEW_ID_LINE = re.compile(r"(?im)^REVIEW ID:\s*([^\s]+)\s*$")
PACKAGE_NAME_LINE = re.compile(r"(?im)^PACKAGE NAME:\s*(.+?)\s*$")
MAX_PACKAGE_BYTES = 100 * 1024 * 1024


class ReviewError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def stable_package(path: Path, max_bytes: int = MAX_PACKAGE_BYTES) -> tuple[bytes, str, str]:
    path = path.resolve(strict=True)
    before = path.stat()
    if not path.is_file() or before.st_size <= 0 or before.st_size > max_bytes:
        raise ReviewError("Review package size is invalid")
    data = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ReviewError("Review package changed while being inspected")
    try:
        with zipfile.ZipFile(path) as archive:
            contracts = [name for name in archive.namelist() if PurePosixPath(name).name == "REVIEW_CONTRACT.md"]
            if len(contracts) != 1:
                raise ReviewError("Review package must contain exactly one REVIEW_CONTRACT.md")
            contract = archive.read(contracts[0]).decode("utf-8")
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError) as exc:
        raise ReviewError("Review package is not a valid review archive") from exc
    review_match = REVIEW_ID_LINE.search(contract)
    name_match = PACKAGE_NAME_LINE.search(contract)
    if not review_match or not name_match or name_match.group(1) != path.name:
        raise ReviewError("Review package contract identity is invalid")
    return data, review_match.group(1), hashlib.sha256(data).hexdigest()


def database_path(root: Path) -> Path:
    # Prefer the authoritative vNext filename.  The legacy name is retained only for
    # pre-cutover compatibility and must never win merely because frozen evidence is
    # present beside the production database.
    for name in ("post-office-next.sqlite3", "hub.sqlite3"):
        candidate = root / name
        if candidate.is_file():
            return candidate
    raise ReviewError("vNext Post Office database is unavailable")


def connect(root: Path) -> sqlite3.Connection:
    con = sqlite3.connect(database_path(root), timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("BEGIN IMMEDIATE")
    kernel = con.execute("SELECT authority_state,status FROM kernel_instances WHERE instance_id='PON-KERNEL'").fetchone()
    if not kernel or kernel["authority_state"] != "AUTHORITATIVE" or kernel["status"] != "READY":
        con.rollback(); con.close()
        raise ReviewError("vNext Post Office is not authoritative and ready")
    return con


def authenticate(con: sqlite3.Connection, root: Path, thread_id: str, host_id: str) -> None:
    token_path = root / "caller-secrets" / f"{thread_id}.token"
    if not token_path.is_file() or token_path.is_symlink():
        raise ReviewError("Review-only requester credential is not provisioned")
    token_hash = hashlib.sha256(token_path.read_bytes()).hexdigest()
    retained = con.execute("SELECT * FROM review_requester_credentials WHERE requester_thread_id=?", (thread_id,)).fetchone()
    timestamp = now()
    if retained:
        if retained["status"] != "ACTIVE" or retained["requester_host_id"] != host_id or not secrets.compare_digest(retained["secret_sha256"], token_hash):
            raise ReviewError("Review-only requester credential does not match retained identity")
        con.execute("UPDATE review_requester_credentials SET last_used_at=? WHERE requester_thread_id=?", (timestamp, thread_id))
    else:
        con.execute("INSERT INTO review_requester_credentials VALUES(?,?,?,?,?,?)", (thread_id, host_id, token_hash, "ACTIVE", timestamp, timestamp))


def requester(con: sqlite3.Connection, thread_id: str) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row]:
    for task in con.execute("SELECT * FROM tasks"):
        grant = con.execute("SELECT scope_json FROM authority_grants WHERE grant_id=?", (task["authority_grant_id"],)).fetchone()
        if grant and json.loads(grant["scope_json"]).get("threadId") == thread_id:
            endpoint = con.execute("SELECT * FROM endpoints WHERE task_id=? AND status='ACTIVE'", (task["task_id"],)).fetchone()
            mailbox = con.execute("SELECT * FROM mailboxes WHERE endpoint_id=? AND status='ACTIVE' ORDER BY generation DESC LIMIT 1", (endpoint["endpoint_id"],)).fetchone() if endpoint else None
            if endpoint and mailbox:
                return task, endpoint, mailbox
    raise ReviewError("Requester task identity was not retained; courier provisioning is required")


def reviewer(con: sqlite3.Connection) -> tuple[sqlite3.Row, sqlite3.Row]:
    row = con.execute(
        """SELECT i.* FROM reviewer_instances i WHERE i.status='ACTIVE'
           ORDER BY COALESCE(i.last_submission_at,'') ASC,i.rotation_order,i.reviewer_thread_id LIMIT 1"""
    ).fetchone()
    if not row:
        raise ReviewError("No active automatic-review browser is registered")
    mailbox = con.execute("SELECT * FROM mailboxes WHERE endpoint_id=? AND status='ACTIVE' ORDER BY generation DESC LIMIT 1", (row["endpoint_id"],)).fetchone()
    if not mailbox:
        raise ReviewError("Active reviewer has no active browser mailbox")
    return row, mailbox


def public_status(state: str) -> str:
    return {
        "QUEUED": "QUEUED_LOCAL", "ACTIVE": "REVIEW_ACTIVE", "RETURNED": "REVIEW_RETURNED",
        "EVALUATED": "COMPLETED", "WITHDRAWN": "WITHDRAWN", "SUPERSEDED": "WITHDRAWN",
        "FAILED_FINAL": "BLOCKED",
    }[state]


def result(row: sqlite3.Row, *, created: bool | None = None, ensure_state: str | None = None,
           ensure_match: str | None = None) -> dict[str, object]:
    value: dict[str, object] = {
        "review_id": row["review_id"], "requester_thread_id": row["requester_thread_id"],
        "requester_host_id": row["requester_host_id"] or "local", "status": public_status(row["state"]),
        "package_name": row["package_name"], "package_size_bytes": row["package_size_bytes"],
        "package_sha256": row["package_sha256"], "readiness": row["readiness"] or "PR_SCALE_NEAR_COMPLETE",
        "idempotency_key": row["idempotency_key"], "reviewer_thread_id": row["reviewer_thread_id"],
        "heartbeat_required": False,
    }
    if ensure_state is not None:
        value.update({"ensure_state": ensure_state, "ensure_match": ensure_match, "created": bool(created)})
    return value


def retain_package(root: Path, data: bytes, sha256: str) -> str:
    relative = f"objects/{sha256[:2]}/{sha256}"
    target = root / Path(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() != sha256:
            raise ReviewError("Local CAS object differs from its digest")
    else:
        temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
        temporary.write_bytes(data)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != sha256:
            temporary.unlink(missing_ok=True)
            raise ReviewError("Local CAS write verification failed")
        os.replace(temporary, target)
    return relative.replace("\\", "/")


def create_review(con: sqlite3.Connection, root: Path, *, thread_id: str, host_id: str,
                  subject: str, package: Path, idempotency_key: str, cooldown_minutes: int) -> tuple[sqlite3.Row, bool, str]:
    data, review_id, package_hash = stable_package(package)
    existing = con.execute("SELECT * FROM automatic_reviews WHERE idempotency_key=?", (idempotency_key,)).fetchone()
    if existing:
        if existing["review_id"] != review_id or existing["package_sha256"] != package_hash or existing["requester_thread_id"] != thread_id:
            raise ReviewError("Review idempotency key is bound to different work")
        return existing, False, "IDEMPOTENCY_KEY"
    existing = con.execute("SELECT * FROM automatic_reviews WHERE review_id=?", (review_id,)).fetchone()
    if existing:
        if existing["package_sha256"] != package_hash or existing["requester_thread_id"] != thread_id:
            raise ReviewError("Review ID is bound to different work")
        return existing, False, "REVIEW_ID"
    recent = con.execute("SELECT * FROM automatic_reviews WHERE requester_thread_id=? ORDER BY queued_at DESC LIMIT 1", (thread_id,)).fetchone()
    if recent and datetime.now(timezone.utc) - datetime.fromisoformat(recent["queued_at"].replace("Z", "+00:00")) < timedelta(minutes=cooldown_minutes):
        raise ReviewError("REVIEW_REQUESTER_COOLDOWN:" + recent["review_id"])
    task, endpoint, _ = requester(con, thread_id)
    review_instance, review_mailbox = reviewer(con)
    timestamp = now()
    relative = retain_package(root, data, package_hash)
    storage_id = "PON-STORAGE-" + package_hash[:40]
    con.execute(
        "INSERT OR IGNORE INTO storage_copies VALUES(?,?,?,?,?,?,?,?)",
        (storage_id, package_hash, "LOCAL_CAS", relative, len(data), "AUTHORITATIVE", "SHA256_READBACK", timestamp),
    )
    suffix = hashlib.sha256((review_id + package_hash).encode()).hexdigest()[:24]
    action_id, grant_id = "PON-ACTION-REVIEW-" + suffix, "PON-GRANT-REVIEW-" + suffix
    cycle_id, message_id, bundle_id = "PON-CYCLE-REVIEW-" + suffix, "PON-MESSAGE-REVIEW-" + suffix, "PON-BUNDLE-REVIEW-" + suffix
    actor_id = endpoint["actor_id"]
    scope = {"reviewId": review_id, "requesterThreadId": thread_id, "packageSha256": package_hash}
    con.execute("INSERT INTO exact_author_actions(action_id,actor_id,operation,scope_json,confirmation_sha256,recorded_at) VALUES(?,?,?,?,?,?)", (action_id, actor_id, "automatic-review.request", canonical(scope).decode(), digest(scope), timestamp))
    con.execute("INSERT INTO authority_grants(grant_id,grantor_actor_id,recipient_actor_id,allowed_operations_json,scope_json,classification,maximum_uses,remaining_uses,status,rationale,source_author_action_id,created_at) VALUES(?,?,?,?,?,'ONE_SHOT',1,0,'EXHAUSTED',?,?,?)", (grant_id, actor_id, actor_id, '["automatic-review.request"]', canonical(scope).decode(), "Task-bound automatic review request", action_id, timestamp))
    cycle_state = {"reviewId": review_id, "state": "ACTIVE"}
    con.execute("INSERT INTO semantic_cycles VALUES(?,?,?,?,?,?,?,?,?,?)", (cycle_id, task["project_id"], "Automatic review " + review_id, action_id, "ACTIVE", None, None, 1, digest(cycle_state), timestamp))
    content = {"reviewId": review_id, "subject": subject, "packageSha256": package_hash}
    aggregate = {**content, "state": "CUSTODY_RECORDED"}
    con.execute("INSERT INTO semantic_messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (message_id, task["project_id"], review_mailbox["mail_domain"], "QUERY", endpoint["endpoint_id"], review_mailbox["mailbox_id"], review_mailbox["generation"], cycle_id, grant_id, None, "Perform independent automatic code review", "Return a retained review result", digest(content), "CUSTODY_RECORDED", 1, digest(aggregate), None, timestamp))
    con.execute("INSERT INTO message_bundles VALUES(?,?,?,?,?,?,?,?,?,?)", (bundle_id, message_id, package.name, 1, len(data), package_hash, digest([{ "ordinal": 0, "sha256": package_hash, "sizeBytes": len(data)}]), "REGISTERED", None, timestamp))
    con.execute("INSERT INTO bundle_payloads VALUES(?,?,?,?,?)", (bundle_id, 0, "payloads/0000-" + package_hash, len(data), package_hash))
    con.execute("INSERT INTO transport_attempts VALUES(?,?,?,?,?,'PENDING',1,NULL,?,?)", ("PON-TRANSPORT-REVIEW-" + suffix, message_id, bundle_id, "AUTOMATIC_REVIEW_COMPANION", review_mailbox["mailbox_id"], timestamp, timestamp))
    con.execute(
        """INSERT INTO automatic_reviews(
           review_id,semantic_message_id,requester_task_id,reviewer_endpoint_id,package_sha256,state,queued_at,
           requester_thread_id,reviewer_thread_id,package_name,package_size_bytes,requester_host_id,subject,
           readiness,idempotency_key,legacy_status)
           VALUES(?,?,?,?,?,'QUEUED',?,?,?,?,?,?,?,?,?,'QUEUED_LOCAL')""",
        (review_id, message_id, task["task_id"], review_instance["endpoint_id"], package_hash, timestamp,
         thread_id, review_instance["reviewer_thread_id"], package.name, len(data), host_id, subject,
         "PR_SCALE_NEAR_COMPLETE", idempotency_key),
    )
    con.execute("UPDATE reviewer_instances SET last_submission_at=?,updated_at=? WHERE reviewer_thread_id=?", (timestamp, timestamp, review_instance["reviewer_thread_id"]))
    return con.execute("SELECT * FROM automatic_reviews WHERE review_id=?", (review_id,)).fetchone(), True, "ABSENT"


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--hub-root", required=True)
    p.add_argument("--capability")
    sub = p.add_subparsers(dest="command", required=True)
    q = sub.add_parser("inspect"); q.add_argument("--package", required=True); q.add_argument("--max-bytes", type=int, default=MAX_PACKAGE_BYTES)
    for name in ("submit", "ensure"):
        q = sub.add_parser(name); q.add_argument("--requester-thread", required=True); q.add_argument("--requester-host", default="local")
        q.add_argument("--requester-project"); q.add_argument("--requester-root"); q.add_argument("--reviewer-thread", default="AUTO")
        q.add_argument("--browser-mailbox"); q.add_argument("--guidance-url"); q.add_argument("--subject", required=True)
        q.add_argument("--readiness", required=True); q.add_argument("--package", required=True); q.add_argument("--idempotency-key", required=True)
        q.add_argument("--max-bytes", type=int, default=MAX_PACKAGE_BYTES); q.add_argument("--cooldown-minutes", type=int, default=30)
    q = sub.add_parser("status"); q.add_argument("--review-id", required=True)
    q = sub.add_parser("complete"); q.add_argument("--review-id", required=True); q.add_argument("--summary", required=True)
    q = sub.add_parser("withdraw"); q.add_argument("--review-id", required=True); q.add_argument("--reason", required=True); q.add_argument("--idempotency-key", required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = Path(args.hub_root).resolve(strict=True)
    try:
        if args.command == "inspect":
            data, review_id, package_hash = stable_package(Path(args.package), args.max_bytes)
            answer = {"review_id": review_id, "package_name": Path(args.package).name,
                      "package_size_bytes": len(data), "package_sha256": package_hash}
        else:
            thread_id = os.environ.get("CODEX_THREAD_ID")
            host_id = os.environ.get("CODEX_HOST_ID", "local")
            if not thread_id:
                raise ReviewError("CODEX_THREAD_ID is required")
            con = connect(root)
            try:
                authenticate(con, root, thread_id, host_id)
                if args.command in {"submit", "ensure"}:
                    if args.requester_thread != thread_id or args.requester_host != host_id:
                        raise ReviewError("Runtime requester identity mismatch")
                    try:
                        row, created, match = create_review(con, root, thread_id=thread_id, host_id=host_id, subject=args.subject,
                                                           package=Path(args.package), idempotency_key=args.idempotency_key,
                                                           cooldown_minutes=args.cooldown_minutes)
                        if args.command == "ensure":
                            answer = result(
                                row, created=created,
                                ensure_state=("ABSENT_SUBMIT_CREATED" if created else
                                              "TERMINAL" if row["state"] in {"EVALUATED","WITHDRAWN","SUPERSEDED","FAILED_FINAL"} else
                                              "RETURNED_COMPLETE_REQUIRED" if row["state"] == "RETURNED" else
                                              "ACTIVE_DO_NOT_RESUBMIT"),
                                ensure_match=match,
                            )
                        else:
                            answer = result(row)
                    except ReviewError as exc:
                        if args.command == "ensure" and str(exc).startswith("REVIEW_REQUESTER_COOLDOWN:"):
                            retained = con.execute("SELECT * FROM automatic_reviews WHERE review_id=?", (str(exc).split(":",1)[1],)).fetchone()
                            answer = result(retained, created=False, ensure_state="CONFLICT", ensure_match="REQUESTER_COOLDOWN")
                            answer.update({"conflict_reason": "COOLDOWN_ACTIVE"})
                        else:
                            raise
                else:
                    row = con.execute("SELECT * FROM automatic_reviews WHERE review_id=? AND requester_thread_id=?", (args.review_id, thread_id)).fetchone()
                    if not row:
                        raise ReviewError("Review is not owned by this requester")
                    if args.command == "status":
                        answer = result(row)
                    elif args.command == "complete":
                        if row["state"] not in {"RETURNED", "EVALUATED"}:
                            raise ReviewError("Only a returned review may be completed")
                        con.execute("UPDATE automatic_reviews SET state='EVALUATED',evaluated_at=?,completed_summary=? WHERE review_id=?", (now(), args.summary, args.review_id))
                        answer = result(con.execute("SELECT * FROM automatic_reviews WHERE review_id=?", (args.review_id,)).fetchone())
                    else:
                        retained_key = row["withdrawal_idempotency_key"]
                        if row["state"] == "WITHDRAWN" and retained_key == args.idempotency_key:
                            replayed = True
                        else:
                            if row["state"] != "QUEUED":
                                raise ReviewError("Only a queued review may be withdrawn")
                            con.execute("UPDATE automatic_reviews SET state='WITHDRAWN',withdrawn_at=?,withdrawal_reason=?,withdrawal_idempotency_key=? WHERE review_id=?", (now(), args.reason.strip(), args.idempotency_key, args.review_id))
                            replayed = False
                        updated = con.execute("SELECT * FROM automatic_reviews WHERE review_id=?", (args.review_id,)).fetchone()
                        answer = result(updated)
                        answer.update({"withdrawal_reason": updated["withdrawal_reason"], "withdrawal_idempotency_key": updated["withdrawal_idempotency_key"],
                                       "withdrawn_at": updated["withdrawn_at"], "withdrawal_replayed": replayed,
                                       "withdrawal_custody_preserved": True})
                con.commit()
            except Exception:
                if con.in_transaction: con.rollback()
                raise
            finally:
                con.close()
        print(json.dumps(answer, ensure_ascii=False, sort_keys=True))
        return 0
    except (ReviewError, OSError, sqlite3.Error, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
