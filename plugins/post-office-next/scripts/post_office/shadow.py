# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import hmac
import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from .canonical import (
    canonical_json_bytes, read_json, require_new_output_file, sha256_bytes, sha256_json,
    sqlite_content_identity, write_json,
)
from .database import inspect_database
from .diagnostics import PostOfficeError
from .kernel import (
    IMPLEMENTED_OPERATIONS,
    KERNEL_INSTANCE_ID,
    _aggregate_position,
    _aggregate_root,
    _contract_root,
    _credential,
    _open_writer,
    _operational_state,
    _timestamp,
    append_hub_event,
)


ROOT_KEYS = (
    "captureRoot", "deltaRoot", "importRoot", "replayRoot",
    "backupRoot", "restoreRoot", "performanceRoot",
)


def _cutover_guard(con: sqlite3.Connection) -> str:
    return sqlite_content_identity(
        con, excluded_tables={"cutover_dossiers", "authority_transfers"}
    )["contentsRoot"]


def _root(value: str | None, *, label: str, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    normalized = str(value or "").removeprefix("sha256:").lower()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise PostOfficeError("PON_INPUT_INVALID", f"{label} must be a SHA-256 root", {})
    return normalized


def _actor(con: sqlite3.Connection, credential_path: Path, *, author_only: bool = False) -> tuple[sqlite3.Row, sqlite3.Row]:
    credential = _credential(credential_path)
    capability = con.execute(
        "SELECT * FROM caller_capabilities WHERE capability_id=?", (credential["capabilityId"],)
    ).fetchone()
    actor = con.execute("SELECT * FROM actors WHERE actor_id=?", (capability["actor_id"],)).fetchone() if capability else None
    allowed_kinds = {"HUMAN"} if author_only else {"HUMAN", "BROWSER", "COURIER", "SYSTEM"}
    if (
        not capability or capability["status"] != "ACTIVE"
        or not hmac.compare_digest(
            str(capability["secret_sha256"]), sha256_bytes(credential["secret"].encode("utf-8"))
        )
        or not actor or actor["status"] != "ACTIVE" or actor["actor_kind"] not in allowed_kinds
        or (author_only and actor["role"] != "author")
    ):
        raise PostOfficeError("PON_AUTHORIZATION_DENIED", "Shadow operation requires an eligible authenticated actor", {})
    return capability, actor


def record_shadow_observation(
    database_path: Path, credential_path: Path, plugin_root: Path, *, source_kind: str,
    source_reference: str, expected_root: str | None, observed_root: str | None,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    expected = _root(expected_root, label="expectedRoot", optional=True)
    observed = _root(observed_root, label="observedRoot", optional=True)
    if expected and observed:
        classification = "MATCH" if expected == observed else "MISMATCH"
    elif expected and not observed:
        classification = "ABSENT"
    else:
        classification = "AMBIGUOUS"
    con = _open_writer(database_path, plugin_root)
    try:
        _, actor = _actor(con, credential_path)
        if source_kind not in {
            "LEGACY_CAPTURE", "POST_BASELINE_DELTA", "LIVE_PROJECTION", "TRANSPORT",
            "REVIEW", "PERFORMANCE", "DRIVE_ACCESS",
        }:
            raise PostOfficeError("PON_INPUT_INVALID", "Shadow observation source kind is invalid", {})
        observation_id = "PON-OBSERVATION-" + uuid.uuid4().hex
        now = _timestamp()
        evidence_value = {"actorId": actor["actor_id"], **evidence}
        evidence_json = canonical_json_bytes(evidence_value).decode("utf-8")
        con.execute(
            """INSERT INTO shadow_observations(
               observation_id,source_kind,source_reference,expected_root,observed_root,
               classification,evidence_json,evidence_root,state,recorded_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                observation_id, source_kind, source_reference, expected, observed,
                classification, evidence_json, sha256_json(evidence_value), "OPEN", now,
            ),
        )
        con.commit()
        return {"ok": True, "observationId": observation_id, "classification": classification, "state": "OPEN"}
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()


def decide_shadow_observation(
    database_path: Path, credential_path: Path, plugin_root: Path, *, observation_id: str,
    decision: str, action_id: str, rationale: str,
) -> dict[str, Any]:
    if decision not in {"RESOLVE", "ACCEPT_EXCEPTION"}:
        raise PostOfficeError("PON_INPUT_INVALID", "Observation decision is invalid", {})
    con = _open_writer(database_path, plugin_root)
    try:
        capability, actor = _actor(con, credential_path, author_only=True)
        row = con.execute("SELECT * FROM shadow_observations WHERE observation_id=?", (observation_id,)).fetchone()
        if not row or row["state"] != "OPEN":
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Shadow observation is not open", {})
        if con.execute("SELECT 1 FROM exact_author_actions WHERE action_id=?", (action_id,)).fetchone():
            raise PostOfficeError("PON_AUTHORIZATION_DENIED", "Observation decision action is already used", {})
        before = {key: row[key] for key in row.keys()}
        before_root = _aggregate_root("ShadowObservation", observation_id, before)
        version = _aggregate_position(
            con, aggregate_type="ShadowObservation", aggregate_id=observation_id, current_root=before_root
        )
        now = _timestamp()
        scope = {"observationId": observation_id, "decision": decision, "rationale": rationale}
        con.execute(
            """INSERT INTO exact_author_actions(
               action_id,actor_id,operation,scope_json,confirmation_sha256,recorded_at)
               VALUES(?,?,?,?,?,?)""",
            (action_id, actor["actor_id"], "shadow.observation.decide", canonical_json_bytes(scope).decode("utf-8"), sha256_json(scope), now),
        )
        state = "RESOLVED" if decision == "RESOLVE" else "ACCEPTED_EXCEPTION"
        con.execute(
            "UPDATE shadow_observations SET state=?,resolved_at=? WHERE observation_id=?",
            (state, now, observation_id),
        )
        updated = con.execute("SELECT * FROM shadow_observations WHERE observation_id=?", (observation_id,)).fetchone()
        after_root = _aggregate_root("ShadowObservation", observation_id, {key: updated[key] for key in updated.keys()})
        request = {
            "operation": "shadow.observation.decide", "requestId": "PON-REQUEST-" + uuid.uuid4().hex,
            "actor": {"id": actor["actor_id"], "kind": actor["actor_kind"], "role": actor["role"]},
            "authority": {"capabilityId": capability["capability_id"], "exactAuthorActionId": action_id},
            "aggregate": {"type": "ShadowObservation", "id": observation_id}, "parameters": scope,
        }
        event = append_hub_event(
            con, request=request, capability_id=capability["capability_id"], aggregate_version=version + 1,
            before_root=before_root, after_root=after_root, event_result={"state": state, "rationale": rationale},
            occurred_at=now,
        )
        con.execute("UPDATE exact_author_actions SET consumed_event_id=? WHERE action_id=?", (event["eventId"], action_id))
        con.commit()
        return {"ok": True, "observationId": observation_id, "state": state, "eventId": event["eventId"]}
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()


def record_cutover_rehearsal(
    database_path: Path, credential_path: Path, plugin_root: Path, *, evidence: dict[str, Any]
) -> dict[str, Any]:
    roots = {key: _root(evidence.get(key), label=key) for key in ROOT_KEYS}
    checks = evidence.get("checks")
    if not isinstance(checks, dict) or not checks or any(value is not True for value in checks.values()):
        raise PostOfficeError("PON_INPUT_INVALID", "Every rehearsal check must be explicitly true", {})
    passed = roots["importRoot"] == roots["replayRoot"]
    retained = {**evidence, **roots, "deterministicReplay": passed}
    con = _open_writer(database_path, plugin_root)
    try:
        _actor(con, credential_path, author_only=True)
        rehearsal_id = "PON-REHEARSAL-" + uuid.uuid4().hex
        now = _timestamp()
        con.execute(
            "INSERT INTO cutover_rehearsals VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rehearsal_id, roots["captureRoot"], roots["deltaRoot"], roots["importRoot"],
                roots["replayRoot"], roots["backupRoot"], roots["restoreRoot"], roots["performanceRoot"],
                int(passed), canonical_json_bytes(retained).decode("utf-8"), sha256_json(retained), now,
            ),
        )
        con.commit()
        return {"ok": True, "rehearsalId": rehearsal_id, "passed": passed, "evidenceRoot": sha256_json(retained)}
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()


def create_cutover_dossier(
    database_path: Path, credential_path: Path, plugin_root: Path, *, legacy_final_root: str,
    output: Path,
) -> dict[str, Any]:
    legacy_root = _root(legacy_final_root, label="legacyFinalRoot")
    output = require_new_output_file(output, inputs=[database_path, credential_path])
    inspection = inspect_database(database_path, plugin_root)
    con = _open_writer(database_path, plugin_root)
    try:
        _, actor = _actor(con, credential_path, author_only=True)
        kernel = con.execute("SELECT * FROM kernel_instances WHERE instance_id=?", (KERNEL_INSTANCE_ID,)).fetchone()
        rehearsal = con.execute("SELECT * FROM cutover_rehearsals ORDER BY recorded_at DESC,rehearsal_id DESC LIMIT 1").fetchone()
        rehearsal_evidence = json.loads(rehearsal["evidence_json"]) if rehearsal else {}
        rehearsal_checks = rehearsal_evidence.get("checks", {})
        counts = {
            "openAnomalies": int(con.execute("SELECT COUNT(*) FROM shadow_observations WHERE state='OPEN'").fetchone()[0]),
            "openAttention": int(con.execute("SELECT COUNT(*) FROM attention_items WHERE state='OPEN'").fetchone()[0]),
            "nonterminalDispatches": int(con.execute("SELECT COUNT(*) FROM transport_dispatches WHERE state NOT IN ('RECEIPTED','CANCELLED','TERMINAL_FAILURE')").fetchone()[0]),
            "nonterminalReviews": int(con.execute("SELECT COUNT(*) FROM automatic_reviews WHERE state IN ('QUEUED','ACTIVE','RETURNED')").fetchone()[0]),
            "unexpectedDriveAccesses": int(con.execute("SELECT COALESCE(SUM(access_count),0) FROM drive_access_metrics WHERE purpose='UNEXPECTED'").fetchone()[0]),
            "nonterminalMessages": int(con.execute("SELECT COUNT(*) FROM semantic_messages WHERE state NOT IN ('DELIVERED','ACKNOWLEDGED','ACCEPTED','CORRECTION_REQUIRED','REJECTED','CLOSED','QUARANTINED','FAILED_FINAL')").fetchone()[0]),
            "readyContinuations": int(con.execute("SELECT COUNT(*) FROM continuation_items WHERE state IN ('READY','LEASED')").fetchone()[0]),
        }
        active_couriers = int(con.execute("SELECT COUNT(*) FROM actors WHERE actor_kind IN ('COURIER','SYSTEM') AND status='ACTIVE'").fetchone()[0])
        assertions = {
            "kernelShadow": bool(kernel and kernel["mode"] == "SHADOW"),
            "kernelReady": bool(kernel and kernel["status"] == "READY"),
            "authorityStillPreview": bool(kernel and kernel["authority_state"] == "PREVIEW"),
            "allContractOperationsImplemented": len(IMPLEMENTED_OPERATIONS) == 64,
            "contractRootCurrent": bool(kernel and kernel["contract_root"] == _contract_root(plugin_root)),
            "databaseValid": True,
            "passedRehearsal": bool(rehearsal and rehearsal["passed"]),
            "noOpenAnomalies": counts["openAnomalies"] == 0,
            "noOpenAttention": counts["openAttention"] == 0,
            "dispatchesDrained": counts["nonterminalDispatches"] == 0,
            "reviewWorkRetained": bool(rehearsal_checks.get("reviewWorkRetained")),
            # Open semantic work is durable business state, not unfinished transport. It may
            # cross the authority boundary only when the final rehearsal explicitly proves
            # that its identities and states were retained exactly.
            "openWorkRetained": bool(rehearsal_checks.get("openWorkRetained")),
            "continuationWorkRetained": bool(rehearsal_checks.get("continuationWorkRetained")),
            "noUnexpectedDriveAccess": counts["unexpectedDriveAccesses"] == 0,
            "activeCourierPresent": active_couriers > 0,
        }
        ready = all(assertions.values())
        checks = {"assertions": assertions, "stateGuardRoot": _cutover_guard(con)}
        dossier_id = "PON-DOSSIER-" + uuid.uuid4().hex
        identity = {
            "dossierId": dossier_id, "rehearsalId": rehearsal["rehearsal_id"] if rehearsal else None,
            "contractRoot": _contract_root(plugin_root),
            "databaseRoot": inspection["logicalStateRoot"], "legacyFinalRoot": legacy_root,
            "counts": counts, "checks": checks, "ready": ready, "authorActorId": actor["actor_id"],
        }
        dossier_root = sha256_json(identity)
        if rehearsal:
            con.execute(
                """INSERT INTO cutover_dossiers VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    dossier_id, rehearsal["rehearsal_id"], identity["contractRoot"], identity["databaseRoot"],
                    legacy_root, counts["openAnomalies"], counts["openAttention"], counts["nonterminalDispatches"],
                    counts["nonterminalReviews"], counts["unexpectedDriveAccesses"], int(ready),
                    canonical_json_bytes(checks).decode("utf-8"), dossier_root, _timestamp(),
                ),
            )
            con.commit()
        else:
            con.rollback()
        document = {**identity, "dossierRoot": dossier_root}
        write_json(output, document)
        return {"ok": True, "ready": ready, "dossierId": dossier_id if rehearsal else None,
                "dossierRoot": dossier_root, "checks": checks, "output": str(output.resolve())}
    except Exception:
        if con.in_transaction:
            con.rollback()
        if os.path.lexists(output):
            output.unlink()
        raise
    finally:
        con.close()


def execute_cutover(
    database_path: Path, credential_path: Path, plugin_root: Path, *, dossier_id: str,
    dossier_root: str, legacy_state_root: Path, pointer_output: Path, action_id: str,
) -> dict[str, Any]:
    expected_dossier_root = _root(dossier_root, label="dossierRoot")
    legacy_state_root = legacy_state_root.resolve(strict=True)
    pointer_output = require_new_output_file(pointer_output, inputs=[database_path, credential_path, legacy_state_root])
    con = _open_writer(database_path, plugin_root)
    try:
        capability, actor = _actor(con, credential_path, author_only=True)
        kernel = con.execute("SELECT * FROM kernel_instances WHERE instance_id=?", (KERNEL_INSTANCE_ID,)).fetchone()
        dossier = con.execute("SELECT * FROM cutover_dossiers WHERE dossier_id=?", (dossier_id,)).fetchone()
        if not kernel or kernel["mode"] != "SHADOW" or kernel["authority_state"] != "PREVIEW":
            raise PostOfficeError("PON_PRODUCTION_MUTATION_FORBIDDEN", "Kernel is not an eligible non-authoritative shadow", {})
        if not dossier or not dossier["ready"] or dossier["dossier_root"] != expected_dossier_root:
            raise PostOfficeError("PON_PRODUCTION_MUTATION_FORBIDDEN", "Exact ready cutover dossier is not retained", {})
        inspect_database(database_path, plugin_root)
        retained_checks = json.loads(dossier["checks_json"])
        if _cutover_guard(con) != retained_checks.get("stateGuardRoot"):
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "vNext state changed after dossier creation", {})
        if con.execute("SELECT 1 FROM authority_transfers WHERE state='COMMITTED'").fetchone():
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Production authority was already transferred", {})
        if con.execute("SELECT 1 FROM exact_author_actions WHERE action_id=?", (action_id,)).fetchone():
            raise PostOfficeError("PON_AUTHORIZATION_DENIED", "Cutover author action is already used", {})
        transfer_id = "PON-TRANSFER-" + uuid.uuid4().hex
        now = _timestamp()
        scope = {"dossierId": dossier_id, "dossierRoot": expected_dossier_root,
                 "legacyStateRoot": str(legacy_state_root), "vnextDatabasePath": str(database_path.resolve())}
        con.execute(
            "INSERT INTO exact_author_actions(action_id,actor_id,operation,scope_json,confirmation_sha256,recorded_at) VALUES(?,?,?,?,?,?)",
            (action_id, actor["actor_id"], "cutover.execute", canonical_json_bytes(scope).decode("utf-8"), sha256_json(scope), now),
        )
        con.execute(
            "INSERT INTO authority_transfers VALUES(?,?,?,?,?,?,?,?,?,?)",
            (transfer_id, dossier_id, expected_dossier_root, str(legacy_state_root), str(database_path.resolve()), str(pointer_output.resolve()), "PREPARED", action_id, None, None),
        )
        con.commit()
        pointer = {
            "schemaVersion": "1", "state": "COMMITTED", "transferId": transfer_id,
            "dossierId": dossier_id, "dossierRoot": expected_dossier_root,
            "legacyStateRoot": str(legacy_state_root), "vnextDatabasePath": str(database_path.resolve()),
            "validationRule": "Both this pointer and the retained COMMITTED authority transfer must match",
        }
        write_json(pointer_output, {**pointer, "pointerRoot": sha256_json(pointer)})
        event = _finish_prepared_transfer(con, capability, actor, transfer_id)
        return {"ok": True, "transferId": transfer_id, "state": "COMMITTED",
                "pointer": str(pointer_output.resolve()), "eventId": event["eventId"]}
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()


