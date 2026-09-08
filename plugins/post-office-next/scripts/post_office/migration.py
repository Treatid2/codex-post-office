# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from .canonical import (
    canonical_json_bytes,
    contained_member,
    is_link_like,
    merkle_root,
    path_is_within,
    read_json,
    require_disjoint_output_directory,
    sha256_bytes,
    sha256_file,
    sha256_json,
    sqlite_content_identity,
    write_json,
)
from .database import initialize_database, inspect_database
from .diagnostics import PostOfficeError
from .legacy import AUTO_REVIEW_SCHEMA_VERSION, HUB_SCHEMA_VERSION, OBSERVER_SCHEMA_VERSION, readonly_connection
from .snapshot import _validate_capture_manifest


DATABASE_NAME = "post-office-next.sqlite3"
IMPORT_RECEIPT_NAME = "migration-receipt.json"
REPLAY_RECEIPT_NAME = "migration-replay-receipt.json"
CAS_MANIFEST_NAME = "cas-manifest.json"
ZERO_DIGEST = "0" * 64
SHA256 = re.compile(r"^[0-9a-f]{64}$")
DETERMINISTIC_SCHEMA_TIME = "1970-01-01T00:00:00Z"


def _json_text(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _typed_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"$blobBase64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, float):
        return {"$floatHex": value.hex()}
    if value is None or isinstance(value, (int, str)):
        return value
    raise PostOfficeError(
        "PON_MIGRATION_MISMATCH",
        "Legacy SQLite value has an unsupported storage class",
        {"pythonType": type(value).__name__},
    )


def _plain_value(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"$blobBase64"}:
        return base64.b64decode(value["$blobBase64"], validate=True)
    if isinstance(value, dict) and set(value) == {"$floatHex"}:
        return float.fromhex(value["$floatHex"])
    return value


def _safe_id(prefix: str, value: Any, length: int = 32) -> str:
    return f"{prefix}-{sha256_json(value)[:length]}"


def _row_values(row: dict[str, Any]) -> dict[str, Any]:
    return {
        name: _plain_value(value)
        for name, value in zip(row["columns"], row["values"])
    }


def _read_legacy_database(path: Path, source_database: str) -> list[dict[str, Any]]:
    con = readonly_connection(path)
    try:
        con.execute("BEGIN")
        tables: list[dict[str, Any]] = []
        names = [
            str(row[0])
            for row in con.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        for table_name in names:
            quoted_table = table_name.replace('"', '""')
            column_rows = list(con.execute(f'PRAGMA table_xinfo("{quoted_table}")'))
            columns = [str(row[1]) for row in column_rows if int(row[6]) == 0]
            declared_types = [str(row[2]) for row in column_rows if int(row[6]) == 0]
            primary_key_columns = [
                str(row[1])
                for row in sorted(column_rows, key=lambda item: int(item[5]) or 2**31)
                if int(row[6]) == 0 and int(row[5]) > 0
            ]
            quoted_columns = ", ".join('"' + name.replace('"', '""') + '"' for name in columns)
            rows: list[dict[str, Any]] = []
            for raw in con.execute(f'SELECT {quoted_columns} FROM "{quoted_table}"'):
                values = [_typed_value(value) for value in raw]
                document = {"columns": columns, "values": values}
                row_hash = sha256_json(document)
                value_map = dict(zip(columns, values))
                primary_key = {name: value_map[name] for name in primary_key_columns}
                rows.append(
                    {
                        **document,
                        "primaryKey": primary_key,
                        "rowSha256": row_hash,
                    }
                )
            rows.sort(key=lambda item: (_json_text(item["primaryKey"]), item["rowSha256"], _json_text(item["values"])))
            for ordinal, row in enumerate(rows):
                row["ordinal"] = ordinal
            tables.append(
                {
                    "sourceDatabase": source_database,
                    "tableName": table_name,
                    "columns": [
                        {"name": name, "declaredType": declared}
                        for name, declared in zip(columns, declared_types)
                    ],
                    "rowCount": len(rows),
                    "rowsRoot": sha256_json([row["rowSha256"] for row in rows]),
                    "rows": rows,
                }
            )
        con.rollback()
        return tables
    finally:
        if con.in_transaction:
            con.rollback()
        con.close()


def _verify_capture(capture_root: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], str]:
    capture_root = capture_root.resolve(strict=True)
    manifest_path = contained_member(capture_root, "capture-manifest.json")
    if not manifest_path.is_file():
        raise PostOfficeError("PON_PATH_NOT_FOUND", "Capture manifest was not found", {"path": str(manifest_path)})
    manifest = read_json(manifest_path)
    databases, external = _validate_capture_manifest(capture_root, manifest)
    versions = manifest.get("legacySchemaVersions", {})
    expected_versions = {
        "hub": HUB_SCHEMA_VERSION,
        "autoReview": AUTO_REVIEW_SCHEMA_VERSION,
        "observer": OBSERVER_SCHEMA_VERSION,
    }
    mismatch = {
        name: {"expected": expected, "actual": versions.get(name)}
        for name, expected in expected_versions.items()
        if versions.get(name) is not None and versions.get(name) != expected
    }
    if mismatch:
        raise PostOfficeError(
            "PON_LEGACY_SCHEMA_UNSUPPORTED",
            "Captured legacy schema is unsupported",
            {"mismatches": mismatch},
        )
    tables: list[dict[str, Any]] = []
    for database in databases:
        path = contained_member(capture_root, database["path"])
        if (
            not path.is_file()
            or path.stat().st_size != database["bytes"]
            or sha256_file(path) != database["sha256"]
        ):
            raise PostOfficeError(
                "PON_SNAPSHOT_INTEGRITY_FAILURE",
                "Captured database does not match its manifest",
                {"database": database["name"]},
            )
        con = readonly_connection(path)
        try:
            con.execute("BEGIN")
            identity = sqlite_content_identity(con)
            con.rollback()
        finally:
            if con.in_transaction:
                con.rollback()
            con.close()
        if (
            identity["contentsRoot"] != database["logicalContentsRoot"]
            or identity["eventBoundary"] != database["eventBoundary"]
        ):
            raise PostOfficeError(
                "PON_SNAPSHOT_INTEGRITY_FAILURE",
                "Captured database logical boundary changed",
                {"database": database["name"]},
            )
        tables.extend(_read_legacy_database(path, database["name"]))
    for record in external.get("records", []):
        captured_path = record.get("capturedPath")
        if not captured_path:
            continue
        path = contained_member(capture_root, captured_path)
        if (
            not path.is_file()
            or path.stat().st_size != record["bytes"]
            or sha256_file(path) != record["sha256"]
        ):
            raise PostOfficeError(
                "PON_SNAPSHOT_INTEGRITY_FAILURE",
                "Captured external evidence changed",
                {"logicalId": record["logicalId"]},
            )
    retained_manifest = external.get("manifestCapturedPath")
    if retained_manifest:
        path = contained_member(capture_root, retained_manifest)
        if (
            not path.is_file()
            or path.stat().st_size != external["manifestBytes"]
            or sha256_file(path) != external["manifestSha256"]
        ):
            raise PostOfficeError(
                "PON_SNAPSHOT_INTEGRITY_FAILURE",
                "Retained external-evidence manifest changed",
                {"path": retained_manifest},
            )
    source_time = DETERMINISTIC_SCHEMA_TIME
    hub_events = next(
        (table for table in tables if table["sourceDatabase"] == "hub" and table["tableName"] == "events"),
        None,
    )
    if hub_events and hub_events["rows"]:
        event_rows = [_row_values(row) for row in hub_events["rows"]]
        event_rows.sort(key=lambda row: int(row.get("sequence") or 0))
        candidate = event_rows[-1].get("occurred_at")
        if isinstance(candidate, str) and candidate:
            source_time = candidate
    return manifest, databases, tables, source_time


def _validate_payload_document(path: Path, kind: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    path = path.resolve(strict=True)
    if is_link_like(path):
        raise PostOfficeError(
            "PON_SNAPSHOT_INTEGRITY_FAILURE",
            "Payload manifest cannot be a link or hard-link alias",
            {"path": str(path)},
        )
    document = read_json(path)
    if (
        not isinstance(document, dict)
        or document.get("schemaVersion") != "1"
        or not isinstance(document.get("payloads"), list)
        or document.get("authority") != "EVIDENCE_ONLY_OLD_POST_OFFICE_REMAINS_AUTHORITATIVE"
    ):
        raise PostOfficeError(
            "PON_SNAPSHOT_INTEGRITY_FAILURE",
            "Payload capture manifest is invalid",
            {"path": str(path)},
        )
    payloads: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(document["payloads"]):
        if not isinstance(raw, dict) or set(raw) != {"sourcePath", "capturedPath", "bytes", "sha256"}:
            raise PostOfficeError(
                "PON_SNAPSHOT_INTEGRITY_FAILURE",
                "Payload custody record is invalid",
                {"path": str(path), "index": index},
            )
        digest = raw.get("sha256")
        size = raw.get("bytes")
        source_path = raw.get("sourcePath")
        captured_path = raw.get("capturedPath")
        if (
            not isinstance(digest, str)
            or not SHA256.fullmatch(digest)
            or digest in seen
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(source_path, str)
            or not source_path.startswith("payloads/")
            or not isinstance(captured_path, str)
            or not captured_path
        ):
            raise PostOfficeError(
                "PON_SNAPSHOT_INTEGRITY_FAILURE",
                "Payload custody record has an invalid identity",
                {"path": str(path), "index": index},
            )
        object_path = contained_member(path.parent, captured_path)
        if (
            not object_path.is_file()
            or object_path.stat().st_size != size
            or sha256_file(object_path) != digest
        ):
            raise PostOfficeError(
                "PON_SNAPSHOT_INTEGRITY_FAILURE",
                "Captured payload bytes do not match their custody record",
                {"sha256": digest, "path": str(object_path)},
            )
        seen.add(digest)
        payloads.append(
            {
                "sourceManifestKind": kind,
                "sourcePath": source_path,
                "capturedPath": captured_path,
                "sizeBytes": size,
                "sha256": digest,
                "objectPath": object_path,
            }
        )
    root_hasher = hashlib.sha256()
    for raw in document["payloads"]:
        root_hasher.update(
            f"{raw['capturedPath']}\n{raw['bytes']}\n{raw['sha256']}\n".encode("utf-8")
        )
    if (
        document.get("payloadCount") != len(payloads)
        or document.get("payloadBytes") != sum(item["sizeBytes"] for item in payloads)
        or document.get("payloadMerkleRoot") != root_hasher.hexdigest()
    ):
        raise PostOfficeError(
            "PON_SNAPSHOT_INTEGRITY_FAILURE",
            "Payload manifest count, byte total, or capture root is invalid",
            {"path": str(path)},
        )
    return document, payloads


def _verify_payload_coverage(
    capture_manifest_path: Path,
    capture_manifest: dict[str, Any],
    baseline_path: Path,
    delta_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    baseline, baseline_payloads = _validate_payload_document(baseline_path, "BASELINE")
    delta, delta_payloads = _validate_payload_document(delta_path, "DELTA")
    capture_manifest_sha = sha256_file(capture_manifest_path)
    if (
        delta.get("status") != "FROZEN_POST_BASELINE_DELTA"
        or delta.get("sourceCaptureRoot") != capture_manifest["captureRoot"]
        or delta.get("sourceCaptureManifestSha256") != capture_manifest_sha
        or delta.get("baselinePayloadManifestSha256") != sha256_file(baseline_path)
        or delta.get("baselinePayloadCount") != len(baseline_payloads)
        or delta.get("sourcePayloadCount") != len(baseline_payloads) + len(delta_payloads)
    ):
        raise PostOfficeError(
            "PON_MIGRATION_MISMATCH",
            "Baseline and frozen payload delta are not bound to the selected capture",
            {},
        )
    combined = baseline_payloads + delta_payloads
    by_source: dict[str, dict[str, Any]] = {}
    by_hash: set[str] = set()
    for item in combined:
        if item["sourcePath"] in by_source or item["sha256"] in by_hash:
            raise PostOfficeError(
                "PON_MIGRATION_MISMATCH",
                "Payload baseline and delta overlap",
                {"sourcePath": item["sourcePath"], "sha256": item["sha256"]},
            )
        by_source[item["sourcePath"]] = item
        by_hash.add(item["sha256"])
    inventory = {
        entry["path"]: entry
        for entry in capture_manifest["files"]
        if isinstance(entry.get("path"), str) and entry["path"].startswith("payloads/")
    }
    if set(by_source) != set(inventory):
        raise PostOfficeError(
            "PON_MIGRATION_MISMATCH",
            "Payload baseline and delta do not exactly cover the frozen capture inventory",
            {
                "missing": sorted(set(inventory) - set(by_source)),
                "extra": sorted(set(by_source) - set(inventory)),
            },
        )
    for source_path, item in by_source.items():
        expected = inventory[source_path]
        if expected.get("bytes") != item["sizeBytes"] or expected.get("sha256") != item["sha256"]:
            raise PostOfficeError(
                "PON_MIGRATION_MISMATCH",
                "Payload custody disagrees with the frozen filesystem inventory",
                {"sourcePath": source_path},
            )
    ordered = sorted(combined, key=lambda item: (item["sha256"], item["sourcePath"]))
    for ordinal, item in enumerate(ordered):
        item["ordinal"] = ordinal
    identity = {
        "baselineManifestSha256": sha256_file(baseline_path),
        "deltaManifestSha256": sha256_file(delta_path),
        "baselineRecordedMerkleRoot": baseline.get("payloadMerkleRoot"),
        "deltaRecordedMerkleRoot": delta.get("payloadMerkleRoot"),
        "payloadCount": len(ordered),
        "payloadBytes": sum(item["sizeBytes"] for item in ordered),
        "payloadsRoot": merkle_root(
            [
                {"sourcePath": item["sourcePath"], "bytes": item["sizeBytes"], "sha256": item["sha256"]}
                for item in ordered
            ]
        ),
    }
    return ordered, identity


def _copy_to_cas(source: Path, output_root: Path, digest: str, size: int) -> str:
    source = source.resolve(strict=True)
    if is_link_like(source):
        raise PostOfficeError(
            "PON_SNAPSHOT_INTEGRITY_FAILURE",
            "Migration source object is linked or aliased",
            {"path": str(source)},
        )
    relative = f"cas/sha256/{digest[:2]}/{digest}"
    destination = output_root.joinpath(*relative.split("/"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.stat().st_size != size or sha256_file(destination) != digest:
            raise PostOfficeError("PON_MIGRATION_MISMATCH", "CAS collision detected", {"sha256": digest})
        return relative
    observed_size = 0
    observed = hashlib.sha256()
    with source.open("rb") as reader, destination.open("xb") as writer:
        for block in iter(lambda: reader.read(1024 * 1024), b""):
            writer.write(block)
            observed.update(block)
            observed_size += len(block)
        writer.flush()
        os.fsync(writer.fileno())
    if observed_size != size or observed.hexdigest() != digest:
        destination.unlink(missing_ok=True)
        raise PostOfficeError(
            "PON_SNAPSHOT_INTEGRITY_FAILURE",
            "Migration source changed while entering local CAS",
            {"path": str(source), "expectedSha256": digest, "observedSha256": observed.hexdigest()},
        )
    if sha256_file(destination) != digest:
        destination.unlink(missing_ok=True)
        raise PostOfficeError("PON_DATABASE_INVALID", "CAS readback verification failed", {"sha256": digest})
    return relative


def _prepare_import_plan(
    capture_root: Path,
    baseline_manifest_path: Path,
    delta_manifest_path: Path,
) -> dict[str, Any]:
    capture_root = capture_root.resolve(strict=True)
    baseline_manifest_path = baseline_manifest_path.resolve(strict=True)
    delta_manifest_path = delta_manifest_path.resolve(strict=True)
    capture_manifest_path = contained_member(capture_root, "capture-manifest.json")
    manifest, databases, tables, source_time = _verify_capture(capture_root)
    payloads, payload_identity = _verify_payload_coverage(
        capture_manifest_path,
        manifest,
        baseline_manifest_path,
        delta_manifest_path,
    )
    artifacts: list[dict[str, Any]] = []

    def add_artifact(kind: str, source_name: str, path: Path, ordinal: int) -> None:
        artifacts.append(
            {
                "artifactKind": kind,
                "ordinal": ordinal,
                "sourceName": source_name,
                "sizeBytes": path.stat().st_size,
                "sha256": sha256_file(path),
                "objectPath": path,
            }
        )

    add_artifact("CAPTURE_MANIFEST", capture_manifest_path.name, capture_manifest_path, 0)
    for ordinal, database in enumerate(sorted(databases, key=lambda item: item["name"])):
        add_artifact(
            "LEGACY_DATABASE",
            database["path"],
            contained_member(capture_root, database["path"]),
            ordinal,
        )
    add_artifact("PAYLOAD_MANIFEST", baseline_manifest_path.name, baseline_manifest_path, 0)
    add_artifact("PAYLOAD_DELTA_MANIFEST", delta_manifest_path.name, delta_manifest_path, 0)
    external = manifest["externalEvidence"]
    retained_manifest = external.get("manifestCapturedPath")
    if retained_manifest:
        add_artifact(
            "EXTERNAL_EVIDENCE_MANIFEST",
            Path(retained_manifest).name,
            contained_member(capture_root, retained_manifest),
            0,
        )

    evidence: list[dict[str, Any]] = []
    for record in sorted(external.get("records", []), key=lambda item: item["logicalId"]):
        item = {
            "logicalId": record["logicalId"],
            "kind": record["kind"],
            "relatedEntityType": record.get("relatedEntityType"),
            "relatedEntityId": record.get("relatedEntityId"),
            "reference": record.get("reference"),
            "sizeBytes": record.get("bytes"),
            "sha256": record.get("sha256"),
            "evidence": record,
        }
        if record.get("capturedPath"):
            item["objectPath"] = contained_member(capture_root, record["capturedPath"])
        evidence.append(item)

    source_database_roots = {
        database["name"]: {
            "sha256": database["sha256"],
            "logicalContentsRoot": database["logicalContentsRoot"],
            "eventBoundary": database["eventBoundary"],
        }
        for database in sorted(databases, key=lambda item: item["name"])
    }
    table_roots = [
        {
            "sourceDatabase": table["sourceDatabase"],
            "tableName": table["tableName"],
            "rowCount": table["rowCount"],
            "rowsRoot": table["rowsRoot"],
        }
        for table in sorted(tables, key=lambda item: (item["sourceDatabase"], item["tableName"]))
    ]
    source_identity = {
        "schemaVersion": "1",
        "captureRoot": manifest["captureRoot"],
        "captureManifestSha256": sha256_file(capture_manifest_path),
        "legacySchemaVersions": manifest["legacySchemaVersions"],
        "sourceDatabaseRoots": source_database_roots,
        "sourceEventBoundary": source_database_roots["hub"]["eventBoundary"],
        "baselinePayloadManifestSha256": payload_identity["baselineManifestSha256"],
        "deltaPayloadManifestSha256": payload_identity["deltaManifestSha256"],
        "payloadIdentity": payload_identity,
        "legacyTablesRoot": sha256_json(table_roots),
        "externalEvidenceRoot": sha256_json(external.get("records", [])),
        "authority": "EVIDENCE_ONLY_OLD_POST_OFFICE_REMAINS_AUTHORITATIVE",
    }
    source_identity_root = sha256_json(source_identity)
    return {
        "migrationRunId": f"MIG-{source_identity_root[:40]}",
        "sourceTime": source_time,
        "sourceIdentity": source_identity,
        "sourceIdentityRoot": source_identity_root,
        "tables": sorted(tables, key=lambda item: (item["sourceDatabase"], item["tableName"])),
        "payloads": payloads,
        "artifacts": sorted(artifacts, key=lambda item: (item["artifactKind"], item["ordinal"])),
        "evidence": evidence,
    }


def _table_index(plan: dict[str, Any]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    return {
        (table["sourceDatabase"], table["tableName"]): [_row_values(row) for row in table["rows"]]
        for table in plan["tables"]
    }


def _insert_identity_map(
    con: sqlite3.Connection,
    run_id: str,
    legacy_type: str,
    legacy_id: str,
    target_type: str,
    target_id: str,
    evidence_sha256: str,
) -> None:
    con.execute(
        "INSERT INTO migration_identity_map VALUES(?,?,?,?,?,?)",
        (run_id, legacy_type, legacy_id, target_type, target_id, evidence_sha256),
    )


def _insert_anomaly(
    con: sqlite3.Connection,
    run_id: str,
    classification: str,
    entity_type: str,
    entity_id: str,
    evidence: Any,
    source_time: str,
) -> None:
    anomaly_id = _safe_id("ANOM", [run_id, classification, entity_type, entity_id])
    con.execute(
        "INSERT INTO migration_anomalies VALUES(?,?,?,?,?,?,?,?)",
        (
            anomaly_id,
            run_id,
            classification,
            entity_type,
            entity_id,
            _json_text(evidence),
            "OPEN",
            source_time,
        ),
    )


def _message_state(legacy_state: str) -> tuple[str, str | None]:
    exact = {
        "ACKNOWLEDGED": "ACKNOWLEDGED",
        "AWAITING_AUTHOR_REVIEW": "RESPONSE_RETURNED",
        "DELIVERED": "DELIVERED",
        "DISPATCHED": "DELIVERED",
        "STALE_GENERATION": "QUARANTINED",
    }
    if legacy_state in exact:
        mapped = exact[legacy_state]
        note = None if legacy_state == mapped else "Legacy state has no exact vNext equivalent"
        return mapped, note
    if legacy_state in {"CLOSED", "CONTINUED"}:
        return "DELIVERED", "Transport/lifecycle terminality preserved raw; no semantic closure inferred"
    return "QUARANTINED", "Unknown legacy state requires review"


def _project_normalized_state(con: sqlite3.Connection, plan: dict[str, Any]) -> dict[str, Any]:
    run_id = plan["migrationRunId"]
    source_time = plan["sourceTime"]
    tables = _table_index(plan)
    mailbox_rows = tables.get(("hub", "mailboxes"), [])
    message_rows = tables.get(("hub", "messages"), [])
    payload_rows = tables.get(("hub", "message_payloads"), [])
    delivery_rows = tables.get(("hub", "browser_deliveries"), [])
    binding_rows = {
        str(row.get("mailbox_id")): row
        for row in tables.get(("hub", "browser_chat_bindings"), [])
    }
    outbox_rows = tables.get(("hub", "browser_outbox"), [])
    project_codes = sorted(
        {
            str(row.get("project_code"))
            for row in [*mailbox_rows, *message_rows, *outbox_rows]
            if row.get("project_code")
        }
    )
    project_ids: dict[str, str] = {}
    mail_domains: dict[str, str] = {}
    for code in project_codes:
        project_id = _safe_id("PROJECT", code, 24)
        domain = f"legacy-{sha256_json(code)[:16]}.post-office.local"
        aggregate = {
            "legacyProjectCode": code,
            "migrationRunId": run_id,
            "status": "ACTIVE",
        }
        con.execute(
            "INSERT INTO projects VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                project_id,
                code,
                code,
                "LEGACY_IMPORTED",
                "ACTIVE",
                f"legacy-projects/{sha256_json(code)[:16]}",
                "cas/sha256",
                "backups",
                1,
                sha256_json(aggregate),
                None,
                source_time,
            ),
        )
        con.execute("INSERT INTO project_mail_domains VALUES(?,?)", (project_id, domain))
        _insert_identity_map(con, run_id, "Project", code, "Project", project_id, sha256_json(aggregate))
        project_ids[code] = project_id
        mail_domains[code] = domain

    legacy_author_actor = "ACTOR-LEGACY-AUTHORITY"
    migration_actor = "ACTOR-P2-MIGRATION-SYSTEM"
    con.execute(
        "INSERT INTO actors VALUES(?,?,?,?,?)",
        (legacy_author_actor, "HUMAN", "LEGACY_EVIDENCE_AUTHORITY", "RETIRED", source_time),
    )
    con.execute(
        "INSERT INTO actors VALUES(?,?,?,?,?)",
        (migration_actor, "SYSTEM", "P2_EVIDENCE_IMPORTER", "RETIRED", source_time),
    )
    capability_id = _safe_id("CAP", [run_id, "migration.import"])
    grant_id = _safe_id("GRANT", [run_id, "migration.import"])
    con.execute(
        "INSERT INTO caller_capabilities VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            capability_id,
            migration_actor,
            "MIGRATION_RUN",
            run_id,
            None,
            _json_text(["migration.import"]),
            sha256_json(["evidence-only", plan["sourceIdentityRoot"]]),
            "REVOKED",
            source_time,
            source_time,
            source_time,
        ),
    )
    con.execute(
        "INSERT INTO authority_grants(grant_id,grantor_actor_id,recipient_actor_id,allowed_operations_json,scope_json,classification,maximum_uses,remaining_uses,status,rationale,created_at,expires_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            grant_id,
            legacy_author_actor,
            migration_actor,
            _json_text(["migration.import"]),
            _json_text({"migrationRunId": run_id, "evidenceOnly": True}),
            "STANDING",
            None,
            None,
            "EXPIRED",
            "Historical evidence-only authority for deterministic P2 migration reconstruction",
            source_time,
            source_time,
        ),
    )

    mailbox_meta: dict[str, dict[str, Any]] = {
        str(row["mailbox_id"]): dict(row)
        for row in mailbox_rows
    }
    generations: dict[str, set[int]] = defaultdict(set)
    for mailbox_id, row in mailbox_meta.items():
        generations[mailbox_id].add(int(row.get("generation") or 1))
    for row in message_rows:
        sender = str(row.get("sender_mailbox_id") or "")
        recipient = str(row.get("recipient_mailbox_id") or "")
        if sender:
            current = mailbox_meta.get(sender, {}).get("generation") or 1
            generations[sender].add(int(row.get("sender_generation") or current))
        if recipient:
            generations[recipient].add(int(row.get("recipient_generation") or 1))
        for mailbox_id in (sender, recipient):
            if mailbox_id and mailbox_id not in mailbox_meta:
                mailbox_meta[mailbox_id] = {
                    "mailbox_id": mailbox_id,
                    "project_code": row.get("project_code"),
                    "label": mailbox_id,
                    "kind": "CODEX",
                    "status": "RETIRED",
                    "generation": max(generations[mailbox_id] or {1}),
                    "created_at": row.get("created_at") or source_time,
                }
                _insert_anomaly(
                    con,
                    run_id,
                    "SYNTHESIZED_REFERENCED_MAILBOX",
                    "Mailbox",
                    mailbox_id,
                    {"messageId": row.get("message_id")},
                    source_time,
                )

    actor_by_mailbox: dict[str, str] = {}
    endpoint_by_mailbox: dict[str, str] = {}
    for mailbox_id in sorted(mailbox_meta):
        row = mailbox_meta[mailbox_id]
        code = str(row.get("project_code") or project_codes[0])
        if code not in project_ids:
            raise PostOfficeError("PON_MIGRATION_MISMATCH", "Mailbox project has no imported project", {"mailboxId": mailbox_id})
        actor_id = _safe_id("ACTOR", ["mailbox", mailbox_id])
        endpoint_id = _safe_id("ENDPOINT", mailbox_id)
        kind = str(row.get("kind") or "CODEX")
        legacy_status = str(row.get("status") or "RETIRED")
        actor_status = "ACTIVE" if legacy_status == "ACTIVE" else "RETIRED"
        endpoint_status = "ACTIVE" if legacy_status == "ACTIVE" else "PROVISIONAL" if legacy_status == "REQUESTED" else "RETIRED"
        binding = binding_rows.get(mailbox_id)
        access_scope = {
            "legacyMailboxId": mailbox_id,
            "legacyKind": kind,
            "threadId": row.get("thread_id"),
            "hostId": row.get("host_id"),
            "expectedRoot": row.get("expected_root"),
            "browserBinding": binding,
            "migrationRunId": run_id,
        }
        con.execute("INSERT INTO actors VALUES(?,?,?,?,?)", (actor_id, "ENDPOINT", kind, actor_status, row.get("created_at") or source_time))
        con.execute(
            "INSERT INTO endpoints VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                endpoint_id,
                actor_id,
                project_ids[code],
                None,
                kind,
                _json_text(access_scope),
                endpoint_status,
                sha256_json(access_scope),
                None,
                row.get("created_at") or source_time,
            ),
        )
        current_generation = int(row.get("generation") or 1)
        for generation in sorted(generations[mailbox_id] or {current_generation}):
            mailbox_status = (
                "ACTIVE"
                if legacy_status == "ACTIVE" and generation == current_generation
                else "PROVISIONAL"
                if legacy_status == "REQUESTED" and generation == current_generation
                else "RETIRED"
            )
            con.execute(
                "INSERT INTO mailboxes VALUES(?,?,?,?,?,?,?)",
                (
                    mailbox_id,
                    generation,
                    mail_domains[code],
                    endpoint_id,
                    mailbox_status,
                    None,
                    row.get("created_at") or source_time,
                ),
            )
        _insert_identity_map(
            con,
            run_id,
            "Mailbox",
            mailbox_id,
            "Endpoint",
            endpoint_id,
            sha256_json(access_scope),
        )
        actor_by_mailbox[mailbox_id] = actor_id
        endpoint_by_mailbox[mailbox_id] = endpoint_id

    messages_by_cycle: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in message_rows:
        cycle_id = str(row.get("root_cycle_id") or row.get("cycle_id") or row["message_id"])
        messages_by_cycle[cycle_id].append(row)
    target_cycle_by_legacy: dict[str, str] = {}
    for legacy_cycle_id in sorted(messages_by_cycle):
        rows = sorted(messages_by_cycle[legacy_cycle_id], key=lambda item: str(item["message_id"]))
        evidence_root = sha256_json(rows)
        action_id = _safe_id("ACTION", ["cycle", legacy_cycle_id])
        target_cycle_id = _safe_id("CYCLE", legacy_cycle_id)
        codes = sorted({str(row.get("project_code")) for row in rows if row.get("project_code")})
        if len(codes) != 1:
            _insert_anomaly(
                con,
                run_id,
                "CYCLE_PROJECT_AMBIGUITY",
                "Cycle",
                legacy_cycle_id,
                {"projectCodes": codes},
                source_time,
            )
        code = codes[0] if codes else project_codes[0]
        semantic_state = "AWAITING_REVIEW" if any(row.get("status") == "AWAITING_AUTHOR_REVIEW" for row in rows) else "ACTIVE"
        con.execute(
            "INSERT INTO exact_author_actions(action_id,actor_id,operation,scope_json,confirmation_sha256,recorded_at) VALUES(?,?,?,?,?,?)",
            (
                action_id,
                legacy_author_actor,
                "migration.legacyCycleObserved",
                _json_text({"legacyCycleId": legacy_cycle_id, "messageIds": [row["message_id"] for row in rows]}),
                evidence_root,
                min(str(row.get("created_at") or source_time) for row in rows),
            ),
        )
        aggregate = {
            "legacyCycleId": legacy_cycle_id,
            "projectCode": code,
            "semanticState": semantic_state,
            "evidenceRoot": evidence_root,
        }
        con.execute(
            "INSERT INTO semantic_cycles VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                target_cycle_id,
                project_ids[code],
                f"Imported legacy cycle {legacy_cycle_id}",
                action_id,
                semantic_state,
                None,
                None,
                1,
                sha256_json(aggregate),
                min(str(row.get("created_at") or source_time) for row in rows),
            ),
        )
        con.execute(
            "INSERT INTO legacy_cycle_mappings VALUES(?,?,?,?,?,?)",
            (
                run_id,
                legacy_cycle_id,
                target_cycle_id,
                evidence_root,
                semantic_state,
                "STRUCTURAL_ONLY_NO_AUTHOR_ACCEPTANCE_INFERRED",
            ),
        )
        _insert_identity_map(con, run_id, "Cycle", legacy_cycle_id, "SemanticCycle", target_cycle_id, evidence_root)
        target_cycle_by_legacy[legacy_cycle_id] = target_cycle_id

    target_messages: set[str] = set()
    for row in sorted(message_rows, key=lambda item: str(item["message_id"])):
        message_id = str(row["message_id"])
        code = str(row["project_code"])
        sender_mailbox = str(row["sender_mailbox_id"])
        recipient_mailbox = str(row["recipient_mailbox_id"])
        cycle_id = str(row.get("root_cycle_id") or row.get("cycle_id") or message_id)
        authorization_class = str(row.get("authorization_class") or "INFO")
        row_evidence = sha256_json(row)
        authority_grant_id: str | None = None
        exact_action_id: str | None = None
        if authorization_class == "AUTHOR":
            exact_action_id = _safe_id("ACTION", ["message", message_id])
            con.execute(
                "INSERT INTO exact_author_actions(action_id,actor_id,operation,scope_json,confirmation_sha256,recorded_at) VALUES(?,?,?,?,?,?)",
                (
                    exact_action_id,
                    legacy_author_actor,
                    "migration.legacyAuthorMessageObserved",
                    _json_text({"legacyMessageId": message_id, "authorizationClass": authorization_class}),
                    row_evidence,
                    row.get("created_at") or source_time,
                ),
            )
        else:
            authority_grant_id = _safe_id("GRANT", ["message", message_id])
            requested = str(row.get("requested_action") or row.get("message_type") or "inform")
            con.execute(
                "INSERT INTO authority_grants(grant_id,grantor_actor_id,recipient_actor_id,allowed_operations_json,scope_json,classification,maximum_uses,remaining_uses,status,rationale,created_at,expires_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    authority_grant_id,
                    legacy_author_actor,
                    actor_by_mailbox[sender_mailbox],
                    _json_text([requested]),
                    _json_text({"legacyMessageId": message_id, "authorizationClass": authorization_class}),
                    "ONE_SHOT",
                    1,
                    0,
                    "EXHAUSTED",
                    "Reconstructed evidence of the authorization class recorded by the legacy message",
                    row.get("created_at") or source_time,
                    row.get("expires_at"),
                ),
            )
        state, mapping_note = _message_state(str(row.get("status") or ""))
        if mapping_note:
            _insert_anomaly(
                con,
                run_id,
                "MESSAGE_STATE_MAPPING",
                "Message",
                message_id,
                {"legacyState": row.get("status"), "targetState": state, "reason": mapping_note},
                source_time,
            )
        content = {
            "subject": row.get("subject"),
            "body": row.get("body"),
            "requestedAction": row.get("requested_action"),
            "completionCriteria": row.get("completion_criteria"),
            "completedSummary": row.get("completed_summary"),
            "unresolved": row.get("unresolved"),
            "legacyRowRoot": row_evidence,
        }
        aggregate = {
            "legacyMessageId": message_id,
            "state": state,
            "contentRoot": sha256_json(content),
            "recipient": [recipient_mailbox, int(row.get("recipient_generation") or 1)],
        }
        con.execute(
            "INSERT INTO semantic_messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                message_id,
                project_ids[code],
                mail_domains[code],
                str(row.get("message_type") or "NOTE"),
                endpoint_by_mailbox[sender_mailbox],
                recipient_mailbox,
                int(row.get("recipient_generation") or 1),
                target_cycle_by_legacy[cycle_id],
                authority_grant_id,
                exact_action_id,
                str(row.get("requested_action") or ""),
                str(row.get("completion_criteria") or ""),
                sha256_json(content),
                state,
                1,
                sha256_json(aggregate),
                None,
                row.get("created_at") or source_time,
            ),
        )
        _insert_identity_map(con, run_id, "Message", message_id, "SemanticMessage", message_id, row_evidence)
        target_messages.add(message_id)

    for row in sorted(message_rows, key=lambda item: str(item["message_id"])):
        parent = row.get("parent_message_id")
        if parent and str(parent) in target_messages:
            con.execute(
                "INSERT INTO message_relations VALUES(?,?,?)",
                (str(row["message_id"]), str(parent), "PARENT"),
            )

    payloads_by_message: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in payload_rows:
        payloads_by_message[str(row["message_id"])].append(row)
    bundle_by_message: dict[str, str] = {}
    for message_id in sorted(payloads_by_message):
        rows = sorted(payloads_by_message[message_id], key=lambda item: int(item["ordinal"]))
        payload_manifest = [
            {
                "ordinal": int(row["ordinal"]),
                "sourceName": str(row["source_name"]),
                "sizeBytes": int(row["size_bytes"]),
                "sha256": str(row["sha256"]),
            }
            for row in rows
        ]
        for payload in payload_manifest:
            storage_copy_id = _safe_id("STORAGE", payload["sha256"], 40)
            present = con.execute(
                "SELECT 1 FROM storage_copies WHERE storage_copy_id=? AND content_sha256=?",
                (storage_copy_id, payload["sha256"]),
            ).fetchone()
            if not present:
                raise PostOfficeError(
                    "PON_MIGRATION_MISMATCH",
                    "Message payload is not present in the verified frozen payload set",
                    {"messageId": message_id, "sha256": payload["sha256"]},
                )
        bundle_id = _safe_id("BUNDLE", message_id)
        canonical_filename = (
            Path(payload_manifest[0]["sourceName"]).name
            if len(payload_manifest) == 1
            else f"{message_id}.bundle"
        )
        con.execute(
            "INSERT INTO message_bundles VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                bundle_id,
                message_id,
                canonical_filename,
                1,
                sum(item["sizeBytes"] for item in payload_manifest),
                sha256_json({"messageId": message_id, "payloads": payload_manifest}),
                sha256_json(payload_manifest),
                "REGISTERED",
                None,
                min(str(row.get("created_at") or source_time) for row in rows),
            ),
        )
        for payload in payload_manifest:
            con.execute(
                "INSERT INTO bundle_payloads VALUES(?,?,?,?,?)",
                (
                    bundle_id,
                    payload["ordinal"],
                    f"payloads/{payload['ordinal']:04d}-{payload['sha256']}",
                    payload["sizeBytes"],
                    payload["sha256"],
                ),
            )
        bundle_by_message[message_id] = bundle_id

    delivery_attempts: dict[str, int] = defaultdict(int)
    for row in sorted(delivery_rows, key=lambda item: str(item["delivery_id"])):
        message_id = str(row["message_id"])
        if message_id not in bundle_by_message:
            _insert_anomaly(
                con,
                run_id,
                "DELIVERY_WITHOUT_IMPORTED_BUNDLE",
                "BrowserDelivery",
                str(row["delivery_id"]),
                row,
                source_time,
            )
            continue
        delivery_attempts[message_id] += 1
        attempt_number = delivery_attempts[message_id]
        attempt_id = _safe_id("TRANSPORT", [row["delivery_id"], attempt_number])
        legacy_state = str(row.get("status") or "")
        if row.get("receipt_reference"):
            state = "RECEIPTED"
        elif "DELIVERED" in legacy_state:
            state = "DELIVERED"
        elif "FAILED" in legacy_state:
            state = "FAILED_FINAL"
        else:
            state = "PENDING"
        con.execute(
            "INSERT INTO transport_attempts VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                attempt_id,
                message_id,
                bundle_by_message[message_id],
                "LEGACY_BROWSER_DELIVERY",
                str(row.get("expected_endpoint") or "LEGACY_BROWSER"),
                state,
                attempt_number,
                None,
                row.get("created_at") or source_time,
                row.get("updated_at") or source_time,
            ),
        )
        drive_file_id = row.get("drive_file_id")
        digest = str(row.get("sha256") or "")
        if drive_file_id and SHA256.fullmatch(digest):
            storage_copy_id = _safe_id("STORAGE", digest, 40)
            bridge_state = "RECEIPTED" if row.get("receipt_reference") else "TRANSFERRED"
            con.execute(
                "INSERT INTO browser_bridge_transfers VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    _safe_id("BRIDGE", row["delivery_id"]),
                    attempt_id,
                    "TO_BROWSER",
                    "GOOGLE_DRIVE",
                    str(drive_file_id),
                    digest,
                    row.get("drive_sha256"),
                    storage_copy_id,
                    bridge_state,
                    1,
                    None,
                    None,
                    row.get("delivered_at"),
                    row.get("created_at") or source_time,
                ),
            )

    normalized_targets = {
        "mailboxes": ["actors", "endpoints", "mailboxes"],
        "messages": ["semantic_cycles", "semantic_messages", "message_relations", "authority_grants", "exact_author_actions"],
        "message_payloads": ["message_bundles", "bundle_payloads", "storage_copies"],
        "browser_deliveries": ["transport_attempts", "browser_bridge_transfers"],
        "browser_chat_bindings": ["endpoints"],
    }
    for table in plan["tables"]:
        source_database = table["sourceDatabase"]
        table_name = table["tableName"]
        targets = normalized_targets.get(table_name, []) if source_database == "hub" else []
        disposition = "NORMALIZED_AND_RAW" if targets else "RAW_ONLY"
        reason = (
            "P2.1 deterministic projection plus exact raw-row retention"
            if targets
            else "Exact facts retained for a later workflow-specific projection without semantic inference"
        )
        con.execute(
            "INSERT INTO migration_projection_coverage VALUES(?,?,?,?,?,?)",
            (run_id, source_database, table_name, disposition, _json_text(targets), reason),
        )
        if not targets and table["rowCount"]:
            _insert_anomaly(
                con,
                run_id,
                "RAW_ONLY_NOT_PROJECTED_P2_1",
                f"{source_database}.{table_name}",
                table_name,
                {"rowCount": table["rowCount"], "rowsRoot": table["rowsRoot"]},
                source_time,
            )
    return {
        "projects": len(project_ids),
        "mailboxes": sum(len(value) for value in generations.values()),
        "messages": len(target_messages),
        "cycles": len(target_cycle_by_legacy),
        "bundles": len(bundle_by_message),
        "transportAttempts": sum(delivery_attempts.values()),
        "capabilityId": capability_id,
        "authorityGrantId": grant_id,
        "migrationActorId": migration_actor,
    }


