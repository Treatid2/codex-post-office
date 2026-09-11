# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import os
import re
import sqlite3
import uuid
import hmac
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .canonical import (
    publish_file_exclusive,
    read_json,
    require_new_output_file,
    require_outside_protected_roots,
    sha256_file,
    sha256_bytes,
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


def _migration_history_reattest_preflight(
    con: sqlite3.Connection,
    database_path: Path,
    plugin_root: Path,
) -> dict[str, Any]:
    """Validate the narrow exceptional case where only stored migration digests drifted."""
    expected = _expected_migrations(plugin_root)
    tables = _user_tables(con)
    if "schema_migrations" not in tables:
        raise PostOfficeError(
            "PON_MIGRATION_MISMATCH",
            "Database has no migration history to re-attest",
            {"path": str(database_path)},
        )
    actual = [
        {"version": int(row[0]), "name": str(row[1]), "sha256": str(row[2]), "appliedAt": str(row[3])}
        for row in con.execute("SELECT version,name,sha256,applied_at FROM schema_migrations ORDER BY version")
    ]
    actual_names = [(item["version"], item["name"]) for item in actual]
    expected_names = [(item["version"], item["name"]) for item in expected]
    failures: list[str] = []
    if int(con.execute("PRAGMA application_id").fetchone()[0]) != APPLICATION_ID:
        failures.append("application_id")
    if int(con.execute("PRAGMA user_version").fetchone()[0]) != expected[-1]["version"]:
        failures.append("user_version")
    if actual_names != expected_names:
        failures.append("migration_versions_or_names")
    if _schema_objects(con) != _expected_schema(plugin_root):
        failures.append("schema_objects")
    if str(con.execute("PRAGMA journal_mode").fetchone()[0]).upper() != "WAL":
        failures.append("journal_mode")
    if str(con.execute("PRAGMA quick_check(1)").fetchone()[0]) != "ok":
        failures.append("quick_check")
    if list(con.execute("PRAGMA foreign_key_check")):
        failures.append("foreign_keys")
    if failures:
        raise PostOfficeError(
            "PON_MIGRATION_MISMATCH",
            "Migration history cannot be re-attested because database identity differs beyond stored digests",
            {"path": str(database_path), "identityFailures": failures},
        )
    expected_by_version = {item["version"]: item for item in expected}
    drift = [
        {
            "version": item["version"],
            "name": item["name"],
            "storedSha256": item["sha256"],
            "expectedSha256": expected_by_version[item["version"]]["sha256"],
        }
        for item in actual
        if item["sha256"] != expected_by_version[item["version"]]["sha256"]
    ]
    migration_paths = {version: path for version, _, path in _migration_files(plugin_root)}
    for item in drift:
        sql = migration_paths[item["version"]].read_text(encoding="utf-8")
        without_comments = re.sub(r"--[^\n]*", "", sql)
        if re.search(r"\b(INSERT|UPDATE|DELETE|REPLACE|DROP)\b", without_comments, re.IGNORECASE):
            raise PostOfficeError(
                "PON_MIGRATION_MISMATCH",
                "A drifted migration contains data-changing or destructive SQL and cannot be digest-re-attested",
                {"version": item["version"], "name": item["name"]},
            )
    content = sqlite_content_identity(con)
    return {
        "actual": actual,
        "expected": expected,
        "drift": drift,
        "schemaObjectsRoot": sha256_json(_schema_objects(con)),
        "logicalContentsRoot": content["contentsRoot"],
        "eventBoundary": content["eventBoundary"],
    }


def reattest_migration_history(
    database_path: Path,
    backup_path: Path,
    receipt_path: Path,
    credential_path: Path,
    exact_author_action_id: str,
    plugin_root: Path | None = None,
) -> dict[str, Any]:
    """Repair digest-only migration drift after an exact-schema verification and retained backup."""
    database_path = database_path.resolve(strict=True)
    backup_path = backup_path.resolve(strict=False)
    receipt_path = receipt_path.resolve(strict=False)
    plugin_root = (plugin_root or Path(__file__).resolve().parents[2]).resolve(strict=True)
    if not isinstance(exact_author_action_id, str) or not exact_author_action_id.strip():
        raise PostOfficeError("PON_INPUT_INVALID", "Migration re-attestation requires an exact author action ID", {})
    require_new_output_file(backup_path, inputs=[database_path, receipt_path])
    require_new_output_file(receipt_path, inputs=[database_path, backup_path])
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    staged_backup = backup_path.with_name(f".{backup_path.name}.{uuid.uuid4().hex}.tmp")
    staged_receipt = receipt_path.with_name(f".{receipt_path.name}.{uuid.uuid4().hex}.tmp")
    backup_published = False
    receipt_published = False
    try:
        source = sqlite3.connect(database_path, timeout=30)
        source.row_factory = sqlite3.Row
        try:
            source.execute("PRAGMA foreign_keys=ON")
            source.execute("BEGIN")
            from .kernel import _credential

            credential = _credential(credential_path)
            instance = source.execute("SELECT * FROM kernel_instances WHERE instance_id='PON-KERNEL'").fetchone()
            capability = source.execute(
                "SELECT * FROM caller_capabilities WHERE capability_id=?", (credential["capabilityId"],)
            ).fetchone()
            actor = source.execute(
                "SELECT * FROM actors WHERE actor_id=?", (capability["actor_id"],)
            ).fetchone() if capability else None
            if (
                not instance
                or credential["capabilityId"] != instance["bootstrap_capability_id"]
                or not capability
                or capability["status"] != "ACTIVE"
                or not hmac.compare_digest(
                    str(capability["secret_sha256"]), sha256_bytes(credential["secret"].encode("utf-8"))
                )
                or (
                    capability["expires_at"]
                    and datetime.fromisoformat(
                        str(capability["expires_at"]).replace("Z", "+00:00")
                    ) <= datetime.now(timezone.utc)
                )
                or not actor
                or actor["actor_id"] != instance["bootstrap_actor_id"]
                or actor["actor_kind"] != "HUMAN"
                or actor["status"] != "ACTIVE"
            ):
                raise PostOfficeError(
                    "PON_AUTHENTICATION_FAILED",
                    "Migration history re-attestation requires the active bootstrap author credential",
                    {},
                )
            before = _migration_history_reattest_preflight(source, database_path, plugin_root)
            if not before["drift"]:
                raise PostOfficeError(
                    "PON_CONCURRENCY_CONFLICT",
                    "Migration history already matches the registered source",
                    {"path": str(database_path)},
                )
            destination = sqlite3.connect(staged_backup)
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
        backup_check = sqlite3.connect(staged_backup)
        try:
            backup_check.execute("PRAGMA foreign_keys=ON")
            retained_before = _migration_history_reattest_preflight(backup_check, staged_backup, plugin_root)
        finally:
            backup_check.close()
        if retained_before["actual"] != before["actual"] or retained_before["logicalContentsRoot"] != before["logicalContentsRoot"]:
            raise PostOfficeError("PON_DATABASE_INVALID", "Migration repair backup does not match the source snapshot", {})
        publish_file_exclusive(staged_backup, backup_path)
        backup_published = True

        writer = sqlite3.connect(database_path, timeout=30)
        writer.row_factory = sqlite3.Row
        try:
            writer.execute("PRAGMA foreign_keys=ON")
            writer.execute("BEGIN IMMEDIATE")
            if writer.execute(
                "SELECT 1 FROM authority_transfers WHERE state='PREPARED' LIMIT 1"
            ).fetchone():
                raise PostOfficeError(
                    "PON_CONCURRENCY_CONFLICT",
                    "A prepared authority transfer fences migration-history writes",
                    {"reason": "PREPARED_CUTOVER_FENCE"},
                )
            locked = _migration_history_reattest_preflight(writer, database_path, plugin_root)
            if locked["actual"] != before["actual"] or locked["logicalContentsRoot"] != before["logicalContentsRoot"]:
                raise PostOfficeError(
                    "PON_CONCURRENCY_CONFLICT",
                    "Database changed after the retained repair backup was taken",
                    {},
                )
            for item in locked["drift"]:
                writer.execute(
                    "UPDATE schema_migrations SET sha256=? WHERE version=? AND name=? AND sha256=?",
                    (item["expectedSha256"], item["version"], item["name"], item["storedSha256"]),
                )
                if writer.execute("SELECT changes()").fetchone()[0] != 1:
                    raise PostOfficeError("PON_CONCURRENCY_CONFLICT", "Migration history changed during re-attestation", {})
            writer.commit()
            writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            if writer.in_transaction:
                writer.rollback()
            raise
        finally:
            writer.close()

        after = inspect_database(database_path, plugin_root)
        identity = {
            "schemaVersion": "1",
            "kind": "POST_OFFICE_NEXT_MIGRATION_HISTORY_REATTESTATION",
            "database": str(database_path),
            "backup": str(backup_path),
            "backupBytes": backup_path.stat().st_size,
            "backupSha256": sha256_file(backup_path),
            "exactAuthorActionId": exact_author_action_id.strip(),
            "drift": before["drift"],
            "beforeLogicalContentsRoot": before["logicalContentsRoot"],
            "afterLogicalContentsRoot": after["database"]["logicalContentsRoot"],
            "eventBoundary": after["database"]["eventBoundary"],
            "schemaObjectsRoot": after["database"]["schemaObjectsRoot"],
            "databaseIdentityRoot": after["databaseIdentityRoot"],
            "recordedAt": _timestamp(),
        }
        receipt = {**identity, "receiptSha256": sha256_json(identity)}
        write_json(staged_receipt, receipt)
        publish_file_exclusive(staged_receipt, receipt_path)
        receipt_published = True
        return {"ok": True, **receipt}
    finally:
        if not backup_published:
            _remove_staged_database(staged_backup)
        if not receipt_published and os.path.lexists(staged_receipt):
            staged_receipt.unlink()


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
