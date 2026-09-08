# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .canonical import (
    contained_member,
    is_link_like,
    merkle_root,
    read_json,
    require_new_output_file,
    sha256_file,
    sha256_json,
    sqlite_content_identity,
    write_json,
)
from .diagnostics import PostOfficeError
from .legacy import (
    AUTO_REVIEW_SCHEMA_VERSION,
    HUB_SCHEMA_VERSION,
    LOCAL_EXTERNAL_EVIDENCE_KINDS,
    OBSERVER_SCHEMA_VERSION,
    REMOTE_EXTERNAL_EVIDENCE_KINDS,
    readonly_connection,
)


SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in con.execute(f'PRAGMA table_info("{table}")')]


def _camel(name: str) -> str:
    pieces = name.split("_")
    return pieces[0] + "".join(piece.title() for piece in pieces[1:])


def _rows(
    con: sqlite3.Connection,
    table: str,
    selected: list[str] | None = None,
    hashed: Iterable[str] = (),
    order: list[str] | None = None,
) -> list[dict[str, Any]]:
    if not _table_exists(con, table):
        return []
    available = _columns(con, table)
    chosen = [name for name in (selected or available) if name in available]
    hashed_set = set(hashed)
    query_names = chosen + [name for name in hashed_set if name in available and name not in chosen]
    ordering = [name for name in (order or query_names[:1]) if name in available]
    sql = f'SELECT {", ".join(chr(34) + name + chr(34) for name in query_names)} FROM "{table}"'
    if ordering:
        sql += " ORDER BY " + ", ".join('"' + name + '"' for name in ordering)
    result = []
    for raw in con.execute(sql):
        source = {name: raw[name] for name in query_names}
        projected: dict[str, Any] = {}
        for name in chosen:
            value = source[name]
            if name in hashed_set:
                projected[_camel(name) + "Sha256"] = hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()
            elif name.endswith("_json") and isinstance(value, str):
                try:
                    projected[_camel(name[:-5])] = json.loads(value)
                except json.JSONDecodeError:
                    projected[_camel(name)] = value
            else:
                projected[_camel(name)] = value
        for name in hashed_set - set(chosen):
            if name in source:
                projected[_camel(name) + "Sha256"] = hashlib.sha256(str(source[name] or "").encode("utf-8")).hexdigest()
        projected["evidence"] = {
            "database": "hub.sqlite3" if table != "thread_state" else "observer.sqlite3",
            "table": table,
            "rowHash": sha256_json(projected),
        }
        result.append(projected)
    return result


def _metadata(con: sqlite3.Connection) -> dict[str, str]:
    if not _table_exists(con, "metadata"):
        return {}
    return {str(row[0]): str(row[1]) for row in con.execute("SELECT key,value FROM metadata ORDER BY key")}