def _portable_item(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key != "objectPath"}


def _event_bodies(
    plan: dict[str, Any],
    projection_counts: dict[str, Any],
    verification_root: str,
) -> list[tuple[str, str, str, dict[str, Any]]]:
    bodies: list[tuple[str, str, str, dict[str, Any]]] = [
        (
            "migration.import.source",
            "MigrationRun",
            plan["migrationRunId"],
            {
                "kind": "SOURCE",
                "migrationRunId": plan["migrationRunId"],
                "sourceTime": plan["sourceTime"],
                "sourceIdentity": plan["sourceIdentity"],
                "sourceIdentityRoot": plan["sourceIdentityRoot"],
            },
        )
    ]
    for artifact in plan["artifacts"]:
        item = _portable_item(artifact)
        bodies.append(
            (
                "migration.import.artifact",
                "MigrationArtifact",
                f"{artifact['artifactKind']}:{artifact['ordinal']}",
                {"kind": "ARTIFACT", **item},
            )
        )
    for table in plan["tables"]:
        table_key = f"{table['sourceDatabase']}:{table['tableName']}"
        bodies.append(
            (
                "migration.import.table",
                "LegacyTable",
                table_key,
                {
                    "kind": "TABLE",
                    "sourceDatabase": table["sourceDatabase"],
                    "tableName": table["tableName"],
                    "columns": table["columns"],
                    "rowCount": table["rowCount"],
                    "rowsRoot": table["rowsRoot"],
                },
            )
        )
        for row in table["rows"]:
            bodies.append(
                (
                    "migration.import.row",
                    "LegacyRow",
                    f"{table_key}:{row['ordinal']}",
                    {
                        "kind": "ROW",
                        "sourceDatabase": table["sourceDatabase"],
                        "tableName": table["tableName"],
                        **row,
                    },
                )
            )
    for payload in plan["payloads"]:
        bodies.append(
            (
                "migration.import.payload",
                "Payload",
                payload["sha256"],
                {"kind": "PAYLOAD", "record": _portable_item(payload)},
            )
        )
    for evidence in plan["evidence"]:
        bodies.append(
            (
                "migration.import.evidence",
                "MigrationEvidence",
                evidence["logicalId"],
                {"kind": "EVIDENCE", "record": _portable_item(evidence)},
            )
        )
    bodies.append(
        (
            "migration.import.verified",
            "MigrationRun",
            f"{plan['migrationRunId']}:verified",
            {
                "kind": "FINAL",
                "migrationRunId": plan["migrationRunId"],
                "sourceIdentityRoot": plan["sourceIdentityRoot"],
                "projectionCounts": projection_counts,
                "verificationRoot": verification_root,
            },
        )
    )
    return bodies


