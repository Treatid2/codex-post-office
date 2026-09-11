# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from post_office.database import (  # noqa: E402
    APPLICATION_ID,
    backup_database,
    initialize_database,
    inspect_database,
    reattest_migration_history,
    restore_database,
)
from post_office.canonical import sha256_file  # noqa: E402
from post_office.diagnostics import PostOfficeError  # noqa: E402
from post_office.kernel import bootstrap_kernel, create_kernel_credential  # noqa: E402


def _insert_event_fixture(path: Path, event_id: str, event_sha256: str) -> None:
    digest0 = "0" * 64
    digest1 = "1" * 64
    digest2 = "2" * 64
    suffix = event_id.rsplit("-", 1)[-1]
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys=ON")
    try:
        con.execute("INSERT INTO actors VALUES(?,?,?,?,?)", (f"AUTHOR-{suffix}", "HUMAN", "author", "ACTIVE", "2026-09-04T12:00:00Z"))
        con.execute("INSERT INTO actors VALUES(?,?,?,?,?)", (f"WORKER-{suffix}", "ENDPOINT", "worker", "ACTIVE", "2026-09-04T12:00:00Z"))
        con.execute(
            "INSERT INTO exact_author_actions(action_id,actor_id,operation,scope_json,confirmation_sha256,recorded_at) VALUES(?,?,?,?,?,?)",
            (f"ACTION-{suffix}", f"AUTHOR-{suffix}", "authority.grant", '{}', digest0, "2026-09-04T12:00:00Z"),
        )
        con.execute(
            "INSERT INTO caller_capabilities VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (f"CAP-{suffix}", f"AUTHOR-{suffix}", "actor", f"AUTHOR-{suffix}", None, '["authority.grant"]', digest1,
             "ACTIVE", None, "2026-09-04T12:00:00Z", None),
        )
        con.execute(
            "INSERT INTO authority_grants(grant_id,grantor_actor_id,recipient_actor_id,allowed_operations_json,scope_json,classification,maximum_uses,remaining_uses,status,rationale,source_author_action_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"GRANT-{suffix}", f"AUTHOR-{suffix}", f"WORKER-{suffix}", '["task.create"]', '{}', "ONE_SHOT", 1, 1,
             "ACTIVE", "bounded", f"ACTION-{suffix}", "2026-09-04T12:00:00Z"),
        )
        con.execute(
            "INSERT INTO hub_events(sequence,event_id,occurred_at,actor_id,operation,capability_id,authority_grant_id,aggregate_type,aggregate_id,aggregate_version,before_state_root,after_state_root,result_json,payload_sha256,event_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (1, event_id, "2026-09-04T12:00:00Z", f"AUTHOR-{suffix}", "task.create", f"CAP-{suffix}", f"GRANT-{suffix}",
             "Task", f"TASK-{suffix}", 1, digest0, digest1, '{"ok":true}', digest2, event_sha256),
        )
        con.commit()
    finally:
        con.close()


