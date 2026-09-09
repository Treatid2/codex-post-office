#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Contract tests for the project-independent automatic-review client."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "scripts" / "review_client.py"
SPEC = importlib.util.spec_from_file_location("review_client_under_test", CLIENT)
assert SPEC and SPEC.loader
client = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(client)


class ReviewClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(os.environ, {
            "CODEX_THREAD_ID": "task-bound-id",
            "CODEX_HOST_ID": "test-host",
        }, clear=False)
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()

    def _main(self, arguments: list[str], backend: Path) -> tuple[int, dict[str, object], str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with patch.object(client, "backend_path", return_value=backend):
                code = client.main(arguments)
        channel = stdout.getvalue() if code == 0 else stderr.getvalue()
        return code, json.loads(channel), stdout.getvalue() + stderr.getvalue()

    def _deployment(self, backend: Path, state_root: Path | None = None) -> client.Deployment:
        return client.Deployment(
            python=Path(sys.executable),
            python_files={Path(sys.executable).name: client.sha256_file(Path(sys.executable))},
            backend=backend,
            backend_files={backend.name: "0" * 64, "codex_comms.py": "1" * 64},
            state_root=state_root or backend.parent,
            lock_path=backend.parent / "runtime-lock.json",
            lock_sha256="2" * 64,
        )

    def _package(self, root: Path, review_id: str = "REVIEW-001") -> Path:
        package = root / "package.zip"
        contract = f"REVIEW ID: {review_id}\nPACKAGE NAME: {package.name}\n"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("CSX-REVIEW-PACKAGE/REVIEW_CONTRACT.md", contract)
        return package

    def test_package_contract_may_be_at_archive_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "package.zip"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr(
                    "REVIEW_CONTRACT.md",
                    "REVIEW ID: REVIEW-ROOT\nPACKAGE NAME: package.zip\n",
                )
            self.assertEqual(client.package_review_id(package), "REVIEW-ROOT")

    def test_package_contract_may_be_nested_at_any_depth(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "package.zip"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr(
                    "one/two/REVIEW_CONTRACT.md",
                    "REVIEW ID: REVIEW-NESTED\nPACKAGE NAME: package.zip\n",
                )
            self.assertEqual(client.package_review_id(package), "REVIEW-NESTED")

    def test_duplicate_package_contracts_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "package.zip"
            contract = "REVIEW ID: REVIEW-DUPLICATE\nPACKAGE NAME: package.zip\n"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr("REVIEW_CONTRACT.md", contract)
                archive.writestr("nested/REVIEW_CONTRACT.md", contract)
            with self.assertRaises(client.ClientError) as raised:
                client.package_review_id(package)
            self.assertEqual(raised.exception.code, "REVIEW_PACKAGE_INVALID")

    def test_similarly_suffixed_member_is_not_a_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "package.zip"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr(
                    "NOT_REVIEW_CONTRACT.md",
                    "REVIEW ID: REVIEW-LOOKALIKE\nPACKAGE NAME: package.zip\n",
                )
            with self.assertRaises(client.ClientError) as raised:
                client.package_review_id(package)
            self.assertEqual(raised.exception.code, "REVIEW_PACKAGE_INVALID")

    def test_submit_derives_identity_and_validates_response(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backend = root / "backend.ps1"
            backend.write_text("# test backend\n", encoding="utf-8")
            package = self._package(root)
            digest = client.sha256_file(package)
            observed: list[str] = []

            def invoke(_: Path, arguments: list[str], identity: tuple[str, str]) -> dict[str, object]:
                observed.extend(arguments)
                self.assertEqual(identity, ("task-bound-id", "test-host"))
                return {
                    "review_id": "REVIEW-001",
                    "requester_thread_id": "task-bound-id",
                    "requester_host_id": "test-host",
                    "status": "PENDING_DRIVE_DELIVERY",
                    "package_name": package.name,
                    "package_size_bytes": package.stat().st_size,
                    "package_sha256": digest,
                    "readiness": "PR_SCALE_NEAR_COMPLETE",
                    "idempotency_key": "key",
                }

            with patch.object(client, "invoke_backend", side_effect=invoke):
                code, payload, _ = self._main([
                    "submit", "--package", str(package), "--subject", "subject",
                    "--readiness", "PR_SCALE_NEAR_COMPLETE", "--idempotency-key", "key",
                ], backend)
            self.assertEqual(code, 0)
            self.assertEqual(payload["coupling"], "POST_OFFICE_COMPANION")
            self.assertEqual(observed[observed.index("--requester-thread") + 1], "task-bound-id")
            self.assertEqual(observed[observed.index("--requester-host") + 1], "test-host")
            self.assertNotIn("--reviewer-thread", observed)

    def test_submit_rejects_review_id_not_bound_to_package_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backend = root / "backend.py"
            backend.write_text("# test backend\n", encoding="utf-8")
            package = self._package(root, "REVIEW-EXPECTED")
            digest = client.sha256_file(package)
            response = {
                "review_id": "REVIEW-WRONG",
                "requester_thread_id": "task-bound-id",
                "requester_host_id": "test-host",
                "status": "PENDING_DRIVE_DELIVERY",
                "package_name": package.name,
                "package_size_bytes": package.stat().st_size,
                "package_sha256": digest,
                "readiness": "PR_SCALE_NEAR_COMPLETE",
                "idempotency_key": "key",
            }
            with patch.object(client, "invoke_backend", return_value=response):
                code, payload, _ = self._main([
                    "submit", "--package", str(package), "--subject", "subject",
                    "--readiness", "PR_SCALE_NEAR_COMPLETE", "--idempotency-key", "key",
                ], backend)
            self.assertEqual(code, 2)
            self.assertEqual(payload["diagnostic"]["code"], "REVIEW_BACKEND_PROTOCOL_ERROR")

    def test_ensure_creates_absent_review_and_validates_package_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backend = root / "backend.py"
            backend.write_text("# test backend\n", encoding="utf-8")
            package = self._package(root)
            digest = client.sha256_file(package)
            observed: list[str] = []

            def invoke(_: Path, arguments: list[str], identity: tuple[str, str]) -> dict[str, object]:
                observed.extend(arguments)
                self.assertEqual(identity, ("task-bound-id", "test-host"))
                return {
                    "review_id": "REVIEW-001",
                    "requester_thread_id": "task-bound-id",
                    "requester_host_id": "test-host",
                    "status": "PENDING_DRIVE_DELIVERY",
                    "package_name": package.name,
                    "package_size_bytes": package.stat().st_size,
                    "package_sha256": digest,
                    "readiness": "PR_SCALE_NEAR_COMPLETE",
                    "idempotency_key": "key",
                    "ensure_state": "ABSENT_SUBMIT_CREATED",
                    "ensure_match": "ABSENT",
                    "created": True,
                }

            with patch.object(client, "invoke_backend", side_effect=invoke):
                code, payload, _ = self._main([
                    "ensure", "--package", str(package), "--subject", "subject",
                    "--readiness", "PR_SCALE_NEAR_COMPLETE", "--idempotency-key", "key",
                ], backend)
            self.assertEqual(code, 0)
            self.assertEqual(payload["result"]["ensure_state"], "ABSENT_SUBMIT_CREATED")
            self.assertEqual(observed[0], "ensure")
            self.assertEqual(observed[observed.index("--requester-thread") + 1], "task-bound-id")

    def test_ensure_conflict_can_report_requesters_different_open_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backend = root / "backend.py"
            backend.write_text("# test backend\n", encoding="utf-8")
            package = self._package(root, "REVIEW-REQUESTED")
            response = {
                "review_id": "REVIEW-ALREADY-OPEN",
                "requester_thread_id": "task-bound-id",
                "requester_host_id": "test-host",
                "status": "REVIEW_ACTIVE",
                "ensure_state": "CONFLICT",
                "ensure_match": "REQUESTER_OPEN_REVIEW",
                "created": False,
                "conflict_reason": "OPEN_REVIEW_DIFFERENT_PACKAGE",
            }
            with patch.object(client, "invoke_backend", return_value=response):
                code, payload, _ = self._main([
                    "ensure", "--package", str(package), "--subject", "subject",
                    "--readiness", "PR_SCALE_NEAR_COMPLETE", "--idempotency-key", "key",
                ], backend)
            self.assertEqual(code, 0)
            self.assertEqual(payload["result"]["review_id"], "REVIEW-ALREADY-OPEN")
            self.assertEqual(payload["result"]["ensure_state"], "CONFLICT")

    def test_withdraw_is_task_bound_idempotent_and_custody_preserving(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            backend = Path(temporary) / "backend.py"
            backend.write_text("# test backend\n", encoding="utf-8")
            observed: list[str] = []

            def invoke(_: Path, arguments: list[str], identity: tuple[str, str]) -> dict[str, object]:
                observed.extend(arguments)
                self.assertEqual(identity, ("task-bound-id", "test-host"))
                return {
                    "review_id": "REVIEW-001", "requester_thread_id": "task-bound-id",
                    "requester_host_id": "test-host", "status": "WITHDRAWN",
                    "withdrawal_reason": "Source changed.",
                    "withdrawal_idempotency_key": "withdraw-key",
                    "withdrawn_at": "2026-09-09T00:00:00+00:00",
                    "withdrawal_replayed": False,
                    "withdrawal_custody_preserved": True,
                }

            with patch.object(client, "invoke_backend", side_effect=invoke):
                code, payload, _ = self._main([
                    "withdraw", "--review-id", "REVIEW-001",
                    "--reason", "Source changed.",
                    "--idempotency-key", "withdraw-key",
                ], backend)
            self.assertEqual(code, 0)
            self.assertEqual(payload["result"]["status"], "WITHDRAWN")
            self.assertEqual(observed[0], "withdraw")

    def test_inspect_rejects_review_id_not_bound_to_package_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backend = root / "backend.py"
            backend.write_text("# test backend\n", encoding="utf-8")
            package = self._package(root, "REVIEW-EXPECTED")
            response = {
                "review_id": "REVIEW-WRONG",
                "package_name": package.name,
                "package_size_bytes": package.stat().st_size,
                "package_sha256": client.sha256_file(package),
            }
            with patch.object(client, "invoke_backend", return_value=response):
                code, payload, _ = self._main(["inspect", "--package", str(package)], backend)
            self.assertEqual(code, 2)
            self.assertEqual(payload["diagnostic"]["code"], "REVIEW_BACKEND_PROTOCOL_ERROR")

    def test_source_replacement_cannot_split_contract_metadata_from_service_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backend = root / "backend.py"
            backend.write_text("# test backend\n", encoding="utf-8")
            package = self._package(root, "REVIEW-AAA")
            replacement = root / "replacement.zip"
            with zipfile.ZipFile(replacement, "w") as archive:
                archive.writestr(
                    "CSX-REVIEW-PACKAGE/REVIEW_CONTRACT.md",
                    "REVIEW ID: REVIEW-BBB\nPACKAGE NAME: package.zip\n",
                )
            self.assertEqual(package.stat().st_size, replacement.stat().st_size)

            def invoke(_: Path, arguments: list[str], __: tuple[str, str]) -> dict[str, object]:
                selected = Path(arguments[arguments.index("--package") + 1])
                self.assertNotEqual(selected, package)
                self.assertEqual(client.package_review_id(selected), "REVIEW-AAA")
                os.replace(replacement, package)
                return {
                    "review_id": "REVIEW-AAA",
                    "package": str(selected),
                    "package_name": selected.name,
                    "package_size_bytes": selected.stat().st_size,
                    "package_sha256": client.sha256_file(selected),
                }

            with patch.object(client, "invoke_backend", side_effect=invoke):
                code, payload, _ = self._main(["inspect", "--package", str(package)], backend)
            self.assertEqual(code, 0)
            self.assertEqual(payload["result"]["review_id"], "REVIEW-AAA")
            self.assertEqual(client.package_review_id(package), "REVIEW-BBB")

    def test_backend_cannot_be_selected_from_cli_or_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            backend = Path(temporary) / "untrusted.ps1"
            backend.write_text("Write-Output '{}'\n", encoding="utf-8")
            os.environ["CODEX_REVIEW_BACKEND"] = str(backend)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = client.main(["--backend", str(backend), "access"])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(stderr.getvalue())["diagnostic"]["code"], "REVIEW_ARGUMENT_INVALID")

    def test_invalid_arguments_are_one_json_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            backend = Path(temporary) / "backend.ps1"
            backend.write_text("# test backend\n", encoding="utf-8")
            code, payload, combined = self._main(["submit"], backend)
            self.assertEqual(code, 2)
            self.assertEqual(payload["diagnostic"]["code"], "REVIEW_ARGUMENT_INVALID")
            self.assertNotIn("usage:", combined)
            self.assertNotIn("Traceback", combined)

    def test_valid_nonobject_json_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            backend = Path(temporary) / "backend.ps1"
            backend.write_text("# test backend\n", encoding="utf-8")
            completed = subprocess.CompletedProcess([], 0, stdout="[]\n", stderr="")
            with patch.object(client, "load_deployment", return_value=self._deployment(backend)), patch.object(client, "_backend_attestation", return_value={"backend": "fixed"}), patch.object(client, "_backend_bundle", return_value="{}"), patch.object(client.subprocess, "run", return_value=completed):
                with self.assertRaises(client.ClientError) as raised:
                    client.invoke_backend(backend, ["status"], ("task-bound-id", "test-host"))
            self.assertEqual(raised.exception.code, "REVIEW_BACKEND_PROTOCOL_ERROR")

    def test_timeout_and_launch_failure_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            backend = Path(temporary) / "backend.ps1"
            backend.write_text("# test backend\n", encoding="utf-8")
            for failure, expected in (
                (subprocess.TimeoutExpired("test", 1), "REVIEW_BACKEND_TIMEOUT"),
                (OSError("denied"), "REVIEW_BACKEND_UNAVAILABLE"),
            ):
                with self.subTest(expected=expected), patch.object(client, "load_deployment", return_value=self._deployment(backend)), patch.object(client, "_backend_attestation", return_value={"backend": "fixed"}), patch.object(client, "_backend_bundle", return_value="{}"), patch.object(client.subprocess, "run", side_effect=failure):
                    with self.assertRaises(client.ClientError) as raised:
                        client.invoke_backend(backend, ["submit", "--idempotency-key", "stable-key"], ("task-bound-id", "test-host"))
                    self.assertEqual(raised.exception.code, expected)
                    if expected == "REVIEW_BACKEND_TIMEOUT":
                        self.assertEqual(raised.exception.details["outcome"], "UNKNOWN")
                        self.assertEqual(raised.exception.details["preserveIdempotencyKey"], "stable-key")

    def test_backend_command_is_fixed_isolated_and_environment_is_sanitized(self) -> None:
        backend = Path(tempfile.gettempdir()) / "automatic-review-test" / "auto_review.py"
        completed = subprocess.CompletedProcess([], 0, stdout='{"ok": true}\n', stderr="")
        observed: dict[str, object] = {}

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            observed["command"] = command
            observed["environment"] = kwargs["env"]
            observed["input"] = kwargs["input"]
            return completed

        with patch.dict(os.environ, {
            "PATH": r"C:\attacker-first",
            "CODEX_COMMS_HUB_ROOT": r"C:\attacker-state",
            "CODEX_COMMS_CAPABILITY": "attacker-token",
            "PYTHONPATH": r"C:\attacker-python",
        }, clear=False), patch.object(client, "load_deployment", return_value=self._deployment(backend)), patch.object(client, "_backend_attestation", return_value={"fixed": "hash"}), patch.object(client, "_backend_bundle", return_value='{"fixed":true}'), patch.object(client.subprocess, "run", side_effect=run):
            result = client.invoke_backend(backend, ["status", "--review-id", "REVIEW-001"], ("task-bound-id", "test-host"))
        self.assertTrue(result["ok"])
        command = observed["command"]
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[1:3], ["-I", "-c"])
        self.assertIn(str(backend.parent), command)
        self.assertEqual(observed["input"], '{"fixed":true}')
        environment = observed["environment"]
        self.assertNotIn("PATH", environment)
        self.assertNotIn("CODEX_COMMS_CAPABILITY", environment)
        self.assertNotIn("PYTHONPATH", environment)
        self.assertEqual(environment["CODEX_THREAD_ID"], "task-bound-id")

    def test_session_only_identity_is_explicitly_unsupported(self) -> None:
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "", "CODEX_SESSION_ID": "session-only"}):
            with self.assertRaises(client.ClientError) as raised:
                client.runtime_identity()
        self.assertEqual(raised.exception.code, "REVIEW_RUNTIME_IDENTITY_MISSING")

    def test_wrapper_bootstrap_failure_is_versioned_json(self) -> None:
        powershell = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
        if not powershell.is_file():
            self.skipTest("Windows PowerShell is unavailable")
        wrapper = ROOT / "scripts" / "Invoke-AutomaticCodeReview.ps1"
        with tempfile.TemporaryDirectory() as temporary:
            isolated = Path(temporary) / wrapper.name
            isolated.write_bytes(wrapper.read_bytes())
            completed = subprocess.run(
                [str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(isolated), "access"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
            )
        self.assertNotEqual(completed.returncode, 0)
        payload = json.loads(completed.stderr.strip())
        self.assertEqual(payload["schemaVersion"], "1.0")
        self.assertIn(payload["diagnostic"]["code"], {"REVIEW_SERVICE_NOT_CONFIGURED", "REVIEW_BOOTSTRAP_FAILURE"})

    def test_wrapper_attests_the_current_client_bytes(self) -> None:
        wrapper = (ROOT / "scripts" / "Invoke-AutomaticCodeReview.ps1").read_text(encoding="utf-8")
        match = re.search(r"\$ExpectedClientSha256 = '([0-9a-f]{64})'", wrapper)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), client.sha256_file(CLIENT))

    def test_wrong_requester_response_is_rejected(self) -> None:
        with self.assertRaises(client.ClientError) as raised:
            client.validate_backend_result("status", {
                "review_id": "REVIEW-001",
                "requester_thread_id": "different-task",
                "requester_host_id": "test-host",
                "status": "REVIEW_ACTIVE",
            }, ("task-bound-id", "test-host"), review_id="REVIEW-001")
        self.assertEqual(raised.exception.code, "REVIEW_BACKEND_PROTOCOL_ERROR")

    def test_missing_runtime_identity_is_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            backend = Path(temporary) / "backend.ps1"
            backend.write_text("# test backend\n", encoding="utf-8")
            with patch.dict(os.environ, {"CODEX_THREAD_ID": "", "CODEX_SESSION_ID": ""}):
                code, payload, _ = self._main(["access"], backend)
            self.assertEqual(code, 2)
            self.assertEqual(payload["diagnostic"]["code"], "REVIEW_RUNTIME_IDENTITY_MISSING")


if __name__ == "__main__":
    unittest.main()