def _event_hash_document(
    sequence: int,
    event_id: str,
    occurred_at: str,
    actor_id: str,
    operation: str,
    capability_id: str,
    authority_grant_id: str,
    aggregate_type: str,
    aggregate_id: str,
    aggregate_version: int,
    before_state_root: str,
    after_state_root: str,
    result: Any,
    payload_sha256: str,
    previous_event_sha256: str | None,
) -> dict[str, Any]:
    return {
        "sequence": sequence,
        "eventId": event_id,
        "occurredAt": occurred_at,
        "actorId": actor_id,
        "operation": operation,
        "capabilityId": capability_id,
        "authorityGrantId": authority_grant_id,
        "aggregateType": aggregate_type,
        "aggregateId": aggregate_id,
        "aggregateVersion": aggregate_version,
        "beforeStateRoot": before_state_root,
        "afterStateRoot": after_state_root,
        "result": result,
        "payloadSha256": payload_sha256,
        "previousEventSha256": previous_event_sha256,
    }


def _insert_import_events(
    con: sqlite3.Connection,
    plan: dict[str, Any],
    projection_counts: dict[str, Any],
    verification_root: str,
) -> tuple[int, str]:
    actor_id = projection_counts["migrationActorId"]
    capability_id = projection_counts["capabilityId"]
    grant_id = projection_counts["authorityGrantId"]
    previous: str | None = None
    bodies = _event_bodies(plan, projection_counts, verification_root)
    for sequence, (operation, aggregate_type, aggregate_key, result) in enumerate(bodies, start=1):
        event_id = _safe_id("MIGEVENT", [plan["migrationRunId"], sequence, operation, aggregate_key], 48)
        after_root = sha256_json(result)
        event_document = _event_hash_document(
            sequence,
            event_id,
            plan["sourceTime"],
            actor_id,
            operation,
            capability_id,
            grant_id,
            aggregate_type,
            f"{plan['migrationRunId']}:{aggregate_key}",
            1,
            ZERO_DIGEST,
            after_root,
            result,
            after_root,
            previous,
        )
        event_sha = sha256_json(event_document)
        con.execute(
            "INSERT INTO hub_events(sequence,event_id,occurred_at,actor_id,operation,capability_id,authority_grant_id,aggregate_type,aggregate_id,aggregate_version,before_state_root,after_state_root,result_json,payload_sha256,previous_event_sha256,event_sha256) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sequence,
                event_id,
                plan["sourceTime"],
                actor_id,
                operation,
                capability_id,
                grant_id,
                aggregate_type,
                f"{plan['migrationRunId']}:{aggregate_key}",
                1,
                ZERO_DIGEST,
                after_root,
                _json_text(result),
                after_root,
                previous,
                event_sha,
            ),
        )
        previous = event_sha
    if previous is None:
        raise PostOfficeError("PON_INTERNAL_ERROR", "Migration event plan is empty", {})
    return len(bodies), previous


