# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import os
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from .canonical import (
    is_link_like,
    merkle_root,
    path_is_within,
    read_json,
    require_disjoint_output_directory,
    require_new_output_file,
    sha256_file,
    sha256_json,
    sqlite_content_identity,
    write_json,
)
from .diagnostics import PostOfficeError


HUB_SCHEMA_VERSION = 12
AUTO_REVIEW_SCHEMA_VERSION = 10
OBSERVER_SCHEMA_VERSION = 1
EXCLUDED_STATE_PREFIXES = (
    "backups/",
    "browser-observer/",
    "caller-secrets/",
)
EXCLUDED_STATE_FILES = {"chrome-history-snapshot", "hub.sqlite3", "events.jsonl.lock"}
LOCAL_EXTERNAL_EVIDENCE_KINDS = {
    "PROJECT_REGISTER",
    "LOCAL_ARCHIVE",
}
REMOTE_EXTERNAL_EVIDENCE_KINDS = {
    "LEGACY_DRIVE_REFERENCE",
    "GOOGLE_SHEET_REFERENCE",
}
EXTERNAL_EVIDENCE_KINDS = LOCAL_EXTERNAL_EVIDENCE_KINDS | REMOTE_EXTERNAL_EVIDENCE_KINDS


def _readonly_uri(path: Path) -> str:
    normalized = str(path.resolve()).replace("\\", "/")
    return "file:" + quote(normalized, safe="/:_") + "?mode=ro"


def readonly_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise PostOfficeError("PON_PATH_NOT_FOUND", "Legacy database was not found", {"path": str(path)})
    con = sqlite3.connect(_readonly_uri(path), uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def _metadata_value(con: sqlite3.Connection, key: str) -> str | None:
    exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'").fetchone()
    if not exists:
        return None
    row = con.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row else None


def _validate_legacy_versions(hub: sqlite3.Connection, observer: sqlite3.Connection | None) -> dict[str, int | None]:
    versions = {
        "hub": int(_metadata_value(hub, "schema_version") or -1),
        "autoReview": int(_metadata_value(hub, "auto_review_schema_version") or -1),
        "observer": int(_metadata_value(observer, "schema_version") or -1) if observer else None,
    }
    expected = {"hub": HUB_SCHEMA_VERSION, "autoReview": AUTO_REVIEW_SCHEMA_VERSION, "observer": OBSERVER_SCHEMA_VERSION}
    mismatches = {
        name: {"expected": expected[name], "actual": value}
        for name, value in versions.items()
        if value is not None and value != expected[name]
    }
    if mismatches:
        raise PostOfficeError("PON_LEGACY_SCHEMA_UNSUPPORTED", "Legacy schema version is not supported", {"mismatches": mismatches})
    return versions


def _included_state_files(root: Path) -> list[Path]:
    excluded_identities = _excluded_file_identities(root)
    result: list[Path] = []
    for directory, names, filenames in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        retained_names: list[str] = []
        for name in sorted(names):
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            normalized = relative.casefold() + "/"
            if any(normalized.startswith(prefix) for prefix in EXCLUDED_STATE_PREFIXES):
                continue
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()) or not path_is_within(path.resolve(strict=False), root):
                raise PostOfficeError(
                    "PON_SNAPSHOT_INTEGRITY_FAILURE",
                    "Legacy state inventory contains a linked or escaping directory",
                    {"path": relative},
                )
            retained_names.append(name)
        names[:] = retained_names
        for name in sorted(filenames):
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            normalized = relative.casefold()
            if normalized in EXCLUDED_STATE_FILES or any(normalized.startswith(prefix) for prefix in EXCLUDED_STATE_PREFIXES):
                continue
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()) or not path_is_within(path.resolve(strict=False), root):
                raise PostOfficeError(
                    "PON_SNAPSHOT_INTEGRITY_FAILURE",
                    "Legacy state inventory contains a linked or escaping file",
                    {"path": relative},
                )
            stat = path.stat(follow_symlinks=False)
            if stat.st_nlink > 1 and (stat.st_dev, stat.st_ino) in excluded_identities:
                raise PostOfficeError(
                    "PON_SNAPSHOT_INTEGRITY_FAILURE",
                    "Legacy state inventory aliases an excluded file",
                    {"path": relative},
                )
            result.append(path)
    return sorted(result, key=lambda item: item.relative_to(root).as_posix())