def _validated_transfer_pointer(transfer: sqlite3.Row) -> dict[str, Any]:
    pointer_path = Path(str(transfer["pointer_path"]))
    if not pointer_path.is_file():
        raise PostOfficeError(
            "PON_PATH_NOT_FOUND", "Prepared authority transfer has no activation pointer",
            {"pointer": str(pointer_path)},
        )
    pointer = read_json(pointer_path)
    if not isinstance(pointer, dict):
        raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Activation pointer is not an object", {})
    unsigned = {key: value for key, value in pointer.items() if key != "pointerRoot"}
    expected = {
        "schemaVersion": "1", "state": "COMMITTED", "transferId": transfer["transfer_id"],
        "dossierId": transfer["dossier_id"], "dossierRoot": transfer["dossier_root"],
        "legacyStateRoot": transfer["legacy_state_root"],
        "vnextDatabasePath": transfer["vnext_database_path"],
        "validationRule": "Both this pointer and the retained COMMITTED authority transfer must match",
    }
    if unsigned != expected or pointer.get("pointerRoot") != sha256_json(expected):
        raise PostOfficeError(
            "PON_SNAPSHOT_INTEGRITY_FAILURE", "Activation pointer does not match the prepared transfer",
            {"pointer": str(pointer_path)},
        )
    return pointer