def _register_cas_objects(plan: dict[str, Any], working_root: Path) -> list[dict[str, Any]]:
    objects: dict[str, dict[str, Any]] = {}

    def register(item: dict[str, Any], role: str) -> None:
        source = item.get("objectPath")
        digest = item.get("sha256")
        size = item.get("sizeBytes")
        if source is None:
            return
        if not isinstance(digest, str) or not SHA256.fullmatch(digest) or not isinstance(size, int):
            raise PostOfficeError("PON_MIGRATION_MISMATCH", "Migration object identity is invalid", {"role": role})
        existing = objects.get(digest)
        if existing and existing["sizeBytes"] != size:
            raise PostOfficeError("PON_MIGRATION_MISMATCH", "Migration object hash has conflicting sizes", {"sha256": digest})
        if existing:
            existing["roles"].append(role)
            return
        location = _copy_to_cas(Path(source), working_root, digest, size)
        objects[digest] = {
            "sha256": digest,
            "sizeBytes": size,
            "location": location,
            "roles": [role],
        }

    for item in plan["artifacts"]:
        register(item, f"artifact:{item['artifactKind']}:{item['ordinal']}")
    for item in plan["payloads"]:
        register(item, f"payload:{item['ordinal']}")
    for item in plan["evidence"]:
        register(item, f"evidence:{item['logicalId']}")
    result = sorted(objects.values(), key=lambda item: item["sha256"])
    for item in result:
        item["roles"] = sorted(item["roles"])
    return result


