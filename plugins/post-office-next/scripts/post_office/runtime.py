# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import hmac
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import uuid
import zipfile
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


def _destination_is_active(
    con: sqlite3.Connection,
    *,
    endpoint_id: str | None,
    mailbox_id: str | None,
    generation: int | None,
) -> bool:
    """Require the exact retained destination identity to remain active.

    A revoked endpoint, mailbox generation, or endpoint actor is held for explicit
    reconciliation.  The courier must never silently retarget durable mail.
    """
    if not endpoint_id or not mailbox_id or generation is None:
        return False
    row = con.execute(
        """SELECT e.status AS endpoint_status,m.status AS mailbox_status,
                  a.status AS actor_status
           FROM endpoints e
           JOIN mailboxes m ON m.endpoint_id=e.endpoint_id
           LEFT JOIN actors a ON a.actor_id=e.actor_id
           WHERE e.endpoint_id=? AND m.mailbox_id=? AND m.generation=?""",
        (endpoint_id, mailbox_id, generation),
    ).fetchone()
    return bool(
        row
        and row["endpoint_status"] == "ACTIVE"
        and row["mailbox_status"] == "ACTIVE"
        and row["actor_status"] == "ACTIVE"
    )


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


def _resolve_superseded_transport_attention(
    con: sqlite3.Connection,
    attempt: sqlite3.Row,
    recorded_at: str,
) -> None:
    """Resolve stale dispatch attention once a retained retry descendant is receipted."""
    previous_id = attempt["canonical_attempt_id"]
    seen: set[str] = set()
    while previous_id and previous_id not in seen and len(seen) < 32:
        seen.add(str(previous_id))
        previous = con.execute(
            "SELECT * FROM transport_attempts WHERE transport_attempt_id=?",
            (previous_id,),
        ).fetchone()
        if not previous:
            break
        previous_dispatch = con.execute(
            "SELECT dispatch_id FROM transport_dispatches WHERE transport_attempt_id=?",
            (previous_id,),
        ).fetchone()
        if previous_dispatch:
            con.execute(
                """UPDATE attention_items SET state='RESOLVED',resolved_at=?
                   WHERE entity_type='TransportDispatch' AND entity_id=? AND state='OPEN'""",
                (recorded_at, previous_dispatch["dispatch_id"]),
            )
        previous_id = previous["canonical_attempt_id"]


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
        for attempt in con.execute(
            """SELECT t.* FROM transport_attempts t
               WHERE t.state='PENDING'
                 AND NOT EXISTS (
                     SELECT 1 FROM automatic_reviews r
                     WHERE r.semantic_message_id=t.message_id
                 )
               ORDER BY t.created_at,t.transport_attempt_id"""
        ):
            if con.execute("SELECT 1 FROM transport_dispatches WHERE transport_attempt_id=?", (attempt["transport_attempt_id"],)).fetchone():
                continue
            message = con.execute("SELECT * FROM semantic_messages WHERE message_id=?", (attempt["message_id"],)).fetchone()
            mailbox = con.execute("SELECT * FROM mailboxes WHERE mailbox_id=? AND generation=?", (message["recipient_mailbox_id"], message["recipient_generation"])).fetchone()
            endpoint = con.execute("SELECT * FROM endpoints WHERE endpoint_id=?", (mailbox["endpoint_id"],)).fetchone() if mailbox else None
            if not mailbox or not endpoint or not _destination_is_active(
                con,
                endpoint_id=endpoint["endpoint_id"] if endpoint else None,
                mailbox_id=message["recipient_mailbox_id"],
                generation=int(message["recipient_generation"]),
            ):
                attention_ids.append(_attention(
                    con,
                    entity_type="TransportAttempt",
                    entity_id=attempt["transport_attempt_id"],
                    severity="ERROR",
                    reason_code="PON_DESTINATION_INACTIVE",
                    details={
                        "endpointId": endpoint["endpoint_id"] if endpoint else None,
                        "mailboxId": message["recipient_mailbox_id"],
                        "generation": int(message["recipient_generation"]),
                    },
                    recorded_at=now,
                ))
                continue
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
            marker = (
                f"POST-OFFICE-PLAYWRIGHT-DISPATCH {dispatch_id}"
                if channel == "PLAYWRIGHT_BROWSER"
                else f"POST OFFICE DELIVERY {dispatch_id} {message['message_id']}"
            )
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
        for receipted_attempt in con.execute(
            "SELECT * FROM transport_attempts WHERE state='RECEIPTED' AND canonical_attempt_id IS NOT NULL"
        ):
            _resolve_superseded_transport_attention(con, receipted_attempt, now)
        con.commit()
        return {"ok": True, "createdDispatchIds": created, "recoveredDispatchIds": recovered,
                "attentionIds": attention_ids, "stateRoot": _operational_state(con)["operationalStateRoot"]}
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()


def retire_review_owned_transport_dispatches(
    database_path: Path,
    credential_path: Path,
    plugin_root: Path,
) -> dict[str, Any]:
    """Retire ordinary dispatches accidentally materialized for review activation.

    Automatic-review request packages remain in Post Office custody, but their browser delivery is
    owned by the review activation path. This repair is deliberately limited to unleased READY
    dispatches without observed receipts; ambiguous or externally visible work is never rewritten.
    """
    con = _open_writer(database_path, plugin_root)
    try:
        capability, actor = _courier(con, credential_path)
        now = _timestamp()
        candidates = list(con.execute(
            """SELECT d.dispatch_id,d.transport_attempt_id,t.message_id
               FROM transport_dispatches d
               JOIN transport_attempts t ON t.transport_attempt_id=d.transport_attempt_id
               JOIN automatic_reviews r ON r.semantic_message_id=t.message_id
               WHERE d.state='READY'
                 AND t.source='AUTOMATIC_REVIEW_COMPANION'
                 AND d.lease_owner_actor_id IS NULL
                 AND d.lease_token_sha256 IS NULL
                 AND d.lease_expires_at IS NULL
                 AND d.observed_receipt_id IS NULL
               ORDER BY d.created_at,d.dispatch_id"""
        ))
        retired: list[str] = []
        event_ids: list[str] = []
        receipt_ids: list[str] = []
        for candidate in candidates:
            attempt = con.execute(
                "SELECT * FROM transport_attempts WHERE transport_attempt_id=?",
                (candidate["transport_attempt_id"],),
            ).fetchone()
            dispatch = con.execute(
                "SELECT * FROM transport_dispatches WHERE dispatch_id=?",
                (candidate["dispatch_id"],),
            ).fetchone()
            message = con.execute(
                "SELECT * FROM semantic_messages WHERE message_id=?",
                (candidate["message_id"],),
            ).fetchone()
            before_root = _aggregate_root(
                "TransportAttempt",
                attempt["transport_attempt_id"],
                _transport_state(attempt, dispatch),
            )
            version = _aggregate_position(
                con,
                aggregate_type="TransportAttempt",
                aggregate_id=attempt["transport_attempt_id"],
                current_root=before_root,
            )
            con.execute(
                """UPDATE transport_dispatches
                   SET state='CANCELLED',last_error_code='PON_REVIEW_ACTIVATION_OWNS_TRANSPORT',
                       updated_at=?
                   WHERE dispatch_id=? AND state='READY'""",
                (now, dispatch["dispatch_id"]),
            )
            con.execute(
                """UPDATE transport_attempts SET state='STORED',updated_at=?
                   WHERE transport_attempt_id=? AND state='PENDING'""",
                (now, attempt["transport_attempt_id"]),
            )
            updated_attempt = con.execute(
                "SELECT * FROM transport_attempts WHERE transport_attempt_id=?",
                (attempt["transport_attempt_id"],),
            ).fetchone()
            updated_dispatch = con.execute(
                "SELECT * FROM transport_dispatches WHERE dispatch_id=?",
                (dispatch["dispatch_id"],),
            ).fetchone()
            after_root = _aggregate_root(
                "TransportAttempt",
                attempt["transport_attempt_id"],
                _transport_state(updated_attempt, updated_dispatch),
            )
            evidence = {
                "outcome": "RETIRED_TO_REVIEW_ACTIVATION",
                "reasonCode": "PON_REVIEW_ACTIVATION_OWNS_TRANSPORT",
                "semanticMessageId": message["message_id"],
            }
            request = _runtime_request(
                actor=actor,
                capability=capability,
                message=message,
                operation="runtime.reviewTransport.retire",
                aggregate_type="TransportAttempt",
                aggregate_id=attempt["transport_attempt_id"],
                parameters={"dispatchId": dispatch["dispatch_id"], **evidence},
            )
            event = append_hub_event(
                con,
                request=request,
                capability_id=capability["capability_id"],
                aggregate_version=version + 1,
                before_root=before_root,
                after_root=after_root,
                event_result={"state": "STORED", "dispatchState": "CANCELLED"},
                occurred_at=now,
            )
            receipt_id = _record_runtime_receipt(
                con, dispatch["dispatch_id"], "CANCELLED", evidence, now
            )
            retired.append(str(dispatch["dispatch_id"]))
            event_ids.append(str(event["eventId"]))
            receipt_ids.append(receipt_id)
        con.commit()
        return {
            "ok": True,
            "retiredDispatchIds": retired,
            "eventIds": event_ids,
            "runtimeReceiptIds": receipt_ids,
            "stateRoot": _operational_state(con)["operationalStateRoot"],
        }
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()


def claim_next_transport(
    database_path: Path, credential_path: Path, plugin_root: Path, *, lease_seconds: int = 600,
    dispatch_id: str | None = None,
) -> dict[str, Any]:
    if lease_seconds < 30 or lease_seconds > 1800:
        raise PostOfficeError("PON_INPUT_INVALID", "Transport lease must be between 30 and 1800 seconds", {})
    con = _open_writer(database_path, plugin_root)
    try:
        capability, actor = _courier(con, credential_path)
        now = _timestamp()
        dispatch = None
        held: list[str] = []
        query = "SELECT * FROM transport_dispatches WHERE state='READY' AND next_attempt_at<=?"
        values: tuple[Any, ...] = (now,)
        if dispatch_id:
            query += " AND dispatch_id=?"
            values = (now, dispatch_id)
        query += " ORDER BY created_at,dispatch_id"
        for candidate in con.execute(query, values):
            if _destination_is_active(
                con,
                endpoint_id=candidate["destination_endpoint_id"],
                mailbox_id=candidate["destination_mailbox_id"],
                generation=int(candidate["destination_generation"]),
            ):
                dispatch = candidate
                break
            con.execute(
                """UPDATE transport_dispatches
                   SET state='RECONCILIATION_REQUIRED',last_error_code='PON_DESTINATION_INACTIVE',updated_at=?
                   WHERE dispatch_id=? AND state='READY'""",
                (now, candidate["dispatch_id"]),
            )
            _attention(
                con,
                entity_type="TransportDispatch",
                entity_id=candidate["dispatch_id"],
                severity="ERROR",
                reason_code="PON_DESTINATION_INACTIVE",
                details={
                    "endpointId": candidate["destination_endpoint_id"],
                    "mailboxId": candidate["destination_mailbox_id"],
                    "generation": int(candidate["destination_generation"]),
                },
                recorded_at=now,
            )
            held.append(str(candidate["dispatch_id"]))
        if not dispatch:
            if held:
                con.commit()
            else:
                con.rollback()
            return {"ok": True, "available": False, "heldDispatchIds": held}
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
    _resolve_superseded_transport_attention(con, attempt, recorded_at)
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


