#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Project-independent client for the registered Post Office automatic-review service."""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


SCHEMA_VERSION = "1.0"
DEPLOYMENT_SCHEMA_VERSION = "1.0"
DEPLOYMENT_RELATIVE_PATH = Path("Treatid2") / "CodexPostOffice" / "automatic-code-review" / "runtime-lock.json"
REVIEW_OPERATIONS = ("review-submit", "review-status", "review-complete", "review-withdraw")
ENSURE_STATES = {
    "ACTIVE_DO_NOT_RESUBMIT", "RETURNED_COMPLETE_REQUIRED", "TERMINAL",
    "ABSENT_SUBMIT_CREATED", "CONFLICT",
}
BACKEND_TIMEOUT_SECONDS = 120
IDENTIFIER = re.compile(r"^[A-Za-z0-9._-]+$")
REVIEW_ID_LINE = re.compile(r"^REVIEW ID:\s*([A-Za-z0-9._-]+)\s*$", re.MULTILINE)
PACKAGE_NAME_LINE = re.compile(r"^PACKAGE NAME:\s*(\S+)\s*$", re.MULTILINE)
# Keep this command-line program on one physical line.  The deployment lock may
# intentionally point at the stable Windows ``python.cmd`` entry point; cmd.exe
# treats embedded newlines in a ``python -c`` argument as command separators and
# can otherwise turn a backend invocation into a silent, successful no-op.
RUNNER = (
    'import base64,json,sys,types;'
    'bundle=json.loads(sys.stdin.buffer.read());'
    'module=types.ModuleType("codex_comms");'
    'module.__file__=bundle["codex_comms_path"];'
    'exec(compile(base64.b64decode(bundle["codex_comms"]),module.__file__,"exec"),module.__dict__);'
    'sys.modules["codex_comms"]=module;'
    'sys.argv=[bundle["auto_review_path"],*sys.argv[1:]];'
    'scope={"__name__":"__main__","__file__":bundle["auto_review_path"]};'
    'exec(compile(base64.b64decode(bundle["auto_review"]),bundle["auto_review_path"],"exec"),scope,scope)'
)


class Deployment:
    def __init__(
        self,
        *,
        python: Path,
        python_files: dict[str, str],
        backend: Path,
        backend_files: dict[str, str],
        state_root: Path,
        state_database: str,
        lock_path: Path,
        lock_sha256: str,
    ) -> None:
        self.python = python
        self.python_files = python_files
        self.backend = backend
        self.backend_files = backend_files
        self.state_root = state_root
        self.state_database = state_database
        self.lock_path = lock_path
        self.lock_sha256 = lock_sha256