def _build_database(plan: dict[str, Any], working_root: Path, plugin_root: Path) -> dict[str, Any]:
    cas_objects = _register_cas_objects(plan, working_root)
    cas_by_hash = {item["sha256"]: item for item in cas_objects}
    for collection in (plan["artifacts"], plan["payloads"], plan["evidence"]):
        for item in collection:
            if item.get("objectPath") is not None:
                item["casLocation"] = cas_by_hash[item["sha256"]]["location"]
    write_json(
        working_root / CAS_MANIFEST_NAME,
        {
            "schemaVersion": "1",
            "kind": "POST_OFFICE_NEXT_LOCAL_CAS",
            "objects": cas_objects,
            "objectCount": len(cas_objects),
            "totalBytes": sum(item["sizeBytes"] for item in cas_objects),
            "casRoot": merkle_root(cas_objects),
        },
    )
    database_path = working_root / DATABASE_NAME
    initialize_database(
        plugin_root,
        database_path,
        migration_applied_at=DETERMINISTIC_SCHEMA_TIME,
    )
    con = sqlite3.connect(database_path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    try:
        con.execute("BEGIN IMMEDIATE")
        run_id = plan["migrationRunId"]
        source = plan["sourceIdentity"]
        con.execute(
            "INSERT INTO migration_runs VALUES(?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                int(source["legacySchemaVersions"]["hub"]),
                source["captureRoot"],
                2,
                "IMPORTING",
                _json_text({}),
                None,
                plan["sourceTime"],
                None,
            ),
        )
        con.execute(
            "INSERT INTO migration_sources VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                source["captureManifestSha256"],
                source["captureRoot"],
                source["baselinePayloadManifestSha256"],
                source["deltaPayloadManifestSha256"],
                _json_text(source["sourceDatabaseRoots"]),
                _json_text(source["sourceEventBoundary"]),
                plan["sourceIdentityRoot"],
                1,
                plan["sourceTime"],
            ),
        )
        for item in cas_objects:
            con.execute(
                "INSERT INTO storage_copies VALUES(?,?,?,?,?,?,?,?)",
                (
                    _safe_id("STORAGE", item["sha256"], 40),
                    item["sha256"],
                    "LOCAL_CAS",
                    item["location"],
                    item["sizeBytes"],
                    "AUTHORITATIVE",
                    "SHA256_READBACK",
                    plan["sourceTime"],
                ),
            )
        for artifact in plan["artifacts"]:
            con.execute(
                "INSERT INTO migration_source_artifacts VALUES(?,?,?,?,?,?,?)",
                (
                    run_id,
                    artifact["artifactKind"],
                    artifact["ordinal"],
                    artifact["sourceName"],
                    artifact["sizeBytes"],
                    artifact["sha256"],
                    _safe_id("STORAGE", artifact["sha256"], 40),
                ),
            )
        for table in plan["tables"]:
            con.execute(
                "INSERT INTO legacy_import_tables VALUES(?,?,?,?,?,?)",
                (
                    run_id,
                    table["sourceDatabase"],
                    table["tableName"],
                    _json_text(table["columns"]),
                    table["rowCount"],
                    table["rowsRoot"],
                ),
            )
            for row in table["rows"]:
                con.execute(
                    "INSERT INTO legacy_import_rows VALUES(?,?,?,?,?,?,?)",
                    (
                        run_id,
                        table["sourceDatabase"],
                        table["tableName"],
                        row["ordinal"],
                        _json_text(row["primaryKey"]),
                        _json_text({"columns": row["columns"], "values": row["values"]}),
                        row["rowSha256"],
                    ),
                )
        for payload in plan["payloads"]:
            con.execute(
                "INSERT INTO migration_payloads VALUES(?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    payload["ordinal"],
                    payload["sourceManifestKind"],
                    payload["sourcePath"],
                    payload["capturedPath"],
                    payload["sizeBytes"],
                    payload["sha256"],
                    _safe_id("STORAGE", payload["sha256"], 40),
                ),
            )
        for evidence in plan["evidence"]:
            storage_copy_id = (
                _safe_id("STORAGE", evidence["sha256"], 40)
                if evidence.get("objectPath") is not None
                else None
            )
            con.execute(
                "INSERT INTO migration_evidence_items VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    evidence["logicalId"],
                    evidence["kind"],
                    evidence.get("relatedEntityType"),
                    evidence.get("relatedEntityId"),
                    evidence.get("reference"),
                    evidence.get("sizeBytes"),
                    evidence.get("sha256"),
                    storage_copy_id,
                    _json_text(evidence["evidence"]),
                ),
            )
        projection_counts = _project_normalized_state(con, plan)
        counts = {
            "sourceDatabases": len(source["sourceDatabaseRoots"]),
            "sourceTables": len(plan["tables"]),
            "sourceRows": sum(table["rowCount"] for table in plan["tables"]),
            "payloads": len(plan["payloads"]),
            "payloadBytes": sum(item["sizeBytes"] for item in plan["payloads"]),
            "evidenceItems": len(plan["evidence"]),
            "casObjects": len(cas_objects),
            **{key: value for key, value in projection_counts.items() if not key.endswith("Id")},
        }
        verification_identity = {
            "sourceIdentityRoot": plan["sourceIdentityRoot"],
            "legacyTablesRoot": source["legacyTablesRoot"],
            "payloadsRoot": source["payloadIdentity"]["payloadsRoot"],
            "externalEvidenceRoot": source["externalEvidenceRoot"],
            "casRoot": merkle_root(cas_objects),
            "counts": counts,
            "authority": "EVIDENCE_ONLY_OLD_POST_OFFICE_REMAINS_AUTHORITATIVE",
        }
        verification_root = sha256_json(verification_identity)
        con.execute(
            "UPDATE migration_runs SET state='VERIFIED', imported_counts_json=?, verification_root=?, completed_at=? WHERE migration_run_id=?",
            (_json_text(counts), verification_root, plan["sourceTime"], run_id),
        )
        event_count, event_chain_root = _insert_import_events(con, plan, projection_counts, verification_root)
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    finally:
        con.close()
    inspection = inspect_database(database_path, plugin_root)
    return {
        "database": inspection,
        "counts": counts,
        "verificationRoot": verification_root,
        "eventCount": event_count,
        "eventChainRoot": event_chain_root,
        "casObjects": cas_objects,
        "casRoot": merkle_root(cas_objects),
    }


