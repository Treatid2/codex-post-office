# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import json
import hashlib
import shutil
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from post_office.database import initialize_database  # noqa: E402
from post_office.diagnostics import PostOfficeError  # noqa: E402
from post_office.kernel import (  # noqa: E402
    IMPLEMENTED_OPERATIONS,
    _contract_root,
    _validate_hub_event_chain,
    append_hub_event,
    assert_aggregate_concurrency,
    bootstrap_kernel,
    bind_kernel_credential,
    create_kernel_credential,
    create_kernel_actor,
    execute_operation,
    inspect_kernel,
)
from post_office.runtime import (  # noqa: E402
    _continuation_destination,
    _resolve_superseded_transport_attention,
    _validate_manifest_backed_return,
    claim_next_continuation,
    claim_next_review,
    claim_next_transport,
    complete_transport,
    ensure_automatic_review,
    ingest_collected_browser_return,
    ingest_recovered_browser_return,
    issue_automatic_review_result_collection_manifest,
    issue_automatic_review_activation_manifest,
    issue_browser_return_collection_manifest,
    issue_supplemental_delivery_manifest,
    issue_transport_delivery_manifest,
    reconcile_continuations,
    reconcile_transport,
    record_recovered_transport_receipt,
    record_automatic_review_activation_receipt,
    retain_outbound_package,
    retire_review_owned_transport_dispatches,
    retire_continuation,
    withdraw_automatic_review,
)


