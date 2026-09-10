# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Iterable

from .diagnostics import PostOfficeError


def _configured_protected_state_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        roots.append(Path(local_app_data) / "Treatid2" / "CodexPostOffice")
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        roots.append(Path(codex_home) / "state" / "codex-comms")
    configured = os.environ.get("POST_OFFICE_PROTECTED_ROOTS", "")
    roots.extend(Path(value) for value in configured.split(os.pathsep) if value)
    return tuple(roots)


PROTECTED_STATE_ROOTS = _configured_protected_state_roots()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def merkle_root(entries: Iterable[dict[str, Any]]) -> str:
    leaves = [sha256_json(entry) for entry in entries]
    if not leaves:
        return sha256_bytes(b"")
    while len(leaves) > 1:
        if len(leaves) % 2:
            leaves.append(leaves[-1])
        leaves = [
            sha256_bytes(bytes.fromhex(leaves[index]) + bytes.fromhex(leaves[index + 1]))
            for index in range(0, len(leaves), 2)
        ]
    return leaves[0]


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path.resolve(strict=False))))


def paths_alias(left: Path, right: Path) -> bool:
    if os.path.lexists(left) and os.path.lexists(right):
        try:
            if os.path.samefile(left, right):
                return True
        except OSError:
            pass
    return _path_key(left) == _path_key(right)


def path_is_within(path: Path, root: Path) -> bool:
    path_key = _path_key(path)
    root_key = _path_key(root)
    try:
        return os.path.commonpath([path_key, root_key]) == root_key
    except ValueError:
        return False


def is_link_like(path: Path) -> bool:
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        return True
    try:
        return path.is_file() and path.stat(follow_symlinks=False).st_nlink > 1
    except (FileNotFoundError, OSError):
        return False


def require_outside_protected_roots(path: Path, additional_roots: Iterable[Path] = ()) -> Path:
    path = path.resolve(strict=False)
    roots = [*PROTECTED_STATE_ROOTS, *additional_roots]
    for root in roots:
        root = root.resolve(strict=False)
        if path_is_within(path, root):
            raise PostOfficeError(
                "PON_PRODUCTION_MUTATION_FORBIDDEN",
                "Output is inside a protected source root",
                {"output": str(path), "protectedRoot": str(root)},
            )
    return path


def require_new_output_file(
    output: Path,
    *,
    inputs: Iterable[Path] = (),
    protected_roots: Iterable[Path] = (),
) -> Path:
    output = output.resolve(strict=False)
    if os.path.lexists(output):
        raise PostOfficeError("PON_OUTPUT_EXISTS", "Output file already exists", {"path": str(output)})
    for source in inputs:
        if paths_alias(output, source):
            raise PostOfficeError(
                "PON_PATH_CONFLICT", "Output aliases an input", {"output": str(output), "input": str(source.resolve(strict=False))}
            )
    return require_outside_protected_roots(output, protected_roots)


def require_disjoint_output_directory(output: Path, protected_roots: Iterable[Path]) -> Path:
    output = output.resolve(strict=False)
    for root in [*PROTECTED_STATE_ROOTS, *protected_roots]:
        root = root.resolve(strict=False)
        if path_is_within(output, root) or path_is_within(root, output):
            raise PostOfficeError(
                "PON_PRODUCTION_MUTATION_FORBIDDEN",
                "Output directory overlaps a protected source root",
                {"output": str(output), "protectedRoot": str(root)},
            )
    if os.path.lexists(output):
        raise PostOfficeError("PON_OUTPUT_EXISTS", "Output directory must be absent for atomic publication", {"path": str(output)})
    return output


def contained_member(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
        raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Capture member path is unsafe", {"path": relative})
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Capture member path is unsafe", {"path": relative})
    root = root.resolve(strict=True)
    candidate = root.joinpath(*pure.parts)
    resolved = candidate.resolve(strict=False)
    if not path_is_within(resolved, root) or paths_alias(resolved, root):
        raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Capture member escapes its root", {"path": relative})
    current = root
    for part in pure.parts:
        current = current / part
        if os.path.lexists(current) and is_link_like(current):
            raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Capture member uses a link or junction", {"path": relative})
    return candidate


def _sqlite_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"blobHex": value.hex()}
    if isinstance(value, float):
        return {"floatHex": value.hex()}
    if value is None or isinstance(value, (int, str)):
        return value
    return {"text": str(value)}


def sqlite_content_identity(
    con: sqlite3.Connection,
    *,
    excluded_tables: Iterable[str] = (),
) -> dict[str, Any]:
    excluded = set(excluded_tables)
    tables = [
        str(row[0])
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        if str(row[0]) not in excluded
    ]
    table_identities: list[dict[str, Any]] = []
    for table in tables:
        quoted_table = table.replace('"', '""')
        columns = [
            str(row[1])
            for row in con.execute(f'PRAGMA table_xinfo("{quoted_table}")')
            if int(row[6]) == 0
        ]
        quoted_columns = ", ".join('"' + column.replace('"', '""') + '"' for column in columns)
        row_hashes = [
            sha256_json({"columns": columns, "values": [_sqlite_value(value) for value in row]})
            for row in con.execute(f'SELECT {quoted_columns} FROM "{quoted_table}"')
        ]
        row_hashes.sort()
        table_identities.append(
            {
                "table": table,
                "columns": columns,
                "rowCount": len(row_hashes),
                "rowsRoot": sha256_json(row_hashes),
            }
        )

    event_boundary: dict[str, Any] | None = None
    if "hub_events" in tables:
        row = con.execute(
            "SELECT sequence,event_id,event_sha256 FROM hub_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        if row:
            event_boundary = {"table": "hub_events", "sequence": int(row[0]), "eventId": str(row[1]), "eventSha256": str(row[2])}
    elif "events" in tables:
        event_columns = {
            str(row[1]) for row in con.execute('PRAGMA table_xinfo("events")') if int(row[6]) == 0
        }
        if {"sequence", "event_id", "event_hash"}.issubset(event_columns):
            row = con.execute("SELECT sequence,event_id,event_hash FROM events ORDER BY sequence DESC LIMIT 1").fetchone()
            if row:
                event_boundary = {"table": "events", "sequence": int(row[0]), "eventId": str(row[1]), "eventSha256": str(row[2])}
        elif {"id", "event_hash"}.issubset(event_columns):
            row = con.execute("SELECT id,event_hash FROM events ORDER BY id DESC LIMIT 1").fetchone()
            if row:
                event_boundary = {"table": "events", "sequence": int(row[0]), "eventId": None, "eventSha256": str(row[1])}

    return {
        "tables": table_identities,
        "contentsRoot": sha256_json(table_identities),
        "eventBoundary": event_boundary,
    }


def publish_file_exclusive(staged: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(staged, destination)
    except FileExistsError as exc:
        raise PostOfficeError("PON_OUTPUT_EXISTS", "Output file already exists", {"path": str(destination)}) from exc


def write_json(path: Path, value: Any, *, overwrite: bool = False) -> None:
    path = path.resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    staged = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with staged.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(staged, path)
        else:
            publish_file_exclusive(staged, path)
    finally:
        if os.path.lexists(staged):
            staged.unlink()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
