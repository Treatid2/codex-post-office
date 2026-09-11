# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import hmac
import json
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .canonical import canonical_json_bytes, sha256_bytes, sha256_json
from .diagnostics import PostOfficeError
from .kernel import (
    _aggregate_position,
    _aggregate_root,
    _credential,
    _message_state,
    _open_writer,
    _operational_state,
    _parse_timestamp,
    _timestamp,
    _transport_state,
    append_hub_event,
)


def _courier(con: sqlite3.Connection, credential_path: Path) -> tuple[sqlite3.Row, sqlite3.Row]:
    credential = _credential(credential_path)
    capability = con.execute(
        "SELECT * FROM caller_capabilities WHERE capability_id=?", (credential["capabilityId"],)
    ).fetchone()
    actor = con.execute("SELECT * FROM actors WHERE actor_id=?", (capability["actor_id"],)).fetchone() if capability else None
    if (
        not capability or capability["status"] != "ACTIVE"
        or not hmac.compare_digest(
            str(capability["secret_sha256"]), sha256_bytes(credential["secret"].encode("utf-8"))
        )
        or (capability["expires_at"] and _parse_timestamp(capability["expires_at"]) <= datetime.now(timezone.utc))
        or not actor or actor["status"] != "ACTIVE" or actor["actor_kind"] not in {"COURIER", "SYSTEM"}
    ):
        raise PostOfficeError("PON_AUTHENTICATION_FAILED", "Runtime credential is not an active courier capability", {})
    return capability, actor


def _message_authority(message: sqlite3.Row) -> dict[str, str]:
    if message["authority_grant_id"]:
        return {"authorityGrantId": str(message["authority_grant_id"])}
    return {"exactAuthorActionId": str(message["exact_author_action_id"])}


def _destination_from_scope(scope: dict[str, Any]) -> dict[str, str | None]:
    """Normalize native and browser endpoint bindings for courier adapters."""
    browser = scope.get("browserBinding")
    browser = browser if isinstance(browser, dict) else {}
    return {
        "destinationThreadId": scope.get("threadId") or browser.get("chat_thread_id"),
        "destinationHostId": scope.get("hostId") or browser.get("host_id"),
        "destinationUrl": scope.get("url") or browser.get("chat_url"),
    }


def _continuation_destination(
    con: sqlite3.Connection, payload: dict[str, Any]
) -> dict[str, str | int | None]:
    candidates = (
        ("recipient_mailbox_id", "recipient_generation"),
        ("reviewer_mailbox_id", "reviewer_generation"),
        ("requester_mailbox_id", "requester_generation"),
        ("browser_mailbox_id", "browser_generation"),
    )
    for mailbox_key, generation_key in candidates:
        mailbox_id = payload.get(mailbox_key)
        if not mailbox_id:
            continue
        generation = payload.get(generation_key)
        if generation is None:
            mailbox = con.execute(
                "SELECT * FROM mailboxes WHERE mailbox_id=? AND status='ACTIVE' "
                "ORDER BY generation DESC LIMIT 1",
                (mailbox_id,),
            ).fetchone()
        else:
            mailbox = con.execute(
                "SELECT * FROM mailboxes WHERE mailbox_id=? AND generation=?",
                (mailbox_id, int(generation)),
            ).fetchone()
        if not mailbox:
            return {
                "destinationMailboxId": str(mailbox_id),
                "destinationGeneration": int(generation) if generation is not None else None,
                "destinationEndpointId": None,
                "destinationThreadId": None,
                "destinationHostId": None,
                "destinationUrl": None,
            }
        endpoint = con.execute(
            "SELECT * FROM endpoints WHERE endpoint_id=?", (mailbox["endpoint_id"],)
        ).fetchone()
        scope = json.loads(endpoint["access_scope_json"]) if endpoint else {}
        return {
            "destinationMailboxId": str(mailbox["mailbox_id"]),
            "destinationGeneration": int(mailbox["generation"]),
            "destinationEndpointId": str(mailbox["endpoint_id"]),
            **_destination_from_scope(scope),
        }
    return {
        "destinationMailboxId": None,
        "destinationGeneration": None,
        "destinationEndpointId": None,
        "destinationThreadId": None,
        "destinationHostId": None,
        "destinationUrl": None,
    }


def _runtime_request(
    *, actor: sqlite3.Row, capability: sqlite3.Row, message: sqlite3.Row,
    operation: str, aggregate_type: str, aggregate_id: str, parameters: dict[str, Any],
) -> dict[str, Any]:
    return {
        "operation": operation, "requestId": "PON-RUNTIME-REQUEST-" + uuid.uuid4().hex,
        "actor": {"id": actor["actor_id"], "kind": actor["actor_kind"], "role": actor["role"]},
        "authority": {"capabilityId": capability["capability_id"], **_message_authority(message)},
        "aggregate": {"type": aggregate_type, "id": aggregate_id}, "parameters": parameters,
    }


def _record_runtime_receipt(
    con: sqlite3.Connection, dispatch_id: str, kind: str, evidence: dict[str, Any], recorded_at: str
) -> str:
    receipt_id = "PON-RUNTIME-RECEIPT-" + uuid.uuid4().hex
    con.execute(
        "INSERT INTO runtime_receipts VALUES(?,?,?,?,?,?)",
        (receipt_id, dispatch_id, kind, canonical_json_bytes(evidence).decode("utf-8"), sha256_json(evidence), recorded_at),
    )
    return receipt_id


