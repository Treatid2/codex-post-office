# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .diagnostics import DIAGNOSTICS


DRAFT = "http://json-schema.org/draft-07/schema#"
SCHEMA_PREFIX = "https://local.codex/post-office-next/contracts/v1"
ID_PATTERN = r"^[A-Za-z][A-Za-z0-9._:-]{2,127}$"
SHA256_PATTERN = r"^(?:sha256:)?[0-9a-f]{64}$"
DATETIME_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$"
SAFE_RELATIVE_PATH_PATTERN = r"^(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$"
SAFE_FILENAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$"


def string(pattern: str | None = None, enum: list[str] | None = None, minimum: int = 1) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "string", "minLength": minimum}
    if pattern:
        result["pattern"] = pattern
    if enum:
        result["enum"] = enum
    return result


def identifier() -> dict[str, Any]:
    return string(ID_PATTERN)


def sha256() -> dict[str, Any]:
    return string(SHA256_PATTERN)


def timestamp() -> dict[str, Any]:
    return string(DATETIME_PATTERN)


def safe_relative_path() -> dict[str, Any]:
    return string(SAFE_RELATIVE_PATH_PATTERN)


def safe_filename() -> dict[str, Any]:
    return string(SAFE_FILENAME_PATTERN)


def array(items: dict[str, Any], minimum: int = 0, maximum: int | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"type": "array", "items": items, "minItems": minimum}
    if maximum is not None:
        result["maxItems"] = maximum
    return result


def obj(
    properties: dict[str, Any],
    required: list[str] | tuple[str, ...] = (),
    additional: bool = False,
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": additional,
    }


def entity_schema(
    name: str,
    properties: dict[str, Any],
    required: list[str],
    example: dict[str, Any],
    one_of: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "$schema": DRAFT,
        "$id": f"{SCHEMA_PREFIX}/entities/{name}.schema.json",
        "title": name,
        "type": "object",
        "additionalProperties": False,
        "properties": {"schemaVersion": {"const": "1"}, **properties},
        "required": ["schemaVersion", *required],
        "examples": [example],
    }
    if one_of:
        schema["oneOf"] = one_of
    return schema