def _hub_inventory(con: sqlite3.Connection) -> dict[str, Any]:
    mailboxes = _rows(con, "mailboxes", order=["mailbox_id"])
    messages = _rows(
        con,
        "messages",
        selected=["message_id", "cycle_id", "root_cycle_id", "project_code", "sender_mailbox_id", "sender_generation",
                  "recipient_mailbox_id", "recipient_generation", "message_type", "authorization_class", "subject", "status",
                  "parent_message_id", "may_delegate", "max_depth", "current_depth", "expires_at", "created_at", "updated_at"],
        hashed=["body", "requested_action", "completion_criteria", "completed_summary", "unresolved", "acknowledgement_evidence"],
        order=["message_id"],
    )
    payloads = _rows(con, "message_payloads", order=["message_id", "ordinal"])
    outboxes = _rows(con, "browser_outbox", order=["outbox_id"])
    deliveries = _rows(con, "browser_deliveries", order=["delivery_id"])
    bindings = _rows(con, "browser_chat_bindings", order=["mailbox_id"])
    pokes = _rows(con, "browser_poke_queue", order=["poke_id"])
    wakes = _rows(con, "wake_authorizations", selected=["authorization_id", "message_id", "courier_mailbox_id", "sender_mailbox_id",
         "recipient_mailbox_id", "recipient_generation", "status", "idempotency_key", "created_at", "updated_at", "packet_issued_at"],
         hashed=["author_confirmation"], order=["authorization_id"])
    capabilities = _rows(con, "caller_capabilities", selected=["capability_id", "subject_kind", "subject_id", "subject_generation", "thread_id", "host_id",
        "operations_json", "status", "expires_at", "created_at", "revoked_at"], order=["capability_id"])
    reviews = _rows(con, "auto_reviews", selected=["review_id", "requester_thread_id", "requester_host_id", "requester_project_id", "requester_root",
        "reviewer_thread_id", "browser_mailbox_id", "browser_generation", "drive_endpoint", "guidance_url", "subject", "readiness", "package_name",
        "package_size_bytes", "package_sha256", "contract_sha256", "manifest_sha256", "submission_mode", "status", "drive_file_id", "drive_url",
        "drive_name", "drive_size_bytes", "drive_parent", "drive_sha256", "drive_verification_level", "queued_at", "queue_sequence",
        "activation_state", "result_name", "result_size_bytes", "result_sha256", "result_source_thread_id", "result_source_message_id",
        "result_evidence_level", "verdict", "return_state", "blocked_reason", "created_at", "updated_at", "completed_at"], order=["review_id"])
    event_headers = _rows(con, "events", selected=["sequence", "event_id", "event_type", "actor", "object_id", "occurred_at", "previous_hash", "event_hash"],
                          hashed=["data_json"], order=["sequence"])

    domains = sorted({str(row.get("projectCode")) for row in [*mailboxes, *messages, *outboxes] if row.get("projectCode")})
    endpoints = [{
        "legacyIdentity": row["mailboxId"],
        "projectCode": row["projectCode"],
        "role": row["kind"],
        "threadId": row.get("threadId"),
        "hostId": row.get("hostId"),
        "mailboxId": row["mailboxId"],
        "mailboxGeneration": row["generation"],
        "identityStatus": "LEGACY_MAILBOX_BOUND_ACTOR_NO_SEPARATE_ENDPOINT_ID",
        "evidence": row["evidence"],
    } for row in mailboxes]
    projects = [{
        "legacyProjectCode": domain,
        "catalogueStatus": "NOT_NATIVE_TO_SCHEMA_12",
        "evidence": sorted({
            row["evidence"]["rowHash"]
            for row in [*mailboxes, *messages, *outboxes]
            if row.get("projectCode") == domain
        }),
    } for domain in domains]
    cycle_groups: dict[str, list[dict[str, Any]]] = {}
    for message in messages:
        cycle_id = str(message.get("rootCycleId") or message.get("cycleId"))
        cycle_groups.setdefault(cycle_id, []).append(message)
    cycles = []
    for cycle_id, cycle_messages in sorted(cycle_groups.items()):
        cycles.append({
            "legacyCycleId": cycle_id,
            "messageIds": [item["messageId"] for item in cycle_messages],
            "messageStates": sorted({item["status"] for item in cycle_messages}),
            "semanticState": "UNRESOLVED_REQUIRES_EXACT_AUTHORITY_EVIDENCE",
            "evidence": [item["evidence"] for item in cycle_messages],
        })
    projection_refs = []
    for delivery in deliveries:
        if any(delivery.get(name) for name in ("driveFileId", "driveUrl", "driveParent", "receiptReference")):
            projection_refs.append({"kind": "DRIVE_DELIVERY", "sourceId": delivery["deliveryId"],
                "driveFileId": delivery.get("driveFileId"), "driveUrl": delivery.get("driveUrl"), "driveParent": delivery.get("driveParent"),
                "verificationLevel": delivery.get("driveVerificationLevel"), "sha256": delivery.get("driveSha256"), "evidence": delivery["evidence"]})
    for binding in bindings:
        projection_refs.append({"kind": "BROWSER_BINDING", "sourceId": binding["mailboxId"], "threadId": binding["chatThreadId"],
                                "url": binding["chatUrl"], "evidence": binding["evidence"]})
    return {
        "metadata": _metadata(con),
        "domains": domains,
        "projects": projects,
        "endpoints": endpoints,
        "mailboxes": mailboxes,
        "messages": messages,
        "payloads": payloads,
        "browserOutboxes": outboxes,
        "browserBridgeReceipts": deliveries,
        "browserBindings": bindings,
        "browserPokes": pokes,
        "wakeAuthorities": wakes,
        "callerCapabilitiesWithoutSecrets": capabilities,
        "semanticCycles": cycles,
        "automaticReviews": reviews,
        "legacyExternalReferences": projection_refs,
        "eventHeaders": event_headers,
        "notNativeToLegacyKernel": {"tasks": [], "softwarePackages": [], "interfaces": [], "authorityGrants": [], "provisioningPlans": []},
    }