def _excluded_file_identities(root: Path) -> set[tuple[int, int]]:
    identities: set[tuple[int, int]] = set()
    candidates = [root / name for name in EXCLUDED_STATE_FILES]
    candidates.extend(root / prefix.rstrip("/") for prefix in EXCLUDED_STATE_PREFIXES)
    for candidate in candidates:
        if not os.path.lexists(candidate) or candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
            continue
        if candidate.is_file():
            stat = candidate.stat(follow_symlinks=False)
            identities.add((stat.st_dev, stat.st_ino))
            continue
        if candidate.is_dir():
            for directory, names, filenames in os.walk(candidate, topdown=True, followlinks=False):
                directory_path = Path(directory)
                names[:] = [
                    name for name in names
                    if not (directory_path / name).is_symlink()
                    and not (hasattr(directory_path / name, "is_junction") and (directory_path / name).is_junction())
                ]
                for name in filenames:
                    path = directory_path / name
                    if path.is_symlink() or not path.is_file():
                        continue
                    stat = path.stat(follow_symlinks=False)
                    identities.add((stat.st_dev, stat.st_ino))
    return identities


def _state_file_stat(root: Path, paths: Iterable[Path]) -> list[dict[str, Any]]:
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "bytes": path.stat().st_size,
            "modifiedNs": path.stat().st_mtime_ns,
            "sha256": sha256_file(path),
        }
        for path in paths
    ]


def _require_regular_database_source(root: Path, path: Path, name: str) -> None:
    if not path.is_file():
        if name == "hub":
            raise PostOfficeError("PON_PATH_NOT_FOUND", "Legacy database was not found", {"path": str(path)})
        return
    if is_link_like(path) or not path_is_within(path.resolve(strict=False), root):
        raise PostOfficeError(
            "PON_SNAPSHOT_INTEGRITY_FAILURE",
            "Legacy database source is linked or escapes its state root",
            {"database": name, "path": str(path)},
        )


def _database_state(con: sqlite3.Connection) -> dict[str, Any]:
    data_version = int(con.execute("PRAGMA data_version").fetchone()[0])
    con.execute("BEGIN")
    try:
        identity = sqlite_content_identity(con)
    finally:
        con.rollback()
    return {
        "dataVersion": data_version,
        "logicalContentsRoot": identity["contentsRoot"],
        "eventBoundary": identity["eventBoundary"],
    }


def _copied_database_state(path: Path) -> dict[str, Any]:
    con = readonly_connection(path)
    try:
        con.execute("BEGIN")
        identity = sqlite_content_identity(con)
        con.rollback()
        return {
            "logicalContentsRoot": identity["contentsRoot"],
            "eventBoundary": identity["eventBoundary"],
        }
    finally:
        if con.in_transaction:
            con.rollback()
        con.close()


def _backup_legacy_database(source: sqlite3.Connection, destination_path: Path) -> None:
    destination = sqlite3.connect(destination_path)
    try:
        source.backup(destination)
        destination.commit()
        journal_mode = str(destination.execute("PRAGMA journal_mode").fetchone()[0]).upper()
        if journal_mode == "WAL":
            destination.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            destination.execute("PRAGMA journal_mode=DELETE")
        destination.commit()
    finally:
        destination.close()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(destination_path) + suffix)
        if os.path.lexists(sidecar):
            sidecar.unlink()


