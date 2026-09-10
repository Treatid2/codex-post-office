# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .canonical import (
    publish_file_exclusive,
    read_json,
    require_new_output_file,
    require_outside_protected_roots,
    sha256_file,
    sha256_json,
    sqlite_content_identity,
    write_json,
)
from .diagnostics import PostOfficeError
from .legacy import readonly_connection


APPLICATION_ID = 0x504F4E31  # PON1
MIGRATION_NAME = re.compile(r"(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")
PROTECTED_DATABASE_NAMES = {"hub.sqlite3", "observer.sqlite3"}


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _migration_files(plugin_root: Path) -> list[tuple[int, str, Path]]:
    directory = plugin_root / "database" / "migrations"
    migrations: list[tuple[int, str, Path]] = []
    for path in sorted(directory.glob("*.sql")):
        match = MIGRATION_NAME.fullmatch(path.name)
        if not match:
            raise PostOfficeError("PON_INPUT_INVALID", "Database migration has an invalid filename", {"path": str(path)})
        migrations.append((int(match.group("version")), match.group("name"), path))
    if not migrations:
        raise PostOfficeError("PON_PATH_NOT_FOUND", "No Post Office Next database migrations were found", {"path": str(directory)})
    versions = [item[0] for item in migrations]
    if versions != list(range(1, len(versions) + 1)):
        raise PostOfficeError("PON_MIGRATION_MISMATCH", "Database migration versions are not contiguous", {"versions": versions})
    return migrations


def _expected_migrations(plugin_root: Path) -> list[dict[str, Any]]:
    return [
        {"version": version, "name": name, "sha256": sha256_file(path)}
        for version, name, path in _migration_files(plugin_root)
    ]


def _user_tables(con: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def _schema_objects(con: sqlite3.Connection) -> list[dict[str, str]]:
    return [
        {"type": str(row[0]), "name": str(row[1]), "table": str(row[2]), "sql": str(row[3])}
        for row in con.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE type IN ('table','index','trigger') AND name NOT LIKE 'sqlite_%' AND sql IS NOT NULL "
            "ORDER BY type,name"
        )
    ]


def _expected_schema(plugin_root: Path) -> list[dict[str, str]]:
    con = sqlite3.connect(":memory:")
    try:
        con.execute("PRAGMA foreign_keys=ON")
        for _, _, path in _migration_files(plugin_root):
            con.executescript(path.read_text(encoding="utf-8"))
        return _schema_objects(con)
    finally:
        con.close()


def _inspect_connection(con: sqlite3.Connection, database_path: Path, plugin_root: Path) -> dict[str, Any]:
    expected_migrations = _expected_migrations(plugin_root)
    expected_schema = _expected_schema(plugin_root)
    application_id = int(con.execute("PRAGMA application_id").fetchone()[0])
    user_version = int(con.execute("PRAGMA user_version").fetchone()[0])
    journal_mode = str(con.execute("PRAGMA journal_mode").fetchone()[0]).upper()
    quick_check = str(con.execute("PRAGMA quick_check").fetchone()[0])
    foreign_key_errors = [tuple(row) for row in con.execute("PRAGMA foreign_key_check")]
    tables = _user_tables(con)
    migrations: list[dict[str, Any]] = []
    if "schema_migrations" in tables:
        migrations = [
            {"version": int(row[0]), "name": str(row[1]), "sha256": str(row[2]), "appliedAt": str(row[3])}
            for row in con.execute("SELECT version,name,sha256,applied_at FROM schema_migrations ORDER BY version")
        ]
    actual_migration_identity = [
        {"version": item["version"], "name": item["name"], "sha256": item["sha256"]}
        for item in migrations
    ]
    actual_schema = _schema_objects(con)
    event_count = int(con.execute("SELECT COUNT(*) FROM hub_events").fetchone()[0]) if "hub_events" in tables else 0
    content_identity = sqlite_content_identity(con)
    failures: list[str] = []
    if application_id != APPLICATION_ID:
        failures.append("application_id")
    if user_version != expected_migrations[-1]["version"]:
        failures.append("user_version")
    if actual_migration_identity != expected_migrations:
        failures.append("migration_set")
    if actual_schema != expected_schema:
        failures.append("schema_objects")
    if journal_mode != "WAL":
        failures.append("journal_mode")
    if quick_check != "ok":
        failures.append("quick_check")
    if foreign_key_errors:
        failures.append("foreign_keys")
    logical_identity = {
        "applicationId": application_id,
        "userVersion": user_version,
        "journalMode": journal_mode,
        "quickCheck": quick_check,
        "foreignKeyErrors": foreign_key_errors,
        "tables": tables,
        "migrations": migrations,
        "schemaObjectsRoot": sha256_json(actual_schema),
        "logicalTables": content_identity["tables"],
        "logicalContentsRoot": content_identity["contentsRoot"],
        "eventBoundary": content_identity["eventBoundary"],
        "eventCount": event_count,
    }
    identity = {"path": str(database_path.resolve(strict=False)), **logical_identity}
    if failures:
        raise PostOfficeError(
            "PON_DATABASE_INVALID",
            "Database does not match the exact Post Office Next identity",
            {
                **identity,
                "identityFailures": failures,
                "expectedMigrations": expected_migrations,
                "expectedSchemaObjectsRoot": sha256_json(expected_schema),
            },
        )
    return {
        "database": identity,
        "databaseIdentityRoot": sha256_json(identity),
        "logicalStateRoot": sha256_json(logical_identity),
    }


