# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from post_office.canonical import read_json, sha256_file, sha256_json  # noqa: E402
from post_office.benchmark import performance_baseline  # noqa: E402
from post_office.contract_specs import build_entity_schemas  # noqa: E402
from post_office.diagnostics import PostOfficeError  # noqa: E402
from post_office.legacy import capture_state, source_manifest  # noqa: E402
import post_office.legacy as legacy_module  # noqa: E402
from post_office.mini_schema import validate  # noqa: E402
from post_office.reconciliation import reconcile_preview  # noqa: E402
from post_office.snapshot import create_snapshot  # noqa: E402


def _digest_tree(root: Path) -> list[tuple[str, int, str]]:
    return [
        (path.relative_to(root).as_posix(), path.stat().st_size, sha256_file(path))
        for path in sorted((item for item in root.rglob("*") if item.is_file()))
    ]


def _create_legacy_state(root: Path, *, wal: bool = False) -> None:
    root.mkdir()
    (root / "payloads").mkdir()
    (root / "caller-secrets").mkdir()
    (root / "browser-observer").mkdir()
    (root / "payloads" / "one.bin").write_bytes(b"same content")
    (root / "payloads" / "two.bin").write_bytes(b"same content")
    (root / "events.jsonl").write_text("{}\n", encoding="utf-8")
    (root / "caller-secrets" / "task.token").write_text("never-copy-this-token", encoding="utf-8")

    con = sqlite3.connect(root / "hub.sqlite3")
    try:
        if wal:
            con.execute("PRAGMA journal_mode=WAL")
        con.executescript(
            """
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT INTO metadata VALUES('schema_version','12');
            INSERT INTO metadata VALUES('auto_review_schema_version','10');
            CREATE TABLE mailboxes(mailbox_id TEXT PRIMARY KEY,project_code TEXT,label TEXT,kind TEXT,status TEXT,generation INTEGER,thread_id TEXT,host_id TEXT,project_id TEXT,expected_root TEXT,expires_at TEXT,created_at TEXT,updated_at TEXT);
            CREATE TABLE messages(message_id TEXT PRIMARY KEY,cycle_id TEXT,root_cycle_id TEXT,project_code TEXT,sender_mailbox_id TEXT,sender_generation INTEGER,recipient_mailbox_id TEXT,recipient_generation INTEGER,message_type TEXT,authorization_class TEXT,subject TEXT,body TEXT,requested_action TEXT,completion_criteria TEXT,status TEXT,parent_message_id TEXT,may_delegate INTEGER,max_depth INTEGER,current_depth INTEGER,expires_at TEXT,created_at TEXT,updated_at TEXT,completed_summary TEXT,unresolved TEXT,acknowledgement_evidence TEXT);
            CREATE TABLE message_payloads(message_id TEXT,ordinal INTEGER,source_name TEXT,stored_path TEXT,size_bytes INTEGER,sha256 TEXT);
            CREATE TABLE browser_outbox(outbox_id TEXT PRIMARY KEY,project_code TEXT,browser_mailbox_id TEXT,browser_generation INTEGER,destination TEXT,subject TEXT,package_name TEXT,expected_sha256 TEXT,delivered_sha256 TEXT,status TEXT,created_at TEXT,updated_at TEXT);
            CREATE TABLE browser_deliveries(delivery_id TEXT PRIMARY KEY,message_id TEXT,payload_ordinal INTEGER,sha256 TEXT,drive_sha256 TEXT);
            CREATE TABLE browser_chat_bindings(mailbox_id TEXT PRIMARY KEY,mailbox_generation INTEGER,chat_thread_id TEXT,chat_url TEXT,browser_family TEXT,status TEXT,created_at TEXT,updated_at TEXT);
            CREATE TABLE browser_poke_queue(poke_id TEXT PRIMARY KEY,message_id TEXT,browser_mailbox_id TEXT,browser_generation INTEGER,status TEXT,created_at TEXT,updated_at TEXT);
            CREATE TABLE wake_authorizations(authorization_id TEXT PRIMARY KEY,message_id TEXT,courier_mailbox_id TEXT,sender_mailbox_id TEXT,recipient_mailbox_id TEXT,recipient_generation INTEGER,author_confirmation TEXT,status TEXT,idempotency_key TEXT,created_at TEXT,updated_at TEXT,packet_issued_at TEXT);
            CREATE TABLE caller_capabilities(capability_id TEXT PRIMARY KEY,subject_kind TEXT,subject_id TEXT,subject_generation INTEGER,thread_id TEXT,host_id TEXT,operations_json TEXT,secret_sha256 TEXT,status TEXT,expires_at TEXT,created_at TEXT,revoked_at TEXT);
            CREATE TABLE auto_reviews(review_id TEXT PRIMARY KEY,status TEXT);
            CREATE TABLE events(sequence INTEGER PRIMARY KEY,event_id TEXT,event_type TEXT,actor TEXT,object_id TEXT,occurred_at TEXT,data_json TEXT,previous_hash TEXT,event_hash TEXT);
            """
        )
        con.execute("INSERT INTO mailboxes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", ("FGPM-MBX-001", "FGPM", "worker", "CODEX", "ACTIVE", 1, "thread-1", "local", "project-1", None, None, "2026-09-04T12:00:00Z", "2026-09-04T12:00:00Z"))
        con.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("FGPM-MSG-001", "FGPM-CYCLE-000013", "FGPM-CYCLE-000013", "FGPM", "FGPM-MBX-001", 1, "FGPM-MBX-001", 1, "RESPONSE", "REPORT", "Response for FGPM-CYCLE-000012", "body", "inspect", "report", "DELIVERED", None, 0, 0, 0, None, "2026-09-04T12:00:00Z", "2026-09-04T12:00:00Z", "open assertion", None, None))
        payload_hash = hashlib.sha256(b"same content").hexdigest()
        con.execute("INSERT INTO message_payloads VALUES(?,?,?,?,?,?)", ("FGPM-MSG-001", 0, "one.bin", str(root / "payloads" / "one.bin"), 12, payload_hash))
        con.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)", (1, "FGPM-EVT-001", "MESSAGE_POSTED", "system", "FGPM-MSG-001", "2026-09-04T12:00:00Z", "{}", "0" * 64, "1" * 64))
        con.commit()
    finally:
        con.close()
    con = sqlite3.connect(root / "browser-observer" / "observer.sqlite3")
    try:
        if wal:
            con.execute("PRAGMA journal_mode=WAL")
        con.executescript(
            """
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT INTO metadata VALUES('schema_version','1');
            CREATE TABLE thread_state(thread_id TEXT PRIMARY KEY,last_seen_at TEXT,last_event_id INTEGER,last_signature TEXT,last_role TEXT,last_message_id TEXT,last_user_message_id TEXT,last_assistant_message_id TEXT,unanswered INTEGER,dirty INTEGER,dirty_since TEXT,acknowledged_event_id INTEGER,acknowledged_at TEXT,url TEXT,tab_id INTEGER,window_id INTEGER);
            CREATE TABLE events(id INTEGER PRIMARY KEY,event_hash TEXT);
            """
        )
        con.commit()
    finally:
        con.close()