def _observer_inventory(con: sqlite3.Connection | None) -> dict[str, Any]:
    if con is None:
        return {"present": False, "threads": [], "eventSummary": {}}
    threads = _rows(con, "thread_state", order=["thread_id"])
    summary = {}
    if _table_exists(con, "events"):
        total, latest_id, latest_hash = con.execute("SELECT COUNT(*), COALESCE(MAX(id),0), COALESCE((SELECT event_hash FROM events ORDER BY id DESC LIMIT 1),'') FROM events").fetchone()
        summary = {"count": total, "latestId": latest_id, "latestHash": latest_hash}
    return {"present": True, "metadata": _metadata(con), "threads": threads, "eventSummary": summary}


def validated_external_evidence_kinds(external: Any, capture_root: Path | None = None) -> set[str]:
    if not isinstance(external, dict) or external.get("networkAccess") != "NONE" or not isinstance(external.get("records"), list):
        raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Capture external-evidence declaration is invalid", {})
    provided = external.get("provided")
    expected_outer = {"provided", "manifestSha256", "records", "networkAccess"}
    if provided is True:
        expected_outer.add("manifestPath")
        if not isinstance(external.get("manifestPath"), str) or not external["manifestPath"]:
            raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Capture external-evidence manifest path is invalid", {})
        if not isinstance(external.get("manifestSha256"), str) or not SHA256_PATTERN.fullmatch(external["manifestSha256"]):
            raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Capture external-evidence manifest hash is invalid", {})
        retained_manifest_fields = {"manifestCapturedPath", "manifestBytes"}
        retained_manifest_present = retained_manifest_fields.intersection(external)
        if retained_manifest_present:
            if retained_manifest_present != retained_manifest_fields:
                raise PostOfficeError(
                    "PON_SNAPSHOT_INTEGRITY_FAILURE",
                    "Retained external-evidence manifest custody is incomplete",
                    {},
                )
            if (
                not isinstance(external.get("manifestCapturedPath"), str)
                or not external["manifestCapturedPath"]
                or not isinstance(external.get("manifestBytes"), int)
                or isinstance(external.get("manifestBytes"), bool)
                or external["manifestBytes"] < 0
            ):
                raise PostOfficeError(
                    "PON_SNAPSHOT_INTEGRITY_FAILURE",
                    "Retained external-evidence manifest custody is invalid",
                    {},
                )
            if capture_root is not None:
                contained_member(capture_root, external["manifestCapturedPath"])
            expected_outer.update(retained_manifest_fields)
    elif provided is False:
        if external.get("manifestSha256") is not None or external.get("records"):
            raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Absent external evidence has custody fields", {})
    else:
        raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Capture external-evidence provided flag is invalid", {})
    if set(external) != expected_outer:
        raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Capture external-evidence shape is invalid", {})

    represented: set[str] = set()
    base_keys = {"kind", "logicalId", "relatedEntityType", "relatedEntityId", "reference"}
    for record in external["records"]:
        if not isinstance(record, dict) or not isinstance(record.get("logicalId"), str) or not record["logicalId"]:
            raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Capture external-evidence record is invalid", {"record": record})
        kind = record.get("kind")
        if kind in LOCAL_EXTERNAL_EVIDENCE_KINDS:
            required = base_keys | {"sourcePath", "capturedPath", "bytes", "sha256"}
            permitted = required | {"sourceRoot"}
            if not required.issubset(record) or not set(record).issubset(permitted):
                raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Local migration evidence custody is incomplete", {"record": record})
            source_path = Path(str(record.get("sourcePath", "")))
            source_root = record.get("sourceRoot")
            if (
                not isinstance(record.get("sourcePath"), str)
                or not record["sourcePath"]
                or ("sourceRoot" in record and (source_path.is_absolute() or ".." in source_path.parts))
                or ("sourceRoot" in record and (not isinstance(source_root, str) or not source_root))
                or not isinstance(record.get("capturedPath"), str)
                or not record["capturedPath"]
                or not isinstance(record.get("bytes"), int)
                or isinstance(record.get("bytes"), bool)
                or record["bytes"] < 0
                or not isinstance(record.get("sha256"), str)
                or not SHA256_PATTERN.fullmatch(record["sha256"])
            ):
                raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Local migration evidence custody is invalid", {"record": record})
            if capture_root is not None:
                contained_member(capture_root, record["capturedPath"])
            represented.add(str(kind))
        elif kind in REMOTE_EXTERNAL_EVIDENCE_KINDS:
            permitted = base_keys | {"sha256"}
            if not base_keys.issubset(record) or not set(record).issubset(permitted) or not isinstance(record.get("reference"), str) or not record["reference"]:
                raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Remote migration evidence reference is invalid", {"record": record})
            if "sha256" in record and (not isinstance(record["sha256"], str) or not SHA256_PATTERN.fullmatch(record["sha256"])):
                raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Remote migration evidence hash is invalid", {"record": record})
            represented.add(str(kind))
        else:
            raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Migration evidence kind is unsupported", {"record": record})
    return represented