def build_entity_schemas() -> dict[str, dict[str, Any]]:
    roots = obj(
        {
            "localProject": string(),
            "localCas": string(),
            "localBackup": string(),
        },
        ["localProject", "localCas", "localBackup"],
    )
    scope = obj(
        {
            "projectIds": array(identifier()),
            "taskIds": array(identifier()),
            "packageIds": array(identifier()),
            "mailDomains": array(identifier()),
        }
    )
    evidence = array(obj({"kind": string(), "reference": string(), "sha256": sha256()}, ["kind", "reference"] ))
    schemas: dict[str, dict[str, Any]] = {}
    schemas["endpoint"] = entity_schema(
        "endpoint",
        {"id": identifier(), "role": string(), "projectId": identifier(), "taskId": identifier(),
         "accessScope": scope, "status": string(enum=["PROVISIONAL", "ACTIVE", "REVOKED", "RETIRED"]),
         "mailboxId": identifier(), "startupRoot": sha256(), "createdEventId": identifier()},
        ["id", "role", "projectId", "accessScope", "status", "mailboxId", "startupRoot", "createdEventId"],
        {"schemaVersion": "1", "id": "PON-ENDPOINT-001", "role": "worker", "projectId": "PON-PROJECT-001",
         "accessScope": {"projectIds": ["PON-PROJECT-001"]}, "status": "ACTIVE", "mailboxId": "PON-MAILBOX-001",
         "startupRoot": "sha256:" + "1" * 64, "createdEventId": "PON-EVENT-001"},
    )
    schemas["mailbox"] = entity_schema(
        "mailbox",
        {"id": identifier(), "generation": {"type": "integer", "minimum": 1}, "domain": identifier(),
         "endpointId": identifier(), "status": string(enum=["PROVISIONAL", "ACTIVE", "RETIRED", "REVOKED"]),
         "createdEventId": identifier()},
        ["id", "generation", "domain", "endpointId", "status", "createdEventId"],
        {"schemaVersion": "1", "id": "PON-MAILBOX-001", "generation": 1, "domain": "PON",
         "endpointId": "PON-ENDPOINT-001", "status": "ACTIVE", "createdEventId": "PON-EVENT-001"},
    )
    schemas["semantic-message"] = entity_schema(
        "semantic-message",
        {"id": identifier(), "domain": identifier(), "messageType": string(), "senderEndpointId": identifier(),
         "recipientMailboxId": identifier(), "recipientGeneration": {"type": "integer", "minimum": 1},
         "relatedMessageIds": array(identifier()), "authorityBasisId": identifier(), "requestedAction": string(),
         "completionCriteria": string(), "contentRoot": sha256(), "semanticCycleId": identifier(),
         "state": string(enum=["DRAFT", "BUNDLED", "CUSTODY_RECORDED", "REGISTERED", "DELIVERED", "ACKNOWLEDGED", "RESPONSE_RETURNED", "REVIEWED", "CLOSED", "QUARANTINED", "SUPERSEDED_BEFORE_REGISTRATION", "CANCELLED_BEFORE_DELIVERY", "BLOCKED"])},
        ["id", "domain", "messageType", "senderEndpointId", "recipientMailboxId", "recipientGeneration",
         "relatedMessageIds", "authorityBasisId", "requestedAction", "completionCriteria", "contentRoot", "semanticCycleId", "state"],
        {"schemaVersion": "1", "id": "PON-MSG-001", "domain": "PON", "messageType": "HANDOFF",
         "senderEndpointId": "PON-ENDPOINT-001", "recipientMailboxId": "PON-MAILBOX-002", "recipientGeneration": 1,
         "relatedMessageIds": [], "authorityBasisId": "PON-GRANT-001", "requestedAction": "Run bounded work",
         "completionCriteria": "Return evidence", "contentRoot": "sha256:" + "2" * 64,
         "semanticCycleId": "PON-CYCLE-001", "state": "REGISTERED"},
    )
    schemas["message-bundle"] = entity_schema(
        "message-bundle",
        {"id": identifier(), "semanticMessageId": identifier(), "canonicalFilename": safe_filename(),
         "bundleVersion": {"type": "integer", "minimum": 1}, "sizeBytes": {"type": "integer", "minimum": 0},
         "sha256": sha256(), "manifestRoot": sha256(), "payloads": array(obj(
             {"path": safe_relative_path(), "sizeBytes": {"type": "integer", "minimum": 0}, "sha256": sha256()},
             ["path", "sizeBytes", "sha256"]), minimum=1), "custodyEvidence": evidence},
        ["id", "semanticMessageId", "canonicalFilename", "bundleVersion", "sizeBytes", "sha256", "manifestRoot", "payloads", "custodyEvidence"],
        {"schemaVersion": "1", "id": "PON-BUNDLE-001", "semanticMessageId": "PON-MSG-001",
         "canonicalFilename": "PON-MSG-001_v01.zip", "bundleVersion": 1, "sizeBytes": 10,
         "sha256": "sha256:" + "3" * 64, "manifestRoot": "sha256:" + "4" * 64,
         "payloads": [{"path": "message.json", "sizeBytes": 10, "sha256": "sha256:" + "5" * 64}],
         "custodyEvidence": []},
    )
    schemas["transport-attempt"] = entity_schema(
        "transport-attempt",
        {"id": identifier(), "semanticMessageId": identifier(), "bundleId": identifier(), "bundleSha256": sha256(),
         "source": string(), "destination": string(), "state": string(enum=["PENDING", "COLLECTED", "VALIDATED", "STORED", "DELIVERED", "RECEIPTED", "FAILED_RETRYABLE", "FAILED_FINAL", "QUARANTINED", "TOMBSTONED_DUPLICATE", "CANCELLED"]),
         "attemptNumber": {"type": "integer", "minimum": 1}, "receiptEvidence": evidence},
        ["id", "semanticMessageId", "bundleId", "bundleSha256", "source", "destination", "state", "attemptNumber", "receiptEvidence"],
        {"schemaVersion": "1", "id": "PON-TRANSPORT-001", "semanticMessageId": "PON-MSG-001",
         "bundleId": "PON-BUNDLE-001", "bundleSha256": "sha256:" + "3" * 64, "source": "local-cas",
         "destination": "mailbox:PON-MAILBOX-002:1", "state": "PENDING", "attemptNumber": 1, "receiptEvidence": []},
    )
    schemas["storage-copy"] = entity_schema(
        "storage-copy", {"id": identifier(), "contentSha256": sha256(), "locationKind": string(enum=["LOCAL_CAS", "LOCAL_BACKUP", "DATABASE_BLOB"]),
                         "location": safe_relative_path(), "sizeBytes": {"type": "integer", "minimum": 0},
                         "retentionClass": string(enum=["AUTHORITATIVE", "BACKUP"]),
                         "verificationLevel": string(enum=["SHA256_WRITE", "SHA256_READBACK"]), "verifiedAt": timestamp()},
        ["id", "contentSha256", "locationKind", "location", "sizeBytes", "retentionClass", "verificationLevel", "verifiedAt"],
        {"schemaVersion": "1", "id": "PON-COPY-001", "contentSha256": "sha256:" + "6" * 64,
         "locationKind": "LOCAL_CAS", "location": "payloads/66/object", "sizeBytes": 10,
         "retentionClass": "AUTHORITATIVE", "verificationLevel": "SHA256_READBACK", "verifiedAt": "2026-09-04T12:00:00Z"},
    )
    schemas["browser-bridge-transfer"] = entity_schema(
        "browser-bridge-transfer",
        {
            "id": identifier(),
            "transportAttemptId": identifier(),
            "direction": string(enum=["TO_BROWSER", "FROM_BROWSER"]),
            "provider": {"const": "GOOGLE_DRIVE"},
            "remoteObjectId": string(),
            "expectedSha256": sha256(),
            "observedSha256": sha256(),
            "durableStorageCopyId": identifier(),
            "state": string(enum=["PENDING", "TRANSFERRED", "INGESTED", "RECEIPTED", "CLEANUP_DUE", "CLEANED", "FAILED"]),
            "accessCount": {"type": "integer", "minimum": 0},
            "createdAt": timestamp(),
            "terminalAt": timestamp(),
        },
        ["id", "transportAttemptId", "direction", "provider", "remoteObjectId", "expectedSha256", "durableStorageCopyId", "state", "accessCount", "createdAt"],
        {"schemaVersion": "1", "id": "PON-BRIDGE-001", "transportAttemptId": "PON-TRANSPORT-001",
         "direction": "TO_BROWSER", "provider": "GOOGLE_DRIVE", "remoteObjectId": "drive-object-001",
         "expectedSha256": "sha256:" + "6" * 64, "durableStorageCopyId": "PON-COPY-001",
         "state": "PENDING", "accessCount": 1, "createdAt": "2026-09-04T12:00:00Z"},
    )
    schemas["semantic-cycle"] = entity_schema(
        "semantic-cycle", {"id": identifier(), "projectId": identifier(), "scope": string(),
                           "openingAuthorityId": identifier(), "state": string(enum=["OPEN", "ACTIVE", "AWAITING_REVIEW", "ACCEPTED", "CLOSED"]),
                           "acceptanceDecisionId": identifier(), "closureAuthorityId": identifier(), "nonClaims": array(string())},
        ["id", "projectId", "scope", "openingAuthorityId", "state", "nonClaims"],
        {"schemaVersion": "1", "id": "PON-CYCLE-001", "projectId": "PON-PROJECT-001", "scope": "Bounded work",
         "openingAuthorityId": "PON-AUTHOR-ACTION-001", "state": "OPEN", "nonClaims": []},
    )
    schemas["legacy-cycle-mapping"] = entity_schema(
        "legacy-cycle-mapping", {"id": identifier(), "legacyCycleId": identifier(), "semanticCycleId": identifier(),
                                 "transportMessageIds": array(identifier()), "classification": string(enum=["EXACT", "TRANSPORT_ONLY", "AMBIGUOUS"]),
                                 "evidence": evidence, "decision": string(enum=["PRESERVE", "REVIEW_REQUIRED"])},
        ["id", "legacyCycleId", "transportMessageIds", "classification", "evidence", "decision"],
        {"schemaVersion": "1", "id": "PON-MAPPING-001", "legacyCycleId": "LEGACY-CYCLE-001",
         "transportMessageIds": ["LEGACY-MSG-001"], "classification": "AMBIGUOUS", "evidence": [], "decision": "REVIEW_REQUIRED"},
    )
    schemas["authority-grant"] = entity_schema(
        "authority-grant", {"id": identifier(), "grantorId": identifier(), "recipientId": identifier(),
                            "allowedOperations": array(string(), minimum=1), "scope": scope,
                            "classification": string(enum=["ONE_SHOT", "STANDING"]), "maximumUses": {"type": "integer", "minimum": 1},
                            "remainingUses": {"type": "integer", "minimum": 0}, "expiresAt": timestamp(),
                            "status": string(enum=["ACTIVE", "EXHAUSTED", "EXPIRED", "REVOKED"]), "rationale": string(),
                            "sourceDecisionId": identifier(), "createdEventId": identifier(), "revokedEventId": identifier()},
        ["id", "grantorId", "recipientId", "allowedOperations", "scope", "classification", "status", "rationale", "sourceDecisionId", "createdEventId"],
        {"schemaVersion": "1", "id": "PON-GRANT-001", "grantorId": "AUTHOR-001", "recipientId": "BROWSER-001",
         "allowedOperations": ["task.create"], "scope": {"projectIds": ["PON-PROJECT-001"]}, "classification": "ONE_SHOT",
         "maximumUses": 1, "remainingUses": 1, "status": "ACTIVE", "rationale": "Bounded task creation",
         "sourceDecisionId": "PON-DECISION-001", "createdEventId": "PON-EVENT-001"},
    )
    schemas["hub-event"] = entity_schema(
        "hub-event",
        {
            "id": identifier(),
            "timestamp": timestamp(),
            "actorId": identifier(),
            "operation": string(),
            "authority": {"oneOf": [
                obj({"capabilityId": identifier(), "authorityGrantId": identifier(), "exactAuthorActionId": identifier()},
                    ["capabilityId", "authorityGrantId"]),
                obj({"capabilityId": identifier(), "authorityGrantId": identifier(), "exactAuthorActionId": identifier()},
                    ["capabilityId", "exactAuthorActionId"]),
            ]},
            "aggregateType": string(),
            "aggregateId": identifier(),
            "aggregateVersion": {"type": "integer", "minimum": 1},
            "beforeStateRoot": sha256(),
            "afterStateRoot": sha256(),
            "result": obj({}, (), True),
            "payloadSha256": sha256(),
            "previousEventSha256": sha256(),
            "eventSha256": sha256(),
        },
        ["id", "timestamp", "actorId", "operation", "authority", "aggregateType", "aggregateId",
         "aggregateVersion", "beforeStateRoot", "afterStateRoot", "result", "payloadSha256", "eventSha256"],
        {"schemaVersion": "1", "id": "PON-EVENT-001", "timestamp": "2026-09-04T12:00:00Z",
         "actorId": "PON-ACTOR-001", "operation": "task.create",
         "authority": {"capabilityId": "PON-CAPABILITY-001", "authorityGrantId": "PON-GRANT-001"},
         "aggregateType": "Task", "aggregateId": "PON-TASK-001", "aggregateVersion": 1,
         "beforeStateRoot": "sha256:" + "0" * 64, "afterStateRoot": "sha256:" + "1" * 64,
         "result": {"created": True}, "payloadSha256": "sha256:" + "2" * 64,
         "eventSha256": "sha256:" + "3" * 64},
    )
    schemas["project"] = entity_schema(
        "project", {"id": identifier(), "code": identifier(), "displayName": string(),
                    "kind": string(enum=["INTEGRATION", "PACKAGE_DEVELOPMENT", "INFRASTRUCTURE", "RESEARCH", "EVALUATION"]),
                    "status": string(enum=["PROPOSED", "PROVISIONING", "ACTIVE", "PAUSED", "PROVISIONING_FAILED", "ARCHIVED"]),
                    "authorityIds": array(identifier()), "roots": roots, "registers": array(identifier()),
                    "mailDomains": array(identifier(), minimum=1),
                    "createdEventId": identifier(), "aggregateVersion": {"type": "integer", "minimum": 1}, "aggregateRoot": sha256()},
        ["id", "code", "displayName", "kind", "status", "authorityIds", "roots", "registers", "mailDomains", "createdEventId", "aggregateVersion", "aggregateRoot"],
        {"schemaVersion": "1", "id": "PON-PROJECT-001", "code": "PON", "displayName": "Post Office Next",
         "kind": "INFRASTRUCTURE", "status": "ACTIVE", "authorityIds": ["PON-GRANT-001"],
         "roots": {"localProject": "L:/Codex/projects/example", "localCas": "L:/Codex/state/post-office-next/cas",
                   "localBackup": "L:/Codex/backups/post-office-next"}, "registers": ["PON-REGISTER-001"],
         "mailDomains": ["PON"], "createdEventId": "PON-EVENT-001", "aggregateVersion": 1, "aggregateRoot": "sha256:" + "7" * 64},
    )
    task_props = {"id": identifier(), "projectId": identifier(), "taskKind": string(enum=["PACKAGE_DEVELOPMENT", "PACKAGE_REVIEW", "PROJECT_MANAGEMENT", "INTEGRATION", "EVALUATION", "FGPM_CORE", "HUB_INFRASTRUCTURE", "DOCUMENTATION"]),
                  "objective": string(), "mutablePackageIds": array(identifier(), maximum=1), "readOnlyInputs": array(identifier()),
                  "acceptanceContractRoot": sha256(), "authorityGrantId": identifier(),
                  "state": string(enum=["PROPOSED", "TRIAGED", "AWAITING_AUTHORITY", "PROVISIONING", "READY", "ACTIVE", "BLOCKED", "RESPONSE_RETURNED", "UNDER_REVIEW", "ACCEPTED", "CORRECTION_REQUIRED", "REJECTED", "CLOSED", "CANCELLED"]),
                  "createdEventId": identifier(),
                  "aggregateVersion": {"type": "integer", "minimum": 1}, "aggregateRoot": sha256()}
    task_one_of = [
        obj({"taskKind": {"const": "PACKAGE_DEVELOPMENT"}, "mutablePackageIds": array(identifier(), minimum=1, maximum=1)}, ["taskKind", "mutablePackageIds"], True),
        obj({"taskKind": {"enum": ["PACKAGE_REVIEW", "PROJECT_MANAGEMENT", "INTEGRATION", "EVALUATION"]}, "mutablePackageIds": array(identifier(), maximum=0)}, ["taskKind", "mutablePackageIds"], True),
        obj({"taskKind": {"enum": ["FGPM_CORE", "HUB_INFRASTRUCTURE", "DOCUMENTATION"]}, "mutablePackageIds": array(identifier(), maximum=1)}, ["taskKind", "mutablePackageIds"], True),
    ]
    schemas["task"] = entity_schema(
        "task", task_props,
        ["id", "projectId", "taskKind", "objective", "mutablePackageIds", "readOnlyInputs", "acceptanceContractRoot", "authorityGrantId", "state", "createdEventId", "aggregateVersion", "aggregateRoot"],
        {"schemaVersion": "1", "id": "PON-TASK-001", "projectId": "PON-PROJECT-001", "taskKind": "PACKAGE_DEVELOPMENT",
         "objective": "Implement one package", "mutablePackageIds": ["PON-PACKAGE-001"], "readOnlyInputs": [],
         "acceptanceContractRoot": "sha256:" + "8" * 64, "authorityGrantId": "PON-GRANT-001", "state": "READY",
         "createdEventId": "PON-EVENT-001", "aggregateVersion": 1, "aggregateRoot": "sha256:" + "9" * 64},
        task_one_of,
    )
    schemas["software-package"] = entity_schema(
        "software-package", {"id": identifier(), "displayName": string(), "owningProjectId": identifier(), "status": string(enum=["PROPOSED", "IN_DEVELOPMENT", "CANDIDATE", "ACCEPTED", "DEPRECATED", "RETIRED"]),
                             "versions": array(obj({"version": string(), "contentRoot": sha256(), "state": string()}, ["version", "contentRoot", "state"])),
                             "providedInterfaceIds": array(identifier()), "requiredInterfaceIds": array(identifier()), "taskIds": array(identifier()),
                             "sourceLocation": string(), "licence": string()},
        ["id", "displayName", "owningProjectId", "status", "versions", "providedInterfaceIds", "requiredInterfaceIds", "taskIds"],
        {"schemaVersion": "1", "id": "PON-PACKAGE-001", "displayName": "Example package", "owningProjectId": "PON-PROJECT-001",
         "status": "PROPOSED", "versions": [], "providedInterfaceIds": [], "requiredInterfaceIds": [], "taskIds": []},
    )
    schemas["interface"] = entity_schema(
        "interface", {"id": identifier(), "version": string(), "stewardId": identifier(), "status": string(enum=["EXPERIMENTAL", "PROVISIONAL", "SUPPORTED", "DEPRECATED", "RETIRED"]),
                      "schemaRoot": sha256(), "humanGuide": string(), "fixtureRoot": sha256(), "providerIds": array(identifier()),
                      "consumerIds": array(identifier()), "replacementInterfaceId": identifier()},
        ["id", "version", "stewardId", "status", "humanGuide", "providerIds", "consumerIds"],
        {"schemaVersion": "1", "id": "PON-INTERFACE-001", "version": "0.1.0", "stewardId": "PON-PACKAGE-001",
         "status": "PROVISIONAL", "humanGuide": "docs/interface.md", "providerIds": ["PON-PACKAGE-001"], "consumerIds": []},
    )
    schemas["capability-request"] = entity_schema(
        "capability-request", {"id": identifier(), "requestingProjectId": identifier(), "requestingTaskId": identifier(),
                               "problem": string(), "requiredBehaviour": array(string(), minimum=1), "acceptanceFixtureRoot": sha256(),
                               "priority": string(enum=["LOW", "NORMAL", "HIGH", "URGENT"]),
                               "status": string(enum=["OPEN", "TRIAGED", "MATCH_EXISTING", "CHANGE_EXISTING", "ADAPTER_REQUIRED", "NEW_PACKAGE_REQUIRED", "FGPM_CORE_ISSUE", "HUB_ISSUE", "DEFERRED", "REJECTED", "IN_PROGRESS", "FULFILLED", "PARTIAL", "WITHDRAWN"]),
                               "triageDecisionId": identifier(), "linkedTaskIds": array(identifier()), "fulfilmentEvidence": evidence},
        ["id", "requestingProjectId", "problem", "requiredBehaviour", "acceptanceFixtureRoot", "priority", "status", "linkedTaskIds", "fulfilmentEvidence"],
        {"schemaVersion": "1", "id": "PON-REQUEST-001", "requestingProjectId": "PON-PROJECT-001", "problem": "Missing capability",
         "requiredBehaviour": ["Deterministic output"], "acceptanceFixtureRoot": "sha256:" + "a" * 64,
         "priority": "NORMAL", "status": "OPEN", "linkedTaskIds": [], "fulfilmentEvidence": []},
    )
    schemas["change-set"] = entity_schema(
        "change-set", {"id": identifier(), "purpose": string(), "taskIds": array(identifier(), minimum=1), "integrationContractRoot": sha256(),
                       "dependencyEdges": array(obj({"fromTaskId": identifier(), "toTaskId": identifier()}, ["fromTaskId", "toTaskId"])),
                       "state": string(enum=["PROPOSED", "TASKS_AUTHORISED", "TASKS_ACTIVE", "CANDIDATES_READY", "INTEGRATION", "ACCEPTED", "PARTIAL", "REJECTED", "CLOSED"]), "decisionId": identifier()},
        ["id", "purpose", "taskIds", "integrationContractRoot", "dependencyEdges", "state"],
        {"schemaVersion": "1", "id": "PON-CHANGE-001", "purpose": "Coordinated change", "taskIds": ["PON-TASK-001"],
         "integrationContractRoot": "sha256:" + "b" * 64, "dependencyEdges": [], "state": "PROPOSED"},
    )
    schemas["integration-candidate"] = entity_schema(
        "integration-candidate", {"id": identifier(), "projectId": identifier(), "parentGenerationId": identifier(),
                                  "packageRoots": obj({}, (), True), "interfaceVersions": obj({}, (), True), "configurationRoot": sha256(),
                                  "testResults": array(obj({"name": string(), "status": string(enum=["PASSED", "FAILED", "SKIPPED"]), "evidenceRoot": sha256()}, ["name", "status"])),
                                  "state": string(enum=["PROPOSED", "VALIDATING", "VALIDATED", "ACCEPTED", "PARTIAL", "REJECTED", "SUPERSEDED"])},
        ["id", "projectId", "parentGenerationId", "packageRoots", "interfaceVersions", "configurationRoot", "testResults", "state"],
        {"schemaVersion": "1", "id": "PON-CANDIDATE-001", "projectId": "PON-PROJECT-001", "parentGenerationId": "PON-GEN-001",
         "packageRoots": {"PON-PACKAGE-001": "sha256:" + "c" * 64}, "interfaceVersions": {"PON-INTERFACE-001": "0.1.0"},
         "configurationRoot": "sha256:" + "d" * 64, "testResults": [], "state": "PROPOSED"},
    )
    schemas["provisioning-plan"] = entity_schema(
        "provisioning-plan", {"id": identifier(), "aggregateType": string(), "aggregateId": identifier(), "requestedResources": array(obj({"kind": string(), "identity": string()}, ["kind", "identity"]), minimum=1),
                              "authorityId": identifier(), "planRoot": sha256(), "stage": string(enum=["PLAN", "AUTHORISED", "IDS_RESERVED", "EXTERNAL_PENDING", "VERIFIED", "COMMITTED", "READY", "COMPENSATING", "ROLLED_BACK", "ORPHANED_REVIEW"]),
                              "compensations": array(obj({"resource": string(), "action": string()}, ["resource", "action"]))},
        ["id", "aggregateType", "aggregateId", "requestedResources", "authorityId", "planRoot", "stage", "compensations"],
        {"schemaVersion": "1", "id": "PON-PLAN-001", "aggregateType": "Task", "aggregateId": "PON-TASK-001",
         "requestedResources": [{"kind": "mailbox", "identity": "PON-MAILBOX-001"}], "authorityId": "PON-GRANT-001",
         "planRoot": "sha256:" + "e" * 64, "stage": "PLAN", "compensations": []},
    )
    schemas["context-bundle"] = entity_schema(
        "context-bundle", {"id": identifier(), "taskId": identifier(), "includedSources": array(obj({"reference": string(), "sha256": sha256(), "access": string(enum=["READ_ONLY", "MUTABLE"])}, ["reference", "sha256", "access"]), minimum=1),
                           "excludedSources": array(string()), "contentRoot": sha256(), "outputBundleId": identifier()},
        ["id", "taskId", "includedSources", "excludedSources", "contentRoot", "outputBundleId"],
        {"schemaVersion": "1", "id": "PON-CONTEXT-001", "taskId": "PON-TASK-001",
         "includedSources": [{"reference": "contracts/task.json", "sha256": "sha256:" + "f" * 64, "access": "READ_ONLY"}],
         "excludedSources": ["caller-secrets"], "contentRoot": "sha256:" + "0" * 64, "outputBundleId": "PON-BUNDLE-001"},
    )
    schemas["projection-cursor"] = entity_schema(
        "projection-cursor", {"id": identifier(), "projectionKind": string(), "sourceEventCursor": identifier(), "sourceRoot": sha256(),
                              "projectionRoot": sha256(), "lastSuccessfulAt": timestamp(), "stale": {"type": "boolean"}, "errors": array(string())},
        ["id", "projectionKind", "sourceEventCursor", "sourceRoot", "projectionRoot", "lastSuccessfulAt", "stale", "errors"],
        {"schemaVersion": "1", "id": "PON-PROJECTION-001", "projectionKind": "DATABASE_VIEW", "sourceEventCursor": "PON-EVENT-001",
         "sourceRoot": "sha256:" + "1" * 64, "projectionRoot": "sha256:" + "2" * 64,
         "lastSuccessfulAt": "2026-09-04T12:00:00Z", "stale": False, "errors": []},
    )
    schemas["attention-item"] = entity_schema(
        "attention-item", {"id": identifier(), "reasonCode": string(), "owningRole": string(), "relatedEntityType": string(),
                           "relatedEntityId": identifier(), "severity": string(enum=["INFO", "WARNING", "ERROR", "CRITICAL"]),
                           "createdEventId": identifier(), "resolvedEventId": identifier(), "state": string(enum=["OPEN", "RESOLVED"])},
        ["id", "reasonCode", "owningRole", "relatedEntityType", "relatedEntityId", "severity", "createdEventId", "state"],
        {"schemaVersion": "1", "id": "PON-ATTENTION-001", "reasonCode": "PROJECTION_STALE", "owningRole": "browser-manager",
         "relatedEntityType": "ProjectionCursor", "relatedEntityId": "PON-PROJECTION-001", "severity": "WARNING",
         "createdEventId": "PON-EVENT-001", "state": "OPEN"},
    )
    return schemas


