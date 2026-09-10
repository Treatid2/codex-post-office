# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import hmac
import json
import os
import secrets
import sqlite3
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .canonical import (
    canonical_json_bytes,
    publish_file_exclusive,
    require_new_output_file,
    require_outside_protected_roots,
    sha256_bytes,
    sha256_json,
    sqlite_content_identity,
)
from .database import inspect_database, validate_operational_connection
from .diagnostics import PostOfficeError
from .mini_schema import ValidationFailure, validate


IMPLEMENTED_OPERATIONS = frozenset({"hub.status"})
KERNEL_INSTANCE_ID = "PON-KERNEL"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PostOfficeError(
            "PON_INPUT_INVALID", f"{label} is not readable canonical JSON", {"path": str(path)}
        ) from exc
    if not isinstance(value, dict):
        raise PostOfficeError("PON_INPUT_INVALID", f"{label} must be a JSON object", {"path": str(path)})
    return value


def _contract_root(plugin_root: Path) -> str:
    manifest = _read_json(plugin_root / "contracts" / "v1" / "manifest.json", label="Contract manifest")
    root = manifest.get("contractRoot")
    if not isinstance(root, str) or len(root) != 64:
        raise PostOfficeError("PON_CONTRACT_INVALID", "Contract manifest root is invalid", {})
    return root


def _open_writer(database_path: Path, plugin_root: Path) -> sqlite3.Connection:
    database_path = require_outside_protected_roots(database_path.resolve(strict=True))
    con = sqlite3.connect(database_path.as_uri() + "?mode=rw", uri=True, timeout=30)
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("BEGIN IMMEDIATE")
        validate_operational_connection(con, plugin_root)
        return con
    except Exception:
        if con.in_transaction:
            con.rollback()
        con.close()
        raise


def _credential(path: Path) -> dict[str, str]:
    value = _read_json(path.resolve(strict=True), label="Kernel credential")
    if set(value) != {"schemaVersion", "capabilityId", "secret"} or value.get("schemaVersion") != "1":
        raise PostOfficeError("PON_CREDENTIAL_INVALID", "Kernel credential has an invalid shape", {})
    capability_id = value.get("capabilityId")
    secret = value.get("secret")
    if not isinstance(capability_id, str) or len(capability_id) < 3:
        raise PostOfficeError("PON_CREDENTIAL_INVALID", "Kernel capability ID is invalid", {})
    if not isinstance(secret, str) or len(secret) < 43:
        raise PostOfficeError("PON_CREDENTIAL_INVALID", "Kernel credential secret is invalid", {})
    return {"capabilityId": capability_id, "secret": secret}