def _finish_prepared_transfer(
    con: sqlite3.Connection, capability: sqlite3.Row, actor: sqlite3.Row, transfer_id: str,
) -> dict[str, Any]:
    transfer = con.execute("SELECT * FROM authority_transfers WHERE transfer_id=?", (transfer_id,)).fetchone()
    if not transfer or transfer["state"] != "PREPARED":
        raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Authority transfer is not finishable", {})
    kernel = con.execute("SELECT * FROM kernel_instances WHERE instance_id=?", (KERNEL_INSTANCE_ID,)).fetchone()
    if not kernel or kernel["authority_state"] != "PREVIEW":
        raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Kernel authority is not at the prepared boundary", {})
    pointer = _validated_transfer_pointer(transfer)
    action = con.execute(
        "SELECT * FROM exact_author_actions WHERE action_id=?", (transfer["exact_author_action_id"],)
    ).fetchone()
    if not action or action["consumed_event_id"] is not None or action["actor_id"] != actor["actor_id"]:
        raise PostOfficeError("PON_AUTHORIZATION_DENIED", "Prepared cutover action is absent, consumed, or actor-mismatched", {})
    scope = json.loads(action["scope_json"])
    if action["operation"] != "cutover.execute" or action["confirmation_sha256"] != sha256_json(scope):
        raise PostOfficeError("PON_AUTHORIZATION_DENIED", "Prepared cutover action failed integrity validation", {})
    now = _timestamp()
    if not con.in_transaction:
        con.execute("BEGIN IMMEDIATE")
    before_root = _aggregate_root("AuthorityTransfer", transfer_id, {**scope, "state": "PREPARED"})
    updated = con.execute(
        "UPDATE authority_transfers SET state='COMMITTED',committed_at=? WHERE transfer_id=? AND state='PREPARED'",
        (now, transfer_id),
    ).rowcount
    kernel_updated = con.execute(
        "UPDATE kernel_instances SET authority_state='AUTHORITATIVE' WHERE instance_id=? AND authority_state='PREVIEW'",
        (KERNEL_INSTANCE_ID,),
    ).rowcount
    if updated != 1 or kernel_updated != 1:
        raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Prepared authority boundary changed", {})
    after_root = _aggregate_root(
        "AuthorityTransfer", transfer_id,
        {**scope, "state": "COMMITTED", "pointer": transfer["pointer_path"]},
    )
    request = {
        "operation": "cutover.execute", "requestId": "PON-REQUEST-" + uuid.uuid4().hex,
        "actor": {"id": actor["actor_id"], "kind": actor["actor_kind"], "role": actor["role"]},
        "authority": {
            "capabilityId": capability["capability_id"],
            "exactAuthorActionId": transfer["exact_author_action_id"],
        },
        "aggregate": {"type": "AuthorityTransfer", "id": transfer_id}, "parameters": scope,
    }
    event = append_hub_event(
        con, request=request, capability_id=capability["capability_id"], aggregate_version=1,
        before_root=before_root, after_root=after_root,
        event_result={"state": "COMMITTED", "pointer": transfer["pointer_path"], "pointerRoot": pointer["pointerRoot"]},
        occurred_at=now,
    )
    con.execute(
        "UPDATE exact_author_actions SET consumed_event_id=? WHERE action_id=? AND consumed_event_id IS NULL",
        (event["eventId"], transfer["exact_author_action_id"]),
    )
    con.commit()
    return event