class SnapshotTests(unittest.TestCase):
    def test_wal_sources_publish_single_file_database_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            capture = root / "capture"
            _create_legacy_state(state, wal=True)
            capture_state(state, capture)
            self.assertFalse((capture / "hub.sqlite3-wal").exists())
            self.assertFalse((capture / "hub.sqlite3-shm").exists())
            self.assertFalse((capture / "observer.sqlite3-wal").exists())
            self.assertFalse((capture / "observer.sqlite3-shm").exists())
            result = create_snapshot(capture, root / "snapshot.json")
            self.assertTrue(result["ok"])

    def test_all_output_entry_points_reject_protected_roots_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            capture = root / "capture"
            snapshot_path = root / "snapshot.json"
            archive = root / "legacy.zip"
            _create_legacy_state(state)
            capture_state(state, capture)
            create_snapshot(capture, snapshot_path)
            archive.write_bytes(b"archive")
            protected = root / "protected"
            with patch("post_office.canonical.PROTECTED_STATE_ROOTS", (protected,)):
                attempts = [
                    lambda: capture_state(state, protected / "capture-output"),
                    lambda: create_snapshot(capture, protected / "snapshot" / "result.json"),
                    lambda: reconcile_preview(snapshot_path, None, protected / "reconcile" / "result.json"),
                    lambda: performance_baseline(PLUGIN_ROOT, protected / "benchmark" / "result.json", 3),
                    lambda: source_manifest(state, archive, protected / "source" / "result.json"),
                ]
                for attempt in attempts:
                    with self.assertRaises(PostOfficeError) as raised:
                        attempt()
                    self.assertEqual(raised.exception.code, "PON_PRODUCTION_MUTATION_FORBIDDEN")
            self.assertFalse(protected.exists())

    def test_capture_rejects_nested_output_before_writing_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            _create_legacy_state(state)
            before = _digest_tree(state)
            nested = state / "capture"
            with self.assertRaises(PostOfficeError):
                capture_state(state, nested)
            self.assertEqual(_digest_tree(state), before)
            self.assertFalse(nested.exists())

    def test_case_variant_secret_directory_is_never_inventoried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            _create_legacy_state(state)
            intermediate = state / "secret-case-transition"
            (state / "caller-secrets").rename(intermediate)
            mixed = state / "Caller-Secrets"
            intermediate.rename(mixed)
            (mixed / "other.token").write_text("also-secret", encoding="utf-8")
            capture = root / "capture"
            capture_state(state, capture)
            manifest = read_json(capture / "capture-manifest.json")
            paths = [item["path"].casefold() for item in manifest["files"]]
            self.assertFalse(any(path.startswith("caller-secrets/") for path in paths))
            self.assertNotIn("also-secret", (capture / "capture-manifest.json").read_text(encoding="utf-8"))

    def test_capture_is_read_only_and_snapshot_is_repeatable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            state = base / "state"
            capture = base / "capture"
            _create_legacy_state(state)
            before = _digest_tree(state)
            capture_result = capture_state(state, capture)
            after = _digest_tree(state)
            self.assertEqual(before, after)
            self.assertEqual(capture_result["sourceAccess"], "SQLITE_MODE_RO_QUERY_ONLY_AND_FILESYSTEM_READ_ONLY")
            capture_manifest = read_json(capture / "capture-manifest.json")
            manifest_text = (capture / "capture-manifest.json").read_text(encoding="utf-8")
            self.assertNotIn("never-copy-this-token", manifest_text)
            self.assertFalse(any(item["path"].startswith("caller-secrets/") for item in capture_manifest["files"]))

            first = base / "snapshot-1.json"
            second = base / "snapshot-2.json"
            first_result = create_snapshot(capture, first)
            second_result = create_snapshot(capture, second)
            self.assertEqual(first_result["snapshotRoot"], second_result["snapshotRoot"])
            self.assertEqual(first.read_bytes(), second.read_bytes())
            snapshot = read_json(first)
            self.assertEqual(snapshot["hub"]["domains"], ["FGPM"])
            self.assertEqual(snapshot["hub"]["semanticCycles"][0]["semanticState"], "UNRESOLVED_REQUIRES_EXACT_AUTHORITY_EVIDENCE")

    def test_conflicting_cycle_assertions_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            state = base / "state"
            capture = base / "capture"
            _create_legacy_state(state)
            capture_state(state, capture)
            snapshot_path = base / "snapshot.json"
            create_snapshot(capture, snapshot_path)
            output = base / "preview.json"
            reconcile_preview(snapshot_path, PLUGIN_ROOT / "tests" / "fixtures" / "fgpm-cycle-000013.assertions.json", output)
            preview = json.loads(output.read_text(encoding="utf-8"))
            conflict = next(item for item in preview["discrepancies"] if item["entityId"] == "FGPM-CYCLE-000013")
            self.assertEqual(conflict["classification"], "AMBIGUOUS_AUTHORITY")
            self.assertFalse(conflict["automaticRepairEligible"])
            self.assertFalse(conflict["mayReopenSemanticCycle"])
            cycle_label = next(item for item in preview["discrepancies"] if item["classification"] == "CYCLE_LABEL_MISMATCH_CANDIDATE")
            self.assertEqual(cycle_label["entityId"], "FGPM-MSG-001")
            self.assertFalse(preview["applyAvailable"])
            self.assertEqual(preview["invariants"]["repairsPerformed"], 0)
            classifications = {item["classification"] for item in preview["discrepancies"]}
            self.assertIn("MIGRATION_EVIDENCE_GAP", classifications)
            self.assertIn("ILLEGAL_STATE_TRANSITION", preview["classificationCoverage"]["deferredUntilVNextEventReplay"])

    def test_external_migration_evidence_is_captured_without_network_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            state = base / "state"
            capture = base / "capture"
            _create_legacy_state(state)
            register = base / "project-register.json"
            archive = base / "legacy-archive.zip"
            register.write_text('{"project":"FGPM"}\n', encoding="utf-8")
            archive.write_bytes(b"legacy archive evidence")
            manifest = base / "migration-evidence.json"
            manifest.write_text(json.dumps({
                "schemaVersion": "1",
                "records": [
                    {"kind": "PROJECT_REGISTER", "logicalId": "FGPM-REGISTER-001", "sourcePath": str(register),
                     "relatedEntityType": "Project", "relatedEntityId": "FGPM"},
                    {"kind": "LOCAL_ARCHIVE", "logicalId": "FGPM-ARCHIVE-001", "sourcePath": str(archive),
                     "relatedEntityType": "Project", "relatedEntityId": "FGPM"},
                    {"kind": "LEGACY_DRIVE_REFERENCE", "logicalId": "FGPM-DRIVE-001",
                     "reference": "drive-object-id-from-existing-receipt"},
                ],
            }), encoding="utf-8")

            result = capture_state(state, capture, manifest)
            self.assertEqual(result["externalEvidenceCount"], 3)
            captured = read_json(capture / "capture-manifest.json")["externalEvidence"]
            self.assertEqual(captured["networkAccess"], "NONE")
            self.assertEqual(len(captured["records"]), 3)
            self.assertTrue(all((capture / item["capturedPath"]).is_file() for item in captured["records"] if item.get("capturedPath")))

            snapshot_path = base / "snapshot.json"
            snapshot_result = create_snapshot(capture, snapshot_path)
            self.assertEqual(snapshot_result["counts"]["migrationEvidence"], 3)
            snapshot = read_json(snapshot_path)
            self.assertEqual(snapshot["migrationEvidence"]["networkAccess"], "NONE")

    def test_database_only_source_changes_abort_capture_without_publication(self) -> None:
        for database_name in ("hub", "observer"):
            with self.subTest(database=database_name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                state = root / "state"
                capture = root / "capture"
                _create_legacy_state(state)
                original = legacy_module._capture_external_evidence

                def mutate_after_database_copies(manifest_path: Path | None, working_root: Path) -> dict[str, object]:
                    result = original(manifest_path, working_root)
                    database = state / "hub.sqlite3" if database_name == "hub" else state / "browser-observer" / "observer.sqlite3"
                    con = sqlite3.connect(database)
                    try:
                        if database_name == "hub":
                            con.execute(
                                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)",
                                (2, "FGPM-EVT-002", "MESSAGE_ACKNOWLEDGED", "system", "FGPM-MSG-001",
                                 "2026-09-04T12:01:00Z", "{}", "1" * 64, "2" * 64),
                            )
                        else:
                            con.execute("INSERT INTO events VALUES(?,?)", (1, "3" * 64))
                        con.commit()
                    finally:
                        con.close()
                    return result

                with patch.object(legacy_module, "_capture_external_evidence", side_effect=mutate_after_database_copies):
                    with self.assertRaises(PostOfficeError) as raised:
                        capture_state(state, capture)
                self.assertEqual(raised.exception.code, "PON_SNAPSHOT_SOURCE_CHANGED")
                self.assertFalse(capture.exists())
                self.assertFalse(any(path.name.startswith(".capture.") for path in root.iterdir()))

    def test_snapshot_rejects_incomplete_local_evidence_and_reconciliation_cannot_count_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            capture = root / "capture"
            _create_legacy_state(state)
            register = root / "project-register.json"
            register.write_text('{"project":"FGPM"}\n', encoding="utf-8")
            evidence_manifest = root / "migration-evidence.json"
            evidence_manifest.write_text(json.dumps({
                "schemaVersion": "1",
                "records": [{"kind": "PROJECT_REGISTER", "logicalId": "FGPM-REGISTER-001", "sourcePath": str(register)}],
            }), encoding="utf-8")
            capture_state(state, capture, evidence_manifest)
            manifest_path = capture / "capture-manifest.json"
            original = read_json(manifest_path)
            identity_keys = [
                "schemaVersion", "sourceStateRoot", "legacySchemaVersions", "databases", "files",
                "externalEvidence", "excluded",
            ]

            captured_member = capture / original["externalEvidence"]["records"][0]["capturedPath"]
            original_bytes = captured_member.read_bytes()
            captured_member.write_bytes(b"tampered")
            with self.assertRaises(PostOfficeError):
                create_snapshot(capture, root / "tampered-evidence-snapshot.json")
            captured_member.write_bytes(original_bytes)

            malformed_records = []
            missing_custody = json.loads(json.dumps(original["externalEvidence"]["records"][0]))
            missing_custody.pop("capturedPath")
            malformed_records.append(missing_custody)
            unknown_kind = json.loads(json.dumps(original["externalEvidence"]["records"][0]))
            unknown_kind["kind"] = "UNKNOWN_KIND"
            malformed_records.append(unknown_kind)
            remote_without_reference = {
                "kind": "LEGACY_DRIVE_REFERENCE",
                "logicalId": "FGPM-REMOTE-001",
                "relatedEntityType": None,
                "relatedEntityId": None,
                "reference": None,
            }
            malformed_records.append(remote_without_reference)
            for index, malformed_record in enumerate(malformed_records):
                malformed = json.loads(json.dumps(original))
                malformed["externalEvidence"]["records"] = [malformed_record]
                malformed["captureRoot"] = sha256_json({key: malformed[key] for key in identity_keys})
                manifest_path.write_text(json.dumps(malformed), encoding="utf-8")
                output = root / f"invalid-evidence-snapshot-{index}.json"
                with self.assertRaises(PostOfficeError):
                    create_snapshot(capture, output)
                self.assertFalse(output.exists())

            manifest_path.write_text(json.dumps(original), encoding="utf-8")
            snapshot_path = root / "snapshot.json"
            create_snapshot(capture, snapshot_path)
            tampered_snapshot = read_json(snapshot_path)
            tampered_snapshot["migrationEvidence"]["records"][0].pop("capturedPath")
            tampered_snapshot["snapshotRoot"] = sha256_json({
                key: value for key, value in tampered_snapshot.items() if key != "snapshotRoot"
            })
            tampered_path = root / "tampered-snapshot.json"
            tampered_path.write_text(json.dumps(tampered_snapshot), encoding="utf-8")
            with self.assertRaises(PostOfficeError):
                reconcile_preview(tampered_path, None, root / "invalid-preview.json")
            self.assertFalse((root / "invalid-preview.json").exists())

    def test_manifest_and_source_links_are_rejected_before_hashing_or_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            capture = root / "capture"
            _create_legacy_state(state)
            capture_state(state, capture)
            manifest_path = capture / "capture-manifest.json"
            outside_manifest = root / "outside-manifest.json"
            outside_manifest.write_bytes(manifest_path.read_bytes())
            manifest_path.unlink()
            os.link(outside_manifest, manifest_path)
            with self.assertRaises(PostOfficeError):
                create_snapshot(capture, root / "linked-manifest-snapshot.json")
            self.assertFalse((root / "linked-manifest-snapshot.json").exists())

        for target_kind in ("secret", "outside"):
            with self.subTest(target=target_kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                state = root / "state"
                _create_legacy_state(state)
                target = state / "caller-secrets" / "task.token" if target_kind == "secret" else root / "outside.bin"
                if target_kind == "outside":
                    outside_directory = root / "outside-directory"
                    outside_directory.mkdir()
                    (outside_directory / "outside.bin").write_bytes(b"outside")
                    link = state / "payloads" / "linked-outside-directory"
                    subprocess.run(
                        ["cmd.exe", "/c", "mklink", "/J", str(link), str(outside_directory)],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                else:
                    os.link(target, state / "payloads" / f"linked-{target_kind}.bin")
                capture = root / "capture"
                with self.assertRaises(PostOfficeError):
                    capture_state(state, capture)
                self.assertFalse(capture.exists())

    def test_snapshot_rejects_tampered_identity_and_incomplete_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            capture = root / "capture"
            _create_legacy_state(state)
            capture_state(state, capture)
            manifest_path = capture / "capture-manifest.json"
            original = read_json(manifest_path)

            tampered = dict(original)
            tampered["captureRoot"] = "0" * 64
            manifest_path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaises(PostOfficeError):
                create_snapshot(capture, root / "tampered-root.json")
            self.assertFalse((root / "tampered-root.json").exists())

            manifest_path.write_text(json.dumps(original), encoding="utf-8")
            (capture / "CAPTURE_INCOMPLETE").write_text("incomplete", encoding="utf-8")
            with self.assertRaises(PostOfficeError):
                create_snapshot(capture, root / "incomplete.json")
            (capture / "CAPTURE_INCOMPLETE").unlink()

            escaped = json.loads(json.dumps(original))
            escaped["databases"][0]["path"] = "../outside.sqlite3"
            identity_keys = [
                "schemaVersion", "sourceStateRoot", "legacySchemaVersions", "databases", "files",
                "externalEvidence", "excluded",
            ]
            escaped["captureRoot"] = sha256_json({key: escaped[key] for key in identity_keys})
            manifest_path.write_text(json.dumps(escaped), encoding="utf-8")
            with self.assertRaises(PostOfficeError):
                create_snapshot(capture, root / "escaped.json")
            manifest_path.write_text(json.dumps(original), encoding="utf-8")

            with self.assertRaises(PostOfficeError):
                create_snapshot(capture, capture / "nested-output.json")

    def test_reconciliation_does_not_infer_semantic_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            capture = root / "capture"
            _create_legacy_state(state)
            capture_state(state, capture)
            snapshot_path = root / "snapshot.json"
            create_snapshot(capture, snapshot_path)
            assertions_path = root / "assertions.json"
            assertions_path.write_text(json.dumps({
                "schemaVersion": "1",
                "assertions": [{
                    "entityType": "SemanticCycle", "entityId": "FGPM-CYCLE-000013", "fact": "state",
                    "value": "CLOSED", "authorityKind": "RETAINED_REPORT", "verified": False,
                }],
            }), encoding="utf-8")
            output = root / "preview.json"
            reconcile_preview(snapshot_path, assertions_path, output)
            mapping = read_json(output)["legacyCycleMappings"][0]
            self.assertNotIn("semanticCycleId", mapping)
            self.assertEqual(mapping["classification"], "TRANSPORT_ONLY")
            self.assertEqual(mapping["decision"], "REVIEW_REQUIRED")
            validate(mapping, build_entity_schemas()["legacy-cycle-mapping"])

            exact_path = root / "exact-assertions.json"
            exact_path.write_text(json.dumps({
                "schemaVersion": "1",
                "assertions": [{
                    "entityType": "SemanticCycle", "entityId": "FGPM-CYCLE-000013", "fact": "semanticCycleId",
                    "value": "PON-CYCLE-000013", "authorityKind": "EXACT_AUTHOR_ACTION", "verified": True,
                }],
            }), encoding="utf-8")
            exact_output = root / "exact-preview.json"
            reconcile_preview(snapshot_path, exact_path, exact_output)
            exact_mapping = read_json(exact_output)["legacyCycleMappings"][0]
            self.assertEqual(exact_mapping["semanticCycleId"], "PON-CYCLE-000013")
            self.assertEqual(exact_mapping["classification"], "EXACT")
            self.assertEqual(exact_mapping["decision"], "PRESERVE")
            validate(exact_mapping, build_entity_schemas()["legacy-cycle-mapping"])


if __name__ == "__main__":
    unittest.main()
