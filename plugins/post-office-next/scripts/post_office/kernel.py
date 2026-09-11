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
from .contracts import validate_contracts
from .database import inspect_database, validate_operational_connection
from .diagnostics import PostOfficeError
from .mini_schema import ValidationFailure, validate


READ_OPERATIONS = frozenset(
    {
        "hub.status", "authority.inspect", "project.read", "task.read", "provisioning.inspect",
        "package.read", "package.rehome.preview", "interface.read", "capabilityRequest.read",
        "bundle.verify",
        "transport.inspect", "attention.list", "hub.snapshot",
    }
)
AUTHOR_MUTATION_OPERATIONS = frozenset(
    {
        "authority.grant",
        "authority.revoke",
        "endpoint.allocate",
        "endpoint.revoke",
        "mailbox.allocate",
        "mailbox.rotateGeneration",
        "project.planCreate",
        "project.create",
        "project.update",
        "project.pause",
        "project.archive",
        "task.planCreate",
        "task.create",
        "task.bindEndpoint",
        "task.activate",
        "task.block",
        "task.moveProject",
        "task.recordResponse",
        "task.review",
        "task.close",
        "package.register", "package.registerVersion", "package.rehome", "package.deprecate",
        "interface.register", "interface.deprecate", "capabilityRequest.create",
        "capabilityRequest.triage", "capabilityRequest.fulfil", "changeSet.create",
        "changeSet.addTask", "changeSet.startIntegration", "changeSet.decide",
        "integration.record", "contextBundle.build", "message.plan", "message.register",
        "message.route", "message.acknowledge", "message.review", "message.close",
        "bundle.supersedeBeforeRegistration", "cycle.open", "cycle.markAwaitingReview",
        "cycle.accept", "cycle.close",
        "transport.retry", "transport.quarantine", "transport.tombstoneDuplicate",
        "hub.reconcile.preview", "hub.reconcile.apply",
    }
)
IMPLEMENTED_OPERATIONS = READ_OPERATIONS | AUTHOR_MUTATION_OPERATIONS
KERNEL_INSTANCE_ID = "PON-KERNEL"


def _bootstrap_operations(actor_role: str) -> list[str]:
    if actor_role == "author":
        return sorted(IMPLEMENTED_OPERATIONS)
    return sorted(READ_OPERATIONS)


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
    validation = validate_contracts(plugin_root)
    return str(validation["contractRoot"])


def _open_writer(
    database_path: Path, plugin_root: Path, *, allow_prepared_cutover: bool = False
) -> sqlite3.Connection:
    database_path = require_outside_protected_roots(database_path.resolve(strict=True))
    con = sqlite3.connect(database_path.as_uri() + "?mode=rw", uri=True, timeout=30)
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("BEGIN IMMEDIATE")
        validate_operational_connection(con, plugin_root)
        _validate_hub_event_chain(con)
        _validate_kernel_projections(con)
        if not allow_prepared_cutover and con.execute(
            "SELECT 1 FROM authority_transfers WHERE state='PREPARED' LIMIT 1"
        ).fetchone():
            raise PostOfficeError(
                "PON_CONCURRENCY_CONFLICT",
                "A prepared authority transfer fences all ordinary writes until it is finished or rolled back",
                {"reason": "PREPARED_CUTOVER_FENCE"},
            )
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