def record_recovered_transport_receipt(
    database_path: Path,
    credential_path: Path,
    plugin_root: Path,
    *,
    message_id: str,
    bundle_id: str,
    channel: str,
    observable_marker: str,
    observed_receipt_id: str,
) -> dict[str, Any]:
    """Record a verified supplemental delivery for an already-delivered migrated message."""
    if channel not in {"NATIVE_TASK", "PLAYWRIGHT_BROWSER"}:
        raise PostOfficeError("PON_INPUT_INVALID", "Recovered delivery channel is invalid", {"channel": channel})
    if not all(isinstance(value, str) and value.strip() for value in (
        message_id, bundle_id, observable_marker, observed_receipt_id
    )):
        raise PostOfficeError("PON_INPUT_INVALID", "Recovered delivery evidence is incomplete", {})
    con = _open_writer(database_path, plugin_root)
    try:
        capability, actor = _courier(con, credential_path)
        replay = con.execute(
            """SELECT d.dispatch_id,d.transport_attempt_id,r.receipt_id
               FROM transport_dispatches d
               JOIN transport_attempts t ON t.transport_attempt_id=d.transport_attempt_id
               JOIN runtime_receipts r ON r.dispatch_id=d.dispatch_id AND r.receipt_kind='RECOVERED'
               WHERE t.message_id=? AND d.observed_receipt_id=?""",
            (message_id, observed_receipt_id),
        ).fetchone()
        if replay:
            con.rollback()
            return {
                "ok": True,
                "replayed": True,
                "messageId": message_id,
                "dispatchId": replay["dispatch_id"],
                "transportAttemptId": replay["transport_attempt_id"],
                "runtimeReceiptId": replay["receipt_id"],
            }
        message = con.execute("SELECT * FROM semantic_messages WHERE message_id=?", (message_id,)).fetchone()
        if not message:
            raise PostOfficeError("PON_INPUT_INVALID", "Recovered delivery message does not exist", {"messageId": message_id})
        if message["state"] not in {"DELIVERED", "ACKNOWLEDGED", "RESPONSE_RETURNED", "REVIEWED", "CLOSED"}:
            raise PostOfficeError(
                "PON_CONCURRENCY_CONFLICT",
                "Supplemental recovery requires an already-delivered semantic message",
                {"messageId": message_id, "state": message["state"]},
            )
        bundle = con.execute(
            "SELECT * FROM message_bundles WHERE message_id=? AND bundle_id=?",
            (message_id, bundle_id),
        ).fetchone()
        if not bundle:
            raise PostOfficeError(
                "PON_CUSTODY_NOT_VERIFIED",
                "Recovered delivery does not match the retained message bundle",
                {"messageId": message_id, "bundleId": bundle_id},
            )
        bundle_copy = con.execute(
            "SELECT 1 FROM storage_copies WHERE content_sha256=? AND location_kind='LOCAL_CAS' LIMIT 1",
            (bundle["sha256"],),
        ).fetchone()
        payload_count = int(con.execute("SELECT COUNT(*) FROM bundle_payloads WHERE bundle_id=?", (bundle["bundle_id"],)).fetchone()[0])
        retained_payload_count = int(con.execute(
            """SELECT COUNT(*) FROM bundle_payloads p WHERE p.bundle_id=? AND EXISTS(
                   SELECT 1 FROM storage_copies s
                   WHERE s.content_sha256=p.sha256 AND s.location_kind='LOCAL_CAS')""",
            (bundle["bundle_id"],),
        ).fetchone()[0])
        if not bundle_copy and retained_payload_count != payload_count:
            raise PostOfficeError(
                "PON_CUSTODY_NOT_VERIFIED",
                "Recovered delivery bundle lacks complete local custody",
                {"bundleId": bundle["bundle_id"]},
            )
        mailbox = con.execute(
            "SELECT * FROM mailboxes WHERE mailbox_id=? AND generation=?",
            (message["recipient_mailbox_id"], message["recipient_generation"]),
        ).fetchone()
        if not mailbox:
            raise PostOfficeError("PON_INPUT_INVALID", "Recovered delivery mailbox generation does not exist", {})
        recorded_at = _timestamp()
        attempt_id = "PON-TRANSPORT-RECOVERED-" + uuid.uuid4().hex
        dispatch_id = "PON-DISPATCH-RECOVERED-" + uuid.uuid4().hex
        attempt_number = int(con.execute(
            "SELECT COALESCE(MAX(attempt_number),0)+1 FROM transport_attempts WHERE message_id=? AND bundle_id=?",
            (message_id, bundle["bundle_id"]),
        ).fetchone()[0])
        destination = f"{message['recipient_mailbox_id']}:{int(message['recipient_generation'])}"
        con.execute(
            "INSERT INTO transport_attempts VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                attempt_id, message_id, bundle["bundle_id"], message["sender_endpoint_id"], destination,
                "RECEIPTED", attempt_number, None, recorded_at, recorded_at,
            ),
        )
        con.execute(
            "INSERT INTO transport_dispatches VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                dispatch_id, attempt_id, channel, mailbox["endpoint_id"], mailbox["mailbox_id"],
                int(mailbox["generation"]), "RECEIPTED", None, None, None, observable_marker,
                observed_receipt_id, 1, recorded_at, None, recorded_at, recorded_at,
            ),
        )
        evidence = {
            "marker": observable_marker,
            "receiptId": observed_receipt_id,
            "bundleSha256": bundle["sha256"],
            "recovery": "SUPPLEMENTAL_POST_MIGRATION_DELIVERY",
        }
        before_root = _aggregate_root("TransportAttempt", attempt_id, None)
        attempt = con.execute("SELECT * FROM transport_attempts WHERE transport_attempt_id=?", (attempt_id,)).fetchone()
        dispatch = con.execute("SELECT * FROM transport_dispatches WHERE dispatch_id=?", (dispatch_id,)).fetchone()
        after_root = _aggregate_root("TransportAttempt", attempt_id, _transport_state(attempt, dispatch))
        request = _runtime_request(
            actor=actor,
            capability=capability,
            message=message,
            operation="runtime.delivery.recovered",
            aggregate_type="TransportAttempt",
            aggregate_id=attempt_id,
            parameters=evidence,
        )
        event = append_hub_event(
            con,
            request=request,
            capability_id=capability["capability_id"],
            aggregate_version=1,
            before_root=before_root,
            after_root=after_root,
            event_result={"state": "RECOVERED", "dispatchId": dispatch_id, "observedReceiptId": observed_receipt_id},
            occurred_at=recorded_at,
        )
        runtime_receipt_id = _record_runtime_receipt(con, dispatch_id, "RECOVERED", evidence, recorded_at)
        state_root = _operational_state(con)["operationalStateRoot"]
        con.commit()
        return {
            "ok": True,
            "replayed": False,
            "messageId": message_id,
            "transportAttemptId": attempt_id,
            "dispatchId": dispatch_id,
            "eventId": event["eventId"],
            "runtimeReceiptId": runtime_receipt_id,
            "stateRoot": state_root,
        }
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()


def _validate_manifest_backed_return(
    data: bytes,
    source_message_id: str,
    *,
    correlation_message_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Validate one browser-produced ZIP without extracting it.

    The current browser mailbox contract uses a single Markdown package-member manifest.  The
    manifest deliberately omits itself, so the outer archive digest protects the manifest while
    its table protects every other member.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (OSError, zipfile.BadZipFile) as exc:
        raise PostOfficeError("PON_CUSTODY_NOT_VERIFIED", "Browser return is not a valid ZIP", {}) from exc
    with archive:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        if len(names) != len(set(names)):
            raise PostOfficeError("PON_CUSTODY_NOT_VERIFIED", "Browser return contains duplicate member names", {})
        if any(
            not name or name.startswith(("/", "\\")) or ".." in Path(name.replace("\\", "/")).parts
            for name in names
        ):
            raise PostOfficeError("PON_CUSTODY_NOT_VERIFIED", "Browser return contains an unsafe member path", {})
        if sum(item.file_size for item in infos) > 512 * 1024 * 1024:
            raise PostOfficeError("PON_CUSTODY_NOT_VERIFIED", "Browser return expands beyond the bounded limit", {})
        bad_member = archive.testzip()
        if bad_member:
            raise PostOfficeError(
                "PON_CUSTODY_NOT_VERIFIED", "Browser return ZIP integrity failed", {"member": bad_member}
            )
        markdown_manifest_names = [
            name for name in names
            if name.lower().endswith(".md")
            and (
                "package-member-manifest" in name.lower()
                or "package-manifest" in name.lower()
                or "package_manifest" in name.lower()
            )
        ]
        json_manifest_names = [
            name for name in names
            if name.lower().endswith(".json")
            and ("package-manifest" in name.lower() or "package_manifest" in name.lower())
        ]
        root_manifest_names = [name for name in names if name.lower() == "manifest.json"]
        manifest_names = markdown_manifest_names + json_manifest_names + root_manifest_names
        if len(manifest_names) != 1:
            raise PostOfficeError(
                "PON_CUSTODY_NOT_VERIFIED",
                "Browser return must contain one supported package manifest",
                {"manifestCount": len(manifest_names)},
            )
        manifest_name = manifest_names[0]
        try:
            manifest_bytes = archive.read(manifest_name)
            manifest_text = manifest_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise PostOfficeError("PON_CUSTODY_NOT_VERIFIED", "Browser return manifest is not UTF-8", {}) from exc
        if manifest_name in markdown_manifest_names:
            row_pattern = re.compile(
                r"^\|\s*`([^`]+)`\s*\|\s*([0-9][0-9,]*)\s*\|\s*`([0-9a-fA-F]{64})`\s*\|\s*$",
                re.MULTILINE,
            )
            listed = [
                (path, int(size.replace(",", "")), digest.lower())
                for path, size, digest in row_pattern.findall(manifest_text)
            ]
        else:
            try:
                manifest_json = json.loads(manifest_text)
            except json.JSONDecodeError as exc:
                raise PostOfficeError("PON_CUSTODY_NOT_VERIFIED", "Browser return JSON manifest is invalid", {}) from exc
            members = manifest_json.get("members") if isinstance(manifest_json, dict) else None
            if manifest_name in root_manifest_names:
                members = manifest_json.get("files") if isinstance(manifest_json, dict) else None
                declared_count = manifest_json.get("fileCount") if isinstance(manifest_json, dict) else None
                declared_bytes = manifest_json.get("payloadBytes") if isinstance(manifest_json, dict) else None
                aggregate_keys_present = any(
                    key in manifest_json
                    for key in ("manifestSelfExcluded", "fileCount", "payloadBytes")
                ) if isinstance(manifest_json, dict) else False
                comment_match = re.fullmatch(
                    rb"Manifest-SHA256:\s*([0-9a-fA-F]{64})\s*",
                    archive.comment,
                )
                comment_authenticated = bool(
                    comment_match
                    and comment_match.group(1).decode("ascii").lower() == sha256_bytes(manifest_bytes)
                )
                aggregate_authenticated = (
                    manifest_json.get("manifestSelfExcluded") is True
                    and isinstance(declared_count, int)
                    and isinstance(declared_bytes, int)
                ) if isinstance(manifest_json, dict) else False
                if not aggregate_authenticated and not comment_authenticated:
                    raise PostOfficeError(
                        "PON_CUSTODY_NOT_VERIFIED",
                        "Root browser-return manifest lacks verified self-exclusion metadata",
                        {},
                    )
            if not isinstance(members, list):
                raise PostOfficeError("PON_CUSTODY_NOT_VERIFIED", "Browser return JSON manifest lacks members", {})
            listed = []
            for member in members:
                if (
                    not isinstance(member, dict)
                    or not isinstance(member.get("path"), str)
                    or not isinstance(member.get("bytes"), int)
                    or not re.fullmatch(r"[0-9a-fA-F]{64}", str(member.get("sha256", "")))
                ):
                    raise PostOfficeError("PON_CUSTODY_NOT_VERIFIED", "Browser return JSON manifest member is invalid", {})
                listed.append((member["path"], member["bytes"], str(member["sha256"]).lower()))
            if manifest_name in root_manifest_names and aggregate_keys_present and (
                not aggregate_authenticated
                or declared_count != len(listed)
                or declared_bytes != sum(size for _, size, _ in listed)
            ):
                raise PostOfficeError(
                    "PON_CUSTODY_NOT_VERIFIED",
                    "Root browser-return manifest aggregate metadata differs",
                    {},
                )
        if not listed or len(listed) != len(names) - 1:
            raise PostOfficeError(
                "PON_CUSTODY_NOT_VERIFIED",
                "Browser return manifest does not enumerate every non-manifest member",
                {"listedCount": len(listed), "nonManifestMemberCount": len(names) - 1},
            )
        if len({path for path, _, _ in listed}) != len(listed) or manifest_name in {path for path, _, _ in listed}:
            raise PostOfficeError("PON_CUSTODY_NOT_VERIFIED", "Browser return manifest membership is invalid", {})
        for member_path, expected_size, expected_hash in listed:
            if member_path not in names:
                raise PostOfficeError(
                    "PON_CUSTODY_NOT_VERIFIED", "Browser return manifest names an absent member", {"member": member_path}
                )
            member = archive.read(member_path)
            if len(member) != expected_size or sha256_bytes(member) != expected_hash:
                raise PostOfficeError(
                    "PON_CUSTODY_NOT_VERIFIED", "Browser return member differs from its manifest", {"member": member_path}
                )
        allowed_correlations = list(dict.fromkeys([
            source_message_id,
            *(correlation_message_ids or []),
        ]))
        correlation_message_id: str | None = None
        for info in infos:
            if info.file_size > 4 * 1024 * 1024 or not info.filename.lower().endswith((".md", ".json", ".txt", ".yaml", ".yml")):
                continue
            try:
                member_text = archive.read(info.filename).decode("utf-8-sig")
                correlation_message_id = next(
                    (item for item in allowed_correlations if item in member_text),
                    None,
                )
                if correlation_message_id:
                    break
            except UnicodeDecodeError:
                continue
        if not correlation_message_id:
            raise PostOfficeError(
                "PON_OBSERVATION_MISMATCH",
                "Browser return does not identify its source message or a verified same-cycle ancestor",
                {"sourceMessageId": source_message_id},
            )
        return {
            "archiveMemberCount": len(names),
            "correlationMessageId": correlation_message_id,
            "manifestListedMemberCount": len(listed),
            "manifestName": manifest_name,
            "manifestSha256": sha256_bytes(manifest_bytes),
            "zipIntegrity": "PASS",
        }


def _browser_return_correlation_ids(
    con: sqlite3.Connection,
    source_message_id: str,
) -> list[str]:
    """Return a bounded RESPONSE_TO ancestry confined to the source semantic cycle."""
    source = con.execute(
        "SELECT semantic_cycle_id FROM semantic_messages WHERE message_id=?",
        (source_message_id,),
    ).fetchone()
    if not source:
        return []
    cycle_id = source["semantic_cycle_id"]
    seen = {source_message_id}
    frontier = [source_message_id]
    ancestors: list[str] = []
    while frontier and len(seen) <= 32:
        current = frontier.pop(0)
        for relation in con.execute(
            """SELECT related_message_id FROM message_relations
               WHERE message_id=? AND relation_kind='RESPONSE_TO'
               ORDER BY related_message_id""",
            (current,),
        ):
            related_id = str(relation["related_message_id"])
            if related_id in seen:
                continue
            related = con.execute(
                "SELECT semantic_cycle_id FROM semantic_messages WHERE message_id=?",
                (related_id,),
            ).fetchone()
            if not related or related["semantic_cycle_id"] != cycle_id:
                continue
            seen.add(related_id)
            ancestors.append(related_id)
            frontier.append(related_id)
    return ancestors


def _browser_return_source(
    con: sqlite3.Connection, source_message_id: str, source_thread_id: str,
) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row, sqlite3.Row, sqlite3.Row]:
    source = con.execute(
        "SELECT * FROM semantic_messages WHERE message_id=?", (source_message_id,)
    ).fetchone()
    if not source or source["state"] not in {
        "DELIVERED", "ACKNOWLEDGED", "RESPONSE_RETURNED", "REVIEWED", "CLOSED"
    }:
        raise PostOfficeError(
            "PON_CONCURRENCY_CONFLICT",
            "Browser-return source is not delivered",
            {"sourceMessageId": source_message_id},
        )
    source_mailbox = con.execute(
        "SELECT * FROM mailboxes WHERE mailbox_id=? AND generation=? AND status='ACTIVE'",
        (source["recipient_mailbox_id"], source["recipient_generation"]),
    ).fetchone()
    source_endpoint = con.execute(
        "SELECT * FROM endpoints WHERE endpoint_id=? AND status='ACTIVE'",
        (source_mailbox["endpoint_id"],),
    ).fetchone() if source_mailbox else None
    destination_endpoint = con.execute(
        "SELECT * FROM endpoints WHERE endpoint_id=? AND status='ACTIVE'",
        (source["sender_endpoint_id"],),
    ).fetchone()
    destination_mailboxes = con.execute(
        "SELECT * FROM mailboxes WHERE endpoint_id=? AND status='ACTIVE' ORDER BY generation DESC",
        (source["sender_endpoint_id"],),
    ).fetchall()
    if not source_mailbox or not source_endpoint or not destination_endpoint or len(destination_mailboxes) != 1:
        raise PostOfficeError("PON_INPUT_INVALID", "Browser-return endpoint mapping is unavailable", {})
    source_scope = json.loads(source_endpoint["access_scope_json"])
    if _destination_from_scope(source_scope)["destinationThreadId"] != source_thread_id:
        raise PostOfficeError(
            "PON_OBSERVATION_MISMATCH",
            "Browser-return source thread differs from its endpoint binding",
            {},
        )
    return source, source_mailbox, source_endpoint, destination_endpoint, destination_mailboxes[0]