class ClientError(RuntimeError):
    def __init__(self, code: str, message: str, *, exit_code: int = 2, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
        self.details = details or {}


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ClientError("REVIEW_ARGUMENT_INVALID", message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def deployment_lock_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise ClientError(
            "REVIEW_SERVICE_NOT_CONFIGURED",
            "LOCALAPPDATA is unavailable; the automatic-review deployment lock cannot be located.",
        )
    return Path(local_app_data) / DEPLOYMENT_RELATIVE_PATH


def _absolute_path(value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ClientError("REVIEW_SERVICE_CONFIGURATION_INVALID", f"Deployment field {field} is invalid.")
    path = Path(value)
    if not path.is_absolute():
        raise ClientError("REVIEW_SERVICE_CONFIGURATION_INVALID", f"Deployment field {field} must be absolute.")
    return Path(os.path.abspath(str(path)))


def _digest_map(value: Any, field: str) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise ClientError("REVIEW_SERVICE_CONFIGURATION_INVALID", f"Deployment field {field} is invalid.")
    result: dict[str, str] = {}
    for name, digest in value.items():
        if (not isinstance(name, str) or not name or Path(name).name != name
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ClientError("REVIEW_SERVICE_CONFIGURATION_INVALID", f"Deployment field {field} is invalid.")
        result[name] = digest
    return result


def load_deployment() -> Deployment:
    lock_path = deployment_lock_path()
    try:
        lock_bytes, lock_sha256 = _stable_file_bytes(
            lock_path,
            maximum_bytes=256 * 1024,
            unavailable_code="REVIEW_SERVICE_NOT_CONFIGURED",
            changed_code="REVIEW_SERVICE_CONFIGURATION_INVALID",
            label="Automatic-review deployment lock",
        )
        value = json.loads(lock_bytes.decode("utf-8"))
    except ClientError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClientError(
            "REVIEW_SERVICE_CONFIGURATION_INVALID",
            "The automatic-review deployment lock is not valid UTF-8 JSON.",
        ) from exc
    if not isinstance(value, dict) or value.get("schemaVersion") != DEPLOYMENT_SCHEMA_VERSION:
        raise ClientError(
            "REVIEW_SERVICE_CONFIGURATION_INVALID",
            "The automatic-review deployment lock has an unsupported schema.",
        )
    python = _absolute_path(value.get("pythonExecutable"), "pythonExecutable")
    backend = _absolute_path(value.get("backendEntrypoint"), "backendEntrypoint")
    state_root = _absolute_path(value.get("stateRoot"), "stateRoot")
    state_database = value.get("stateDatabase", "hub.sqlite3")
    if (
        not isinstance(state_database, str)
        or Path(state_database).name != state_database
        or state_database not in {"post-office-next.sqlite3", "hub.sqlite3"}
    ):
        raise ClientError(
            "REVIEW_SERVICE_CONFIGURATION_INVALID",
            "Deployment field stateDatabase is invalid.",
        )
    python_files = _digest_map(value.get("pythonFiles"), "pythonFiles")
    backend_files = _digest_map(value.get("backendFiles"), "backendFiles")
    if python.name not in python_files or backend.name not in backend_files or "codex_comms.py" not in backend_files:
        raise ClientError(
            "REVIEW_SERVICE_CONFIGURATION_INVALID",
            "The deployment lock is missing required runtime or backend attestations.",
        )
    return Deployment(
        python=python,
        python_files=python_files,
        backend=backend,
        backend_files=backend_files,
        state_root=state_root,
        state_database=state_database,
        lock_path=lock_path,
        lock_sha256=lock_sha256,
    )


def _stable_file_bytes(
    path: Path,
    *,
    maximum_bytes: int | None = None,
    unavailable_code: str = "REVIEW_BACKEND_UNAVAILABLE",
    changed_code: str = "REVIEW_BACKEND_ATTESTATION_FAILED",
    label: str = "Registered automatic-review execution file",
) -> tuple[bytes, str]:
    if _path_chain_has_link(path) or not _unlinked_regular_file(path):
        raise ClientError(unavailable_code, f"{label} is unavailable or uses linked custody.")
    before = path.stat(follow_symlinks=False)
    if maximum_bytes is not None and before.st_size > maximum_bytes:
        raise ClientError("REVIEW_PACKAGE_INVALID", "Review package exceeds the client size limit.")
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_nlink) != (
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, 1,
        ):
            raise ClientError(changed_code, f"{label} identity changed before read.")
        data = handle.read()
        finished = os.fstat(handle.fileno())
    after = path.stat(follow_symlinks=False)
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if identity != (finished.st_dev, finished.st_ino, finished.st_size, finished.st_mtime_ns):
        raise ClientError(changed_code, f"{label} identity changed during read.")
    if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ClientError(changed_code, f"{label} path changed during read.")
    return data, hashlib.sha256(data).hexdigest()


def _stable_package_bytes(path: Path) -> tuple[bytes, str]:
    return _stable_file_bytes(
        path,
        maximum_bytes=1024 * 1024,
        unavailable_code="REVIEW_PACKAGE_INVALID",
        changed_code="REVIEW_PACKAGE_CHANGED",
        label="Review package",
    )


def _stable_attested_bytes(path: Path, expected: str) -> bytes:
    data, digest = _stable_file_bytes(path)
    if digest != expected:
        raise ClientError("REVIEW_BACKEND_ATTESTATION_FAILED", "Registered automatic-review execution file failed attestation.")
    return data


def runtime_identity() -> tuple[str, str]:
    thread_id = os.environ.get("CODEX_THREAD_ID")
    host_id = os.environ.get("CODEX_HOST_ID", "local")
    if not thread_id:
        raise ClientError(
            "REVIEW_RUNTIME_IDENTITY_MISSING",
            "CODEX_THREAD_ID is required by the registered review service; session-only identity is unsupported.",
        )
    if not IDENTIFIER.fullmatch(thread_id) or not IDENTIFIER.fullmatch(host_id):
        raise ClientError("REVIEW_RUNTIME_IDENTITY_INVALID", "Runtime task or host identity is invalid.")
    return thread_id, host_id


def _unlinked_regular_file(path: Path) -> bool:
    try:
        return (
            path.is_file()
            and not path.is_symlink()
            and not (hasattr(path, "is_junction") and path.is_junction())
            and path.stat(follow_symlinks=False).st_nlink == 1
        )
    except OSError:
        return False


def _path_chain_has_link(path: Path) -> bool:
    absolute = Path(os.path.abspath(str(path)))
    for component in (absolute, *absolute.parents):
        try:
            if component.is_symlink() or (hasattr(component, "is_junction") and component.is_junction()):
                return True
        except OSError:
            return True
    return False


def _attest_files(root: Path, expected_files: dict[str, str]) -> dict[str, str]:
    if _path_chain_has_link(root):
        raise ClientError("REVIEW_BACKEND_ATTESTATION_FAILED", "Registered execution path uses a link or junction.")
    observed: dict[str, str] = {}
    for name, expected in expected_files.items():
        candidate = root / name
        _stable_attested_bytes(candidate, expected)
        observed[name] = expected
    return observed


def backend_path() -> Path:
    """Resolve the deployment-locked, hash-attested service script."""
    deployment = load_deployment()
    result = deployment.backend.resolve(strict=False)
    if result != deployment.backend or _path_chain_has_link(result.parent):
        raise ClientError("REVIEW_BACKEND_UNAVAILABLE", "Registered automatic-review backend is unavailable.")
    _attest_files(result.parent, deployment.backend_files)
    _attest_files(deployment.python.parent, deployment.python_files)
    if (_path_chain_has_link(deployment.state_root) or not deployment.state_root.is_dir()
            or not _unlinked_regular_file(deployment.state_root / deployment.state_database)):
        raise ClientError("REVIEW_BACKEND_UNAVAILABLE", "Registered Post Office service state is unavailable.")
    return result


def _backend_attestation(backend: Path) -> dict[str, str]:
    deployment = load_deployment()
    if backend != deployment.backend:
        raise ClientError("REVIEW_BACKEND_ATTESTATION_FAILED", "Backend does not match the deployment lock.")
    result = {f"backend/{name}": digest for name, digest in _attest_files(backend.parent, deployment.backend_files).items()}
    result.update({f"runtime/{name}": digest for name, digest in _attest_files(deployment.python.parent, deployment.python_files).items()})
    result["deployment/runtime-lock.json"] = deployment.lock_sha256
    return result


def _backend_bundle(backend: Path) -> str:
    deployment = load_deployment()
    if backend != deployment.backend:
        raise ClientError("REVIEW_BACKEND_ATTESTATION_FAILED", "Backend does not match the deployment lock.")
    auto_review = _stable_attested_bytes(backend, deployment.backend_files[backend.name])
    codex_comms_path = backend.parent / "codex_comms.py"
    codex_comms = _stable_attested_bytes(codex_comms_path, deployment.backend_files["codex_comms.py"])
    return json.dumps({
        "auto_review_path": str(backend),
        "auto_review": base64.b64encode(auto_review).decode("ascii"),
        "codex_comms_path": str(codex_comms_path),
        "codex_comms": base64.b64encode(codex_comms).decode("ascii"),
    }, separators=(",", ":"))


def _backend_environment(identity: tuple[str, str]) -> dict[str, str]:
    environment = {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "CODEX_THREAD_ID": identity[0],
        "CODEX_HOST_ID": identity[1],
    }
    for name in ("SystemRoot", "WINDIR", "COMSPEC"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def invoke_backend(backend: Path, arguments: list[str], identity: tuple[str, str]) -> dict[str, Any]:
    deployment = load_deployment()
    if backend != deployment.backend:
        raise ClientError("REVIEW_BACKEND_ATTESTATION_FAILED", "Backend does not match the deployment lock.")
    before = _backend_attestation(backend)
    source_bundle = _backend_bundle(backend)
    try:
        completed = subprocess.run(
            [
                str(deployment.python), "-I", "-c", RUNNER,
                "--hub-root", str(deployment.state_root), *arguments,
            ],
            input=source_bundle,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=BACKEND_TIMEOUT_SECONDS,
            cwd=str(backend.parent),
            env=_backend_environment(identity),
        )
    except subprocess.TimeoutExpired as exc:
        idempotency_key = None
        if "--idempotency-key" in arguments:
            index = arguments.index("--idempotency-key")
            if index + 1 < len(arguments):
                idempotency_key = arguments[index + 1]
        raise ClientError(
            "REVIEW_BACKEND_TIMEOUT",
            "Registered automatic-review operation timed out with an unknown durable outcome.",
            details={
                "outcome": "UNKNOWN",
                "automaticRetry": False,
                "preserveIdempotencyKey": idempotency_key,
                "reconciliation": "Query the registered service or repeat only the exact idempotent request.",
            },
        ) from exc
    except OSError as exc:
        raise ClientError("REVIEW_BACKEND_UNAVAILABLE", "Registered automatic-review backend could not be started.") from exc
    if _backend_attestation(backend) != before:
        raise ClientError("REVIEW_BACKEND_ATTESTATION_FAILED", "Registered automatic-review backend changed during invocation.")
    output = completed.stdout.strip() or completed.stderr.strip()
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ClientError(
            "REVIEW_BACKEND_PROTOCOL_ERROR",
            f"Registered backend returned non-JSON output (exit {completed.returncode}).",
        ) from exc
    if not isinstance(parsed, dict):
        raise ClientError("REVIEW_BACKEND_PROTOCOL_ERROR", "Registered backend response is not a JSON object.")
    if completed.returncode != 0 or parsed.get("ok") is False:
        message = str(parsed.get("error") or "")
        if "capability" in message.casefold():
            raise ClientError(
                "REVIEW_ACCESS_REQUIRED",
                "Registered review service rejected this task's review capability.",
                exit_code=4,
            )
        raise ClientError("REVIEW_BACKEND_REJECTED", "Registered review service rejected the operation.")
    if "ok" in parsed and parsed["ok"] is not True:
        raise ClientError("REVIEW_BACKEND_PROTOCOL_ERROR", "Registered backend returned an invalid success marker.")
    return parsed


def package_review_id_bytes(package_bytes: bytes, package_name: str) -> str:
    try:
        import io
        with zipfile.ZipFile(io.BytesIO(package_bytes)) as archive:
            # Match the backend's contract discovery rule exactly: the contract may
            # be at the archive root or below one or more enclosing directories,
            # but similarly suffixed filenames must not count as contracts.
            members = [
                name for name in archive.namelist()
                if PurePosixPath(name).name == "REVIEW_CONTRACT.md"
            ]
            if len(members) != 1:
                raise ClientError("REVIEW_PACKAGE_INVALID", "Package must contain exactly one review contract.")
            info = archive.getinfo(members[0])
            if info.file_size > 256 * 1024:
                raise ClientError("REVIEW_PACKAGE_INVALID", "Review contract exceeds the client size limit.")
            text = archive.read(info).decode("utf-8")
    except (OSError, zipfile.BadZipFile, KeyError, UnicodeDecodeError) as exc:
        raise ClientError("REVIEW_PACKAGE_INVALID", "Package review contract could not be read.") from exc
    review_match = REVIEW_ID_LINE.search(text)
    package_match = PACKAGE_NAME_LINE.search(text)
    if (not review_match or not package_match or package_match.group(1) != package_name
            or not IDENTIFIER.fullmatch(review_match.group(1))):
        raise ClientError("REVIEW_PACKAGE_INVALID", "Package contract identity does not match the submitted archive.")
    return review_match.group(1)


def package_review_id(package: Path) -> str:
    package_bytes, _ = _stable_package_bytes(package)
    return package_review_id_bytes(package_bytes, package.name)


@contextlib.contextmanager
def frozen_package(source: Path):
    """Select one stable package version and keep all downstream reads on its private copy."""
    package_bytes, digest = _stable_package_bytes(source)
    review_id = package_review_id_bytes(package_bytes, source.name)
    with tempfile.TemporaryDirectory(prefix="codex-review-package-") as temporary:
        snapshot = Path(temporary) / source.name
        with snapshot.open("xb") as handle:
            handle.write(package_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        observed, observed_digest = _stable_package_bytes(snapshot)
        if observed != package_bytes or observed_digest != digest:
            raise ClientError("REVIEW_PACKAGE_CHANGED", "Private package snapshot does not match the selected source bytes.")
        try:
            yield snapshot, review_id, digest
            final_bytes, final_digest = _stable_package_bytes(snapshot)
            if final_bytes != package_bytes or final_digest != digest:
                raise ClientError("REVIEW_PACKAGE_CHANGED", "Private package snapshot changed during the service operation.")
        finally:
            try:
                snapshot.chmod(0o600)
            except OSError:
                pass


def _require_fields(result: dict[str, Any], fields: tuple[str, ...]) -> None:
    if any(field not in result for field in fields):
        raise ClientError("REVIEW_BACKEND_PROTOCOL_ERROR", "Registered backend response is missing required fields.")


def validate_backend_result(
    command: str,
    result: dict[str, Any],
    identity: tuple[str, str],
    *,
    package: Path | None = None,
    review_id: str | None = None,
    idempotency_key: str | None = None,
    withdrawal_reason: str | None = None,
) -> None:
    if command == "inspect":
        _require_fields(result, ("review_id", "package_name", "package_size_bytes", "package_sha256"))
        if (package is None or result["package_name"] != package.name
                or result["package_size_bytes"] != package.stat().st_size
                or result["package_sha256"] != sha256_file(package)
                or not isinstance(result["review_id"], str) or not result["review_id"]
                or (review_id is not None and result["review_id"] != review_id)):
            raise ClientError("REVIEW_BACKEND_PROTOCOL_ERROR", "Inspect response does not match the submitted package.")
    elif command in {"submit", "ensure", "status", "complete", "withdraw"}:
        _require_fields(result, ("review_id", "requester_thread_id", "requester_host_id", "status"))
        ensure_conflict = command == "ensure" and result.get("ensure_state") == "CONFLICT"
        if (not isinstance(result["review_id"], str) or not result["review_id"]
                or result["requester_thread_id"] != identity[0]
                or result["requester_host_id"] != identity[1]
                or (review_id is not None and result["review_id"] != review_id and not ensure_conflict)
                or not isinstance(result["status"], str)):
            raise ClientError("REVIEW_BACKEND_PROTOCOL_ERROR", "Review response does not match the runtime requester.")
        if command == "submit":
            _require_fields(result, ("package_name", "package_size_bytes", "package_sha256", "readiness", "idempotency_key"))
            if (package is None or result["package_name"] != package.name
                    or result["package_size_bytes"] != package.stat().st_size
                    or result["package_sha256"] != sha256_file(package)
                    or result["readiness"] != "PR_SCALE_NEAR_COMPLETE"
                    or result["idempotency_key"] != idempotency_key):
                raise ClientError("REVIEW_BACKEND_PROTOCOL_ERROR", "Submit response does not match the submitted package.")
        if command == "ensure":
            _require_fields(result, ("ensure_state", "ensure_match", "created"))
            if (result["ensure_state"] not in ENSURE_STATES
                    or not isinstance(result["ensure_match"], str)
                    or not isinstance(result["created"], bool)
                    or result["created"] != (result["ensure_state"] == "ABSENT_SUBMIT_CREATED")):
                raise ClientError("REVIEW_BACKEND_PROTOCOL_ERROR", "Ensure response has an invalid outcome.")
            if result["ensure_state"] == "CONFLICT":
                _require_fields(result, ("conflict_reason",))
            else:
                _require_fields(result, ("package_name", "package_size_bytes", "package_sha256", "readiness"))
                if (package is None or result["package_name"] != package.name
                        or result["package_size_bytes"] != package.stat().st_size
                        or result["package_sha256"] != sha256_file(package)
                        or result["readiness"] != "PR_SCALE_NEAR_COMPLETE"):
                    raise ClientError("REVIEW_BACKEND_PROTOCOL_ERROR", "Ensure response does not match the supplied package.")
                if (result["ensure_match"] in {"IDEMPOTENCY_KEY", "ABSENT"}
                        and result.get("idempotency_key") != idempotency_key):
                    raise ClientError("REVIEW_BACKEND_PROTOCOL_ERROR", "Ensure response changed the idempotency identity.")
        if command == "complete" and result["status"] != "COMPLETED":
            raise ClientError("REVIEW_BACKEND_PROTOCOL_ERROR", "Complete response is not terminal.")
        if command == "withdraw":
            _require_fields(result, (
                "withdrawal_reason", "withdrawal_idempotency_key", "withdrawn_at",
                "withdrawal_replayed", "withdrawal_custody_preserved",
            ))
            if (result["status"] != "WITHDRAWN"
                    or result["withdrawal_idempotency_key"] != idempotency_key
                    or result["withdrawal_reason"] != withdrawal_reason.strip()
                    or result["withdrawal_custody_preserved"] is not True
                    or not isinstance(result["withdrawal_replayed"], bool)):
                raise ClientError(
                    "REVIEW_BACKEND_PROTOCOL_ERROR",
                    "Withdraw response is not an exact custody-preserving terminal result.",
                )


def envelope(command: str, identity: tuple[str, str], result: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "command": command,
        "requester": {"threadId": identity[0], "hostId": identity[1]},
        "coupling": "POST_OFFICE_COMPANION",
        "trustBoundary": {
            "service": "FIXED_STATE_AND_HASH_ATTESTED_ISOLATED_EXECUTOR",
            "caller": "REVIEW_CAPABILITY_BEARER_WITHIN_LOCAL_ACCOUNT",
            "taskHostFields": "ROUTING_AND_TRANSACTION_BINDING_METADATA",
        },
        "result": result,
    }


def access_result(identity: tuple[str, str]) -> dict[str, Any]:
    deployment = load_deployment()
    credential = deployment.state_root / "caller-secrets" / f"{identity[0]}.token"
    if not _unlinked_regular_file(credential):
        return {
            "state": "PROVISION_REQUIRED",
            "requiredOperations": list(REVIEW_OPERATIONS),
            "postalMailboxRequired": False,
            "callerTrustDomain": "LOCAL_WINDOWS_ACCOUNT",
            "provisioning": {
                "authority": "trusted-courier",
                "credentialSubject": "active-courier-mailbox",
                "secretDelivery": "protected-caller-secret-file",
                "tokenInChat": False,
            },
        }
    return {
        "state": "CREDENTIAL_PRESENT",
        "requiredOperations": list(REVIEW_OPERATIONS),
        "postalMailboxRequired": False,
        "callerTrustDomain": "LOCAL_WINDOWS_ACCOUNT",
        "validation": "deferred-to-first-authorized-operation",
    }


def parser() -> argparse.ArgumentParser:
    result = JsonArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True, parser_class=JsonArgumentParser)
    sub.add_parser("access")
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--package", required=True)
    submit = sub.add_parser("submit")
    submit.add_argument("--package", required=True)
    submit.add_argument("--subject", required=True)
    submit.add_argument("--readiness", required=True, choices=["PR_SCALE_NEAR_COMPLETE"])
    submit.add_argument("--idempotency-key", required=True)
    submit.add_argument("--requester-project")
    submit.add_argument("--requester-root")
    ensure = sub.add_parser("ensure")
    ensure.add_argument("--package", required=True)
    ensure.add_argument("--subject", required=True)
    ensure.add_argument("--readiness", required=True, choices=["PR_SCALE_NEAR_COMPLETE"])
    ensure.add_argument("--idempotency-key", required=True)
    ensure.add_argument("--requester-project")
    ensure.add_argument("--requester-root")
    status = sub.add_parser("status")
    status.add_argument("--review-id", required=True)
    complete = sub.add_parser("complete")
    complete.add_argument("--review-id", required=True)
    complete.add_argument("--summary", required=True)
    withdraw = sub.add_parser("withdraw")
    withdraw.add_argument("--review-id", required=True)
    withdraw.add_argument("--reason", required=True)
    withdraw.add_argument("--idempotency-key", required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        identity = runtime_identity()
        backend = backend_path()
        package: Path | None = None
        source_package: Path | None = None
        expected_review_id: str | None = None
        with contextlib.ExitStack() as stack:
            if args.command in {"inspect", "submit", "ensure"}:
                source_package = Path(os.path.abspath(args.package))
                package, expected_review_id, _ = stack.enter_context(frozen_package(source_package))
            if args.command == "access":
                result = access_result(identity)
            elif args.command == "inspect":
                assert package is not None
                result = invoke_backend(backend, ["inspect", "--package", str(package)], identity)
            elif args.command == "submit":
                assert package is not None
                command = [
                    "submit", "--requester-thread", identity[0], "--requester-host", identity[1],
                    "--subject", args.subject, "--readiness", args.readiness,
                    "--package", str(package), "--idempotency-key", args.idempotency_key,
                ]
                if args.requester_project:
                    command.extend(["--requester-project", args.requester_project])
                if args.requester_root:
                    command.extend(["--requester-root", args.requester_root])
                result = invoke_backend(backend, command, identity)
            elif args.command == "ensure":
                assert package is not None
                command = [
                    "ensure", "--requester-thread", identity[0], "--requester-host", identity[1],
                    "--subject", args.subject, "--readiness", args.readiness,
                    "--package", str(package), "--idempotency-key", args.idempotency_key,
                ]
                if args.requester_project:
                    command.extend(["--requester-project", args.requester_project])
                if args.requester_root:
                    command.extend(["--requester-root", args.requester_root])
                result = invoke_backend(backend, command, identity)
            elif args.command == "status":
                expected_review_id = args.review_id
                result = invoke_backend(backend, ["status", "--review-id", args.review_id], identity)
            elif args.command == "complete":
                expected_review_id = args.review_id
                result = invoke_backend(
                    backend, ["complete", "--review-id", args.review_id, "--summary", args.summary], identity,
                )
            elif args.command == "withdraw":
                expected_review_id = args.review_id
                result = invoke_backend(
                    backend, [
                        "withdraw", "--review-id", args.review_id,
                        "--reason", args.reason,
                        "--idempotency-key", args.idempotency_key,
                    ], identity,
                )
            else:
                raise ClientError("REVIEW_COMMAND_UNSUPPORTED", "Unsupported command")
            if args.command != "access":
                validate_backend_result(
                    args.command, result, identity, package=package, review_id=expected_review_id,
                    idempotency_key=getattr(args, "idempotency_key", None),
                    withdrawal_reason=getattr(args, "reason", None),
                )
        if args.command == "inspect" and source_package is not None and "package" in result:
            result = {**result, "package": str(source_package)}
        print(json.dumps(envelope(args.command, identity, result), indent=2, ensure_ascii=False))
        return 0
    except ClientError as exc:
        print(json.dumps({
            "schemaVersion": SCHEMA_VERSION,
            "ok": False,
            "diagnostic": {"code": exc.code, "message": str(exc), "details": exc.details},
        }, ensure_ascii=False), file=sys.stderr)
        return exc.exit_code
    except (OSError, ValueError):
        print(json.dumps({
            "schemaVersion": SCHEMA_VERSION,
            "ok": False,
            "diagnostic": {
                "code": "REVIEW_CLIENT_FAILURE",
                "message": "Automatic-review client failed before the operation could be verified.",
            },
        }, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