def _publish_migration_output(
    plan: dict[str, Any],
    output_root: Path,
    plugin_root: Path,
    *,
    receipt_kind: str,
    receipt_name: str,
    source_replay: dict[str, Any] | None = None,
) -> dict[str, Any]:
    output_root.parent.mkdir(parents=True, exist_ok=True)
    working_root = output_root.with_name(f".{output_root.name}.{uuid.uuid4().hex}.tmp")
    working_root.mkdir()
    try:
        built = _build_database(plan, working_root, plugin_root)
        database = built["database"]
        if source_replay:
            if (
                built["database"]["logicalStateRoot"] != source_replay["logicalStateRoot"]
                or built["eventChainRoot"] != source_replay["eventChainRoot"]
            ):
                raise PostOfficeError(
                    "PON_MIGRATION_MISMATCH",
                    "Replayed migration does not reproduce the source logical state and event chain",
                    {
                        "sourceLogicalStateRoot": source_replay["logicalStateRoot"],
                        "replayLogicalStateRoot": built["database"]["logicalStateRoot"],
                        "sourceEventChainRoot": source_replay["eventChainRoot"],
                        "replayEventChainRoot": built["eventChainRoot"],
                    },
                )
        identity: dict[str, Any] = {
            "schemaVersion": "1",
            "kind": receipt_kind,
            "migrationRunId": plan["migrationRunId"],
            "sourceIdentityRoot": plan["sourceIdentityRoot"],
            "sourceAuthority": "EVIDENCE_ONLY_OLD_POST_OFFICE_REMAINS_AUTHORITATIVE",
            "targetAuthority": "NON_AUTHORITATIVE_ISOLATED_REHEARSAL",
            "verificationRoot": built["verificationRoot"],
            "logicalStateRoot": database["logicalStateRoot"],
            "logicalContentsRoot": database["database"]["logicalContentsRoot"],
            "eventCount": built["eventCount"],
            "eventChainRoot": built["eventChainRoot"],
            "casRoot": built["casRoot"],
            "casObjectCount": len(built["casObjects"]),
            "casBytes": sum(item["sizeBytes"] for item in built["casObjects"]),
            "counts": built["counts"],
            "databaseUserVersion": database["database"]["userVersion"],
            "quickCheck": database["database"]["quickCheck"],
            "foreignKeyErrors": database["database"]["foreignKeyErrors"],
            "completedAt": plan["sourceTime"],
            "outputRoot": str(output_root),
        }
        if source_replay:
            identity["replaySource"] = source_replay
        receipt = {**identity, "receiptSha256": sha256_json(identity)}
        write_json(working_root / receipt_name, receipt)
        working_root.rename(output_root)
    except Exception:
        if working_root.exists():
            shutil.rmtree(working_root)
        raise
    final_database = output_root / DATABASE_NAME
    final_inspection = inspect_database(final_database, plugin_root)
    if final_inspection["logicalStateRoot"] != receipt["logicalStateRoot"]:
        raise PostOfficeError(
            "PON_DATABASE_INVALID",
            "Published migration database identity changed",
            {"path": str(final_database)},
        )
    return {
        "ok": True,
        **receipt,
        "database": str(final_database),
        "casManifest": str(output_root / CAS_MANIFEST_NAME),
        "receipt": str(output_root / receipt_name),
    }