def _scope_input() -> dict[str, Any]:
    result = obj({"projectIds": array(identifier()), "taskIds": array(identifier()),
                  "packageIds": array(identifier()), "mailDomains": array(identifier())})
    result["minProperties"] = 1
    return result


def _semantic_message_input() -> dict[str, Any]:
    return obj(
        {"id": identifier(), "domain": identifier(), "messageType": string(), "senderEndpointId": identifier(),
         "recipientMailboxId": identifier(), "recipientGeneration": {"type": "integer", "minimum": 1},
         "relatedMessageIds": array(identifier()), "authorityBasisId": identifier(), "requestedAction": string(),
         "completionCriteria": string(), "contentRoot": sha256(), "semanticCycleId": identifier()},
        ["id", "domain", "messageType", "senderEndpointId", "recipientMailboxId", "recipientGeneration",
         "relatedMessageIds", "authorityBasisId", "requestedAction", "completionCriteria", "contentRoot", "semanticCycleId"],
    )


def _bundle_input() -> dict[str, Any]:
    return obj(
        {"id": identifier(), "semanticMessageId": identifier(), "canonicalFilename": safe_filename(),
         "bundleVersion": {"type": "integer", "minimum": 1}, "sizeBytes": {"type": "integer", "minimum": 0},
         "sha256": sha256(), "manifestRoot": sha256(),
         "payloads": array(obj({"path": safe_relative_path(), "sizeBytes": {"type": "integer", "minimum": 0},
                                "sha256": sha256()}, ["path", "sizeBytes", "sha256"]), minimum=1)},
        ["id", "semanticMessageId", "canonicalFilename", "bundleVersion", "sizeBytes", "sha256", "manifestRoot", "payloads"],
    )