def _capture_external_evidence(manifest_path: Path | None, capture_root: Path) -> dict[str, Any]:
    if manifest_path is None:
        return {"provided": False, "manifestSha256": None, "records": [], "networkAccess": "NONE"}
    if not manifest_path.is_file():
        raise PostOfficeError("PON_PATH_NOT_FOUND", "External evidence manifest was not found", {"path": str(manifest_path)})
    document = read_json(manifest_path)
    if document.get("schemaVersion") != "1" or not isinstance(document.get("records"), list):
        raise PostOfficeError("PON_INPUT_INVALID", "External evidence manifest is invalid", {"path": str(manifest_path)})
    captured_records: list[dict[str, Any]] = []
    evidence_root = capture_root / "external-evidence"
    for index, raw in enumerate(document["records"]):
        if not isinstance(raw, dict) or raw.get("kind") not in EXTERNAL_EVIDENCE_KINDS or not raw.get("logicalId"):
            raise PostOfficeError("PON_INPUT_INVALID", "External evidence record is invalid", {"index": index})
        record = {
            "kind": raw["kind"],
            "logicalId": str(raw["logicalId"]),
            "relatedEntityType": raw.get("relatedEntityType"),
            "relatedEntityId": raw.get("relatedEntityId"),
            "reference": raw.get("reference"),
        }
        if raw.get("sourcePath"):
            source = Path(str(raw["sourcePath"])).resolve()
            if not source.is_file():
                raise PostOfficeError("PON_PATH_NOT_FOUND", "External evidence file was not found", {"index": index, "path": str(source)})
            if any(part.lower() == "caller-secrets" for part in source.parts):
                raise PostOfficeError("PON_INPUT_INVALID", "Secret-bearing paths cannot be migration evidence", {"index": index, "path": str(source)})
            before_size = source.stat().st_size
            before_hash = sha256_file(source)
            if raw.get("expectedSha256") and str(raw["expectedSha256"]).lower().removeprefix("sha256:") != before_hash:
                raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "External evidence hash did not match", {"index": index, "path": str(source)})
            evidence_root.mkdir(parents=True, exist_ok=True)
            destination = evidence_root / f"{index:04d}-{source.name}"
            shutil.copyfile(source, destination)
            after_hash = sha256_file(source)
            copied_hash = sha256_file(destination)
            if source.stat().st_size != before_size or after_hash != before_hash or copied_hash != before_hash:
                raise PostOfficeError("PON_SNAPSHOT_SOURCE_CHANGED", "External evidence changed during capture", {"index": index, "path": str(source)})
            record.update({
                "sourcePath": str(source),
                "capturedPath": destination.relative_to(capture_root).as_posix(),
                "bytes": before_size,
                "sha256": before_hash,
            })
        elif raw["kind"] in REMOTE_EXTERNAL_EVIDENCE_KINDS:
            if not raw.get("reference"):
                raise PostOfficeError("PON_INPUT_INVALID", "Remote migration evidence requires an existing reference", {"index": index})
            if raw.get("expectedSha256"):
                record["sha256"] = str(raw["expectedSha256"]).lower().removeprefix("sha256:")
        else:
            raise PostOfficeError("PON_INPUT_INVALID", "Local migration evidence requires sourcePath", {"index": index})
        captured_records.append(record)
    return {
        "provided": True,
        "manifestPath": str(manifest_path.resolve()),
        "manifestSha256": sha256_file(manifest_path),
        "records": captured_records,
        "networkAccess": "NONE",
    }


def _preflight_external_evidence(manifest_path: Path | None, capture_root: Path) -> None:
    if manifest_path is None:
        return
    if not manifest_path.is_file():
        raise PostOfficeError("PON_PATH_NOT_FOUND", "External evidence manifest was not found", {"path": str(manifest_path)})
    document = read_json(manifest_path)
    if document.get("schemaVersion") != "1" or not isinstance(document.get("records"), list):
        raise PostOfficeError("PON_INPUT_INVALID", "External evidence manifest is invalid", {"path": str(manifest_path)})
    for index, raw in enumerate(document["records"]):
        if not isinstance(raw, dict) or raw.get("kind") not in EXTERNAL_EVIDENCE_KINDS or not raw.get("logicalId"):
            raise PostOfficeError("PON_INPUT_INVALID", "External evidence record is invalid", {"index": index})
        if raw.get("sourcePath"):
            source = Path(str(raw["sourcePath"])).resolve(strict=False)
            if path_is_within(source, capture_root):
                raise PostOfficeError(
                    "PON_PATH_CONFLICT", "External evidence source overlaps the capture output", {"index": index, "path": str(source)}
                )
            if not source.is_file():
                raise PostOfficeError("PON_PATH_NOT_FOUND", "External evidence file was not found", {"index": index, "path": str(source)})