def create_kernel_credential(output_path: Path, capability_id: str) -> dict[str, Any]:
    output_path = require_new_output_file(output_path)
    if len(capability_id) < 3:
        raise PostOfficeError("PON_INPUT_INVALID", "Capability ID is too short", {})
    value = {
        "schemaVersion": "1",
        "capabilityId": capability_id,
        "secret": secrets.token_urlsafe(48),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    staged = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(staged, flags, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(staged, stat.S_IRUSR | stat.S_IWUSR)
        publish_file_exclusive(staged, output_path)
    finally:
        if os.path.lexists(staged):
            staged.unlink()
    return {
        "ok": True,
        "created": True,
        "credential": str(output_path),
        "capabilityId": capability_id,
        "secretSha256": sha256_bytes(value["secret"].encode("utf-8")),
    }


def bootstrap_kernel(
    database_path: Path,
    credential_path: Path,
    *,
    actor_id: str,
    actor_kind: str,
    actor_role: str,
    mode: str,
    plugin_root: Path,
) -> dict[str, Any]:
    if mode not in {"ISOLATED", "SHADOW"}:
        raise PostOfficeError(
            "PON_PRODUCTION_MUTATION_FORBIDDEN",
            "P3.1 supports only ISOLATED or non-authoritative SHADOW mode",
            {"mode": mode},
        )
    credential = _credential(credential_path)
    secret_hash = sha256_bytes(credential["secret"].encode("utf-8"))
    contract_root = _contract_root(plugin_root)
    now = _timestamp()
    con = _open_writer(database_path, plugin_root)
    try:
        retained = con.execute(
            "SELECT * FROM kernel_instances WHERE instance_id=?", (KERNEL_INSTANCE_ID,)
        ).fetchone()
        if retained:
            actor = con.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
            capability = con.execute(
                "SELECT * FROM caller_capabilities WHERE capability_id=?",
                (credential["capabilityId"],),
            ).fetchone()
            exact = bool(
                retained["mode"] == mode
                and retained["status"] == "READY"
                and retained["contract_root"] == contract_root
                and retained["bootstrap_actor_id"] == actor_id
                and retained["bootstrap_capability_id"] == credential["capabilityId"]
                and actor
                and actor["actor_kind"] == actor_kind
                and actor["role"] == actor_role
                and actor["status"] == "ACTIVE"
                and capability
                and capability["actor_id"] == actor_id
                and capability["secret_sha256"] == secret_hash
                and json.loads(capability["allowed_operations_json"]) == sorted(IMPLEMENTED_OPERATIONS)
                and capability["status"] == "ACTIVE"
            )
            if not exact:
                raise PostOfficeError(
                    "PON_KERNEL_ALREADY_BOOTSTRAPPED",
                    "Kernel bootstrap replay differs from the retained instance",
                    {"instanceId": KERNEL_INSTANCE_ID},
                )
            con.rollback()
            inspection = inspect_database(database_path, plugin_root)
            return {
                "ok": True,
                "created": False,
                "instanceId": KERNEL_INSTANCE_ID,
                "mode": mode,
                "status": "READY",
                "actorId": actor_id,
                "capabilityId": credential["capabilityId"],
                "contractRoot": contract_root,
                "logicalStateRoot": inspection["logicalStateRoot"],
            }

        actor = con.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if actor and (actor["actor_kind"] != actor_kind or actor["role"] != actor_role or actor["status"] != "ACTIVE"):
            raise PostOfficeError(
                "PON_KERNEL_ALREADY_BOOTSTRAPPED",
                "Bootstrap actor identity collides with different retained state",
                {"actorId": actor_id},
            )
        if not actor:
            con.execute(
                "INSERT INTO actors(actor_id,actor_kind,role,status,created_at) VALUES(?,?,?,?,?)",
                (actor_id, actor_kind, actor_role, "ACTIVE", now),
            )
        if con.execute(
            "SELECT 1 FROM caller_capabilities WHERE capability_id=?",
            (credential["capabilityId"],),
        ).fetchone():
            raise PostOfficeError(
                "PON_KERNEL_ALREADY_BOOTSTRAPPED",
                "Bootstrap capability ID is already in use",
                {"capabilityId": credential["capabilityId"]},
            )
        con.execute(
            """INSERT INTO caller_capabilities(
               capability_id,actor_id,subject_kind,subject_id,subject_generation,
               allowed_operations_json,secret_sha256,status,expires_at,created_at,revoked_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                credential["capabilityId"], actor_id, "actor", actor_id, None,
                json.dumps(sorted(IMPLEMENTED_OPERATIONS), separators=(",", ":")),
                secret_hash, "ACTIVE", None, now, None,
            ),
        )
        con.execute(
            """INSERT INTO kernel_instances(
               instance_id,mode,status,contract_root,bootstrap_actor_id,
               bootstrap_capability_id,bootstrapped_at)
               VALUES(?,?,?,?,?,?,?)""",
            (
                KERNEL_INSTANCE_ID, mode, "READY", contract_root, actor_id,
                credential["capabilityId"], now,
            ),
        )
        con.commit()
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()
    inspection = inspect_database(database_path, plugin_root)
    return {
        "ok": True,
        "created": True,
        "instanceId": KERNEL_INSTANCE_ID,
        "mode": mode,
        "status": "READY",
        "actorId": actor_id,
        "capabilityId": credential["capabilityId"],
        "contractRoot": contract_root,
        "logicalStateRoot": inspection["logicalStateRoot"],
    }


def inspect_kernel(database_path: Path, plugin_root: Path) -> dict[str, Any]:
    inspection = inspect_database(database_path, plugin_root)
    con = sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT * FROM kernel_instances WHERE instance_id=?", (KERNEL_INSTANCE_ID,)
        ).fetchone()
        if not row:
            raise PostOfficeError("PON_KERNEL_NOT_BOOTSTRAPPED", "Operational kernel is not bootstrapped", {})
        return {
            "ok": True,
            "instanceId": row["instance_id"],
            "mode": row["mode"],
            "status": row["status"],
            "contractRoot": row["contract_root"],
            "bootstrapActorId": row["bootstrap_actor_id"],
            "bootstrapCapabilityId": row["bootstrap_capability_id"],
            "bootstrappedAt": row["bootstrapped_at"],
            "implementedOperations": sorted(IMPLEMENTED_OPERATIONS),
            "database": inspection["database"],
            "logicalStateRoot": inspection["logicalStateRoot"],
        }
    finally:
        con.close()


def _authenticate(
    con: sqlite3.Connection,
    request: dict[str, Any],
    credential: dict[str, str],
) -> sqlite3.Row:
    capability_id = request["authority"]["capabilityId"]
    if capability_id != credential["capabilityId"]:
        raise PostOfficeError("PON_AUTHENTICATION_FAILED", "Credential does not match request capability", {})
    capability = con.execute(
        "SELECT * FROM caller_capabilities WHERE capability_id=?", (capability_id,)
    ).fetchone()
    supplied_hash = sha256_bytes(credential["secret"].encode("utf-8"))
    if not capability or not hmac.compare_digest(capability["secret_sha256"], supplied_hash):
        raise PostOfficeError("PON_AUTHENTICATION_FAILED", "Capability authentication failed", {})
    if capability["status"] != "ACTIVE":
        raise PostOfficeError("PON_AUTHORIZATION_DENIED", "Capability is not active", {})
    expires_at = capability["expires_at"]
    if expires_at and datetime.fromisoformat(expires_at.replace("Z", "+00:00")) <= datetime.now(timezone.utc):
        raise PostOfficeError("PON_AUTHORIZATION_DENIED", "Capability has expired", {})
    actor = con.execute("SELECT * FROM actors WHERE actor_id=?", (capability["actor_id"],)).fetchone()
    requested_actor = request["actor"]
    if (
        not actor
        or actor["status"] != "ACTIVE"
        or requested_actor["id"] != actor["actor_id"]
        or requested_actor["kind"] != actor["actor_kind"]
        or requested_actor["role"] != actor["role"]
    ):
        raise PostOfficeError("PON_AUTHENTICATION_FAILED", "Request actor does not match capability actor", {})
    allowed = set(json.loads(capability["allowed_operations_json"]))
    if request["operation"] not in allowed:
        raise PostOfficeError("PON_AUTHORIZATION_DENIED", "Capability does not allow this operation", {})
    return capability


def _operational_state(con: sqlite3.Connection) -> dict[str, Any]:
    identity = sqlite_content_identity(con, excluded_tables={"idempotency_records"})
    return {
        "operationalStateRoot": identity["contentsRoot"],
        "eventBoundary": identity["eventBoundary"],
    }


def _status_result(
    con: sqlite3.Connection,
    request: dict[str, Any],
    request_hash: str,
    recorded_at: str,
) -> dict[str, Any]:
    kernel = con.execute(
        "SELECT * FROM kernel_instances WHERE instance_id=?", (KERNEL_INSTANCE_ID,)
    ).fetchone()
    if not kernel or kernel["status"] != "READY":
        raise PostOfficeError("PON_KERNEL_NOT_BOOTSTRAPPED", "Operational kernel is not ready", {})
    state = _operational_state(con)
    root = state["operationalStateRoot"]
    event_count = int(con.execute("SELECT COUNT(*) FROM hub_events").fetchone()[0])
    idempotency_count = int(con.execute("SELECT COUNT(*) FROM idempotency_records").fetchone()[0])
    last_event_id = state["eventBoundary"]["eventId"] if state["eventBoundary"] else None
    result: dict[str, Any] = {
        "schemaVersion": "1",
        "requestId": request["requestId"],
        "operation": request["operation"],
        "ok": True,
        "beforeRoot": root,
        "afterRoot": root,
        "aggregateVersion": 0,
        "createdIds": [],
        "warnings": [],
        "status": {
            "instanceId": kernel["instance_id"],
            "mode": kernel["mode"],
            "status": kernel["status"],
            "contractRoot": kernel["contract_root"],
            "databaseUserVersion": int(con.execute("PRAGMA user_version").fetchone()[0]),
            "eventCount": event_count,
            "lastEventId": last_event_id,
            "idempotencyRecordCountBeforeRequest": idempotency_count,
            "implementedOperations": sorted(IMPLEMENTED_OPERATIONS),
        },
        "receipt": {
            "receiptId": "PON-RECEIPT-" + uuid.uuid4().hex,
            "requestSha256": request_hash,
            "recordedAt": recorded_at,
            "stateRoot": root,
        },
    }
    return result


def _validate_contract(
    plugin_root: Path,
    operation: str,
    kind: str,
    value: dict[str, Any],
) -> None:
    schema_path = plugin_root / "contracts" / "v1" / "operations" / kind / f"{operation}.schema.json"
    if not schema_path.is_file():
        raise PostOfficeError("PON_CONTRACT_INVALID", "Operation contract is missing", {"operation": operation})
    schema = _read_json(schema_path, label="Operation contract")
    try:
        validate(value, schema)
    except ValidationFailure as exc:
        raise PostOfficeError(
            "PON_CONTRACT_INVALID",
            f"Operation {kind[:-1]} does not satisfy its contract",
            {"operation": operation, "validationError": str(exc)},
        ) from exc


def execute_operation(
    database_path: Path,
    request_path: Path,
    credential_path: Path,
    plugin_root: Path,
) -> dict[str, Any]:
    database_path = database_path.resolve(strict=True)
    request = _read_json(request_path.resolve(strict=True), label="Operation request")
    operation = request.get("operation")
    if not isinstance(operation, str):
        raise PostOfficeError("PON_CONTRACT_INVALID", "Operation request has no operation", {})
    _validate_contract(plugin_root, operation, "requests", request)
    credential = _credential(credential_path)
    request_hash = sha256_json(request)
    con = _open_writer(database_path, plugin_root)
    try:
        kernel = con.execute(
            "SELECT * FROM kernel_instances WHERE instance_id=?", (KERNEL_INSTANCE_ID,)
        ).fetchone()
        if not kernel or kernel["status"] != "READY":
            raise PostOfficeError("PON_KERNEL_NOT_BOOTSTRAPPED", "Operational kernel is not ready", {})
        if kernel["mode"] not in {"ISOLATED", "SHADOW"}:
            raise PostOfficeError(
                "PON_PRODUCTION_MUTATION_FORBIDDEN", "Kernel mode is not permitted in P3.1", {}
            )
        _authenticate(con, request, credential)
        retained = con.execute(
            "SELECT * FROM idempotency_records WHERE request_id=?", (request["requestId"],)
        ).fetchone()
        if retained:
            if retained["operation"] != operation or retained["canonical_request_sha256"] != request_hash:
                raise PostOfficeError(
                    "PON_IDEMPOTENCY_CONFLICT",
                    "Request ID replay differs from the retained canonical request",
                    {"requestId": request["requestId"]},
                )
            result = json.loads(retained["result_json"])
            _validate_contract(plugin_root, operation, "results", result)
            con.rollback()
            return result
        if operation not in IMPLEMENTED_OPERATIONS:
            raise PostOfficeError(
                "PON_OPERATION_NOT_IMPLEMENTED",
                "Operation is contracted but not implemented in P3.1",
                {"operation": operation},
            )
        recorded_at = _timestamp()
        if operation == "hub.status":
            result = _status_result(con, request, request_hash, recorded_at)
        else:  # pragma: no cover - guarded by IMPLEMENTED_OPERATIONS
            raise PostOfficeError("PON_OPERATION_NOT_IMPLEMENTED", "Operation is not implemented", {})
        _validate_contract(plugin_root, operation, "results", result)
        con.execute(
            """INSERT INTO idempotency_records(
               request_id,operation,canonical_request_sha256,result_json,event_id,recorded_at)
               VALUES(?,?,?,?,?,?)""",
            (
                request["requestId"], operation, request_hash,
                canonical_json_bytes(result).decode("utf-8"), None, recorded_at,
            ),
        )
        con.commit()
        return result
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()


def assert_aggregate_concurrency(
    aggregate: dict[str, Any],
    *,
    actual_type: str,
    actual_id: str,
    actual_version: int,
    actual_root: str,
) -> None:
    """Shared compare-and-swap gate for later P3 mutation handlers."""
    expected_root = str(aggregate.get("expectedRoot", "")).removeprefix("sha256:")
    if aggregate.get("type") != actual_type or aggregate.get("id") != actual_id:
        raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Aggregate identity differs", {})
    if "expectedVersion" in aggregate and aggregate["expectedVersion"] != actual_version:
        raise PostOfficeError(
            "PON_CONCURRENCY_CONFLICT", "Aggregate version differs",
            {"expectedVersion": aggregate["expectedVersion"], "actualVersion": actual_version},
        )
    if expected_root and expected_root != actual_root.removeprefix("sha256:"):
        raise PostOfficeError(
            "PON_CONCURRENCY_CONFLICT", "Aggregate root differs",
            {"expectedRoot": expected_root, "actualRoot": actual_root.removeprefix("sha256:")},
        )


def append_hub_event(
    con: sqlite3.Connection,
    *,
    request: dict[str, Any],
    capability_id: str,
    aggregate_version: int,
    before_root: str,
    after_root: str,
    event_result: dict[str, Any],
    occurred_at: str | None = None,
) -> dict[str, Any]:
    """Append one canonical event inside the caller's existing write transaction."""
    authority = request["authority"]
    grant_id = authority.get("authorityGrantId")
    action_id = authority.get("exactAuthorActionId")
    if bool(grant_id) == bool(action_id):
        raise PostOfficeError("PON_AUTHORIZATION_DENIED", "Mutation requires exactly one authority basis", {})
    previous = con.execute(
        "SELECT event_sha256 FROM hub_events ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    sequence = int(con.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM hub_events").fetchone()[0])
    event_id = "PON-EVENT-" + uuid.uuid4().hex
    timestamp = occurred_at or _timestamp()
    payload_hash = sha256_json(request)
    event_body = {
        "sequence": sequence,
        "eventId": event_id,
        "occurredAt": timestamp,
        "actorId": request["actor"]["id"],
        "operation": request["operation"],
        "capabilityId": capability_id,
        "authorityGrantId": grant_id,
        "exactAuthorActionId": action_id,
        "aggregateType": request["aggregate"]["type"],
        "aggregateId": request["aggregate"]["id"],
        "aggregateVersion": aggregate_version,
        "beforeStateRoot": before_root.removeprefix("sha256:"),
        "afterStateRoot": after_root.removeprefix("sha256:"),
        "result": event_result,
        "payloadSha256": payload_hash,
        "previousEventSha256": str(previous[0]) if previous else None,
    }
    event_hash = sha256_json(event_body)
    con.execute(
        """INSERT INTO hub_events(
           sequence,event_id,occurred_at,actor_id,operation,capability_id,
           authority_grant_id,exact_author_action_id,aggregate_type,aggregate_id,
           aggregate_version,before_state_root,after_state_root,result_json,payload_sha256,
           previous_event_sha256,event_sha256)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            sequence, event_id, timestamp, request["actor"]["id"], request["operation"],
            capability_id, grant_id, action_id, request["aggregate"]["type"],
            request["aggregate"]["id"], aggregate_version,
            event_body["beforeStateRoot"], event_body["afterStateRoot"],
            canonical_json_bytes(event_result).decode("utf-8"), payload_hash,
            event_body["previousEventSha256"], event_hash,
        ),
    )
    return {"eventId": event_id, "eventSha256": event_hash, "sequence": sequence}