def _validate_capture_manifest(capture_root: Path, manifest: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    required_keys = {
        "schemaVersion", "sourceStateRoot", "legacySchemaVersions", "databases", "files",
        "externalEvidence", "excluded", "fileMerkleRoot", "captureRoot", "sourceAccess",
    }
    if not isinstance(manifest, dict) or set(manifest) != required_keys or manifest.get("schemaVersion") != "1":
        raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Capture manifest shape is invalid", {})
    if manifest.get("sourceAccess") != "SQLITE_MODE_RO_QUERY_ONLY_AND_FILESYSTEM_READ_ONLY":
        raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Capture source-access declaration is invalid", {})
    if not isinstance(manifest.get("databases"), list) or not isinstance(manifest.get("files"), list):
        raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Capture manifest collections are invalid", {})
    external = manifest.get("externalEvidence")
    validated_external_evidence_kinds(external, capture_root)

    identity_keys = [
        "schemaVersion", "sourceStateRoot", "legacySchemaVersions", "databases", "files", "externalEvidence", "excluded"
    ]
    identity = {key: manifest[key] for key in identity_keys}
    if manifest["captureRoot"] != sha256_json(identity):
        raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Capture root does not match capture identity", {})
    if manifest["fileMerkleRoot"] != merkle_root(manifest["files"]):
        raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Capture file Merkle root does not match inventory", {})
    if (capture_root / "CAPTURE_INCOMPLETE").exists():
        raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Capture is marked incomplete", {})

    expected_members = {"capture-manifest.json"}
    seen_database_names: set[str] = set()
    expected_database_paths = {"hub": "hub.sqlite3", "observer": "observer.sqlite3"}
    for database in manifest["databases"]:
        if not isinstance(database, dict) or set(database) != {
            "name", "path", "bytes", "sha256", "logicalContentsRoot", "eventBoundary",
            "sourceDataVersionBefore", "sourceDataVersionAfter",
        }:
            raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Captured database record is invalid", {"database": database})
        name = database.get("name")
        if name not in expected_database_paths or database.get("path") != expected_database_paths[name] or name in seen_database_names:
            raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Captured database identity is invalid", {"database": database})
        if (
            not isinstance(database.get("bytes"), int)
            or isinstance(database.get("bytes"), bool)
            or database["bytes"] < 0
            or not isinstance(database.get("sha256"), str)
            or not SHA256_PATTERN.fullmatch(database["sha256"])
            or not isinstance(database.get("logicalContentsRoot"), str)
            or not SHA256_PATTERN.fullmatch(database["logicalContentsRoot"])
            or not isinstance(database.get("sourceDataVersionBefore"), int)
            or not isinstance(database.get("sourceDataVersionAfter"), int)
            or database["sourceDataVersionBefore"] != database["sourceDataVersionAfter"]
        ):
            raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Captured database boundary is invalid", {"database": database})
        seen_database_names.add(name)
        expected_members.add(database["path"])
    if "hub" not in seen_database_names:
        raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Capture is missing the hub database", {})

    inventory_paths: set[str] = set()
    for entry in manifest["files"]:
        if not isinstance(entry, dict) or set(entry) != {"path", "bytes", "modifiedNs", "sha256"}:
            raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Capture inventory entry is invalid", {"entry": entry})
        contained_member(capture_root, entry["path"])
        if entry["path"] in inventory_paths:
            raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Capture inventory contains a duplicate path", {"path": entry["path"]})
        inventory_paths.add(entry["path"])

    for record in external["records"]:
        captured_path = record.get("capturedPath")
        if captured_path:
            contained_member(capture_root, captured_path)
            if captured_path in expected_members:
                raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Capture member path is duplicated", {"path": captured_path})
            expected_members.add(captured_path)
    retained_manifest_path = external.get("manifestCapturedPath")
    if retained_manifest_path:
        contained_member(capture_root, retained_manifest_path)
        if retained_manifest_path in expected_members:
            raise PostOfficeError(
                "PON_SNAPSHOT_INTEGRITY_FAILURE",
                "Retained external-evidence manifest path is duplicated",
                {"path": retained_manifest_path},
            )
        expected_members.add(retained_manifest_path)

    for path in capture_root.rglob("*"):
        if os.path.lexists(path) and is_link_like(path):
            raise PostOfficeError(
                "PON_SNAPSHOT_INTEGRITY_FAILURE",
                "Capture contains a linked member",
                {"path": path.relative_to(capture_root).as_posix()},
            )
    actual_members = {
        path.relative_to(capture_root).as_posix()
        for path in capture_root.rglob("*")
        if path.is_file()
    }
    if actual_members != expected_members:
        raise PostOfficeError(
            "PON_SNAPSHOT_INTEGRITY_FAILURE",
            "Capture member set does not match its manifest",
            {"missing": sorted(expected_members - actual_members), "extra": sorted(actual_members - expected_members)},
        )
    return manifest["databases"], external


def create_snapshot(capture_root: Path, output: Path) -> dict[str, Any]:
    capture_root = capture_root.resolve()
    manifest_path = contained_member(capture_root, "capture-manifest.json")
    if not manifest_path.is_file():
        raise PostOfficeError("PON_PATH_NOT_FOUND", "Capture manifest was not found", {"path": str(manifest_path)})
    output = require_new_output_file(output, protected_roots=[capture_root])
    manifest = read_json(manifest_path)
    databases, external_evidence = _validate_capture_manifest(capture_root, manifest)
    for database in databases:
        path = contained_member(capture_root, database["path"])
        if not path.is_file() or path.stat().st_size != database["bytes"] or sha256_file(path) != database["sha256"]:
            raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Captured database does not match its manifest", {"database": database})
        con = readonly_connection(path)
        try:
            con.execute("BEGIN")
            content_identity = sqlite_content_identity(con)
            con.rollback()
        finally:
            if con.in_transaction:
                con.rollback()
            con.close()
        if content_identity["contentsRoot"] != database["logicalContentsRoot"] or content_identity["eventBoundary"] != database["eventBoundary"]:
            raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Captured database boundary does not match its manifest", {"database": database})
    for record in external_evidence.get("records", []):
        captured_path = record.get("capturedPath")
        if not captured_path:
            continue
        path = contained_member(capture_root, captured_path)
        if not path.is_file() or path.stat().st_size != record["bytes"] or sha256_file(path) != record["sha256"]:
            raise PostOfficeError(
                "PON_SNAPSHOT_INTEGRITY_FAILURE",
                "Captured external migration evidence does not match its manifest",
                {"record": record},
            )
    retained_manifest_path = external_evidence.get("manifestCapturedPath")
    if retained_manifest_path:
        path = contained_member(capture_root, retained_manifest_path)
        if (
            not path.is_file()
            or path.stat().st_size != external_evidence["manifestBytes"]
            or sha256_file(path) != external_evidence["manifestSha256"]
        ):
            raise PostOfficeError(
                "PON_SNAPSHOT_INTEGRITY_FAILURE",
                "Retained external-evidence manifest does not match its custody record",
                {"path": retained_manifest_path},
            )
    hub = readonly_connection(capture_root / "hub.sqlite3")
    observer_path = capture_root / "observer.sqlite3"
    observer = readonly_connection(observer_path) if observer_path.exists() else None
    try:
        hub_meta = _metadata(hub)
        observer_meta = _metadata(observer) if observer else {}
        versions = {
            "hub": int(hub_meta.get("schema_version", -1)),
            "autoReview": int(hub_meta.get("auto_review_schema_version", -1)),
            "observer": int(observer_meta.get("schema_version", -1)) if observer else None,
        }
        expected = {"hub": HUB_SCHEMA_VERSION, "autoReview": AUTO_REVIEW_SCHEMA_VERSION, "observer": OBSERVER_SCHEMA_VERSION}
        mismatch = {name: {"actual": value, "expected": expected[name]} for name, value in versions.items() if value is not None and value != expected[name]}
        if mismatch:
            raise PostOfficeError("PON_LEGACY_SCHEMA_UNSUPPORTED", "Captured legacy schema is unsupported", {"mismatches": mismatch})
        identity = {
            "schemaVersion": "1",
            "captureRoot": manifest["captureRoot"],
            "legacySchemaVersions": versions,
            "sourceAccess": "CAPTURED_COPY_READ_ONLY",
            "databaseCopies": manifest["databases"],
            "filesystem": {"files": manifest["files"], "fileMerkleRoot": manifest["fileMerkleRoot"], "excluded": manifest["excluded"]},
            "migrationEvidence": external_evidence,
            "hub": _hub_inventory(hub),
            "browserObserver": _observer_inventory(observer),
        }
    finally:
        hub.close()
        if observer:
            observer.close()
    snapshot = {**identity, "snapshotRoot": sha256_json(identity)}
    write_json(output, snapshot)
    return {"ok": True, "snapshotRoot": snapshot["snapshotRoot"], "output": str(output.resolve()),
            "counts": {"domains": len(snapshot["hub"]["domains"]), "mailboxes": len(snapshot["hub"]["mailboxes"]),
                       "messages": len(snapshot["hub"]["messages"]), "payloads": len(snapshot["hub"]["payloads"]),
                        "cycles": len(snapshot["hub"]["semanticCycles"]), "automaticReviews": len(snapshot["hub"]["automaticReviews"]),
                        "migrationEvidence": len(snapshot["migrationEvidence"]["records"])}}
