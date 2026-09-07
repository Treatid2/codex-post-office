# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from post_office.canonical import require_new_output_file, sha256_file, sha256_json, write_json  # noqa: E402


def inventory(root: Path, excluded: set[Path]) -> list[dict[str, object]]:
    entries = []
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: item.relative_to(root).as_posix()):
        if (path.resolve() in excluded or "__pycache__" in path.parts or path.suffix == ".pyc"
                or path.name.endswith("-wal") or path.name.endswith("-shm")):
            continue
        entries.append({"path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size, "sha256": sha256_file(path)})
    return entries


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin-root", required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    plugin_root = Path(args.plugin_root).resolve()
    artifact_root = Path(args.artifact_root).resolve()
    output = Path(args.output).resolve()
    output = require_new_output_file(output, protected_roots=[plugin_root])
    excluded = {output}
    plugin_files = inventory(plugin_root, excluded)
    artifact_files = inventory(artifact_root, excluded)
    identity = {
        "schemaVersion": "1",
        "pluginRoot": str(plugin_root),
        "artifactRoot": str(artifact_root),
        "pluginFiles": plugin_files,
        "artifactFiles": artifact_files,
    }
    manifest = {**identity, "deliveryRoot": sha256_json(identity)}
    write_json(output, manifest)
    print(json.dumps({
        "ok": True,
        "deliveryRoot": manifest["deliveryRoot"],
        "pluginFileCount": len(plugin_files),
        "artifactFileCount": len(artifact_files),
        "output": str(output),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
