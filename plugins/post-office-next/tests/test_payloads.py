# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import tempfile
import unittest
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from post_office.canonical import sha256_file, write_json
from post_office.payloads import capture_payload_delta


class PayloadDeltaTests(unittest.TestCase):
    def test_delta_exactly_excludes_retained_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            capture = root / "capture"
            baseline = root / "baseline"
            capture.mkdir(); baseline.mkdir()
            first = b"first"; second = b"second"
            first_digest = __import__("hashlib").sha256(first).hexdigest()
            second_digest = __import__("hashlib").sha256(second).hexdigest()
            first_path = capture / "payloads" / first_digest[:2] / first_digest
            second_path = capture / "payloads" / second_digest[:2] / second_digest
            first_path.parent.mkdir(parents=True); second_path.parent.mkdir(parents=True)
            first_path.write_bytes(first); second_path.write_bytes(second)
            baseline_object = baseline / "objects" / first_digest[:2] / first_digest
            baseline_object.parent.mkdir(parents=True); baseline_object.write_bytes(first)
            baseline_manifest = baseline / "payload-capture-manifest.json"
            write_json(baseline_manifest, {
                "schemaVersion": "1", "sourceCaptureRoot": "b" * 64,
                "payloadCount": 1, "payloadBytes": len(first),
                "payloadMerkleRoot": __import__("hashlib").sha256(
                    f"objects/{first_digest[:2]}/{first_digest}\n{len(first)}\n{first_digest}\n".encode()
                ).hexdigest(),
                "payloads": [{"sourcePath": first_path.relative_to(capture).as_posix(),
                              "capturedPath": baseline_object.relative_to(baseline).as_posix(),
                              "bytes": len(first), "sha256": first_digest}],
            })
            files = [
                {"path": path.relative_to(capture).as_posix(), "bytes": path.stat().st_size,
                 "sha256": sha256_file(path), "modifiedNs": path.stat().st_mtime_ns}
                for path in (first_path, second_path)
            ]
            write_json(capture / "capture-manifest.json", {
                "captureRoot": "c" * 64, "sourceStateRoot": "X:/retired", "files": files,
            })
            output = root / "delta"
            result = capture_payload_delta(capture, baseline_manifest, capture, output)
            self.assertEqual(1, result["payloadCount"])
            self.assertTrue((output / "objects" / second_digest[:2] / second_digest).is_file())


if __name__ == "__main__":
    unittest.main()