def finish_prepared_cutover(
    database_path: Path, credential_path: Path, plugin_root: Path, *, transfer_id: str,
) -> dict[str, Any]:
    """Finish the exact prepared transfer after an interruption following pointer publication."""
    inspect_database(database_path, plugin_root)
    con = _open_writer(database_path, plugin_root)
    try:
        capability, actor = _actor(con, credential_path, author_only=True)
        event = _finish_prepared_transfer(con, capability, actor, transfer_id)
        transfer = con.execute("SELECT * FROM authority_transfers WHERE transfer_id=?", (transfer_id,)).fetchone()
        return {
            "ok": True, "transferId": transfer_id, "state": "COMMITTED",
            "pointer": transfer["pointer_path"], "eventId": event["eventId"], "recovered": True,
        }
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()


def cutover_preflight(
    database_path: Path, credential_path: Path, plugin_root: Path, *, dossier_id: str,
    dossier_root: str,
) -> dict[str, Any]:
    expected = _root(dossier_root, label="dossierRoot")
    inspect_database(database_path, plugin_root)
    con = _open_writer(database_path, plugin_root)
    try:
        _actor(con, credential_path, author_only=True)
        kernel = con.execute("SELECT * FROM kernel_instances WHERE instance_id=?", (KERNEL_INSTANCE_ID,)).fetchone()
        dossier = con.execute("SELECT * FROM cutover_dossiers WHERE dossier_id=?", (dossier_id,)).fetchone()
        retained_checks = json.loads(dossier["checks_json"]) if dossier else {}
        checks = {
            "exactReadyDossier": bool(dossier and dossier["ready"] and dossier["dossier_root"] == expected),
            "shadowMode": bool(kernel and kernel["mode"] == "SHADOW"),
            "authorityStillPreview": bool(kernel and kernel["authority_state"] == "PREVIEW"),
            "stateUnchangedSinceDossier": bool(dossier and _cutover_guard(con) == retained_checks.get("stateGuardRoot")),
            "noCommittedTransfer": con.execute("SELECT 1 FROM authority_transfers WHERE state='COMMITTED'").fetchone() is None,
        }
        con.rollback()
        return {"ok": True, "ready": all(checks.values()), "dossierId": dossier_id,
                "dossierRoot": expected, "checks": checks}
    finally:
        con.close()