def _remove_staged_database(path: Path) -> None:
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        if os.path.lexists(candidate):
            candidate.unlink()


def _validate_new_database_path(database_path: Path) -> Path:
    database_path = require_outside_protected_roots(database_path)
    if database_path.name.lower() in PROTECTED_DATABASE_NAMES:
        raise PostOfficeError(
            "PON_PRODUCTION_MUTATION_FORBIDDEN",
            "Post Office Next database destination is reserved for legacy state",
            {"path": str(database_path)},
        )
    return database_path


def initialize_database(
    plugin_root: Path,
    database_path: Path,
    *,
    migration_applied_at: str | None = None,
) -> dict[str, Any]:
    plugin_root = plugin_root.resolve(strict=True)
    database_path = _validate_new_database_path(database_path)
    migrations = _migration_files(plugin_root)
    if os.path.lexists(database_path):
        inspection = inspect_database(database_path, plugin_root)
        applied = [
            {"version": version, "name": name, "sha256": sha256_file(path), "alreadyApplied": True}
            for version, name, path in migrations
        ]
        return {
            "ok": True,
            "created": False,
            "journalMode": inspection["database"]["journalMode"],
            "migrations": applied,
            **inspection,
        }

    database_path.parent.mkdir(parents=True, exist_ok=True)
    staged = database_path.with_name(f".{database_path.name}.{uuid.uuid4().hex}.tmp")
    applied: list[dict[str, Any]] = []
    applied_at = migration_applied_at or _timestamp()
    if not isinstance(applied_at, str) or not applied_at:
        raise PostOfficeError("PON_INPUT_INVALID", "Migration application timestamp is invalid", {})
    try:
        con = sqlite3.connect(staged, timeout=30)
        try:
            con.execute("PRAGMA foreign_keys=ON")
            journal_mode = str(con.execute("PRAGMA journal_mode=WAL").fetchone()[0]).upper()
            con.execute("PRAGMA synchronous=FULL")
            con.execute(f"PRAGMA application_id={APPLICATION_ID}")
            for version, name, path in migrations:
                digest = sha256_file(path)
                escaped_name = name.replace("'", "''")
                script = path.read_text(encoding="utf-8")
                transaction = (
                    "BEGIN IMMEDIATE;\n"
                    + script
                    + f"\nINSERT INTO schema_migrations(version,name,sha256,applied_at) VALUES({version},'{escaped_name}','{digest}','{applied_at.replace(chr(39), chr(39) * 2)}');\n"
                    + f"PRAGMA user_version={version};\nCOMMIT;\n"
                )
                try:
                    con.executescript(transaction)
                except sqlite3.Error as exc:
                    if con.in_transaction:
                        con.rollback()
                    raise PostOfficeError(
                        "PON_DATABASE_INVALID",
                        "Database migration failed",
                        {"version": version, "name": name, "sqliteError": str(exc)},
                    ) from exc
                applied.append({"version": version, "name": name, "sha256": digest, "alreadyApplied": False})
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.commit()
        finally:
            con.close()
        inspect_database(staged, plugin_root)
        publish_file_exclusive(staged, database_path)
    finally:
        _remove_staged_database(staged)
    inspection = inspect_database(database_path, plugin_root)
    return {"ok": True, "created": True, "journalMode": journal_mode, "migrations": applied, **inspection}


def inspect_database(database_path: Path, plugin_root: Path | None = None) -> dict[str, Any]:
    database_path = database_path.resolve(strict=False)
    plugin_root = (plugin_root or Path(__file__).resolve().parents[2]).resolve(strict=True)
    con = readonly_connection(database_path)
    try:
        con.execute("BEGIN")
        result = _inspect_connection(con, database_path, plugin_root)
        con.rollback()
        return result
    finally:
        if con.in_transaction:
            con.rollback()
        con.close()


