# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from post_office.database import initialize_database  # noqa: E402
from post_office.kernel import (  # noqa: E402
    bind_kernel_credential,
    bootstrap_kernel,
    create_kernel_credential,
)


SPEC = importlib.util.spec_from_file_location("vnext_auto_review", PLUGIN_ROOT / "scripts" / "auto_review.py")
assert SPEC and SPEC.loader
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)


class AutoReviewAdapterTests(unittest.TestCase):
    def test_task_facing_ensure_status_and_withdraw(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "post-office-next.sqlite3"
            credential = root / "author.json"
            create_kernel_credential(credential, "PON-CAPABILITY-AUTHOR")
            initialize_database(PLUGIN_ROOT, database)
            bootstrap_kernel(
                database, credential, actor_id="PON-ACTOR-AUTHOR", actor_kind="HUMAN",
                actor_role="author", mode="SHADOW", plugin_root=PLUGIN_ROOT,
            )
            timestamp = "2026-09-04T12:00:00Z"
            con = sqlite3.connect(database)
            try:
                con.execute("UPDATE kernel_instances SET authority_state='AUTHORITATIVE'")
                con.execute("INSERT INTO projects VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", ("PON-PROJECT-TEST", "TEST", "Test", "PROJECT", "ACTIVE", "project", "objects", "backups", 1, "a" * 64, None, timestamp, None))
                con.execute("INSERT INTO project_mail_domains VALUES(?,?)", ("PON-PROJECT-TEST", "TEST"))
                con.execute("INSERT INTO actors VALUES(?,?,?,?,?)", ("PON-ACTOR-TASK", "ENDPOINT", "TASK", "ACTIVE", timestamp))
                con.execute(
                    """INSERT INTO exact_author_actions(
                       action_id,actor_id,operation,scope_json,confirmation_sha256,recorded_at)
                       VALUES(?,?,?,?,?,?)""",
                    ("PON-ACTION-TASK-GRANT", "PON-ACTOR-AUTHOR", "authority.grant", "{}", "a" * 64, timestamp),
                )
                con.execute("INSERT INTO authority_grants(grant_id,grantor_actor_id,recipient_actor_id,allowed_operations_json,scope_json,classification,maximum_uses,remaining_uses,status,rationale,source_author_action_id,created_at) VALUES(?,?,?,?,?,'STANDING',NULL,NULL,'ACTIVE',?,?,?)", ("PON-GRANT-TASK", "PON-ACTOR-AUTHOR", "PON-ACTOR-TASK", '["task.review"]', '{"taskIds":["PON-TASK-TEST"]}', "Task review access", "PON-ACTION-TASK-GRANT", timestamp))
                con.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("PON-TASK-TEST", "PON-PROJECT-TEST", "PROJECT_MANAGEMENT", "Test", None, "b" * 64, "PON-GRANT-TASK", "ACTIVE", 1, "c" * 64, None, timestamp))
                con.execute("INSERT INTO endpoints VALUES(?,?,?,?,?,?,?,?,?,?)", ("PON-ENDPOINT-TASK", "PON-ACTOR-TASK", "PON-PROJECT-TEST", "PON-TASK-TEST", "TASK", '{"threadId":"task-thread","hostId":"local"}', "ACTIVE", "d" * 64, None, timestamp))
                con.execute("INSERT INTO mailboxes VALUES(?,?,?,?,?,?,?)", ("TEST-MBX-0001", 1, "TEST", "PON-ENDPOINT-TASK", "ACTIVE", None, timestamp))
                con.execute("INSERT INTO actors VALUES(?,?,?,?,?)", ("PON-ACTOR-REVIEWER", "BROWSER", "REVIEWER", "ACTIVE", timestamp))
                con.execute("INSERT INTO endpoints VALUES(?,?,?,?,?,?,?,?,?,?)", ("PON-ENDPOINT-REVIEWER", "PON-ACTOR-REVIEWER", "PON-PROJECT-TEST", None, "REVIEWER", '{}', "ACTIVE", "e" * 64, None, timestamp))
                con.execute("INSERT INTO mailboxes VALUES(?,?,?,?,?,?,?)", ("REVIEW-MBX-0001", 1, "TEST", "PON-ENDPOINT-REVIEWER", "ACTIVE", None, timestamp))
                con.execute("INSERT INTO reviewer_instances VALUES(?,?,?,?,?,?,?,?,?,?,?)", ("reviewer-thread", "PON-ENDPOINT-REVIEWER", "Reviewer", "ACTIVE", 1, None, 30, None, timestamp, timestamp, None))
                con.commit()
            finally:
                con.close()
            secrets_root = root / "caller-secrets"
            secrets_root.mkdir()
            bind_kernel_credential(
                database,
                credential,
                secrets_root / "task-thread.token",
                action_id="PON-ACTION-TASK-CREDENTIAL",
                actor_id="PON-ACTOR-TASK",
                capability_id="PON-CAPABILITY-TASK",
                subject_kind="TASK",
                subject_id="PON-TASK-TEST",
                subject_generation=None,
                allowed_operations=["task.review"],
                expires_at=None,
                plugin_root=PLUGIN_ROOT,
            )
            package = root / "review.zip"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr("REVIEW_CONTRACT.md", "REVIEW ID: REVIEW-001\nPACKAGE NAME: review.zip\n")

            def call(arguments: list[str]) -> tuple[int, dict[str, object]]:
                output = io.StringIO()
                with patch.dict(os.environ, {"CODEX_THREAD_ID": "task-thread", "CODEX_HOST_ID": "local"}), contextlib.redirect_stdout(output):
                    code = adapter.main(["--hub-root", str(root), *arguments])
                return code, json.loads(output.getvalue())

            code, ensured = call(["ensure", "--requester-thread", "task-thread", "--requester-host", "local", "--subject", "Review", "--readiness", "PR_SCALE_NEAR_COMPLETE", "--package", str(package), "--idempotency-key", "review-key"])
            self.assertEqual(code, 0)
            self.assertEqual(ensured["ensure_state"], "ABSENT_SUBMIT_CREATED")
            self.assertEqual(call(["status", "--review-id", "REVIEW-001"])[1]["status"], "QUEUED_LOCAL")
            con = sqlite3.connect(database)
            try:
                self.assertEqual(
                    con.execute("SELECT state FROM transport_attempts").fetchone()[0],
                    "STORED",
                )
            finally:
                con.close()
            withdrawn = call(["withdraw", "--review-id", "REVIEW-001", "--reason", "Changed", "--idempotency-key", "withdraw-key"])[1]
            self.assertEqual(withdrawn["status"], "WITHDRAWN")
            self.assertTrue(withdrawn["withdrawal_custody_preserved"])
            con = sqlite3.connect(database)
            try:
                withdrawal_event = con.execute(
                    "SELECT operation,aggregate_version FROM hub_events "
                    "WHERE aggregate_type='AutomaticReview' AND aggregate_id='REVIEW-001' "
                    "ORDER BY aggregate_version DESC LIMIT 1"
                ).fetchone()
                self.assertEqual(withdrawal_event, ("automatic-review.withdraw", 2))
                con.execute(
                    "UPDATE caller_capabilities SET status='REVOKED' WHERE capability_id='PON-CAPABILITY-TASK'"
                )
                con.commit()
            finally:
                con.close()
            errors = io.StringIO()
            with patch.dict(os.environ, {"CODEX_THREAD_ID": "task-thread", "CODEX_HOST_ID": "local"}), contextlib.redirect_stderr(errors):
                code = adapter.main(["--hub-root", str(root), "status", "--review-id", "REVIEW-001"])
            self.assertEqual(code, 2)
            self.assertIn("inactive, expired, mismatched, or disallows review", errors.getvalue())
            con = sqlite3.connect(database)
            try:
                self.assertEqual(
                    con.execute("SELECT state FROM automatic_reviews WHERE review_id='REVIEW-001'").fetchone()[0],
                    "WITHDRAWN",
                )
            finally:
                con.close()


if __name__ == "__main__":
    unittest.main()
