# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from post_office.canonical import sha256_file, write_json  # noqa: E402
from post_office.diagnostics import PostOfficeError  # noqa: E402
from post_office.legacy import capture_state  # noqa: E402
from post_office.migration import DATABASE_NAME, import_legacy_capture, replay_migration  # noqa: E402
from post_office.kernel import bootstrap_kernel, create_kernel_credential  # noqa: E402


def _create_legacy_state(root: Path) -> tuple[str, Path]:
    root.mkdir()
    payload = b"p2.1 deterministic payload\n"
    digest = hashlib.sha256(payload).hexdigest()
    payload_path = root / "payloads" / digest[:2] / digest
    payload_path.parent.mkdir(parents=True)
    payload_path.write_bytes(payload)
    (root / "browser-observer").mkdir()
    (root / "events.jsonl").write_text("{}\n", encoding="utf-8")
    con = sqlite3.connect(root / "hub.sqlite3")
    try:
        con.executescript(
            """
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT INTO metadata VALUES('schema_version','12');
            INSERT INTO metadata VALUES('auto_review_schema_version','10');
            CREATE TABLE mailboxes(mailbox_id TEXT PRIMARY KEY,project_code TEXT,label TEXT,kind TEXT,status TEXT,generation INTEGER,thread_id TEXT,host_id TEXT,project_id TEXT,expected_root TEXT,expires_at TEXT,created_at TEXT,updated_at TEXT);
            CREATE TABLE messages(message_id TEXT PRIMARY KEY,cycle_id TEXT,root_cycle_id TEXT,project_code TEXT,sender_mailbox_id TEXT,sender_generation INTEGER,recipient_mailbox_id TEXT,recipient_generation INTEGER,message_type TEXT,authorization_class TEXT,subject TEXT,body TEXT,requested_action TEXT,completion_criteria TEXT,status TEXT,parent_message_id TEXT,may_delegate INTEGER,max_depth INTEGER,current_depth INTEGER,expires_at TEXT,created_at TEXT,updated_at TEXT,completed_summary TEXT,unresolved TEXT,acknowledgement_evidence TEXT);
            CREATE TABLE message_payloads(message_id TEXT,ordinal INTEGER,source_name TEXT,stored_path TEXT,size_bytes INTEGER,sha256 TEXT,PRIMARY KEY(message_id,ordinal));
            CREATE TABLE browser_outbox(outbox_id TEXT PRIMARY KEY,project_code TEXT,browser_mailbox_id TEXT,browser_generation INTEGER,destination TEXT,subject TEXT,package_name TEXT,expected_sha256 TEXT,delivered_sha256 TEXT,status TEXT,created_at TEXT,updated_at TEXT);
            CREATE TABLE browser_deliveries(delivery_id TEXT PRIMARY KEY,message_id TEXT,payload_ordinal INTEGER,expected_endpoint TEXT,sha256 TEXT,status TEXT,receipt_reference TEXT,drive_file_id TEXT,drive_sha256 TEXT,created_at TEXT,updated_at TEXT,delivered_at TEXT);
            CREATE TABLE browser_chat_bindings(mailbox_id TEXT PRIMARY KEY,mailbox_generation INTEGER,chat_thread_id TEXT,chat_url TEXT,browser_family TEXT,status TEXT,created_at TEXT,updated_at TEXT);
            CREATE TABLE raw_only_fact(fact_id TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE events(sequence INTEGER PRIMARY KEY,event_id TEXT,event_type TEXT,actor TEXT,object_id TEXT,occurred_at TEXT,data_json TEXT,previous_hash TEXT,event_hash TEXT);
            """
        )
        timestamp = "2026-09-06T20:00:00Z"
        con.execute(
            "INSERT INTO mailboxes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("FGPM-MBX-001", "FGPM", "worker", "CODEX", "ACTIVE", 1, "thread-1", "local", "project-1", None, None, timestamp, timestamp),
        )
        con.execute(
            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("FGPM-MSG-001", "FGPM-CYCLE-001", "FGPM-CYCLE-001", "FGPM", "FGPM-MBX-001", 1, "FGPM-MBX-001", 1, "RESPONSE", "REPORT", "P2.1 fixture", "body", "inspect", "report", "DELIVERED", None, 0, 0, 0, None, timestamp, timestamp, None, None, None),
        )
        con.execute(
            "INSERT INTO message_payloads VALUES(?,?,?,?,?,?)",
            ("FGPM-MSG-001", 0, "fixture.bin", str(payload_path), len(payload), digest),
        )
        con.execute(
            "INSERT INTO browser_deliveries VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("DELIVERY-001", "FGPM-MSG-001", 0, "browser", digest, "DELIVERED", "fixture-receipt", None, None, timestamp, timestamp, timestamp),
        )
        con.execute(
            "INSERT INTO browser_chat_bindings VALUES(?,?,?,?,?,?,?,?)",
            ("FGPM-MBX-001", 1, "chat-1", "https://chatgpt.com/c/chat-1", "CHROME", "ACTIVE", timestamp, timestamp),
        )
        con.execute("INSERT INTO raw_only_fact VALUES('FACT-001','preserve exactly')")
        con.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)",
            (1, "LEGACY-EVENT-001", "MESSAGE_POSTED", "system", "FGPM-MSG-001", timestamp, "{}", "0" * 64, "1" * 64),
        )
        con.commit()
    finally:
        con.close()
    observer = sqlite3.connect(root / "browser-observer" / "observer.sqlite3")
    try:
        observer.executescript(
            """
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT INTO metadata VALUES('schema_version','1');
            CREATE TABLE events(id INTEGER PRIMARY KEY,event_hash TEXT);
            CREATE TABLE thread_state(thread_id TEXT PRIMARY KEY,dirty INTEGER);
            INSERT INTO thread_state VALUES('chat-1',0);
            """
        )
        observer.commit()
    finally:
        observer.close()
    return digest, payload_path