def validate_operational_connection(
    con: sqlite3.Connection,
    plugin_root: Path,
) -> None:
    """Fail closed when an already-open writer is not the exact vNext schema."""
    expected_migrations = _expected_migrations(plugin_root)
    actual_migrations = [
        {"version": int(row[0]), "name": str(row[1]), "sha256": str(row[2])}
        for row in con.execute(
            "SELECT version,name,sha256 FROM schema_migrations ORDER BY version"
        )
    ]
    failures: list[str] = []
    if int(con.execute("PRAGMA application_id").fetchone()[0]) != APPLICATION_ID:
        failures.append("application_id")
    if int(con.execute("PRAGMA user_version").fetchone()[0]) != expected_migrations[-1]["version"]:
        failures.append("user_version")
    if actual_migrations != expected_migrations:
        failures.append("migration_set")
    if _schema_objects(con) != _expected_schema(plugin_root):
        failures.append("schema_objects")
    if str(con.execute("PRAGMA journal_mode").fetchone()[0]).upper() != "WAL":
        failures.append("journal_mode")
    if str(con.execute("PRAGMA quick_check(1)").fetchone()[0]) != "ok":
        failures.append("quick_check")
    if failures:
        raise PostOfficeError(
            "PON_DATABASE_INVALID",
            "Operational database does not match the exact Post Office Next identity",
            {"identityFailures": failures},
        )


def backup_database(
    database_path: Path,
    destination_path: Path,
    receipt_path: Path,
    plugin_root: Path | None = None,
) -> dict[str, Any]:
    database_path = database_path.resolve(strict=True)
    destination_path = destination_path.resolve(strict=False)
    receipt_path = receipt_path.resolve(strict=False)
    plugin_root = (plugin_root or Path(__file__).resolve().parents[2]).resolve(strict=True)
    require_new_output_file(destination_path, inputs=[database_path, receipt_path])
    require_new_output_file(receipt_path, inputs=[database_path, destination_path])
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    staged_database = destination_path.with_name(f".{destination_path.name}.{uuid.uuid4().hex}.tmp")
    staged_receipt = receipt_path.with_name(f".{receipt_path.name}.{uuid.uuid4().hex}.tmp")
    destination_published = False
    receipt_published = False
    try:
        source = readonly_connection(database_path)
        try:
            source.execute("BEGIN")
            source_inspection = _inspect_connection(source, database_path, plugin_root)
            destination = sqlite3.connect(staged_database)
            try:
                source.backup(destination)
                destination.commit()
            finally:
                destination.close()
            source.rollback()
        finally:
            source.close()

        destination_inspection = inspect_database(staged_database, plugin_root)
        if source_inspection["logicalStateRoot"] != destination_inspection["logicalStateRoot"]:
            raise PostOfficeError(
                "PON_DATABASE_INVALID",
                "Database backup does not match the stable source snapshot",
                {
                    "sourceLogicalStateRoot": source_inspection["logicalStateRoot"],
                    "destinationLogicalStateRoot": destination_inspection["logicalStateRoot"],
                },
            )
        identity = {
            "schemaVersion": "1",
            "kind": "POST_OFFICE_NEXT_DATABASE_BACKUP",
            "createdAt": _timestamp(),
            "sourceDatabaseIdentityRoot": source_inspection["databaseIdentityRoot"],
            "sourceSnapshotRoot": source_inspection["logicalStateRoot"],
            "sourceLogicalContentsRoot": source_inspection["database"]["logicalContentsRoot"],
            "sourceEventCount": source_inspection["database"]["eventCount"],
            "sourceEventBoundary": source_inspection["database"]["eventBoundary"],
            "destinationLogicalStateRoot": destination_inspection["logicalStateRoot"],
            "destinationLogicalContentsRoot": destination_inspection["database"]["logicalContentsRoot"],
            "destinationEventBoundary": destination_inspection["database"]["eventBoundary"],
            "databaseUserVersion": destination_inspection["database"]["userVersion"],
            "destination": str(destination_path),
            "bytes": staged_database.stat().st_size,
            "sha256": sha256_file(staged_database),
            "quickCheck": destination_inspection["database"]["quickCheck"],
            "foreignKeyErrors": destination_inspection["database"]["foreignKeyErrors"],
        }
        receipt = {**identity, "receiptSha256": sha256_json(identity)}
        write_json(staged_receipt, receipt)
        publish_file_exclusive(staged_database, destination_path)
        destination_published = True
        publish_file_exclusive(staged_receipt, receipt_path)
        receipt_published = True
        if sha256_file(destination_path) != receipt["sha256"]:
            raise PostOfficeError("PON_DATABASE_INVALID", "Published backup hash changed", {"path": str(destination_path)})
        return {"ok": True, **receipt, "receipt": str(receipt_path)}
    except Exception:
        if receipt_published and os.path.lexists(receipt_path):
            receipt_path.unlink()
        if destination_published and os.path.lexists(destination_path):
            destination_path.unlink()
        raise
    finally:
        _remove_staged_database(staged_database)
        if os.path.lexists(staged_receipt):
            staged_receipt.unlink()