def _nonempty_object(properties: dict[str, Any]) -> dict[str, Any]:
    result = obj(properties)
    result["minProperties"] = 1
    return result


READ_OPERATIONS = {
    "hub.status": ([], {"includeDetails": {"type": "boolean"}}),
    "hub.snapshot": ([], {"domains": array(identifier()), "includeFileHashes": {"type": "boolean"}}),
    "authority.inspect": (["actorId", "proposedOperation"], {"actorId": identifier(), "proposedOperation": string()}),
    "project.read": (["projectId"], {"projectId": identifier()}),
    "task.read": (["taskId"], {"taskId": identifier()}),
    "package.read": (["packageId"], {"packageId": identifier()}),
    "package.rehome.preview": (["packageId", "destinationProjectId"], {"packageId": identifier(), "destinationProjectId": identifier()}),
    "interface.read": (["interfaceId"], {"interfaceId": identifier(), "version": string()}),
    "capabilityRequest.read": (["capabilityRequestId"], {"capabilityRequestId": identifier()}),
    "bundle.verify": (["bundle"], {"bundle": _bundle_input()}),
    "transport.inspect": (["semanticMessageId"], {"semanticMessageId": identifier()}),
    "provisioning.inspect": (["provisioningPlanId"], {"provisioningPlanId": identifier()}),
    "attention.list": ([], {"state": string(enum=["OPEN", "RESOLVED"]), "severity": string()}),
}