def capture_state(source_state_root: Path, capture_root: Path, external_evidence_manifest: Path | None = None) -> dict[str, Any]:
    source_state_root = source_state_root.resolve()
    if not source_state_root.is_dir():
        raise PostOfficeError("PON_PATH_NOT_FOUND", "Legacy state root was not found", {"path": str(source_state_root)})
    capture_root = require_disjoint_output_directory(capture_root, [source_state_root])
    _preflight_external_evidence(external_evidence_manifest, capture_root)
    source_hub_path = source_state_root / "hub.sqlite3"
    source_observer_path = source_state_root / "browser-observer" / "observer.sqlite3"
    _require_regular_database_source(source_state_root, source_hub_path, "hub")
    _require_regular_database_source(source_state_root, source_observer_path, "observer")
    observer_present_before = source_observer_path.is_file()
    hub = readonly_connection(source_hub_path)
    observer = readonly_connection(source_observer_path) if observer_present_before else None
    working_root: Path | None = None
    try:
        versions = _validate_legacy_versions(hub, observer)
        database_before = {"hub": _database_state(hub)}
        if observer:
            database_before["observer"] = _database_state(observer)
        first_paths = _included_state_files(source_state_root)
        before = _state_file_stat(source_state_root, first_paths)

        capture_root.parent.mkdir(parents=True, exist_ok=True)
        working_root = capture_root.with_name(f".{capture_root.name}.{uuid.uuid4().hex}.tmp")
        working_root.mkdir()
        marker = working_root / "CAPTURE_INCOMPLETE"
        marker.write_text("Capture is incomplete until capture-manifest.json exists.\n", encoding="utf-8")

        destination_hub_path = working_root / "hub.sqlite3"
        destination_observer_path = working_root / "observer.sqlite3"
        _backup_legacy_database(hub, destination_hub_path)
        if observer:
            _backup_legacy_database(observer, destination_observer_path)

        external_evidence = _capture_external_evidence(external_evidence_manifest, working_root)
        second_paths = _included_state_files(source_state_root)
        after = _state_file_stat(source_state_root, second_paths)
        observer_present_after = source_observer_path.is_file()
        database_after = {"hub": _database_state(hub)}
        if observer:
            database_after["observer"] = _database_state(observer)
        copied_database_states = {"hub": _copied_database_state(destination_hub_path)}
        if observer:
            copied_database_states["observer"] = _copied_database_state(destination_observer_path)

        database_changes: dict[str, Any] = {}
        for name, start in database_before.items():
            end = database_after[name]
            copied = copied_database_states[name]
            if (
                start["dataVersion"] != end["dataVersion"]
                or start["logicalContentsRoot"] != end["logicalContentsRoot"]
                or start["logicalContentsRoot"] != copied["logicalContentsRoot"]
                or start["eventBoundary"] != end["eventBoundary"]
                or start["eventBoundary"] != copied["eventBoundary"]
            ):
                database_changes[name] = {"before": start, "after": end, "captured": copied}
        if observer_present_before != observer_present_after:
            database_changes["observerPresence"] = {
                "before": observer_present_before,
                "after": observer_present_after,
            }
        if before != after or database_changes:
            raise PostOfficeError(
                "PON_SNAPSHOT_SOURCE_CHANGED",
                "Legacy state changed during capture",
                {
                    "captureRoot": str(capture_root),
                    "beforeFilesystemRoot": sha256_json(before),
                    "afterFilesystemRoot": sha256_json(after),
                    "databaseChanges": database_changes,
                },
            )

        file_entries = before
        databases = []
        for logical_name, path in (("hub", destination_hub_path), ("observer", destination_observer_path)):
            if path.exists():
                source_boundary = database_before[logical_name]
                copied_boundary = copied_database_states[logical_name]
                databases.append({
                    "name": logical_name,
                    "path": path.name,
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                    "logicalContentsRoot": copied_boundary["logicalContentsRoot"],
                    "eventBoundary": copied_boundary["eventBoundary"],
                    "sourceDataVersionBefore": source_boundary["dataVersion"],
                    "sourceDataVersionAfter": database_after[logical_name]["dataVersion"],
                })
        capture_identity = {
            "schemaVersion": "1",
            "sourceStateRoot": str(source_state_root),
            "legacySchemaVersions": versions,
            "databases": databases,
            "files": file_entries,
            "externalEvidence": external_evidence,
            "excluded": [
                {"path": "caller-secrets/", "reason": "bearer secrets must never be copied or inventoried"},
                {"path": "backups/", "reason": "historical database backups are outside the operational snapshot"},
                {"path": "browser-observer/", "reason": "observer.sqlite3 is captured transactionally as a database copy"},
                {"path": "chrome-history-snapshot", "reason": "browser history is not Codex Comms operational state"},
                {"path": "events.jsonl.lock", "reason": "lock marker is volatile and carries no operational fact"},
            ],
        }
        capture = {
            **capture_identity,
            "fileMerkleRoot": merkle_root(file_entries),
            "captureRoot": sha256_json(capture_identity),
            "sourceAccess": "SQLITE_MODE_RO_QUERY_ONLY_AND_FILESYSTEM_READ_ONLY",
        }
        write_json(working_root / "capture-manifest.json", capture)
        marker.unlink()
        working_root.rename(capture_root)
    except Exception:
        if working_root is not None and working_root.exists():
            shutil.rmtree(working_root)
        raise
    finally:
        hub.close()
        if observer:
            observer.close()
    return {
        "ok": True,
        "captureRoot": capture["captureRoot"],
        "fileMerkleRoot": capture["fileMerkleRoot"],
        "fileCount": len(file_entries),
        "databaseCopies": databases,
        "legacySchemaVersions": versions,
        "externalEvidenceCount": len(external_evidence["records"]),
        "sourceAccess": capture["sourceAccess"],
        "manifest": str((capture_root / "capture-manifest.json").resolve()),
    }


