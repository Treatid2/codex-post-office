# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from post_office.canonical import read_json, sha256_file, sha256_json  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest")
    args = parser.parse_args()
    path = Path(args.manifest).resolve()
    manifest = read_json(path)
    errors = []
    for root_name, list_name in (("pluginRoot", "pluginFiles"), ("artifactRoot", "artifactFiles")):
        root = Path(manifest[root_name])
        for entry in manifest[list_name]:
            candidate = root / entry["path"]
            if not candidate.is_file():
                errors.append({"path": str(candidate), "error": "missing"})
            elif candidate.stat().st_size != entry["bytes"] or sha256_file(candidate) != entry["sha256"]:
                errors.append({"path": str(candidate), "error": "size-or-hash-mismatch"})
    identity = {key: value for key, value in manifest.items() if key != "deliveryRoot"}
    if sha256_json(identity) != manifest["deliveryRoot"]:
        errors.append({"path": str(path), "error": "delivery-root-mismatch"})
    result = {"ok": not errors, "deliveryRoot": manifest["deliveryRoot"], "verifiedFiles": len(manifest["pluginFiles"]) + len(manifest["artifactFiles"]), "errors": errors}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