MUTATION_OPERATIONS = {
    "hub.reconcile.preview": (["snapshotRoot"], {"snapshotRoot": sha256(), "assertionSetIds": array(identifier())}),
    "authority.grant": (["grant"], {"grant": obj(
        {"grantorId": identifier(), "recipientId": identifier(), "allowedOperations": array(string(), minimum=1),
         "scope": _scope_input(), "classification": string(enum=["ONE_SHOT", "STANDING"]),
         "maximumUses": {"type": "integer", "minimum": 1}, "expiresAt": timestamp(), "rationale": string(),
         "sourceDecisionId": identifier()},
        ["grantorId", "recipientId", "allowedOperations", "scope", "classification", "rationale", "sourceDecisionId"]) }),
    "authority.revoke": (["grantId", "reason"], {"grantId": identifier(), "reason": string()}),
    "project.planCreate": (
        ["projectId", "projectCode", "displayName", "kind", "localProjectRoot", "localCasRoot", "localBackupRoot", "mailDomains"],
        {
            "projectId": identifier(), "projectCode": identifier(), "displayName": string(), "kind": string(),
            "localProjectRoot": string(), "localCasRoot": string(), "localBackupRoot": string(),
            "mailDomains": array(identifier(), minimum=1), "policyRoot": sha256(),
        },
    ),
    "project.create": (["approvedPlanId", "planRoot"], {"approvedPlanId": identifier(), "planRoot": sha256()}),
    "project.update": (["projectId", "changes"], {"projectId": identifier(), "changes": _nonempty_object(
        {"displayName": string(), "policyRoot": sha256(), "mailDomains": array(identifier(), minimum=1)})}),
    "project.pause": (["projectId", "reason"], {"projectId": identifier(), "reason": string()}),
    "project.archive": (["projectId", "reason"], {"projectId": identifier(), "reason": string()}),
    "package.register": (["package"], {"package": obj(
        {"id": identifier(), "displayName": string(), "owningProjectId": identifier(), "sourceLocation": string(),
         "licence": string(), "providedInterfaceIds": array(identifier()), "requiredInterfaceIds": array(identifier())},
        ["id", "displayName", "owningProjectId", "providedInterfaceIds", "requiredInterfaceIds"])}),
    "package.registerVersion": (["packageId", "version", "contentRoot"], {"packageId": identifier(), "version": string(), "contentRoot": sha256()}),
    "package.rehome": (["packageId", "destinationProjectId", "approvedPlanId", "planRoot"],
                       {"packageId": identifier(), "destinationProjectId": identifier(), "approvedPlanId": identifier(), "planRoot": sha256()}),
    "package.deprecate": (["packageId", "reason"], {"packageId": identifier(), "version": string(),
                                                            "replacementPackageId": identifier(), "reason": string()}),
    "interface.register": (["interface"], {"interface": obj(
        {"id": identifier(), "version": string(), "stewardId": identifier(),
         "status": string(enum=["EXPERIMENTAL", "PROVISIONAL", "SUPPORTED"]), "schemaRoot": sha256(),
         "humanGuide": string(), "fixtureRoot": sha256(), "providerIds": array(identifier(), minimum=1),
         "consumerIds": array(identifier())},
        ["id", "version", "stewardId", "status", "humanGuide", "providerIds", "consumerIds"])}),
    "interface.deprecate": (["interfaceId", "version", "reason"], {"interfaceId": identifier(), "version": string(),
                                                                        "replacementInterfaceId": identifier(), "reason": string()}),
    "capabilityRequest.create": (["request"], {"request": obj(
        {"id": identifier(), "requestingProjectId": identifier(), "requestingTaskId": identifier(),
         "problem": string(), "requiredBehaviour": array(string(), minimum=1), "acceptanceFixtureRoot": sha256(),
         "priority": string(enum=["LOW", "NORMAL", "HIGH", "URGENT"])},
        ["id", "requestingProjectId", "problem", "requiredBehaviour", "acceptanceFixtureRoot", "priority"])}),
    "capabilityRequest.triage": (["capabilityRequestId", "outcome", "rationale"], {"capabilityRequestId": identifier(), "outcome": string(enum=["MATCH_EXISTING", "CHANGE_EXISTING", "ADAPTER_REQUIRED", "NEW_PACKAGE_REQUIRED", "FGPM_CORE_ISSUE", "HUB_ISSUE", "DEFERRED", "REJECTED"]), "rationale": string()}),
    "capabilityRequest.fulfil": (["capabilityRequestId", "outcome", "evidenceRoots"], {"capabilityRequestId": identifier(), "outcome": string(enum=["FULFILLED", "PARTIAL", "WITHDRAWN"]), "evidenceRoots": array(sha256(), minimum=1)}),
    "task.planCreate": (
        ["taskId", "projectId", "taskKind", "objective", "acceptanceContractRoot", "authorityGrantId"],
        {
            "taskId": identifier(), "projectId": identifier(), "taskKind": string(), "objective": string(),
            "acceptanceContractRoot": sha256(), "authorityGrantId": identifier(),
            "mutablePackageIds": array(identifier(), maximum=1),
        },
    ),
    "task.create": (["approvedPlanId", "planRoot"], {"approvedPlanId": identifier(), "planRoot": sha256()}),
    "task.bindEndpoint": (["taskId", "endpointId", "mailboxGeneration"], {"taskId": identifier(), "endpointId": identifier(), "mailboxGeneration": {"type": "integer", "minimum": 1}}),
    "task.activate": (["taskId"], {"taskId": identifier()}),
    "task.block": (["taskId", "reason"], {"taskId": identifier(), "reason": string()}),
    "task.recordResponse": (["taskId", "messageId", "evidenceRoots"], {"taskId": identifier(), "messageId": identifier(), "evidenceRoots": array(sha256(), minimum=1)}),
    "task.review": (["taskId", "decision", "rationale"], {"taskId": identifier(), "decision": string(enum=["ACCEPTED", "CORRECTION_REQUIRED", "REJECTED"]), "rationale": string()}),
    "task.close": (["taskId", "summary"], {"taskId": identifier(), "summary": string()}),
    "task.moveProject": (["taskId", "destinationProjectId", "reason"], {"taskId": identifier(), "destinationProjectId": identifier(), "reason": string()}),
    "changeSet.create": (["purpose", "taskIds", "integrationContractRoot"], {"purpose": string(), "taskIds": array(identifier(), minimum=1), "integrationContractRoot": sha256()}),
    "changeSet.addTask": (["changeSetId", "taskId"], {"changeSetId": identifier(), "taskId": identifier()}),
    "changeSet.startIntegration": (["changeSetId", "parentGenerationId"], {"changeSetId": identifier(), "parentGenerationId": identifier()}),
    "changeSet.decide": (["changeSetId", "decision", "rationale"], {"changeSetId": identifier(), "decision": string(enum=["ACCEPTED", "PARTIAL", "REJECTED", "ABANDONED"]), "rationale": string()}),
    "integration.record": (["candidate"], {"candidate": obj(
        {"id": identifier(), "projectId": identifier(), "parentGenerationId": identifier(),
         "packageRoots": obj({}, (), True), "interfaceVersions": obj({}, (), True), "configurationRoot": sha256(),
         "testResults": array(obj({"name": string(), "status": string(enum=["PASSED", "FAILED", "SKIPPED"]),
                                         "evidenceRoot": sha256()}, ["name", "status"]))},
        ["id", "projectId", "parentGenerationId", "packageRoots", "interfaceVersions", "configurationRoot", "testResults"])}),
    "endpoint.allocate": (["projectId", "role", "accessScope"], {"projectId": identifier(), "taskId": identifier(), "role": string(), "actorKind": string(enum=["BROWSER", "COURIER", "ENDPOINT", "SYSTEM"]), "accessScope": _scope_input()}),
    "endpoint.revoke": (["endpointId", "reason"], {"endpointId": identifier(), "reason": string()}),
    "mailbox.allocate": (["endpointId", "domain"], {"endpointId": identifier(), "domain": identifier()}),
    "mailbox.rotateGeneration": (["mailboxId", "reason"], {"mailboxId": identifier(), "reason": string()}),
    "contextBundle.build": (["taskId", "sourceInventory"], {"taskId": identifier(), "sourceInventory": array(obj({"reference": string(), "sha256": sha256(), "access": string(enum=["READ_ONLY", "MUTABLE"])}, ["reference", "sha256", "access"]), minimum=1)}),
    "message.plan": (["semanticMessage", "bundle"], {"semanticMessage": _semantic_message_input(), "bundle": _bundle_input()}),
    "message.register": (["semanticMessage", "bundleId"], {"semanticMessage": _semantic_message_input(), "bundleId": identifier()}),
    "message.route": (["semanticMessageId", "bundleId", "destinationMailboxId", "destinationGeneration"], {"semanticMessageId": identifier(), "bundleId": identifier(), "destinationMailboxId": identifier(), "destinationGeneration": {"type": "integer", "minimum": 1}}),
    "message.acknowledge": (["semanticMessageId", "payloadHashes"], {"semanticMessageId": identifier(), "payloadHashes": array(sha256(), minimum=1)}),
    "message.review": (["semanticMessageId", "decision", "rationale"], {"semanticMessageId": identifier(), "decision": string(enum=["ACCEPTED", "CORRECTION_REQUIRED", "REJECTED"]), "rationale": string()}),
    "message.close": (["semanticMessageId", "summary"], {"semanticMessageId": identifier(), "summary": string()}),
    "bundle.supersedeBeforeRegistration": (["oldFilename", "oldSha256", "replacement"], {"oldFilename": safe_filename(), "oldSha256": sha256(), "replacement": _bundle_input()}),
    "transport.retry": (["transportAttemptId"], {"transportAttemptId": identifier()}),
    "transport.quarantine": (["transportAttemptId", "reasonCode"], {"transportAttemptId": identifier(), "reasonCode": string()}),
    "transport.tombstoneDuplicate": (["transportAttemptId", "canonicalTransportAttemptId"], {"transportAttemptId": identifier(), "canonicalTransportAttemptId": identifier()}),
    "cycle.open": (["cycleId", "projectId", "scope"], {"cycleId": identifier(), "projectId": identifier(), "scope": string()}),
    "cycle.markAwaitingReview": (["cycleId", "summary"], {"cycleId": identifier(), "summary": string()}),
    "cycle.accept": (["cycleId", "decisionId"], {"cycleId": identifier(), "decisionId": identifier()}),
    "cycle.close": (["cycleId", "acceptanceDecisionId"], {"cycleId": identifier(), "acceptanceDecisionId": identifier()}),
    "hub.reconcile.apply": (["previewPlanId", "planRoot"], {"previewPlanId": identifier(), "planRoot": sha256()}),
}