def import_legacy_capture(
    capture_root: Path,
    baseline_manifest_path: Path,
    delta_manifest_path: Path,
    output_root: Path,
    plugin_root: Path | None = None,
) -> dict[str, Any]:
    plugin_root = (plugin_root or Path(__file__).resolve().parents[2]).resolve(strict=True)
    capture_root = capture_root.resolve(strict=True)
    baseline_manifest_path = baseline_manifest_path.resolve(strict=True)
    delta_manifest_path = delta_manifest_path.resolve(strict=True)
    output_root = require_disjoint_output_directory(
        output_root,
        [capture_root, baseline_manifest_path.parent, delta_manifest_path.parent],
    )
    plan = _prepare_import_plan(capture_root, baseline_manifest_path, delta_manifest_path)
    return _publish_migration_output(
        plan,
        output_root,
        plugin_root,
        receipt_kind="POST_OFFICE_NEXT_DETERMINISTIC_LEGACY_IMPORT",
        receipt_name=IMPORT_RECEIPT_NAME,
    )


def _read_replay_plan(source_root: Path, plugin_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    database_path = contained_member(source_root, DATABASE_NAME)
    source_inspection = inspect_database(database_path, plugin_root)
    con = readonly_connection(database_path)
    previous: str | None = None
    source_body: dict[str, Any] | None = None
    final_body: dict[str, Any] | None = None
    tables: dict[tuple[str, str], dict[str, Any]] = {}
    artifacts: list[dict[str, Any]] = []
    payloads: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    try:
        con.execute("BEGIN")
        rows = list(
            con.execute(
                "SELECT sequence,event_id,occurred_at,actor_id,operation,capability_id,authority_grant_id,"
                "aggregate_type,aggregate_id,aggregate_version,before_state_root,after_state_root,result_json,"
                "payload_sha256,previous_event_sha256,event_sha256 FROM hub_events ORDER BY sequence"
            )
        )
        for expected_sequence, row in enumerate(rows, start=1):
            if int(row[0]) != expected_sequence:
                raise PostOfficeError(
                    "PON_MIGRATION_MISMATCH",
                    "Migration event sequence is not contiguous",
                    {"expected": expected_sequence, "actual": int(row[0])},
                )
            try:
                result = json.loads(str(row[12]))
            except json.JSONDecodeError as exc:
                raise PostOfficeError(
                    "PON_MIGRATION_MISMATCH",
                    "Migration event result is invalid JSON",
                    {"sequence": expected_sequence},
                ) from exc
            document = _event_hash_document(
                int(row[0]),
                str(row[1]),
                str(row[2]),
                str(row[3]),
                str(row[4]),
                str(row[5]),
                str(row[6]),
                str(row[7]),
                str(row[8]),
                int(row[9]),
                str(row[10]),
                str(row[11]),
                result,
                str(row[13]),
                str(row[14]) if row[14] is not None else None,
            )
            if row[14] != previous or str(row[15]) != sha256_json(document):
                raise PostOfficeError(
                    "PON_MIGRATION_MISMATCH",
                    "Migration event chain verification failed",
                    {"sequence": expected_sequence, "eventId": str(row[1])},
                )
            previous = str(row[15])
            kind = result.get("kind") if isinstance(result, dict) else None
            if kind == "SOURCE":
                if source_body is not None:
                    raise PostOfficeError("PON_MIGRATION_MISMATCH", "Replay contains multiple source events", {})
                source_body = result
            elif kind == "ARTIFACT":
                artifacts.append({**{key: value for key, value in result.items() if key != "kind"}})
            elif kind == "TABLE":
                key = (str(result["sourceDatabase"]), str(result["tableName"]))
                if key in tables:
                    raise PostOfficeError("PON_MIGRATION_MISMATCH", "Replay contains a duplicate table event", {"table": key})
                tables[key] = {
                    "sourceDatabase": key[0],
                    "tableName": key[1],
                    "columns": result["columns"],
                    "rowCount": int(result["rowCount"]),
                    "rowsRoot": str(result["rowsRoot"]),
                    "rows": [],
                }
            elif kind == "ROW":
                key = (str(result["sourceDatabase"]), str(result["tableName"]))
                if key not in tables:
                    raise PostOfficeError("PON_MIGRATION_MISMATCH", "Replay row precedes its table event", {"table": key})
                row_document = {"columns": result["columns"], "values": result["values"]}
                if sha256_json(row_document) != result["rowSha256"]:
                    raise PostOfficeError(
                        "PON_MIGRATION_MISMATCH",
                        "Replay row hash is invalid",
                        {"table": key, "ordinal": result.get("ordinal")},
                    )
                tables[key]["rows"].append(
                    {
                        "columns": result["columns"],
                        "values": result["values"],
                        "primaryKey": result["primaryKey"],
                        "rowSha256": result["rowSha256"],
                        "ordinal": int(result["ordinal"]),
                    }
                )
            elif kind == "PAYLOAD":
                if not isinstance(result.get("record"), dict):
                    raise PostOfficeError("PON_MIGRATION_MISMATCH", "Replay payload record is invalid", {"sequence": expected_sequence})
                payloads.append(dict(result["record"]))
            elif kind == "EVIDENCE":
                if not isinstance(result.get("record"), dict):
                    raise PostOfficeError("PON_MIGRATION_MISMATCH", "Replay evidence record is invalid", {"sequence": expected_sequence})
                evidence.append(dict(result["record"]))
            elif kind == "FINAL":
                final_body = result
            else:
                raise PostOfficeError(
                    "PON_MIGRATION_MISMATCH",
                    "Replay encountered a non-migration event",
                    {"sequence": expected_sequence, "kind": kind},
                )
        con.rollback()
    finally:
        if con.in_transaction:
            con.rollback()
        con.close()
    if not source_body or not final_body or previous is None:
        raise PostOfficeError("PON_MIGRATION_MISMATCH", "Migration replay evidence is incomplete", {})
    for key, table in tables.items():
        table["rows"].sort(key=lambda item: item["ordinal"])
        if [row["ordinal"] for row in table["rows"]] != list(range(table["rowCount"])):
            raise PostOfficeError("PON_MIGRATION_MISMATCH", "Replay row ordinals are incomplete", {"table": key})
        if sha256_json([row["rowSha256"] for row in table["rows"]]) != table["rowsRoot"]:
            raise PostOfficeError("PON_MIGRATION_MISMATCH", "Replay table root is invalid", {"table": key})
    run_id = str(source_body["migrationRunId"])
    if final_body.get("migrationRunId") != run_id or final_body.get("sourceIdentityRoot") != source_body.get("sourceIdentityRoot"):
        raise PostOfficeError("PON_MIGRATION_MISMATCH", "Replay source and final events disagree", {})

    def attach_object(item: dict[str, Any]) -> None:
        location = item.get("casLocation")
        if not isinstance(location, str):
            return
        path = contained_member(source_root, location)
        if (
            not path.is_file()
            or path.stat().st_size != item.get("sizeBytes")
            or sha256_file(path) != item.get("sha256")
        ):
            raise PostOfficeError(
                "PON_MIGRATION_MISMATCH",
                "Replay CAS object does not match its import event",
                {"location": location, "sha256": item.get("sha256")},
            )
        item["objectPath"] = path

    for item in [*artifacts, *payloads, *evidence]:
        attach_object(item)
    plan = {
        "migrationRunId": run_id,
        "sourceTime": source_body["sourceTime"],
        "sourceIdentity": source_body["sourceIdentity"],
        "sourceIdentityRoot": source_body["sourceIdentityRoot"],
        "tables": [tables[key] for key in sorted(tables)],
        "payloads": sorted(payloads, key=lambda item: int(item["ordinal"])),
        "artifacts": sorted(artifacts, key=lambda item: (item["artifactKind"], int(item["ordinal"]))),
        "evidence": sorted(evidence, key=lambda item: item["logicalId"]),
    }
    replay_source = {
        "logicalStateRoot": source_inspection["logicalStateRoot"],
        "eventChainRoot": previous,
        "eventCount": source_inspection["database"]["eventCount"],
        "databaseSha256": sha256_file(database_path),
    }
    return plan, replay_source


def replay_migration(
    source_root: Path,
    output_root: Path,
    plugin_root: Path | None = None,
) -> dict[str, Any]:
    plugin_root = (plugin_root or Path(__file__).resolve().parents[2]).resolve(strict=True)
    source_root = source_root.resolve(strict=True)
    if not source_root.is_dir():
        raise PostOfficeError("PON_PATH_NOT_FOUND", "Migration output root was not found", {"path": str(source_root)})
    output_root = require_disjoint_output_directory(output_root, [source_root])
    plan, replay_source = _read_replay_plan(source_root, plugin_root)
    return _publish_migration_output(
        plan,
        output_root,
        plugin_root,
        receipt_kind="POST_OFFICE_NEXT_MIGRATION_REPLAY",
        receipt_name=REPLAY_RECEIPT_NAME,
        source_replay=replay_source,
    )