class DatabaseFoundationTests(unittest.TestCase):
    def test_digest_only_migration_history_drift_can_be_reattested_with_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "post-office-next.sqlite3"
            backup = root / "pre-reattest.sqlite3"
            receipt = root / "reattest-receipt.json"
            initialize_database(PLUGIN_ROOT, database)
            credential = root / "author.json"
            create_kernel_credential(credential, "PON-CAPABILITY-REATTEST-AUTHOR")
            bootstrap_kernel(
                database,
                credential,
                actor_id="PON-ACTOR-REATTEST-AUTHOR",
                actor_kind="HUMAN",
                actor_role="author",
                mode="ISOLATED",
                plugin_root=PLUGIN_ROOT,
            )
            con = sqlite3.connect(database)
            try:
                con.execute("UPDATE schema_migrations SET sha256=? WHERE version=4", ("f" * 64,))
                con.commit()
            finally:
                con.close()
            with self.assertRaises(PostOfficeError):
                inspect_database(database)

            result = reattest_migration_history(
                database, backup, receipt, credential, "AUTHOR-TEST-REATTEST"
            )

            self.assertTrue(result["ok"])
            self.assertTrue(backup.is_file())
            self.assertTrue(receipt.is_file())
            self.assertEqual(result["exactAuthorActionId"], "AUTHOR-TEST-REATTEST")
            self.assertEqual(result["drift"][0]["version"], 4)
            self.assertEqual(result["backupSha256"], sha256_file(backup))
            self.assertTrue(inspect_database(database)["databaseIdentityRoot"])
            with self.assertRaises(PostOfficeError):
                inspect_database(backup)

    def test_migration_history_reattest_rejects_schema_drift_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "post-office-next.sqlite3"
            initialize_database(PLUGIN_ROOT, database)
            credential = root / "author.json"
            create_kernel_credential(credential, "PON-CAPABILITY-REATTEST-AUTHOR")
            bootstrap_kernel(
                database,
                credential,
                actor_id="PON-ACTOR-REATTEST-AUTHOR",
                actor_kind="HUMAN",
                actor_role="author",
                mode="ISOLATED",
                plugin_root=PLUGIN_ROOT,
            )
            con = sqlite3.connect(database)
            try:
                con.execute("UPDATE schema_migrations SET sha256=? WHERE version=4", ("f" * 64,))
                con.execute("CREATE TABLE unexpected_schema_drift(value TEXT) STRICT")
                con.commit()
            finally:
                con.close()
            before = database.read_bytes()
            with self.assertRaises(PostOfficeError) as raised:
                reattest_migration_history(
                    database,
                    root / "backup.sqlite3",
                    root / "receipt.json",
                    credential,
                    "AUTHOR-TEST-REJECT",
                )
            self.assertEqual(raised.exception.code, "PON_MIGRATION_MISMATCH")
            self.assertEqual(database.read_bytes(), before)
            self.assertFalse((root / "backup.sqlite3").exists())

    def test_initial_schema_is_transactional_integral_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "post-office-next.sqlite3"
            first = initialize_database(PLUGIN_ROOT, path)
            self.assertTrue(first["ok"])
            self.assertTrue(first["created"])
            self.assertEqual(first["journalMode"], "WAL")
            self.assertEqual(first["database"]["applicationId"], APPLICATION_ID)
            self.assertEqual(first["database"]["userVersion"], 9)
            self.assertEqual(first["database"]["quickCheck"], "ok")
            self.assertEqual(first["database"]["foreignKeyErrors"], [])
            self.assertIn("hub_events", first["database"]["tables"])
            self.assertIn("browser_bridge_transfers", first["database"]["tables"])

            second = initialize_database(PLUGIN_ROOT, path)
            self.assertFalse(second["created"])
            self.assertTrue(all(item["alreadyApplied"] for item in second["migrations"]))
            self.assertEqual(inspect_database(path)["database"]["userVersion"], 9)

    def test_drive_exists_only_in_transient_browser_bridge_table(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "post-office-next.sqlite3"
            initialize_database(PLUGIN_ROOT, path)
            con = sqlite3.connect(path)
            try:
                storage_sql = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='storage_copies'").fetchone()[0]
                bridge_sql = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='browser_bridge_transfers'").fetchone()[0]
            finally:
                con.close()
            self.assertNotIn("GOOGLE_DRIVE", storage_sql)
            self.assertIn("GOOGLE_DRIVE", bridge_sql)
            self.assertIn("durable_storage_copy_id", bridge_sql)
            con = sqlite3.connect(path)
            con.execute("PRAGMA foreign_keys=ON")
            try:
                with self.assertRaises(sqlite3.IntegrityError):
                    con.execute(
                        "INSERT INTO storage_copies VALUES(?,?,?,?,?,?,?,?)",
                        ("COPY-REMOTE", "a" * 64, "LOCAL_CAS", "https://drive.google.com/object", 1,
                         "AUTHORITATIVE", "SHA256_READBACK", "2026-09-04T12:00:00Z"),
                    )
                bridge_fks = list(con.execute("PRAGMA foreign_key_list(browser_bridge_transfers)"))
            finally:
                con.close()
            digest_fk = {(row[3], row[4]) for row in bridge_fks}
            self.assertIn(("expected_sha256", "content_sha256"), digest_fk)
            self.assertIn(("durable_storage_copy_id", "storage_copy_id"), digest_fk)

    def test_transactional_backup_is_integral_and_receipted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "post-office-next.sqlite3"
            backup = root / "backup.sqlite3"
            receipt = root / "backup-receipt.json"
            initialize_database(PLUGIN_ROOT, source)
            _insert_event_fixture(source, "EVENT-001", "1" * 64)
            result = backup_database(source, backup, receipt)
            self.assertTrue(result["ok"])
            self.assertTrue(backup.is_file())
            self.assertTrue(receipt.is_file())
            self.assertEqual(result["quickCheck"], "ok")
            self.assertEqual(result["foreignKeyErrors"], [])
            self.assertEqual(inspect_database(backup)["database"]["userVersion"], 9)
            self.assertEqual(result["sourceSnapshotRoot"], result["destinationLogicalStateRoot"])
            self.assertEqual(result["sourceLogicalContentsRoot"], result["destinationLogicalContentsRoot"])
            self.assertEqual(result["sourceEventBoundary"], result["destinationEventBoundary"])
            self.assertEqual(result["sourceEventCount"], 1)
            self.assertEqual(inspect_database(backup)["database"]["eventBoundary"]["eventId"], "EVENT-001")

    def test_verified_backup_restore_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "post-office-next.sqlite3"
            backup = root / "backup.sqlite3"
            backup_receipt = root / "backup-receipt.json"
            restored = root / "restored.sqlite3"
            restore_receipt = root / "restore-receipt.json"
            initialize_database(PLUGIN_ROOT, source)
            _insert_event_fixture(source, "EVENT-RESTORE", "5" * 64)
            original = inspect_database(source)
            backup_database(source, backup, backup_receipt)
            result = restore_database(backup, backup_receipt, restored, restore_receipt)
            self.assertTrue(result["ok"])
            self.assertEqual(result["sourceLogicalStateRoot"], original["logicalStateRoot"])
            self.assertEqual(result["destinationLogicalStateRoot"], original["logicalStateRoot"])
            self.assertEqual(inspect_database(restored)["logicalStateRoot"], original["logicalStateRoot"])

    def test_logical_state_distinguishes_equal_event_counts_with_different_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            left = root / "left.sqlite3"
            right = root / "right.sqlite3"
            initialize_database(PLUGIN_ROOT, left)
            backup_database(left, right, root / "seed-receipt.json")
            _insert_event_fixture(left, "EVENT-LEFT", "3" * 64)
            _insert_event_fixture(right, "EVENT-RIGHT", "4" * 64)
            left_identity = inspect_database(left)
            right_identity = inspect_database(right)
            self.assertEqual(left_identity["database"]["eventCount"], 1)
            self.assertEqual(right_identity["database"]["eventCount"], 1)
            self.assertNotEqual(left_identity["logicalStateRoot"], right_identity["logicalStateRoot"])
            self.assertNotEqual(left_identity["database"]["logicalContentsRoot"], right_identity["database"]["logicalContentsRoot"])
            self.assertEqual(left_identity["database"]["eventBoundary"]["eventId"], "EVENT-LEFT")
            self.assertEqual(right_identity["database"]["eventBoundary"]["eventId"], "EVENT-RIGHT")

    def test_database_and_backup_outputs_reject_protected_roots_before_writing(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.sqlite3"
            initialize_database(PLUGIN_ROOT, source)
            protected = root / "protected"
            with patch("post_office.canonical.PROTECTED_STATE_ROOTS", (protected,)):
                with self.assertRaises(PostOfficeError):
                    initialize_database(PLUGIN_ROOT, protected / "new" / "next.sqlite3")
                with self.assertRaises(PostOfficeError):
                    backup_database(source, protected / "backup" / "next.sqlite3", root / "receipt-one.json")
                with self.assertRaises(PostOfficeError):
                    backup_database(source, root / "backup-two.sqlite3", protected / "receipts" / "next.json")
            self.assertFalse(protected.exists())
            self.assertFalse((root / "receipt-one.json").exists())
            self.assertFalse((root / "backup-two.sqlite3").exists())

    def test_unrelated_existing_database_is_rejected_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "legacy.sqlite3"
            con = sqlite3.connect(path)
            con.execute("CREATE TABLE legacy_state(id INTEGER PRIMARY KEY, value TEXT)")
            con.execute("INSERT INTO legacy_state(value) VALUES('keep')")
            con.commit()
            con.close()
            before = path.read_bytes()
            before_stat = path.stat()
            before_names = sorted(item.name for item in root.iterdir())
            with self.assertRaises(PostOfficeError):
                initialize_database(PLUGIN_ROOT, path)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(path.stat().st_mtime_ns, before_stat.st_mtime_ns)
            self.assertEqual(sorted(item.name for item in root.iterdir()), before_names)

    def test_empty_existing_and_reserved_legacy_paths_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            empty = root / "empty.sqlite3"
            empty.write_bytes(b"")
            with self.assertRaises(PostOfficeError):
                initialize_database(PLUGIN_ROOT, empty)
            self.assertEqual(empty.read_bytes(), b"")
            reserved = root / "hub.sqlite3"
            with self.assertRaises(PostOfficeError):
                initialize_database(PLUGIN_ROOT, reserved)
            self.assertFalse(reserved.exists())

    def test_schema_drift_and_extra_migration_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            drifted = root / "post-office-next.sqlite3"
            initialize_database(PLUGIN_ROOT, drifted)
            con = sqlite3.connect(drifted)
            con.execute("DROP TRIGGER hub_events_no_delete")
            con.commit()
            con.close()
            drifted_bytes = drifted.read_bytes()
            with self.assertRaises(PostOfficeError):
                inspect_database(drifted)
            with self.assertRaises(PostOfficeError):
                initialize_database(PLUGIN_ROOT, drifted)
            self.assertEqual(drifted.read_bytes(), drifted_bytes)

            extra = root / "post-office-next-extra.sqlite3"
            initialize_database(PLUGIN_ROOT, extra)
            con = sqlite3.connect(extra)
            try:
                con.execute(
                    "INSERT INTO schema_migrations(version,name,sha256,applied_at) VALUES(10,'unknown',?,'2026-09-04T12:00:00Z')",
                    ("f" * 64,),
                )
                con.execute("PRAGMA user_version=10")
                con.commit()
            finally:
                con.close()
            with self.assertRaises(PostOfficeError):
                inspect_database(extra)

    def test_backup_rejects_every_source_destination_receipt_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "post-office-next.sqlite3"
            initialize_database(PLUGIN_ROOT, source)
            source_bytes = source.read_bytes()
            with self.assertRaises(PostOfficeError):
                backup_database(source, root / "backup.sqlite3", source)
            self.assertEqual(source.read_bytes(), source_bytes)
            same_output = root / "same-output"
            with self.assertRaises(PostOfficeError):
                backup_database(source, same_output, same_output)
            self.assertFalse(same_output.exists())

    def test_event_journal_requires_authority_and_is_append_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "post-office-next.sqlite3"
            initialize_database(PLUGIN_ROOT, path)
            digest0 = "0" * 64
            digest1 = "1" * 64
            digest2 = "2" * 64
            con = sqlite3.connect(path)
            con.execute("PRAGMA foreign_keys=ON")
            try:
                con.execute("INSERT INTO actors VALUES(?,?,?,?,?)", ("AUTHOR-001", "HUMAN", "author", "ACTIVE", "2026-09-04T12:00:00Z"))
                con.execute("INSERT INTO actors VALUES(?,?,?,?,?)", ("WORKER-001", "ENDPOINT", "worker", "ACTIVE", "2026-09-04T12:00:00Z"))
                con.execute(
                    "INSERT INTO exact_author_actions(action_id,actor_id,operation,scope_json,confirmation_sha256,recorded_at) VALUES(?,?,?,?,?,?)",
                    ("ACTION-001", "AUTHOR-001", "authority.grant", '{}', digest0, "2026-09-04T12:00:00Z"),
                )
                con.execute(
                    "INSERT INTO caller_capabilities VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    ("CAP-001", "AUTHOR-001", "actor", "AUTHOR-001", None, '["authority.grant"]', digest1,
                     "ACTIVE", None, "2026-09-04T12:00:00Z", None),
                )
                con.execute(
                    "INSERT INTO authority_grants(grant_id,grantor_actor_id,recipient_actor_id,allowed_operations_json,scope_json,classification,maximum_uses,remaining_uses,status,rationale,source_author_action_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("GRANT-001", "AUTHOR-001", "WORKER-001", '["task.create"]', '{}', "ONE_SHOT", 1, 1,
                     "ACTIVE", "bounded", "ACTION-001", "2026-09-04T12:00:00Z"),
                )
                con.execute(
                    "INSERT INTO hub_events(sequence,event_id,occurred_at,actor_id,operation,capability_id,authority_grant_id,aggregate_type,aggregate_id,aggregate_version,before_state_root,after_state_root,result_json,payload_sha256,event_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (1, "EVENT-001", "2026-09-04T12:00:00Z", "AUTHOR-001", "task.create", "CAP-001", "GRANT-001",
                     "Task", "TASK-001", 1, digest0, digest1, '{"ok":true}', digest2, digest1),
                )
                con.commit()
                with self.assertRaises(sqlite3.IntegrityError):
                    con.execute("UPDATE hub_events SET operation='task.close' WHERE event_id='EVENT-001'")
                with self.assertRaises(sqlite3.IntegrityError):
                    con.execute("DELETE FROM hub_events WHERE event_id='EVENT-001'")
                with self.assertRaises(sqlite3.IntegrityError):
                    con.execute(
                        "INSERT INTO hub_events(sequence,event_id,occurred_at,actor_id,operation,capability_id,aggregate_type,aggregate_id,aggregate_version,before_state_root,after_state_root,result_json,payload_sha256,event_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (2, "EVENT-002", "2026-09-04T12:01:00Z", "AUTHOR-001", "task.create", "CAP-001", "Task",
                         "TASK-002", 1, digest0, digest1, '{}', digest2, digest2),
                    )
            finally:
                con.close()


if __name__ == "__main__":
    unittest.main()