MINIMUM_AUTHORITIES = {
    "hub.status": "authenticated-reader", "hub.snapshot": "authenticated-reader",
    "hub.reconcile.preview": "browser-or-courier", "hub.reconcile.apply": "author-or-delegated-browser",
    "authority.inspect": "authenticated-reader", "authority.grant": "author", "authority.revoke": "author",
    "project.read": "authenticated-reader", "project.planCreate": "browser", "project.create": "author-or-delegated-browser",
    "project.update": "author-or-delegated-browser", "project.pause": "author-or-browser", "project.archive": "author",
    "package.read": "authenticated-reader", "package.register": "browser", "package.registerVersion": "browser-after-review",
    "package.rehome.preview": "browser", "package.rehome": "author-or-delegated-browser", "package.deprecate": "package-steward-or-browser",
    "interface.read": "authenticated-reader", "interface.register": "interface-steward-or-browser", "interface.deprecate": "interface-steward-or-browser",
    "capabilityRequest.read": "authenticated-reader", "capabilityRequest.create": "project-manager",
    "capabilityRequest.triage": "browser", "capabilityRequest.fulfil": "browser-after-integration",
    "task.read": "authenticated-reader", "task.planCreate": "browser", "task.create": "author-or-delegated-browser",
    "task.bindEndpoint": "author-or-delegated-browser", "task.activate": "author-or-delegated-browser",
    "task.block": "recipient-or-browser", "task.recordResponse": "courier", "task.review": "browser",
    "task.close": "browser-or-author", "task.moveProject": "author-or-delegated-browser",
    "changeSet.create": "author-or-delegated-browser", "changeSet.addTask": "browser",
    "changeSet.startIntegration": "browser", "changeSet.decide": "browser-or-author",
    "integration.record": "project-manager-or-integration-runner", "endpoint.allocate": "author-or-delegated-browser",
    "endpoint.revoke": "author-or-browser", "mailbox.allocate": "author-or-delegated-browser",
    "mailbox.rotateGeneration": "browser-or-courier", "contextBundle.build": "browser",
    "message.plan": "browser-or-endpoint", "message.register": "courier", "message.route": "courier",
    "message.acknowledge": "recipient", "message.review": "browser-or-author", "message.close": "browser-or-author",
    "bundle.verify": "courier-or-browser", "bundle.supersedeBeforeRegistration": "originator",
    "transport.inspect": "authenticated-reader", "transport.retry": "courier", "transport.tombstoneDuplicate": "courier",
    "transport.quarantine": "courier", "cycle.open": "author", "cycle.markAwaitingReview": "browser",
    "cycle.accept": "author", "cycle.close": "author", "provisioning.inspect": "authenticated-reader",
    "attention.list": "authenticated-reader",
}


CATEGORY_OVERRIDES = {"capabilityRequest": "request", "changeSet": "change-set", "contextBundle": "context"}
LOCAL_EXTENSION_OPERATIONS = {
    "attention.list",
    "capabilityRequest.read",
    "hub.snapshot",
    "interface.read",
    "package.read",
    "project.read",
    "provisioning.inspect",
    "task.bindEndpoint",
    "task.read",
}
P3_2_OPERATIONS = {
    "authority.inspect",
    "authority.grant",
    "authority.revoke",
    "endpoint.allocate",
    "endpoint.revoke",
    "mailbox.allocate",
    "mailbox.rotateGeneration",
}
P3_2_IMPLEMENTED_OPERATIONS = sorted(P3_2_OPERATIONS | {"hub.status"})
P3_3_OPERATIONS = {
    "project.planCreate", "project.create", "project.read", "project.update",
    "project.pause", "project.archive", "task.planCreate", "task.create", "task.read",
    "task.bindEndpoint", "task.activate", "task.block", "task.moveProject",
    "task.recordResponse", "task.review", "task.close", "provisioning.inspect",
}
P3_3_IMPLEMENTED_OPERATIONS = sorted(P3_2_OPERATIONS | P3_3_OPERATIONS | {"hub.status"})
P3_4_OPERATIONS = {
    "package.read", "package.register", "package.registerVersion", "package.rehome.preview",
    "package.rehome", "package.deprecate", "interface.read", "interface.register",
    "interface.deprecate", "capabilityRequest.read", "capabilityRequest.create",
    "capabilityRequest.triage", "capabilityRequest.fulfil", "changeSet.create",
    "changeSet.addTask", "changeSet.startIntegration", "changeSet.decide", "integration.record",
    "contextBundle.build", "message.plan", "message.register", "message.route",
    "message.acknowledge", "message.review", "message.close",
    "bundle.verify", "bundle.supersedeBeforeRegistration", "cycle.open",
    "cycle.markAwaitingReview", "cycle.accept", "cycle.close",
}
P3_4_IMPLEMENTED_OPERATIONS = sorted(P3_2_OPERATIONS | P3_3_OPERATIONS | P3_4_OPERATIONS | {"hub.status"})
P3_5_OPERATIONS = {
    "transport.inspect", "transport.retry", "transport.quarantine",
    "transport.tombstoneDuplicate", "attention.list",
}
P3_5_IMPLEMENTED_OPERATIONS = sorted(P3_2_OPERATIONS | P3_3_OPERATIONS | P3_4_OPERATIONS | P3_5_OPERATIONS | {"hub.status"})
P3_6_OPERATIONS = {"hub.snapshot", "hub.reconcile.preview", "hub.reconcile.apply"}
P3_6_IMPLEMENTED_OPERATIONS = sorted(
    P3_2_OPERATIONS | P3_3_OPERATIONS | P3_4_OPERATIONS | P3_5_OPERATIONS
    | P3_6_OPERATIONS | {"hub.status"}
)