def _review_state(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "reviewId": row["review_id"], "semanticMessageId": row["semantic_message_id"],
        "requesterTaskId": row["requester_task_id"], "reviewerEndpointId": row["reviewer_endpoint_id"],
        "packageSha256": row["package_sha256"], "state": row["state"],
        "resultMessageId": row["result_message_id"], "wakeDispatchId": row["wake_dispatch_id"],
        "requesterThreadId": row["requester_thread_id"], "reviewerThreadId": row["reviewer_thread_id"],
    }


def _attention(
    con: sqlite3.Connection, *, entity_type: str, entity_id: str, severity: str,
    reason_code: str, details: dict[str, Any], recorded_at: str,
) -> str:
    retained = con.execute(
        "SELECT attention_id FROM attention_items WHERE entity_type=? AND entity_id=? AND reason_code=? AND state='OPEN'",
        (entity_type, entity_id, reason_code),
    ).fetchone()
    if retained:
        return str(retained[0])
    attention_id = "PON-ATTENTION-" + uuid.uuid4().hex
    con.execute(
        "INSERT INTO attention_items VALUES(?,?,?,?,?,?,?,?,?)",
        (attention_id, entity_type, entity_id, severity, reason_code,
         canonical_json_bytes(details).decode("utf-8"), "OPEN", recorded_at, None),
    )
    return attention_id