def issue_browser_return_collection_manifest(
    database_path: Path,
    credential_path: Path,
    plugin_root: Path,
    *,
    source_message_id: str,
    source_thread_id: str,
    source_turn_id: str,
    attachment_reference: str,
    attachment_name: str,
    expected_sha256: str,
    expected_size_bytes: int,
    observed_at: str,
    required_text: list[str],
) -> dict[str, Any]:
    """Issue one immutable, mailbox-bound Playwright collection manifest.

    This read-side operation grants no mail authority and records no result.  Its deterministic
    identity makes retry safe; collected bytes must still pass ``ingest_collected_browser_return``.
    """
    expected_sha256 = expected_sha256.lower().removeprefix("sha256:")
    if (
        not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
        or expected_size_bytes < 1
        or expected_size_bytes > 268_435_456
        or Path(attachment_name).name != attachment_name
        or not attachment_name
        or len(attachment_name) > 240
        or not re.fullmatch(r"[0-9a-fA-F-]{36}", source_thread_id)
        or not re.fullmatch(r"[0-9a-fA-F-]{36}", source_turn_id)
        or not attachment_reference
        or len(attachment_reference) > 2048
        or len(required_text) > 16
        or any(not item or len(item) > 512 for item in required_text)
    ):
        raise PostOfficeError("PON_INPUT_INVALID", "Browser-return collection identity is invalid", {})
    _parse_timestamp(observed_at)
    con = _open_writer(database_path, plugin_root)
    try:
        _courier(con, credential_path)
        source, source_mailbox, _, _, _ = _browser_return_source(
            con, source_message_id, source_thread_id
        )
        identity = {
            "sourceMessageId": source_message_id,
            "threadId": source_thread_id,
            "sourceTurnId": source_turn_id,
            "attachmentReference": attachment_reference,
            "attachmentName": attachment_name,
            "expectedBytes": expected_size_bytes,
            "expectedSha256": expected_sha256,
        }
        collection_id = "PON-COLLECTION-BROWSER-RETURN-" + sha256_json(identity)[:24]
        manifest = {
            "schemaVersion": 1,
            "collectionId": collection_id,
            "threadId": source_thread_id,
            "mailboxId": source_mailbox["mailbox_id"],
            "mailboxGeneration": int(source_mailbox["generation"]),
            "scopeKind": "BROWSER_SWEEP",
            "scopeId": source["message_id"],
            "sourceTurnId": source_turn_id,
            "attachmentReference": attachment_reference,
            "attachmentName": attachment_name,
            "expectedBytes": expected_size_bytes,
            "expectedSha256": expected_sha256,
            "observedAt": observed_at,
            "requiredText": required_text,
        }
        con.rollback()
    finally:
        con.close()
    manifest_path = (
        database_path.resolve().parent / "playwright-collections" / "manifests"
        / collection_id / "manifest.json"
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_bytes = canonical_json_bytes(manifest) + b"\n"
    if manifest_path.exists():
        if manifest_path.read_bytes() != manifest_bytes:
            raise PostOfficeError(
                "PON_IDEMPOTENCY_CONFLICT",
                "Retained browser-return collection manifest differs",
                {"collectionId": collection_id},
            )
        replayed = True
    else:
        temporary = manifest_path.with_name(manifest_path.name + "." + uuid.uuid4().hex + ".tmp")
        temporary.write_bytes(manifest_bytes)
        os.replace(temporary, manifest_path)
        replayed = False
    return {
        "ok": True,
        "replayed": replayed,
        "collectionId": collection_id,
        "manifestPath": str(manifest_path),
        "manifestSha256": sha256_bytes(manifest_bytes),
        "sourceMessageId": source_message_id,
    }


def issue_automatic_review_result_collection_manifest(
    database_path: Path,
    credential_path: Path,
    plugin_root: Path,
    *,
    review_id: str,
    activation_dispatch_id: str,
    verdict: str,
    source_thread_id: str,
    source_turn_id: str,
    attachment_reference: str,
    attachment_name: str,
    expected_sha256: str,
    expected_size_bytes: int,
    observed_at: str,
) -> dict[str, Any]:
    """Issue one immutable collection manifest for an active automatic-review result."""
    expected_sha256 = expected_sha256.lower().removeprefix("sha256:")
    expected_name = f"{review_id}_RESULT.md"
    if (
        verdict not in {"PASS", "PASS_WITH_FINDINGS", "CHANGES_REQUIRED", "BLOCKED_BY_EVIDENCE"}
        or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
        or expected_size_bytes < 1
        or expected_size_bytes > 268_435_456
        or attachment_name != expected_name
        or Path(attachment_name).name != attachment_name
        or not re.fullmatch(r"[0-9a-fA-F-]{36}", source_thread_id)
        or not re.fullmatch(r"[0-9a-fA-F-]{36}", source_turn_id)
        or not activation_dispatch_id
        or len(activation_dispatch_id) > 240
        or not attachment_reference
        or len(attachment_reference) > 2048
    ):
        raise PostOfficeError(
            "PON_INPUT_INVALID", "Automatic-review collection identity is invalid", {}
        )
    _parse_timestamp(observed_at)
    con = _open_writer(database_path, plugin_root)
    try:
        _courier(con, credential_path)
        review = con.execute(
            "SELECT * FROM automatic_reviews WHERE review_id=?", (review_id,)
        ).fetchone()
        if not review or review["state"] != "ACTIVE":
            raise PostOfficeError(
                "PON_CONCURRENCY_CONFLICT",
                "Automatic review is not active",
                {"reviewId": review_id},
            )
        if review["reviewer_thread_id"] != source_thread_id:
            raise PostOfficeError(
                "PON_OBSERVATION_MISMATCH",
                "Review result source thread differs from its reviewer binding",
                {},
            )
        message = con.execute(
            "SELECT * FROM semantic_messages WHERE message_id=?",
            (review["semantic_message_id"],),
        ).fetchone()
        mailbox = con.execute(
            """SELECT * FROM mailboxes
               WHERE mailbox_id=? AND generation=? AND endpoint_id=? AND status='ACTIVE'""",
            (
                message["recipient_mailbox_id"],
                message["recipient_generation"],
                review["reviewer_endpoint_id"],
            ),
        ).fetchone() if message else None
        if not message or not mailbox:
            raise PostOfficeError(
                "PON_INPUT_INVALID", "Active review mailbox binding is unavailable", {}
            )
        identity = {
            "reviewId": review_id,
            "activationDispatchId": activation_dispatch_id,
            "threadId": source_thread_id,
            "sourceTurnId": source_turn_id,
            "attachmentReference": attachment_reference,
            "attachmentName": attachment_name,
            "expectedBytes": expected_size_bytes,
            "expectedSha256": expected_sha256,
        }
        collection_id = "PON-COLLECTION-REVIEW-RESULT-" + sha256_json(identity)[:24]
        manifest = {
            "schemaVersion": 1,
            "collectionId": collection_id,
            "threadId": source_thread_id,
            "mailboxId": mailbox["mailbox_id"],
            "mailboxGeneration": int(mailbox["generation"]),
            "scopeKind": "AUTOMATIC_REVIEW",
            "scopeId": review_id,
            "sourceTurnId": source_turn_id,
            "attachmentReference": attachment_reference,
            "attachmentName": attachment_name,
            "expectedBytes": expected_size_bytes,
            "expectedSha256": expected_sha256,
            "observedAt": observed_at,
            "requiredText": [
                "REVIEW RESULT",
                f"Review ID: {review_id}",
                f"Activation Dispatch ID: {activation_dispatch_id}",
                f"Verdict: {verdict}",
            ],
        }
        con.rollback()
    finally:
        con.close()
    manifest_path = (
        database_path.resolve().parent / "playwright-collections" / "manifests"
        / collection_id / "manifest.json"
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_bytes = canonical_json_bytes(manifest) + b"\n"
    if manifest_path.exists():
        if manifest_path.read_bytes() != manifest_bytes:
            raise PostOfficeError(
                "PON_IDEMPOTENCY_CONFLICT",
                "Retained automatic-review collection manifest differs",
                {"collectionId": collection_id},
            )
        replayed = True
    else:
        temporary = manifest_path.with_name(
            manifest_path.name + "." + uuid.uuid4().hex + ".tmp"
        )
        temporary.write_bytes(manifest_bytes)
        os.replace(temporary, manifest_path)
        replayed = False
    return {
        "ok": True,
        "replayed": replayed,
        "collectionId": collection_id,
        "manifestPath": str(manifest_path),
        "manifestSha256": sha256_bytes(manifest_bytes),
        "reviewId": review_id,
    }


def ingest_collected_browser_return(
    database_path: Path,
    credential_path: Path,
    plugin_root: Path,
    *,
    collection_manifest_path: Path,
    result_path: Path,
    collection_receipt: str,
) -> dict[str, Any]:
    """Retain an exact Playwright collection and register its RESPONSE for normal routing."""
    expected_manifest_root = database_path.resolve().parent / "playwright-collections" / "manifests"
    manifest_path = collection_manifest_path.resolve()
    try:
        manifest_path.relative_to(expected_manifest_root.resolve())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise PostOfficeError("PON_INPUT_INVALID", "Collection manifest is unavailable or outside the state root", {}) from exc
    expected_sha256 = str(manifest.get("expectedSha256", ""))
    expected_size = manifest.get("expectedBytes")
    collection_id = str(manifest.get("collectionId", ""))
    thread_id = str(manifest.get("threadId", ""))
    source_message_id = str(manifest.get("scopeId", ""))
    expected_receipt = f"playwright-chatgpt-collection:{collection_id}:{thread_id}:{expected_sha256}"
    if (
        manifest.get("schemaVersion") != 1
        or manifest.get("scopeKind") != "BROWSER_SWEEP"
        or collection_receipt != expected_receipt
        or not isinstance(expected_size, int)
    ):
        raise PostOfficeError("PON_OBSERVATION_MISMATCH", "Playwright collection evidence does not match its manifest", {})
    try:
        data = result_path.read_bytes()
    except OSError as exc:
        raise PostOfficeError("PON_INPUT_INVALID", "Collected browser-return file is unavailable", {}) from exc
    if len(data) != expected_size or sha256_bytes(data) != expected_sha256:
        raise PostOfficeError("PON_OBSERVATION_MISMATCH", "Collected browser-return bytes differ from the manifest", {})
    con = _open_writer(database_path, plugin_root)
    target: Path | None = None
    target_created = False
    try:
        capability, actor = _courier(con, credential_path)
        source, source_mailbox, source_endpoint, _, destination_mailbox = _browser_return_source(
            con, source_message_id, thread_id
        )
        archive_evidence = _validate_manifest_backed_return(
            data,
            source_message_id,
            correlation_message_ids=_browser_return_correlation_ids(con, source_message_id),
        )
        if (
            source_mailbox["mailbox_id"] != manifest.get("mailboxId")
            or int(source_mailbox["generation"]) != int(manifest.get("mailboxGeneration", 0))
        ):
            raise PostOfficeError("PON_OBSERVATION_MISMATCH", "Collection mailbox generation changed", {})
        retained = con.execute(
            """SELECT m.message_id,m.state,b.bundle_id FROM message_relations rel
               JOIN semantic_messages m ON m.message_id=rel.message_id
               JOIN message_bundles b ON b.message_id=m.message_id
               WHERE rel.related_message_id=? AND rel.relation_kind='RESPONSE_TO' AND b.sha256=?""",
            (source_message_id, expected_sha256),
        ).fetchone()
        if retained:
            con.rollback()
            return {
                "ok": True, "replayed": True, "sourceMessageId": source_message_id,
                "messageId": retained["message_id"], "bundleId": retained["bundle_id"],
                "state": retained["state"], "sha256": expected_sha256, "sizeBytes": expected_size,
            }
        relative = f"objects/{expected_sha256[:2]}/{expected_sha256}"
        target = database_path.resolve().parent / Path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if sha256_bytes(target.read_bytes()) != expected_sha256:
                raise PostOfficeError("PON_CUSTODY_NOT_VERIFIED", "Existing CAS object differs from its digest", {})
        else:
            temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
            temporary.write_bytes(data)
            if sha256_bytes(temporary.read_bytes()) != expected_sha256:
                temporary.unlink(missing_ok=True)
                raise PostOfficeError("PON_CUSTODY_NOT_VERIFIED", "Collected browser-return CAS verification failed", {})
            os.replace(temporary, target)
            target_created = True
        now = _timestamp()
        con.execute(
            "INSERT OR IGNORE INTO storage_copies VALUES(?,?,?,?,?,?,?,?)",
            ("PON-STORAGE-" + expected_sha256[:40], expected_sha256, "LOCAL_CAS", relative,
             len(data), "AUTHORITATIVE", "SHA256_READBACK", now),
        )
        project = con.execute("SELECT code FROM projects WHERE project_id=?", (source["project_id"],)).fetchone()
        prefix = f"{project['code']}-C2C-"
        numbers = []
        for row in con.execute("SELECT message_id FROM semantic_messages WHERE message_id LIKE ?", (prefix + "%",)):
            suffix = str(row["message_id"])[len(prefix):]
            if suffix.isdigit():
                numbers.append(int(suffix))
        message_id = prefix + f"{max(numbers, default=0) + 1:06d}"
        suffix = sha256_bytes((source_message_id + expected_sha256).encode("utf-8"))[:24]
        bundle_id = "PON-BUNDLE-BROWSER-RETURN-" + suffix
        attempt_id = "PON-TRANSPORT-BROWSER-RETURN-" + suffix
        content = {
            "collectionId": collection_id, "collectionReceipt": collection_receipt,
            "sourceMessageId": source_message_id, "sourceThreadId": thread_id,
            "sourceTurnId": manifest["sourceTurnId"], "sha256": expected_sha256,
            "sizeBytes": len(data), **archive_evidence,
        }
        con.execute(
            """INSERT INTO semantic_messages(message_id,project_id,mail_domain,message_type,sender_endpoint_id,
               recipient_mailbox_id,recipient_generation,semantic_cycle_id,authority_grant_id,exact_author_action_id,
               requested_action,completion_criteria,content_root,state,aggregate_version,aggregate_root,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'CUSTODY_RECORDED',1,?,?)""",
            (message_id, source["project_id"], source["mail_domain"], "RESPONSE", source_endpoint["endpoint_id"],
             destination_mailbox["mailbox_id"], int(destination_mailbox["generation"]), source["semantic_cycle_id"],
             source["authority_grant_id"], source["exact_author_action_id"],
             "Review the exact retained browser return without inferring acceptance or further work",
             "Record bounded reception while preserving candidate-only status", sha256_json(content), "0" * 64, now),
        )
        con.execute("INSERT INTO message_relations VALUES(?,?,?)", (message_id, source_message_id, "RESPONSE_TO"))
        canonical_name = str(manifest["attachmentName"])
        con.execute(
            "INSERT INTO message_bundles VALUES(?,?,?,?,?,?,?,?,?,?)",
            (bundle_id, message_id, canonical_name, 1, len(data), expected_sha256,
             archive_evidence["manifestSha256"], "REGISTERED", None, now),
        )
        con.execute("INSERT INTO bundle_payloads VALUES(?,?,?,?,?)", (bundle_id, 0, canonical_name, len(data), expected_sha256))
        con.execute(
            "INSERT INTO transport_attempts VALUES(?,?,?,?,?,'PENDING',1,NULL,?,?)",
            (attempt_id, message_id, bundle_id, source_endpoint["endpoint_id"],
             f"{destination_mailbox['mailbox_id']}:{int(destination_mailbox['generation'])}", now, now),
        )
        row = con.execute("SELECT * FROM semantic_messages WHERE message_id=?", (message_id,)).fetchone()
        before_root = _aggregate_root("SemanticMessage", message_id, None)
        after_root = _aggregate_root("SemanticMessage", message_id, _message_state(con, row))
        con.execute("UPDATE semantic_messages SET aggregate_root=? WHERE message_id=?", (after_root, message_id))
        request = _runtime_request(
            actor=actor, capability=capability, message=source,
            operation="runtime.browserReturn.collectAndRoute", aggregate_type="SemanticMessage",
            aggregate_id=message_id, parameters=content,
        )
        event = append_hub_event(
            con, request=request, capability_id=capability["capability_id"], aggregate_version=1,
            before_root=before_root, after_root=after_root,
            event_result={"state": "CUSTODY_RECORDED", "bundleId": bundle_id,
                          "transportAttemptId": attempt_id}, occurred_at=now,
        )
        con.execute("UPDATE semantic_messages SET registered_event_id=? WHERE message_id=?", (event["eventId"], message_id))
        state_root = _operational_state(con)["operationalStateRoot"]
        con.commit()
        return {
            "ok": True, "replayed": False, "sourceMessageId": source_message_id,
            "messageId": message_id, "bundleId": bundle_id, "transportAttemptId": attempt_id,
            "recipientMailboxId": destination_mailbox["mailbox_id"],
            "recipientGeneration": int(destination_mailbox["generation"]),
            "sha256": expected_sha256, "sizeBytes": len(data), "eventId": event["eventId"],
            **archive_evidence, "stateRoot": state_root,
        }
    except Exception:
        if con.in_transaction:
            con.rollback()
        if target_created and target is not None:
            target.unlink(missing_ok=True)
        raise
    finally:
        con.close()


def issue_transport_delivery_manifest(
    database_path: Path,
    credential_path: Path,
    plugin_root: Path,
    *,
    dispatch_id: str,
    lease_token: str,
) -> dict[str, Any]:
    """Issue the immutable Playwright delivery packet for one currently leased dispatch."""
    con = _open_writer(database_path, plugin_root)
    try:
        _, actor = _courier(con, credential_path)
        dispatch = con.execute("SELECT * FROM transport_dispatches WHERE dispatch_id=?", (dispatch_id,)).fetchone()
        if (
            not dispatch or dispatch["state"] != "LEASED"
            or dispatch["channel"] != "PLAYWRIGHT_BROWSER"
            or dispatch["lease_owner_actor_id"] != actor["actor_id"]
            or not hmac.compare_digest(str(dispatch["lease_token_sha256"]), sha256_bytes(lease_token.encode("utf-8")))
            or _parse_timestamp(dispatch["lease_expires_at"]) <= datetime.now(timezone.utc)
        ):
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Playwright dispatch is not actively leased by this courier", {})
        attempt = con.execute("SELECT * FROM transport_attempts WHERE transport_attempt_id=?", (dispatch["transport_attempt_id"],)).fetchone()
        message = con.execute("SELECT * FROM semantic_messages WHERE message_id=?", (attempt["message_id"],)).fetchone()
        bundle = con.execute("SELECT * FROM message_bundles WHERE bundle_id=?", (attempt["bundle_id"],)).fetchone()
        endpoint = con.execute("SELECT * FROM endpoints WHERE endpoint_id=?", (dispatch["destination_endpoint_id"],)).fetchone()
        destination = _destination_from_scope(json.loads(endpoint["access_scope_json"]))
        thread_id = destination["destinationThreadId"]
        if not isinstance(thread_id, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", thread_id):
            raise PostOfficeError("PON_INPUT_INVALID", "Browser destination lacks a ChatGPT conversation UUID", {})
        copy = con.execute(
            "SELECT * FROM storage_copies WHERE content_sha256=? AND location_kind='LOCAL_CAS' LIMIT 1",
            (bundle["sha256"],),
        ).fetchone()
        if not copy:
            raise PostOfficeError("PON_CUSTODY_NOT_VERIFIED", "Browser delivery bundle lacks local archive custody", {})
        source_path = Path(copy["local_location"])
        if not source_path.is_absolute():
            source_path = database_path.resolve().parent / source_path
        canonical_name = str(bundle["canonical_filename"])
        stage_path = database_path.resolve().parent / "playwright-dispatches" / dispatch_id / "attachments" / canonical_name
        prompt = (
            "Post Office direct browser delivery. The exact retained package is attached; do not use Google Drive "
            "or a developer MCP to retrieve it. Process only the bounded envelope below and preserve the package's "
            "stated authority.\n\nEnvelope:\n" + canonical_json_bytes({
                "message_id": message["message_id"], "message_type": message["message_type"],
                "sender_endpoint_id": message["sender_endpoint_id"],
                "recipient_mailbox_id": message["recipient_mailbox_id"],
                "recipient_generation": int(message["recipient_generation"]),
                "semantic_cycle_id": message["semantic_cycle_id"],
                "requested_action": message["requested_action"],
                "completion_criteria": message["completion_criteria"],
                "ordered_payload_sha256": [bundle["sha256"]],
            }).decode("utf-8")
        )
        manifest = {
            "schemaVersion": 1, "dispatchId": dispatch_id, "threadId": thread_id,
            "messageId": message["message_id"], "mailboxId": dispatch["destination_mailbox_id"],
            "mailboxGeneration": int(dispatch["destination_generation"]), "prompt": prompt,
            "attachments": [{"path": str(stage_path), "sourceName": canonical_name,
                             "sizeBytes": int(bundle["size_bytes"]), "sha256": bundle["sha256"]}],
        }
        con.rollback()
    finally:
        con.close()
    stage_path.parent.mkdir(parents=True, exist_ok=True)
    if stage_path.exists():
        if stage_path.stat().st_size != int(bundle["size_bytes"]) or sha256_bytes(stage_path.read_bytes()) != bundle["sha256"]:
            raise PostOfficeError("PON_CUSTODY_NOT_VERIFIED", "Existing staged browser attachment differs", {})
    else:
        temporary = stage_path.with_name(stage_path.name + "." + uuid.uuid4().hex + ".tmp")
        shutil.copyfile(source_path, temporary)
        if temporary.stat().st_size != int(bundle["size_bytes"]) or sha256_bytes(temporary.read_bytes()) != bundle["sha256"]:
            temporary.unlink(missing_ok=True)
            raise PostOfficeError("PON_CUSTODY_NOT_VERIFIED", "Staged browser attachment verification failed", {})
        os.replace(temporary, stage_path)
    manifest_path = stage_path.parent.parent / "delivery-manifest.json"
    manifest_bytes = canonical_json_bytes(manifest) + b"\n"
    if manifest_path.exists():
        if manifest_path.read_bytes() != manifest_bytes:
            raise PostOfficeError("PON_IDEMPOTENCY_CONFLICT", "Retained delivery manifest differs", {"dispatchId": dispatch_id})
        replayed = True
    else:
        temporary = manifest_path.with_name(manifest_path.name + "." + uuid.uuid4().hex + ".tmp")
        temporary.write_bytes(manifest_bytes)
        os.replace(temporary, manifest_path)
        replayed = False
    return {"ok": True, "replayed": replayed, "dispatchId": dispatch_id,
            "manifestPath": str(manifest_path), "manifestSha256": sha256_bytes(manifest_bytes),
            "threadId": thread_id, "messageId": message["message_id"]}


def issue_supplemental_delivery_manifest(
    database_path: Path,
    credential_path: Path,
    plugin_root: Path,
    *,
    message_id: str,
    bundle_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Issue an immutable same-message recovery packet after attachment loss.

    This does not create a new semantic message or pre-claim successful delivery.  After the
    bridge returns its exact receipt, ``record_recovered_transport_receipt`` records the
    supplemental attempt atomically against the original retained message and bundle.
    """
    if not all(isinstance(value, str) and value.strip() for value in (
        message_id, bundle_id, idempotency_key
    )):
        raise PostOfficeError(
            "PON_INPUT_INVALID",
            "Supplemental delivery requires message, bundle and idempotency identities",
            {},
        )
    recovery_id = "PON-RECOVERY-" + sha256_json({
        "messageId": message_id,
        "bundleId": bundle_id,
        "idempotencyKey": idempotency_key,
    })[:32]
    con = _open_writer(database_path, plugin_root)
    try:
        _courier(con, credential_path)
        message = con.execute(
            "SELECT * FROM semantic_messages WHERE message_id=?", (message_id,)
        ).fetchone()
        if not message or message["state"] not in {
            "DELIVERED", "ACKNOWLEDGED", "RESPONSE_RETURNED", "REVIEWED", "CLOSED"
        }:
            raise PostOfficeError(
                "PON_CONCURRENCY_CONFLICT",
                "Supplemental delivery requires an already-delivered semantic message",
                {"messageId": message_id, "state": message["state"] if message else None},
            )
        bundle = con.execute(
            "SELECT * FROM message_bundles WHERE message_id=? AND bundle_id=?",
            (message_id, bundle_id),
        ).fetchone()
        if not bundle:
            raise PostOfficeError(
                "PON_CUSTODY_NOT_VERIFIED",
                "Supplemental delivery does not match the retained message bundle",
                {"messageId": message_id, "bundleId": bundle_id},
            )
        mailbox = con.execute(
            "SELECT * FROM mailboxes WHERE mailbox_id=? AND generation=?",
            (message["recipient_mailbox_id"], message["recipient_generation"]),
        ).fetchone()
        endpoint = con.execute(
            "SELECT * FROM endpoints WHERE endpoint_id=?", (mailbox["endpoint_id"],)
        ).fetchone() if mailbox else None
        if not mailbox or not endpoint or not _destination_is_active(
            con,
            endpoint_id=endpoint["endpoint_id"] if endpoint else None,
            mailbox_id=message["recipient_mailbox_id"],
            generation=int(message["recipient_generation"]),
        ):
            raise PostOfficeError(
                "PON_INPUT_INVALID", "Supplemental browser destination is inactive", {}
            )
        destination = _destination_from_scope(json.loads(endpoint["access_scope_json"]))
        thread_id = destination["destinationThreadId"]
        if not isinstance(thread_id, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", thread_id):
            raise PostOfficeError(
                "PON_INPUT_INVALID", "Browser destination lacks a ChatGPT conversation UUID", {}
            )
        copy = con.execute(
            "SELECT * FROM storage_copies WHERE content_sha256=? AND location_kind='LOCAL_CAS' LIMIT 1",
            (bundle["sha256"],),
        ).fetchone()
        if not copy:
            raise PostOfficeError(
                "PON_CUSTODY_NOT_VERIFIED", "Supplemental delivery lacks local archive custody", {}
            )
        source_path = Path(copy["local_location"])
        if not source_path.is_absolute():
            source_path = database_path.resolve().parent / source_path
        canonical_name = str(bundle["canonical_filename"])
        stage_path = (
            database_path.resolve().parent / "playwright-dispatches" / recovery_id
            / "attachments" / canonical_name
        )
        prompt = (
            "Post Office supplemental attachment recovery for the same immutable delivery. "
            "The exact retained package is attached again because the recipient could not access "
            "the original attachment. This is not a new message, handoff, authority, or candidate. "
            "Do not use Google Drive or a developer MCP. Process the original bounded envelope "
            "below and preserve its stated authority.\n\nEnvelope:\n"
            + canonical_json_bytes({
                "message_id": message["message_id"],
                "message_type": message["message_type"],
                "sender_endpoint_id": message["sender_endpoint_id"],
                "recipient_mailbox_id": message["recipient_mailbox_id"],
                "recipient_generation": int(message["recipient_generation"]),
                "semantic_cycle_id": message["semantic_cycle_id"],
                "requested_action": message["requested_action"],
                "completion_criteria": message["completion_criteria"],
                "ordered_payload_sha256": [bundle["sha256"]],
                "recovery_for_message_id": message["message_id"],
            }).decode("utf-8")
        )
        manifest = {
            "schemaVersion": 1,
            "dispatchId": recovery_id,
            "threadId": thread_id,
            "messageId": message["message_id"],
            "mailboxId": message["recipient_mailbox_id"],
            "mailboxGeneration": int(message["recipient_generation"]),
            "prompt": prompt,
            "attachments": [{
                "path": str(stage_path),
                "sourceName": canonical_name,
                "sizeBytes": int(bundle["size_bytes"]),
                "sha256": bundle["sha256"],
            }],
        }
        con.rollback()
    finally:
        con.close()
    stage_path.parent.mkdir(parents=True, exist_ok=True)
    if stage_path.exists():
        if (
            stage_path.stat().st_size != int(bundle["size_bytes"])
            or sha256_bytes(stage_path.read_bytes()) != bundle["sha256"]
        ):
            raise PostOfficeError(
                "PON_CUSTODY_NOT_VERIFIED", "Existing supplemental attachment differs", {}
            )
    else:
        temporary = stage_path.with_name(stage_path.name + "." + uuid.uuid4().hex + ".tmp")
        shutil.copyfile(source_path, temporary)
        if (
            temporary.stat().st_size != int(bundle["size_bytes"])
            or sha256_bytes(temporary.read_bytes()) != bundle["sha256"]
        ):
            temporary.unlink(missing_ok=True)
            raise PostOfficeError(
                "PON_CUSTODY_NOT_VERIFIED", "Supplemental attachment verification failed", {}
            )
        os.replace(temporary, stage_path)
    manifest_path = stage_path.parent.parent / "delivery-manifest.json"
    manifest_bytes = canonical_json_bytes(manifest) + b"\n"
    if manifest_path.exists():
        if manifest_path.read_bytes() != manifest_bytes:
            raise PostOfficeError(
                "PON_IDEMPOTENCY_CONFLICT",
                "Retained supplemental delivery manifest differs",
                {"recoveryId": recovery_id},
            )
        replayed = True
    else:
        temporary = manifest_path.with_name(
            manifest_path.name + "." + uuid.uuid4().hex + ".tmp"
        )
        temporary.write_bytes(manifest_bytes)
        os.replace(temporary, manifest_path)
        replayed = False
    return {
        "ok": True,
        "replayed": replayed,
        "recoveryId": recovery_id,
        "observableMarker": f"POST-OFFICE-PLAYWRIGHT-DISPATCH {recovery_id}",
        "manifestPath": str(manifest_path),
        "manifestSha256": sha256_bytes(manifest_bytes),
        "threadId": thread_id,
        "messageId": message["message_id"],
        "bundleId": bundle["bundle_id"],
    }


def retain_outbound_package(
    database_path: Path,
    credential_path: Path,
    plugin_root: Path,
    *,
    source_message_id: str,
    result_path: Path,
    expected_sha256: str,
    expected_size_bytes: int,
) -> dict[str, Any]:
    """Retain one manifest-backed local return before semantic routing.

    Task/native returns may already exist as immutable files before a browser-facing semantic
    message is planned.  This operation closes that custody gap without inventing the message,
    destination, authority, or delivery receipt.  Normal ``message.plan`` / ``message.register`` /
    ``message.route`` operations remain responsible for those decisions.
    """
    if not isinstance(source_message_id, str) or not source_message_id.strip():
        raise PostOfficeError("PON_INPUT_INVALID", "Outbound package source message is required", {})
    expected_sha256 = expected_sha256.lower().removeprefix("sha256:")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256) or expected_size_bytes < 1:
        raise PostOfficeError("PON_INPUT_INVALID", "Outbound package identity is invalid", {})
    try:
        data = result_path.read_bytes()
    except OSError as exc:
        raise PostOfficeError(
            "PON_INPUT_INVALID", "Outbound package file is unavailable", {"path": str(result_path)}
        ) from exc
    observed_sha256 = sha256_bytes(data)
    if len(data) != expected_size_bytes or observed_sha256 != expected_sha256:
        raise PostOfficeError(
            "PON_OBSERVATION_MISMATCH",
            "Outbound package bytes differ from the declared identity",
            {
                "expectedSha256": expected_sha256,
                "observedSha256": observed_sha256,
                "expectedSizeBytes": expected_size_bytes,
                "observedSizeBytes": len(data),
            },
        )

    con = _open_writer(database_path, plugin_root)
    target: Path | None = None
    target_created = False
    try:
        capability, actor = _courier(con, credential_path)
        source = con.execute(
            "SELECT * FROM semantic_messages WHERE message_id=?", (source_message_id,)
        ).fetchone()
        if not source or source["state"] not in {
            "DELIVERED", "ACKNOWLEDGED", "RESPONSE_RETURNED", "REVIEWED", "CLOSED"
        }:
            raise PostOfficeError(
                "PON_CONCURRENCY_CONFLICT",
                "Outbound package source is not a delivered semantic message",
                {"sourceMessageId": source_message_id},
            )
        archive_evidence = _validate_manifest_backed_return(
            data,
            source_message_id,
            correlation_message_ids=_browser_return_correlation_ids(con, source_message_id),
        )
        retained = con.execute(
            "SELECT * FROM storage_copies WHERE content_sha256=? AND location_kind='LOCAL_CAS' "
            "ORDER BY storage_copy_id LIMIT 1",
            (expected_sha256,),
        ).fetchone()
        if retained:
            retained_path = Path(retained["local_location"])
            if not retained_path.is_absolute():
                retained_path = database_path.resolve().parent / retained_path
            if (
                int(retained["size_bytes"]) != len(data)
                or not retained_path.exists()
                or retained_path.stat().st_size != len(data)
                or sha256_bytes(retained_path.read_bytes()) != expected_sha256
            ):
                raise PostOfficeError(
                    "PON_CUSTODY_NOT_VERIFIED", "Retained outbound storage copy is inconsistent", {}
                )
            con.rollback()
            return {
                "ok": True,
                "replayed": True,
                "sourceMessageId": source_message_id,
                "storageCopyId": retained["storage_copy_id"],
                "localLocation": str(retained_path),
                "sha256": expected_sha256,
                "sizeBytes": len(data),
                **archive_evidence,
            }

        relative = f"objects/{expected_sha256[:2]}/{expected_sha256}"
        target = database_path.resolve().parent / Path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.stat().st_size != len(data) or sha256_bytes(target.read_bytes()) != expected_sha256:
                raise PostOfficeError(
                    "PON_CUSTODY_NOT_VERIFIED", "Existing outbound CAS object differs", {}
                )
        else:
            temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
            temporary.write_bytes(data)
            if temporary.stat().st_size != len(data) or sha256_bytes(temporary.read_bytes()) != expected_sha256:
                temporary.unlink(missing_ok=True)
                raise PostOfficeError(
                    "PON_CUSTODY_NOT_VERIFIED", "Outbound package CAS verification failed", {}
                )
            os.replace(temporary, target)
            target_created = True

        now = _timestamp()
        storage_id = "PON-STORAGE-OUTBOUND-" + expected_sha256[:32]
        detail = {
            "sourceMessageId": source_message_id,
            "storageCopyId": storage_id,
            "localLocation": relative,
            "sha256": expected_sha256,
            "sizeBytes": len(data),
            **archive_evidence,
        }
        con.execute(
            "INSERT INTO storage_copies VALUES(?,?,?,?,?,?,?,?)",
            (
                storage_id,
                expected_sha256,
                "LOCAL_CAS",
                relative,
                len(data),
                "AUTHORITATIVE",
                "SHA256_READBACK",
                now,
            ),
        )
        before_root = _aggregate_root("StorageCopy", storage_id, None)
        after_root = _aggregate_root("StorageCopy", storage_id, detail)
        request = _runtime_request(
            actor=actor,
            capability=capability,
            message=source,
            operation="runtime.outboundPackage.retain",
            aggregate_type="StorageCopy",
            aggregate_id=storage_id,
            parameters=detail,
        )
        event = append_hub_event(
            con,
            request=request,
            capability_id=capability["capability_id"],
            aggregate_version=1,
            before_root=before_root,
            after_root=after_root,
            event_result={"state": "RETAINED", **detail},
            occurred_at=now,
        )
        state_root = _operational_state(con)["operationalStateRoot"]
        con.commit()
        return {
            "ok": True,
            "replayed": False,
            "eventId": event["eventId"],
            "stateRoot": state_root,
            **detail,
        }
    except Exception:
        if con.in_transaction:
            con.rollback()
        if target_created and target is not None:
            target.unlink(missing_ok=True)
        raise
    finally:
        con.close()


def ingest_recovered_browser_return(
    database_path: Path,
    credential_path: Path,
    plugin_root: Path,
    *,
    source_message_id: str,
    result_path: Path,
    expected_sha256: str,
    expected_size_bytes: int,
    source_thread_id: str,
    source_turn_id: str,
    destination_thread_id: str,
    destination_turn_id: str,
) -> dict[str, Any]:
    """Atomically retain and receipt an already-delivered manifest-backed browser return.

    This is a recovery operation for a user shortcut.  It never sends to the destination browser;
    the destination turn plus the exact attachment digest are the delivery evidence.
    """
    values = (
        source_message_id, expected_sha256, source_thread_id, source_turn_id,
        destination_thread_id, destination_turn_id,
    )
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise PostOfficeError("PON_INPUT_INVALID", "Recovered browser-return evidence is incomplete", {})
    expected_sha256 = expected_sha256.lower().removeprefix("sha256:")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256) or expected_size_bytes < 1:
        raise PostOfficeError("PON_INPUT_INVALID", "Recovered browser-return identity is invalid", {})
    try:
        data = result_path.read_bytes()
    except OSError as exc:
        raise PostOfficeError("PON_INPUT_INVALID", "Recovered browser-return file is unavailable", {"path": str(result_path)}) from exc
    observed_sha256 = sha256_bytes(data)
    if len(data) != expected_size_bytes or observed_sha256 != expected_sha256:
        raise PostOfficeError(
            "PON_OBSERVATION_MISMATCH",
            "Recovered browser-return bytes differ from the observed browser result",
            {"expectedSha256": expected_sha256, "observedSha256": observed_sha256,
             "expectedSizeBytes": expected_size_bytes, "observedSizeBytes": len(data)},
        )
    observed_receipt_id = (
        f"chatgpt-shortcut:{destination_thread_id}:{destination_turn_id}:sha256:{expected_sha256}"
    )

    con = _open_writer(database_path, plugin_root)
    target: Path | None = None
    target_created = False
    try:
        capability, actor = _courier(con, credential_path)
        source = con.execute("SELECT * FROM semantic_messages WHERE message_id=?", (source_message_id,)).fetchone()
        if not source or source["state"] not in {"DELIVERED", "ACKNOWLEDGED", "RESPONSE_RETURNED", "REVIEWED", "CLOSED"}:
            raise PostOfficeError(
                "PON_CONCURRENCY_CONFLICT", "Recovered browser return source is not delivered", {"sourceMessageId": source_message_id}
            )
        source_mailbox = con.execute(
            "SELECT * FROM mailboxes WHERE mailbox_id=? AND generation=? AND status='ACTIVE'",
            (source["recipient_mailbox_id"], source["recipient_generation"]),
        ).fetchone()
        source_endpoint = con.execute(
            "SELECT * FROM endpoints WHERE endpoint_id=? AND status='ACTIVE'", (source_mailbox["endpoint_id"],)
        ).fetchone() if source_mailbox else None
        destination_endpoint = con.execute(
            "SELECT * FROM endpoints WHERE endpoint_id=? AND status='ACTIVE'", (source["sender_endpoint_id"],)
        ).fetchone()
        destination_mailboxes = con.execute(
            "SELECT * FROM mailboxes WHERE endpoint_id=? AND status='ACTIVE' ORDER BY generation DESC",
            (source["sender_endpoint_id"],),
        ).fetchall()
        if not source_endpoint or not destination_endpoint or len(destination_mailboxes) != 1:
            raise PostOfficeError("PON_INPUT_INVALID", "Recovered browser-return endpoint mapping is unavailable", {})
        destination_mailbox = destination_mailboxes[0]
        archive_evidence = _validate_manifest_backed_return(
            data,
            source_message_id,
            correlation_message_ids=_browser_return_correlation_ids(con, source_message_id),
        )
        source_scope = json.loads(source_endpoint["access_scope_json"])
        destination_scope = json.loads(destination_endpoint["access_scope_json"])
        if _destination_from_scope(source_scope)["destinationThreadId"] != source_thread_id:
            raise PostOfficeError("PON_OBSERVATION_MISMATCH", "Browser-return source thread differs from its endpoint binding", {})
        if _destination_from_scope(destination_scope)["destinationThreadId"] != destination_thread_id:
            raise PostOfficeError("PON_OBSERVATION_MISMATCH", "Browser-return destination thread differs from its endpoint binding", {})
        replay = con.execute(
            """SELECT m.message_id,b.bundle_id,d.dispatch_id,r.receipt_id
               FROM message_relations rel
               JOIN semantic_messages m ON m.message_id=rel.message_id
               JOIN message_bundles b ON b.message_id=m.message_id
               JOIN transport_attempts t ON t.message_id=m.message_id
               JOIN transport_dispatches d ON d.transport_attempt_id=t.transport_attempt_id
               JOIN runtime_receipts r ON r.dispatch_id=d.dispatch_id AND r.receipt_kind='RECOVERED'
               WHERE rel.related_message_id=? AND rel.relation_kind='RESPONSE_TO'
                 AND b.sha256=? AND d.observed_receipt_id=?""",
            (source_message_id, expected_sha256, observed_receipt_id),
        ).fetchone()
        if replay:
            con.rollback()
            return {
                "ok": True, "replayed": True, "sourceMessageId": source_message_id,
                "messageId": replay["message_id"], "bundleId": replay["bundle_id"],
                "dispatchId": replay["dispatch_id"], "runtimeReceiptId": replay["receipt_id"],
                "sha256": expected_sha256, "sizeBytes": expected_size_bytes,
            }
        project = con.execute("SELECT code FROM projects WHERE project_id=?", (source["project_id"],)).fetchone()
        if not project:
            raise PostOfficeError("PON_INPUT_INVALID", "Recovered browser-return project is unavailable", {})
        prefix = f"{project['code']}-C2C-"
        retained_numbers = []
        for row in con.execute("SELECT message_id FROM semantic_messages WHERE message_id LIKE ?", (prefix + "%",)):
            suffix = str(row["message_id"])[len(prefix):]
            if suffix.isdigit():
                retained_numbers.append(int(suffix))
        message_id = prefix + f"{max(retained_numbers, default=0) + 1:06d}"
        suffix = sha256_bytes((source_message_id + expected_sha256).encode("utf-8"))[:24]
        bundle_id = "PON-BUNDLE-BROWSER-RETURN-" + suffix
        attempt_id = "PON-TRANSPORT-BROWSER-RETURN-" + suffix
        dispatch_id = "PON-DISPATCH-BROWSER-RETURN-" + suffix
        if con.execute(
            """SELECT 1 FROM message_relations rel JOIN message_bundles b ON b.message_id=rel.message_id
               WHERE rel.related_message_id=? AND rel.relation_kind='RESPONSE_TO' AND b.sha256=?""",
            (source_message_id, expected_sha256),
        ).fetchone():
            raise PostOfficeError(
                "PON_CONCURRENCY_CONFLICT", "The browser return is already retained with different delivery evidence", {}
            )

        relative = f"objects/{expected_sha256[:2]}/{expected_sha256}"
        target = database_path.parent / Path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if sha256_bytes(target.read_bytes()) != expected_sha256:
                raise PostOfficeError("PON_CUSTODY_NOT_VERIFIED", "Existing CAS object differs from its digest", {})
        else:
            temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
            temporary.write_bytes(data)
            if sha256_bytes(temporary.read_bytes()) != expected_sha256:
                temporary.unlink(missing_ok=True)
                raise PostOfficeError("PON_CUSTODY_NOT_VERIFIED", "Recovered browser-return CAS verification failed", {})
            os.replace(temporary, target)
            target_created = True

        now = _timestamp()
        storage_id = "PON-STORAGE-" + expected_sha256[:40]
        con.execute(
            "INSERT OR IGNORE INTO storage_copies VALUES(?,?,?,?,?,?,?,?)",
            (storage_id, expected_sha256, "LOCAL_CAS", relative, len(data), "AUTHORITATIVE", "SHA256_READBACK", now),
        )
        content = {
            "sourceMessageId": source_message_id, "sourceThreadId": source_thread_id,
            "sourceTurnId": source_turn_id, "destinationThreadId": destination_thread_id,
            "destinationTurnId": destination_turn_id, "sha256": expected_sha256,
            "sizeBytes": len(data), **archive_evidence,
        }
        con.execute(
            """INSERT INTO semantic_messages(message_id,project_id,mail_domain,message_type,sender_endpoint_id,
               recipient_mailbox_id,recipient_generation,semantic_cycle_id,authority_grant_id,exact_author_action_id,
               requested_action,completion_criteria,content_root,state,aggregate_version,aggregate_root,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'DELIVERED',1,?,?)""",
            (
                message_id, source["project_id"], source["mail_domain"], "RESPONSE", source_endpoint["endpoint_id"],
                destination_mailbox["mailbox_id"], int(destination_mailbox["generation"]), source["semantic_cycle_id"],
                source["authority_grant_id"], source["exact_author_action_id"],
                "Review the exact retained browser return without inferring acceptance or further work",
                "Record bounded reception while preserving candidate-only status", sha256_json(content), "0" * 64, now,
            ),
        )
        con.execute("INSERT INTO message_relations VALUES(?,?,?)", (message_id, source_message_id, "RESPONSE_TO"))
        con.execute(
            "INSERT INTO message_bundles VALUES(?,?,?,?,?,?,?,?,?,?)",
            (bundle_id, message_id, result_path.name.removesuffix("(1).zip") + ".zip" if result_path.name.endswith("(1).zip") else result_path.name,
             1, len(data), expected_sha256, archive_evidence["manifestSha256"], "REGISTERED", None, now),
        )
        con.execute(
            "INSERT INTO bundle_payloads VALUES(?,?,?,?,?)", (bundle_id, 0, result_path.name, len(data), expected_sha256)
        )
        destination = f"{destination_mailbox['mailbox_id']}:{int(destination_mailbox['generation'])}"
        con.execute(
            "INSERT INTO transport_attempts VALUES(?,?,?,?,?,'RECEIPTED',1,NULL,?,?)",
            (attempt_id, message_id, bundle_id, source_endpoint["endpoint_id"], destination, now, now),
        )
        marker = f"BROWSER-RETURN-SHA256 {expected_sha256}"
        con.execute(
            "INSERT INTO transport_dispatches VALUES(?,?,?,?,?,?,'RECEIPTED',NULL,NULL,NULL,?,?,1,?,NULL,?,?)",
            (
                dispatch_id, attempt_id, "PLAYWRIGHT_BROWSER", destination_endpoint["endpoint_id"],
                destination_mailbox["mailbox_id"], int(destination_mailbox["generation"]),
                marker, observed_receipt_id, now, now, now,
            ),
        )
        row = con.execute("SELECT * FROM semantic_messages WHERE message_id=?", (message_id,)).fetchone()
        before_root = _aggregate_root("SemanticMessage", message_id, None)
        after_root = _aggregate_root("SemanticMessage", message_id, _message_state(con, row))
        con.execute("UPDATE semantic_messages SET aggregate_root=? WHERE message_id=?", (after_root, message_id))
        request = _runtime_request(
            actor=actor, capability=capability, message=source,
            operation="runtime.browserReturn.ingestRecovered", aggregate_type="SemanticMessage",
            aggregate_id=message_id, parameters=content,
        )
        event = append_hub_event(
            con, request=request, capability_id=capability["capability_id"], aggregate_version=1,
            before_root=before_root, after_root=after_root,
            event_result={"state": "DELIVERED", "sourceMessageId": source_message_id,
                          "bundleId": bundle_id, "dispatchId": dispatch_id,
                          "observedReceiptId": observed_receipt_id}, occurred_at=now,
        )
        con.execute("UPDATE semantic_messages SET registered_event_id=? WHERE message_id=?", (event["eventId"], message_id))
        receipt_id = _record_runtime_receipt(
            con, dispatch_id, "RECOVERED",
            {**content, "marker": marker, "receiptId": observed_receipt_id,
             "recovery": "USER_SHORTCUT_ALREADY_DELIVERED"}, now,
        )
        state_root = _operational_state(con)["operationalStateRoot"]
        con.commit()
        return {
            "ok": True, "replayed": False, "sourceMessageId": source_message_id,
            "messageId": message_id, "bundleId": bundle_id, "transportAttemptId": attempt_id,
            "dispatchId": dispatch_id, "runtimeReceiptId": receipt_id, "eventId": event["eventId"],
            "recipientMailboxId": destination_mailbox["mailbox_id"],
            "recipientGeneration": int(destination_mailbox["generation"]),
            "destinationThreadId": destination_thread_id, "sha256": expected_sha256,
            "sizeBytes": len(data), **archive_evidence, "stateRoot": state_root,
        }
    except Exception:
        if con.in_transaction:
            con.rollback()
        if target_created and target is not None:
            target.unlink(missing_ok=True)
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
        reviewer_mailbox = con.execute(
            "SELECT * FROM mailboxes WHERE mailbox_id=? AND generation=?",
            (message["recipient_mailbox_id"], message["recipient_generation"]),
        ).fetchone()
        if (
            not reviewer_actor
            or reviewer_actor["actor_kind"] != "BROWSER"
            or reviewer_actor["status"] != "ACTIVE"
            or not reviewer_mailbox
            or not _destination_is_active(
                con,
                endpoint_id=reviewer_endpoint_id,
                mailbox_id=reviewer_mailbox["mailbox_id"],
                generation=int(reviewer_mailbox["generation"]),
            )
        ):
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
        con.execute(
            """UPDATE transport_attempts SET state='STORED',updated_at=?
               WHERE message_id=? AND state='PENDING'
                 AND source='AUTOMATIC_REVIEW_COMPANION'""",
            (now, semantic_message_id),
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
                 JOIN endpoints e ON e.endpoint_id=r.reviewer_endpoint_id AND e.status='ACTIVE'
                 JOIN actors a ON a.actor_id=e.actor_id AND a.status='ACTIVE'
                 JOIN semantic_messages m ON m.message_id=r.semantic_message_id
                 JOIN mailboxes mb ON mb.mailbox_id=m.recipient_mailbox_id
                                  AND mb.generation=m.recipient_generation
                                  AND mb.endpoint_id=e.endpoint_id AND mb.status='ACTIVE'
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


def issue_automatic_review_activation_manifest(
    database_path: Path,
    credential_path: Path,
    plugin_root: Path,
    *,
    review_id: str,
    activation_dispatch_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Issue one immutable package-bearing activation for an active browser reviewer."""
    activation_dispatch_id = activation_dispatch_id.strip()
    idempotency_key = idempotency_key.strip()
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", activation_dispatch_id)
        or not idempotency_key
        or len(idempotency_key) > 240
    ):
        raise PostOfficeError("PON_INPUT_INVALID", "Review activation identity is invalid", {})
    con = _open_writer(database_path, plugin_root)
    created_directory: Path | None = None
    try:
        capability, actor = _courier(con, credential_path)
        review = con.execute(
            "SELECT * FROM automatic_reviews WHERE review_id=?", (review_id,)
        ).fetchone()
        if not review or review["state"] != "ACTIVE":
            raise PostOfficeError(
                "PON_CONCURRENCY_CONFLICT", "Automatic review is not active", {"reviewId": review_id}
            )
        message = con.execute(
            "SELECT * FROM semantic_messages WHERE message_id=?", (review["semantic_message_id"],)
        ).fetchone()
        bundle = con.execute(
            "SELECT * FROM message_bundles WHERE message_id=?", (review["semantic_message_id"],)
        ).fetchone()
        mailbox = con.execute(
            """SELECT * FROM mailboxes
               WHERE mailbox_id=? AND generation=? AND endpoint_id=? AND status='ACTIVE'""",
            (
                message["recipient_mailbox_id"] if message else None,
                message["recipient_generation"] if message else None,
                review["reviewer_endpoint_id"],
            ),
        ).fetchone()
        retained_copy = con.execute(
            """SELECT * FROM storage_copies
               WHERE content_sha256=? AND location_kind='LOCAL_CAS'
               ORDER BY verified_at DESC LIMIT 1""",
            (review["package_sha256"],),
        ).fetchone()
        if not message or not bundle or not mailbox or not retained_copy:
            raise PostOfficeError(
                "PON_CUSTODY_NOT_VERIFIED",
                "Review activation lacks its active mailbox or verified package custody",
                {},
            )
        canonical_name = review["package_name"] or bundle["canonical_filename"]
        package_size = review["package_size_bytes"] or retained_copy["size_bytes"]
        if (
            int(package_size) < 1
            or int(retained_copy["size_bytes"]) != int(package_size)
            or Path(canonical_name).name != canonical_name
        ):
            raise PostOfficeError("PON_CUSTODY_NOT_VERIFIED", "Review package identity differs", {})
        request_fingerprint = sha256_json({
            "reviewId": review_id,
            "activationDispatchId": activation_dispatch_id,
            "reviewerThreadId": review["reviewer_thread_id"],
            "mailboxId": mailbox["mailbox_id"],
            "mailboxGeneration": int(mailbox["generation"]),
            "packageSha256": review["package_sha256"],
        })
        existing = con.execute(
            "SELECT * FROM automatic_review_activations WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if existing:
            if existing["request_fingerprint"] != request_fingerprint:
                raise PostOfficeError(
                    "PON_IDEMPOTENCY_CONFLICT", "Review activation replay differs", {}
                )
            manifest_path = Path(existing["manifest_path"])
            if (
                not manifest_path.is_file()
                or sha256_bytes(manifest_path.read_bytes()) != existing["manifest_sha256"]
            ):
                raise PostOfficeError(
                    "PON_CUSTODY_NOT_VERIFIED", "Retained review activation manifest differs", {}
                )
            con.rollback()
            return {
                "ok": True,
                "replayed": True,
                "activationId": existing["activation_id"],
                "reviewId": review_id,
                "activationDispatchId": existing["activation_dispatch_id"],
                "state": existing["state"],
                "manifestPath": str(manifest_path),
                "manifestSha256": existing["manifest_sha256"],
            }
        conflict = con.execute(
            "SELECT activation_id FROM automatic_review_activations WHERE review_id=? OR activation_dispatch_id=?",
            (review_id, activation_dispatch_id),
        ).fetchone()
        if conflict:
            raise PostOfficeError(
                "PON_IDEMPOTENCY_CONFLICT",
                "Review already has a different activation identity",
                {"activationId": conflict["activation_id"]},
            )
        source_path = Path(retained_copy["local_location"])
        if not source_path.is_absolute():
            source_path = database_path.resolve().parent / source_path
        if (
            not source_path.is_file()
            or source_path.stat().st_size != int(package_size)
            or sha256_bytes(source_path.read_bytes()) != review["package_sha256"]
        ):
            raise PostOfficeError("PON_CUSTODY_NOT_VERIFIED", "Retained review package differs", {})
        result_name = f"{review_id}_RESULT.md"
        subject = review["subject"] or message["requested_action"]
        prompt = (
            f"AUTOMATIC CSX CODE REVIEW\n\nReview ID: {review_id}\n"
            f"Activation Dispatch ID: {activation_dispatch_id}\nSubject: {subject}\n"
            f"Package: attached as {canonical_name}\n"
            f"Package SHA-256: {review['package_sha256']}\n"
            f"Exact result filename: {result_name}\n\n"
            "Process only this request under the automatic-review guidance and the attached package's "
            "REVIEW_CONTRACT.md. This is a read-only request-review-return transaction. Do not modify "
            "code, create a patch, contact another task, open a handoff, or infer implementation authority. "
            f"Write the complete review as exact UTF-8 Markdown named {result_name}. The file must begin "
            f"exactly with REVIEW RESULT and include Review ID: {review_id}, Activation Dispatch ID: "
            f"{activation_dispatch_id}, and exactly one supported Verdict line. Attach that exact Markdown "
            "file to the final response and report its byte count and SHA-256. Do not upload the result to "
            "Drive, create a ZIP, or create a response package. Keep the final response compact; the attached "
            "Markdown file is the authoritative complete review."
        )
        now = _timestamp()
        activation_id = "PON-REVIEW-ACTIVATION-" + uuid.uuid4().hex
        created_directory = database_path.resolve().parent / "playwright-activations" / activation_id
        attachment_path = created_directory / "attachments" / canonical_name
        attachment_path.parent.mkdir(parents=True, exist_ok=False)
        shutil.copyfile(source_path, attachment_path)
        if (
            attachment_path.stat().st_size != int(package_size)
            or sha256_bytes(attachment_path.read_bytes()) != review["package_sha256"]
        ):
            raise PostOfficeError("PON_CUSTODY_NOT_VERIFIED", "Staged review package differs", {})
        manifest = {
            "schemaVersion": 1,
            "kind": "AUTOMATIC_REVIEW_ACTIVATION",
            "activationId": activation_id,
            "reviewId": review_id,
            "dispatchId": activation_dispatch_id,
            "threadId": review["reviewer_thread_id"],
            "mailboxId": mailbox["mailbox_id"],
            "mailboxGeneration": int(mailbox["generation"]),
            "prompt": prompt,
            "promptSha256": sha256_bytes(prompt.encode("utf-8")),
            "attachments": [{
                "path": str(attachment_path),
                "sourceName": canonical_name,
                "sizeBytes": int(package_size),
                "sha256": review["package_sha256"],
            }],
            "issuedAt": now,
        }
        manifest_path = created_directory / "activation-manifest.json"
        manifest_bytes = canonical_json_bytes(manifest) + b"\n"
        manifest_path.write_bytes(manifest_bytes)
        manifest_sha256 = sha256_bytes(manifest_bytes)
        con.execute(
            """INSERT INTO automatic_review_activations(
               activation_id,review_id,activation_dispatch_id,reviewer_thread_id,mailbox_id,
               mailbox_generation,prompt_sha256,manifest_path,manifest_sha256,state,idempotency_key,
               request_fingerprint,source_message_id,receipt_reference,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,'PACKET_ISSUED',?,?,NULL,NULL,?,?)""",
            (
                activation_id, review_id, activation_dispatch_id, review["reviewer_thread_id"],
                mailbox["mailbox_id"], int(mailbox["generation"]), manifest["promptSha256"],
                str(manifest_path), manifest_sha256, idempotency_key, request_fingerprint, now, now,
            ),
        )
        aggregate_root = _aggregate_root("AutomaticReview", review_id, _review_state(review))
        version = _aggregate_position(
            con, aggregate_type="AutomaticReview", aggregate_id=review_id, current_root=aggregate_root
        )
        request = _runtime_request(
            actor=actor, capability=capability, message=message,
            operation="runtime.review.activation.issue", aggregate_type="AutomaticReview",
            aggregate_id=review_id, parameters={"activationDispatchId": activation_dispatch_id},
        )
        event = append_hub_event(
            con, request=request, capability_id=capability["capability_id"],
            aggregate_version=version + 1, before_root=aggregate_root, after_root=aggregate_root,
            event_result={"state": "PACKET_ISSUED", "activationId": activation_id,
                          "manifestSha256": manifest_sha256}, occurred_at=now,
        )
        con.commit()
        return {
            "ok": True, "replayed": False, "activationId": activation_id,
            "reviewId": review_id, "activationDispatchId": activation_dispatch_id,
            "state": "PACKET_ISSUED", "manifestPath": str(manifest_path),
            "manifestSha256": manifest_sha256, "eventId": event["eventId"],
        }
    except Exception:
        if con.in_transaction:
            con.rollback()
        if created_directory and created_directory.exists():
            shutil.rmtree(created_directory)
        raise
    finally:
        con.close()


def record_automatic_review_activation_receipt(
    database_path: Path,
    credential_path: Path,
    plugin_root: Path,
    *,
    review_id: str,
    activation_dispatch_id: str,
    source_message_id: str,
    receipt_reference: str,
) -> dict[str, Any]:
    """Record the exact visible browser receipt for one issued review activation."""
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", source_message_id):
        raise PostOfficeError("PON_INPUT_INVALID", "Activation source message ID must be a UUID", {})
    con = _open_writer(database_path, plugin_root)
    try:
        capability, actor = _courier(con, credential_path)
        activation = con.execute(
            """SELECT a.*,r.semantic_message_id FROM automatic_review_activations a
               JOIN automatic_reviews r ON r.review_id=a.review_id
               WHERE a.review_id=? AND a.activation_dispatch_id=?""",
            (review_id, activation_dispatch_id),
        ).fetchone()
        if not activation:
            raise PostOfficeError("PON_INPUT_INVALID", "Review activation is unavailable", {})
        expected_receipt = (
            f"playwright-chatgpt-review-activation:{review_id}:{activation_dispatch_id}:"
            f"{activation['reviewer_thread_id']}:{source_message_id}"
        )
        if receipt_reference != expected_receipt:
            raise PostOfficeError("PON_OBSERVATION_MISMATCH", "Activation receipt identity differs", {})
        if activation["state"] == "SENT":
            if (
                activation["source_message_id"] != source_message_id
                or activation["receipt_reference"] != receipt_reference
            ):
                raise PostOfficeError("PON_IDEMPOTENCY_CONFLICT", "Activation receipt replay differs", {})
            con.rollback()
            return {
                "ok": True, "replayed": True, "activationId": activation["activation_id"],
                "reviewId": review_id, "state": "SENT", "sourceMessageId": source_message_id,
            }
        review = con.execute(
            "SELECT * FROM automatic_reviews WHERE review_id=?", (review_id,)
        ).fetchone()
        message = con.execute(
            "SELECT * FROM semantic_messages WHERE message_id=?", (activation["semantic_message_id"],)
        ).fetchone()
        if not review or review["state"] != "ACTIVE" or not message:
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Active review is unavailable", {})
        now = _timestamp()
        con.execute(
            """UPDATE automatic_review_activations
               SET state='SENT',source_message_id=?,receipt_reference=?,updated_at=?
               WHERE activation_id=? AND state='PACKET_ISSUED'""",
            (source_message_id, receipt_reference, now, activation["activation_id"]),
        )
        aggregate_root = _aggregate_root("AutomaticReview", review_id, _review_state(review))
        version = _aggregate_position(
            con, aggregate_type="AutomaticReview", aggregate_id=review_id, current_root=aggregate_root
        )
        request = _runtime_request(
            actor=actor, capability=capability, message=message,
            operation="runtime.review.activation.receipt", aggregate_type="AutomaticReview",
            aggregate_id=review_id, parameters={"activationDispatchId": activation_dispatch_id},
        )
        event = append_hub_event(
            con, request=request, capability_id=capability["capability_id"],
            aggregate_version=version + 1, before_root=aggregate_root, after_root=aggregate_root,
            event_result={"state": "SENT", "activationId": activation["activation_id"],
                          "sourceMessageId": source_message_id}, occurred_at=now,
        )
        con.commit()
        return {
            "ok": True, "replayed": False, "activationId": activation["activation_id"],
            "reviewId": review_id, "state": "SENT", "sourceMessageId": source_message_id,
            "eventId": event["eventId"],
        }
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
        before_root = _aggregate_root("AutomaticReview", review_id, before)
        position = _aggregate_position(
            con, aggregate_type="AutomaticReview", aggregate_id=review_id,
            current_root=before_root,
        )
        con.execute("UPDATE automatic_reviews SET state='RETURNED',returned_at=?,result_message_id=?,wake_dispatch_id=? WHERE review_id=?", (now, result_message_id, dispatch["dispatch_id"], review_id))
        updated_review = con.execute("SELECT * FROM automatic_reviews WHERE review_id=?", (review_id,)).fetchone()
        after = _review_state(updated_review)
        request_message = con.execute("SELECT * FROM semantic_messages WHERE message_id=?", (review["semantic_message_id"],)).fetchone()
        request = _runtime_request(actor=actor, capability=capability, message=request_message, operation="runtime.review.return", aggregate_type="AutomaticReview", aggregate_id=review_id, parameters={"resultMessageId": result_message_id, "wakeDispatchId": dispatch["dispatch_id"]})
        event = append_hub_event(con, request=request, capability_id=capability["capability_id"], aggregate_version=position + 1, before_root=before_root, after_root=_aggregate_root("AutomaticReview", review_id, after), event_result={"state": "RETURNED", "wakeDispatchId": dispatch["dispatch_id"]}, occurred_at=now)
        con.commit()
        return {"ok": True, "reviewId": review_id, "state": "RETURNED", "resultMessageId": result_message_id, "wakeDispatchId": dispatch["dispatch_id"], "eventId": event["eventId"], "heartbeatRequired": False}
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()


def ingest_automatic_review_result(
    database_path: Path, credential_path: Path, plugin_root: Path, *, review_id: str,
    result_path: Path, source_thread_id: str, source_message_id: str,
    activation_dispatch_id: str, verdict: str,
) -> dict[str, Any]:
    """Import one exact browser review result and materialize its requester wake atomically."""
    if verdict not in {"PASS", "PASS_WITH_FINDINGS", "CHANGES_REQUIRED", "BLOCKED_BY_EVIDENCE"}:
        raise PostOfficeError("PON_INPUT_INVALID", "Automatic-review verdict is unsupported", {})
    if result_path.is_symlink():
        raise PostOfficeError("PON_CUSTODY_NOT_VERIFIED", "Review result must be an unlinked regular file", {})
    result_path = result_path.resolve(strict=True)
    if not result_path.is_file():
        raise PostOfficeError("PON_CUSTODY_NOT_VERIFIED", "Review result must be an unlinked regular file", {})
    data = result_path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PostOfficeError("PON_INPUT_INVALID", "Review result is not exact UTF-8", {}) from exc
    if not text.startswith("REVIEW RESULT"):
        raise PostOfficeError("PON_INPUT_INVALID", "Review result does not begin with REVIEW RESULT", {})
    required = (
        f"Review ID: {review_id}",
        f"Activation Dispatch ID: {activation_dispatch_id}",
        f"Verdict: {verdict}",
    )
    if any(marker not in text for marker in required):
        raise PostOfficeError("PON_OBSERVATION_MISMATCH", "Review result correlation text is incomplete", {})
    content_sha256 = sha256_bytes(data)
    suffix = sha256_bytes((review_id + source_message_id + content_sha256).encode("utf-8"))[:24]
    result_message_id = "PON-MESSAGE-REVIEW-RESULT-" + suffix
    bundle_id = "PON-BUNDLE-REVIEW-RESULT-" + suffix
    attempt_id = "PON-TRANSPORT-REVIEW-RESULT-" + suffix
    dispatch_id = "PON-DISPATCH-REVIEW-RETURN-" + suffix
    con = _open_writer(database_path, plugin_root)
    try:
        capability, actor = _courier(con, credential_path)
        review = con.execute("SELECT * FROM automatic_reviews WHERE review_id=?", (review_id,)).fetchone()
        if not review or review["state"] not in {"ACTIVE", "RETURNED"}:
            raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Review is not active or already returned", {})
        if review["reviewer_thread_id"] != source_thread_id:
            raise PostOfficeError("PON_OBSERVATION_MISMATCH", "Review result source thread differs from its reviewer binding", {})
        retained = con.execute("SELECT * FROM semantic_messages WHERE message_id=?", (result_message_id,)).fetchone()
        if retained:
            if review["result_message_id"] != result_message_id or review["wake_dispatch_id"] != dispatch_id:
                raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Retained review-result identity is inconsistent", {})
            con.rollback()
            return {"ok": True, "created": False, "reviewId": review_id,
                    "resultMessageId": result_message_id, "wakeDispatchId": dispatch_id,
                    "resultSha256": content_sha256, "resultSizeBytes": len(data)}
        request_message = con.execute(
            "SELECT * FROM semantic_messages WHERE message_id=?", (review["semantic_message_id"],)
        ).fetchone()
        binding = con.execute(
            "SELECT * FROM task_endpoint_bindings WHERE task_id=?", (review["requester_task_id"],)
        ).fetchone()
        mailbox = con.execute(
            "SELECT * FROM mailboxes WHERE mailbox_id=? AND generation=? AND status='ACTIVE'",
            (binding["mailbox_id"], binding["mailbox_generation"]),
        ).fetchone() if binding else None
        endpoint = con.execute(
            "SELECT * FROM endpoints WHERE endpoint_id=? AND status='ACTIVE'", (binding["endpoint_id"],)
        ).fetchone() if binding else None
        if not request_message or not mailbox or not endpoint:
            raise PostOfficeError("PON_INPUT_INVALID", "Requester mailbox binding is unavailable", {})
        relative = f"objects/{content_sha256[:2]}/{content_sha256}"
        target = database_path.parent / Path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if sha256_bytes(target.read_bytes()) != content_sha256:
                raise PostOfficeError("PON_CUSTODY_NOT_VERIFIED", "Retained CAS object differs from its digest", {})
        else:
            temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
            temporary.write_bytes(data)
            if sha256_bytes(temporary.read_bytes()) != content_sha256:
                temporary.unlink(missing_ok=True)
                raise PostOfficeError("PON_CUSTODY_NOT_VERIFIED", "Review result CAS verification failed", {})
            os.replace(temporary, target)
        now = _timestamp()
        storage_id = "PON-STORAGE-" + content_sha256[:40]
        con.execute(
            "INSERT OR IGNORE INTO storage_copies VALUES(?,?,?,?,?,?,?,?)",
            (storage_id, content_sha256, "LOCAL_CAS", relative, len(data), "AUTHORITATIVE", "SHA256_READBACK", now),
        )
        result_content = {
            "reviewId": review_id, "activationDispatchId": activation_dispatch_id,
            "sourceThreadId": source_thread_id, "sourceMessageId": source_message_id,
            "verdict": verdict, "sha256": content_sha256, "sizeBytes": len(data),
        }
        authority_grant_id = request_message["authority_grant_id"]
        exact_author_action_id = None if authority_grant_id else request_message["exact_author_action_id"]
        con.execute(
            """INSERT INTO semantic_messages(message_id,project_id,mail_domain,message_type,sender_endpoint_id,
               recipient_mailbox_id,recipient_generation,semantic_cycle_id,authority_grant_id,exact_author_action_id,
               requested_action,completion_criteria,content_root,state,aggregate_version,aggregate_root,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'CUSTODY_RECORDED',1,?,?)""",
            (result_message_id, request_message["project_id"], mailbox["mail_domain"], "RESPONSE",
             review["reviewer_endpoint_id"], mailbox["mailbox_id"], int(mailbox["generation"]),
             request_message["semantic_cycle_id"], authority_grant_id, exact_author_action_id,
             "Evaluate the retained automatic-review result", "Record evaluation and continue only under existing authority",
             sha256_json(result_content), "0" * 64, now),
        )
        con.execute("INSERT INTO message_relations VALUES(?,?,?)", (result_message_id, review["semantic_message_id"], "RESPONSE_TO"))
        manifest_root = sha256_json([{"ordinal": 0, "sha256": content_sha256, "sizeBytes": len(data)}])
        con.execute(
            "INSERT INTO message_bundles VALUES(?,?,?,?,?,?,?,?,?,?)",
            (bundle_id, result_message_id, result_path.name, 1, len(data), content_sha256,
             manifest_root, "REGISTERED", None, now),
        )
        con.execute("INSERT INTO bundle_payloads VALUES(?,?,?,?,?)", (bundle_id, 0, "payloads/0000-" + content_sha256, len(data), content_sha256))
        con.execute(
            "INSERT INTO transport_attempts VALUES(?,?,?,?,?,'PENDING',1,NULL,?,?)",
            (attempt_id, result_message_id, bundle_id, review["reviewer_endpoint_id"], mailbox["mailbox_id"], now, now),
        )
        marker = f"POST OFFICE DELIVERY {dispatch_id} {result_message_id}"
        con.execute(
            """INSERT INTO transport_dispatches(dispatch_id,transport_attempt_id,channel,destination_endpoint_id,
               destination_mailbox_id,destination_generation,state,observable_marker,attempt_count,next_attempt_at,
               created_at,updated_at) VALUES(?,?,?,?,?,?,'READY',?,0,?,?,?)""",
            (dispatch_id, attempt_id, "NATIVE_TASK", endpoint["endpoint_id"], mailbox["mailbox_id"],
             int(mailbox["generation"]), marker, now, now, now),
        )
        result_row = con.execute("SELECT * FROM semantic_messages WHERE message_id=?", (result_message_id,)).fetchone()
        after_message_root = _aggregate_root("SemanticMessage", result_message_id, _message_state(con, result_row))
        con.execute("UPDATE semantic_messages SET aggregate_root=? WHERE message_id=?", (after_message_root, result_message_id))
        message_request = _runtime_request(
            actor=actor, capability=capability, message=request_message,
            operation="runtime.review.result.ingest", aggregate_type="SemanticMessage",
            aggregate_id=result_message_id, parameters=result_content,
        )
        message_event = append_hub_event(
            con, request=message_request, capability_id=capability["capability_id"], aggregate_version=1,
            before_root=_aggregate_root("SemanticMessage", result_message_id, None), after_root=after_message_root,
            event_result={"state": "CUSTODY_RECORDED", "bundleId": bundle_id, "wakeDispatchId": dispatch_id},
            occurred_at=now,
        )
        con.execute("UPDATE semantic_messages SET registered_event_id=? WHERE message_id=?", (message_event["eventId"], result_message_id))
        before_review = _review_state(review)
        before_review_root = _aggregate_root("AutomaticReview", review_id, before_review)
        review_position = _aggregate_position(
            con, aggregate_type="AutomaticReview", aggregate_id=review_id,
            current_root=before_review_root,
        )
        con.execute(
            "UPDATE automatic_reviews SET state='RETURNED',returned_at=?,result_message_id=?,wake_dispatch_id=? WHERE review_id=?",
            (now, result_message_id, dispatch_id, review_id),
        )
        after_review_row = con.execute("SELECT * FROM automatic_reviews WHERE review_id=?", (review_id,)).fetchone()
        review_request = _runtime_request(
            actor=actor, capability=capability, message=request_message,
            operation="runtime.review.return", aggregate_type="AutomaticReview", aggregate_id=review_id,
            parameters={"resultMessageId": result_message_id, "wakeDispatchId": dispatch_id},
        )
        review_event = append_hub_event(
            con, request=review_request, capability_id=capability["capability_id"], aggregate_version=review_position + 1,
            before_root=before_review_root,
            after_root=_aggregate_root("AutomaticReview", review_id, _review_state(after_review_row)),
            event_result={"state": "RETURNED", "wakeDispatchId": dispatch_id}, occurred_at=now,
        )
        con.commit()
        return {"ok": True, "created": True, "reviewId": review_id,
                "resultMessageId": result_message_id, "bundleId": bundle_id,
                "wakeDispatchId": dispatch_id, "resultSha256": content_sha256,
                "resultSizeBytes": len(data), "messageEventId": message_event["eventId"],
                "reviewEventId": review_event["eventId"], "heartbeatRequired": False}
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
        if review["state"] == "ACTIVE" and con.execute(
            "SELECT 1 FROM automatic_review_activations WHERE review_id=?", (review_id,)
        ).fetchone():
            raise PostOfficeError(
                "PON_CONCURRENCY_CONFLICT",
                "A package-bearing review activation has started and cannot be withdrawn",
                {},
            )
        message = con.execute("SELECT * FROM semantic_messages WHERE message_id=?", (review["semantic_message_id"],)).fetchone()
        now = _timestamp()
        before = _review_state(review)
        before_root = _aggregate_root("AutomaticReview", review_id, before)
        position = _aggregate_position(
            con, aggregate_type="AutomaticReview", aggregate_id=review_id,
            current_root=before_root,
        )
        con.execute("UPDATE automatic_reviews SET state='WITHDRAWN' WHERE review_id=?", (review_id,))
        updated_review = con.execute("SELECT * FROM automatic_reviews WHERE review_id=?", (review_id,)).fetchone()
        after = _review_state(updated_review)
        request = _runtime_request(actor=actor, capability=capability, message=message, operation="runtime.review.withdraw", aggregate_type="AutomaticReview", aggregate_id=review_id, parameters={"reason": reason})
        event = append_hub_event(con, request=request, capability_id=capability["capability_id"], aggregate_version=position + 1, before_root=before_root, after_root=_aggregate_root("AutomaticReview", review_id, after), event_result={"state": "WITHDRAWN", "reason": reason}, occurred_at=now)
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
            result_message = con.execute(
                "SELECT * FROM semantic_messages WHERE message_id=?", (review["result_message_id"],)
            ).fetchone()
            result_before_root = _aggregate_root(
                "SemanticMessage", result_message["message_id"], _message_state(con, result_message)
            )
            result_version = _aggregate_position(
                con,
                aggregate_type="SemanticMessage",
                aggregate_id=result_message["message_id"],
                current_root=result_before_root,
            )
            changed = con.execute(
                "UPDATE semantic_messages SET state='ACKNOWLEDGED' WHERE message_id=? AND state='DELIVERED'",
                (review["result_message_id"],),
            ).rowcount
        updated = con.execute("SELECT * FROM automatic_reviews WHERE review_id=?", (review_id,)).fetchone()
        after = _review_state(updated)
        request = _runtime_request(actor=actor, capability=capability, message=message, operation="runtime.review.complete", aggregate_type="AutomaticReview", aggregate_id=review_id, parameters={"summary": summary})
        position = _aggregate_position(con, aggregate_type="AutomaticReview", aggregate_id=review_id, current_root=_aggregate_root("AutomaticReview", review_id, before))
        event = append_hub_event(con, request=request, capability_id=capability["capability_id"], aggregate_version=position + 1, before_root=_aggregate_root("AutomaticReview", review_id, before), after_root=_aggregate_root("AutomaticReview", review_id, after), event_result={"state": "EVALUATED", "summary": summary}, occurred_at=now)
        message_event_id = None
        if review["result_message_id"] and changed:
            result_updated = con.execute(
                "SELECT * FROM semantic_messages WHERE message_id=?", (review["result_message_id"],)
            ).fetchone()
            result_after_root = _aggregate_root(
                "SemanticMessage", result_updated["message_id"], _message_state(con, result_updated)
            )
            con.execute(
                "UPDATE semantic_messages SET aggregate_version=?,aggregate_root=? WHERE message_id=?",
                (result_version + 1, result_after_root.removeprefix("sha256:"), result_updated["message_id"]),
            )
            message_request = _runtime_request(
                actor=actor,
                capability=capability,
                message=result_message,
                operation="runtime.review.complete",
                aggregate_type="SemanticMessage",
                aggregate_id=result_updated["message_id"],
                parameters={"reviewId": review_id, "summary": summary},
            )
            message_event = append_hub_event(
                con,
                request=message_request,
                capability_id=capability["capability_id"],
                aggregate_version=result_version + 1,
                before_root=result_before_root,
                after_root=result_after_root,
                event_result={"state": "ACKNOWLEDGED", "reviewEventId": event["eventId"]},
                occurred_at=now,
            )
            message_event_id = message_event["eventId"]
        con.commit()
        return {"ok": True, **after, "completed": True, "summary": summary,
                "eventId": event["eventId"], "messageEventId": message_event_id}
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


def _continuation_observation_matches_lease(
    con: sqlite3.Connection,
    item: sqlite3.Row,
    observation: Any,
    reconciled_at: str,
) -> bool:
    """Require evidence to name the exact expired lease attempt it resolves."""
    if not isinstance(observation, dict):
        return False
    binding = observation.get("leaseBinding")
    if not isinstance(binding, dict):
        return False
    receipt_id = binding.get("leaseReceiptId")
    if not isinstance(receipt_id, str) or not receipt_id:
        return False
    receipt = con.execute(
        """SELECT evidence_json FROM continuation_receipts
           WHERE receipt_id=? AND continuation_id=? AND receipt_kind='LEASED'""",
        (receipt_id, item["continuation_id"]),
    ).fetchone()
    if not receipt:
        return False
    try:
        receipt_evidence = json.loads(receipt["evidence_json"])
        expected = {
            "continuationId": str(item["continuation_id"]),
            "attemptCount": int(item["attempt_count"]),
            "leaseOwnerActorId": str(item["lease_owner_actor_id"]),
            "leaseTokenSha256": str(item["lease_token_sha256"]),
            "evidenceRoot": str(item["evidence_root"]),
        }
        if any(binding.get(key) != value for key, value in expected.items()):
            return False
        if any(receipt_evidence.get(key) != value for key, value in expected.items()):
            return False
        observed_at = observation.get("observedAt")
        if not isinstance(observed_at, str) or not observed_at:
            return False
        observed = _parse_timestamp(observed_at)
        return (
            observed >= _parse_timestamp(str(item["lease_expires_at"]))
            and observed <= _parse_timestamp(reconciled_at)
        )
    except (PostOfficeError, TypeError, ValueError, json.JSONDecodeError):
        return False


def reconcile_continuations(
    database_path: Path, credential_path: Path, plugin_root: Path,
    *, observations: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Recover expired continuation leases only from exact external evidence."""
    observations = observations or {}
    con = _open_writer(database_path, plugin_root)
    try:
        _courier(con, credential_path)
        now = _timestamp()
        expired = list(con.execute(
            "SELECT * FROM continuation_items WHERE state='LEASED' AND lease_expires_at<=? ORDER BY lease_expires_at,continuation_id",
            (now,),
        ))
        recovered: list[str] = []
        completed: list[str] = []
        attention_ids: list[str] = []
        for item in expired:
            observation = observations.get(str(item["continuation_id"]))
            observation_matches = _continuation_observation_matches_lease(
                con, item, observation, now
            )
            performed_receipt = observation.get("performedReceiptId") if isinstance(observation, dict) else None
            if observation_matches and isinstance(performed_receipt, str) and performed_receipt:
                con.execute(
                    """UPDATE continuation_items SET state='COMPLETED',lease_owner_actor_id=NULL,
                       lease_token_sha256=NULL,lease_expires_at=NULL,last_error_code=NULL,
                       updated_at=?,completed_at=? WHERE continuation_id=?""",
                    (now, now, item["continuation_id"]),
                )
                _continuation_receipt(
                    con,
                    item["continuation_id"],
                    "COMPLETED",
                    {"outcome": "RECOVERED_PERFORMED", **observation},
                    now,
                )
                completed.append(str(item["continuation_id"]))
            elif observation_matches and observation.get("actionAbsent") is True:
                con.execute(
                    """UPDATE continuation_items SET state='READY',lease_owner_actor_id=NULL,
                       lease_token_sha256=NULL,lease_expires_at=NULL,last_error_code=NULL,updated_at=?
                       WHERE continuation_id=?""",
                    (now, item["continuation_id"]),
                )
                _continuation_receipt(
                    con,
                    item["continuation_id"],
                    "RECOVERED",
                    {"outcome": "LEASE_EXPIRED_PROVEN_ABSENT_SAFE_REQUEUE", **observation},
                    now,
                )
                recovered.append(str(item["continuation_id"]))
            else:
                con.execute(
                    "UPDATE continuation_items SET last_error_code='PON_OBSERVATION_AMBIGUOUS',updated_at=? WHERE continuation_id=?",
                    (now, item["continuation_id"]),
                )
                attention_ids.append(_attention(
                    con,
                    entity_type="Continuation",
                    entity_id=item["continuation_id"],
                    severity="ERROR",
                    reason_code="PON_OBSERVATION_AMBIGUOUS",
                    details={
                        "leaseExpiredAt": item["lease_expires_at"],
                        "attemptCount": int(item["attempt_count"]),
                        "observationPresent": observation is not None,
                        "leaseBindingMatched": observation_matches,
                    },
                    recorded_at=now,
                ))
        con.commit()
        return {"ok": True, "recoveredContinuationIds": recovered,
                "completedContinuationIds": completed, "attentionIds": attention_ids,
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
        token_sha256 = sha256_bytes(token.encode("utf-8"))
        attempt_count = int(item["attempt_count"]) + 1
        con.execute(
            """UPDATE continuation_items SET state='LEASED',lease_owner_actor_id=?,
               lease_token_sha256=?,lease_expires_at=?,attempt_count=attempt_count+1,updated_at=?
               WHERE continuation_id=? AND state='READY'""",
            (actor["actor_id"], token_sha256, expires, now, item["continuation_id"]),
        )
        lease_evidence = {
            "continuationId": str(item["continuation_id"]),
            "attemptCount": attempt_count,
            "leaseOwnerActorId": str(actor["actor_id"]),
            "leaseTokenSha256": token_sha256,
            "leaseExpiresAt": expires,
            "evidenceRoot": str(item["evidence_root"]),
        }
        receipt_id = _continuation_receipt(
            con, item["continuation_id"], "LEASED", lease_evidence, now
        )
        con.commit()
        payload = json.loads(item["payload_json"])
        destination = _continuation_destination(con, payload)
        return {"ok": True, "available": True, "continuationId": item["continuation_id"],
                "continuationKind": item["continuation_kind"], "sourceTable": item["source_table"],
                "sourceId": item["source_id"], "payload": payload, **destination,
                "evidenceRoot": item["evidence_root"], "leaseToken": token,
                "leaseExpiresAt": expires, "receiptId": receipt_id,
                "observationBinding": {**lease_evidence, "leaseReceiptId": receipt_id}}
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