def build_operation_specs() -> dict[str, dict[str, Any]]:
    specs: dict[str, dict[str, Any]] = {}
    for mutates, operations in ((False, READ_OPERATIONS), (True, MUTATION_OPERATIONS)):
        for operation, (required, properties) in operations.items():
            prefix = operation.split(".", 1)[0]
            specs[operation] = {
                "category": CATEGORY_OVERRIDES.get(prefix, prefix),
                "mutates": mutates,
                "minimumAuthority": MINIMUM_AUTHORITIES[operation],
                "parameters": obj(properties, required),
                "contractStatus": (
                    "IMPLEMENTED_P3_6"
                    if operation in P3_6_OPERATIONS
                    else "IMPLEMENTED_P3_1"
                    if operation == "hub.status"
                    else "IMPLEMENTED_P3_2"
                    if operation in P3_2_OPERATIONS
                    else "IMPLEMENTED_P3_3"
                    if operation in P3_3_OPERATIONS
                    else "IMPLEMENTED_P3_4"
                    if operation in P3_4_OPERATIONS
                    else "IMPLEMENTED_P3_5"
                    if operation in P3_5_OPERATIONS
                    else "CONTRACT_ONLY"
                ),
                "catalogueSource": "POST_OFFICE_NEXT_EXTENSION" if operation in LOCAL_EXTENSION_OPERATIONS else "FGPM_WISHLIST",
            }
    return specs


def _sample_value(schema: dict[str, Any], name: str) -> Any:
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "boolean":
        return False
    if kind == "integer":
        return max(1, schema.get("minimum", 0))
    if kind == "array":
        count = max(1, schema.get("minItems", 0))
        return [_sample_value(schema["items"], name) for _ in range(count)]
    if kind == "object":
        result = {key: _sample_value(value, key) for key, value in schema.get("properties", {}).items() if key in schema.get("required", [])}
        if not result and schema.get("minProperties", 0) > 0:
            declared = schema.get("properties", {})
            if declared:
                key = next(iter(declared))
                result[key] = _sample_value(declared[key], key)
            else:
                result["value"] = "example"
        return result
    if name.lower().endswith("root") or "sha256" in name.lower() or schema.get("pattern") == SHA256_PATTERN:
        return "sha256:" + "a" * 64
    if name.lower().endswith("version"):
        return "1.0.0"
    if name.lower().endswith("id") or schema.get("pattern") == ID_PATTERN:
        return "PON-EXAMPLE-001"
    return "example"


def build_request_schema(operation: str, spec: dict[str, Any]) -> dict[str, Any]:
    actor = obj(
        {"id": identifier(), "kind": string(enum=["HUMAN", "BROWSER", "COURIER", "ENDPOINT", "SYSTEM"]),
         "role": string()},
        ["id", "kind", "role"],
    )
    authority_properties = {
        "capabilityId": identifier(),
        "authorityGrantId": identifier(),
        "exactAuthorActionId": identifier(),
    }
    author_only = {"authority.grant", "authority.revoke", "project.archive", "cycle.open", "cycle.accept", "cycle.close"}
    if not spec["mutates"]:
        authority = obj(authority_properties, ["capabilityId"])
    elif operation in author_only:
        authority = obj(
            {"capabilityId": identifier(), "exactAuthorActionId": identifier()},
            ["capabilityId", "exactAuthorActionId"],
        )
    else:
        authority = {"oneOf": [
            obj(authority_properties, ["capabilityId", "authorityGrantId"]),
            obj(authority_properties, ["capabilityId", "exactAuthorActionId"]),
        ]}
    properties: dict[str, Any] = {
        "schemaVersion": {"const": "1"},
        "operation": {"const": operation},
        "requestId": identifier(),
        "actor": actor,
        "authority": authority,
        "parameters": deepcopy(spec["parameters"]),
    }
    required = ["schemaVersion", "operation", "requestId", "actor", "authority", "parameters"]
    if spec["mutates"]:
        properties["aggregate"] = obj(
            {"type": string(), "id": identifier(), "expectedVersion": {"type": "integer", "minimum": 0}, "expectedRoot": sha256()},
            ["type", "id"],
        )
        properties["aggregate"]["anyOf"] = [{"required": ["expectedVersion"]}, {"required": ["expectedRoot"]}]
        required.append("aggregate")
    example_parameters = {
        name: _sample_value(schema, name)
        for name, schema in spec["parameters"].get("properties", {}).items()
        if name in spec["parameters"].get("required", [])
    }
    example: dict[str, Any] = {
        "schemaVersion": "1", "operation": operation, "requestId": "PON-REQUEST-001",
        "actor": {"id": "PON-ACTOR-001", "kind": "HUMAN", "role": spec["minimumAuthority"]},
        "authority": {"capabilityId": "PON-CAPABILITY-001"}, "parameters": example_parameters,
    }
    if spec["mutates"]:
        if operation in author_only:
            example["authority"]["exactAuthorActionId"] = "PON-AUTHOR-ACTION-001"
        else:
            example["authority"]["authorityGrantId"] = "PON-GRANT-001"
        example["aggregate"] = {"type": "Example", "id": "PON-AGGREGATE-001", "expectedVersion": 0}
    return {
        "$schema": DRAFT,
        "$id": f"{SCHEMA_PREFIX}/operations/{operation}.request.schema.json",
        "title": f"{operation} request",
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
        "examples": [example],
        "x-mutates": spec["mutates"],
        "x-minimumAuthority": spec["minimumAuthority"],
        "x-contractStatus": spec["contractStatus"],
    }


