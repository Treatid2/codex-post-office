# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .canonical import read_json, sha256_file, write_json
from .diagnostics import PostOfficeError


def capture_payload_delta(
    capture_root: Path, baseline_manifest: Path, source_state_root: Path, output_root: Path
) -> dict[str, Any]:
    capture_root = capture_root.resolve(strict=True)
    baseline_manifest = baseline_manifest.resolve(strict=True)
    source_state_root = source_state_root.resolve(strict=True)
    output_root = output_root.resolve(strict=False)
    if output_root.exists() or os.path.lexists(output_root):
        raise PostOfficeError("PON_OUTPUT_EXISTS", "Payload delta output root already exists", {"path": str(output_root)})
    capture_manifest_path = capture_root / "capture-manifest.json"
    capture = read_json(capture_manifest_path)
    baseline = read_json(baseline_manifest)
    if not isinstance(capture, dict) or not isinstance(baseline, dict):
        raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Capture or baseline manifest is invalid", {})
    inventory = {
        item["path"]: item for item in capture.get("files", [])
        if isinstance(item, dict) and isinstance(item.get("path"), str) and item["path"].startswith("payloads/")
    }
    baseline_by_source = {item["sourcePath"]: item for item in baseline.get("payloads", [])}
    if not set(baseline_by_source).issubset(inventory):
        raise PostOfficeError("PON_MIGRATION_MISMATCH", "Baseline payloads are not a subset of the frozen capture", {})
    for source_path, item in baseline_by_source.items():
        retained = inventory[source_path]
        if retained.get("sha256") != item.get("sha256") or retained.get("bytes") != item.get("bytes"):
            raise PostOfficeError("PON_MIGRATION_MISMATCH", "A baseline payload changed at the frozen boundary", {"path": source_path})
    delta_inventory = [inventory[path] for path in sorted(set(inventory) - set(baseline_by_source))]
    staging = output_root.with_name(f".{output_root.name}.{uuid.uuid4().hex}.tmp")
    records: list[dict[str, Any]] = []
    try:
        staging.mkdir(parents=True, exist_ok=False)
        for item in delta_inventory:
            digest = str(item["sha256"])
            source = source_state_root / Path(str(item["path"]))
            destination_relative = Path("objects") / digest[:2] / digest
            destination = staging / destination_relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            if destination.stat().st_size != item["bytes"] or sha256_file(destination) != digest:
                raise PostOfficeError("PON_SNAPSHOT_INTEGRITY_FAILURE", "Copied payload delta object failed verification", {"path": str(source)})
            records.append({
                "sourcePath": str(item["path"]).replace("\\", "/"),
                "capturedPath": destination_relative.as_posix(),
                "bytes": int(item["bytes"]),
                "sha256": digest,
            })
        hasher = hashlib.sha256()
        for item in records:
            hasher.update(f"{item['capturedPath']}\n{item['bytes']}\n{item['sha256']}\n".encode("utf-8"))
        document = {
            "schemaVersion": "1",
            "phase": "P1_EXACT_READ_ONLY_CAPTURE_AND_RECONCILIATION",
            "status": "FROZEN_POST_BASELINE_DELTA",
            "authority": "EVIDENCE_ONLY_OLD_POST_OFFICE_REMAINS_AUTHORITATIVE",
            "sourceStateRoot": str(source_state_root),
            "sourceCaptureManifest": str(capture_manifest_path),
            "sourceCaptureManifestSha256": sha256_file(capture_manifest_path),
            "sourceCaptureRoot": capture.get("captureRoot"),
            "sourcePayloadCount": len(inventory),
            "baselinePayloadManifest": str(baseline_manifest),
            "baselinePayloadManifestSha256": sha256_file(baseline_manifest),
            "baselineCaptureRoot": baseline.get("sourceCaptureRoot"),
            "baselinePayloadCount": len(baseline_by_source),
            "payloadCount": len(records),
            "payloadBytes": sum(item["bytes"] for item in records),
            "payloadMerkleRoot": hasher.hexdigest(),
            "createdAtUtc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "coverage": "The retained baseline and this delta exactly cover the source capture manifest payload inventory.",
            "payloads": records,
        }
        write_json(staging / "payload-capture-manifest.json", document)
        os.replace(staging, output_root)
        return {"ok": True, **{key: value for key, value in document.items() if key != "payloads"}, "outputRoot": str(output_root)}
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