def _create_fixture(root: Path) -> tuple[Path, Path, Path]:
    state = root / "state"
    capture = root / "capture"
    baseline_root = root / "payload-baseline"
    delta_root = root / "payload-delta"
    digest, payload_path = _create_legacy_state(state)
    capture_state(state, capture)
    capture_manifest = json.loads((capture / "capture-manifest.json").read_text(encoding="utf-8"))
    baseline_object = baseline_root / "objects" / digest[:2] / digest
    baseline_object.parent.mkdir(parents=True)
    shutil.copyfile(payload_path, baseline_object)
    baseline_payload = {
        "sourcePath": f"payloads/{digest[:2]}/{digest}",
        "capturedPath": f"objects/{digest[:2]}/{digest}",
        "bytes": payload_path.stat().st_size,
        "sha256": digest,
    }
    baseline_root_hash = hashlib.sha256(
        f"{baseline_payload['capturedPath']}\n{baseline_payload['bytes']}\n{baseline_payload['sha256']}\n".encode("utf-8")
    ).hexdigest()
    baseline = {
        "schemaVersion": "1",
        "phase": "P1_EXACT_READ_ONLY_CAPTURE_AND_RECONCILIATION",
        "status": "BASELINE_NON_FINAL",
        "authority": "EVIDENCE_ONLY_OLD_POST_OFFICE_REMAINS_AUTHORITATIVE",
        "sourceCaptureRoot": "a" * 64,
        "payloadCount": 1,
        "payloadBytes": payload_path.stat().st_size,
        "payloadMerkleRoot": baseline_root_hash,
        "payloads": [baseline_payload],
    }
    baseline_path = baseline_root / "payload-capture-manifest.json"
    write_json(baseline_path, baseline)
    delta_root.mkdir()
    delta = {
        "schemaVersion": "1",
        "phase": "P1_EXACT_READ_ONLY_CAPTURE_AND_RECONCILIATION",
        "status": "FROZEN_POST_BASELINE_DELTA",
        "authority": "EVIDENCE_ONLY_OLD_POST_OFFICE_REMAINS_AUTHORITATIVE",
        "sourceCaptureManifest": str(capture / "capture-manifest.json"),
        "sourceCaptureManifestSha256": sha256_file(capture / "capture-manifest.json"),
        "sourceCaptureRoot": capture_manifest["captureRoot"],
        "sourcePayloadCount": 1,
        "baselinePayloadManifest": str(baseline_path),
        "baselinePayloadManifestSha256": sha256_file(baseline_path),
        "baselineCaptureRoot": baseline["sourceCaptureRoot"],
        "baselinePayloadCount": 1,
        "payloadCount": 0,
        "payloadBytes": 0,
        "payloadMerkleRoot": hashlib.sha256(b"").hexdigest(),
        "coverage": "exact fixture coverage",
        "payloads": [],
    }
    delta_path = delta_root / "payload-capture-manifest.json"
    write_json(delta_path, delta)
    return capture, baseline_path, delta_path