def source_manifest(source_root: Path, snapshot_archive: Path, output: Path) -> dict[str, Any]:
    source_root = source_root.resolve()
    if not source_root.is_dir() or not snapshot_archive.is_file():
        raise PostOfficeError("PON_PATH_NOT_FOUND", "Source root or snapshot archive was not found", {
            "sourceRoot": str(source_root), "snapshotArchive": str(snapshot_archive)})
    output = require_new_output_file(output, inputs=[snapshot_archive], protected_roots=[source_root])
    entries = []
    for path in sorted((item for item in source_root.rglob("*") if item.is_file()), key=lambda item: item.relative_to(source_root).as_posix()):
        entries.append({"path": path.relative_to(source_root).as_posix(), "bytes": path.stat().st_size, "sha256": sha256_file(path)})
    identity = {
        "schemaVersion": "1",
        "sourceRoot": str(source_root),
        "revisionControl": {"kind": "NONE", "recommendation": "Create a repository at the Codex-Comms project root after explicit author approval; keep both plugin trees in one history."},
        "snapshotArchive": {"path": str(snapshot_archive.resolve()), "bytes": snapshot_archive.stat().st_size, "sha256": sha256_file(snapshot_archive)},
        "files": entries,
    }
    result = {**identity, "sourceRootHash": sha256_json(entries)}
    write_json(output, result)
    return {
        "ok": True,
        "sourceRootHash": result["sourceRootHash"],
        "fileCount": len(entries),
        "snapshotArchive": result["snapshotArchive"],
        "manifest": str(output.resolve()),
        "revisionControl": result["revisionControl"],
    }