def reconcile_transport(
    database_path: Path, credential_path: Path, plugin_root: Path,
    *, observations: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Materialize pending dispatches and reconcile expired leases once.

    Observation keys are dispatch IDs. A positive exact marker plus receipt completes delivery; an
    explicit marker absence requeues safely; unknown evidence becomes attention, never a resend.
    """
    observations = observations or {}
    con = _open_writer(database_path, plugin_root)
    try:
        capability, actor = _courier(con, credential_path)
        now = _timestamp()
        created: list[str] = []
        recovered: list[str] = []
        attention_ids: list[str] = []
        for attempt in con.execute("SELECT * FROM transport_attempts WHERE state='PENDING' ORDER BY created_at,transport_attempt_id"):
            if con.execute("SELECT 1 FROM transport_dispatches WHERE transport_attempt_id=?", (attempt["transport_attempt_id"],)).fetchone():
                continue
            message = con.execute("SELECT * FROM semantic_messages WHERE message_id=?", (attempt["message_id"],)).fetchone()
            mailbox = con.execute("SELECT * FROM mailboxes WHERE mailbox_id=? AND generation=?", (message["recipient_mailbox_id"], message["recipient_generation"])).fetchone()
            endpoint = con.execute("SELECT * FROM endpoints WHERE endpoint_id=?", (mailbox["endpoint_id"],)).fetchone()
            bundle = con.execute("SELECT * FROM message_bundles WHERE bundle_id=?", (attempt["bundle_id"],)).fetchone()
            payloads = list(con.execute("SELECT * FROM bundle_payloads WHERE bundle_id=? ORDER BY ordinal", (bundle["bundle_id"],)))
            missing = [
                str(payload["sha256"]) for payload in payloads
                if not con.execute("SELECT 1 FROM storage_copies WHERE content_sha256=?", (payload["sha256"],)).fetchone()
            ]
            aggregate_copy = con.execute(
                "SELECT 1 FROM storage_copies WHERE content_sha256=?", (bundle["sha256"],)
            ).fetchone()
            if (not payloads or missing) and not aggregate_copy:
                attention_ids.append(_attention(
                    con, entity_type="TransportAttempt", entity_id=attempt["transport_attempt_id"], severity="CRITICAL",
                    reason_code="PON_CUSTODY_NOT_VERIFIED", details={"bundleId": bundle["bundle_id"], "missingPayloads": missing}, recorded_at=now,
                ))
                continue
            dispatch_id = "PON-DISPATCH-" + uuid.uuid4().hex
            channel = "PLAYWRIGHT_BROWSER" if endpoint["actor_id"] and con.execute("SELECT actor_kind FROM actors WHERE actor_id=?", (endpoint["actor_id"],)).fetchone()[0] == "BROWSER" else "NATIVE_TASK"
            marker = f"POST OFFICE DELIVERY {dispatch_id} {message['message_id']}"
            con.execute(
                """INSERT INTO transport_dispatches(dispatch_id,transport_attempt_id,channel,
                   destination_endpoint_id,destination_mailbox_id,destination_generation,state,
                   observable_marker,attempt_count,next_attempt_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (dispatch_id, attempt["transport_attempt_id"], channel, endpoint["endpoint_id"],
                 mailbox["mailbox_id"], int(mailbox["generation"]), "READY", marker, 0, now, now, now),
            )
            created.append(dispatch_id)
        expired = list(con.execute(
            "SELECT * FROM transport_dispatches WHERE state='LEASED' AND lease_expires_at<=? ORDER BY lease_expires_at,dispatch_id",
            (now,),
        ))
        for dispatch in expired:
            observation = observations.get(str(dispatch["dispatch_id"]))
            if observation and observation.get("marker") == dispatch["observable_marker"] and observation.get("receiptId"):
                _complete_locked(con, capability, actor, dispatch, observation, now)
                recovered.append(str(dispatch["dispatch_id"]))
            elif observation and observation.get("markerAbsent") is True:
                con.execute(
                    """UPDATE transport_dispatches SET state='READY',lease_owner_actor_id=NULL,
                       lease_token_sha256=NULL,lease_expires_at=NULL,next_attempt_at=?,last_error_code=NULL,updated_at=?
                       WHERE dispatch_id=?""",
                    (now, now, dispatch["dispatch_id"]),
                )
                _record_runtime_receipt(con, dispatch["dispatch_id"], "RECOVERED", {"outcome": "SAFE_REQUEUE", "markerAbsent": True}, now)
                recovered.append(str(dispatch["dispatch_id"]))
            else:
                con.execute("UPDATE transport_dispatches SET state='RECONCILIATION_REQUIRED',last_error_code='PON_OBSERVATION_AMBIGUOUS',updated_at=? WHERE dispatch_id=?", (now, dispatch["dispatch_id"]))
                attention_ids.append(_attention(
                    con, entity_type="TransportDispatch", entity_id=dispatch["dispatch_id"], severity="ERROR",
                    reason_code="PON_OBSERVATION_AMBIGUOUS", details={"observableMarker": dispatch["observable_marker"]}, recorded_at=now,
                ))
        con.commit()
        return {"ok": True, "createdDispatchIds": created, "recoveredDispatchIds": recovered,
                "attentionIds": attention_ids, "stateRoot": _operational_state(con)["operationalStateRoot"]}
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()


def claim_next_transport(
    database_path: Path, credential_path: Path, plugin_root: Path, *, lease_seconds: int = 600
) -> dict[str, Any]:
    if lease_seconds < 30 or lease_seconds > 1800:
        raise PostOfficeError("PON_INPUT_INVALID", "Transport lease must be between 30 and 1800 seconds", {})
    con = _open_writer(database_path, plugin_root)
    try:
        capability, actor = _courier(con, credential_path)
        now = _timestamp()
        dispatch = con.execute(
            "SELECT * FROM transport_dispatches WHERE state='READY' AND next_attempt_at<=? ORDER BY created_at,dispatch_id LIMIT 1",
            (now,),
        ).fetchone()
        if not dispatch:
            con.rollback()
            return {"ok": True, "available": False}
        token = secrets.token_urlsafe(32)
        expires = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat().replace("+00:00", "Z")
        con.execute(
            """UPDATE transport_dispatches SET state='LEASED',lease_owner_actor_id=?,lease_token_sha256=?,
               lease_expires_at=?,attempt_count=attempt_count+1,updated_at=? WHERE dispatch_id=? AND state='READY'""",
            (actor["actor_id"], sha256_bytes(token.encode("utf-8")), expires, now, dispatch["dispatch_id"]),
        )
        receipt_id = _record_runtime_receipt(con, dispatch["dispatch_id"], "LEASED", {"actorId": actor["actor_id"], "expiresAt": expires}, now)
        attempt = con.execute("SELECT * FROM transport_attempts WHERE transport_attempt_id=?", (dispatch["transport_attempt_id"],)).fetchone()
        bundle = con.execute("SELECT * FROM message_bundles WHERE bundle_id=?", (attempt["bundle_id"],)).fetchone()
        destination_endpoint = con.execute(
            "SELECT * FROM endpoints WHERE endpoint_id=?", (dispatch["destination_endpoint_id"],)
        ).fetchone()
        destination_scope = json.loads(destination_endpoint["access_scope_json"]) if destination_endpoint else {}
        destination = _destination_from_scope(destination_scope)
        def local_path(value: str | None) -> str | None:
            if not value:
                return None
            candidate = Path(value)
            if not candidate.is_absolute():
                candidate = database_path.resolve().parent / candidate
            return str(candidate.resolve())
        payloads = []
        for payload in con.execute("SELECT * FROM bundle_payloads WHERE bundle_id=? ORDER BY ordinal", (bundle["bundle_id"],)):
            copy = con.execute("SELECT * FROM storage_copies WHERE content_sha256=? ORDER BY CASE location_kind WHEN 'LOCAL_CAS' THEN 0 ELSE 1 END,storage_copy_id LIMIT 1", (payload["sha256"],)).fetchone()
            if copy:
                payloads.append({"ordinal": int(payload["ordinal"]), "relativePath": payload["relative_path"], "sizeBytes": int(payload["size_bytes"]), "sha256": payload["sha256"], "localLocation": local_path(copy["local_location"])})
        bundle_copy = con.execute("SELECT * FROM storage_copies WHERE content_sha256=? ORDER BY CASE location_kind WHEN 'LOCAL_CAS' THEN 0 ELSE 1 END,storage_copy_id LIMIT 1", (bundle["sha256"],)).fetchone()
        if len(payloads) != int(con.execute("SELECT COUNT(*) FROM bundle_payloads WHERE bundle_id=?", (bundle["bundle_id"],)).fetchone()[0]):
            if not bundle_copy:
                raise PostOfficeError("PON_CUSTODY_NOT_VERIFIED", "Leased bundle lacks complete payload or archive custody", {"bundleId": bundle["bundle_id"]})
            payloads = []
        con.commit()
        return {
            "ok": True, "available": True, "dispatchId": dispatch["dispatch_id"], "leaseToken": token,
            "leaseExpiresAt": expires, "channel": dispatch["channel"], "destinationEndpointId": dispatch["destination_endpoint_id"],
            "destinationMailboxId": dispatch["destination_mailbox_id"], "destinationGeneration": int(dispatch["destination_generation"]),
            **destination,
            "observableMarker": dispatch["observable_marker"], "bundleId": bundle["bundle_id"],
            "bundleSha256": bundle["sha256"], "bundleArchiveLocation": local_path(bundle_copy["local_location"]) if bundle_copy else None,
            "payloads": payloads, "leaseReceiptId": receipt_id,
        }
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()


def _complete_locked(
    con: sqlite3.Connection, capability: sqlite3.Row, actor: sqlite3.Row,
    dispatch: sqlite3.Row, evidence: dict[str, Any], recorded_at: str,
) -> dict[str, Any]:
    attempt = con.execute("SELECT * FROM transport_attempts WHERE transport_attempt_id=?", (dispatch["transport_attempt_id"],)).fetchone()
    message = con.execute("SELECT * FROM semantic_messages WHERE message_id=?", (attempt["message_id"],)).fetchone()
    before_detail = _message_state(con, message)
    before_root = _aggregate_root("SemanticMessage", message["message_id"], before_detail)
    version = _aggregate_position(con, aggregate_type="SemanticMessage", aggregate_id=message["message_id"], current_root=before_root)
    con.execute("UPDATE transport_dispatches SET state='RECEIPTED',observed_receipt_id=?,lease_owner_actor_id=NULL,lease_token_sha256=NULL,lease_expires_at=NULL,updated_at=? WHERE dispatch_id=?", (evidence["receiptId"], recorded_at, dispatch["dispatch_id"]))
    con.execute("UPDATE transport_attempts SET state='RECEIPTED',updated_at=? WHERE transport_attempt_id=?", (recorded_at, attempt["transport_attempt_id"]))
    con.execute("UPDATE semantic_messages SET state='DELIVERED' WHERE message_id=?", (message["message_id"],))
    updated = con.execute("SELECT * FROM semantic_messages WHERE message_id=?", (message["message_id"],)).fetchone()
    after_root = _aggregate_root("SemanticMessage", message["message_id"], _message_state(con, updated))
    con.execute("UPDATE semantic_messages SET aggregate_version=?,aggregate_root=? WHERE message_id=?", (version + 1, after_root, message["message_id"]))
    request = _runtime_request(actor=actor, capability=capability, message=message, operation="runtime.delivery.complete", aggregate_type="SemanticMessage", aggregate_id=message["message_id"], parameters=evidence)
    event = append_hub_event(con, request=request, capability_id=capability["capability_id"], aggregate_version=version + 1, before_root=before_root, after_root=after_root, event_result={"state": "DELIVERED", "dispatchId": dispatch["dispatch_id"], "observedReceiptId": evidence["receiptId"]}, occurred_at=recorded_at)
    receipt_id = _record_runtime_receipt(con, dispatch["dispatch_id"], "DELIVERED", evidence, recorded_at)
    con.execute("UPDATE attention_items SET state='RESOLVED',resolved_at=? WHERE entity_type='TransportDispatch' AND entity_id=? AND state='OPEN'", (recorded_at, dispatch["dispatch_id"]))
    return {"eventId": event["eventId"], "runtimeReceiptId": receipt_id, "messageId": message["message_id"]}


def complete_transport(
    database_path: Path, credential_path: Path, plugin_root: Path, *, dispatch_id: str,
    lease_token: str, observable_marker: str, observed_receipt_id: str,
) -> dict[str, Any]:
    con = _open_writer(database_path, plugin_root)
    try:
        capability, actor = _courier(con, credential_path)
        dispatch = con.execute("SELECT * FROM transport_dispatches WHERE dispatch_id=?", (dispatch_id,)).fetchone()
        if not dispatch or dispatch["state"] != "LEASED" or dispatch["lease_owner_actor_id"] != actor["actor_id"]:
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Transport dispatch is not leased by this courier", {})
        if not hmac.compare_digest(str(dispatch["lease_token_sha256"]), sha256_bytes(lease_token.encode("utf-8"))):
            raise PostOfficeError("PON_AUTHENTICATION_FAILED", "Transport lease token is invalid", {})
        if _parse_timestamp(dispatch["lease_expires_at"]) <= datetime.now(timezone.utc):
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Transport lease expired and requires reconciliation", {})
        if observable_marker != dispatch["observable_marker"] or not observed_receipt_id:
            raise PostOfficeError("PON_OBSERVATION_MISMATCH", "Delivery evidence does not match the dispatch marker", {})
        recorded_at = _timestamp()
        completed = _complete_locked(con, capability, actor, dispatch, {"marker": observable_marker, "receiptId": observed_receipt_id}, recorded_at)
        state_root = _operational_state(con)["operationalStateRoot"]
        con.commit()
        return {"ok": True, "dispatchId": dispatch_id, **completed, "stateRoot": state_root}
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()


def ensure_automatic_review(
    database_path: Path, credential_path: Path, plugin_root: Path, *, review_id: str,
    semantic_message_id: str, requester_task_id: str, reviewer_endpoint_id: str,
    package_sha256: str, minimum_interval_minutes: int = 30,
) -> dict[str, Any]:
    con = _open_writer(database_path, plugin_root)
    try:
        capability, actor = _courier(con, credential_path)
        retained = con.execute("SELECT * FROM automatic_reviews WHERE review_id=?", (review_id,)).fetchone()
        if retained:
            exact = (retained["semantic_message_id"] == semantic_message_id
                     and retained["requester_task_id"] == requester_task_id
                     and retained["reviewer_endpoint_id"] == reviewer_endpoint_id
                     and retained["package_sha256"] == package_sha256.removeprefix("sha256:"))
            if not exact:
                raise PostOfficeError("PON_IDEMPOTENCY_CONFLICT", "Review ID is bound to a different request", {})
            con.rollback()
            return {"ok": True, "created": False, "reviewId": review_id, "state": retained["state"]}
        message = con.execute("SELECT * FROM semantic_messages WHERE message_id=?", (semantic_message_id,)).fetchone()
        task = con.execute("SELECT * FROM tasks WHERE task_id=?", (requester_task_id,)).fetchone()
        reviewer = con.execute("SELECT * FROM endpoints WHERE endpoint_id=?", (reviewer_endpoint_id,)).fetchone()
        if not message or not task or not reviewer or reviewer["status"] != "ACTIVE":
            raise PostOfficeError("PON_INPUT_INVALID", "Review message, requester task, or reviewer endpoint is unavailable", {})
        if message["project_id"] != task["project_id"]:
            raise PostOfficeError("PON_INPUT_INVALID", "Review request is outside the requester task project", {})
        reviewer_actor = con.execute("SELECT * FROM actors WHERE actor_id=?", (reviewer["actor_id"],)).fetchone()
        if not reviewer_actor or reviewer_actor["actor_kind"] != "BROWSER":
            raise PostOfficeError("PON_INPUT_INVALID", "Automatic reviewer endpoint must be a browser", {})
        package_hash = package_sha256.removeprefix("sha256:")
        if not con.execute("SELECT 1 FROM storage_copies WHERE content_sha256=?", (package_hash,)).fetchone():
            raise PostOfficeError("PON_CUSTODY_NOT_VERIFIED", "Review package lacks verified local custody", {})
        reviewer_instance = con.execute(
            "SELECT * FROM reviewer_instances WHERE endpoint_id=? AND status='ACTIVE' ORDER BY rotation_order LIMIT 1",
            (reviewer_endpoint_id,),
        ).fetchone()
        if not reviewer_instance:
            raise PostOfficeError("PON_INPUT_INVALID", "Reviewer endpoint has no active reviewer instance", {})
        task_grant = con.execute("SELECT scope_json FROM authority_grants WHERE grant_id=?", (task["authority_grant_id"],)).fetchone()
        task_scope = json.loads(task_grant["scope_json"]) if task_grant else {}
        requester_thread_id = task_scope.get("threadId")
        last = con.execute("SELECT queued_at FROM automatic_reviews WHERE reviewer_endpoint_id=? ORDER BY queued_at DESC LIMIT 1", (reviewer_endpoint_id,)).fetchone()
        if last and datetime.now(timezone.utc) - _parse_timestamp(last["queued_at"]) < timedelta(minutes=minimum_interval_minutes):
            raise PostOfficeError(
                "PON_RATE_LIMITED", "Reviewer submission interval has not elapsed",
                {"reviewerEndpointId": reviewer_endpoint_id, "minimumIntervalMinutes": minimum_interval_minutes, "lastQueuedAt": last["queued_at"]},
            )
        now = _timestamp()
        con.execute(
            """INSERT INTO automatic_reviews(review_id,semantic_message_id,requester_task_id,
               reviewer_endpoint_id,package_sha256,state,queued_at,requester_thread_id,reviewer_thread_id)
               VALUES(?,?,?,?,?,'QUEUED',?,?,?)""",
            (review_id, semantic_message_id, requester_task_id, reviewer_endpoint_id, package_hash, now,
             requester_thread_id, reviewer_instance["reviewer_thread_id"]),
        )
        con.execute("UPDATE reviewer_instances SET last_submission_at=?,updated_at=? WHERE reviewer_thread_id=?", (now, now, reviewer_instance["reviewer_thread_id"]))
        request = _runtime_request(actor=actor, capability=capability, message=message, operation="runtime.review.ensure", aggregate_type="AutomaticReview", aggregate_id=review_id, parameters={"packageSha256": package_hash})
        before_root = _aggregate_root("AutomaticReview", review_id, None)
        inserted = con.execute("SELECT * FROM automatic_reviews WHERE review_id=?", (review_id,)).fetchone()
        after_state = _review_state(inserted)
        after_root = _aggregate_root("AutomaticReview", review_id, after_state)
        event = append_hub_event(con, request=request, capability_id=capability["capability_id"], aggregate_version=1, before_root=before_root, after_root=after_root, event_result={"state": "QUEUED"}, occurred_at=now)
        con.commit()
        return {"ok": True, "created": True, **after_state, "eventId": event["eventId"]}
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()


def claim_next_review(
    database_path: Path, credential_path: Path, plugin_root: Path, *, reviewer_endpoint_id: str | None = None
) -> dict[str, Any]:
    con = _open_writer(database_path, plugin_root)
    try:
        capability, actor = _courier(con, credential_path)
        sql = """SELECT r.* FROM automatic_reviews r
                 JOIN reviewer_instances i ON i.reviewer_thread_id=r.reviewer_thread_id
                 WHERE r.state='QUEUED' AND i.status='ACTIVE'
                 AND NOT EXISTS (SELECT 1 FROM automatic_reviews active
                                 WHERE active.reviewer_thread_id=r.reviewer_thread_id AND active.state='ACTIVE')"""
        values: tuple[Any, ...] = ()
        if reviewer_endpoint_id:
            sql += " AND r.reviewer_endpoint_id=?"; values = (reviewer_endpoint_id,)
        review = con.execute(sql + " ORDER BY r.queued_at,r.review_id LIMIT 1", values).fetchone()
        if not review:
            con.rollback()
            return {"ok": True, "available": False}
        message = con.execute("SELECT * FROM semantic_messages WHERE message_id=?", (review["semantic_message_id"],)).fetchone()
        before = _review_state(review)
        now = _timestamp()
        con.execute("UPDATE automatic_reviews SET state='ACTIVE',activated_at=? WHERE review_id=? AND state='QUEUED'", (now, review["review_id"]))
        updated_review = con.execute("SELECT * FROM automatic_reviews WHERE review_id=?", (review["review_id"],)).fetchone()
        after = _review_state(updated_review)
        request = _runtime_request(actor=actor, capability=capability, message=message, operation="runtime.review.claim", aggregate_type="AutomaticReview", aggregate_id=review["review_id"], parameters={})
        event = append_hub_event(con, request=request, capability_id=capability["capability_id"], aggregate_version=2, before_root=_aggregate_root("AutomaticReview", review["review_id"], before), after_root=_aggregate_root("AutomaticReview", review["review_id"], after), event_result={"state": "ACTIVE"}, occurred_at=now)
        bundle = con.execute("SELECT * FROM message_bundles WHERE message_id=?", (review["semantic_message_id"],)).fetchone()
        payloads = [dict(row) for row in con.execute("SELECT * FROM bundle_payloads WHERE bundle_id=? ORDER BY ordinal", (bundle["bundle_id"],))] if bundle else []
        con.commit()
        return {"ok": True, "available": True, "reviewId": review["review_id"], "semanticMessageId": review["semantic_message_id"],
                "reviewerEndpointId": review["reviewer_endpoint_id"], "reviewerThreadId": review["reviewer_thread_id"],
                "requesterThreadId": review["requester_thread_id"], "packageName": review["package_name"] or (bundle["canonical_filename"] if bundle else None),
                "packageSizeBytes": review["package_size_bytes"] or (bundle["size_bytes"] if bundle else None),
                "packageSha256": review["package_sha256"], "payloads": payloads, "eventId": event["eventId"]}
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()


def return_automatic_review(
    database_path: Path, credential_path: Path, plugin_root: Path, *, review_id: str,
    result_message_id: str,
) -> dict[str, Any]:
    con = _open_writer(database_path, plugin_root)
    try:
        capability, actor = _courier(con, credential_path)
        review = con.execute("SELECT * FROM automatic_reviews WHERE review_id=?", (review_id,)).fetchone()
        result_message = con.execute("SELECT * FROM semantic_messages WHERE message_id=?", (result_message_id,)).fetchone()
        if not review or review["state"] != "ACTIVE" or not result_message:
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Active review or result message is unavailable", {})
        requester = con.execute("SELECT * FROM tasks WHERE task_id=?", (review["requester_task_id"],)).fetchone()
        mailbox = con.execute("SELECT * FROM mailboxes WHERE mailbox_id=? AND generation=?", (result_message["recipient_mailbox_id"], result_message["recipient_generation"])).fetchone()
        endpoint = con.execute("SELECT * FROM endpoints WHERE endpoint_id=?", (mailbox["endpoint_id"],)).fetchone() if mailbox else None
        if not endpoint or endpoint["task_id"] != requester["task_id"]:
            raise PostOfficeError("PON_INPUT_INVALID", "Review result is not addressed to the retained requester task", {})
        attempt = con.execute("SELECT * FROM transport_attempts WHERE message_id=? ORDER BY attempt_number DESC LIMIT 1", (result_message_id,)).fetchone()
        dispatch = con.execute("SELECT * FROM transport_dispatches WHERE transport_attempt_id=?", (attempt["transport_attempt_id"],)).fetchone() if attempt else None
        if not dispatch:
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Review return has no materialized wake dispatch", {})
        now = _timestamp()
        before = _review_state(review)
        con.execute("UPDATE automatic_reviews SET state='RETURNED',returned_at=?,result_message_id=?,wake_dispatch_id=? WHERE review_id=?", (now, result_message_id, dispatch["dispatch_id"], review_id))
        updated_review = con.execute("SELECT * FROM automatic_reviews WHERE review_id=?", (review_id,)).fetchone()
        after = _review_state(updated_review)
        request_message = con.execute("SELECT * FROM semantic_messages WHERE message_id=?", (review["semantic_message_id"],)).fetchone()
        request = _runtime_request(actor=actor, capability=capability, message=request_message, operation="runtime.review.return", aggregate_type="AutomaticReview", aggregate_id=review_id, parameters={"resultMessageId": result_message_id, "wakeDispatchId": dispatch["dispatch_id"]})
        event = append_hub_event(con, request=request, capability_id=capability["capability_id"], aggregate_version=3, before_root=_aggregate_root("AutomaticReview", review_id, before), after_root=_aggregate_root("AutomaticReview", review_id, after), event_result={"state": "RETURNED", "wakeDispatchId": dispatch["dispatch_id"]}, occurred_at=now)
        con.commit()
        return {"ok": True, "reviewId": review_id, "state": "RETURNED", "resultMessageId": result_message_id, "wakeDispatchId": dispatch["dispatch_id"], "eventId": event["eventId"], "heartbeatRequired": False}
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()


def withdraw_automatic_review(
    database_path: Path, credential_path: Path, plugin_root: Path, *, review_id: str, reason: str
) -> dict[str, Any]:
    con = _open_writer(database_path, plugin_root)
    try:
        capability, actor = _courier(con, credential_path)
        review = con.execute("SELECT * FROM automatic_reviews WHERE review_id=?", (review_id,)).fetchone()
        if not review or review["state"] not in {"QUEUED", "ACTIVE"}:
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Only a queued or active review may be withdrawn", {})
        message = con.execute("SELECT * FROM semantic_messages WHERE message_id=?", (review["semantic_message_id"],)).fetchone()
        now = _timestamp()
        before = _review_state(review)
        con.execute("UPDATE automatic_reviews SET state='WITHDRAWN' WHERE review_id=?", (review_id,))
        updated_review = con.execute("SELECT * FROM automatic_reviews WHERE review_id=?", (review_id,)).fetchone()
        after = _review_state(updated_review)
        request = _runtime_request(actor=actor, capability=capability, message=message, operation="runtime.review.withdraw", aggregate_type="AutomaticReview", aggregate_id=review_id, parameters={"reason": reason})
        event = append_hub_event(con, request=request, capability_id=capability["capability_id"], aggregate_version=2 if review["state"] == "QUEUED" else 3, before_root=_aggregate_root("AutomaticReview", review_id, before), after_root=_aggregate_root("AutomaticReview", review_id, after), event_result={"state": "WITHDRAWN", "reason": reason}, occurred_at=now)
        con.commit()
        return {"ok": True, "reviewId": review_id, "state": "WITHDRAWN", "eventId": event["eventId"]}
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()


def status_automatic_review(
    database_path: Path, credential_path: Path, plugin_root: Path, *, review_id: str,
) -> dict[str, Any]:
    con = _open_writer(database_path, plugin_root)
    try:
        _courier(con, credential_path)
        row = con.execute("SELECT * FROM automatic_reviews WHERE review_id=?", (review_id,)).fetchone()
        if not row:
            raise PostOfficeError("PON_NOT_FOUND", "Automatic review is not retained", {"reviewId": review_id})
        con.rollback()
        return {"ok": True, **_review_state(row), "heartbeatRequired": False}
    finally:
        con.close()


def complete_automatic_review(
    database_path: Path, credential_path: Path, plugin_root: Path, *, review_id: str, summary: str,
) -> dict[str, Any]:
    con = _open_writer(database_path, plugin_root)
    try:
        capability, actor = _courier(con, credential_path)
        review = con.execute("SELECT * FROM automatic_reviews WHERE review_id=?", (review_id,)).fetchone()
        if not review or review["state"] not in {"RETURNED", "EVALUATED"}:
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Only a returned review may be evaluated", {})
        if review["state"] == "EVALUATED":
            con.rollback()
            return {"ok": True, **_review_state(review), "completed": False, "summary": summary}
        message = con.execute("SELECT * FROM semantic_messages WHERE message_id=?", (review["semantic_message_id"],)).fetchone()
        before = _review_state(review)
        now = _timestamp()
        con.execute("UPDATE automatic_reviews SET state='EVALUATED',evaluated_at=? WHERE review_id=? AND state='RETURNED'", (now, review_id))
        if review["result_message_id"]:
            con.execute("UPDATE semantic_messages SET state='ACKNOWLEDGED' WHERE message_id=? AND state='DELIVERED'", (review["result_message_id"],))
        updated = con.execute("SELECT * FROM automatic_reviews WHERE review_id=?", (review_id,)).fetchone()
        after = _review_state(updated)
        request = _runtime_request(actor=actor, capability=capability, message=message, operation="runtime.review.complete", aggregate_type="AutomaticReview", aggregate_id=review_id, parameters={"summary": summary})
        position = _aggregate_position(con, aggregate_type="AutomaticReview", aggregate_id=review_id, current_root=_aggregate_root("AutomaticReview", review_id, before))
        event = append_hub_event(con, request=request, capability_id=capability["capability_id"], aggregate_version=position + 1, before_root=_aggregate_root("AutomaticReview", review_id, before), after_root=_aggregate_root("AutomaticReview", review_id, after), event_result={"state": "EVALUATED", "summary": summary}, occurred_at=now)
        con.commit()
        return {"ok": True, **after, "completed": True, "summary": summary, "eventId": event["eventId"]}
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()


def _continuation_receipt(
    con: sqlite3.Connection, continuation_id: str, kind: str, evidence: dict[str, Any], recorded_at: str,
) -> str:
    receipt_id = "PON-CONTINUATION-RECEIPT-" + uuid.uuid4().hex
    con.execute(
        "INSERT INTO continuation_receipts VALUES(?,?,?,?,?,?)",
        (receipt_id, continuation_id, kind, canonical_json_bytes(evidence).decode("utf-8"),
         sha256_json(evidence), recorded_at),
    )
    return receipt_id


def reconcile_continuations(
    database_path: Path, credential_path: Path, plugin_root: Path,
) -> dict[str, Any]:
    con = _open_writer(database_path, plugin_root)
    try:
        _courier(con, credential_path)
        now = _timestamp()
        expired = list(con.execute(
            "SELECT * FROM continuation_items WHERE state='LEASED' AND lease_expires_at<=? ORDER BY lease_expires_at,continuation_id",
            (now,),
        ))
        recovered: list[str] = []
        for item in expired:
            con.execute(
                """UPDATE continuation_items SET state='READY',lease_owner_actor_id=NULL,
                   lease_token_sha256=NULL,lease_expires_at=NULL,last_error_code=NULL,updated_at=?
                   WHERE continuation_id=?""",
                (now, item["continuation_id"]),
            )
            _continuation_receipt(con, item["continuation_id"], "RECOVERED", {"outcome": "LEASE_EXPIRED_SAFE_REQUEUE"}, now)
            recovered.append(str(item["continuation_id"]))
        con.commit()
        return {"ok": True, "recoveredContinuationIds": recovered,
                "readyCount": int(con.execute("SELECT COUNT(*) FROM continuation_items WHERE state='READY'").fetchone()[0])}
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()


def claim_next_continuation(
    database_path: Path, credential_path: Path, plugin_root: Path, *,
    continuation_kind: str | None = None, lease_seconds: int = 600,
) -> dict[str, Any]:
    if lease_seconds < 30 or lease_seconds > 1800:
        raise PostOfficeError("PON_INPUT_INVALID", "Continuation lease must be between 30 and 1800 seconds", {})
    con = _open_writer(database_path, plugin_root)
    try:
        _, actor = _courier(con, credential_path)
        sql = "SELECT * FROM continuation_items WHERE state='READY'"
        values: tuple[Any, ...] = ()
        if continuation_kind:
            sql += " AND continuation_kind=?"
            values = (continuation_kind,)
        item = con.execute(sql + " ORDER BY created_at,continuation_id LIMIT 1", values).fetchone()
        if not item:
            con.rollback()
            return {"ok": True, "available": False}
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat().replace("+00:00", "Z")
        expires = (now_dt + timedelta(seconds=lease_seconds)).isoformat().replace("+00:00", "Z")
        token = secrets.token_urlsafe(32)
        con.execute(
            """UPDATE continuation_items SET state='LEASED',lease_owner_actor_id=?,
               lease_token_sha256=?,lease_expires_at=?,attempt_count=attempt_count+1,updated_at=?
               WHERE continuation_id=? AND state='READY'""",
            (actor["actor_id"], sha256_bytes(token.encode("utf-8")), expires, now, item["continuation_id"]),
        )
        receipt_id = _continuation_receipt(con, item["continuation_id"], "LEASED", {"leaseExpiresAt": expires}, now)
        con.commit()
        payload = json.loads(item["payload_json"])
        destination = _continuation_destination(con, payload)
        return {"ok": True, "available": True, "continuationId": item["continuation_id"],
                "continuationKind": item["continuation_kind"], "sourceTable": item["source_table"],
                "sourceId": item["source_id"], "payload": payload, **destination,
                "evidenceRoot": item["evidence_root"], "leaseToken": token,
                "leaseExpiresAt": expires, "receiptId": receipt_id}
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()


def complete_continuation(
    database_path: Path, credential_path: Path, plugin_root: Path, *,
    continuation_id: str, lease_token: str, outcome: dict[str, Any],
) -> dict[str, Any]:
    con = _open_writer(database_path, plugin_root)
    try:
        _, actor = _courier(con, credential_path)
        item = con.execute("SELECT * FROM continuation_items WHERE continuation_id=?", (continuation_id,)).fetchone()
        if not item or item["state"] != "LEASED" or item["lease_owner_actor_id"] != actor["actor_id"]:
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Continuation is not leased by this courier", {})
        if not hmac.compare_digest(str(item["lease_token_sha256"]), sha256_bytes(lease_token.encode("utf-8"))):
            raise PostOfficeError("PON_AUTHENTICATION_FAILED", "Continuation lease token is invalid", {})
        if _parse_timestamp(item["lease_expires_at"]) <= datetime.now(timezone.utc):
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Continuation lease expired and requires reconciliation", {})
        if not isinstance(outcome, dict) or not outcome.get("receiptId"):
            raise PostOfficeError("PON_OBSERVATION_MISMATCH", "Continuation completion requires an exact external receipt", {})
        now = _timestamp()
        con.execute(
            """UPDATE continuation_items SET state='COMPLETED',lease_owner_actor_id=NULL,
               lease_token_sha256=NULL,lease_expires_at=NULL,updated_at=?,completed_at=?
               WHERE continuation_id=?""",
            (now, now, continuation_id),
        )
        receipt_id = _continuation_receipt(con, continuation_id, "COMPLETED", outcome, now)
        con.commit()
        return {"ok": True, "continuationId": continuation_id, "state": "COMPLETED",
                "receiptId": receipt_id, "outcomeRoot": sha256_json(outcome)}
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()


def retire_continuation(
    database_path: Path, credential_path: Path, plugin_root: Path, *,
    continuation_id: str, lease_token: str, outcome: dict[str, Any],
) -> dict[str, Any]:
    """Retire proven-obsolete migrated work without claiming its original action completed."""
    con = _open_writer(database_path, plugin_root)
    try:
        _, actor = _courier(con, credential_path)
        item = con.execute(
            "SELECT * FROM continuation_items WHERE continuation_id=?", (continuation_id,)
        ).fetchone()
        if not item or item["state"] != "LEASED" or item["lease_owner_actor_id"] != actor["actor_id"]:
            raise PostOfficeError(
                "PON_CONCURRENCY_CONFLICT", "Continuation is not leased by this courier", {}
            )
        if not hmac.compare_digest(
            str(item["lease_token_sha256"]), sha256_bytes(lease_token.encode("utf-8"))
        ):
            raise PostOfficeError(
                "PON_AUTHENTICATION_FAILED", "Continuation lease token is invalid", {}
            )
        if _parse_timestamp(item["lease_expires_at"]) <= datetime.now(timezone.utc):
            raise PostOfficeError(
                "PON_CONCURRENCY_CONFLICT",
                "Continuation lease expired and requires reconciliation",
                {},
            )
        if (
            not isinstance(outcome, dict)
            or not outcome.get("receiptId")
            or not outcome.get("disposition")
        ):
            raise PostOfficeError(
                "PON_OBSERVATION_MISMATCH",
                "Continuation retirement requires an exact receipt and disposition",
                {},
            )
        now = _timestamp()
        con.execute(
            """UPDATE continuation_items SET state='RETIRED',lease_owner_actor_id=NULL,
               lease_token_sha256=NULL,lease_expires_at=NULL,updated_at=?,completed_at=?
               WHERE continuation_id=?""",
            (now, now, continuation_id),
        )
        receipt_id = _continuation_receipt(
            con, continuation_id, "COMPLETED", {**outcome, "terminalState": "RETIRED"}, now
        )
        con.commit()
        return {
            "ok": True,
            "continuationId": continuation_id,
            "state": "RETIRED",
            "receiptId": receipt_id,
            "outcomeRoot": sha256_json(outcome),
        }
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()