class MigrationTests(unittest.TestCase):
    def test_import_is_deterministic_complete_and_replayable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            capture, baseline, delta = _create_fixture(root)
            first = import_legacy_capture(capture, baseline, delta, root / "import-one", PLUGIN_ROOT)
            second = import_legacy_capture(capture, baseline, delta, root / "import-two", PLUGIN_ROOT)
            self.assertEqual(first["logicalStateRoot"], second["logicalStateRoot"])
            self.assertEqual(first["eventChainRoot"], second["eventChainRoot"])
            self.assertEqual(first["verificationRoot"], second["verificationRoot"])
            self.assertEqual(first["casRoot"], second["casRoot"])
            self.assertEqual(first["counts"]["messages"], 1)
            self.assertEqual(first["counts"]["payloads"], 1)

            database = sqlite3.connect(root / "import-one" / DATABASE_NAME)
            try:
                self.assertEqual(database.execute("SELECT state FROM migration_runs").fetchone()[0], "VERIFIED")
                self.assertEqual(database.execute("SELECT COUNT(*) FROM semantic_messages").fetchone()[0], 1)
                self.assertEqual(database.execute("SELECT COUNT(*) FROM migration_payloads").fetchone()[0], 1)
                self.assertEqual(database.execute("SELECT COUNT(*) FROM legacy_import_rows WHERE table_name='raw_only_fact'").fetchone()[0], 1)
                self.assertEqual(
                    database.execute("SELECT confidence FROM legacy_cycle_mappings").fetchone()[0],
                    "STRUCTURAL_ONLY_NO_AUTHOR_ACCEPTANCE_INFERRED",
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    database.execute("UPDATE legacy_import_rows SET row_sha256=?", ("f" * 64,))
            finally:
                database.close()

            replay = replay_migration(root / "import-one", root / "replay", PLUGIN_ROOT)
            self.assertEqual(replay["logicalStateRoot"], first["logicalStateRoot"])
            self.assertEqual(replay["eventChainRoot"], first["eventChainRoot"])

            credential = root / "author.json"
            create_kernel_credential(credential, "PON-CAPABILITY-IMPORTED-AUTHOR")
            bootstrapped = bootstrap_kernel(
                root / "import-one" / DATABASE_NAME, credential,
                actor_id="PON-ACTOR-IMPORTED-AUTHOR", actor_kind="HUMAN",
                actor_role="author", mode="SHADOW", plugin_root=PLUGIN_ROOT,
            )
            self.assertTrue(bootstrapped["ok"])

    def test_tampered_payload_fails_before_output_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            capture, baseline, delta = _create_fixture(root)
            document = json.loads(baseline.read_text(encoding="utf-8"))
            object_path = baseline.parent.joinpath(*document["payloads"][0]["capturedPath"].split("/"))
            object_path.write_bytes(b"tampered")
            output = root / "must-not-exist"
            with self.assertRaises(PostOfficeError) as raised:
                import_legacy_capture(capture, baseline, delta, output, PLUGIN_ROOT)
            self.assertEqual(raised.exception.code, "PON_SNAPSHOT_INTEGRITY_FAILURE")
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