def restore_database(
    backup_path: Path,
    backup_receipt_path: Path,
    destination_path: Path,
    receipt_path: Path,
    plugin_root: Path | None = None,
) -> dict[str, Any]:
    backup_path = backup_path.resolve(strict=True)
    backup_receipt_path = backup_receipt_path.resolve(strict=True)
    destination_path = _validate_new_database_path(destination_path)
    receipt_path = require_outside_protected_roots(receipt_path)
    plugin_root = (plugin_root or Path(__file__).resolve().parents[2]).resolve(strict=True)
    require_new_output_file(
        destination_path,
        inputs=[backup_path, backup_receipt_path, receipt_path],
    )
    require_new_output_file(
        receipt_path,
        inputs=[backup_path, backup_receipt_path, destination_path],
    )
    source_receipt = read_json(backup_receipt_path)
    if not isinstance(source_receipt, dict) or source_receipt.get("kind") != "POST_OFFICE_NEXT_DATABASE_BACKUP":
        raise PostOfficeError(
            "PON_DATABASE_INVALID",
            "Backup receipt does not describe a Post Office Next database backup",
            {"receipt": str(backup_receipt_path)},
        )
    expected_hash = source_receipt.get("sha256")
    if not isinstance(expected_hash, str) or expected_hash != sha256_file(backup_path):
        raise PostOfficeError(
            "PON_DATABASE_INVALID",
            "Backup bytes do not match the backup receipt",
            {"backup": str(backup_path), "receipt": str(backup_receipt_path)},
        )
    source_inspection = inspect_database(backup_path, plugin_root)
    expected_root = source_receipt.get("destinationLogicalStateRoot")
    if expected_root != source_inspection["logicalStateRoot"]:
        raise PostOfficeError(
            "PON_DATABASE_INVALID",
            "Backup logical state does not match its receipt",
            {
                "expectedLogicalStateRoot": expected_root,
                "observedLogicalStateRoot": source_inspection["logicalStateRoot"],
            },
        )

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    staged_database = destination_path.with_name(f".{destination_path.name}.{uuid.uuid4().hex}.tmp")
    staged_receipt = receipt_path.with_name(f".{receipt_path.name}.{uuid.uuid4().hex}.tmp")
    destination_published = False
    receipt_published = False
    try:
        source = readonly_connection(backup_path)
        try:
            source.execute("BEGIN")
            destination = sqlite3.connect(staged_database)
            try:
                source.backup(destination)
                destination.commit()
            finally:
                destination.close()
            source.rollback()
        finally:
            if source.in_transaction:
                source.rollback()
            source.close()
        restored = inspect_database(staged_database, plugin_root)
        if restored["logicalStateRoot"] != source_inspection["logicalStateRoot"]:
            raise PostOfficeError(
                "PON_DATABASE_INVALID",
                "Restored database does not match the verified backup",
                {
                    "backupLogicalStateRoot": source_inspection["logicalStateRoot"],
                    "restoredLogicalStateRoot": restored["logicalStateRoot"],
                },
            )
        identity = {
            "schemaVersion": "1",
            "kind": "POST_OFFICE_NEXT_DATABASE_RESTORE",
            "createdAt": _timestamp(),
            "backupReceiptSha256": sha256_file(backup_receipt_path),
            "backupSha256": expected_hash,
            "sourceLogicalStateRoot": source_inspection["logicalStateRoot"],
            "destinationLogicalStateRoot": restored["logicalStateRoot"],
            "destinationLogicalContentsRoot": restored["database"]["logicalContentsRoot"],
            "destinationEventBoundary": restored["database"]["eventBoundary"],
            "destination": str(destination_path),
            "bytes": staged_database.stat().st_size,
            "sha256": sha256_file(staged_database),
            "quickCheck": restored["database"]["quickCheck"],
            "foreignKeyErrors": restored["database"]["foreignKeyErrors"],
        }
        receipt = {**identity, "receiptSha256": sha256_json(identity)}
        write_json(staged_receipt, receipt)
        publish_file_exclusive(staged_database, destination_path)
        destination_published = True
        publish_file_exclusive(staged_receipt, receipt_path)
        receipt_published = True
        if sha256_file(destination_path) != receipt["sha256"]:
            raise PostOfficeError(
                "PON_DATABASE_INVALID",
                "Published restored database hash changed",
                {"path": str(destination_path)},
            )
        return {"ok": True, **receipt, "receipt": str(receipt_path)}
    except Exception:
        if receipt_published and os.path.lexists(receipt_path):
            receipt_path.unlink()
        if destination_published and os.path.lexists(destination_path):
            destination_path.unlink()
        raise
    finally:
        _remove_staged_database(staged_database)
        if os.path.lexists(staged_receipt):
            staged_receipt.unlink()