def build_result_schema(operation: str, spec: dict[str, Any]) -> dict[str, Any]:
    diagnostic = obj(
        {"schemaVersion": {"const": "1"}, "code": string(enum=sorted(DIAGNOSTICS)), "message": string(), "severity": string(enum=["INFO", "WARNING", "ERROR", "CRITICAL"]),
         "retryable": {"type": "boolean"}, "details": {"type": "object"}},
        ["schemaVersion", "code", "message", "severity", "retryable", "details"],
    )
    receipt_required = ["receiptId", "requestSha256", "recordedAt", "stateRoot"]
    if spec["mutates"]:
        receipt_required.append("mutationEventId")
    receipt = obj(
        {"receiptId": identifier(), "requestSha256": sha256(), "recordedAt": timestamp(),
         "stateRoot": sha256(), "mutationEventId": identifier()},
        receipt_required,
    )
    properties = {
        "schemaVersion": {"const": "1"}, "requestId": identifier(), "operation": {"const": operation},
        "ok": {"type": "boolean"}, "eventId": identifier(), "beforeRoot": sha256(), "afterRoot": sha256(),
        "aggregateVersion": {"type": "integer", "minimum": 0}, "createdIds": array(identifier()),
        "warnings": array(diagnostic), "receipt": receipt, "diagnostic": diagnostic,
    }
    if operation == "hub.status":
        properties["status"] = obj(
            {
                "instanceId": identifier(),
                "mode": string(enum=["ISOLATED", "SHADOW"]),
                "status": string(enum=["READY", "SEALED"]),
                "authorityState": string(enum=["PREVIEW", "AUTHORITATIVE", "RETIRED"]),
                "contractRoot": sha256(),
                "databaseUserVersion": {"type": "integer", "minimum": 1},
                "eventCount": {"type": "integer", "minimum": 0},
                "lastEventId": {"type": ["string", "null"]},
                "idempotencyRecordCountBeforeRequest": {"type": "integer", "minimum": 0},
                "implementedOperations": array(string(), minimum=1),
            },
            [
                "instanceId", "mode", "status", "authorityState", "contractRoot", "databaseUserVersion",
                "eventCount", "lastEventId", "idempotencyRecordCountBeforeRequest",
                "implementedOperations",
            ],
        )
    if operation == "authority.inspect":
        properties["authorization"] = obj(
            {
                "actorId": identifier(),
                "actorStatus": string(enum=["ACTIVE", "REVOKED", "RETIRED", "NOT_FOUND"]),
                "proposedOperation": string(),
                "implemented": {"type": "boolean"},
                "capabilityAllowed": {"type": "boolean"},
                "activeCapabilityIds": array(identifier()),
                "activeGrantIds": array(identifier()),
                "decision": string(enum=["CAPABILITY_ALLOWED", "DENIED"]),
            },
            [
                "actorId", "actorStatus", "proposedOperation", "implemented",
                "capabilityAllowed", "activeCapabilityIds", "activeGrantIds", "decision",
            ],
        )
    if operation in {"project.planCreate", "task.planCreate", "provisioning.inspect"}:
        properties["provisioningPlan"] = obj(
            {
                "id": identifier(), "aggregateType": string(enum=["Project", "Task"]),
                "aggregateId": identifier(), "authorityId": identifier(),
                "requestedResources": array(string()), "planRoot": sha256(),
                "stage": string(enum=["PLAN", "AUTHORISED", "IDS_RESERVED", "EXTERNAL_PENDING", "VERIFIED", "COMMITTED", "READY", "COMPENSATING", "ROLLED_BACK", "ORPHANED_REVIEW"]),
                "payload": obj({}, (), True),
            },
            ["id", "aggregateType", "aggregateId", "authorityId", "requestedResources", "planRoot", "stage", "payload"],
        )
    if operation in {"project.read", "project.create", "project.update", "project.pause", "project.archive"}:
        properties["project"] = obj(
            {
                "id": identifier(), "code": identifier(), "displayName": string(), "kind": string(),
                "status": string(enum=["PROPOSED", "PROVISIONING", "ACTIVE", "PAUSED", "PROVISIONING_FAILED", "ARCHIVED"]),
                "localProjectRoot": string(), "localCasRoot": string(), "localBackupRoot": string(),
                "mailDomains": array(identifier(), minimum=1), "policyRoot": sha256(),
            },
            ["id", "code", "displayName", "kind", "status", "localProjectRoot", "localCasRoot", "localBackupRoot", "mailDomains"],
        )
    if operation in {"task.read", "task.create", "task.bindEndpoint", "task.activate", "task.block", "task.moveProject", "task.recordResponse", "task.review", "task.close"}:
        properties["task"] = obj(
            {
                "id": identifier(), "projectId": identifier(), "taskKind": string(), "objective": string(),
                "mutablePackageId": {"type": ["string", "null"]}, "acceptanceContractRoot": sha256(),
                "authorityGrantId": identifier(),
                "state": string(enum=["PROPOSED", "TRIAGED", "AWAITING_AUTHORITY", "PROVISIONING", "READY", "ACTIVE", "BLOCKED", "RESPONSE_RETURNED", "UNDER_REVIEW", "ACCEPTED", "CORRECTION_REQUIRED", "REJECTED", "CLOSED", "CANCELLED"]),
                "endpointBinding": {"type": ["object", "null"]},
            },
            ["id", "projectId", "taskKind", "objective", "mutablePackageId", "acceptanceContractRoot", "authorityGrantId", "state", "endpointBinding"],
        )
    if operation in {"hub.snapshot", "hub.reconcile.preview", "package.read", "package.rehome.preview", "interface.read", "capabilityRequest.read", "bundle.verify", "transport.inspect", "attention.list"}:
        properties["resource"] = obj({}, (), True)
    example = {"schemaVersion": "1", "requestId": "PON-REQUEST-001", "operation": operation, "ok": True,
               "beforeRoot": "sha256:" + "1" * 64, "afterRoot": "sha256:" + "1" * 64,
               "aggregateVersion": 0, "createdIds": [], "warnings": [],
               "receipt": {"receiptId": "PON-RECEIPT-001", "requestSha256": "sha256:" + "2" * 64,
                           "recordedAt": "2026-09-04T12:00:00Z", "stateRoot": "sha256:" + "1" * 64}}
    success_required = ["ok", "beforeRoot", "afterRoot", "createdIds", "warnings", "receipt"]
    if operation == "hub.status":
        success_required.append("status")
        example["status"] = {
            "instanceId": "PON-KERNEL",
            "mode": "ISOLATED",
            "status": "READY",
            "authorityState": "PREVIEW",
            "contractRoot": "sha256:" + "3" * 64,
            "databaseUserVersion": 7,
            "eventCount": 0,
            "lastEventId": None,
            "idempotencyRecordCountBeforeRequest": 0,
            "implementedOperations": P3_6_IMPLEMENTED_OPERATIONS,
        }
    if operation == "authority.inspect":
        success_required.append("authorization")
        example["authorization"] = {
            "actorId": "PON-ACTOR-001",
            "actorStatus": "ACTIVE",
            "proposedOperation": "hub.status",
            "implemented": True,
            "capabilityAllowed": True,
            "activeCapabilityIds": ["PON-CAPABILITY-001"],
            "activeGrantIds": [],
            "decision": "CAPABILITY_ALLOWED",
        }
    if operation in {"project.planCreate", "task.planCreate", "provisioning.inspect"}:
        success_required.append("provisioningPlan")
        example["provisioningPlan"] = {
            "id": "PON-PLAN-001", "aggregateType": "Project", "aggregateId": "PON-PROJECT-001",
            "authorityId": "PON-GRANT-001", "requestedResources": ["project:PON-PROJECT-001"],
            "planRoot": "sha256:" + "4" * 64, "stage": "PLAN", "payload": {},
        }
    if operation in {"project.read", "project.create", "project.update", "project.pause", "project.archive"}:
        success_required.append("project")
        example["project"] = {
            "id": "PON-PROJECT-001", "code": "DEMO", "displayName": "Demo", "kind": "LOCAL",
            "status": "ACTIVE", "localProjectRoot": "L:/Codex/projects/demo",
            "localCasRoot": "L:/Codex/cas/demo", "localBackupRoot": "L:/Codex/backups/demo",
            "mailDomains": ["DEMO"],
        }
    if operation in {"task.read", "task.create", "task.bindEndpoint", "task.activate", "task.block", "task.moveProject", "task.recordResponse", "task.review", "task.close"}:
        success_required.append("task")
        example["task"] = {
            "id": "PON-TASK-001", "projectId": "PON-PROJECT-001", "taskKind": "PROJECT_MANAGEMENT",
            "objective": "Example objective", "mutablePackageId": None,
            "acceptanceContractRoot": "sha256:" + "5" * 64, "authorityGrantId": "PON-GRANT-001",
            "state": "ACTIVE", "endpointBinding": None,
        }
    if operation in {"hub.snapshot", "hub.reconcile.preview", "package.read", "package.rehome.preview", "interface.read", "capabilityRequest.read", "bundle.verify", "transport.inspect", "attention.list"}:
        success_required.append("resource")
        example["resource"] = {"state": "VALID"}
    if spec["mutates"]:
        success_required.extend(["eventId", "aggregateVersion"])
        example["eventId"] = "PON-EVENT-001"
        example["receipt"]["mutationEventId"] = "PON-EVENT-001"
    failure_example = {
        "schemaVersion": "1", "requestId": "PON-REQUEST-001", "operation": operation, "ok": False,
        "diagnostic": {"schemaVersion": "1", "code": "PON_INPUT_INVALID", "message": "Example failure",
                       "severity": "ERROR", "retryable": False, "details": {}},
    }
    failure_forbidden = ["eventId", "beforeRoot", "afterRoot", "aggregateVersion", "createdIds", "receipt"]
    schema = {
        "$schema": DRAFT,
        "$id": f"{SCHEMA_PREFIX}/operations/{operation}.result.schema.json",
        "title": f"{operation} result",
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": ["schemaVersion", "requestId", "operation", "ok"],
        "oneOf": [
            obj({"ok": {"const": True}, "eventId": identifier(), "aggregateVersion": {"type": "integer", "minimum": 0}}, success_required, True),
            {
                **obj({"ok": {"const": False}, "diagnostic": diagnostic}, ["ok", "diagnostic"], True),
                "not": {"anyOf": [{"required": [name]} for name in failure_forbidden]},
            },
        ],
        "examples": [example, failure_example],
        "x-mutates": spec["mutates"],
        "x-contractStatus": spec["contractStatus"],
        "x-invariants": ["For mutating success, receipt.mutationEventId equals eventId"] if spec["mutates"] else [],
    }
    return schema