def bind_kernel_credential(
    database_path: Path,
    author_credential_path: Path,
    output_path: Path,
    *,
    action_id: str,
    actor_id: str,
    capability_id: str,
    subject_kind: str,
    subject_id: str,
    subject_generation: int | None,
    allowed_operations: list[str],
    expires_at: str | None,
    plugin_root: Path,
) -> dict[str, Any]:
    """Bind a new secret-bearing caller capability to an existing actor.

    The secret is written only to the new operator-selected file and is never included in the
    returned receipt. A failed database transaction removes only the file created by this call.
    """
    if not action_id or not actor_id or not capability_id or not subject_kind or not subject_id:
        raise PostOfficeError("PON_INPUT_INVALID", "Credential binding identifiers are required", {})
    if not allowed_operations or len(set(allowed_operations)) != len(allowed_operations):
        raise PostOfficeError("PON_INPUT_INVALID", "Allowed operations must be a non-empty unique list", {})
    unsupported = sorted(set(allowed_operations) - IMPLEMENTED_OPERATIONS)
    if unsupported:
        raise PostOfficeError("PON_AUTHORIZATION_DENIED", "Credential cannot allow unavailable operations", {"operations": unsupported})
    if subject_generation is not None and subject_generation < 1:
        raise PostOfficeError("PON_INPUT_INVALID", "Subject generation must be positive", {})
    if expires_at and _parse_timestamp(expires_at) <= datetime.now(timezone.utc):
        raise PostOfficeError("PON_INPUT_INVALID", "Credential expiry must be in the future", {})
    output_path = require_new_output_file(output_path)
    author_credential = _credential(author_credential_path)
    con = _open_writer(database_path, plugin_root)
    created_output = False
    try:
        author_capability = con.execute(
            "SELECT * FROM caller_capabilities WHERE capability_id=?", (author_credential["capabilityId"],)
        ).fetchone()
        author = con.execute(
            "SELECT * FROM actors WHERE actor_id=?", (author_capability["actor_id"],)
        ).fetchone() if author_capability else None
        if (
            not author_capability or author_capability["status"] != "ACTIVE"
            or not hmac.compare_digest(
                str(author_capability["secret_sha256"]),
                sha256_bytes(author_credential["secret"].encode("utf-8")),
            )
            or (
                author_capability["expires_at"]
                and _parse_timestamp(author_capability["expires_at"]) <= datetime.now(timezone.utc)
            )
            or not author or author["actor_kind"] != "HUMAN" or author["role"] != "author"
            or author["status"] != "ACTIVE"
        ):
            raise PostOfficeError("PON_AUTHORIZATION_DENIED", "Credential binding requires the authenticated human author", {})
        actor = con.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if not actor or actor["status"] != "ACTIVE":
            raise PostOfficeError("PON_INPUT_INVALID", "Credential target must be an existing active actor", {})
        if con.execute("SELECT 1 FROM caller_capabilities WHERE capability_id=?", (capability_id,)).fetchone():
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Caller capability already exists", {})
        plan = {
            "actionId": action_id, "actorId": actor_id, "capabilityId": capability_id,
            "subjectKind": subject_kind, "subjectId": subject_id,
            "subjectGeneration": subject_generation, "allowedOperations": sorted(allowed_operations),
            "expiresAt": expires_at,
        }
        confirmation = sha256_json(plan)
        if con.execute("SELECT 1 FROM exact_author_actions WHERE action_id=?", (action_id,)).fetchone():
            raise PostOfficeError("PON_AUTHORIZATION_DENIED", "Credential-binding author action already exists", {})
        created = create_kernel_credential(output_path, capability_id)
        created_output = True
        new_credential = _credential(output_path)
        now = _timestamp()
        before_root = _aggregate_root("CallerCapability", capability_id, None)
        after_state = {**plan, "status": "ACTIVE", "secretSha256": created["secretSha256"]}
        after_root = _aggregate_root("CallerCapability", capability_id, after_state)
        con.execute(
            "INSERT INTO exact_author_actions(action_id,actor_id,operation,scope_json,confirmation_sha256,recorded_at) VALUES(?,?,?,?,?,?)",
            (action_id, author["actor_id"], "kernel.credentialBind", canonical_json_bytes(plan).decode("utf-8"), confirmation.removeprefix("sha256:"), now),
        )
        con.execute(
            """INSERT INTO caller_capabilities(capability_id,actor_id,subject_kind,subject_id,
               subject_generation,allowed_operations_json,secret_sha256,status,expires_at,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (capability_id, actor_id, subject_kind, subject_id, subject_generation,
             canonical_json_bytes(sorted(allowed_operations)).decode("utf-8"),
             sha256_bytes(new_credential["secret"].encode("utf-8")), "ACTIVE", expires_at, now),
        )
        event_request = {
            "operation": "kernel.credentialBind", "requestId": "PON-REQUEST-" + uuid.uuid4().hex,
            "actor": {"id": author["actor_id"], "kind": author["actor_kind"], "role": author["role"]},
            "authority": {"capabilityId": author_capability["capability_id"], "exactAuthorActionId": action_id},
            "aggregate": {"type": "CallerCapability", "id": capability_id, "expectedVersion": 0},
            "parameters": plan,
        }
        event = append_hub_event(
            con, request=event_request, capability_id=author_capability["capability_id"],
            aggregate_version=1, before_root=before_root, after_root=after_root,
            event_result={"state": "ACTIVE", "actorId": actor_id, "allowedOperations": sorted(allowed_operations)},
            occurred_at=now,
        )
        con.execute("UPDATE exact_author_actions SET consumed_event_id=? WHERE action_id=?", (event["eventId"], action_id))
        state_root = _operational_state(con)["operationalStateRoot"]
        con.commit()
        return {
            "ok": True, "created": True, "credential": str(output_path),
            "actorId": actor_id, "capabilityId": capability_id,
            "allowedOperations": sorted(allowed_operations), "expiresAt": expires_at,
            "eventId": event["eventId"], "stateRoot": state_root,
            "secretSha256": created["secretSha256"],
        }
    except Exception:
        if con.in_transaction:
            con.rollback()
        if created_output and os.path.lexists(output_path):
            output_path.unlink()
        raise
    finally:
        con.close()


def create_kernel_actor(
    database_path: Path, author_credential_path: Path, *, action_id: str,
    actor_id: str, actor_kind: str, actor_role: str, plugin_root: Path,
) -> dict[str, Any]:
    if actor_kind not in {"HUMAN", "BROWSER", "COURIER", "ENDPOINT", "SYSTEM"}:
        raise PostOfficeError("PON_INPUT_INVALID", "Actor kind is invalid", {})
    con = _open_writer(database_path, plugin_root)
    try:
        credential = _credential(author_credential_path)
        capability = con.execute("SELECT * FROM caller_capabilities WHERE capability_id=?", (credential["capabilityId"],)).fetchone()
        author = con.execute("SELECT * FROM actors WHERE actor_id=?", (capability["actor_id"],)).fetchone() if capability else None
        if (not capability or capability["status"] != "ACTIVE"
                or not hmac.compare_digest(str(capability["secret_sha256"]), sha256_bytes(credential["secret"].encode("utf-8")))
                or (capability["expires_at"] and _parse_timestamp(capability["expires_at"]) <= datetime.now(timezone.utc))
                or not author or author["actor_kind"] != "HUMAN" or author["role"] != "author" or author["status"] != "ACTIVE"):
            raise PostOfficeError("PON_AUTHORIZATION_DENIED", "Actor creation requires the authenticated human author", {})
        if con.execute("SELECT 1 FROM actors WHERE actor_id=?", (actor_id,)).fetchone():
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Actor already exists", {})
        if con.execute("SELECT 1 FROM exact_author_actions WHERE action_id=?", (action_id,)).fetchone():
            raise PostOfficeError("PON_AUTHORIZATION_DENIED", "Actor author action already exists", {})
        now = _timestamp()
        plan = {"actorId": actor_id, "actorKind": actor_kind, "actorRole": actor_role}
        confirmation = sha256_json(plan)
        con.execute("INSERT INTO exact_author_actions(action_id,actor_id,operation,scope_json,confirmation_sha256,recorded_at) VALUES(?,?,?,?,?,?)", (action_id, author["actor_id"], "kernel.actorCreate", canonical_json_bytes(plan).decode("utf-8"), confirmation, now))
        con.execute("INSERT INTO actors VALUES(?,?,?,?,?)", (actor_id, actor_kind, actor_role, "ACTIVE", now))
        before_root = _aggregate_root("Actor", actor_id, None)
        after_root = _aggregate_root("Actor", actor_id, {**plan, "status": "ACTIVE"})
        request = {"operation": "kernel.actorCreate", "requestId": "PON-REQUEST-" + uuid.uuid4().hex,
                   "actor": {"id": author["actor_id"], "kind": author["actor_kind"], "role": author["role"]},
                   "authority": {"capabilityId": capability["capability_id"], "exactAuthorActionId": action_id},
                   "aggregate": {"type": "Actor", "id": actor_id}, "parameters": plan}
        event = append_hub_event(con, request=request, capability_id=capability["capability_id"], aggregate_version=1,
                                 before_root=before_root, after_root=after_root, event_result={"state": "ACTIVE", **plan}, occurred_at=now)
        con.execute("UPDATE exact_author_actions SET consumed_event_id=? WHERE action_id=?", (event["eventId"], action_id))
        state_root = _operational_state(con)["operationalStateRoot"]
        con.commit()
        return {"ok": True, "created": True, "actorId": actor_id, "actorKind": actor_kind,
                "actorRole": actor_role, "eventId": event["eventId"], "stateRoot": state_root}
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()


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
    bootstrap_operations = _bootstrap_operations(actor_role)
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
                and json.loads(capability["allowed_operations_json"]) == bootstrap_operations
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
                json.dumps(bootstrap_operations, separators=(",", ":")),
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
        current_contract_root = _contract_root(plugin_root)
        if row["contract_root"] != current_contract_root:
            raise PostOfficeError(
                "PON_CONTRACT_INVALID",
                "Bootstrapped kernel contract root differs from the installed contract catalogue",
                {"retainedContractRoot": row["contract_root"], "installedContractRoot": current_contract_root},
            )
        return {
            "ok": True,
            "instanceId": row["instance_id"],
            "mode": row["mode"],
            "status": row["status"],
            "authorityState": row["authority_state"],
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


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _aggregate_root(aggregate_type: str, aggregate_id: str, state: Any) -> str:
    return sha256_json(
        {"aggregateType": aggregate_type, "aggregateId": aggregate_id, "state": state}
    )


def _aggregate_position(
    con: sqlite3.Connection,
    *,
    aggregate_type: str,
    aggregate_id: str,
    current_root: str,
) -> int:
    retained = con.execute(
        """SELECT aggregate_version,after_state_root FROM hub_events
           WHERE aggregate_type=? AND aggregate_id=?
           ORDER BY aggregate_version DESC LIMIT 1""",
        (aggregate_type, aggregate_id),
    ).fetchone()
    if not retained:
        return 0
    if retained["after_state_root"] != current_root:
        raise PostOfficeError(
            "PON_DATABASE_INVALID",
            "Aggregate row state differs from its latest mutation event",
            {"aggregateType": aggregate_type, "aggregateId": aggregate_id},
        )
    return int(retained["aggregate_version"])


def _require_aggregate(
    request: dict[str, Any],
    *,
    aggregate_type: str,
    aggregate_id: str,
    actual_version: int,
    actual_root: str,
) -> None:
    assert_aggregate_concurrency(
        request["aggregate"],
        actual_type=aggregate_type,
        actual_id=aggregate_id,
        actual_version=actual_version,
        actual_root=actual_root,
    )


def _grant_state(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "grantId": row["grant_id"],
        "grantorActorId": row["grantor_actor_id"],
        "recipientActorId": row["recipient_actor_id"],
        "allowedOperations": json.loads(row["allowed_operations_json"]),
        "scope": json.loads(row["scope_json"]),
        "classification": row["classification"],
        "maximumUses": row["maximum_uses"],
        "remainingUses": row["remaining_uses"],
        "status": row["status"],
        "rationale": row["rationale"],
        "sourceAuthorActionId": row["source_author_action_id"],
        "expiresAt": row["expires_at"],
    }


def _endpoint_state(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "endpointId": row["endpoint_id"],
        "actorId": row["actor_id"],
        "projectId": row["project_id"],
        "taskId": row["task_id"],
        "role": row["role"],
        "accessScope": json.loads(row["access_scope_json"]),
        "status": row["status"],
        "startupRoot": row["startup_root"],
    }


def _mailbox_state(rows: list[sqlite3.Row], mailbox_id: str) -> dict[str, Any]:
    generations = [
        {
            "generation": int(row["generation"]),
            "mailDomain": row["mail_domain"],
            "endpointId": row["endpoint_id"],
            "status": row["status"],
        }
        for row in rows
    ]
    return {"mailboxId": mailbox_id, "generations": generations}


def _project_state(con: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    domains = [
        str(item[0])
        for item in con.execute(
            "SELECT mail_domain FROM project_mail_domains WHERE project_id=? ORDER BY mail_domain",
            (row["project_id"],),
        )
    ]
    value: dict[str, Any] = {
        "id": row["project_id"],
        "code": row["code"],
        "displayName": row["display_name"],
        "kind": row["kind"],
        "status": row["status"],
        "localProjectRoot": row["local_project_root"],
        "localCasRoot": row["local_cas_root"],
        "localBackupRoot": row["local_backup_root"],
        "mailDomains": domains,
    }
    if row["policy_root"]:
        value["policyRoot"] = "sha256:" + str(row["policy_root"])
    return value


def _task_state(con: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    binding = con.execute(
        "SELECT * FROM task_endpoint_bindings WHERE task_id=?", (row["task_id"],)
    ).fetchone()
    endpoint_binding = None
    if binding:
        endpoint_binding = {
            "endpointId": binding["endpoint_id"],
            "mailboxId": binding["mailbox_id"],
            "mailboxGeneration": int(binding["mailbox_generation"]),
        }
    return {
        "id": row["task_id"],
        "projectId": row["project_id"],
        "taskKind": row["task_kind"],
        "objective": row["objective"],
        "mutablePackageId": row["mutable_package_id"],
        "acceptanceContractRoot": "sha256:" + str(row["acceptance_contract_root"]),
        "authorityGrantId": row["authority_grant_id"],
        "state": row["state"],
        "endpointBinding": endpoint_binding,
    }


def _plan_state(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["plan_id"],
        "aggregateType": row["aggregate_type"],
        "aggregateId": row["aggregate_id"],
        "authorityId": row["authority_id"],
        "requestedResources": json.loads(row["requested_resources_json"]),
        "planRoot": str(row["plan_root"]),
        "stage": row["stage"],
        "payload": json.loads(row["payload_json"]),
    }


def _package_state(con: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    package_id = str(row["package_id"])
    versions = [
        {"version": item["version"], "contentRoot": item["content_root"], "state": item["state"]}
        for item in con.execute("SELECT * FROM package_versions WHERE package_id=? ORDER BY version", (package_id,))
    ]
    interfaces = list(con.execute(
        "SELECT interface_id,direction FROM package_interfaces WHERE package_id=? ORDER BY direction,interface_id",
        (package_id,),
    ))
    value: dict[str, Any] = {
        "id": package_id, "displayName": row["display_name"],
        "owningProjectId": row["owning_project_id"], "status": row["status"],
        "versions": versions,
        "providedInterfaceIds": [item["interface_id"] for item in interfaces if item["direction"] == "PROVIDES"],
        "requiredInterfaceIds": [item["interface_id"] for item in interfaces if item["direction"] == "REQUIRES"],
        "taskIds": [str(item[0]) for item in con.execute("SELECT task_id FROM tasks WHERE mutable_package_id=? ORDER BY task_id", (package_id,))],
    }
    if row["source_location"]:
        value["sourceLocation"] = row["source_location"]
    if row["licence"]:
        value["licence"] = row["licence"]
    return value


def _interface_state(con: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    participants = list(con.execute(
        "SELECT package_id,relation FROM interface_participants WHERE interface_id=? AND version=? ORDER BY relation,package_id",
        (row["interface_id"], row["version"]),
    ))
    value: dict[str, Any] = {
        "id": row["interface_id"], "version": row["version"],
        "stewardId": row["steward_package_id"], "status": row["status"],
        "humanGuide": row["human_guide"],
        "providerIds": [item["package_id"] for item in participants if item["relation"] == "PROVIDER"],
        "consumerIds": [item["package_id"] for item in participants if item["relation"] == "CONSUMER"],
    }
    if row["schema_root"]:
        value["schemaRoot"] = row["schema_root"]
    if row["fixture_root"]:
        value["fixtureRoot"] = row["fixture_root"]
    if row["replacement_interface_id"]:
        value["replacementInterfaceId"] = row["replacement_interface_id"]
    return value


def _interface_family_state(con: sqlite3.Connection, interface_id: str) -> dict[str, Any]:
    return {
        "interfaceId": interface_id,
        "versions": [
            _interface_state(con, row)
            for row in con.execute("SELECT * FROM interfaces WHERE interface_id=? ORDER BY version", (interface_id,))
        ],
    }


def _workflow_state(row: sqlite3.Row) -> dict[str, Any]:
    return json.loads(row["state_json"])


def _cycle_state(con: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": row["cycle_id"], "projectId": row["project_id"], "scope": row["scope"],
        "openingAuthorityId": row["opening_author_action_id"], "state": row["state"],
        "nonClaims": [str(item[0]) for item in con.execute(
            "SELECT note FROM cycle_notes WHERE cycle_id=? AND note_kind='NON_CLAIM' ORDER BY note,event_id",
            (row["cycle_id"],),
        )],
    }
    if row["acceptance_author_action_id"]:
        value["acceptanceDecisionId"] = row["acceptance_author_action_id"]
    if row["closure_author_action_id"]:
        value["closureAuthorityId"] = row["closure_author_action_id"]
    return value


def _message_state(con: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    basis = row["authority_grant_id"] or row["exact_author_action_id"]
    return {
        "id": row["message_id"], "domain": row["mail_domain"], "messageType": row["message_type"],
        "senderEndpointId": row["sender_endpoint_id"], "recipientMailboxId": row["recipient_mailbox_id"],
        "recipientGeneration": int(row["recipient_generation"]),
        "relatedMessageIds": [str(item[0]) for item in con.execute(
            "SELECT related_message_id FROM message_relations WHERE message_id=? ORDER BY related_message_id",
            (row["message_id"],),
        )],
        "authorityBasisId": basis, "requestedAction": row["requested_action"],
        "completionCriteria": row["completion_criteria"], "contentRoot": row["content_root"],
        "semanticCycleId": row["semantic_cycle_id"], "state": row["state"],
    }


def _bundle_state(con: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["bundle_id"], "semanticMessageId": row["message_id"],
        "canonicalFilename": row["canonical_filename"], "bundleVersion": int(row["bundle_version"]),
        "sizeBytes": int(row["size_bytes"]), "sha256": row["sha256"],
        "manifestRoot": row["manifest_root"],
        "payloads": [
            {"path": item["relative_path"], "sizeBytes": int(item["size_bytes"]), "sha256": item["sha256"]}
            for item in con.execute("SELECT * FROM bundle_payloads WHERE bundle_id=? ORDER BY ordinal", (row["bundle_id"],))
        ],
        "custodyEvidence": [
            {"kind": item["location_kind"], "reference": item["local_location"], "sha256": item["content_sha256"]}
            for item in con.execute("SELECT * FROM storage_copies WHERE content_sha256=? ORDER BY storage_copy_id", (row["sha256"],))
        ],
        "state": row["state"],
    }


def _message_plan_state(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "planId": row["plan_id"], "message": json.loads(row["semantic_message_json"]),
        "bundle": json.loads(row["bundle_json"]), "planRoot": row["plan_root"], "state": row["state"],
    }


def _transport_state(row: sqlite3.Row, dispatch: sqlite3.Row | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": row["transport_attempt_id"], "semanticMessageId": row["message_id"],
        "bundleId": row["bundle_id"], "source": row["source"], "destination": row["destination"],
        "state": row["state"], "attemptNumber": int(row["attempt_number"]),
    }
    if row["canonical_attempt_id"]:
        value["canonicalAttemptId"] = row["canonical_attempt_id"]
    if dispatch:
        value["dispatch"] = {
            "id": dispatch["dispatch_id"], "channel": dispatch["channel"], "state": dispatch["state"],
            "destinationEndpointId": dispatch["destination_endpoint_id"],
            "destinationMailboxId": dispatch["destination_mailbox_id"],
            "destinationGeneration": int(dispatch["destination_generation"]),
            "observableMarker": dispatch["observable_marker"], "attemptCount": int(dispatch["attempt_count"]),
            "nextAttemptAt": dispatch["next_attempt_at"], "lastErrorCode": dispatch["last_error_code"],
        }
    return value


def _validate_hub_event_chain(con: sqlite3.Connection) -> None:
    previous_hash: str | None = None
    expected_sequence = 1
    aggregate_positions: dict[tuple[str, str], tuple[int, str]] = {}
    rows = con.execute(
        """SELECT sequence,event_id,occurred_at,actor_id,operation,capability_id,
                  authority_grant_id,exact_author_action_id,aggregate_type,aggregate_id,
                  aggregate_version,before_state_root,after_state_root,result_json,payload_sha256,
                  previous_event_sha256,event_sha256
           FROM hub_events ORDER BY sequence"""
    )
    for row in rows:
        sequence = int(row["sequence"])
        if sequence != expected_sequence:
            raise PostOfficeError(
                "PON_DATABASE_INVALID",
                "Operational event sequence is not contiguous",
                {"expectedSequence": expected_sequence, "actualSequence": sequence},
            )
        if row["previous_event_sha256"] != previous_hash:
            raise PostOfficeError(
                "PON_DATABASE_INVALID",
                "Operational event chain predecessor differs",
                {"sequence": sequence, "eventId": row["event_id"]},
            )
        try:
            event_result = json.loads(row["result_json"])
        except json.JSONDecodeError as exc:
            raise PostOfficeError(
                "PON_DATABASE_INVALID",
                "Operational event result is not valid JSON",
                {"sequence": sequence, "eventId": row["event_id"]},
            ) from exc
        if not isinstance(event_result, dict):
            raise PostOfficeError(
                "PON_DATABASE_INVALID",
                "Operational event result must be an object",
                {"sequence": sequence, "eventId": row["event_id"]},
            )
        event_body = {
            "sequence": sequence,
            "eventId": row["event_id"],
            "occurredAt": row["occurred_at"],
            "actorId": row["actor_id"],
            "operation": row["operation"],
            "capabilityId": row["capability_id"],
            "authorityGrantId": row["authority_grant_id"],
            "exactAuthorActionId": row["exact_author_action_id"],
            "aggregateType": row["aggregate_type"],
            "aggregateId": row["aggregate_id"],
            "aggregateVersion": int(row["aggregate_version"]),
            "beforeStateRoot": row["before_state_root"],
            "afterStateRoot": row["after_state_root"],
            "result": event_result,
            "payloadSha256": row["payload_sha256"],
            "previousEventSha256": row["previous_event_sha256"],
        }
        if sha256_json(event_body) != row["event_sha256"]:
            raise PostOfficeError(
                "PON_DATABASE_INVALID",
                "Operational event digest verification failed",
                {"sequence": sequence, "eventId": row["event_id"]},
            )
        aggregate_key = (str(row["aggregate_type"]), str(row["aggregate_id"]))
        retained = aggregate_positions.get(aggregate_key)
        actual_version = int(row["aggregate_version"])
        if retained:
            if actual_version != retained[0] + 1 or row["before_state_root"] != retained[1]:
                raise PostOfficeError(
                    "PON_DATABASE_INVALID",
                    "Operational aggregate event history is discontinuous",
                    {
                        "aggregateType": aggregate_key[0],
                        "aggregateId": aggregate_key[1],
                        "eventId": row["event_id"],
                    },
                )
        aggregate_positions[aggregate_key] = (actual_version, str(row["after_state_root"]))
        previous_hash = str(row["event_sha256"])
        expected_sequence += 1


def _validate_kernel_projections(con: sqlite3.Connection) -> None:
    for row in con.execute("SELECT * FROM authority_grants ORDER BY grant_id"):
        root = _aggregate_root("AuthorityGrant", row["grant_id"], _grant_state(row))
        _aggregate_position(
            con, aggregate_type="AuthorityGrant", aggregate_id=row["grant_id"], current_root=root
        )
    for row in con.execute("SELECT * FROM endpoints ORDER BY endpoint_id"):
        root = _aggregate_root("Endpoint", row["endpoint_id"], _endpoint_state(row))
        _aggregate_position(
            con, aggregate_type="Endpoint", aggregate_id=row["endpoint_id"], current_root=root
        )
    mailbox_ids = [
        str(row[0]) for row in con.execute("SELECT DISTINCT mailbox_id FROM mailboxes ORDER BY mailbox_id")
    ]
    for mailbox_id in mailbox_ids:
        rows = list(
            con.execute(
                "SELECT * FROM mailboxes WHERE mailbox_id=? ORDER BY generation", (mailbox_id,)
            )
        )
        root = _aggregate_root("Mailbox", mailbox_id, _mailbox_state(rows, mailbox_id))
        _aggregate_position(
            con, aggregate_type="Mailbox", aggregate_id=mailbox_id, current_root=root
        )
    for row in con.execute("SELECT * FROM provisioning_plans ORDER BY plan_id"):
        root = _aggregate_root("ProvisioningPlan", row["plan_id"], _plan_state(row))
        if root.removeprefix("sha256:") != row["aggregate_root"]:
            raise PostOfficeError(
                "PON_DATABASE_INVALID", "Provisioning plan root differs from retained state",
                {"provisioningPlanId": row["plan_id"]},
            )
        _aggregate_position(
            con, aggregate_type="ProvisioningPlan", aggregate_id=row["plan_id"], current_root=root
        )
    for row in con.execute("SELECT * FROM projects ORDER BY project_id"):
        root = _aggregate_root("Project", row["project_id"], _project_state(con, row))
        version = _aggregate_position(
            con, aggregate_type="Project", aggregate_id=row["project_id"], current_root=root
        )
        if version and root.removeprefix("sha256:") != row["aggregate_root"]:
            raise PostOfficeError(
                "PON_DATABASE_INVALID", "Project root differs from retained state",
                {"projectId": row["project_id"]},
            )
    for row in con.execute("SELECT * FROM tasks ORDER BY task_id"):
        root = _aggregate_root("Task", row["task_id"], _task_state(con, row))
        version = _aggregate_position(
            con, aggregate_type="Task", aggregate_id=row["task_id"], current_root=root
        )
        if version and root.removeprefix("sha256:") != row["aggregate_root"]:
            raise PostOfficeError(
                "PON_DATABASE_INVALID", "Task root differs from retained state", {"taskId": row["task_id"]}
            )
    for row in con.execute("SELECT * FROM software_packages ORDER BY package_id"):
        root = _aggregate_root("SoftwarePackage", row["package_id"], _package_state(con, row))
        _aggregate_position(con, aggregate_type="SoftwarePackage", aggregate_id=row["package_id"], current_root=root)
    for row in con.execute("SELECT * FROM workflow_aggregates ORDER BY aggregate_type,aggregate_id"):
        root = _aggregate_root(row["aggregate_type"], row["aggregate_id"], _workflow_state(row))
        if root.removeprefix("sha256:") != row["aggregate_root"]:
            raise PostOfficeError("PON_DATABASE_INVALID", "Workflow aggregate root differs", {"aggregateType": row["aggregate_type"], "aggregateId": row["aggregate_id"]})
        _aggregate_position(con, aggregate_type=row["aggregate_type"], aggregate_id=row["aggregate_id"], current_root=root)
    for row in con.execute("SELECT * FROM message_plans ORDER BY plan_id"):
        root = _aggregate_root("MessagePlan", row["plan_id"], _message_plan_state(row))
        if root.removeprefix("sha256:") != row["aggregate_root"]:
            raise PostOfficeError("PON_DATABASE_INVALID", "Message plan root differs", {"planId": row["plan_id"]})
        _aggregate_position(con, aggregate_type="MessagePlan", aggregate_id=row["plan_id"], current_root=root)
    for row in con.execute("SELECT * FROM semantic_cycles ORDER BY cycle_id"):
        root = _aggregate_root("SemanticCycle", row["cycle_id"], _cycle_state(con, row))
        _aggregate_position(con, aggregate_type="SemanticCycle", aggregate_id=row["cycle_id"], current_root=root)
    for row in con.execute("SELECT * FROM semantic_messages ORDER BY message_id"):
        root = _aggregate_root("SemanticMessage", row["message_id"], _message_state(con, row))
        _aggregate_position(con, aggregate_type="SemanticMessage", aggregate_id=row["message_id"], current_root=root)


def _scope_projects(con: sqlite3.Connection, scope: dict[str, Any]) -> dict[str, dict[str, str]]:
    checks = {
        "projectIds": ("projects", "project_id", "project_id"),
        "taskIds": ("tasks", "task_id", "project_id"),
        "packageIds": ("software_packages", "package_id", "owning_project_id"),
        "mailDomains": ("project_mail_domains", "mail_domain", "project_id"),
    }
    resolved: dict[str, dict[str, str]] = {key: {} for key in checks}
    total = 0
    for key, (table, column, project_column) in checks.items():
        values = scope.get(key, [])
        total += len(values)
        for value in values:
            row = con.execute(
                f"SELECT {project_column} FROM {table} WHERE {column}=?", (value,)
            ).fetchone()
            if not row:
                raise PostOfficeError(
                    "PON_INPUT_INVALID", "Authority scope references an unknown entity",
                    {"scopeField": key, "value": value},
                )
            resolved[key][value] = str(row[0])
    if total == 0:
        raise PostOfficeError("PON_INPUT_INVALID", "Authority scope must not be empty", {})
    return resolved


def _validate_scope_references(con: sqlite3.Connection, scope: dict[str, Any]) -> None:
    _scope_projects(con, scope)


def _validate_endpoint_access_scope(
    con: sqlite3.Connection,
    access_scope: dict[str, Any],
    *,
    owner_project_id: str,
    owner_task_id: str | None,
    grant_scope: dict[str, Any] | None,
) -> None:
    access_projects = _scope_projects(con, access_scope)
    for key, members in access_projects.items():
        for value, project_id in members.items():
            if project_id != owner_project_id:
                raise PostOfficeError(
                    "PON_INPUT_INVALID",
                    "Endpoint access scope crosses its owning project",
                    {"scopeField": key, "value": value, "owningProjectId": owner_project_id},
                )
    if owner_task_id and any(value != owner_task_id for value in access_scope.get("taskIds", [])):
        raise PostOfficeError(
            "PON_INPUT_INVALID",
            "Task-bound endpoint scope names a different task",
            {"endpointTaskId": owner_task_id},
        )
    if grant_scope is None:
        return

    grant_projects = _scope_projects(con, grant_scope)
    project_authority = set(grant_scope.get("projectIds", []))
    for key in ("projectIds", "taskIds", "packageIds", "mailDomains"):
        exact_authority = set(grant_scope.get(key, []))
        for value in access_scope.get(key, []):
            owning_project = access_projects[key][value]
            if value not in exact_authority and owning_project not in project_authority:
                raise PostOfficeError(
                    "PON_AUTHORIZATION_DENIED",
                    "Authority grant does not permit the requested endpoint access scope",
                    {"scopeField": key, "value": value},
                )


def _scope_allows(scope: dict[str, Any], facts: dict[str, set[str]]) -> bool:
    constrained = False
    for key in ("projectIds", "taskIds", "packageIds", "mailDomains"):
        declared = set(scope.get(key, []))
        if not declared:
            continue
        constrained = True
        if not declared.intersection(facts.get(key, set())):
            return False
    return constrained


def _prepare_mutation_authority(
    con: sqlite3.Connection,
    *,
    request: dict[str, Any],
    capability: sqlite3.Row,
    request_hash: str,
    recorded_at: str,
    scope_facts: dict[str, set[str]],
) -> dict[str, Any]:
    authority = request["authority"]
    action_id = authority.get("exactAuthorActionId")
    grant_id = authority.get("authorityGrantId")
    if bool(action_id) == bool(grant_id):
        raise PostOfficeError(
            "PON_AUTHORIZATION_DENIED", "Mutation requires exactly one authority basis", {}
        )
    if action_id:
        actor = con.execute(
            "SELECT * FROM actors WHERE actor_id=?", (capability["actor_id"],)
        ).fetchone()
        if not actor or actor["actor_kind"] != "HUMAN" or actor["role"] != "author":
            raise PostOfficeError(
                "PON_AUTHORIZATION_DENIED", "Only an authenticated human author may mint an exact action", {}
            )
        retained = con.execute(
            "SELECT * FROM exact_author_actions WHERE action_id=?", (action_id,)
        ).fetchone()
        scope_json = canonical_json_bytes(
            {"aggregate": request["aggregate"], "parameters": request["parameters"]}
        ).decode("utf-8")
        if retained:
            if (
                retained["actor_id"] != actor["actor_id"]
                or retained["operation"] != request["operation"]
                or retained["scope_json"] != scope_json
                or retained["confirmation_sha256"] != request_hash
                or retained["consumed_event_id"] is not None
            ):
                raise PostOfficeError(
                    "PON_AUTHORIZATION_DENIED", "Exact author action is changed or already consumed",
                    {"exactAuthorActionId": action_id},
                )
        else:
            con.execute(
                """INSERT INTO exact_author_actions(
                   action_id,actor_id,operation,scope_json,confirmation_sha256,recorded_at)
                   VALUES(?,?,?,?,?,?)""",
                (action_id, actor["actor_id"], request["operation"], scope_json, request_hash, recorded_at),
            )
        return {"kind": "EXACT_AUTHOR_ACTION", "id": action_id}

    grant = con.execute(
        "SELECT * FROM authority_grants WHERE grant_id=?", (grant_id,)
    ).fetchone()
    if not grant or grant["recipient_actor_id"] != capability["actor_id"]:
        raise PostOfficeError("PON_AUTHORIZATION_DENIED", "Authority grant does not bind the caller", {})
    if grant["status"] != "ACTIVE" or request["operation"] not in json.loads(grant["allowed_operations_json"]):
        raise PostOfficeError("PON_AUTHORIZATION_DENIED", "Authority grant does not allow this operation", {})
    if grant["expires_at"] and _parse_timestamp(grant["expires_at"]) <= datetime.now(timezone.utc):
        raise PostOfficeError("PON_AUTHORIZATION_DENIED", "Authority grant has expired", {})
    if grant["remaining_uses"] is not None and int(grant["remaining_uses"]) <= 0:
        raise PostOfficeError("PON_AUTHORIZATION_DENIED", "Authority grant is exhausted", {})
    if not _scope_allows(json.loads(grant["scope_json"]), scope_facts):
        raise PostOfficeError("PON_AUTHORIZATION_DENIED", "Authority grant scope does not cover the request", {})
    return {"kind": "AUTHORITY_GRANT", "id": grant_id, "row": grant}


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
            "authorityState": kernel["authority_state"],
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


def _authority_inspect_result(
    con: sqlite3.Connection,
    request: dict[str, Any],
    request_hash: str,
    recorded_at: str,
) -> dict[str, Any]:
    state = _operational_state(con)
    root = state["operationalStateRoot"]
    actor_id = request["parameters"]["actorId"]
    proposed_operation = request["parameters"]["proposedOperation"]
    actor = con.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
    now = datetime.now(timezone.utc)
    capabilities: list[str] = []
    grants: list[str] = []
    if actor and actor["status"] == "ACTIVE":
        for row in con.execute(
            "SELECT * FROM caller_capabilities WHERE actor_id=? AND status='ACTIVE' ORDER BY capability_id",
            (actor_id,),
        ):
            if row["expires_at"] and _parse_timestamp(row["expires_at"]) <= now:
                continue
            if proposed_operation in json.loads(row["allowed_operations_json"]):
                capabilities.append(row["capability_id"])
        for row in con.execute(
            "SELECT * FROM authority_grants WHERE recipient_actor_id=? AND status='ACTIVE' ORDER BY grant_id",
            (actor_id,),
        ):
            if row["expires_at"] and _parse_timestamp(row["expires_at"]) <= now:
                continue
            if proposed_operation in json.loads(row["allowed_operations_json"]):
                grants.append(row["grant_id"])
    implemented = proposed_operation in IMPLEMENTED_OPERATIONS
    return {
        "schemaVersion": "1",
        "requestId": request["requestId"],
        "operation": request["operation"],
        "ok": True,
        "beforeRoot": root,
        "afterRoot": root,
        "aggregateVersion": 0,
        "createdIds": [],
        "warnings": [],
        "authorization": {
            "actorId": actor_id,
            "actorStatus": actor["status"] if actor else "NOT_FOUND",
            "proposedOperation": proposed_operation,
            "implemented": implemented,
            "capabilityAllowed": bool(capabilities),
            "activeCapabilityIds": capabilities,
            "activeGrantIds": grants,
            "decision": "CAPABILITY_ALLOWED" if implemented and capabilities else "DENIED",
        },
        "receipt": {
            "receiptId": "PON-RECEIPT-" + uuid.uuid4().hex,
            "requestSha256": request_hash,
            "recordedAt": recorded_at,
            "stateRoot": root,
        },
    }


def _shadow_observation_state(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "observationId": row["observation_id"],
        "sourceKind": row["source_kind"],
        "sourceReference": row["source_reference"],
        "expectedRoot": row["expected_root"],
        "observedRoot": row["observed_root"],
        "classification": row["classification"],
        "evidenceRoot": row["evidence_root"],
        "state": row["state"],
    }


def _reconciliation_plan_state(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "planId": row["plan_id"],
        "snapshotRoot": row["snapshot_root"],
        "assertionSetRoot": row["assertion_set_root"],
        "observationSetRoot": row["observation_set_root"],
        "actions": json.loads(row["actions_json"]),
        "planRoot": row["plan_root"],
        "state": row["state"],
    }


def _hub_snapshot_detail(con: sqlite3.Connection, parameters: dict[str, Any]) -> dict[str, Any]:
    requested_domains = sorted(set(parameters.get("domains", [])))
    domains = [
        {"mailDomain": row["mail_domain"], "projectId": row["project_id"]}
        for row in con.execute(
            "SELECT mail_domain,project_id FROM project_mail_domains ORDER BY mail_domain"
        )
        if not requested_domains or row["mail_domain"] in requested_domains
    ]
    selected = {item["mailDomain"] for item in domains}
    mailbox_count = int(con.execute(
        "SELECT COUNT(*) FROM mailboxes" if not selected else
        "SELECT COUNT(*) FROM mailboxes WHERE mail_domain IN (%s)" % ",".join("?" for _ in selected),
        () if not selected else tuple(sorted(selected)),
    ).fetchone()[0])
    message_count = int(con.execute(
        "SELECT COUNT(*) FROM semantic_messages" if not selected else
        "SELECT COUNT(*) FROM semantic_messages WHERE mail_domain IN (%s)" % ",".join("?" for _ in selected),
        () if not selected else tuple(sorted(selected)),
    ).fetchone()[0])
    detail: dict[str, Any] = {
        "domains": domains,
        "counts": {
            "projects": int(con.execute("SELECT COUNT(*) FROM projects").fetchone()[0]),
            "tasks": int(con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]),
            "mailboxes": mailbox_count,
            "messages": message_count,
            "transportAttempts": int(con.execute("SELECT COUNT(*) FROM transport_attempts").fetchone()[0]),
            "automaticReviews": int(con.execute("SELECT COUNT(*) FROM automatic_reviews").fetchone()[0]),
            "openAttention": int(con.execute("SELECT COUNT(*) FROM attention_items WHERE state='OPEN'").fetchone()[0]),
            "openShadowObservations": int(con.execute("SELECT COUNT(*) FROM shadow_observations WHERE state='OPEN'").fetchone()[0]),
        },
        "eventBoundary": _operational_state(con)["eventBoundary"],
    }
    if parameters.get("includeFileHashes"):
        detail["custody"] = [
            {"storageCopyId": row["storage_copy_id"], "contentSha256": row["content_sha256"],
             "locationKind": row["location_kind"]}
            for row in con.execute(
                "SELECT storage_copy_id,content_sha256,location_kind FROM storage_copies ORDER BY storage_copy_id"
            )
        ]
    detail["snapshotRoot"] = sha256_json(detail)
    return detail


def _catalogue_read_result(
    con: sqlite3.Connection,
    request: dict[str, Any],
    request_hash: str,
    recorded_at: str,
) -> dict[str, Any]:
    operation = request["operation"]
    if operation == "hub.snapshot":
        detail_name, detail = "resource", _hub_snapshot_detail(con, request["parameters"])
        aggregate_type, aggregate_id = "HubSnapshot", detail["snapshotRoot"]
    elif operation == "project.read":
        row = con.execute(
            "SELECT * FROM projects WHERE project_id=?", (request["parameters"]["projectId"],)
        ).fetchone()
        if not row:
            raise PostOfficeError("PON_INPUT_INVALID", "Project does not exist", {})
        detail_name, detail = "project", _project_state(con, row)
        aggregate_type, aggregate_id = "Project", str(row["project_id"])
    elif operation == "task.read":
        row = con.execute(
            "SELECT * FROM tasks WHERE task_id=?", (request["parameters"]["taskId"],)
        ).fetchone()
        if not row:
            raise PostOfficeError("PON_INPUT_INVALID", "Task does not exist", {})
        detail_name, detail = "task", _task_state(con, row)
        aggregate_type, aggregate_id = "Task", str(row["task_id"])
    elif operation == "provisioning.inspect":
        row = con.execute(
            "SELECT * FROM provisioning_plans WHERE plan_id=?",
            (request["parameters"]["provisioningPlanId"],),
        ).fetchone()
        if not row:
            raise PostOfficeError("PON_INPUT_INVALID", "Provisioning plan does not exist", {})
        detail_name, detail = "provisioningPlan", _plan_state(row)
        aggregate_type, aggregate_id = "ProvisioningPlan", str(row["plan_id"])
    elif operation in {"package.read", "package.rehome.preview"}:
        package_id = request["parameters"]["packageId"]
        row = con.execute("SELECT * FROM software_packages WHERE package_id=?", (package_id,)).fetchone()
        if not row:
            raise PostOfficeError("PON_INPUT_INVALID", "Software package does not exist", {})
        detail = _package_state(con, row)
        if operation == "package.rehome.preview":
            destination = con.execute("SELECT * FROM projects WHERE project_id=?", (request["parameters"]["destinationProjectId"],)).fetchone()
            if not destination or destination["status"] != "ACTIVE":
                raise PostOfficeError("PON_INPUT_INVALID", "Destination project is not active", {})
            blockers = [
                str(item[0]) for item in con.execute(
                    "SELECT task_id FROM tasks WHERE mutable_package_id=? AND state NOT IN ('CLOSED','CANCELLED') ORDER BY task_id",
                    (package_id,),
                )
            ]
            preview = {"package": detail, "destinationProjectId": destination["project_id"], "blockingTaskIds": blockers, "ready": not blockers}
            preview_root = sha256_json(preview)
            detail = {**preview, "planId": "PON-REHOME-" + preview_root[:24], "planRoot": preview_root}
        detail_name, aggregate_type, aggregate_id = "resource", "SoftwarePackage", package_id
    elif operation == "interface.read":
        row = con.execute(
            "SELECT * FROM interfaces WHERE interface_id=? AND (? IS NULL OR version=?) ORDER BY version DESC LIMIT 1",
            (request["parameters"]["interfaceId"], request["parameters"].get("version"), request["parameters"].get("version")),
        ).fetchone()
        if not row:
            raise PostOfficeError("PON_INPUT_INVALID", "Interface does not exist", {})
        detail_name, detail = "resource", _interface_state(con, row)
        aggregate_type, aggregate_id = "Interface", f"{row['interface_id']}@{row['version']}"
    elif operation == "capabilityRequest.read":
        row = con.execute(
            "SELECT * FROM workflow_aggregates WHERE aggregate_type='CapabilityRequest' AND aggregate_id=?",
            (request["parameters"]["capabilityRequestId"],),
        ).fetchone()
        if not row:
            raise PostOfficeError("PON_INPUT_INVALID", "Capability request does not exist", {})
        detail_name, detail = "resource", _workflow_state(row)
        aggregate_type, aggregate_id = "CapabilityRequest", str(row["aggregate_id"])
    elif operation == "bundle.verify":
        bundle = request["parameters"]["bundle"]
        copies = list(con.execute(
            "SELECT * FROM storage_copies WHERE content_sha256=? ORDER BY storage_copy_id",
            (bundle["sha256"].removeprefix("sha256:"),),
        ))
        if not copies:
            raise PostOfficeError("PON_CUSTODY_NOT_VERIFIED", "Bundle has no verified local custody copy", {"sha256": bundle["sha256"]})
        payload_total = sum(int(item["sizeBytes"]) for item in bundle["payloads"])
        if payload_total > int(bundle["sizeBytes"]):
            raise PostOfficeError("PON_INPUT_INVALID", "Bundle payload sizes exceed archive size", {})
        detail_name, detail = "resource", {"state": "VERIFIED", "bundle": bundle, "storageCopyIds": [item["storage_copy_id"] for item in copies]}
        aggregate_type, aggregate_id = "BundleVerification", str(bundle["id"])
    elif operation == "transport.inspect":
        message_id = request["parameters"]["semanticMessageId"]
        attempts = []
        for item in con.execute("SELECT * FROM transport_attempts WHERE message_id=? ORDER BY attempt_number", (message_id,)):
            dispatch = con.execute("SELECT * FROM transport_dispatches WHERE transport_attempt_id=?", (item["transport_attempt_id"],)).fetchone()
            attempts.append(_transport_state(item, dispatch))
        if not attempts:
            raise PostOfficeError("PON_INPUT_INVALID", "Semantic message has no transport attempts", {})
        detail_name, detail = "resource", {"semanticMessageId": message_id, "attempts": attempts}
        aggregate_type, aggregate_id = "TransportView", message_id
    elif operation == "attention.list":
        clauses, values = ["1=1"], []
        if request["parameters"].get("state"):
            clauses.append("state=?"); values.append(request["parameters"]["state"])
        if request["parameters"].get("severity"):
            clauses.append("severity=?"); values.append(request["parameters"]["severity"])
        items = [
            {"id": item["attention_id"], "entityType": item["entity_type"], "entityId": item["entity_id"],
             "severity": item["severity"], "reasonCode": item["reason_code"],
             "details": json.loads(item["details_json"]), "state": item["state"], "openedAt": item["opened_at"]}
            for item in con.execute(f"SELECT * FROM attention_items WHERE {' AND '.join(clauses)} ORDER BY opened_at,attention_id", values)
        ]
        detail_name, detail = "resource", {"items": items, "count": len(items)}
        aggregate_type, aggregate_id = "AttentionProjection", "PON-ATTENTION"
    else:
        raise PostOfficeError("PON_OPERATION_NOT_IMPLEMENTED", "Read operation is unavailable", {})
    root_detail = detail["package"] if operation == "package.rehome.preview" else detail
    if operation == "interface.read":
        root_detail = _interface_family_state(con, str(row["interface_id"]))
        aggregate_id = str(row["interface_id"])
    root = _aggregate_root(aggregate_type, aggregate_id, root_detail)
    version = _aggregate_position(con, aggregate_type=aggregate_type, aggregate_id=aggregate_id, current_root=root)
    state_root = _operational_state(con)["operationalStateRoot"]
    return {
        "schemaVersion": "1", "requestId": request["requestId"], "operation": operation,
        "ok": True, "beforeRoot": root, "afterRoot": root, "aggregateVersion": version,
        "createdIds": [], "warnings": [], detail_name: detail,
        "receipt": {
            "receiptId": "PON-RECEIPT-" + uuid.uuid4().hex,
            "requestSha256": request_hash, "recordedAt": recorded_at, "stateRoot": state_root,
        },
    }


def _consume_grant_authority(
    con: sqlite3.Connection,
    *,
    request: dict[str, Any],
    capability_id: str,
    basis: dict[str, Any],
    recorded_at: str,
) -> str | None:
    if basis["kind"] != "AUTHORITY_GRANT" or basis["row"]["remaining_uses"] is None:
        return None
    grant_id = basis["id"]
    retained = con.execute(
        "SELECT * FROM authority_grants WHERE grant_id=?", (grant_id,)
    ).fetchone()
    before_root = _aggregate_root("AuthorityGrant", grant_id, _grant_state(retained))
    version = _aggregate_position(
        con, aggregate_type="AuthorityGrant", aggregate_id=grant_id, current_root=before_root
    )
    remaining = int(retained["remaining_uses"]) - 1
    status = "EXHAUSTED" if remaining == 0 else "ACTIVE"
    con.execute(
        "UPDATE authority_grants SET remaining_uses=?,status=? WHERE grant_id=?",
        (remaining, status, grant_id),
    )
    updated = con.execute(
        "SELECT * FROM authority_grants WHERE grant_id=?", (grant_id,)
    ).fetchone()
    after_root = _aggregate_root("AuthorityGrant", grant_id, _grant_state(updated))
    event = append_hub_event(
        con,
        request=request,
        capability_id=capability_id,
        aggregate_version=version + 1,
        before_root=before_root,
        after_root=after_root,
        event_result={"authorityUse": "CONSUMED", "grantId": grant_id, "remainingUses": remaining},
        occurred_at=recorded_at,
        aggregate_type="AuthorityGrant",
        aggregate_id=grant_id,
    )
    return event["eventId"]


def _finalize_mutation(
    con: sqlite3.Connection,
    *,
    request: dict[str, Any],
    capability: sqlite3.Row,
    basis: dict[str, Any],
    request_hash: str,
    recorded_at: str,
    before_root: str,
    after_root: str,
    aggregate_version: int,
    created_ids: list[str],
    event_result: dict[str, Any],
    event_link: tuple[str, tuple[Any, ...]] | None = None,
    extra_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    authority_use_event_id = _consume_grant_authority(
        con,
        request=request,
        capability_id=capability["capability_id"],
        basis=basis,
        recorded_at=recorded_at,
    )
    if authority_use_event_id:
        event_result = {**event_result, "authorityUseEventId": authority_use_event_id}
    event = append_hub_event(
        con,
        request=request,
        capability_id=capability["capability_id"],
        aggregate_version=aggregate_version,
        before_root=before_root,
        after_root=after_root,
        event_result=event_result,
        occurred_at=recorded_at,
    )
    if basis["kind"] == "EXACT_AUTHOR_ACTION":
        changed = con.execute(
            """UPDATE exact_author_actions SET consumed_event_id=?
               WHERE action_id=? AND consumed_event_id IS NULL""",
            (event["eventId"], basis["id"]),
        ).rowcount
        if changed != 1:
            raise PostOfficeError("PON_AUTHORIZATION_DENIED", "Exact author action was not consumable", {})
    if event_link:
        sql, parameters = event_link
        con.execute(sql, (event["eventId"], *parameters))
    operational_root = _operational_state(con)["operationalStateRoot"]
    return {
        "schemaVersion": "1",
        "requestId": request["requestId"],
        "operation": request["operation"],
        "ok": True,
        "beforeRoot": before_root,
        "afterRoot": after_root,
        "aggregateVersion": aggregate_version,
        "createdIds": created_ids,
        "warnings": [],
        "eventId": event["eventId"],
        "receipt": {
            "receiptId": "PON-RECEIPT-" + uuid.uuid4().hex,
            "requestSha256": request_hash,
            "recordedAt": recorded_at,
            "stateRoot": operational_root,
            "mutationEventId": event["eventId"],
        },
        **(extra_result or {}),
    }


def _execute_mutation(
    con: sqlite3.Connection,
    *,
    request: dict[str, Any],
    capability: sqlite3.Row,
    request_hash: str,
    recorded_at: str,
) -> dict[str, Any]:
    operation = request["operation"]
    parameters = request["parameters"]
    aggregate_id = request["aggregate"]["id"]

    if operation in {"project.planCreate", "task.planCreate"}:
        aggregate_type = "ProvisioningPlan"
        before_root = _aggregate_root(aggregate_type, aggregate_id, None)
        if con.execute("SELECT 1 FROM provisioning_plans WHERE plan_id=?", (aggregate_id,)).fetchone():
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Provisioning plan already exists", {})
        _require_aggregate(
            request, aggregate_type=aggregate_type, aggregate_id=aggregate_id,
            actual_version=0, actual_root=before_root,
        )
        target_type = "Project" if operation == "project.planCreate" else "Task"
        target_id = parameters["projectId"] if target_type == "Project" else parameters["taskId"]
        if con.execute(
            "SELECT 1 FROM provisioning_plans WHERE aggregate_type=? AND aggregate_id=?",
            (target_type, target_id),
        ).fetchone():
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Target already has a provisioning plan", {})
        if target_type == "Project":
            if con.execute("SELECT 1 FROM projects WHERE project_id=? OR code=?", (target_id, parameters["projectCode"])).fetchone():
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Project identity or code already exists", {})
            domains = parameters["mailDomains"]
            if len(set(domains)) != len(domains):
                raise PostOfficeError("PON_INPUT_INVALID", "Project mail domains must be unique", {})
            collision = con.execute(
                "SELECT mail_domain FROM project_mail_domains WHERE mail_domain IN (%s) LIMIT 1"
                % ",".join("?" for _ in domains), domains,
            ).fetchone()
            if collision:
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Project mail domain is already owned", {"mailDomain": collision[0]})
            requested_resources = [f"project:{target_id}", *[f"mail-domain:{item}" for item in sorted(domains)]]
            scope_facts: dict[str, set[str]] = {}
        else:
            project = con.execute("SELECT * FROM projects WHERE project_id=?", (parameters["projectId"],)).fetchone()
            if not project or project["status"] != "ACTIVE":
                raise PostOfficeError("PON_INPUT_INVALID", "Task project is not active", {})
            if con.execute("SELECT 1 FROM tasks WHERE task_id=?", (target_id,)).fetchone():
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Task identity already exists", {})
            mutable_ids = parameters.get("mutablePackageIds", [])
            if parameters["taskKind"] == "PACKAGE_DEVELOPMENT" and len(mutable_ids) != 1:
                raise PostOfficeError("PON_INPUT_INVALID", "Package-development task requires exactly one mutable package", {})
            if parameters["taskKind"] != "PACKAGE_DEVELOPMENT" and mutable_ids:
                raise PostOfficeError("PON_INPUT_INVALID", "Only package-development tasks may mutate a package", {})
            if mutable_ids:
                package = con.execute("SELECT * FROM software_packages WHERE package_id=?", (mutable_ids[0],)).fetchone()
                if not package or package["owning_project_id"] != parameters["projectId"]:
                    raise PostOfficeError("PON_INPUT_INVALID", "Mutable package does not belong to the task project", {})
            grant = con.execute("SELECT * FROM authority_grants WHERE grant_id=?", (parameters["authorityGrantId"],)).fetchone()
            if not grant or grant["status"] != "ACTIVE":
                raise PostOfficeError("PON_INPUT_INVALID", "Task authority grant is not active", {})
            grant_scope = json.loads(grant["scope_json"])
            if parameters["projectId"] not in grant_scope.get("projectIds", []) and not set(mutable_ids).intersection(grant_scope.get("packageIds", [])):
                raise PostOfficeError("PON_INPUT_INVALID", "Task authority grant does not cover its project or package", {})
            requested_resources = [f"task:{target_id}", *[f"package:{item}" for item in mutable_ids]]
            scope_facts = {"projectIds": {parameters["projectId"]}, "packageIds": set(mutable_ids)}
        basis = _prepare_mutation_authority(
            con, request=request, capability=capability, request_hash=request_hash,
            recorded_at=recorded_at, scope_facts=scope_facts,
        )
        payload = dict(parameters)
        plan_root = sha256_json(
            {"aggregateType": target_type, "aggregateId": target_id, "payload": payload,
             "requestedResources": requested_resources}
        )
        authority_id = request["authority"].get("authorityGrantId") or request["authority"].get("exactAuthorActionId")
        draft = {
            "id": aggregate_id, "aggregateType": target_type, "aggregateId": target_id,
            "authorityId": authority_id, "requestedResources": requested_resources,
            "planRoot": plan_root, "stage": "PLAN", "payload": payload,
        }
        after_root = _aggregate_root(aggregate_type, aggregate_id, draft)
        con.execute(
            """INSERT INTO provisioning_plans(
               plan_id,aggregate_type,aggregate_id,authority_id,requested_resources_json,payload_json,
               plan_root,stage,aggregate_version,aggregate_root,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (aggregate_id, target_type, target_id, authority_id,
             canonical_json_bytes(requested_resources).decode("utf-8"),
             canonical_json_bytes(payload).decode("utf-8"), plan_root.removeprefix("sha256:"),
             "PLAN", 1, after_root.removeprefix("sha256:"), recorded_at),
        )
        row = con.execute("SELECT * FROM provisioning_plans WHERE plan_id=?", (aggregate_id,)).fetchone()
        result = _finalize_mutation(
            con, request=request, capability=capability, basis=basis,
            request_hash=request_hash, recorded_at=recorded_at,
            before_root=before_root, after_root=after_root, aggregate_version=1,
            created_ids=[aggregate_id], event_result={"state": "PLAN", "targetId": target_id},
            event_link=("UPDATE provisioning_plans SET created_event_id=? WHERE plan_id=?", (aggregate_id,)),
            extra_result={"provisioningPlan": _plan_state(row)},
        )
        return result

    if operation in {"project.create", "task.create"}:
        plan = con.execute(
            "SELECT * FROM provisioning_plans WHERE plan_id=?", (parameters["approvedPlanId"],)
        ).fetchone()
        if not plan or plan["stage"] != "PLAN":
            raise PostOfficeError("PON_INPUT_INVALID", "Provisioning plan is absent or already consumed", {})
        if parameters["planRoot"].removeprefix("sha256:") != plan["plan_root"]:
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Approved provisioning plan root differs", {})
        expected_type = "Project" if operation == "project.create" else "Task"
        if plan["aggregate_type"] != expected_type or aggregate_id != plan["aggregate_id"]:
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Provisioning plan target differs", {})
        aggregate_type = expected_type
        before_root = _aggregate_root(aggregate_type, aggregate_id, None)
        _require_aggregate(
            request, aggregate_type=aggregate_type, aggregate_id=aggregate_id,
            actual_version=0, actual_root=before_root,
        )
        payload = json.loads(plan["payload_json"])
        scope_facts = {"projectIds": {aggregate_id if expected_type == "Project" else payload["projectId"]}}
        if expected_type == "Task":
            scope_facts["taskIds"] = {aggregate_id}
            scope_facts["packageIds"] = set(payload.get("mutablePackageIds", []))
        basis = _prepare_mutation_authority(
            con, request=request, capability=capability, request_hash=request_hash,
            recorded_at=recorded_at, scope_facts=scope_facts,
        )
        if expected_type == "Project":
            con.execute(
                """INSERT INTO projects(project_id,code,display_name,kind,status,local_project_root,
                   local_cas_root,local_backup_root,aggregate_version,aggregate_root,policy_root,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (aggregate_id, payload["projectCode"], payload["displayName"], payload["kind"], "ACTIVE",
                 payload["localProjectRoot"], payload["localCasRoot"], payload["localBackupRoot"],
                 1, "0" * 64, payload.get("policyRoot", "").removeprefix("sha256:") or None, recorded_at),
            )
            for domain in sorted(payload["mailDomains"]):
                con.execute("INSERT INTO project_mail_domains(project_id,mail_domain) VALUES(?,?)", (aggregate_id, domain))
            row = con.execute("SELECT * FROM projects WHERE project_id=?", (aggregate_id,)).fetchone()
            detail_name, detail = "project", _project_state(con, row)
        else:
            mutable = payload.get("mutablePackageIds", [])
            con.execute(
                """INSERT INTO tasks(task_id,project_id,task_kind,objective,mutable_package_id,
                   acceptance_contract_root,authority_grant_id,state,aggregate_version,aggregate_root,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (aggregate_id, payload["projectId"], payload["taskKind"], payload["objective"],
                 mutable[0] if mutable else None, payload["acceptanceContractRoot"].removeprefix("sha256:"),
                 payload["authorityGrantId"], "PROVISIONING", 1, "0" * 64, recorded_at),
            )
            row = con.execute("SELECT * FROM tasks WHERE task_id=?", (aggregate_id,)).fetchone()
            detail_name, detail = "task", _task_state(con, row)
        after_root = _aggregate_root(aggregate_type, aggregate_id, detail)
        table = "projects" if expected_type == "Project" else "tasks"
        id_column = "project_id" if expected_type == "Project" else "task_id"
        con.execute(f"UPDATE {table} SET aggregate_root=? WHERE {id_column}=?", (after_root.removeprefix("sha256:"), aggregate_id))
        result = _finalize_mutation(
            con, request=request, capability=capability, basis=basis,
            request_hash=request_hash, recorded_at=recorded_at, before_root=before_root,
            after_root=after_root, aggregate_version=1, created_ids=[aggregate_id],
            event_result={"state": detail.get("status", detail.get("state")), "provisioningPlanId": plan["plan_id"]},
            event_link=(f"UPDATE {table} SET created_event_id=? WHERE {id_column}=?", (aggregate_id,)),
            extra_result={detail_name: detail},
        )
        plan_before = _aggregate_root("ProvisioningPlan", plan["plan_id"], _plan_state(plan))
        plan_version = _aggregate_position(
            con, aggregate_type="ProvisioningPlan", aggregate_id=plan["plan_id"], current_root=plan_before
        )
        con.execute(
            "UPDATE provisioning_plans SET stage='COMMITTED',aggregate_version=?,committed_event_id=?,committed_at=? WHERE plan_id=?",
            (plan_version + 1, result["eventId"], recorded_at, plan["plan_id"]),
        )
        committed = con.execute("SELECT * FROM provisioning_plans WHERE plan_id=?", (plan["plan_id"],)).fetchone()
        plan_after = _aggregate_root("ProvisioningPlan", plan["plan_id"], _plan_state(committed))
        con.execute("UPDATE provisioning_plans SET aggregate_root=? WHERE plan_id=?", (plan_after.removeprefix("sha256:"), plan["plan_id"]))
        plan_event = append_hub_event(
            con, request=request, capability_id=capability["capability_id"], aggregate_version=plan_version + 1,
            before_root=plan_before, after_root=plan_after,
            event_result={"state": "COMMITTED", "targetEventId": result["eventId"]},
            occurred_at=recorded_at, aggregate_type="ProvisioningPlan", aggregate_id=plan["plan_id"],
        )
        result["receipt"]["stateRoot"] = _operational_state(con)["operationalStateRoot"]
        result["warnings"] = []
        return result

    if operation in {"project.update", "project.pause", "project.archive"}:
        project_id = parameters["projectId"]
        row = con.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if not row:
            raise PostOfficeError("PON_INPUT_INVALID", "Project does not exist", {})
        before_detail = _project_state(con, row)
        before_root = _aggregate_root("Project", project_id, before_detail)
        version = _aggregate_position(con, aggregate_type="Project", aggregate_id=project_id, current_root=before_root)
        _require_aggregate(request, aggregate_type="Project", aggregate_id=project_id, actual_version=version, actual_root=before_root)
        basis = _prepare_mutation_authority(
            con, request=request, capability=capability, request_hash=request_hash,
            recorded_at=recorded_at, scope_facts={"projectIds": {project_id}},
        )
        if operation == "project.update":
            if row["status"] not in {"ACTIVE", "PAUSED"}:
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Project is not updateable", {})
            changes = parameters["changes"]
            if "displayName" in changes:
                con.execute("UPDATE projects SET display_name=? WHERE project_id=?", (changes["displayName"], project_id))
            if "policyRoot" in changes:
                con.execute("UPDATE projects SET policy_root=? WHERE project_id=?", (changes["policyRoot"].removeprefix("sha256:"), project_id))
            if "mailDomains" in changes:
                domains = changes["mailDomains"]
                if len(set(domains)) != len(domains):
                    raise PostOfficeError("PON_INPUT_INVALID", "Project mail domains must be unique", {})
                placeholders = ",".join("?" for _ in domains)
                collision = con.execute(
                    f"SELECT mail_domain FROM project_mail_domains WHERE project_id<>? AND mail_domain IN ({placeholders}) LIMIT 1",
                    (project_id, *domains),
                ).fetchone()
                if collision:
                    raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Mail domain is owned by another project", {"mailDomain": collision[0]})
                con.execute("DELETE FROM project_mail_domains WHERE project_id=?", (project_id,))
                for domain in sorted(domains):
                    con.execute("INSERT INTO project_mail_domains(project_id,mail_domain) VALUES(?,?)", (project_id, domain))
        elif operation == "project.pause":
            if row["status"] != "ACTIVE":
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Only an active project may be paused", {})
            con.execute("UPDATE projects SET status='PAUSED' WHERE project_id=?", (project_id,))
        else:
            if row["status"] != "PAUSED":
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Project must be paused before archival", {})
            open_tasks = int(con.execute("SELECT COUNT(*) FROM tasks WHERE project_id=? AND state NOT IN ('CLOSED','CANCELLED')", (project_id,)).fetchone()[0])
            if open_tasks:
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Project has open tasks", {"openTaskCount": open_tasks})
            con.execute("UPDATE projects SET status='ARCHIVED' WHERE project_id=?", (project_id,))
        updated = con.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
        detail = _project_state(con, updated)
        after_root = _aggregate_root("Project", project_id, detail)
        con.execute("UPDATE projects SET aggregate_version=?,aggregate_root=? WHERE project_id=?", (version + 1, after_root.removeprefix("sha256:"), project_id))
        return _finalize_mutation(
            con, request=request, capability=capability, basis=basis, request_hash=request_hash,
            recorded_at=recorded_at, before_root=before_root, after_root=after_root,
            aggregate_version=version + 1, created_ids=[],
            event_result={"state": detail["status"], "reason": parameters.get("reason")},
            extra_result={"project": detail},
        )

    if operation in {"task.bindEndpoint", "task.activate", "task.block", "task.moveProject", "task.recordResponse", "task.review", "task.close"}:
        task_id = parameters["taskId"]
        row = con.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            raise PostOfficeError("PON_INPUT_INVALID", "Task does not exist", {})
        before_detail = _task_state(con, row)
        before_root = _aggregate_root("Task", task_id, before_detail)
        version = _aggregate_position(con, aggregate_type="Task", aggregate_id=task_id, current_root=before_root)
        _require_aggregate(request, aggregate_type="Task", aggregate_id=task_id, actual_version=version, actual_root=before_root)
        scope_facts = {"projectIds": {row["project_id"]}, "taskIds": {task_id}}
        if row["mutable_package_id"]:
            scope_facts["packageIds"] = {row["mutable_package_id"]}
        basis = _prepare_mutation_authority(
            con, request=request, capability=capability, request_hash=request_hash,
            recorded_at=recorded_at, scope_facts=scope_facts,
        )
        link: tuple[str, tuple[Any, ...]] | None = None
        event_result: dict[str, Any]
        if operation == "task.bindEndpoint":
            if row["state"] not in {"PROVISIONING", "BLOCKED"}:
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Task is not awaiting endpoint binding", {})
            endpoint = con.execute("SELECT * FROM endpoints WHERE endpoint_id=?", (parameters["endpointId"],)).fetchone()
            if not endpoint or endpoint["status"] != "ACTIVE" or endpoint["task_id"] != task_id or endpoint["project_id"] != row["project_id"]:
                raise PostOfficeError("PON_INPUT_INVALID", "Endpoint is not the active endpoint for this task", {})
            mailbox = con.execute(
                """SELECT * FROM mailboxes WHERE endpoint_id=? AND generation=? AND status='ACTIVE'
                   ORDER BY mailbox_id LIMIT 1""",
                (endpoint["endpoint_id"], parameters["mailboxGeneration"]),
            ).fetchone()
            if not mailbox:
                raise PostOfficeError("PON_INPUT_INVALID", "Requested active mailbox generation does not exist", {})
            if con.execute("SELECT 1 FROM task_endpoint_bindings WHERE task_id=? OR endpoint_id=?", (task_id, endpoint["endpoint_id"])).fetchone():
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Task or endpoint is already bound", {})
            con.execute(
                "INSERT INTO task_endpoint_bindings(task_id,endpoint_id,mailbox_id,mailbox_generation,bound_event_id,bound_at) VALUES(?,?,?,?,?,?)",
                (task_id, endpoint["endpoint_id"], mailbox["mailbox_id"], int(mailbox["generation"]), "PENDING", recorded_at),
            )
            con.execute("UPDATE tasks SET state='READY' WHERE task_id=?", (task_id,))
            link = ("UPDATE task_endpoint_bindings SET bound_event_id=? WHERE task_id=?", (task_id,))
            event_result = {"state": "READY", "endpointId": endpoint["endpoint_id"], "mailboxId": mailbox["mailbox_id"], "mailboxGeneration": int(mailbox["generation"])}
        elif operation == "task.activate":
            if row["state"] not in {"READY", "BLOCKED", "CORRECTION_REQUIRED"}:
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Task is not activatable", {})
            if not con.execute("SELECT 1 FROM task_endpoint_bindings WHERE task_id=?", (task_id,)).fetchone():
                raise PostOfficeError("PON_INPUT_INVALID", "Task has no endpoint binding", {})
            con.execute("UPDATE tasks SET state='ACTIVE' WHERE task_id=?", (task_id,))
            event_result = {"state": "ACTIVE"}
        elif operation == "task.block":
            if row["state"] not in {"PROVISIONING", "READY", "ACTIVE", "CORRECTION_REQUIRED"}:
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Task cannot be blocked from its current state", {})
            con.execute("UPDATE tasks SET state='BLOCKED' WHERE task_id=?", (task_id,))
            event_result = {"state": "BLOCKED", "reason": parameters["reason"]}
        elif operation == "task.moveProject":
            if row["state"] not in {"PROVISIONING", "READY", "BLOCKED"}:
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Active or decided task cannot move project", {})
            if con.execute("SELECT 1 FROM task_endpoint_bindings WHERE task_id=?", (task_id,)).fetchone():
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Unbind the task endpoint before moving project", {})
            destination = con.execute("SELECT * FROM projects WHERE project_id=?", (parameters["destinationProjectId"],)).fetchone()
            if not destination or destination["status"] != "ACTIVE":
                raise PostOfficeError("PON_INPUT_INVALID", "Destination project is not active", {})
            if row["mutable_package_id"]:
                package = con.execute("SELECT * FROM software_packages WHERE package_id=?", (row["mutable_package_id"],)).fetchone()
                if not package or package["owning_project_id"] != destination["project_id"]:
                    raise PostOfficeError("PON_INPUT_INVALID", "Mutable package does not belong to destination project", {})
            con.execute("UPDATE tasks SET project_id=? WHERE task_id=?", (destination["project_id"], task_id))
            event_result = {"state": row["state"], "fromProjectId": row["project_id"], "toProjectId": destination["project_id"], "reason": parameters["reason"]}
        elif operation == "task.recordResponse":
            if row["state"] not in {"ACTIVE", "BLOCKED"}:
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Task is not awaiting a response", {})
            response_id = "PON-RESPONSE-" + sha256_bytes(f"{task_id}:{parameters['messageId']}".encode("utf-8"))[:24]
            con.execute(
                "INSERT INTO task_responses(response_id,task_id,message_id,evidence_roots_json,recorded_event_id,recorded_at) VALUES(?,?,?,?,?,?)",
                (response_id, task_id, parameters["messageId"], canonical_json_bytes(parameters["evidenceRoots"]).decode("utf-8"), "PENDING", recorded_at),
            )
            con.execute("UPDATE tasks SET state='RESPONSE_RETURNED' WHERE task_id=?", (task_id,))
            link = ("UPDATE task_responses SET recorded_event_id=? WHERE response_id=?", (response_id,))
            event_result = {"state": "RESPONSE_RETURNED", "responseId": response_id, "messageId": parameters["messageId"]}
        elif operation == "task.review":
            if row["state"] != "RESPONSE_RETURNED":
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Task has no unreviewed response", {})
            decision_state = parameters["decision"]
            review_id = "PON-TASK-REVIEW-" + uuid.uuid4().hex
            con.execute(
                "INSERT INTO task_reviews(review_id,task_id,decision,rationale,recorded_event_id,recorded_at) VALUES(?,?,?,?,?,?)",
                (review_id, task_id, decision_state, parameters["rationale"], "PENDING", recorded_at),
            )
            con.execute("UPDATE tasks SET state=? WHERE task_id=?", (decision_state, task_id))
            link = ("UPDATE task_reviews SET recorded_event_id=? WHERE review_id=?", (review_id,))
            event_result = {"state": decision_state, "reviewId": review_id, "rationale": parameters["rationale"]}
        else:
            if row["state"] not in {"ACCEPTED", "REJECTED", "CANCELLED"}:
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Task requires a terminal decision before closure", {})
            con.execute("UPDATE tasks SET state='CLOSED' WHERE task_id=?", (task_id,))
            event_result = {"state": "CLOSED", "summary": parameters["summary"]}
        updated = con.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        detail = _task_state(con, updated)
        after_root = _aggregate_root("Task", task_id, detail)
        con.execute("UPDATE tasks SET aggregate_version=?,aggregate_root=? WHERE task_id=?", (version + 1, after_root.removeprefix("sha256:"), task_id))
        return _finalize_mutation(
            con, request=request, capability=capability, basis=basis, request_hash=request_hash,
            recorded_at=recorded_at, before_root=before_root, after_root=after_root,
            aggregate_version=version + 1, created_ids=[], event_result=event_result,
            event_link=link, extra_result={"task": detail},
        )

    if operation in {"package.register", "package.registerVersion", "package.rehome", "package.deprecate"}:
        package_id = parameters.get("packageId") or parameters.get("package", {}).get("id") or aggregate_id
        row = con.execute("SELECT * FROM software_packages WHERE package_id=?", (package_id,)).fetchone()
        if operation == "package.register":
            if row:
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Software package already exists", {})
            package = parameters["package"]
            if aggregate_id != package["id"]:
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Package aggregate ID differs", {})
            project = con.execute("SELECT * FROM projects WHERE project_id=?", (package["owningProjectId"],)).fetchone()
            if not project or project["status"] != "ACTIVE":
                raise PostOfficeError("PON_INPUT_INVALID", "Owning project is not active", {})
            before_root = _aggregate_root("SoftwarePackage", package_id, None)
            _require_aggregate(request, aggregate_type="SoftwarePackage", aggregate_id=package_id, actual_version=0, actual_root=before_root)
            basis = _prepare_mutation_authority(
                con, request=request, capability=capability, request_hash=request_hash,
                recorded_at=recorded_at, scope_facts={"projectIds": {project["project_id"]}},
            )
            con.execute(
                """INSERT INTO software_packages(package_id,display_name,owning_project_id,status,
                   aggregate_version,aggregate_root,created_at,source_location,licence)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (package_id, package["displayName"], project["project_id"], "PROPOSED", 1, "0" * 64,
                 recorded_at, package.get("sourceLocation"), package.get("licence")),
            )
            for interface_id in sorted(set(package["providedInterfaceIds"])):
                con.execute("INSERT INTO package_interfaces VALUES(?,?,?)", (package_id, interface_id, "PROVIDES"))
            for interface_id in sorted(set(package["requiredInterfaceIds"])):
                con.execute("INSERT INTO package_interfaces VALUES(?,?,?)", (package_id, interface_id, "REQUIRES"))
            row = con.execute("SELECT * FROM software_packages WHERE package_id=?", (package_id,)).fetchone()
            detail = _package_state(con, row)
            after_root = _aggregate_root("SoftwarePackage", package_id, detail)
            con.execute("UPDATE software_packages SET aggregate_root=? WHERE package_id=?", (after_root, package_id))
            return _finalize_mutation(
                con, request=request, capability=capability, basis=basis, request_hash=request_hash,
                recorded_at=recorded_at, before_root=before_root, after_root=after_root,
                aggregate_version=1, created_ids=[package_id], event_result={"state": "PROPOSED"},
                event_link=("UPDATE software_packages SET created_event_id=? WHERE package_id=?", (package_id,)),
            )
        if not row:
            raise PostOfficeError("PON_INPUT_INVALID", "Software package does not exist", {})
        before_detail = _package_state(con, row)
        before_root = _aggregate_root("SoftwarePackage", package_id, before_detail)
        version = _aggregate_position(con, aggregate_type="SoftwarePackage", aggregate_id=package_id, current_root=before_root)
        _require_aggregate(request, aggregate_type="SoftwarePackage", aggregate_id=package_id, actual_version=version, actual_root=before_root)
        scope_facts = {"projectIds": {row["owning_project_id"]}, "packageIds": {package_id}}
        basis = _prepare_mutation_authority(
            con, request=request, capability=capability, request_hash=request_hash,
            recorded_at=recorded_at, scope_facts=scope_facts,
        )
        if operation == "package.registerVersion":
            if row["status"] in {"DEPRECATED", "RETIRED"}:
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Inactive package cannot accept a version", {})
            content_root = parameters["contentRoot"].removeprefix("sha256:")
            if not con.execute("SELECT 1 FROM storage_copies WHERE content_sha256=?", (content_root,)).fetchone():
                raise PostOfficeError("PON_CUSTODY_NOT_VERIFIED", "Package version has no verified local custody", {"contentRoot": content_root})
            con.execute(
                "INSERT INTO package_versions(package_id,version,content_root,state,created_at) VALUES(?,?,?,?,?)",
                (package_id, parameters["version"], content_root, "ACCEPTED", recorded_at),
            )
            con.execute("UPDATE software_packages SET status='ACCEPTED' WHERE package_id=?", (package_id,))
            event_result = {"state": "ACCEPTED", "version": parameters["version"], "contentRoot": content_root}
        elif operation == "package.rehome":
            destination = con.execute("SELECT * FROM projects WHERE project_id=?", (parameters["destinationProjectId"],)).fetchone()
            if not destination or destination["status"] != "ACTIVE":
                raise PostOfficeError("PON_INPUT_INVALID", "Destination project is not active", {})
            blockers = [str(item[0]) for item in con.execute("SELECT task_id FROM tasks WHERE mutable_package_id=? AND state NOT IN ('CLOSED','CANCELLED') ORDER BY task_id", (package_id,))]
            preview = {"package": before_detail, "destinationProjectId": destination["project_id"], "blockingTaskIds": blockers, "ready": not blockers}
            expected_root = sha256_json(preview)
            expected_id = "PON-REHOME-" + expected_root[:24]
            if blockers or parameters["planRoot"].removeprefix("sha256:") != expected_root or parameters["approvedPlanId"] != expected_id:
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Package rehome preview is stale or blocked", {"expectedPlanId": expected_id, "expectedPlanRoot": expected_root, "blockingTaskIds": blockers})
            con.execute("UPDATE software_packages SET owning_project_id=? WHERE package_id=?", (destination["project_id"], package_id))
            event_result = {"state": row["status"], "fromProjectId": row["owning_project_id"], "toProjectId": destination["project_id"]}
        else:
            if row["status"] in {"DEPRECATED", "RETIRED"}:
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Package is already inactive", {})
            if parameters.get("version"):
                changed = con.execute("UPDATE package_versions SET state='DEPRECATED' WHERE package_id=? AND version=?", (package_id, parameters["version"])).rowcount
                if changed != 1:
                    raise PostOfficeError("PON_INPUT_INVALID", "Package version does not exist", {})
            con.execute("UPDATE software_packages SET status='DEPRECATED' WHERE package_id=?", (package_id,))
            event_result = {"state": "DEPRECATED", "reason": parameters["reason"], "replacementPackageId": parameters.get("replacementPackageId")}
        updated = con.execute("SELECT * FROM software_packages WHERE package_id=?", (package_id,)).fetchone()
        detail = _package_state(con, updated)
        after_root = _aggregate_root("SoftwarePackage", package_id, detail)
        con.execute("UPDATE software_packages SET aggregate_version=?,aggregate_root=? WHERE package_id=?", (version + 1, after_root, package_id))
        return _finalize_mutation(
            con, request=request, capability=capability, basis=basis, request_hash=request_hash,
            recorded_at=recorded_at, before_root=before_root, after_root=after_root,
            aggregate_version=version + 1, created_ids=[], event_result=event_result,
        )

    if operation in {"interface.register", "interface.deprecate"}:
        definition = parameters.get("interface", {})
        interface_id = parameters.get("interfaceId") or definition.get("id") or aggregate_id
        if aggregate_id != interface_id:
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Interface aggregate ID differs", {})
        rows = list(con.execute("SELECT * FROM interfaces WHERE interface_id=? ORDER BY version", (interface_id,)))
        before_detail = _interface_family_state(con, interface_id) if rows else None
        before_root = _aggregate_root("Interface", interface_id, before_detail)
        version = _aggregate_position(con, aggregate_type="Interface", aggregate_id=interface_id, current_root=before_root)
        _require_aggregate(request, aggregate_type="Interface", aggregate_id=interface_id, actual_version=version, actual_root=before_root)
        if operation == "interface.register":
            steward = con.execute("SELECT * FROM software_packages WHERE package_id=?", (definition["stewardId"],)).fetchone()
            if not steward or steward["status"] in {"DEPRECATED", "RETIRED"}:
                raise PostOfficeError("PON_INPUT_INVALID", "Interface steward package is not active", {})
            participant_ids = set(definition["providerIds"] + definition["consumerIds"] + [definition["stewardId"]])
            placeholders = ",".join("?" for _ in participant_ids)
            found = {str(item[0]) for item in con.execute(f"SELECT package_id FROM software_packages WHERE package_id IN ({placeholders})", tuple(participant_ids))}
            if found != participant_ids:
                raise PostOfficeError("PON_INPUT_INVALID", "Interface participant package is unknown", {"missingPackageIds": sorted(participant_ids - found)})
            if con.execute("SELECT 1 FROM interfaces WHERE interface_id=? AND version=?", (interface_id, definition["version"])).fetchone():
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Interface version already exists", {})
            scope_facts = {"projectIds": {steward["owning_project_id"]}, "packageIds": participant_ids}
        else:
            target = con.execute("SELECT * FROM interfaces WHERE interface_id=? AND version=?", (interface_id, parameters["version"])).fetchone()
            if not target:
                raise PostOfficeError("PON_INPUT_INVALID", "Interface version does not exist", {})
            steward = con.execute("SELECT * FROM software_packages WHERE package_id=?", (target["steward_package_id"],)).fetchone()
            scope_facts = {"projectIds": {steward["owning_project_id"]}, "packageIds": {steward["package_id"]}}
        basis = _prepare_mutation_authority(
            con, request=request, capability=capability, request_hash=request_hash,
            recorded_at=recorded_at, scope_facts=scope_facts,
        )
        if operation == "interface.register":
            con.execute(
                """INSERT INTO interfaces(interface_id,version,steward_package_id,status,schema_root,
                   fixture_root,human_guide,created_event_id) VALUES(?,?,?,?,?,?,?,?)""",
                (interface_id, definition["version"], definition["stewardId"], definition["status"],
                 definition.get("schemaRoot", "").removeprefix("sha256:") or None,
                 definition.get("fixtureRoot", "").removeprefix("sha256:") or None,
                 definition["humanGuide"], None),
            )
            for package_id in sorted(set(definition["providerIds"])):
                con.execute("INSERT INTO interface_participants VALUES(?,?,?,?)", (interface_id, definition["version"], package_id, "PROVIDER"))
            for package_id in sorted(set(definition["consumerIds"])):
                con.execute("INSERT INTO interface_participants VALUES(?,?,?,?)", (interface_id, definition["version"], package_id, "CONSUMER"))
            event_result = {"state": definition["status"], "version": definition["version"]}
        else:
            if target["status"] in {"DEPRECATED", "RETIRED"}:
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Interface version is already inactive", {})
            con.execute(
                "UPDATE interfaces SET status='DEPRECATED',replacement_interface_id=? WHERE interface_id=? AND version=?",
                (parameters.get("replacementInterfaceId"), interface_id, parameters["version"]),
            )
            event_result = {"state": "DEPRECATED", "version": parameters["version"], "reason": parameters["reason"]}
        after_detail = _interface_family_state(con, interface_id)
        after_root = _aggregate_root("Interface", interface_id, after_detail)
        return _finalize_mutation(
            con, request=request, capability=capability, basis=basis, request_hash=request_hash,
            recorded_at=recorded_at, before_root=before_root, after_root=after_root,
            aggregate_version=version + 1, created_ids=[interface_id] if not rows else [], event_result=event_result,
        )

    workflow_types = {
        "capabilityRequest.create": "CapabilityRequest", "capabilityRequest.triage": "CapabilityRequest",
        "capabilityRequest.fulfil": "CapabilityRequest", "changeSet.create": "ChangeSet",
        "changeSet.addTask": "ChangeSet", "changeSet.startIntegration": "ChangeSet",
        "changeSet.decide": "ChangeSet", "integration.record": "IntegrationCandidate",
        "contextBundle.build": "ContextBundle",
    }
    if operation in workflow_types:
        aggregate_type = workflow_types[operation]
        record_id = (
            parameters.get("capabilityRequestId") or parameters.get("changeSetId")
            or parameters.get("candidate", {}).get("id") or aggregate_id
        )
        if aggregate_id != record_id:
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Workflow aggregate ID differs", {})
        row = con.execute(
            "SELECT * FROM workflow_aggregates WHERE aggregate_type=? AND aggregate_id=?",
            (aggregate_type, record_id),
        ).fetchone()
        before_detail = _workflow_state(row) if row else None
        before_root = _aggregate_root(aggregate_type, record_id, before_detail)
        version = _aggregate_position(con, aggregate_type=aggregate_type, aggregate_id=record_id, current_root=before_root)
        _require_aggregate(request, aggregate_type=aggregate_type, aggregate_id=record_id, actual_version=version, actual_root=before_root)
        created = False
        if operation == "capabilityRequest.create":
            if row:
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Capability request already exists", {})
            source = parameters["request"]
            if source["id"] != record_id:
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Capability request ID differs", {})
            project = con.execute("SELECT * FROM projects WHERE project_id=?", (source["requestingProjectId"],)).fetchone()
            task_id = source.get("requestingTaskId")
            if not project or project["status"] != "ACTIVE":
                raise PostOfficeError("PON_INPUT_INVALID", "Requesting project is not active", {})
            if task_id:
                task = con.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
                if not task or task["project_id"] != project["project_id"]:
                    raise PostOfficeError("PON_INPUT_INVALID", "Requesting task does not belong to project", {})
            detail = {"schemaVersion": "1", **source, "status": "OPEN", "linkedTaskIds": [], "fulfilmentEvidence": []}
            project_id = project["project_id"]
            scope_facts = {"projectIds": {project_id}, "taskIds": {task_id} if task_id else set()}
            created = True
        elif operation == "changeSet.create":
            if row:
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Change set already exists", {})
            task_ids = parameters["taskIds"]
            if len(set(task_ids)) != len(task_ids):
                raise PostOfficeError("PON_INPUT_INVALID", "Change-set tasks must be unique", {})
            tasks = list(con.execute("SELECT * FROM tasks WHERE task_id IN (%s)" % ",".join("?" for _ in task_ids), task_ids))
            if len(tasks) != len(task_ids) or len({item["project_id"] for item in tasks}) != 1:
                raise PostOfficeError("PON_INPUT_INVALID", "Change-set tasks must exist in one project", {})
            project_id = tasks[0]["project_id"]
            task_id = None
            detail = {"schemaVersion": "1", "id": record_id, "purpose": parameters["purpose"],
                      "taskIds": sorted(task_ids), "integrationContractRoot": parameters["integrationContractRoot"],
                      "dependencyEdges": [], "state": "PROPOSED"}
            scope_facts = {"projectIds": {project_id}, "taskIds": set(task_ids)}
            created = True
        elif operation == "integration.record":
            if row:
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Integration candidate already exists", {})
            source = parameters["candidate"]
            project = con.execute("SELECT * FROM projects WHERE project_id=?", (source["projectId"],)).fetchone()
            if not project or project["status"] != "ACTIVE":
                raise PostOfficeError("PON_INPUT_INVALID", "Integration project is not active", {})
            for package_id, content_root in source["packageRoots"].items():
                package = con.execute("SELECT * FROM software_packages WHERE package_id=?", (package_id,)).fetchone()
                if not package or package["owning_project_id"] != project["project_id"]:
                    raise PostOfficeError("PON_INPUT_INVALID", "Integration package is outside the project", {"packageId": package_id})
                if not con.execute("SELECT 1 FROM storage_copies WHERE content_sha256=?", (str(content_root).removeprefix("sha256:"),)).fetchone():
                    raise PostOfficeError("PON_CUSTODY_NOT_VERIFIED", "Integration package root lacks local custody", {"packageId": package_id})
            failed = any(item["status"] == "FAILED" for item in source["testResults"])
            detail = {"schemaVersion": "1", **source, "state": "REJECTED" if failed else "VALIDATED"}
            project_id, task_id = project["project_id"], None
            scope_facts = {"projectIds": {project_id}, "packageIds": set(source["packageRoots"])}
            created = True
        elif operation == "contextBundle.build":
            if row:
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Context bundle already exists", {})
            task = con.execute("SELECT * FROM tasks WHERE task_id=?", (parameters["taskId"],)).fetchone()
            if not task:
                raise PostOfficeError("PON_INPUT_INVALID", "Context task does not exist", {})
            inventory = parameters["sourceInventory"]
            references = [item["reference"].lower() for item in inventory]
            if any("credential" in value or "secret" in value for value in references):
                raise PostOfficeError("PON_AUTHORIZATION_DENIED", "Context bundle cannot include caller secrets", {})
            content_root = sha256_json(inventory)
            detail = {"schemaVersion": "1", "id": record_id, "taskId": task["task_id"],
                      "includedSources": inventory, "excludedSources": ["caller-secrets"],
                      "contentRoot": content_root, "outputBundleId": "PON-CONTEXT-BUNDLE-" + content_root[:24],
                      "state": "BUILT"}
            project_id, task_id = task["project_id"], task["task_id"]
            scope_facts = {"projectIds": {project_id}, "taskIds": {task_id}}
            created = True
        else:
            if not row:
                raise PostOfficeError("PON_INPUT_INVALID", f"{aggregate_type} does not exist", {})
            detail = dict(before_detail)
            project_id, task_id = row["project_id"], row["task_id"]
            scope_facts = {"projectIds": {project_id} if project_id else set(), "taskIds": {task_id} if task_id else set()}
            if operation == "capabilityRequest.triage":
                if detail["status"] != "OPEN":
                    raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Capability request is not open", {})
                detail["status"] = parameters["outcome"]
                detail["triageDecisionId"] = request["authority"].get("exactAuthorActionId") or request["authority"].get("authorityGrantId")
                detail["triageRationale"] = parameters["rationale"]
            elif operation == "capabilityRequest.fulfil":
                if detail["status"] in {"FULFILLED", "WITHDRAWN"}:
                    raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Capability request is already terminal", {})
                detail["status"] = parameters["outcome"]
                detail["fulfilmentEvidence"] = [{"kind": "ROOT", "reference": root} for root in parameters["evidenceRoots"]]
            elif operation == "changeSet.addTask":
                if detail["state"] not in {"PROPOSED", "TASKS_AUTHORISED"}:
                    raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Change set no longer accepts tasks", {})
                task = con.execute("SELECT * FROM tasks WHERE task_id=?", (parameters["taskId"],)).fetchone()
                if not task or task["project_id"] != project_id:
                    raise PostOfficeError("PON_INPUT_INVALID", "Task is outside the change-set project", {})
                detail["taskIds"] = sorted(set(detail["taskIds"] + [task["task_id"]]))
                detail["state"] = "TASKS_AUTHORISED"
                scope_facts["taskIds"] = {task["task_id"]}
            elif operation == "changeSet.startIntegration":
                if detail["state"] not in {"TASKS_ACTIVE", "CANDIDATES_READY", "TASKS_AUTHORISED", "PROPOSED"}:
                    raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Change set is not ready for integration", {})
                detail["state"] = "INTEGRATION"
                detail["parentGenerationId"] = parameters["parentGenerationId"]
            else:
                if detail["state"] != "INTEGRATION":
                    raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Change set is not under integration", {})
                detail["state"] = "CLOSED" if parameters["decision"] == "ABANDONED" else parameters["decision"]
                detail["decisionId"] = request["authority"].get("exactAuthorActionId") or request["authority"].get("authorityGrantId")
                detail["decisionRationale"] = parameters["rationale"]
        basis = _prepare_mutation_authority(
            con, request=request, capability=capability, request_hash=request_hash,
            recorded_at=recorded_at, scope_facts=scope_facts,
        )
        after_root = _aggregate_root(aggregate_type, record_id, detail)
        if created:
            con.execute(
                """INSERT INTO workflow_aggregates(aggregate_type,aggregate_id,project_id,task_id,state,
                   state_json,aggregate_version,aggregate_root,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (aggregate_type, record_id, project_id, task_id, detail.get("state", detail.get("status", "BUILT")),
                 canonical_json_bytes(detail).decode("utf-8"), 1, after_root, recorded_at),
            )
        else:
            con.execute(
                "UPDATE workflow_aggregates SET state=?,state_json=?,aggregate_version=?,aggregate_root=? WHERE aggregate_type=? AND aggregate_id=?",
                (detail.get("state", detail.get("status")), canonical_json_bytes(detail).decode("utf-8"), version + 1, after_root, aggregate_type, record_id),
            )
        return _finalize_mutation(
            con, request=request, capability=capability, basis=basis, request_hash=request_hash,
            recorded_at=recorded_at, before_root=before_root, after_root=after_root,
            aggregate_version=1 if created else version + 1, created_ids=[record_id] if created else [],
            event_result={"state": detail.get("state", detail.get("status"))},
            event_link=("UPDATE workflow_aggregates SET created_event_id=? WHERE aggregate_type=? AND aggregate_id=?", (aggregate_type, record_id)) if created else None,
        )

    if operation in {"cycle.open", "cycle.markAwaitingReview", "cycle.accept", "cycle.close"}:
        cycle_id = parameters["cycleId"]
        if aggregate_id != cycle_id:
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Cycle aggregate ID differs", {})
        row = con.execute("SELECT * FROM semantic_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
        before_detail = _cycle_state(con, row) if row else None
        before_root = _aggregate_root("SemanticCycle", cycle_id, before_detail)
        version = _aggregate_position(con, aggregate_type="SemanticCycle", aggregate_id=cycle_id, current_root=before_root)
        _require_aggregate(request, aggregate_type="SemanticCycle", aggregate_id=cycle_id, actual_version=version, actual_root=before_root)
        if operation == "cycle.open":
            if row:
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Semantic cycle already exists", {})
            project = con.execute("SELECT * FROM projects WHERE project_id=?", (parameters["projectId"],)).fetchone()
            if not project or project["status"] != "ACTIVE":
                raise PostOfficeError("PON_INPUT_INVALID", "Cycle project is not active", {})
            scope_facts = {"projectIds": {project["project_id"]}}
        else:
            if not row:
                raise PostOfficeError("PON_INPUT_INVALID", "Semantic cycle does not exist", {})
            scope_facts = {"projectIds": {row["project_id"]}}
        basis = _prepare_mutation_authority(
            con, request=request, capability=capability, request_hash=request_hash,
            recorded_at=recorded_at, scope_facts=scope_facts,
        )
        if operation == "cycle.open":
            action_id = request["authority"].get("exactAuthorActionId")
            con.execute(
                """INSERT INTO semantic_cycles(cycle_id,project_id,scope,opening_author_action_id,state,
                   aggregate_version,aggregate_root,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (cycle_id, project["project_id"], parameters["scope"], action_id, "OPEN", 1, "0" * 64, recorded_at),
            )
            event_result = {"state": "OPEN"}
        elif operation == "cycle.markAwaitingReview":
            if row["state"] not in {"OPEN", "ACTIVE"}:
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Cycle cannot enter review from its current state", {})
            con.execute("UPDATE semantic_cycles SET state='AWAITING_REVIEW' WHERE cycle_id=?", (cycle_id,))
            event_result = {"state": "AWAITING_REVIEW", "summary": parameters["summary"]}
        elif operation == "cycle.accept":
            if row["state"] != "AWAITING_REVIEW":
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Cycle is not awaiting review", {})
            action_id = request["authority"].get("exactAuthorActionId")
            if parameters["decisionId"] != action_id:
                raise PostOfficeError("PON_AUTHORIZATION_DENIED", "Cycle decision must be the exact author action", {})
            con.execute("UPDATE semantic_cycles SET state='ACCEPTED',acceptance_author_action_id=? WHERE cycle_id=?", (action_id, cycle_id))
            event_result = {"state": "ACCEPTED", "decisionId": action_id}
        else:
            if row["state"] != "ACCEPTED" or parameters["acceptanceDecisionId"] != row["acceptance_author_action_id"]:
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Cycle acceptance decision does not match", {})
            closure_id = request["authority"].get("exactAuthorActionId")
            con.execute("UPDATE semantic_cycles SET state='CLOSED',closure_author_action_id=? WHERE cycle_id=?", (closure_id, cycle_id))
            event_result = {"state": "CLOSED", "acceptanceDecisionId": parameters["acceptanceDecisionId"]}
        updated = con.execute("SELECT * FROM semantic_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
        detail = _cycle_state(con, updated)
        after_root = _aggregate_root("SemanticCycle", cycle_id, detail)
        con.execute("UPDATE semantic_cycles SET aggregate_version=?,aggregate_root=? WHERE cycle_id=?", (1 if operation == "cycle.open" else version + 1, after_root, cycle_id))
        return _finalize_mutation(
            con, request=request, capability=capability, basis=basis, request_hash=request_hash,
            recorded_at=recorded_at, before_root=before_root, after_root=after_root,
            aggregate_version=1 if operation == "cycle.open" else version + 1,
            created_ids=[cycle_id] if operation == "cycle.open" else [], event_result=event_result,
        )

    if operation == "message.plan":
        before_root = _aggregate_root("MessagePlan", aggregate_id, None)
        if con.execute("SELECT 1 FROM message_plans WHERE plan_id=?", (aggregate_id,)).fetchone():
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Message plan already exists", {})
        _require_aggregate(request, aggregate_type="MessagePlan", aggregate_id=aggregate_id, actual_version=0, actual_root=before_root)
        message = parameters["semanticMessage"]
        bundle = parameters["bundle"]
        if bundle["semanticMessageId"] != message["id"]:
            raise PostOfficeError("PON_INPUT_INVALID", "Bundle and semantic message IDs differ", {})
        sender = con.execute("SELECT * FROM endpoints WHERE endpoint_id=?", (message["senderEndpointId"],)).fetchone()
        recipient = con.execute(
            "SELECT * FROM mailboxes WHERE mailbox_id=? AND generation=?",
            (message["recipientMailboxId"], message["recipientGeneration"]),
        ).fetchone()
        cycle = con.execute("SELECT * FROM semantic_cycles WHERE cycle_id=?", (message["semanticCycleId"],)).fetchone()
        if not sender or sender["status"] != "ACTIVE" or not recipient or recipient["status"] != "ACTIVE":
            raise PostOfficeError("PON_INPUT_INVALID", "Message endpoints are not active", {})
        recipient_endpoint = con.execute("SELECT * FROM endpoints WHERE endpoint_id=?", (recipient["endpoint_id"],)).fetchone()
        domain = con.execute("SELECT * FROM project_mail_domains WHERE mail_domain=?", (message["domain"],)).fetchone()
        if not recipient_endpoint or not domain or sender["project_id"] != recipient_endpoint["project_id"] or domain["project_id"] != sender["project_id"]:
            raise PostOfficeError("PON_INPUT_INVALID", "Message route crosses project or domain scope", {})
        if not cycle or cycle["project_id"] != sender["project_id"] or cycle["state"] not in {"OPEN", "ACTIVE"}:
            raise PostOfficeError("PON_INPUT_INVALID", "Message semantic cycle is not active for the project", {})
        basis_id = message["authorityBasisId"]
        if not con.execute("SELECT 1 FROM authority_grants WHERE grant_id=?", (basis_id,)).fetchone() and not con.execute("SELECT 1 FROM exact_author_actions WHERE action_id=?", (basis_id,)).fetchone():
            raise PostOfficeError("PON_AUTHORIZATION_DENIED", "Message authority basis is unknown", {})
        scope_facts = {"projectIds": {sender["project_id"]}, "mailDomains": {message["domain"]}}
        basis = _prepare_mutation_authority(
            con, request=request, capability=capability, request_hash=request_hash,
            recorded_at=recorded_at, scope_facts=scope_facts,
        )
        plan_root = sha256_json({"message": message, "bundle": bundle})
        draft = {"planId": aggregate_id, "message": message, "bundle": bundle, "planRoot": plan_root, "state": "PLANNED"}
        after_root = _aggregate_root("MessagePlan", aggregate_id, draft)
        con.execute(
            """INSERT INTO message_plans(plan_id,message_id,bundle_id,semantic_message_json,bundle_json,
               plan_root,state,aggregate_version,aggregate_root,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (aggregate_id, message["id"], bundle["id"], canonical_json_bytes(message).decode("utf-8"),
             canonical_json_bytes(bundle).decode("utf-8"), plan_root, "PLANNED", 1, after_root, recorded_at),
        )
        return _finalize_mutation(
            con, request=request, capability=capability, basis=basis, request_hash=request_hash,
            recorded_at=recorded_at, before_root=before_root, after_root=after_root,
            aggregate_version=1, created_ids=[aggregate_id], event_result={"state": "PLANNED", "messageId": message["id"], "bundleId": bundle["id"]},
            event_link=("UPDATE message_plans SET created_event_id=? WHERE plan_id=?", (aggregate_id,)),
        )

    if operation == "bundle.supersedeBeforeRegistration":
        plan = con.execute(
            "SELECT * FROM message_plans WHERE state='PLANNED' AND json_extract(bundle_json,'$.canonicalFilename')=? AND json_extract(bundle_json,'$.sha256') IN (?,?)",
            (parameters["oldFilename"], parameters["oldSha256"], parameters["oldSha256"].removeprefix("sha256:")),
        ).fetchone()
        if not plan or aggregate_id != plan["plan_id"]:
            raise PostOfficeError("PON_INPUT_INVALID", "Supersedable message plan does not exist", {})
        before_detail = _message_plan_state(plan)
        before_root = _aggregate_root("MessagePlan", plan["plan_id"], before_detail)
        version = _aggregate_position(con, aggregate_type="MessagePlan", aggregate_id=plan["plan_id"], current_root=before_root)
        _require_aggregate(request, aggregate_type="MessagePlan", aggregate_id=plan["plan_id"], actual_version=version, actual_root=before_root)
        replacement = parameters["replacement"]
        message = json.loads(plan["semantic_message_json"])
        if replacement["semanticMessageId"] != message["id"] or replacement["id"] == plan["bundle_id"]:
            raise PostOfficeError("PON_INPUT_INVALID", "Replacement bundle identity is invalid", {})
        sender = con.execute("SELECT * FROM endpoints WHERE endpoint_id=?", (message["senderEndpointId"],)).fetchone()
        basis = _prepare_mutation_authority(
            con, request=request, capability=capability, request_hash=request_hash,
            recorded_at=recorded_at, scope_facts={"projectIds": {sender["project_id"]}, "mailDomains": {message["domain"]}},
        )
        plan_root = sha256_json({"message": message, "bundle": replacement})
        con.execute(
            "UPDATE message_plans SET bundle_id=?,bundle_json=?,plan_root=?,aggregate_version=? WHERE plan_id=?",
            (replacement["id"], canonical_json_bytes(replacement).decode("utf-8"), plan_root, version + 1, plan["plan_id"]),
        )
        updated = con.execute("SELECT * FROM message_plans WHERE plan_id=?", (plan["plan_id"],)).fetchone()
        after_root = _aggregate_root("MessagePlan", plan["plan_id"], _message_plan_state(updated))
        con.execute("UPDATE message_plans SET aggregate_root=? WHERE plan_id=?", (after_root, plan["plan_id"]))
        return _finalize_mutation(
            con, request=request, capability=capability, basis=basis, request_hash=request_hash,
            recorded_at=recorded_at, before_root=before_root, after_root=after_root,
            aggregate_version=version + 1, created_ids=[replacement["id"]],
            event_result={"state": "PLANNED", "supersededBundleId": plan["bundle_id"], "replacementBundleId": replacement["id"]},
        )

    if operation in {"message.register", "message.route", "message.acknowledge", "message.review", "message.close"}:
        message_id = parameters.get("semanticMessageId") or parameters.get("semanticMessage", {}).get("id") or aggregate_id
        if aggregate_id != message_id:
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Semantic message aggregate ID differs", {})
        row = con.execute("SELECT * FROM semantic_messages WHERE message_id=?", (message_id,)).fetchone()
        before_detail = _message_state(con, row) if row else None
        before_root = _aggregate_root("SemanticMessage", message_id, before_detail)
        version = _aggregate_position(con, aggregate_type="SemanticMessage", aggregate_id=message_id, current_root=before_root)
        _require_aggregate(request, aggregate_type="SemanticMessage", aggregate_id=message_id, actual_version=version, actual_root=before_root)
        if operation == "message.register":
            if row:
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Semantic message already exists", {})
            message = parameters["semanticMessage"]
            plan = con.execute("SELECT * FROM message_plans WHERE message_id=? AND bundle_id=? AND state='PLANNED'", (message_id, parameters["bundleId"])).fetchone()
            if not plan or json.loads(plan["semantic_message_json"]) != message:
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Registered message differs from its retained plan", {})
            bundle = json.loads(plan["bundle_json"])
            bundle_hash = bundle["sha256"].removeprefix("sha256:")
            if not con.execute("SELECT 1 FROM storage_copies WHERE content_sha256=? AND location_kind IN ('LOCAL_CAS','DATABASE_BLOB')", (bundle_hash,)).fetchone():
                raise PostOfficeError("PON_CUSTODY_NOT_VERIFIED", "Message bundle lacks verified local custody", {"bundleId": bundle["id"]})
            sender = con.execute("SELECT * FROM endpoints WHERE endpoint_id=?", (message["senderEndpointId"],)).fetchone()
            scope_facts = {"projectIds": {sender["project_id"]}, "mailDomains": {message["domain"]}}
        else:
            if not row:
                raise PostOfficeError("PON_INPUT_INVALID", "Semantic message does not exist", {})
            scope_facts = {"projectIds": {row["project_id"]}, "mailDomains": {row["mail_domain"]}}
        basis = _prepare_mutation_authority(
            con, request=request, capability=capability, request_hash=request_hash,
            recorded_at=recorded_at, scope_facts=scope_facts,
        )
        link: tuple[str, tuple[Any, ...]] | None = None
        if operation == "message.register":
            authority_grant = message["authorityBasisId"] if con.execute("SELECT 1 FROM authority_grants WHERE grant_id=?", (message["authorityBasisId"],)).fetchone() else None
            author_action = None if authority_grant else message["authorityBasisId"]
            con.execute(
                """INSERT INTO semantic_messages(message_id,project_id,mail_domain,message_type,sender_endpoint_id,
                   recipient_mailbox_id,recipient_generation,semantic_cycle_id,authority_grant_id,exact_author_action_id,
                   requested_action,completion_criteria,content_root,state,aggregate_version,aggregate_root,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (message_id, sender["project_id"], message["domain"], message["messageType"], message["senderEndpointId"],
                 message["recipientMailboxId"], message["recipientGeneration"], message["semanticCycleId"], authority_grant,
                 author_action, message["requestedAction"], message["completionCriteria"], message["contentRoot"].removeprefix("sha256:"),
                 "REGISTERED", 1, "0" * 64, recorded_at),
            )
            for related_id in sorted(set(message["relatedMessageIds"])):
                if not con.execute("SELECT 1 FROM semantic_messages WHERE message_id=?", (related_id,)).fetchone():
                    raise PostOfficeError("PON_INPUT_INVALID", "Related message does not exist", {"relatedMessageId": related_id})
                con.execute("INSERT INTO message_relations VALUES(?,?,?)", (message_id, related_id, "RELATED"))
            con.execute(
                "INSERT INTO message_bundles(bundle_id,message_id,canonical_filename,bundle_version,size_bytes,sha256,manifest_root,state,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (bundle["id"], message_id, bundle["canonicalFilename"], bundle["bundleVersion"], bundle["sizeBytes"], bundle_hash,
                 bundle["manifestRoot"].removeprefix("sha256:"), "REGISTERED", recorded_at),
            )
            for ordinal, payload in enumerate(bundle["payloads"]):
                con.execute("INSERT INTO bundle_payloads VALUES(?,?,?,?,?)", (bundle["id"], ordinal, payload["path"], payload["sizeBytes"], payload["sha256"].removeprefix("sha256:")))
            event_result = {"state": "REGISTERED", "bundleId": bundle["id"]}
            link = ("UPDATE semantic_messages SET registered_event_id=? WHERE message_id=?", (message_id,))
        elif operation == "message.route":
            if row["state"] != "REGISTERED" or parameters["destinationMailboxId"] != row["recipient_mailbox_id"] or int(parameters["destinationGeneration"]) != int(row["recipient_generation"]):
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Message route differs from registered destination", {})
            bundle = con.execute("SELECT * FROM message_bundles WHERE bundle_id=? AND message_id=? AND state='REGISTERED'", (parameters["bundleId"], message_id)).fetchone()
            if not bundle:
                raise PostOfficeError("PON_INPUT_INVALID", "Registered message bundle does not exist", {})
            attempt_id = "PON-TRANSPORT-" + uuid.uuid4().hex
            con.execute(
                "INSERT INTO transport_attempts VALUES(?,?,?,?,?,?,?,?,?,?)",
                (attempt_id, message_id, bundle["bundle_id"], row["sender_endpoint_id"], f"{row['recipient_mailbox_id']}:{row['recipient_generation']}", "PENDING", 1, None, recorded_at, recorded_at),
            )
            con.execute("UPDATE semantic_messages SET state='CUSTODY_RECORDED' WHERE message_id=?", (message_id,))
            event_result = {"state": "CUSTODY_RECORDED", "transportAttemptId": attempt_id}
        elif operation == "message.acknowledge":
            if row["state"] != "DELIVERED":
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Only a delivered message may be acknowledged", {})
            mailbox = con.execute("SELECT * FROM mailboxes WHERE mailbox_id=? AND generation=?", (row["recipient_mailbox_id"], row["recipient_generation"])).fetchone()
            evidence_root = sha256_json(parameters["payloadHashes"])
            acknowledgement_id = "PON-ACK-" + uuid.uuid4().hex
            con.execute(
                "INSERT INTO acknowledgements VALUES(?,?,?,?,?,?)",
                (acknowledgement_id, message_id, mailbox["endpoint_id"], canonical_json_bytes(parameters["payloadHashes"]).decode("utf-8"), evidence_root, recorded_at),
            )
            con.execute("UPDATE semantic_messages SET state='ACKNOWLEDGED' WHERE message_id=?", (message_id,))
            event_result = {"state": "ACKNOWLEDGED", "acknowledgementId": acknowledgement_id}
        elif operation == "message.review":
            if row["state"] not in {"ACKNOWLEDGED", "RESPONSE_RETURNED"}:
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Message is not ready for review", {})
            decision_id = "PON-MESSAGE-DECISION-" + uuid.uuid4().hex
            con.execute("INSERT INTO message_decisions VALUES(?,?,?,?,?,?)", (decision_id, message_id, parameters["decision"], parameters["rationale"], "PENDING", recorded_at))
            con.execute("UPDATE semantic_messages SET state='REVIEWED' WHERE message_id=?", (message_id,))
            link = ("UPDATE message_decisions SET event_id=? WHERE decision_id=?", (decision_id,))
            event_result = {"state": "REVIEWED", "decision": parameters["decision"], "decisionId": decision_id}
        else:
            if row["state"] != "REVIEWED":
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Message must be reviewed before closure", {})
            con.execute("INSERT INTO message_closures VALUES(?,?,?,?)", (message_id, parameters["summary"], "PENDING", recorded_at))
            con.execute("UPDATE semantic_messages SET state='CLOSED' WHERE message_id=?", (message_id,))
            link = ("UPDATE message_closures SET event_id=? WHERE message_id=?", (message_id,))
            event_result = {"state": "CLOSED", "summary": parameters["summary"]}
        updated = con.execute("SELECT * FROM semantic_messages WHERE message_id=?", (message_id,)).fetchone()
        detail = _message_state(con, updated)
        after_root = _aggregate_root("SemanticMessage", message_id, detail)
        next_version = 1 if operation == "message.register" else version + 1
        con.execute("UPDATE semantic_messages SET aggregate_version=?,aggregate_root=? WHERE message_id=?", (next_version, after_root, message_id))
        result = _finalize_mutation(
            con, request=request, capability=capability, basis=basis, request_hash=request_hash,
            recorded_at=recorded_at, before_root=before_root, after_root=after_root,
            aggregate_version=next_version, created_ids=[message_id] if operation == "message.register" else [],
            event_result=event_result, event_link=link,
        )
        if operation == "message.register":
            plan_before = _aggregate_root("MessagePlan", plan["plan_id"], _message_plan_state(plan))
            plan_version = _aggregate_position(con, aggregate_type="MessagePlan", aggregate_id=plan["plan_id"], current_root=plan_before)
            con.execute("UPDATE message_plans SET state='REGISTERED',aggregate_version=?,registered_event_id=?,registered_at=? WHERE plan_id=?", (plan_version + 1, result["eventId"], recorded_at, plan["plan_id"]))
            updated_plan = con.execute("SELECT * FROM message_plans WHERE plan_id=?", (plan["plan_id"],)).fetchone()
            plan_after = _aggregate_root("MessagePlan", plan["plan_id"], _message_plan_state(updated_plan))
            con.execute("UPDATE message_plans SET aggregate_root=? WHERE plan_id=?", (plan_after, plan["plan_id"]))
            append_hub_event(
                con, request=request, capability_id=capability["capability_id"], aggregate_version=plan_version + 1,
                before_root=plan_before, after_root=plan_after, event_result={"state": "REGISTERED", "messageEventId": result["eventId"]},
                occurred_at=recorded_at, aggregate_type="MessagePlan", aggregate_id=plan["plan_id"],
            )
            result["receipt"]["stateRoot"] = _operational_state(con)["operationalStateRoot"]
        return result

    if operation == "hub.reconcile.preview":
        plan_id = aggregate_id
        if con.execute("SELECT 1 FROM reconciliation_plans WHERE plan_id=?", (plan_id,)).fetchone():
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Reconciliation plan already exists", {})
        before_root = _aggregate_root("ReconciliationPlan", plan_id, None)
        _require_aggregate(
            request, aggregate_type="ReconciliationPlan", aggregate_id=plan_id,
            actual_version=0, actual_root=before_root,
        )
        basis = _prepare_mutation_authority(
            con, request=request, capability=capability, request_hash=request_hash,
            recorded_at=recorded_at, scope_facts={},
        )
        observations = list(con.execute(
            "SELECT * FROM shadow_observations WHERE state='OPEN' ORDER BY observation_id"
        ))
        observation_states = [_shadow_observation_state(item) for item in observations]
        actions = [
            {
                "observationId": item["observationId"],
                "action": "AUTO_RESOLVE" if item["classification"] == "MATCH" else "REVIEW_REQUIRED",
                "classification": item["classification"],
            }
            for item in observation_states
        ]
        assertion_ids = sorted(set(parameters.get("assertionSetIds", [])))
        identity = {
            "snapshotRoot": parameters["snapshotRoot"].removeprefix("sha256:"),
            "assertionSetRoot": sha256_json(assertion_ids),
            "observationSetRoot": sha256_json(observation_states),
            "actions": actions,
        }
        plan_root = sha256_json(identity)
        con.execute(
            """INSERT INTO reconciliation_plans(
               plan_id,snapshot_root,assertion_set_root,observation_set_root,actions_json,
               plan_root,state,aggregate_version,aggregate_root,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                plan_id, identity["snapshotRoot"], identity["assertionSetRoot"],
                identity["observationSetRoot"], canonical_json_bytes(actions).decode("utf-8"),
                plan_root, "PREVIEWED", 1, "0" * 64, recorded_at,
            ),
        )
        row = con.execute("SELECT * FROM reconciliation_plans WHERE plan_id=?", (plan_id,)).fetchone()
        after_root = _aggregate_root("ReconciliationPlan", plan_id, _reconciliation_plan_state(row))
        con.execute("UPDATE reconciliation_plans SET aggregate_root=? WHERE plan_id=?", (after_root, plan_id))
        detail = {
            **identity, "planId": plan_id, "planRoot": plan_root,
            "state": "PREVIEWED",
            "automaticActionCount": sum(1 for item in actions if item["action"] == "AUTO_RESOLVE"),
            "reviewRequiredCount": sum(1 for item in actions if item["action"] == "REVIEW_REQUIRED"),
        }
        return _finalize_mutation(
            con, request=request, capability=capability, basis=basis,
            request_hash=request_hash, recorded_at=recorded_at,
            before_root=before_root, after_root=after_root, aggregate_version=1,
            created_ids=[plan_id], event_result={"state": "PREVIEWED", "planRoot": plan_root},
            extra_result={"resource": detail},
        )

    if operation == "hub.reconcile.apply":
        plan_id = parameters["previewPlanId"]
        if aggregate_id != plan_id:
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Reconciliation plan aggregate ID differs", {})
        row = con.execute("SELECT * FROM reconciliation_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if not row:
            raise PostOfficeError("PON_INPUT_INVALID", "Reconciliation plan does not exist", {})
        if row["state"] != "PREVIEWED" or row["plan_root"] != parameters["planRoot"].removeprefix("sha256:"):
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Reconciliation plan root or state differs", {})
        before_root = _aggregate_root("ReconciliationPlan", plan_id, _reconciliation_plan_state(row))
        version = _aggregate_position(
            con, aggregate_type="ReconciliationPlan", aggregate_id=plan_id, current_root=before_root
        )
        _require_aggregate(
            request, aggregate_type="ReconciliationPlan", aggregate_id=plan_id,
            actual_version=version, actual_root=before_root,
        )
        actions = json.loads(row["actions_json"])
        unresolved = [item for item in actions if item["action"] != "AUTO_RESOLVE"]
        if unresolved:
            raise PostOfficeError(
                "PON_RECONCILIATION_AMBIGUOUS",
                "Reconciliation plan contains discrepancies requiring an explicit decision",
                {"observationIds": [item["observationId"] for item in unresolved]},
            )
        basis = _prepare_mutation_authority(
            con, request=request, capability=capability, request_hash=request_hash,
            recorded_at=recorded_at, scope_facts={},
        )
        for action in actions:
            changed = con.execute(
                """UPDATE shadow_observations SET state='RESOLVED',resolved_at=?
                   WHERE observation_id=? AND state='OPEN' AND classification='MATCH'""",
                (recorded_at, action["observationId"]),
            ).rowcount
            if changed != 1:
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Shadow observation changed after preview", {"observationId": action["observationId"]})
        con.execute(
            "UPDATE reconciliation_plans SET state='APPLIED',aggregate_version=?,applied_at=? WHERE plan_id=?",
            (version + 1, recorded_at, plan_id),
        )
        updated = con.execute("SELECT * FROM reconciliation_plans WHERE plan_id=?", (plan_id,)).fetchone()
        after_root = _aggregate_root("ReconciliationPlan", plan_id, _reconciliation_plan_state(updated))
        con.execute("UPDATE reconciliation_plans SET aggregate_root=? WHERE plan_id=?", (after_root, plan_id))
        return _finalize_mutation(
            con, request=request, capability=capability, basis=basis,
            request_hash=request_hash, recorded_at=recorded_at,
            before_root=before_root, after_root=after_root, aggregate_version=version + 1,
            created_ids=[], event_result={"state": "APPLIED", "resolvedObservationIds": [item["observationId"] for item in actions]},
            event_link=("UPDATE reconciliation_plans SET applied_event_id=? WHERE plan_id=?", (plan_id,)),
        )

    if operation in {"transport.retry", "transport.quarantine", "transport.tombstoneDuplicate"}:
        attempt_id = parameters["transportAttemptId"]
        if aggregate_id != attempt_id:
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Transport aggregate ID differs", {})
        row = con.execute("SELECT * FROM transport_attempts WHERE transport_attempt_id=?", (attempt_id,)).fetchone()
        if not row:
            raise PostOfficeError("PON_INPUT_INVALID", "Transport attempt does not exist", {})
        dispatch = con.execute("SELECT * FROM transport_dispatches WHERE transport_attempt_id=?", (attempt_id,)).fetchone()
        before_detail = _transport_state(row, dispatch)
        before_root = _aggregate_root("TransportAttempt", attempt_id, before_detail)
        version = _aggregate_position(con, aggregate_type="TransportAttempt", aggregate_id=attempt_id, current_root=before_root)
        _require_aggregate(request, aggregate_type="TransportAttempt", aggregate_id=attempt_id, actual_version=version, actual_root=before_root)
        message = con.execute("SELECT * FROM semantic_messages WHERE message_id=?", (row["message_id"],)).fetchone()
        message_before_root = _aggregate_root(
            "SemanticMessage", message["message_id"], _message_state(con, message)
        )
        message_version = _aggregate_position(
            con,
            aggregate_type="SemanticMessage",
            aggregate_id=message["message_id"],
            current_root=message_before_root,
        )
        basis = _prepare_mutation_authority(
            con, request=request, capability=capability, request_hash=request_hash,
            recorded_at=recorded_at,
            scope_facts={"projectIds": {message["project_id"]}, "mailDomains": {message["mail_domain"]}},
        )
        if operation == "transport.retry":
            retryable = row["state"] == "FAILED_RETRYABLE" or (dispatch and dispatch["state"] in {"WAITING_FOR_RECIPIENT", "RECONCILIATION_REQUIRED", "TERMINAL_FAILURE"})
            if not retryable:
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Transport attempt is not retryable", {})
            next_number = int(con.execute("SELECT COALESCE(MAX(attempt_number),0)+1 FROM transport_attempts WHERE message_id=? AND bundle_id=?", (row["message_id"], row["bundle_id"])).fetchone()[0])
            new_id = "PON-TRANSPORT-" + uuid.uuid4().hex
            con.execute(
                "INSERT INTO transport_attempts VALUES(?,?,?,?,?,?,?,?,?,?)",
                (new_id, row["message_id"], row["bundle_id"], row["source"], row["destination"], "PENDING", next_number, attempt_id, recorded_at, recorded_at),
            )
            if dispatch:
                con.execute("UPDATE transport_dispatches SET state='CANCELLED',updated_at=? WHERE dispatch_id=?", (recorded_at, dispatch["dispatch_id"]))
            con.execute("UPDATE semantic_messages SET state='CUSTODY_RECORDED' WHERE message_id=?", (row["message_id"],))
            event_result = {"state": "RETRIED", "nextTransportAttemptId": new_id, "attemptNumber": next_number}
        elif operation == "transport.quarantine":
            if row["state"] in {"RECEIPTED", "TOMBSTONED_DUPLICATE", "CANCELLED"}:
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Terminal transport attempt cannot be quarantined", {})
            con.execute("UPDATE transport_attempts SET state='QUARANTINED',updated_at=? WHERE transport_attempt_id=?", (recorded_at, attempt_id))
            if dispatch:
                con.execute("UPDATE transport_dispatches SET state='TERMINAL_FAILURE',last_error_code=?,updated_at=? WHERE dispatch_id=?", (parameters["reasonCode"], recorded_at, dispatch["dispatch_id"]))
            con.execute("UPDATE semantic_messages SET state='QUARANTINED' WHERE message_id=?", (row["message_id"],))
            event_result = {"state": "QUARANTINED", "reasonCode": parameters["reasonCode"]}
        else:
            canonical_id = parameters["canonicalTransportAttemptId"]
            canonical = con.execute("SELECT * FROM transport_attempts WHERE transport_attempt_id=?", (canonical_id,)).fetchone()
            if canonical_id == attempt_id or not canonical or canonical["message_id"] != row["message_id"] or canonical["bundle_id"] != row["bundle_id"]:
                raise PostOfficeError("PON_INPUT_INVALID", "Canonical attempt is not a distinct attempt for the same message and bundle", {})
            if row["state"] in {"RECEIPTED", "TOMBSTONED_DUPLICATE"}:
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Receipted or tombstoned attempt cannot change", {})
            con.execute("UPDATE transport_attempts SET state='TOMBSTONED_DUPLICATE',canonical_attempt_id=?,updated_at=? WHERE transport_attempt_id=?", (canonical_id, recorded_at, attempt_id))
            if dispatch:
                con.execute("UPDATE transport_dispatches SET state='CANCELLED',updated_at=? WHERE dispatch_id=?", (recorded_at, dispatch["dispatch_id"]))
            event_result = {"state": "TOMBSTONED_DUPLICATE", "canonicalTransportAttemptId": canonical_id}
        updated = con.execute("SELECT * FROM transport_attempts WHERE transport_attempt_id=?", (attempt_id,)).fetchone()
        updated_dispatch = con.execute("SELECT * FROM transport_dispatches WHERE transport_attempt_id=?", (attempt_id,)).fetchone()
        after_root = _aggregate_root("TransportAttempt", attempt_id, _transport_state(updated, updated_dispatch))
        result = _finalize_mutation(
            con, request=request, capability=capability, basis=basis, request_hash=request_hash,
            recorded_at=recorded_at, before_root=before_root, after_root=after_root,
            aggregate_version=version + 1, created_ids=[event_result["nextTransportAttemptId"]] if operation == "transport.retry" else [],
            event_result=event_result,
        )
        changed_message = con.execute(
            "SELECT * FROM semantic_messages WHERE message_id=?", (message["message_id"],)
        ).fetchone()
        message_after_root = _aggregate_root(
            "SemanticMessage", message["message_id"], _message_state(con, changed_message)
        )
        if message_after_root != message_before_root:
            con.execute(
                "UPDATE semantic_messages SET aggregate_version=?,aggregate_root=? WHERE message_id=?",
                (message_version + 1, message_after_root.removeprefix("sha256:"), message["message_id"]),
            )
            message_event = append_hub_event(
                con,
                request=request,
                capability_id=capability["capability_id"],
                aggregate_version=message_version + 1,
                before_root=message_before_root,
                after_root=message_after_root,
                event_result={
                    "state": changed_message["state"],
                    "transportAttemptId": attempt_id,
                    "sideEffectOfEventId": result["eventId"],
                },
                occurred_at=recorded_at,
                aggregate_type="SemanticMessage",
                aggregate_id=message["message_id"],
            )
            result["messageEventId"] = message_event["eventId"]
            result["receipt"]["stateRoot"] = _operational_state(con)["operationalStateRoot"]
        return result

    if operation == "authority.grant":
        aggregate_type = "AuthorityGrant"
        before_root = _aggregate_root(aggregate_type, aggregate_id, None)
        if con.execute("SELECT 1 FROM authority_grants WHERE grant_id=?", (aggregate_id,)).fetchone():
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Authority grant already exists", {})
        _require_aggregate(
            request, aggregate_type=aggregate_type, aggregate_id=aggregate_id,
            actual_version=0, actual_root=before_root,
        )
        grant = parameters["grant"]
        if grant["grantorId"] != request["actor"]["id"]:
            raise PostOfficeError("PON_AUTHORIZATION_DENIED", "Grantor must be the authenticated author", {})
        if grant["sourceDecisionId"] != request["authority"]["exactAuthorActionId"]:
            raise PostOfficeError("PON_AUTHORIZATION_DENIED", "Grant source decision must be the exact author action", {})
        recipient = con.execute(
            "SELECT * FROM actors WHERE actor_id=?", (grant["recipientId"],)
        ).fetchone()
        if not recipient or recipient["status"] != "ACTIVE":
            raise PostOfficeError("PON_INPUT_INVALID", "Grant recipient is not an active actor", {})
        allowed_operations = grant["allowedOperations"]
        if len(set(allowed_operations)) != len(allowed_operations):
            raise PostOfficeError("PON_INPUT_INVALID", "Grant operations must be unique", {})
        unsupported = sorted(set(allowed_operations) - IMPLEMENTED_OPERATIONS)
        if unsupported:
            raise PostOfficeError(
                "PON_AUTHORIZATION_DENIED", "P3.2 cannot grant unavailable operations",
                {"operations": unsupported},
            )
        _validate_scope_references(con, grant["scope"])
        maximum_uses = grant.get("maximumUses")
        if grant["classification"] == "ONE_SHOT" and maximum_uses is None:
            raise PostOfficeError("PON_INPUT_INVALID", "ONE_SHOT authority requires maximumUses", {})
        if grant["classification"] == "STANDING" and maximum_uses is not None:
            raise PostOfficeError("PON_INPUT_INVALID", "STANDING authority cannot declare maximumUses", {})
        expires_at = grant.get("expiresAt")
        if expires_at and _parse_timestamp(expires_at) <= datetime.now(timezone.utc):
            raise PostOfficeError("PON_INPUT_INVALID", "Authority expiry must be in the future", {})
        basis = _prepare_mutation_authority(
            con, request=request, capability=capability, request_hash=request_hash,
            recorded_at=recorded_at, scope_facts={},
        )
        con.execute(
            """INSERT INTO authority_grants(
               grant_id,grantor_actor_id,recipient_actor_id,allowed_operations_json,
               scope_json,classification,maximum_uses,remaining_uses,status,rationale,
               source_author_action_id,expires_at,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                aggregate_id, grant["grantorId"], grant["recipientId"],
                canonical_json_bytes(sorted(allowed_operations)).decode("utf-8"),
                canonical_json_bytes(grant["scope"]).decode("utf-8"), grant["classification"],
                maximum_uses, maximum_uses, "ACTIVE", grant["rationale"],
                grant["sourceDecisionId"], expires_at, recorded_at,
            ),
        )
        row = con.execute(
            "SELECT * FROM authority_grants WHERE grant_id=?", (aggregate_id,)
        ).fetchone()
        after_root = _aggregate_root(aggregate_type, aggregate_id, _grant_state(row))
        return _finalize_mutation(
            con, request=request, capability=capability, basis=basis,
            request_hash=request_hash, recorded_at=recorded_at,
            before_root=before_root, after_root=after_root, aggregate_version=1,
            created_ids=[aggregate_id], event_result={"state": "ACTIVE"},
            event_link=("UPDATE authority_grants SET created_event_id=? WHERE grant_id=?", (aggregate_id,)),
        )

    if operation == "authority.revoke":
        aggregate_type = "AuthorityGrant"
        grant_id = parameters["grantId"]
        row = con.execute("SELECT * FROM authority_grants WHERE grant_id=?", (grant_id,)).fetchone()
        if not row:
            raise PostOfficeError("PON_INPUT_INVALID", "Authority grant does not exist", {})
        before_root = _aggregate_root(aggregate_type, grant_id, _grant_state(row))
        version = _aggregate_position(
            con, aggregate_type=aggregate_type, aggregate_id=grant_id, current_root=before_root
        )
        _require_aggregate(
            request, aggregate_type=aggregate_type, aggregate_id=grant_id,
            actual_version=version, actual_root=before_root,
        )
        if row["status"] == "REVOKED":
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Authority grant is already revoked", {})
        basis = _prepare_mutation_authority(
            con, request=request, capability=capability, request_hash=request_hash,
            recorded_at=recorded_at, scope_facts={},
        )
        con.execute("UPDATE authority_grants SET status='REVOKED' WHERE grant_id=?", (grant_id,))
        updated = con.execute("SELECT * FROM authority_grants WHERE grant_id=?", (grant_id,)).fetchone()
        after_root = _aggregate_root(aggregate_type, grant_id, _grant_state(updated))
        return _finalize_mutation(
            con, request=request, capability=capability, basis=basis,
            request_hash=request_hash, recorded_at=recorded_at,
            before_root=before_root, after_root=after_root, aggregate_version=version + 1,
            created_ids=[], event_result={"state": "REVOKED", "reason": parameters["reason"]},
            event_link=("UPDATE authority_grants SET revoked_event_id=? WHERE grant_id=?", (grant_id,)),
        )

    if operation == "endpoint.allocate":
        aggregate_type = "Endpoint"
        before_root = _aggregate_root(aggregate_type, aggregate_id, None)
        if con.execute("SELECT 1 FROM endpoints WHERE endpoint_id=?", (aggregate_id,)).fetchone():
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Endpoint already exists", {})
        _require_aggregate(
            request, aggregate_type=aggregate_type, aggregate_id=aggregate_id,
            actual_version=0, actual_root=before_root,
        )
        project = con.execute(
            "SELECT * FROM projects WHERE project_id=?", (parameters["projectId"],)
        ).fetchone()
        if not project or project["status"] not in {"PROVISIONING", "ACTIVE"}:
            raise PostOfficeError("PON_INPUT_INVALID", "Endpoint project is not provisionable", {})
        task_id = parameters.get("taskId")
        if task_id:
            task = con.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if not task or task["project_id"] != project["project_id"]:
                raise PostOfficeError("PON_INPUT_INVALID", "Endpoint task does not belong to its project", {})
        access_scope = parameters["accessScope"]
        scope_facts = {
            "projectIds": {project["project_id"]},
            "taskIds": {task_id} if task_id else set(),
        }
        basis = _prepare_mutation_authority(
            con, request=request, capability=capability, request_hash=request_hash,
            recorded_at=recorded_at, scope_facts=scope_facts,
        )
        _validate_endpoint_access_scope(
            con,
            access_scope,
            owner_project_id=str(project["project_id"]),
            owner_task_id=str(task_id) if task_id else None,
            grant_scope=(json.loads(basis["row"]["scope_json"]) if basis["kind"] == "AUTHORITY_GRANT" else None),
        )
        actor_id = "PON-ACTOR-ENDPOINT-" + sha256_bytes(aggregate_id.encode("utf-8"))[:24]
        if con.execute("SELECT 1 FROM actors WHERE actor_id=?", (actor_id,)).fetchone():
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Endpoint actor identity already exists", {})
        startup_root = sha256_json(
            {
                "endpointId": aggregate_id, "projectId": project["project_id"],
                "taskId": task_id, "role": parameters["role"], "accessScope": access_scope,
            }
        )
        actor_kind = parameters.get("actorKind", "ENDPOINT")
        con.execute(
            "INSERT INTO actors(actor_id,actor_kind,role,status,created_at) VALUES(?,?,?,?,?)",
            (actor_id, actor_kind, parameters["role"], "ACTIVE", recorded_at),
        )
        con.execute(
            """INSERT INTO endpoints(
               endpoint_id,actor_id,project_id,task_id,role,access_scope_json,status,startup_root,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                aggregate_id, actor_id, project["project_id"], task_id, parameters["role"],
                canonical_json_bytes(access_scope).decode("utf-8"), "ACTIVE", startup_root, recorded_at,
            ),
        )
        row = con.execute("SELECT * FROM endpoints WHERE endpoint_id=?", (aggregate_id,)).fetchone()
        after_root = _aggregate_root(aggregate_type, aggregate_id, _endpoint_state(row))
        return _finalize_mutation(
            con, request=request, capability=capability, basis=basis,
            request_hash=request_hash, recorded_at=recorded_at,
            before_root=before_root, after_root=after_root, aggregate_version=1,
            created_ids=[aggregate_id, actor_id], event_result={"state": "ACTIVE", "actorId": actor_id},
            event_link=("UPDATE endpoints SET created_event_id=? WHERE endpoint_id=?", (aggregate_id,)),
        )

    if operation == "endpoint.revoke":
        aggregate_type = "Endpoint"
        endpoint_id = parameters["endpointId"]
        row = con.execute("SELECT * FROM endpoints WHERE endpoint_id=?", (endpoint_id,)).fetchone()
        if not row:
            raise PostOfficeError("PON_INPUT_INVALID", "Endpoint does not exist", {})
        before_root = _aggregate_root(aggregate_type, endpoint_id, _endpoint_state(row))
        version = _aggregate_position(
            con, aggregate_type=aggregate_type, aggregate_id=endpoint_id, current_root=before_root
        )
        _require_aggregate(
            request, aggregate_type=aggregate_type, aggregate_id=endpoint_id,
            actual_version=version, actual_root=before_root,
        )
        if row["status"] in {"REVOKED", "RETIRED"}:
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Endpoint is already inactive", {})
        scope_facts = {
            "projectIds": {row["project_id"]},
            "taskIds": {row["task_id"]} if row["task_id"] else set(),
        }
        basis = _prepare_mutation_authority(
            con, request=request, capability=capability, request_hash=request_hash,
            recorded_at=recorded_at, scope_facts=scope_facts,
        )
        mailbox_ids = [
            value[0] for value in con.execute(
                "SELECT DISTINCT mailbox_id FROM mailboxes WHERE endpoint_id=? AND status IN ('ACTIVE','PROVISIONAL') ORDER BY mailbox_id",
                (endpoint_id,),
            )
        ]
        child_event_ids: list[str] = []
        for mailbox_id in mailbox_ids:
            rows = list(con.execute(
                "SELECT * FROM mailboxes WHERE mailbox_id=? ORDER BY generation", (mailbox_id,)
            ))
            mailbox_before = _aggregate_root("Mailbox", mailbox_id, _mailbox_state(rows, mailbox_id))
            mailbox_version = _aggregate_position(
                con, aggregate_type="Mailbox", aggregate_id=mailbox_id, current_root=mailbox_before
            )
            con.execute(
                "UPDATE mailboxes SET status='REVOKED' WHERE mailbox_id=? AND status IN ('ACTIVE','PROVISIONAL')",
                (mailbox_id,),
            )
            updated_rows = list(con.execute(
                "SELECT * FROM mailboxes WHERE mailbox_id=? ORDER BY generation", (mailbox_id,)
            ))
            mailbox_after = _aggregate_root("Mailbox", mailbox_id, _mailbox_state(updated_rows, mailbox_id))
            child = append_hub_event(
                con, request=request, capability_id=capability["capability_id"],
                aggregate_version=mailbox_version + 1, before_root=mailbox_before,
                after_root=mailbox_after,
                event_result={"state": "REVOKED", "parentEndpointId": endpoint_id},
                occurred_at=recorded_at, aggregate_type="Mailbox", aggregate_id=mailbox_id,
            )
            child_event_ids.append(child["eventId"])
        con.execute("UPDATE endpoints SET status='REVOKED' WHERE endpoint_id=?", (endpoint_id,))
        con.execute("UPDATE actors SET status='REVOKED' WHERE actor_id=?", (row["actor_id"],))
        updated = con.execute("SELECT * FROM endpoints WHERE endpoint_id=?", (endpoint_id,)).fetchone()
        after_root = _aggregate_root(aggregate_type, endpoint_id, _endpoint_state(updated))
        return _finalize_mutation(
            con, request=request, capability=capability, basis=basis,
            request_hash=request_hash, recorded_at=recorded_at,
            before_root=before_root, after_root=after_root, aggregate_version=version + 1,
            created_ids=[], event_result={
                "state": "REVOKED", "reason": parameters["reason"],
                "mailboxRevocationEventIds": child_event_ids,
            },
        )

    if operation == "mailbox.allocate":
        aggregate_type = "Mailbox"
        before_root = _aggregate_root(aggregate_type, aggregate_id, None)
        if con.execute("SELECT 1 FROM mailboxes WHERE mailbox_id=?", (aggregate_id,)).fetchone():
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Mailbox already exists", {})
        _require_aggregate(
            request, aggregate_type=aggregate_type, aggregate_id=aggregate_id,
            actual_version=0, actual_root=before_root,
        )
        endpoint = con.execute(
            "SELECT * FROM endpoints WHERE endpoint_id=?", (parameters["endpointId"],)
        ).fetchone()
        domain = con.execute(
            "SELECT * FROM project_mail_domains WHERE mail_domain=?", (parameters["domain"],)
        ).fetchone()
        if not endpoint or endpoint["status"] != "ACTIVE":
            raise PostOfficeError("PON_INPUT_INVALID", "Mailbox endpoint is not active", {})
        if not domain or domain["project_id"] != endpoint["project_id"]:
            raise PostOfficeError("PON_INPUT_INVALID", "Mailbox domain does not belong to the endpoint project", {})
        scope_facts = {
            "projectIds": {endpoint["project_id"]},
            "taskIds": {endpoint["task_id"]} if endpoint["task_id"] else set(),
            "mailDomains": {parameters["domain"]},
        }
        basis = _prepare_mutation_authority(
            con, request=request, capability=capability, request_hash=request_hash,
            recorded_at=recorded_at, scope_facts=scope_facts,
        )
        con.execute(
            """INSERT INTO mailboxes(
               mailbox_id,generation,mail_domain,endpoint_id,status,created_at)
               VALUES(?,?,?,?,?,?)""",
            (aggregate_id, 1, parameters["domain"], endpoint["endpoint_id"], "ACTIVE", recorded_at),
        )
        rows = list(con.execute(
            "SELECT * FROM mailboxes WHERE mailbox_id=? ORDER BY generation", (aggregate_id,)
        ))
        after_root = _aggregate_root(aggregate_type, aggregate_id, _mailbox_state(rows, aggregate_id))
        return _finalize_mutation(
            con, request=request, capability=capability, basis=basis,
            request_hash=request_hash, recorded_at=recorded_at,
            before_root=before_root, after_root=after_root, aggregate_version=1,
            created_ids=[aggregate_id], event_result={"state": "ACTIVE", "generation": 1},
            event_link=(
                "UPDATE mailboxes SET created_event_id=? WHERE mailbox_id=? AND generation=?",
                (aggregate_id, 1),
            ),
        )

    if operation == "mailbox.rotateGeneration":
        aggregate_type = "Mailbox"
        mailbox_id = parameters["mailboxId"]
        rows = list(con.execute(
            "SELECT * FROM mailboxes WHERE mailbox_id=? ORDER BY generation", (mailbox_id,)
        ))
        if not rows:
            raise PostOfficeError("PON_INPUT_INVALID", "Mailbox does not exist", {})
        active = [row for row in rows if row["status"] == "ACTIVE"]
        if len(active) != 1:
            raise PostOfficeError("PON_DATABASE_INVALID", "Mailbox must have exactly one active generation", {})
        current = active[0]
        endpoint = con.execute(
            "SELECT * FROM endpoints WHERE endpoint_id=?", (current["endpoint_id"],)
        ).fetchone()
        if not endpoint or endpoint["status"] != "ACTIVE":
            raise PostOfficeError("PON_INPUT_INVALID", "Mailbox endpoint is not active", {})
        before_root = _aggregate_root(aggregate_type, mailbox_id, _mailbox_state(rows, mailbox_id))
        version = _aggregate_position(
            con, aggregate_type=aggregate_type, aggregate_id=mailbox_id, current_root=before_root
        )
        _require_aggregate(
            request, aggregate_type=aggregate_type, aggregate_id=mailbox_id,
            actual_version=version, actual_root=before_root,
        )
        scope_facts = {
            "projectIds": {endpoint["project_id"]},
            "taskIds": {endpoint["task_id"]} if endpoint["task_id"] else set(),
            "mailDomains": {current["mail_domain"]},
        }
        basis = _prepare_mutation_authority(
            con, request=request, capability=capability, request_hash=request_hash,
            recorded_at=recorded_at, scope_facts=scope_facts,
        )
        next_generation = max(int(row["generation"]) for row in rows) + 1
        con.execute(
            "UPDATE mailboxes SET status='RETIRED' WHERE mailbox_id=? AND generation=?",
            (mailbox_id, int(current["generation"])),
        )
        con.execute(
            """INSERT INTO mailboxes(
               mailbox_id,generation,mail_domain,endpoint_id,status,created_at)
               VALUES(?,?,?,?,?,?)""",
            (
                mailbox_id, next_generation, current["mail_domain"], current["endpoint_id"],
                "ACTIVE", recorded_at,
            ),
        )
        updated_rows = list(con.execute(
            "SELECT * FROM mailboxes WHERE mailbox_id=? ORDER BY generation", (mailbox_id,)
        ))
        after_root = _aggregate_root(
            aggregate_type, mailbox_id, _mailbox_state(updated_rows, mailbox_id)
        )
        return _finalize_mutation(
            con, request=request, capability=capability, basis=basis,
            request_hash=request_hash, recorded_at=recorded_at,
            before_root=before_root, after_root=after_root, aggregate_version=version + 1,
            created_ids=[mailbox_id],
            event_result={
                "state": "ACTIVE", "previousGeneration": int(current["generation"]),
                "generation": next_generation, "reason": parameters["reason"],
            },
            event_link=(
                "UPDATE mailboxes SET created_event_id=? WHERE mailbox_id=? AND generation=?",
                (mailbox_id, next_generation),
            ),
        )

    raise PostOfficeError("PON_OPERATION_NOT_IMPLEMENTED", "Operation is not implemented", {})


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
                "PON_PRODUCTION_MUTATION_FORBIDDEN", "Kernel mode is not permitted before cutover", {}
            )
        installed_contract_root = _contract_root(plugin_root)
        if kernel["contract_root"] != installed_contract_root:
            raise PostOfficeError(
                "PON_CONTRACT_INVALID",
                "Bootstrapped kernel contract root differs from the installed contract catalogue",
                {
                    "retainedContractRoot": kernel["contract_root"],
                    "installedContractRoot": installed_contract_root,
                },
            )
        capability = _authenticate(con, request, credential)
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
                "Operation is contracted but not implemented by the current kernel phase",
                {"operation": operation},
            )
        recorded_at = _timestamp()
        if operation == "hub.status":
            result = _status_result(con, request, request_hash, recorded_at)
        elif operation == "authority.inspect":
            result = _authority_inspect_result(con, request, request_hash, recorded_at)
        elif operation in {
            "project.read", "task.read", "provisioning.inspect", "package.read",
            "package.rehome.preview", "interface.read", "capabilityRequest.read", "bundle.verify",
            "transport.inspect", "attention.list", "hub.snapshot",
        }:
            result = _catalogue_read_result(con, request, request_hash, recorded_at)
        else:
            result = _execute_mutation(
                con,
                request=request,
                capability=capability,
                request_hash=request_hash,
                recorded_at=recorded_at,
            )
        _validate_contract(plugin_root, operation, "results", result)
        con.execute(
            """INSERT INTO idempotency_records(
               request_id,operation,canonical_request_sha256,result_json,event_id,recorded_at)
               VALUES(?,?,?,?,?,?)""",
            (
                request["requestId"], operation, request_hash,
                canonical_json_bytes(result).decode("utf-8"), result.get("eventId"), recorded_at,
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
    aggregate_type: str | None = None,
    aggregate_id: str | None = None,
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
    recorded_aggregate_type = aggregate_type or request["aggregate"]["type"]
    recorded_aggregate_id = aggregate_id or request["aggregate"]["id"]
    event_body = {
        "sequence": sequence,
        "eventId": event_id,
        "occurredAt": timestamp,
        "actorId": request["actor"]["id"],
        "operation": request["operation"],
        "capabilityId": capability_id,
        "authorityGrantId": grant_id,
        "exactAuthorActionId": action_id,
        "aggregateType": recorded_aggregate_type,
        "aggregateId": recorded_aggregate_id,
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
            capability_id, grant_id, action_id, recorded_aggregate_type,
            recorded_aggregate_id, aggregate_version,
            event_body["beforeStateRoot"], event_body["afterStateRoot"],
            canonical_json_bytes(event_result).decode("utf-8"), payload_hash,
            event_body["previousEventSha256"], event_hash,
        ),
    )
    return {"eventId": event_id, "eventSha256": event_hash, "sequence": sequence}
