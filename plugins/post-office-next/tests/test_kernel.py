# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from post_office.database import initialize_database  # noqa: E402
from post_office.diagnostics import PostOfficeError  # noqa: E402
from post_office.kernel import (  # noqa: E402
    append_hub_event,
    assert_aggregate_concurrency,
    bootstrap_kernel,
    create_kernel_credential,
    execute_operation,
    inspect_kernel,
)


class OperationalKernelTests(unittest.TestCase):
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
            self.assertEqual(inspected["implementedOperations"], ["hub.status"])
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
            self.assertEqual(first["status"]["databaseUserVersion"], 3)
            self.assertEqual(first["status"]["implementedOperations"], ["hub.status"])
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

    def test_capability_is_default_deny_for_unimplemented_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database, credential = self._bootstrapped(root)
            request = {
                "schemaVersion": "1",
                "operation": "transport.inspect",
                "requestId": "PON-REQUEST-TRANSPORT",
                "actor": {
                    "id": "PON-ACTOR-OPERATOR",
                    "kind": "HUMAN",
                    "role": "authenticated-reader",
                },
                "authority": {"capabilityId": "PON-CAPABILITY-OPERATOR"},
                "parameters": {"semanticMessageId": "PON-MESSAGE-001"},
            }
            request_path = root / "transport.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            with self.assertRaises(PostOfficeError) as raised:
                execute_operation(database, request_path, credential, PLUGIN_ROOT)
            self.assertEqual(raised.exception.code, "PON_AUTHORIZATION_DENIED")

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


if __name__ == "__main__":
    unittest.main()