def rollback_pre_authority(
    database_path: Path, credential_path: Path, plugin_root: Path, *, transfer_id: str,
    action_id: str, reason: str,
) -> dict[str, Any]:
    con = _open_writer(database_path, plugin_root)
    try:
        capability, actor = _actor(con, credential_path, author_only=True)
        transfer = con.execute("SELECT * FROM authority_transfers WHERE transfer_id=?", (transfer_id,)).fetchone()
        if not transfer or transfer["state"] != "PREPARED":
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Authority transfer is not rollback-eligible", {})
        if os.path.lexists(transfer["pointer_path"]):
            raise PostOfficeError(
                "PON_PRODUCTION_MUTATION_FORBIDDEN",
                "Activation pointer exists; reconcile or finish the prepared transfer instead of rolling back",
                {"pointer": transfer["pointer_path"]},
            )
        if con.execute("SELECT 1 FROM exact_author_actions WHERE action_id=?", (action_id,)).fetchone():
            raise PostOfficeError("PON_AUTHORIZATION_DENIED", "Rollback author action is already used", {})
        now = _timestamp()
        scope = {"transferId": transfer_id, "reason": reason}
        con.execute(
            "INSERT INTO exact_author_actions(action_id,actor_id,operation,scope_json,confirmation_sha256,recorded_at) VALUES(?,?,?,?,?,?)",
            (action_id, actor["actor_id"], "cutover.rollbackPreAuthority", canonical_json_bytes(scope).decode("utf-8"), sha256_json(scope), now),
        )
        before_root = _aggregate_root("AuthorityTransfer", transfer_id, {"state": "PREPARED", "dossierRoot": transfer["dossier_root"]})
        con.execute("UPDATE authority_transfers SET state='ROLLED_BACK',rollback_reason=? WHERE transfer_id=?", (reason, transfer_id))
        after_root = _aggregate_root("AuthorityTransfer", transfer_id, {"state": "ROLLED_BACK", "dossierRoot": transfer["dossier_root"], "reason": reason})
        request = {
            "operation": "cutover.rollbackPreAuthority", "requestId": "PON-REQUEST-" + uuid.uuid4().hex,
            "actor": {"id": actor["actor_id"], "kind": actor["actor_kind"], "role": actor["role"]},
            "authority": {"capabilityId": capability["capability_id"], "exactAuthorActionId": action_id},
            "aggregate": {"type": "AuthorityTransfer", "id": transfer_id}, "parameters": scope,
        }
        event = append_hub_event(
            con, request=request, capability_id=capability["capability_id"], aggregate_version=1,
            before_root=before_root, after_root=after_root, event_result={"state": "ROLLED_BACK", "reason": reason}, occurred_at=now,
        )
        con.execute("UPDATE exact_author_actions SET consumed_event_id=? WHERE action_id=?", (event["eventId"], action_id))
        con.commit()
        return {"ok": True, "transferId": transfer_id, "state": "ROLLED_BACK", "eventId": event["eventId"]}
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()