class OperationalKernelTests(unittest.TestCase):
    def test_receipted_retry_resolves_superseded_dispatch_attention(self) -> None:
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        try:
            con.execute(
                "CREATE TABLE transport_attempts(transport_attempt_id TEXT PRIMARY KEY, canonical_attempt_id TEXT)"
            )
            con.execute(
                "CREATE TABLE transport_dispatches(dispatch_id TEXT PRIMARY KEY, transport_attempt_id TEXT)"
            )
            con.execute(
                "CREATE TABLE attention_items(entity_type TEXT, entity_id TEXT, state TEXT, resolved_at TEXT)"
            )
            con.execute("INSERT INTO transport_attempts VALUES('ATTEMPT-OLD',NULL)")
            con.execute("INSERT INTO transport_attempts VALUES('ATTEMPT-NEW','ATTEMPT-OLD')")
            con.execute("INSERT INTO transport_dispatches VALUES('DISPATCH-OLD','ATTEMPT-OLD')")
            con.execute("INSERT INTO attention_items VALUES('TransportDispatch','DISPATCH-OLD','OPEN',NULL)")
            current = con.execute(
                "SELECT * FROM transport_attempts WHERE transport_attempt_id='ATTEMPT-NEW'"
            ).fetchone()
            _resolve_superseded_transport_attention(con, current, "2026-09-12T05:00:00Z")
            state, resolved_at = con.execute(
                "SELECT state,resolved_at FROM attention_items"
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(state, "RESOLVED")
        self.assertEqual(resolved_at, "2026-09-12T05:00:00Z")

    def test_root_browser_return_manifest_rejects_incorrect_aggregate_metadata(self) -> None:
        response = b"in_reply_to: DEMO-C2C-000001\nresult: COMPLETE\n"
        manifest = json.dumps({
            "manifestSelfExcluded": True,
            "fileCount": 1,
            "payloadBytes": len(response) + 1,
            "files": [{
                "path": "response.md",
                "bytes": len(response),
                "sha256": hashlib.sha256(response).hexdigest(),
            }],
        }).encode("utf-8")
        with tempfile.TemporaryDirectory() as temporary:
            archive_path = Path(temporary) / "return.zip"
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("response.md", response)
                archive.writestr("MANIFEST.json", manifest)
            with self.assertRaises(PostOfficeError) as raised:
                _validate_manifest_backed_return(archive_path.read_bytes(), "DEMO-C2C-000001")
        self.assertEqual(raised.exception.code, "PON_CUSTODY_NOT_VERIFIED")

    def test_browser_return_rejects_ambiguous_supported_manifests(self) -> None:
        response = b"in_reply_to: DEMO-C2C-000001\nresult: COMPLETE\n"
        member = {
            "path": "response.md",
            "bytes": len(response),
            "sha256": hashlib.sha256(response).hexdigest(),
        }
        with tempfile.TemporaryDirectory() as temporary:
            archive_path = Path(temporary) / "return.zip"
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("response.md", response)
                archive.writestr("PACKAGE-MANIFEST.json", json.dumps({"members": [member]}))
                archive.writestr("MANIFEST.json", json.dumps({
                    "manifestSelfExcluded": True,
                    "fileCount": 2,
                    "payloadBytes": len(response),
                    "files": [member],
                }))
            with self.assertRaises(PostOfficeError) as raised:
                _validate_manifest_backed_return(archive_path.read_bytes(), "DEMO-C2C-000001")
        self.assertEqual(raised.exception.code, "PON_CUSTODY_NOT_VERIFIED")

    def test_root_browser_return_manifest_accepts_matching_zip_comment(self) -> None:
        response = b"in_reply_to: DEMO-C2C-000001\nresult: COMPLETE\n"
        manifest = json.dumps({
            "files": [{
                "path": "response.md",
                "bytes": len(response),
                "sha256": hashlib.sha256(response).hexdigest(),
            }],
        }).encode("utf-8")
        with tempfile.TemporaryDirectory() as temporary:
            archive_path = Path(temporary) / "return.zip"
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("response.md", response)
                archive.writestr("MANIFEST.json", manifest)
                archive.comment = (
                    "Manifest-SHA256: " + hashlib.sha256(manifest).hexdigest()
                ).encode("ascii")
            verified = _validate_manifest_backed_return(
                archive_path.read_bytes(), "DEMO-C2C-000001"
            )
        self.assertEqual(verified["manifestName"], "MANIFEST.json")
        self.assertEqual(verified["manifestListedMemberCount"], 1)

    def test_browser_return_accepts_a_verified_ancestor_correlation(self) -> None:
        response = b"governing_handoff: DEMO-C2C-000006\nresult: REVISION\n"
        manifest_name = "DEMO_Package-Manifest.json"
        manifest = json.dumps({
            "members": [{
                "path": "response.md",
                "bytes": len(response),
                "sha256": hashlib.sha256(response).hexdigest(),
            }],
        }).encode("utf-8")
        with tempfile.TemporaryDirectory() as temporary:
            archive_path = Path(temporary) / "return.zip"
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("response.md", response)
                archive.writestr(manifest_name, manifest)
            verified = _validate_manifest_backed_return(
                archive_path.read_bytes(),
                "DEMO-C2C-000007",
                correlation_message_ids=["DEMO-C2C-000006"],
            )
        self.assertEqual(verified["correlationMessageId"], "DEMO-C2C-000006")

    def test_expired_author_capability_cannot_create_actor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, credential = self._author_bootstrapped(root)
            con = sqlite3.connect(database)
            try:
                con.execute(
                    "UPDATE caller_capabilities SET expires_at='2000-01-01T00:00:00Z' "
                    "WHERE capability_id='PON-CAPABILITY-AUTHOR'"
                )
                con.commit()
            finally:
                con.close()
            with self.assertRaisesRegex(PostOfficeError, "authenticated human author"):
                create_kernel_actor(
                    database,
                    credential,
                    action_id="PON-ACTION-MUST-NOT-COMMIT",
                    actor_id="PON-ACTOR-MUST-NOT-EXIST",
                    actor_kind="COURIER",
                    actor_role="courier",
                    plugin_root=PLUGIN_ROOT,
                )
            con = sqlite3.connect(database)
            try:
                self.assertIsNone(
                    con.execute(
                        "SELECT 1 FROM actors WHERE actor_id='PON-ACTOR-MUST-NOT-EXIST'"
                    ).fetchone()
                )
            finally:
                con.close()

    def test_recovered_browser_return_is_atomically_retained_and_receipted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, author_credential = self._author_bootstrapped(root)
            self._seed_project(database)
            courier_credential = root / "courier.json"
            create_kernel_actor(
                database, author_credential, action_id="PON-ACTION-RETURN-COURIER-ACTOR",
                actor_id="PON-ACTOR-RETURN-COURIER", actor_kind="COURIER", actor_role="courier",
                plugin_root=PLUGIN_ROOT,
            )
            bind_kernel_credential(
                database, author_credential, courier_credential,
                action_id="PON-ACTION-RETURN-COURIER-CREDENTIAL",
                actor_id="PON-ACTOR-RETURN-COURIER", capability_id="PON-CAPABILITY-RETURN-COURIER",
                subject_kind="SYSTEM", subject_id="PON-KERNEL", subject_generation=1,
                allowed_operations=["transport.inspect"], expires_at=None, plugin_root=PLUGIN_ROOT,
            )
            now = "2026-09-12T00:00:00Z"
            source_thread = "6a837997-7528-83ea-83e7-3b721acf9b8d"
            destination_thread = "6a81ee71-0f60-83eb-93d7-962f5ad0f963"
            con = sqlite3.connect(database)
            try:
                con.execute(
                    "INSERT INTO actors VALUES(?,?,?,?,?)",
                    ("PON-ACTOR-MASTER", "BROWSER", "browser", "ACTIVE", now),
                )
                con.execute(
                    "INSERT INTO actors VALUES(?,?,?,?,?)",
                    ("PON-ACTOR-ELOQUENT", "BROWSER", "browser", "ACTIVE", now),
                )
                con.execute(
                    """INSERT INTO endpoints(endpoint_id,actor_id,project_id,task_id,role,access_scope_json,
                       status,startup_root,created_event_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    ("PON-ENDPOINT-MASTER", "PON-ACTOR-MASTER", "PON-PROJECT-001", None, "BROWSER_PROXY",
                     json.dumps({"browserBinding": {"chat_thread_id": destination_thread}}),
                     "ACTIVE", "1" * 64, None, now),
                )
                con.execute(
                    """INSERT INTO endpoints(endpoint_id,actor_id,project_id,task_id,role,access_scope_json,
                       status,startup_root,created_event_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    ("PON-ENDPOINT-ELOQUENT", "PON-ACTOR-ELOQUENT", "PON-PROJECT-001", None, "BROWSER_PROXY",
                     json.dumps({"browserBinding": {"chat_thread_id": source_thread}}),
                     "ACTIVE", "2" * 64, None, now),
                )
                con.execute(
                    "INSERT INTO mailboxes VALUES(?,?,?,?,?,?,?)",
                    ("DEMO-MBX-MASTER", 1, "DEMO", "PON-ENDPOINT-MASTER", "ACTIVE", None, now),
                )
                con.execute(
                    "INSERT INTO mailboxes VALUES(?,?,?,?,?,?,?)",
                    ("DEMO-MBX-ELOQUENT", 1, "DEMO", "PON-ENDPOINT-ELOQUENT", "ACTIVE", None, now),
                )
                con.execute(
                    "INSERT INTO exact_author_actions VALUES(?,?,?,?,?,?,?)",
                    ("PON-ACTION-SOURCE", "PON-ACTOR-AUTHOR", "message.send", "{}", "3" * 64, now, None),
                )
                con.execute(
                    """INSERT INTO semantic_cycles(cycle_id,project_id,scope,opening_author_action_id,state,
                       aggregate_version,aggregate_root,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                    ("PON-CYCLE-RETURN", "PON-PROJECT-001", "Return test", "PON-ACTION-SOURCE",
                     "ACTIVE", 1, "4" * 64, now),
                )
                con.execute(
                    """INSERT INTO semantic_messages(message_id,project_id,mail_domain,message_type,sender_endpoint_id,
                       recipient_mailbox_id,recipient_generation,semantic_cycle_id,authority_grant_id,exact_author_action_id,
                       requested_action,completion_criteria,content_root,state,aggregate_version,aggregate_root,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("DEMO-C2C-000001", "PON-PROJECT-001", "DEMO", "HANDOFF", "PON-ENDPOINT-MASTER",
                     "DEMO-MBX-ELOQUENT", 1, "PON-CYCLE-RETURN", None, "PON-ACTION-SOURCE",
                     "Produce candidate", "Return exact package", "5" * 64, "DELIVERED", 1, "6" * 64, now),
                )
                con.commit()
            finally:
                con.close()

            collected_response = b"in_reply_to: DEMO-C2C-000001\nresult: COLLECTED_THEN_ROUTED\n"
            collected_response_hash = hashlib.sha256(collected_response).hexdigest()
            collected_manifest = json.dumps({
                "in_response_to": "DEMO-C2C-000001",
                "manifestSelfExcluded": True,
                "fileCount": 1,
                "payloadBytes": len(collected_response),
                "files": [{
                    "path": "DEMO_Collected-Response.md",
                    "bytes": len(collected_response),
                    "sha256": collected_response_hash,
                }],
            }).encode("utf-8")
            collected_archive = root / "DEMO_COLLECTED_BROWSER_RETURN_v01.zip"
            with zipfile.ZipFile(collected_archive, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("DEMO_Collected-Response.md", collected_response)
                archive.writestr("MANIFEST.json", collected_manifest)
            collected_data = collected_archive.read_bytes()
            collected_hash = hashlib.sha256(collected_data).hexdigest()
            collection = issue_browser_return_collection_manifest(
                database, courier_credential, PLUGIN_ROOT,
                source_message_id="DEMO-C2C-000001", source_thread_id=source_thread,
                source_turn_id="374f6018-f5c6-4ed5-a252-062fe51c5dc5",
                attachment_reference=':chatgpt-content-reference{index="0"}',
                attachment_name=collected_archive.name, expected_sha256=collected_hash,
                expected_size_bytes=len(collected_data), observed_at="2026-09-12T00:10:00Z",
                required_text=["DEMO-C2C-000001"],
            )
            collection_manifest = Path(collection["manifestPath"])
            receipt = (
                f"playwright-chatgpt-collection:{collection['collectionId']}:"
                f"{source_thread}:{collected_hash}"
            )
            ingested = ingest_collected_browser_return(
                database, courier_credential, PLUGIN_ROOT,
                collection_manifest_path=collection_manifest, result_path=collected_archive,
                collection_receipt=receipt,
            )
            self.assertEqual(ingested["messageId"], "DEMO-C2C-000002")
            self.assertEqual(ingested["recipientMailboxId"], "DEMO-MBX-MASTER")
            dispatches = reconcile_transport(database, courier_credential, PLUGIN_ROOT)
            self.assertEqual(len(dispatches["createdDispatchIds"]), 1)
            claim = claim_next_transport(
                database, courier_credential, PLUGIN_ROOT, lease_seconds=60,
                dispatch_id=dispatches["createdDispatchIds"][0],
            )
            self.assertEqual(claim["channel"], "PLAYWRIGHT_BROWSER")
            self.assertEqual(
                claim["observableMarker"],
                f"POST-OFFICE-PLAYWRIGHT-DISPATCH {claim['dispatchId']}",
            )
            delivery = issue_transport_delivery_manifest(
                database, courier_credential, PLUGIN_ROOT,
                dispatch_id=claim["dispatchId"], lease_token=claim["leaseToken"],
            )
            delivery_manifest = json.loads(Path(delivery["manifestPath"]).read_text(encoding="utf-8"))
            self.assertEqual(delivery_manifest["threadId"], destination_thread)
            self.assertEqual(delivery_manifest["messageId"], "DEMO-C2C-000002")
            self.assertEqual(delivery_manifest["attachments"][0]["sha256"], collected_hash)
            complete_transport(
                database, courier_credential, PLUGIN_ROOT,
                dispatch_id=claim["dispatchId"], lease_token=claim["leaseToken"],
                observable_marker=claim["observableMarker"],
                observed_receipt_id="playwright-chatgpt:DEMO-C2C-000002",
            )
            recovery = issue_supplemental_delivery_manifest(
                database, courier_credential, PLUGIN_ROOT,
                message_id="DEMO-C2C-000002", bundle_id=ingested["bundleId"],
                idempotency_key="missing-chatgpt-attachment-001",
            )
            recovery_manifest = json.loads(
                Path(recovery["manifestPath"]).read_text(encoding="utf-8")
            )
            self.assertEqual(recovery_manifest["messageId"], "DEMO-C2C-000002")
            self.assertEqual(recovery_manifest["attachments"][0]["sha256"], collected_hash)
            self.assertIn("not a new message", recovery_manifest["prompt"])
            self.assertTrue(issue_supplemental_delivery_manifest(
                database, courier_credential, PLUGIN_ROOT,
                message_id="DEMO-C2C-000002", bundle_id=ingested["bundleId"],
                idempotency_key="missing-chatgpt-attachment-001",
            )["replayed"])

            response_name = "DEMO_BROWSER_RESPONSE_RE-DEMO-C2C-000001.md"
            response = b"in_reply_to: DEMO-C2C-000001\nresult: COMPLETE_CANDIDATE\n"
            response_hash = hashlib.sha256(response).hexdigest()
            manifest_name = "DEMO_Package-Member-Manifest_v01.md"
            manifest = (
                "# Package member manifest\n\n"
                "| Relative member | Bytes | SHA-256 |\n"
                "|---|---:|---|\n"
                f"| `{response_name}` | {len(response):,} | `{response_hash}` |\n"
            ).encode("utf-8")
            archive_path = root / "DEMO_BROWSER_RETURN_v01.zip"
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(response_name, response)
                archive.writestr(manifest_name, manifest)
            archive_data = archive_path.read_bytes()
            archive_hash = hashlib.sha256(archive_data).hexdigest()

            result = ingest_recovered_browser_return(
                database, courier_credential, PLUGIN_ROOT,
                source_message_id="DEMO-C2C-000001", result_path=archive_path,
                expected_sha256=archive_hash, expected_size_bytes=len(archive_data),
                source_thread_id=source_thread, source_turn_id="PON-TURN-ELOQUENT",
                destination_thread_id=destination_thread, destination_turn_id="PON-TURN-MASTER",
            )
            self.assertFalse(result["replayed"])
            self.assertEqual(result["messageId"], "DEMO-C2C-000003")
            self.assertEqual(result["recipientMailboxId"], "DEMO-MBX-MASTER")
            replay = ingest_recovered_browser_return(
                database, courier_credential, PLUGIN_ROOT,
                source_message_id="DEMO-C2C-000001", result_path=archive_path,
                expected_sha256=archive_hash, expected_size_bytes=len(archive_data),
                source_thread_id=source_thread, source_turn_id="PON-TURN-ELOQUENT",
                destination_thread_id=destination_thread, destination_turn_id="PON-TURN-MASTER",
            )
            self.assertTrue(replay["replayed"])
            self.assertEqual(replay["messageId"], result["messageId"])
            con = sqlite3.connect(database)
            try:
                self.assertEqual(
                    con.execute("SELECT state FROM semantic_messages WHERE message_id='DEMO-C2C-000003'").fetchone()[0],
                    "DELIVERED",
                )
                self.assertEqual(
                    con.execute(
                        "SELECT COUNT(*) FROM runtime_receipts WHERE receipt_kind='RECOVERED' "
                        "AND json_extract(evidence_json,'$.recovery')='USER_SHORTCUT_ALREADY_DELIVERED'"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    con.execute("SELECT COUNT(*) FROM storage_copies WHERE content_sha256=?", (archive_hash,)).fetchone()[0],
                    1,
                )
            finally:
                con.close()

            outbound_response = b"in_reply_to: DEMO-C2C-000001\nresult: LOCAL_TASK_RETURN\n"
            outbound_hash = hashlib.sha256(outbound_response).hexdigest()
            outbound_manifest = json.dumps({
                "manifestSelfExcluded": True,
                "fileCount": 1,
                "payloadBytes": len(outbound_response),
                "files": [{
                    "path": "DEMO_Local-Task-Return.md",
                    "bytes": len(outbound_response),
                    "sha256": outbound_hash,
                }],
            }).encode("utf-8")
            outbound_archive = root / "DEMO_LOCAL_TASK_RETURN_v01.zip"
            with zipfile.ZipFile(outbound_archive, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("DEMO_Local-Task-Return.md", outbound_response)
                archive.writestr("MANIFEST.json", outbound_manifest)
            outbound_data = outbound_archive.read_bytes()
            retained = retain_outbound_package(
                database, courier_credential, PLUGIN_ROOT,
                source_message_id="DEMO-C2C-000001", result_path=outbound_archive,
                expected_sha256=hashlib.sha256(outbound_data).hexdigest(),
                expected_size_bytes=len(outbound_data),
            )
            self.assertFalse(retained["replayed"])
            self.assertEqual(retained["correlationMessageId"], "DEMO-C2C-000001")
            replayed = retain_outbound_package(
                database, courier_credential, PLUGIN_ROOT,
                source_message_id="DEMO-C2C-000001", result_path=outbound_archive,
                expected_sha256=hashlib.sha256(outbound_data).hexdigest(),
                expected_size_bytes=len(outbound_data),
            )
            self.assertTrue(replayed["replayed"])

    def _bootstrapped(self, root: Path) -> tuple[Path, Path]:
        database = root / "post-office-next.sqlite3"
        credential = root / "operator-credential.json"
        initialize_database(PLUGIN_ROOT, database)
        created = create_kernel_credential(credential, "PON-CAPABILITY-OPERATOR")
        self.assertNotIn("secret", created)
        bootstrap = bootstrap_kernel(
            database,
            credential,
            actor_id="PON-ACTOR-OPERATOR",
            actor_kind="HUMAN",
            actor_role="authenticated-reader",
            mode="ISOLATED",
            plugin_root=PLUGIN_ROOT,
        )
        self.assertTrue(bootstrap["created"])
        return database, credential

    def _author_bootstrapped(self, root: Path) -> tuple[Path, Path]:
        database = root / "post-office-next.sqlite3"
        credential = root / "author-credential.json"
        initialize_database(PLUGIN_ROOT, database)
        create_kernel_credential(credential, "PON-CAPABILITY-AUTHOR")
        bootstrap_kernel(
            database,
            credential,
            actor_id="PON-ACTOR-AUTHOR",
            actor_kind="HUMAN",
            actor_role="author",
            mode="ISOLATED",
            plugin_root=PLUGIN_ROOT,
        )
        return database, credential

    @staticmethod
    def _write_request(root: Path, name: str, value: dict[str, object]) -> Path:
        path = root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    @staticmethod
    def _author_request(
        operation: str,
        request_id: str,
        action_id: str,
        aggregate_type: str,
        aggregate_id: str,
        expected_version: int,
        parameters: dict[str, object],
    ) -> dict[str, object]:
        return {
            "schemaVersion": "1",
            "operation": operation,
            "requestId": request_id,
            "actor": {"id": "PON-ACTOR-AUTHOR", "kind": "HUMAN", "role": "author"},
            "authority": {
                "capabilityId": "PON-CAPABILITY-AUTHOR",
                "exactAuthorActionId": action_id,
            },
            "aggregate": {
                "type": aggregate_type,
                "id": aggregate_id,
                "expectedVersion": expected_version,
            },
            "parameters": parameters,
        }

    @staticmethod
    def _seed_project(database: Path) -> None:
        con = sqlite3.connect(database)
        try:
            con.execute(
                """INSERT INTO projects(
                   project_id,code,display_name,kind,status,local_project_root,local_cas_root,
                   local_backup_root,aggregate_version,aggregate_root,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "PON-PROJECT-001", "DEMO", "Demo", "LOCAL", "ACTIVE",
                    "projects/demo", "cas/demo", "backup/demo", 1, "1" * 64,
                    "2026-09-11T00:00:00Z",
                ),
            )
            con.execute(
                "INSERT INTO project_mail_domains(project_id,mail_domain) VALUES(?,?)",
                ("PON-PROJECT-001", "DEMO"),
            )
            con.commit()
        finally:
            con.close()

    @staticmethod
    def _status_request(path: Path, request_id: str = "PON-REQUEST-STATUS") -> Path:
        value = {
            "schemaVersion": "1",
            "operation": "hub.status",
            "requestId": request_id,
            "actor": {
                "id": "PON-ACTOR-OPERATOR",
                "kind": "HUMAN",
                "role": "authenticated-reader",
            },
            "authority": {"capabilityId": "PON-CAPABILITY-OPERATOR"},
            "parameters": {"includeDetails": True},
        }
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_bootstrap_is_exactly_replayable_and_inspectable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, credential = self._bootstrapped(root)
            replay = bootstrap_kernel(
                database,
                credential,
                actor_id="PON-ACTOR-OPERATOR",
                actor_kind="HUMAN",
                actor_role="authenticated-reader",
                mode="ISOLATED",
                plugin_root=PLUGIN_ROOT,
            )
            self.assertFalse(replay["created"])
            inspected = inspect_kernel(database, PLUGIN_ROOT)
            self.assertEqual(inspected["mode"], "ISOLATED")
            self.assertEqual(inspected["status"], "READY")
            self.assertEqual(inspected["implementedOperations"], sorted(IMPLEMENTED_OPERATIONS))
            with self.assertRaises(PostOfficeError) as raised:
                bootstrap_kernel(
                    database,
                    credential,
                    actor_id="PON-ACTOR-OPERATOR",
                    actor_kind="HUMAN",
                    actor_role="authenticated-reader",
                    mode="SHADOW",
                    plugin_root=PLUGIN_ROOT,
                )
            self.assertEqual(raised.exception.code, "PON_KERNEL_ALREADY_BOOTSTRAPPED")

    def test_status_dispatch_is_authenticated_contract_valid_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, credential = self._bootstrapped(root)
            request = self._status_request(root / "status.json")
            first = execute_operation(database, request, credential, PLUGIN_ROOT)
            replay = execute_operation(database, request, credential, PLUGIN_ROOT)
            self.assertEqual(first, replay)
            self.assertTrue(first["ok"])
            self.assertEqual(first["beforeRoot"], first["afterRoot"])
            self.assertEqual(first["status"]["mode"], "ISOLATED")
            self.assertEqual(first["status"]["databaseUserVersion"], 10)
            self.assertEqual(first["status"]["implementedOperations"], sorted(IMPLEMENTED_OPERATIONS))
            con = sqlite3.connect(database)
            try:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM idempotency_records").fetchone()[0], 1)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM hub_events").fetchone()[0], 0)
            finally:
                con.close()

    def test_concurrent_exact_replay_commits_one_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, credential = self._bootstrapped(root)
            request = self._status_request(root / "status.json")
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(
                    pool.map(
                        lambda _: execute_operation(database, request, credential, PLUGIN_ROOT),
                        range(8),
                    )
                )
            self.assertTrue(all(result == results[0] for result in results))
            con = sqlite3.connect(database)
            try:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM idempotency_records").fetchone()[0], 1)
            finally:
                con.close()

    def test_changed_replay_and_bad_identity_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, credential = self._bootstrapped(root)
            request = self._status_request(root / "status.json")
            execute_operation(database, request, credential, PLUGIN_ROOT)
            changed = json.loads(request.read_text(encoding="utf-8"))
            changed["parameters"]["includeDetails"] = False
            request.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaises(PostOfficeError) as raised:
                execute_operation(database, request, credential, PLUGIN_ROOT)
            self.assertEqual(raised.exception.code, "PON_IDEMPOTENCY_CONFLICT")

            changed["requestId"] = "PON-REQUEST-BAD-ACTOR"
            changed["actor"]["id"] = "PON-ACTOR-IMPOSTOR"
            request.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaises(PostOfficeError) as raised:
                execute_operation(database, request, credential, PLUGIN_ROOT)
            self.assertEqual(raised.exception.code, "PON_AUTHENTICATION_FAILED")

            changed["actor"]["id"] = "PON-ACTOR-OPERATOR"
            changed["requestId"] = "PON-REQUEST-BAD-SECRET"
            request.write_text(json.dumps(changed), encoding="utf-8")
            bad_credential = root / "bad-credential.json"
            bad_value = json.loads(credential.read_text(encoding="utf-8"))
            bad_value["secret"] = "x" * 64
            bad_credential.write_text(json.dumps(bad_value), encoding="utf-8")
            with self.assertRaises(PostOfficeError) as raised:
                execute_operation(database, request, bad_credential, PLUGIN_ROOT)
            self.assertEqual(raised.exception.code, "PON_AUTHENTICATION_FAILED")

    def test_all_contracted_operations_are_implemented(self) -> None:
        catalogue = json.loads(
            (PLUGIN_ROOT / "contracts" / "v1" / "operation-catalogue.json").read_text(encoding="utf-8")
        )
        contracted = {item["operation"] for item in catalogue["operations"]}
        self.assertEqual(contracted, set(IMPLEMENTED_OPERATIONS))

    def test_authority_inspection_reports_capability_without_minting_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, credential = self._bootstrapped(root)
            request = {
                "schemaVersion": "1",
                "operation": "authority.inspect",
                "requestId": "PON-REQUEST-AUTHORITY-INSPECT",
                "actor": {
                    "id": "PON-ACTOR-OPERATOR",
                    "kind": "HUMAN",
                    "role": "authenticated-reader",
                },
                "authority": {"capabilityId": "PON-CAPABILITY-OPERATOR"},
                "parameters": {
                    "actorId": "PON-ACTOR-OPERATOR",
                    "proposedOperation": "endpoint.allocate",
                },
            }
            result = execute_operation(
                database,
                self._write_request(root, "authority-inspect.json", request),
                credential,
                PLUGIN_ROOT,
            )
            self.assertTrue(result["authorization"]["implemented"])
            self.assertFalse(result["authorization"]["capabilityAllowed"])
            self.assertEqual(result["authorization"]["decision"], "DENIED")
            con = sqlite3.connect(database)
            try:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM hub_events").fetchone()[0], 0)
            finally:
                con.close()

    def test_author_grants_and_revokes_scoped_authority_with_exact_actions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, credential = self._author_bootstrapped(root)
            self._seed_project(database)
            con = sqlite3.connect(database)
            try:
                con.execute(
                    "INSERT INTO actors(actor_id,actor_kind,role,status,created_at) VALUES(?,?,?,?,?)",
                    ("PON-ACTOR-RECIPIENT", "BROWSER", "delegated-browser", "ACTIVE", "2026-09-11T00:00:00Z"),
                )
                con.commit()
            finally:
                con.close()
            grant_request = self._author_request(
                "authority.grant",
                "PON-REQUEST-GRANT-001",
                "PON-ACTION-GRANT-001",
                "AuthorityGrant",
                "PON-GRANT-001",
                0,
                {
                    "grant": {
                        "grantorId": "PON-ACTOR-AUTHOR",
                        "recipientId": "PON-ACTOR-RECIPIENT",
                        "allowedOperations": ["endpoint.allocate"],
                        "scope": {"projectIds": ["PON-PROJECT-001"]},
                        "classification": "ONE_SHOT",
                        "maximumUses": 1,
                        "rationale": "Bounded endpoint provisioning",
                        "sourceDecisionId": "PON-ACTION-GRANT-001",
                    }
                },
            )
            grant_path = self._write_request(root, "grant.json", grant_request)
            first = execute_operation(database, grant_path, credential, PLUGIN_ROOT)
            self.assertEqual(first, execute_operation(database, grant_path, credential, PLUGIN_ROOT))
            self.assertEqual(first["aggregateVersion"], 1)
            self.assertEqual(first["receipt"]["mutationEventId"], first["eventId"])
            revoke_request = self._author_request(
                "authority.revoke",
                "PON-REQUEST-REVOKE-001",
                "PON-ACTION-REVOKE-001",
                "AuthorityGrant",
                "PON-GRANT-001",
                1,
                {"grantId": "PON-GRANT-001", "reason": "No longer required"},
            )
            revoked = execute_operation(
                database,
                self._write_request(root, "revoke.json", revoke_request),
                credential,
                PLUGIN_ROOT,
            )
            self.assertEqual(revoked["aggregateVersion"], 2)
            con = sqlite3.connect(database)
            try:
                row = con.execute(
                    "SELECT status,created_event_id,revoked_event_id FROM authority_grants WHERE grant_id='PON-GRANT-001'"
                ).fetchone()
                self.assertEqual(row[0], "REVOKED")
                self.assertTrue(row[1])
                self.assertEqual(row[2], revoked["eventId"])
                self.assertEqual(
                    con.execute(
                        "SELECT COUNT(*) FROM exact_author_actions WHERE consumed_event_id IS NOT NULL"
                    ).fetchone()[0],
                    2,
                )
                self.assertEqual(con.execute("SELECT COUNT(*) FROM hub_events").fetchone()[0], 2)
            finally:
                con.close()

    def test_endpoint_and_mailbox_lifecycle_is_atomic_generation_safe_and_cascades(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, credential = self._author_bootstrapped(root)
            self._seed_project(database)
            endpoint_request = self._author_request(
                "endpoint.allocate",
                "PON-REQUEST-ENDPOINT-001",
                "PON-ACTION-ENDPOINT-001",
                "Endpoint",
                "PON-ENDPOINT-001",
                0,
                {
                    "projectId": "PON-PROJECT-001",
                    "role": "project-worker",
                    "accessScope": {"projectIds": ["PON-PROJECT-001"]},
                },
            )
            endpoint = execute_operation(
                database,
                self._write_request(root, "endpoint.json", endpoint_request),
                credential,
                PLUGIN_ROOT,
            )
            self.assertEqual(endpoint["aggregateVersion"], 1)
            mailbox_request = self._author_request(
                "mailbox.allocate",
                "PON-REQUEST-MAILBOX-001",
                "PON-ACTION-MAILBOX-001",
                "Mailbox",
                "PON-MAILBOX-001",
                0,
                {"endpointId": "PON-ENDPOINT-001", "domain": "DEMO"},
            )
            mailbox = execute_operation(
                database,
                self._write_request(root, "mailbox.json", mailbox_request),
                credential,
                PLUGIN_ROOT,
            )
            self.assertEqual(mailbox["aggregateVersion"], 1)
            rotate_request = self._author_request(
                "mailbox.rotateGeneration",
                "PON-REQUEST-MAILBOX-ROTATE-001",
                "PON-ACTION-MAILBOX-ROTATE-001",
                "Mailbox",
                "PON-MAILBOX-001",
                1,
                {"mailboxId": "PON-MAILBOX-001", "reason": "Credential rotation"},
            )
            rotated = execute_operation(
                database,
                self._write_request(root, "mailbox-rotate.json", rotate_request),
                credential,
                PLUGIN_ROOT,
            )
            self.assertEqual(rotated["aggregateVersion"], 2)
            self.assertEqual(rotated["createdIds"], ["PON-MAILBOX-001"])
            revoke_request = self._author_request(
                "endpoint.revoke",
                "PON-REQUEST-ENDPOINT-REVOKE-001",
                "PON-ACTION-ENDPOINT-REVOKE-001",
                "Endpoint",
                "PON-ENDPOINT-001",
                1,
                {"endpointId": "PON-ENDPOINT-001", "reason": "Retirement"},
            )
            revoked = execute_operation(
                database,
                self._write_request(root, "endpoint-revoke.json", revoke_request),
                credential,
                PLUGIN_ROOT,
            )
            self.assertEqual(revoked["aggregateVersion"], 2)
            con = sqlite3.connect(database)
            try:
                self.assertEqual(
                    con.execute("SELECT status FROM endpoints WHERE endpoint_id='PON-ENDPOINT-001'").fetchone()[0],
                    "REVOKED",
                )
                self.assertEqual(
                    con.execute(
                        "SELECT status FROM actors WHERE actor_id=(SELECT actor_id FROM endpoints WHERE endpoint_id='PON-ENDPOINT-001')"
                    ).fetchone()[0],
                    "REVOKED",
                )
                generations = con.execute(
                    "SELECT generation,status FROM mailboxes WHERE mailbox_id='PON-MAILBOX-001' ORDER BY generation"
                ).fetchall()
                self.assertEqual(generations, [(1, "RETIRED"), (2, "REVOKED")])
                self.assertEqual(con.execute("SELECT COUNT(*) FROM hub_events").fetchone()[0], 5)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM idempotency_records").fetchone()[0], 4)
            finally:
                con.close()

    def test_maximum_length_mailbox_id_rotates_across_generation_digit_width(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, credential = self._author_bootstrapped(root)
            self._seed_project(database)
            execute_operation(
                database,
                self._write_request(
                    root,
                    "long-endpoint.json",
                    self._author_request(
                        "endpoint.allocate", "PON-REQUEST-LONG-ENDPOINT", "PON-ACTION-LONG-ENDPOINT",
                        "Endpoint", "PON-ENDPOINT-LONG", 0,
                        {
                            "projectId": "PON-PROJECT-001",
                            "role": "project-worker",
                            "accessScope": {"projectIds": ["PON-PROJECT-001"]},
                        },
                    ),
                ),
                credential,
                PLUGIN_ROOT,
            )
            mailbox_id = "M" + ("x" * 127)
            allocated = execute_operation(
                database,
                self._write_request(
                    root,
                    "long-mailbox.json",
                    self._author_request(
                        "mailbox.allocate", "PON-REQUEST-LONG-MAILBOX", "PON-ACTION-LONG-MAILBOX",
                        "Mailbox", mailbox_id, 0,
                        {"endpointId": "PON-ENDPOINT-LONG", "domain": "DEMO"},
                    ),
                ),
                credential,
                PLUGIN_ROOT,
            )
            self.assertEqual(allocated["createdIds"], [mailbox_id])
            latest = allocated
            for generation in range(2, 11):
                latest = execute_operation(
                    database,
                    self._write_request(
                        root,
                        f"long-mailbox-rotate-{generation}.json",
                        self._author_request(
                            "mailbox.rotateGeneration",
                            f"PON-REQUEST-LONG-ROTATE-{generation}",
                            f"PON-ACTION-LONG-ROTATE-{generation}",
                            "Mailbox",
                            mailbox_id,
                            generation - 1,
                            {"mailboxId": mailbox_id, "reason": f"Rotation {generation}"},
                        ),
                    ),
                    credential,
                    PLUGIN_ROOT,
                )
                self.assertEqual(latest["createdIds"], [mailbox_id])
            self.assertEqual(latest["aggregateVersion"], 10)
            con = sqlite3.connect(database)
            try:
                active = con.execute(
                    "SELECT generation FROM mailboxes WHERE mailbox_id=? AND status='ACTIVE'", (mailbox_id,)
                ).fetchall()
                self.assertEqual(active, [(10,)])
            finally:
                con.close()

    def test_one_shot_grant_consumption_is_a_separate_chained_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, author_credential = self._author_bootstrapped(root)
            self._seed_project(database)
            recipient_credential = root / "recipient-credential.json"
            create_kernel_credential(recipient_credential, "PON-CAPABILITY-RECIPIENT")
            recipient_secret = json.loads(recipient_credential.read_text(encoding="utf-8"))["secret"]
            con = sqlite3.connect(database)
            try:
                con.execute(
                    "INSERT INTO actors(actor_id,actor_kind,role,status,created_at) VALUES(?,?,?,?,?)",
                    ("PON-ACTOR-RECIPIENT", "BROWSER", "delegated-browser", "ACTIVE", "2026-09-11T00:00:00Z"),
                )
                con.execute(
                    """INSERT INTO caller_capabilities(
                       capability_id,actor_id,subject_kind,subject_id,allowed_operations_json,
                       secret_sha256,status,created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        "PON-CAPABILITY-RECIPIENT", "PON-ACTOR-RECIPIENT", "actor",
                        "PON-ACTOR-RECIPIENT", '["endpoint.allocate"]',
                        hashlib.sha256(recipient_secret.encode("utf-8")).hexdigest(),
                        "ACTIVE", "2026-09-11T00:00:00Z",
                    ),
                )
                con.commit()
            finally:
                con.close()
            grant_request = self._author_request(
                "authority.grant", "PON-REQUEST-GRANT-USE-001", "PON-ACTION-GRANT-USE-001",
                "AuthorityGrant", "PON-GRANT-USE-001", 0,
                {"grant": {
                    "grantorId": "PON-ACTOR-AUTHOR", "recipientId": "PON-ACTOR-RECIPIENT",
                    "allowedOperations": ["endpoint.allocate"],
                    "scope": {"projectIds": ["PON-PROJECT-001"]},
                    "classification": "ONE_SHOT", "maximumUses": 1,
                    "rationale": "One endpoint only", "sourceDecisionId": "PON-ACTION-GRANT-USE-001",
                }},
            )
            execute_operation(
                database, self._write_request(root, "grant-use.json", grant_request),
                author_credential, PLUGIN_ROOT,
            )
            delegated_request = {
                "schemaVersion": "1",
                "operation": "endpoint.allocate",
                "requestId": "PON-REQUEST-DELEGATED-ENDPOINT-001",
                "actor": {"id": "PON-ACTOR-RECIPIENT", "kind": "BROWSER", "role": "delegated-browser"},
                "authority": {
                    "capabilityId": "PON-CAPABILITY-RECIPIENT",
                    "authorityGrantId": "PON-GRANT-USE-001",
                },
                "aggregate": {"type": "Endpoint", "id": "PON-ENDPOINT-DELEGATED-001", "expectedVersion": 0},
                "parameters": {
                    "projectId": "PON-PROJECT-001", "role": "project-worker",
                    "accessScope": {"projectIds": ["PON-PROJECT-001"], "mailDomains": ["DEMO"]},
                },
            }
            result = execute_operation(
                database,
                self._write_request(root, "delegated-endpoint.json", delegated_request),
                recipient_credential,
                PLUGIN_ROOT,
            )
            self.assertEqual(result["aggregateVersion"], 1)
            con = sqlite3.connect(database)
            try:
                grant = con.execute(
                    "SELECT status,remaining_uses FROM authority_grants WHERE grant_id='PON-GRANT-USE-001'"
                ).fetchone()
                self.assertEqual(grant, ("EXHAUSTED", 0))
                events = con.execute(
                    "SELECT aggregate_type,aggregate_id,previous_event_sha256,event_sha256 FROM hub_events ORDER BY sequence"
                ).fetchall()
                self.assertEqual([event[0] for event in events], ["AuthorityGrant", "AuthorityGrant", "Endpoint"])
                self.assertEqual(events[2][2], events[1][3])
            finally:
                con.close()

    def test_delegated_endpoint_access_scope_is_attenuated_without_consuming_denied_grant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, author_credential = self._author_bootstrapped(root)
            self._seed_project(database)
            recipient_credential = root / "recipient-credential.json"
            create_kernel_credential(recipient_credential, "PON-CAPABILITY-RECIPIENT")
            recipient_secret = json.loads(recipient_credential.read_text(encoding="utf-8"))["secret"]
            con = sqlite3.connect(database)
            try:
                con.execute(
                    "INSERT INTO actors(actor_id,actor_kind,role,status,created_at) VALUES(?,?,?,?,?)",
                    ("PON-ACTOR-RECIPIENT", "BROWSER", "delegated-browser", "ACTIVE", "2026-09-11T00:00:00Z"),
                )
                con.execute(
                    """INSERT INTO caller_capabilities(
                       capability_id,actor_id,subject_kind,subject_id,allowed_operations_json,
                       secret_sha256,status,created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        "PON-CAPABILITY-RECIPIENT", "PON-ACTOR-RECIPIENT", "actor",
                        "PON-ACTOR-RECIPIENT", '["endpoint.allocate"]',
                        hashlib.sha256(recipient_secret.encode("utf-8")).hexdigest(),
                        "ACTIVE", "2026-09-11T00:00:00Z",
                    ),
                )
                con.commit()
            finally:
                con.close()
            grant_request = self._author_request(
                "authority.grant", "PON-REQUEST-GRANT-TASK", "PON-ACTION-GRANT-TASK",
                "AuthorityGrant", "PON-GRANT-TASK", 0,
                {"grant": {
                    "grantorId": "PON-ACTOR-AUTHOR", "recipientId": "PON-ACTOR-RECIPIENT",
                    "allowedOperations": ["endpoint.allocate"],
                    "scope": {"taskIds": ["PON-TASK-001"]},
                    "classification": "ONE_SHOT", "maximumUses": 1,
                    "rationale": "One task-bound endpoint", "sourceDecisionId": "PON-ACTION-GRANT-TASK",
                }},
            )
            # Seed the pre-existing task under a separate retained grant; P3.2 does not create tasks.
            con = sqlite3.connect(database)
            try:
                con.execute(
                    """INSERT INTO authority_grants(
                       grant_id,grantor_actor_id,recipient_actor_id,allowed_operations_json,
                       scope_json,classification,maximum_uses,remaining_uses,status,rationale,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "PON-GRANT-SEED", "PON-ACTOR-AUTHOR", "PON-ACTOR-AUTHOR", '[]',
                        '{"projectIds":["PON-PROJECT-001"]}', "STANDING", None, None,
                        "ACTIVE", "Imported task authority", "2026-09-11T00:00:00Z",
                    ),
                )
                con.execute(
                    """INSERT INTO tasks(
                       task_id,project_id,task_kind,objective,acceptance_contract_root,
                       authority_grant_id,state,aggregate_version,aggregate_root,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "PON-TASK-001", "PON-PROJECT-001", "EVALUATION", "Bounded test task",
                        "2" * 64, "PON-GRANT-SEED", "READY", 1, "3" * 64,
                        "2026-09-11T00:00:00Z",
                    ),
                )
                con.commit()
            finally:
                con.close()
            execute_operation(
                database, self._write_request(root, "grant-task.json", grant_request),
                author_credential, PLUGIN_ROOT,
            )
            denied = {
                "schemaVersion": "1",
                "operation": "endpoint.allocate",
                "requestId": "PON-REQUEST-ENDPOINT-DENIED",
                "actor": {"id": "PON-ACTOR-RECIPIENT", "kind": "BROWSER", "role": "delegated-browser"},
                "authority": {
                    "capabilityId": "PON-CAPABILITY-RECIPIENT",
                    "authorityGrantId": "PON-GRANT-TASK",
                },
                "aggregate": {"type": "Endpoint", "id": "PON-ENDPOINT-DENIED", "expectedVersion": 0},
                "parameters": {
                    "projectId": "PON-PROJECT-001", "taskId": "PON-TASK-001", "role": "project-worker",
                    "accessScope": {"taskIds": ["PON-TASK-001"], "mailDomains": ["DEMO"]},
                },
            }
            with self.assertRaises(PostOfficeError) as raised:
                execute_operation(
                    database, self._write_request(root, "endpoint-denied.json", denied),
                    recipient_credential, PLUGIN_ROOT,
                )
            self.assertEqual(raised.exception.code, "PON_AUTHORIZATION_DENIED")
            con = sqlite3.connect(database)
            try:
                self.assertEqual(
                    con.execute(
                        "SELECT status,remaining_uses FROM authority_grants WHERE grant_id='PON-GRANT-TASK'"
                    ).fetchone(),
                    ("ACTIVE", 1),
                )
                self.assertIsNone(
                    con.execute("SELECT 1 FROM endpoints WHERE endpoint_id='PON-ENDPOINT-DENIED'").fetchone()
                )
            finally:
                con.close()
            allowed = json.loads(json.dumps(denied))
            allowed["requestId"] = "PON-REQUEST-ENDPOINT-ALLOWED"
            allowed["aggregate"]["id"] = "PON-ENDPOINT-ALLOWED"
            allowed["parameters"]["accessScope"] = {"taskIds": ["PON-TASK-001"]}
            result = execute_operation(
                database, self._write_request(root, "endpoint-allowed.json", allowed),
                recipient_credential, PLUGIN_ROOT,
            )
            self.assertEqual(result["aggregateVersion"], 1)

    def test_runtime_contract_event_and_projection_integrity_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            copied_plugin = root / "plugin"
            (copied_plugin / "contracts").parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(PLUGIN_ROOT / "contracts", copied_plugin / "contracts")
            fixture = copied_plugin / "contracts" / "v1" / "fixtures" / "good" / "operation.hub.status.request.json"
            fixture.write_text(fixture.read_text(encoding="utf-8") + " ", encoding="utf-8")
            with self.assertRaises(PostOfficeError) as contract_error:
                _contract_root(copied_plugin)
            self.assertEqual(contract_error.exception.code, "PON_CONTRACT_INVALID")

            database, credential = self._author_bootstrapped(root)
            self._seed_project(database)
            execute_operation(
                database,
                self._write_request(
                    root,
                    "integrity-endpoint.json",
                    self._author_request(
                        "endpoint.allocate", "PON-REQUEST-INTEGRITY-ENDPOINT", "PON-ACTION-INTEGRITY-ENDPOINT",
                        "Endpoint", "PON-ENDPOINT-INTEGRITY", 0,
                        {
                            "projectId": "PON-PROJECT-001", "role": "project-worker",
                            "accessScope": {"projectIds": ["PON-PROJECT-001"]},
                        },
                    ),
                ),
                credential,
                PLUGIN_ROOT,
            )
            con = sqlite3.connect(database)
            con.row_factory = sqlite3.Row
            try:
                con.execute("DROP TRIGGER hub_events_no_update")
                con.execute("UPDATE hub_events SET event_sha256=? WHERE sequence=1", ("f" * 64,))
                with self.assertRaises(PostOfficeError) as chain_error:
                    _validate_hub_event_chain(con)
                self.assertEqual(chain_error.exception.code, "PON_DATABASE_INVALID")
                con.rollback()
                con.execute(
                    "UPDATE endpoints SET role='tampered-role' WHERE endpoint_id='PON-ENDPOINT-INTEGRITY'"
                )
                con.commit()
            finally:
                con.close()
            with self.assertRaises(PostOfficeError) as projection_error:
                execute_operation(
                    database,
                    self._write_request(root, "integrity-status.json", {
                        "schemaVersion": "1", "operation": "hub.status",
                        "requestId": "PON-REQUEST-INTEGRITY-STATUS",
                        "actor": {"id": "PON-ACTOR-AUTHOR", "kind": "HUMAN", "role": "author"},
                        "authority": {"capabilityId": "PON-CAPABILITY-AUTHOR"},
                        "parameters": {"includeDetails": True},
                    }),
                    credential,
                    PLUGIN_ROOT,
                )
            self.assertEqual(projection_error.exception.code, "PON_DATABASE_INVALID")

    def test_shared_concurrency_and_event_primitives_fail_closed_and_chain(self) -> None:
        aggregate = {"type": "Task", "id": "PON-TASK-001", "expectedVersion": 2}
        assert_aggregate_concurrency(
            aggregate,
            actual_type="Task",
            actual_id="PON-TASK-001",
            actual_version=2,
            actual_root="a" * 64,
        )
        with self.assertRaises(PostOfficeError) as raised:
            assert_aggregate_concurrency(
                aggregate,
                actual_type="Task",
                actual_id="PON-TASK-001",
                actual_version=3,
                actual_root="a" * 64,
            )
        self.assertEqual(raised.exception.code, "PON_CONCURRENCY_CONFLICT")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, _ = self._bootstrapped(root)
            con = sqlite3.connect(database)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                """INSERT INTO exact_author_actions(
                   action_id,actor_id,operation,scope_json,confirmation_sha256,recorded_at)
                   VALUES(?,?,?,?,?,?)""",
                (
                    "PON-ACTION-001", "PON-ACTOR-OPERATOR", "task.block", "{}",
                    "1" * 64, "2026-09-10T12:00:00Z",
                ),
            )
            request = {
                "actor": {"id": "PON-ACTOR-OPERATOR"},
                "operation": "task.block",
                "authority": {
                    "capabilityId": "PON-CAPABILITY-OPERATOR",
                    "exactAuthorActionId": "PON-ACTION-001",
                },
                "aggregate": {"type": "Task", "id": "PON-TASK-001"},
                "parameters": {"reason": "test"},
            }
            first = append_hub_event(
                con,
                request=request,
                capability_id="PON-CAPABILITY-OPERATOR",
                aggregate_version=1,
                before_root="0" * 64,
                after_root="1" * 64,
                event_result={"state": "BLOCKED"},
                occurred_at="2026-09-10T12:00:01Z",
            )
            second = append_hub_event(
                con,
                request=request,
                capability_id="PON-CAPABILITY-OPERATOR",
                aggregate_version=2,
                before_root="1" * 64,
                after_root="2" * 64,
                event_result={"state": "BLOCKED"},
                occurred_at="2026-09-10T12:00:02Z",
            )
            con.commit()
            previous = con.execute(
                "SELECT previous_event_sha256 FROM hub_events WHERE event_id=?",
                (second["eventId"],),
            ).fetchone()[0]
            con.close()
            self.assertEqual(previous, first["eventSha256"])

    def test_p3_3_project_plan_lifecycle_and_credential_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, credential = self._author_bootstrapped(root)
            plan_request = self._write_request(
                root, "project-plan.json",
                self._author_request(
                    "project.planCreate", "PON-REQUEST-PROJECT-PLAN", "PON-ACTION-PROJECT-PLAN",
                    "ProvisioningPlan", "PON-PLAN-PROJECT", 0,
                    {
                        "projectId": "PON-PROJECT-NEW", "projectCode": "NEWPROJECT",
                        "displayName": "New Project", "kind": "LOCAL",
                        "localProjectRoot": "L:/Codex/projects/new-project",
                        "localCasRoot": "L:/Codex/cas/new-project",
                        "localBackupRoot": "L:/Codex/backups/new-project",
                        "mailDomains": ["NEWPROJECT"],
                    },
                ),
            )
            plan = execute_operation(database, plan_request, credential, PLUGIN_ROOT)
            self.assertEqual(plan["provisioningPlan"]["stage"], "PLAN")
            create = execute_operation(
                database,
                self._write_request(
                    root, "project-create.json",
                    self._author_request(
                        "project.create", "PON-REQUEST-PROJECT-CREATE", "PON-ACTION-PROJECT-CREATE",
                        "Project", "PON-PROJECT-NEW", 0,
                        {"approvedPlanId": "PON-PLAN-PROJECT", "planRoot": plan["provisioningPlan"]["planRoot"]},
                    ),
                ),
                credential, PLUGIN_ROOT,
            )
            self.assertEqual(create["project"]["status"], "ACTIVE")
            inspection = execute_operation(
                database,
                self._write_request(root, "plan-inspect.json", {
                    "schemaVersion": "1", "operation": "provisioning.inspect",
                    "requestId": "PON-REQUEST-PLAN-INSPECT",
                    "actor": {"id": "PON-ACTOR-AUTHOR", "kind": "HUMAN", "role": "author"},
                    "authority": {"capabilityId": "PON-CAPABILITY-AUTHOR"},
                    "parameters": {"provisioningPlanId": "PON-PLAN-PROJECT"},
                }), credential, PLUGIN_ROOT,
            )
            self.assertEqual(inspection["provisioningPlan"]["stage"], "COMMITTED")

            bound_path = root / "project-caller.json"
            bound = bind_kernel_credential(
                database, credential, bound_path,
                action_id="PON-ACTION-BIND-CREDENTIAL", actor_id="PON-ACTOR-AUTHOR",
                capability_id="PON-CAPABILITY-PROJECT-CALLER", subject_kind="PROJECT",
                subject_id="PON-PROJECT-NEW", subject_generation=1,
                allowed_operations=["project.read", "task.read"], expires_at=None,
                plugin_root=PLUGIN_ROOT,
            )
            self.assertTrue(bound_path.is_file())
            self.assertNotIn("secret", bound)
            self.assertEqual(bound["allowedOperations"], ["project.read", "task.read"])

            for index, (operation, version, parameters, expected_status) in enumerate([
                ("project.update", 1, {"projectId": "PON-PROJECT-NEW", "changes": {"displayName": "Renamed Project", "policyRoot": "c" * 64}}, "ACTIVE"),
                ("project.pause", 2, {"projectId": "PON-PROJECT-NEW", "reason": "Maintenance"}, "PAUSED"),
                ("project.archive", 3, {"projectId": "PON-PROJECT-NEW", "reason": "Completed"}, "ARCHIVED"),
            ], start=1):
                result = execute_operation(
                    database,
                    self._write_request(root, f"project-step-{index}.json", self._author_request(
                        operation, f"PON-REQUEST-PROJECT-STEP-{index}", f"PON-ACTION-PROJECT-STEP-{index}",
                        "Project", "PON-PROJECT-NEW", version, parameters,
                    )), credential, PLUGIN_ROOT,
                )
                self.assertEqual(result["project"]["status"], expected_status)
            read = execute_operation(
                database,
                self._write_request(root, "project-read.json", {
                    "schemaVersion": "1", "operation": "project.read", "requestId": "PON-REQUEST-PROJECT-READ",
                    "actor": {"id": "PON-ACTOR-AUTHOR", "kind": "HUMAN", "role": "author"},
                    "authority": {"capabilityId": "PON-CAPABILITY-AUTHOR"},
                    "parameters": {"projectId": "PON-PROJECT-NEW"},
                }), credential, PLUGIN_ROOT,
            )
            self.assertEqual(read["project"]["displayName"], "Renamed Project")
            self.assertEqual(read["project"]["status"], "ARCHIVED")

    def test_p3_3_task_provisioning_binding_response_review_and_close(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, credential = self._author_bootstrapped(root)
            self._seed_project(database)
            grant = execute_operation(
                database,
                self._write_request(root, "task-grant.json", self._author_request(
                    "authority.grant", "PON-REQUEST-TASK-GRANT", "PON-ACTION-TASK-GRANT",
                    "AuthorityGrant", "PON-GRANT-TASK", 0,
                    {"grant": {
                        "grantorId": "PON-ACTOR-AUTHOR", "recipientId": "PON-ACTOR-AUTHOR",
                        "allowedOperations": ["task.activate", "task.recordResponse"],
                        "scope": {"projectIds": ["PON-PROJECT-001"]}, "classification": "STANDING",
                        "rationale": "Task execution authority", "sourceDecisionId": "PON-ACTION-TASK-GRANT",
                    }},
                )), credential, PLUGIN_ROOT,
            )
            self.assertTrue(grant["ok"])
            plan = execute_operation(
                database,
                self._write_request(root, "task-plan.json", self._author_request(
                    "task.planCreate", "PON-REQUEST-TASK-PLAN", "PON-ACTION-TASK-PLAN",
                    "ProvisioningPlan", "PON-PLAN-TASK", 0,
                    {"taskId": "PON-TASK-NEW", "projectId": "PON-PROJECT-001",
                     "taskKind": "PROJECT_MANAGEMENT", "objective": "Exercise the task lifecycle",
                     "acceptanceContractRoot": "a" * 64, "authorityGrantId": "PON-GRANT-TASK"},
                )), credential, PLUGIN_ROOT,
            )
            task = execute_operation(
                database,
                self._write_request(root, "task-create.json", self._author_request(
                    "task.create", "PON-REQUEST-TASK-CREATE", "PON-ACTION-TASK-CREATE",
                    "Task", "PON-TASK-NEW", 0,
                    {"approvedPlanId": "PON-PLAN-TASK", "planRoot": plan["provisioningPlan"]["planRoot"]},
                )), credential, PLUGIN_ROOT,
            )
            self.assertEqual(task["task"]["state"], "PROVISIONING")

            execute_operation(database, self._write_request(root, "endpoint.json", self._author_request(
                "endpoint.allocate", "PON-REQUEST-TASK-ENDPOINT", "PON-ACTION-TASK-ENDPOINT",
                "Endpoint", "PON-ENDPOINT-TASK", 0,
                {"projectId": "PON-PROJECT-001", "taskId": "PON-TASK-NEW", "role": "worker",
                 "accessScope": {"projectIds": ["PON-PROJECT-001"], "taskIds": ["PON-TASK-NEW"]}},
            )), credential, PLUGIN_ROOT)
            execute_operation(database, self._write_request(root, "mailbox.json", self._author_request(
                "mailbox.allocate", "PON-REQUEST-TASK-MAILBOX", "PON-ACTION-TASK-MAILBOX",
                "Mailbox", "PON-MAILBOX-TASK", 0,
                {"endpointId": "PON-ENDPOINT-TASK", "domain": "DEMO"},
            )), credential, PLUGIN_ROOT)

            lifecycle = [
                ("task.bindEndpoint", 1, {"taskId": "PON-TASK-NEW", "endpointId": "PON-ENDPOINT-TASK", "mailboxGeneration": 1}, "READY"),
                ("task.activate", 2, {"taskId": "PON-TASK-NEW"}, "ACTIVE"),
                ("task.recordResponse", 3, {"taskId": "PON-TASK-NEW", "messageId": "PON-MESSAGE-RESPONSE", "evidenceRoots": ["b" * 64]}, "RESPONSE_RETURNED"),
                ("task.review", 4, {"taskId": "PON-TASK-NEW", "decision": "ACCEPTED", "rationale": "Meets contract"}, "ACCEPTED"),
                ("task.close", 5, {"taskId": "PON-TASK-NEW", "summary": "Complete"}, "CLOSED"),
            ]
            for index, (operation, version, parameters, expected_state) in enumerate(lifecycle, start=1):
                result = execute_operation(
                    database,
                    self._write_request(root, f"task-step-{index}.json", self._author_request(
                        operation, f"PON-REQUEST-TASK-STEP-{index}", f"PON-ACTION-TASK-STEP-{index}",
                        "Task", "PON-TASK-NEW", version, parameters,
                    )), credential, PLUGIN_ROOT,
                )
                self.assertEqual(result["task"]["state"], expected_state)
            con = sqlite3.connect(database)
            try:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM task_responses").fetchone()[0], 1)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM task_reviews").fetchone()[0], 1)
                self.assertEqual(con.execute("SELECT state FROM tasks WHERE task_id='PON-TASK-NEW'").fetchone()[0], "CLOSED")
            finally:
                con.close()

    def test_p3_4_semantic_package_cycle_and_message_registration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, credential = self._author_bootstrapped(root)
            self._seed_project(database)

            grant = execute_operation(database, self._write_request(root, "semantic-grant.json", self._author_request(
                "authority.grant", "PON-REQUEST-SEMANTIC-GRANT", "PON-ACTION-SEMANTIC-GRANT",
                "AuthorityGrant", "PON-GRANT-SEMANTIC", 0,
                {"grant": {"grantorId": "PON-ACTOR-AUTHOR", "recipientId": "PON-ACTOR-AUTHOR",
                           "allowedOperations": ["message.register", "message.route"],
                           "scope": {"projectIds": ["PON-PROJECT-001"]}, "classification": "STANDING",
                           "rationale": "Semantic message authority", "sourceDecisionId": "PON-ACTION-SEMANTIC-GRANT"}},
            )), credential, PLUGIN_ROOT)
            self.assertTrue(grant["ok"])

            con = sqlite3.connect(database)
            try:
                con.execute(
                    """INSERT INTO tasks(task_id,project_id,task_kind,objective,acceptance_contract_root,
                       authority_grant_id,state,aggregate_version,aggregate_root,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    ("PON-TASK-REQUESTER", "PON-PROJECT-001", "PROJECT_MANAGEMENT", "Request review",
                     "9" * 64, "PON-GRANT-SEMANTIC", "ACTIVE", 1, "8" * 64, "2026-09-11T00:00:00Z"),
                )
                con.commit()
            finally:
                con.close()

            package = execute_operation(database, self._write_request(root, "package-register.json", self._author_request(
                "package.register", "PON-REQUEST-PACKAGE", "PON-ACTION-PACKAGE",
                "SoftwarePackage", "PON-PACKAGE-SEMANTIC", 0,
                {"package": {"id": "PON-PACKAGE-SEMANTIC", "displayName": "Semantic package",
                             "owningProjectId": "PON-PROJECT-001", "sourceLocation": "packages/semantic",
                             "licence": "MPL-2.0", "providedInterfaceIds": ["PON-INTERFACE-SEMANTIC"],
                             "requiredInterfaceIds": []}},
            )), credential, PLUGIN_ROOT)
            self.assertTrue(package["ok"])
            interface = execute_operation(database, self._write_request(root, "interface-register.json", self._author_request(
                "interface.register", "PON-REQUEST-INTERFACE", "PON-ACTION-INTERFACE",
                "Interface", "PON-INTERFACE-SEMANTIC", 0,
                {"interface": {"id": "PON-INTERFACE-SEMANTIC", "version": "1.0.0",
                               "stewardId": "PON-PACKAGE-SEMANTIC", "status": "SUPPORTED",
                               "humanGuide": "docs/interface.md", "providerIds": ["PON-PACKAGE-SEMANTIC"],
                               "consumerIds": []}},
            )), credential, PLUGIN_ROOT)
            self.assertTrue(interface["ok"])

            for endpoint_id in ("PON-ENDPOINT-SENDER", "PON-ENDPOINT-RECIPIENT"):
                execute_operation(database, self._write_request(root, f"{endpoint_id}.json", self._author_request(
                    "endpoint.allocate", f"PON-REQUEST-{endpoint_id}", f"PON-ACTION-{endpoint_id}",
                    "Endpoint", endpoint_id, 0,
                    {"projectId": "PON-PROJECT-001", "role": "semantic-peer",
                     **({"actorKind": "BROWSER"} if endpoint_id == "PON-ENDPOINT-RECIPIENT" else {}),
                     "accessScope": {"projectIds": ["PON-PROJECT-001"]}},
                )), credential, PLUGIN_ROOT)
            execute_operation(database, self._write_request(root, "recipient-mailbox.json", self._author_request(
                "mailbox.allocate", "PON-REQUEST-RECIPIENT-MAILBOX", "PON-ACTION-RECIPIENT-MAILBOX",
                "Mailbox", "PON-MAILBOX-RECIPIENT", 0,
                {"endpointId": "PON-ENDPOINT-RECIPIENT", "domain": "DEMO"},
            )), credential, PLUGIN_ROOT)
            execute_operation(database, self._write_request(root, "cycle-open.json", self._author_request(
                "cycle.open", "PON-REQUEST-CYCLE-OPEN", "PON-ACTION-CYCLE-OPEN",
                "SemanticCycle", "PON-CYCLE-SEMANTIC", 0,
                {"cycleId": "PON-CYCLE-SEMANTIC", "projectId": "PON-PROJECT-001", "scope": "Semantic test"},
            )), credential, PLUGIN_ROOT)

            bundle_bytes = b"0123456789"
            bundle_hash = hashlib.sha256(bundle_bytes).hexdigest()
            bundle_path = root / "sha256" / "dd" / "object"
            bundle_path.parent.mkdir(parents=True, exist_ok=True)
            bundle_path.write_bytes(bundle_bytes)
            con = sqlite3.connect(database)
            try:
                con.execute(
                    "INSERT INTO storage_copies VALUES(?,?,?,?,?,?,?,?)",
                    ("PON-COPY-BUNDLE", bundle_hash, "LOCAL_CAS", "sha256/dd/object", 10,
                     "AUTHORITATIVE", "SHA256_READBACK", "2026-09-11T00:00:00Z"),
                )
                con.commit()
            finally:
                con.close()

            message = {
                "id": "PON-MESSAGE-SEMANTIC", "domain": "DEMO", "messageType": "HANDOFF",
                "senderEndpointId": "PON-ENDPOINT-SENDER", "recipientMailboxId": "PON-MAILBOX-RECIPIENT",
                "recipientGeneration": 1, "relatedMessageIds": [], "authorityBasisId": "PON-GRANT-SEMANTIC",
                "requestedAction": "Process the package", "completionCriteria": "Return evidence",
                "contentRoot": "e" * 64, "semanticCycleId": "PON-CYCLE-SEMANTIC",
            }
            bundle = {"id": "PON-BUNDLE-SEMANTIC", "semanticMessageId": "PON-MESSAGE-SEMANTIC",
                      "canonicalFilename": "PON-MESSAGE-SEMANTIC_v01.zip", "bundleVersion": 1,
                      "sizeBytes": 10, "sha256": bundle_hash, "manifestRoot": "f" * 64,
                      "payloads": [{"path": "message.json", "sizeBytes": 10, "sha256": "a" * 64}]}
            execute_operation(database, self._write_request(root, "message-plan.json", self._author_request(
                "message.plan", "PON-REQUEST-MESSAGE-PLAN", "PON-ACTION-MESSAGE-PLAN",
                "MessagePlan", "PON-MESSAGE-PLAN", 0, {"semanticMessage": message, "bundle": bundle},
            )), credential, PLUGIN_ROOT)
            registered = execute_operation(database, self._write_request(root, "message-register.json", self._author_request(
                "message.register", "PON-REQUEST-MESSAGE-REGISTER", "PON-ACTION-MESSAGE-REGISTER",
                "SemanticMessage", "PON-MESSAGE-SEMANTIC", 0,
                {"semanticMessage": message, "bundleId": "PON-BUNDLE-SEMANTIC"},
            )), credential, PLUGIN_ROOT)
            self.assertTrue(registered["ok"])
            routed = execute_operation(database, self._write_request(root, "message-route.json", self._author_request(
                "message.route", "PON-REQUEST-MESSAGE-ROUTE", "PON-ACTION-MESSAGE-ROUTE",
                "SemanticMessage", "PON-MESSAGE-SEMANTIC", 1,
                {"semanticMessageId": "PON-MESSAGE-SEMANTIC", "bundleId": "PON-BUNDLE-SEMANTIC",
                 "destinationMailboxId": "PON-MAILBOX-RECIPIENT", "destinationGeneration": 1},
            )), credential, PLUGIN_ROOT)
            self.assertTrue(routed["ok"])
            con = sqlite3.connect(database)
            try:
                self.assertEqual(con.execute("SELECT state FROM semantic_messages").fetchone()[0], "CUSTODY_RECORDED")
                self.assertEqual(con.execute("SELECT state FROM transport_attempts").fetchone()[0], "PENDING")
            finally:
                con.close()

            create_kernel_actor(
                database, credential, action_id="PON-ACTION-COURIER-ACTOR",
                actor_id="PON-ACTOR-COURIER", actor_kind="COURIER", actor_role="courier",
                plugin_root=PLUGIN_ROOT,
            )
            courier_credential = root / "courier.json"
            bind_kernel_credential(
                database, credential, courier_credential,
                action_id="PON-ACTION-COURIER-CREDENTIAL", actor_id="PON-ACTOR-COURIER",
                capability_id="PON-CAPABILITY-COURIER", subject_kind="SYSTEM",
                subject_id="PON-KERNEL", subject_generation=1,
                allowed_operations=["transport.inspect", "transport.retry", "transport.quarantine", "transport.tombstoneDuplicate"],
                expires_at=None, plugin_root=PLUGIN_ROOT,
            )
            con = sqlite3.connect(database)
            try:
                con.execute(
                    """INSERT INTO reviewer_instances(
                       reviewer_thread_id,endpoint_id,label,status,rotation_order,guidance_url,
                       minimum_interval_minutes,last_submission_at,registered_at,updated_at,retired_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    ("00000000-0000-0000-0000-000000000001", "PON-ENDPOINT-RECIPIENT", "Reviewer 1", "ACTIVE", 1,
                     None, 30, None, "2026-09-04T12:00:00Z", "2026-09-04T12:00:00Z", None),
                )
                con.commit()
            finally:
                con.close()
            reconciled = reconcile_transport(database, courier_credential, PLUGIN_ROOT)
            self.assertEqual(len(reconciled["createdDispatchIds"]), 1)
            con = sqlite3.connect(database)
            try:
                con.execute(
                    "UPDATE actors SET status='REVOKED' WHERE actor_id=(SELECT actor_id FROM endpoints WHERE endpoint_id='PON-ENDPOINT-RECIPIENT')"
                )
                con.commit()
            finally:
                con.close()

            held = claim_next_transport(database, courier_credential, PLUGIN_ROOT, lease_seconds=60)
            self.assertFalse(held["available"])
            self.assertEqual(held["heldDispatchIds"], reconciled["createdDispatchIds"])
            con = sqlite3.connect(database)
            try:
                self.assertEqual(
                    con.execute(
                        "SELECT state FROM transport_dispatches WHERE dispatch_id=?",
                        (reconciled["createdDispatchIds"][0],),
                    ).fetchone()[0],
                    "RECONCILIATION_REQUIRED",
                )
                con.execute(
                    "UPDATE actors SET status='ACTIVE' WHERE actor_id=(SELECT actor_id FROM endpoints WHERE endpoint_id='PON-ENDPOINT-RECIPIENT')"
                )
                con.execute(
                    "UPDATE transport_dispatches SET state='READY',last_error_code=NULL WHERE dispatch_id=?",
                    (reconciled["createdDispatchIds"][0],),
                )
                con.commit()
            finally:
                con.close()

            ensured = ensure_automatic_review(
                database, courier_credential, PLUGIN_ROOT,
                review_id="PON-REVIEW-001", semantic_message_id="PON-MESSAGE-SEMANTIC",
                requester_task_id="PON-TASK-REQUESTER", reviewer_endpoint_id="PON-ENDPOINT-RECIPIENT",
                package_sha256=bundle_hash,
            )
            self.assertTrue(ensured["created"])
            claimed_review = claim_next_review(database, courier_credential, PLUGIN_ROOT)
            self.assertEqual(claimed_review["reviewId"], "PON-REVIEW-001")
            # Imported legacy rows may retain a wrapper-bundle digest that differs from the
            # separately verified review package digest. Activation follows the review's exact
            # package custody fields, not that migration-only wrapper identity.
            con = sqlite3.connect(database)
            try:
                con.execute(
                    "UPDATE message_bundles SET sha256=? WHERE bundle_id='PON-BUNDLE-SEMANTIC'",
                    ("0" * 64,),
                )
                con.commit()
            finally:
                con.close()
            activation_dispatch_id = "PON-ARD-REVIEW-001"
            activation = issue_automatic_review_activation_manifest(
                database, courier_credential, PLUGIN_ROOT,
                review_id="PON-REVIEW-001", activation_dispatch_id=activation_dispatch_id,
                idempotency_key="review-001-activation",
            )
            self.assertFalse(activation["replayed"])
            activation_manifest = json.loads(
                Path(activation["manifestPath"]).read_text(encoding="utf-8")
            )
            self.assertEqual(activation_manifest["kind"], "AUTOMATIC_REVIEW_ACTIVATION")
            self.assertEqual(activation_manifest["dispatchId"], activation_dispatch_id)
            self.assertEqual(len(activation_manifest["attachments"]), 1)
            activation_attachment = activation_manifest["attachments"][0]
            self.assertEqual(activation_attachment["sha256"], bundle_hash)
            self.assertEqual(
                hashlib.sha256(Path(activation_attachment["path"]).read_bytes()).hexdigest(),
                bundle_hash,
            )
            self.assertTrue(issue_automatic_review_activation_manifest(
                database, courier_credential, PLUGIN_ROOT,
                review_id="PON-REVIEW-001", activation_dispatch_id=activation_dispatch_id,
                idempotency_key="review-001-activation",
            )["replayed"])
            activation_source_message_id = "00000000-0000-0000-0000-000000000003"
            activation_receipt_reference = (
                "playwright-chatgpt-review-activation:PON-REVIEW-001:"
                f"{activation_dispatch_id}:00000000-0000-0000-0000-000000000001:"
                f"{activation_source_message_id}"
            )
            activation_receipt = record_automatic_review_activation_receipt(
                database, courier_credential, PLUGIN_ROOT,
                review_id="PON-REVIEW-001", activation_dispatch_id=activation_dispatch_id,
                source_message_id=activation_source_message_id,
                receipt_reference=activation_receipt_reference,
            )
            self.assertEqual(activation_receipt["state"], "SENT")
            self.assertTrue(record_automatic_review_activation_receipt(
                database, courier_credential, PLUGIN_ROOT,
                review_id="PON-REVIEW-001", activation_dispatch_id=activation_dispatch_id,
                source_message_id=activation_source_message_id,
                receipt_reference=activation_receipt_reference,
            )["replayed"])
            con = sqlite3.connect(database)
            try:
                con.execute(
                    "UPDATE message_bundles SET sha256=? WHERE bundle_id='PON-BUNDLE-SEMANTIC'",
                    (bundle_hash,),
                )
                con.commit()
            finally:
                con.close()
            review_collection = issue_automatic_review_result_collection_manifest(
                database, courier_credential, PLUGIN_ROOT,
                review_id="PON-REVIEW-001", activation_dispatch_id=activation_dispatch_id,
                verdict="PASS", source_thread_id="00000000-0000-0000-0000-000000000001",
                source_turn_id="00000000-0000-0000-0000-000000000002",
                attachment_reference=':chatgpt-content-reference{index="0"}',
                attachment_name="PON-REVIEW-001_RESULT.md", expected_sha256="7" * 64,
                expected_size_bytes=1234, observed_at="2026-09-12T00:00:00Z",
            )
            self.assertFalse(review_collection["replayed"])
            self.assertEqual(
                json.loads(Path(review_collection["manifestPath"]).read_text(encoding="utf-8"))["scopeKind"],
                "AUTOMATIC_REVIEW",
            )
            with self.assertRaises(PostOfficeError):
                withdraw_automatic_review(
                    database, courier_credential, PLUGIN_ROOT, review_id="PON-REVIEW-001",
                    reason="Too late after browser activation",
                )

            # Simulate a pre-fix review custody attempt and the unsent ordinary dispatch that an
            # old reconciliation pass could materialize for it.
            con = sqlite3.connect(database)
            try:
                recorded_at = "2026-09-12T00:00:00Z"
                con.execute(
                    "INSERT INTO transport_attempts VALUES(?,?,?,?,?,'PENDING',3,NULL,?,?)",
                    ("PON-TRANSPORT-REVIEW-LEGACY", "PON-MESSAGE-SEMANTIC",
                     "PON-BUNDLE-SEMANTIC", "AUTOMATIC_REVIEW_COMPANION",
                     "PON-MAILBOX-RECIPIENT", recorded_at, recorded_at),
                )
                con.execute(
                    """INSERT INTO transport_dispatches(
                       dispatch_id,transport_attempt_id,channel,destination_endpoint_id,
                       destination_mailbox_id,destination_generation,state,observable_marker,
                       attempt_count,next_attempt_at,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,'READY',?,0,?,?,?)""",
                    ("PON-DISPATCH-REVIEW-LEGACY", "PON-TRANSPORT-REVIEW-LEGACY",
                     "PLAYWRIGHT_BROWSER", "PON-ENDPOINT-RECIPIENT", "PON-MAILBOX-RECIPIENT",
                     1, "POST-OFFICE-PLAYWRIGHT-DISPATCH PON-DISPATCH-REVIEW-LEGACY",
                     recorded_at, recorded_at, recorded_at),
                )
                con.commit()
            finally:
                con.close()
            self.assertEqual(
                reconcile_transport(database, courier_credential, PLUGIN_ROOT)["createdDispatchIds"],
                [],
            )
            retired = retire_review_owned_transport_dispatches(
                database, courier_credential, PLUGIN_ROOT
            )
            self.assertEqual(
                retired["retiredDispatchIds"], ["PON-DISPATCH-REVIEW-LEGACY"]
            )
            self.assertEqual(
                retire_review_owned_transport_dispatches(
                    database, courier_credential, PLUGIN_ROOT
                )["retiredDispatchIds"],
                [],
            )
            con = sqlite3.connect(database)
            try:
                self.assertEqual(
                    con.execute(
                        "SELECT state FROM transport_attempts WHERE transport_attempt_id='PON-TRANSPORT-REVIEW-LEGACY'"
                    ).fetchone()[0],
                    "STORED",
                )
                self.assertEqual(
                    con.execute(
                        "SELECT state,last_error_code FROM transport_dispatches WHERE dispatch_id='PON-DISPATCH-REVIEW-LEGACY'"
                    ).fetchone(),
                    ("CANCELLED", "PON_REVIEW_ACTIVATION_OWNS_TRANSPORT"),
                )
            finally:
                con.close()
            claim = claim_next_transport(database, courier_credential, PLUGIN_ROOT, lease_seconds=60)
            self.assertTrue(claim["available"])
            con = sqlite3.connect(database)
            try:
                con.execute(
                    "UPDATE transport_dispatches SET lease_expires_at='2000-01-01T00:00:00Z' WHERE dispatch_id=?",
                    (claim["dispatchId"],),
                )
                con.commit()
            finally:
                con.close()
            recovered = reconcile_transport(
                database, courier_credential, PLUGIN_ROOT,
                observations={claim["dispatchId"]: {"markerAbsent": True}},
            )
            self.assertEqual(recovered["recoveredDispatchIds"], [claim["dispatchId"]])
            claim = claim_next_transport(database, courier_credential, PLUGIN_ROOT, lease_seconds=60)
            delivered = complete_transport(
                database, courier_credential, PLUGIN_ROOT,
                dispatch_id=claim["dispatchId"], lease_token=claim["leaseToken"],
                observable_marker=claim["observableMarker"], observed_receipt_id="PON-OBSERVED-RECEIPT-001",
            )
            self.assertTrue(delivered["ok"])
            supplemental = record_recovered_transport_receipt(
                database,
                courier_credential,
                PLUGIN_ROOT,
                message_id="PON-MESSAGE-SEMANTIC",
                bundle_id="PON-BUNDLE-SEMANTIC",
                channel="PLAYWRIGHT_BROWSER",
                observable_marker="POST-OFFICE-PLAYWRIGHT-DISPATCH PON-RECOVERY-001",
                observed_receipt_id="playwright-chatgpt:PON-RECOVERY-001:PON-THREAD-001",
            )
            self.assertFalse(supplemental["replayed"])
            replayed = record_recovered_transport_receipt(
                database,
                courier_credential,
                PLUGIN_ROOT,
                message_id="PON-MESSAGE-SEMANTIC",
                bundle_id="PON-BUNDLE-SEMANTIC",
                channel="PLAYWRIGHT_BROWSER",
                observable_marker="POST-OFFICE-PLAYWRIGHT-DISPATCH PON-RECOVERY-001",
                observed_receipt_id="playwright-chatgpt:PON-RECOVERY-001:PON-THREAD-001",
            )
            self.assertTrue(replayed["replayed"])
            con = sqlite3.connect(database)
            try:
                self.assertEqual(con.execute("SELECT state FROM semantic_messages").fetchone()[0], "DELIVERED")
                self.assertEqual(con.execute("SELECT COUNT(*) FROM transport_attempts WHERE state='RECEIPTED'").fetchone()[0], 2)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM transport_dispatches WHERE state='RECEIPTED'").fetchone()[0], 2)
                self.assertEqual(con.execute(
                    "SELECT COUNT(*) FROM runtime_receipts WHERE receipt_kind='RECOVERED' AND dispatch_id=?",
                    (supplemental["dispatchId"],),
                ).fetchone()[0], 1)
            finally:
                con.close()
    def test_p3_6_snapshot_and_retained_reconciliation_plan_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, credential = self._author_bootstrapped(root)
            evidence_json = json.dumps({"source": "shadow-test"}, separators=(",", ":"))
            evidence_root = hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()
            con = sqlite3.connect(database)
            try:
                con.execute(
                    """INSERT INTO shadow_observations(
                       observation_id,source_kind,source_reference,expected_root,observed_root,
                       classification,evidence_json,evidence_root,state,recorded_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "PON-OBSERVATION-001", "LIVE_PROJECTION", "test:projection",
                        "a" * 64, "a" * 64, "MATCH", evidence_json, evidence_root,
                        "OPEN", "2026-09-11T00:00:00Z",
                    ),
                )
                con.commit()
            finally:
                con.close()
            snapshot_request = {
                "schemaVersion": "1", "operation": "hub.snapshot",
                "requestId": "PON-REQUEST-HUB-SNAPSHOT",
                "actor": {"id": "PON-ACTOR-AUTHOR", "kind": "HUMAN", "role": "author"},
                "authority": {"capabilityId": "PON-CAPABILITY-AUTHOR"},
                "parameters": {"includeFileHashes": True},
            }
            snapshot = execute_operation(
                database, self._write_request(root, "hub-snapshot.json", snapshot_request),
                credential, PLUGIN_ROOT,
            )
            self.assertEqual(snapshot["resource"]["counts"]["openShadowObservations"], 1)
            preview = execute_operation(
                database,
                self._write_request(root, "reconcile-preview.json", self._author_request(
                    "hub.reconcile.preview", "PON-REQUEST-RECONCILE-PREVIEW",
                    "PON-ACTION-RECONCILE-PREVIEW", "ReconciliationPlan", "PON-PLAN-RECONCILE",
                    0, {"snapshotRoot": snapshot["resource"]["snapshotRoot"], "assertionSetIds": []},
                )), credential, PLUGIN_ROOT,
            )
            self.assertEqual(preview["resource"]["automaticActionCount"], 1)
            applied = execute_operation(
                database,
                self._write_request(root, "reconcile-apply.json", self._author_request(
                    "hub.reconcile.apply", "PON-REQUEST-RECONCILE-APPLY",
                    "PON-ACTION-RECONCILE-APPLY", "ReconciliationPlan", "PON-PLAN-RECONCILE",
                    1, {"previewPlanId": "PON-PLAN-RECONCILE", "planRoot": preview["resource"]["planRoot"]},
                )), credential, PLUGIN_ROOT,
            )
            self.assertTrue(applied["ok"])
            con = sqlite3.connect(database)
            try:
                self.assertEqual(con.execute("SELECT state FROM shadow_observations").fetchone()[0], "RESOLVED")
                self.assertEqual(con.execute("SELECT state FROM reconciliation_plans").fetchone()[0], "APPLIED")
            finally:
                con.close()

    def test_continuation_destination_normalizes_browser_binding(self) -> None:
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        try:
            con.executescript(
                """
                CREATE TABLE endpoints(
                    endpoint_id TEXT PRIMARY KEY,
                    access_scope_json TEXT NOT NULL
                );
                CREATE TABLE mailboxes(
                    mailbox_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    endpoint_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    PRIMARY KEY(mailbox_id,generation)
                );
                """
            )
            con.execute(
                "INSERT INTO endpoints VALUES(?,?)",
                (
                    "PON-ENDPOINT-BROWSER",
                    json.dumps(
                        {
                            "browserBinding": {
                                "chat_thread_id": "PON-BROWSER-THREAD",
                                "chat_url": "https://chatgpt.example/c/PON-BROWSER-THREAD",
                            }
                        }
                    ),
                ),
            )
            con.execute(
                "INSERT INTO mailboxes VALUES(?,?,?,?)",
                ("PON-MAILBOX-BROWSER", 2, "PON-ENDPOINT-BROWSER", "ACTIVE"),
            )
            resolved = _continuation_destination(
                con,
                {"recipient_mailbox_id": "PON-MAILBOX-BROWSER", "recipient_generation": 2},
            )
            self.assertEqual(resolved["destinationThreadId"], "PON-BROWSER-THREAD")
            self.assertEqual(
                resolved["destinationUrl"],
                "https://chatgpt.example/c/PON-BROWSER-THREAD",
            )
            self.assertEqual(resolved["destinationGeneration"], 2)
        finally:
            con.close()

    def test_obsolete_continuation_is_retired_without_claiming_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, author_credential = self._author_bootstrapped(root)
            create_kernel_actor(
                database,
                author_credential,
                action_id="PON-ACTION-CONTINUATION-COURIER",
                actor_id="PON-ACTOR-CONTINUATION-COURIER",
                actor_kind="COURIER",
                actor_role="courier",
                plugin_root=PLUGIN_ROOT,
            )
            courier_credential = root / "continuation-courier.json"
            bind_kernel_credential(
                database,
                author_credential,
                courier_credential,
                action_id="PON-ACTION-CONTINUATION-CREDENTIAL",
                actor_id="PON-ACTOR-CONTINUATION-COURIER",
                capability_id="PON-CAPABILITY-CONTINUATION-COURIER",
                subject_kind="SYSTEM",
                subject_id="PON-KERNEL",
                subject_generation=1,
                allowed_operations=["transport.inspect"],
                expires_at=None,
                plugin_root=PLUGIN_ROOT,
            )
            payload_json = json.dumps({"legacyId": "OLD-COLLECTION"}, separators=(",", ":"))
            con = sqlite3.connect(database)
            try:
                con.execute(
                    """INSERT INTO continuation_items(
                       continuation_id,source_table,source_id,continuation_kind,state,payload_json,
                       evidence_root,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        "PON-CONTINUATION-OBSOLETE",
                        "legacy_collections",
                        "OLD-COLLECTION#1",
                        "BROWSER_COLLECTION",
                        "READY",
                        payload_json,
                        hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
                        "2026-09-11T00:00:00Z",
                        "2026-09-11T00:00:00Z",
                    ),
                )
                con.commit()
            finally:
                con.close()
            claim = claim_next_continuation(
                database,
                courier_credential,
                PLUGIN_ROOT,
                continuation_kind="BROWSER_COLLECTION",
                lease_seconds=60,
            )
            con = sqlite3.connect(database)
            try:
                con.execute(
                    "UPDATE continuation_items SET lease_expires_at='2000-01-01T00:00:00Z' WHERE continuation_id=?",
                    (claim["continuationId"],),
                )
                con.commit()
            finally:
                con.close()
            ambiguous = reconcile_continuations(database, courier_credential, PLUGIN_ROOT)
            self.assertEqual(ambiguous["recoveredContinuationIds"], [])
            self.assertEqual(len(ambiguous["attentionIds"]), 1)
            con = sqlite3.connect(database)
            try:
                self.assertEqual(
                    con.execute(
                        "SELECT state FROM continuation_items WHERE continuation_id=?",
                        (claim["continuationId"],),
                    ).fetchone()[0],
                    "LEASED",
                )
            finally:
                con.close()
            recovered = reconcile_continuations(
                database,
                courier_credential,
                PLUGIN_ROOT,
                observations={
                    claim["continuationId"]: {
                        "actionAbsent": True,
                        "evidenceId": "PON-ABSENCE-EVIDENCE-001",
                    }
                },
            )
            self.assertEqual(recovered["recoveredContinuationIds"], [claim["continuationId"]])
            claim = claim_next_continuation(
                database,
                courier_credential,
                PLUGIN_ROOT,
                continuation_kind="BROWSER_COLLECTION",
                lease_seconds=60,
            )
            retired = retire_continuation(
                database,
                courier_credential,
                PLUGIN_ROOT,
                continuation_id=claim["continuationId"],
                lease_token=claim["leaseToken"],
                outcome={
                    "receiptId": "PON-EXTERNAL-SUPERSESSION-RECEIPT",
                    "disposition": "SUPERSEDED",
                },
            )
            self.assertEqual(retired["state"], "RETIRED")
            con = sqlite3.connect(database)
            try:
                self.assertEqual(
                    con.execute(
                        "SELECT state FROM continuation_items WHERE continuation_id=?",
                        ("PON-CONTINUATION-OBSOLETE",),
                    ).fetchone()[0],
                    "RETIRED",
                )
            finally:
                con.close()


if __name__ == "__main__":
    unittest.main()
