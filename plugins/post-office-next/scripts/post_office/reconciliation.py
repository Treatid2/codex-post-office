# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import re
from typing import Any

from .canonical import read_json, require_new_output_file, sha256_json, write_json
from .contract_specs import build_entity_schemas
from .diagnostics import PostOfficeError
from .mini_schema import ValidationFailure, validate
from .snapshot import validated_external_evidence_kinds


def _evidence(kind: str, source_id: str, row_hash: str | None = None) -> dict[str, Any]:
    result = {"kind": kind, "reference": source_id}
    if row_hash:
        result["sha256"] = row_hash
    return result


def _mapping_evidence(cycle: dict[str, Any] | None, assertions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in (cycle or {}).get("evidence", []):
        if isinstance(raw, dict):
            reference = ":".join(str(raw.get(key) or "unknown") for key in ("database", "table"))
            result.append(_evidence("HUB_ROW", reference, raw.get("rowHash")))
    for assertion in assertions:
        reference = str(assertion.get("assertionId") or assertion.get("id") or "sha256:" + sha256_json(assertion))
        result.append(_evidence("RETAINED_ASSERTION", reference, sha256_json(assertion)))
    return result


def _exact_semantic_cycle_id(assertions: list[dict[str, Any]]) -> tuple[str | None, bool]:
    exact = {
        str(item.get("value"))
        for item in assertions
        if item.get("fact") == "semanticCycleId"
        and item.get("authorityKind") == "EXACT_AUTHOR_ACTION"
        and item.get("verified") is True
        and item.get("value")
    }
    return (next(iter(exact)), False) if len(exact) == 1 else (None, len(exact) > 1)


def reconcile_preview(snapshot_path: Path, assertions_path: Path | None, output: Path) -> dict[str, Any]:
    inputs = [snapshot_path, *([assertions_path] if assertions_path else [])]
    output = require_new_output_file(output, inputs=inputs)
    snapshot = read_json(snapshot_path)
    if snapshot.get("snapshotRoot") != sha256_json({key: value for key, value in snapshot.items() if key != "snapshotRoot"}):
        raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Snapshot root does not match snapshot content", {"path": str(snapshot_path)})
    assertions_doc = read_json(assertions_path) if assertions_path else {"schemaVersion": "1", "assertions": []}
    assertions = assertions_doc.get("assertions", [])
    discrepancies: list[dict[str, Any]] = []

    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for assertion in assertions:
        grouped[(assertion["entityType"], assertion["entityId"], assertion["fact"])].append(assertion)
    for key, values in sorted(grouped.items()):
        distinct = {str(item["value"]) for item in values}
        if len(distinct) <= 1:
            continue
        authoritative = [item for item in values if item.get("authorityKind") == "EXACT_AUTHOR_ACTION" and item.get("verified") is True]
        classification = "STALE_PROJECTION" if len(authoritative) == 1 else "AMBIGUOUS_AUTHORITY"
        discrepancies.append({
            "id": f"PON-DISCREPANCY-{len(discrepancies)+1:06d}",
            "classification": classification,
            "entityType": key[0], "entityId": key[1], "fact": key[2],
            "assertions": values,
            "proposedAuthority": authoritative[0] if len(authoritative) == 1 else None,
            "recommendedAction": "REVIEW_EXACT_AUTHOR_EVIDENCE" if classification == "AMBIGUOUS_AUTHORITY" else "PREVIEW_PROJECTION_REPAIR_ONLY",
            "automaticRepairEligible": False,
            "mayReopenSemanticCycle": False,
        })

    for outbox in snapshot["hub"]["browserOutboxes"]:
        expected = outbox.get("expectedSha256")
        delivered = outbox.get("deliveredSha256")
        if expected and delivered and expected.lower() != delivered.lower():
            discrepancies.append({
                "id": f"PON-DISCREPANCY-{len(discrepancies)+1:06d}", "classification": "HASH_MISMATCH",
                "entityType": "TransportAttempt", "entityId": outbox["outboxId"], "fact": "deliveredSha256",
                "evidence": [_evidence("HUB_ROW", outbox["outboxId"], outbox["evidence"]["rowHash"])],
                "recommendedAction": "QUARANTINE_REVIEW", "automaticRepairEligible": False, "mayReopenSemanticCycle": False,
            })
    for delivery in snapshot["hub"]["browserBridgeReceipts"]:
        expected = delivery.get("sha256")
        actual = delivery.get("driveSha256")
        if expected and actual and expected.lower() != actual.lower():
            discrepancies.append({
                "id": f"PON-DISCREPANCY-{len(discrepancies)+1:06d}", "classification": "HASH_MISMATCH",
                "entityType": "StorageCopy", "entityId": delivery["deliveryId"], "fact": "driveSha256",
                "evidence": [_evidence("HUB_ROW", delivery["deliveryId"], delivery["evidence"]["rowHash"])],
                "recommendedAction": "QUARANTINE_REVIEW", "automaticRepairEligible": False, "mayReopenSemanticCycle": False,
            })

    message_ids = {item["messageId"] for item in snapshot["hub"]["messages"]}
    delivery_message_ids = {
        item["messageId"] for item in snapshot["hub"]["browserBridgeReceipts"] if item.get("messageId")
    }
    for delivery in snapshot["hub"]["browserBridgeReceipts"]:
        if delivery.get("messageId") and delivery["messageId"] not in message_ids:
            discrepancies.append({
                "id": f"PON-DISCREPANCY-{len(discrepancies)+1:06d}", "classification": "UNREGISTERED_DELIVERED",
                "entityType": "BrowserBridgeTransfer", "entityId": delivery["deliveryId"], "fact": "messageId",
                "evidence": [_evidence("HUB_ROW", delivery["deliveryId"], delivery["evidence"]["rowHash"])],
                "recommendedAction": "QUARANTINE_AND_IDENTIFY_SEMANTIC_MESSAGE", "automaticRepairEligible": False,
                "mayReopenSemanticCycle": False,
            })
    for message in snapshot["hub"]["messages"]:
        if message.get("status") in {"REGISTERED", "PENDING_DRIVE_DELIVERY"} and message["messageId"] not in delivery_message_ids:
            discrepancies.append({
                "id": f"PON-DISCREPANCY-{len(discrepancies)+1:06d}", "classification": "REGISTERED_UNDELIVERED",
                "entityType": "SemanticMessage", "entityId": message["messageId"], "fact": "transportReceipt",
                "evidence": [_evidence("HUB_ROW", message["messageId"], message["evidence"]["rowHash"])],
                "recommendedAction": "REVIEW_TRANSPORT_STATE_WITHOUT_CREATING_NEW_IDENTITY", "automaticRepairEligible": False,
                "mayReopenSemanticCycle": False,
            })

    mailbox_domains = {item["mailboxId"]: item.get("projectCode") for item in snapshot["hub"]["mailboxes"]}
    for message in snapshot["hub"]["messages"]:
        claimed = message.get("projectCode")
        endpoint_domains = {
            value for value in (
                mailbox_domains.get(message.get("senderMailboxId")),
                mailbox_domains.get(message.get("recipientMailboxId")),
            ) if value
        }
        if claimed and endpoint_domains and endpoint_domains != {claimed}:
            discrepancies.append({
                "id": f"PON-DISCREPANCY-{len(discrepancies)+1:06d}", "classification": "CROSS_DOMAIN_PLACEMENT",
                "entityType": "SemanticMessage", "entityId": message["messageId"], "fact": "projectCode",
                "claimedDomain": claimed, "mailboxDomains": sorted(endpoint_domains),
                "evidence": [_evidence("HUB_ROW", message["messageId"], message["evidence"]["rowHash"])],
                "recommendedAction": "REVIEW_ADDRESS_AND_AUTHORITY", "automaticRepairEligible": False,
                "mayReopenSemanticCycle": False,
            })

    migration_evidence = snapshot.get("migrationEvidence", {"records": []})
    records = migration_evidence.get("records", [])
    represented_kinds = validated_external_evidence_kinds(migration_evidence)
    for required_kind in ("PROJECT_REGISTER", "LOCAL_ARCHIVE"):
        if required_kind not in represented_kinds:
            discrepancies.append({
                "id": f"PON-DISCREPANCY-{len(discrepancies)+1:06d}", "classification": "MIGRATION_EVIDENCE_GAP",
                "entityType": "MigrationEvidence", "entityId": required_kind, "fact": "coverage",
                "evidence": [], "recommendedAction": "SUPPLY_EXACT_LOCAL_EVIDENCE_MANIFEST",
                "automaticRepairEligible": False, "mayReopenSemanticCycle": False,
            })
    evidence_identities: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        evidence_identities[(str(record.get("kind")), str(record.get("logicalId")))].append(record)
    for (kind, logical_id), matches in sorted(evidence_identities.items()):
        fingerprints = {(item.get("sha256"), item.get("reference")) for item in matches}
        if len(fingerprints) > 1:
            discrepancies.append({
                "id": f"PON-DISCREPANCY-{len(discrepancies)+1:06d}", "classification": "IDENTITY_COLLISION",
                "entityType": "MigrationEvidence", "entityId": logical_id, "fact": kind,
                "evidence": matches, "recommendedAction": "REVIEW_EXACT_IDENTITY_AND_CUSTODY",
                "automaticRepairEligible": False, "mayReopenSemanticCycle": False,
            })
    known_external_targets = {
        "Project": {item["legacyProjectCode"] for item in snapshot["hub"]["projects"]},
        "SemanticMessage": message_ids,
        "Mailbox": {item["mailboxId"] for item in snapshot["hub"]["mailboxes"]},
        "SemanticCycle": {item["legacyCycleId"] for item in snapshot["hub"]["semanticCycles"]},
    }
    for record in records:
        entity_type = record.get("relatedEntityType")
        entity_id = record.get("relatedEntityId")
        if entity_type and entity_id and entity_type in known_external_targets and entity_id not in known_external_targets[entity_type]:
            discrepancies.append({
                "id": f"PON-DISCREPANCY-{len(discrepancies)+1:06d}", "classification": "ORPHAN_EXTERNAL_RESOURCE",
                "entityType": entity_type, "entityId": entity_id, "fact": "migrationEvidenceReference",
                "evidence": [record], "recommendedAction": "REVIEW_OR_QUARANTINE_EXTERNAL_EVIDENCE",
                "automaticRepairEligible": False, "mayReopenSemanticCycle": False,
            })

    cycle_pattern = re.compile(r"\b[A-Z][A-Z0-9]*-CYCLE-\d+\b")
    for message in snapshot["hub"]["messages"]:
        structured = {str(value) for value in (message.get("cycleId"), message.get("rootCycleId")) if value}
        labelled = set(cycle_pattern.findall(str(message.get("subject") or "")))
        if labelled and not labelled.issubset(structured):
            discrepancies.append({
                "id": f"PON-DISCREPANCY-{len(discrepancies)+1:06d}", "classification": "CYCLE_LABEL_MISMATCH_CANDIDATE",
                "entityType": "SemanticMessage", "entityId": message["messageId"], "fact": "cycleReference",
                "structuredCycleIds": sorted(structured), "subjectCycleIds": sorted(labelled),
                "evidence": [_evidence("HUB_ROW", message["messageId"], message["evidence"]["rowHash"])],
                "recommendedAction": "REVIEW_SEMANTIC_TRANSPORT_MAPPING", "automaticRepairEligible": False,
                "mayReopenSemanticCycle": False,
            })

    by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in snapshot["filesystem"]["files"]:
        by_hash[item["sha256"]].append(item)
    for content_hash, files in sorted(by_hash.items()):
        if len(files) > 1:
            discrepancies.append({
                "id": f"PON-DISCREPANCY-{len(discrepancies)+1:06d}", "classification": "DUPLICATE_CONTENT_CANDIDATE",
                "entityType": "StorageCopy", "entityId": "sha256:" + content_hash, "fact": "locations",
                "evidence": [_evidence("CAPTURED_FILE", item["path"], item["sha256"]) for item in files],
                "recommendedAction": "REVIEW_ALIAS_OR_TOMBSTONE_ONLY", "automaticRepairEligible": False, "mayReopenSemanticCycle": False,
            })

    cycle_assertions = defaultdict(list)
    for assertion in assertions:
        if assertion["entityType"] == "SemanticCycle":
            cycle_assertions[assertion["entityId"]].append(assertion)
    mappings = []
    for cycle in snapshot["hub"]["semanticCycles"]:
        relevant = cycle_assertions.get(cycle["legacyCycleId"], [])
        values = {str(item["value"]) for item in relevant if item.get("fact") == "state"}
        semantic_id, conflicting_exact_ids = _exact_semantic_cycle_id(relevant)
        mapping_class = "EXACT" if semantic_id else ("AMBIGUOUS" if len(values) > 1 or conflicting_exact_ids else "TRANSPORT_ONLY")
        mapping = {
            "schemaVersion": "1", "id": f"PON-LEGACY-MAPPING-{len(mappings)+1:06d}",
            "legacyCycleId": cycle["legacyCycleId"],
            "transportMessageIds": cycle["messageIds"], "classification": mapping_class,
            "decision": "PRESERVE" if mapping_class == "EXACT" else "REVIEW_REQUIRED",
            "evidence": _mapping_evidence(cycle, relevant),
        }
        if semantic_id:
            mapping["semanticCycleId"] = semantic_id
        mappings.append(mapping)
    known_cycle_ids = {item["legacyCycleId"] for item in mappings}
    for cycle_id, relevant in sorted(cycle_assertions.items()):
        if cycle_id not in known_cycle_ids:
            values = {str(item["value"]) for item in relevant if item.get("fact") == "state"}
            semantic_id, conflicting_exact_ids = _exact_semantic_cycle_id(relevant)
            mapping_class = "EXACT" if semantic_id else ("AMBIGUOUS" if len(values) > 1 or conflicting_exact_ids else "TRANSPORT_ONLY")
            mapping = {
                "schemaVersion": "1", "id": f"PON-LEGACY-MAPPING-{len(mappings)+1:06d}",
                "legacyCycleId": cycle_id, "transportMessageIds": [], "classification": mapping_class,
                "decision": "PRESERVE" if mapping_class == "EXACT" else "REVIEW_REQUIRED",
                "evidence": _mapping_evidence(None, relevant),
            }
            if semantic_id:
                mapping["semanticCycleId"] = semantic_id
            mappings.append(mapping)

    mapping_schema = build_entity_schemas()["legacy-cycle-mapping"]
    try:
        for mapping in mappings:
            validate(mapping, mapping_schema)
    except ValidationFailure as exc:
        raise PostOfficeError("PON_CONTRACT_INVALID", "Reconciliation mapping violates its contract", {"error": str(exc)}) from exc

    identity = {
        "schemaVersion": "1",
        "snapshotRoot": snapshot["snapshotRoot"],
        "assertionSetRoot": sha256_json(assertions_doc),
        "mode": "READ_ONLY_PREVIEW",
        "applyAvailable": False,
        "discrepancies": discrepancies,
        "legacyCycleMappings": mappings,
        "invariants": {
            "inferredAuthorityForbidden": True,
            "semanticCycleReopenForbidden": True,
            "transportRetryCreatesSemanticIdentity": False,
            "repairsPerformed": 0,
        },
        "classificationCoverage": {
            "active": [
                "AMBIGUOUS_AUTHORITY", "CROSS_DOMAIN_PLACEMENT", "CYCLE_LABEL_MISMATCH_CANDIDATE",
                "DUPLICATE_CONTENT_CANDIDATE", "HASH_MISMATCH", "IDENTITY_COLLISION",
                "MIGRATION_EVIDENCE_GAP", "ORPHAN_EXTERNAL_RESOURCE", "REGISTERED_UNDELIVERED",
                "STALE_PROJECTION", "UNREGISTERED_DELIVERED",
            ],
            "deferredUntilVNextEventReplay": ["ILLEGAL_STATE_TRANSITION", "MISSING_PROJECTION"],
        },
    }
    preview = {**identity, "planRoot": sha256_json(identity)}
    write_json(output, preview)
    return {"ok": True, "planRoot": preview["planRoot"], "snapshotRoot": snapshot["snapshotRoot"],
            "discrepancyCount": len(discrepancies), "cycleMappingCount": len(mappings), "applyAvailable": False,
            "output": str(output.resolve())}
