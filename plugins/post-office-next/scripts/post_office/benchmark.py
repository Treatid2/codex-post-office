# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import platform
import statistics
import tempfile
import time
from math import ceil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .canonical import require_new_output_file, sha256_json, write_json
from .contracts import validate_contracts
from .diagnostics import PostOfficeError
from .snapshot import create_snapshot


def _measure(action: Callable[[], Any], iterations: int) -> dict[str, Any]:
    samples: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter_ns()
        action()
        elapsed = time.perf_counter_ns() - started
        samples.append(round(elapsed / 1_000_000, 3))
    ordered = sorted(samples)
    p95_index = min(len(ordered) - 1, max(0, ceil(len(ordered) * 0.95) - 1))
    return {
        "iterations": iterations,
        "samplesMs": samples,
        "minimumMs": ordered[0],
        "medianMs": round(statistics.median(ordered), 3),
        "p95Ms": ordered[p95_index],
        "maximumMs": ordered[-1],
    }


def performance_baseline(
    plugin_root: Path,
    output: Path,
    iterations: int = 5,
    capture_root: Path | None = None,
) -> dict[str, Any]:
    if iterations < 3 or iterations > 100:
        raise PostOfficeError("PON_INPUT_INVALID", "iterations must be between 3 and 100", {"iterations": iterations})
    output = require_new_output_file(output, protected_roots=[capture_root] if capture_root else [])

    contract_result: dict[str, Any] = {}

    def validate_action() -> None:
        nonlocal contract_result
        contract_result = validate_contracts(plugin_root)

    measurements: dict[str, Any] = {
        "contractValidation": _measure(validate_action, iterations),
    }
    snapshot_root = None
    if capture_root is not None:
        observed_roots: list[str] = []

        with tempfile.TemporaryDirectory(prefix="post-office-next-benchmark-") as temporary:
            temporary_root = Path(temporary)
            sequence = 0

            def snapshot_action() -> None:
                nonlocal sequence
                destination = temporary_root / f"snapshot-{sequence:03d}.json"
                sequence += 1
                observed_roots.append(create_snapshot(capture_root, destination)["snapshotRoot"])

            measurements["snapshotFromFrozenCapture"] = _measure(snapshot_action, iterations)
        if len(set(observed_roots)) != 1:
            raise PostOfficeError(
                "PON_SNAPSHOT_INTEGRITY_FAILURE",
                "Frozen capture produced non-deterministic snapshot roots during benchmark",
                {"observedRoots": observed_roots},
            )
        snapshot_root = observed_roots[0]

    identity = {
        "schemaVersion": "1",
        "kind": "P0_PERFORMANCE_BASELINE",
        "measuredAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "runtime": {"python": platform.python_version(), "platform": platform.platform()},
        "contractRoot": contract_result["contractRoot"],
        "operationCount": contract_result["operationCount"],
        "entityCount": contract_result["entityCount"],
        "captureRoot": str(capture_root.resolve()) if capture_root is not None else None,
        "snapshotRoot": snapshot_root,
        "measurements": measurements,
        "interpretation": "Baseline evidence only; thresholds require an explicit acceptance budget.",
    }
    receipt = {**identity, "receiptSha256": sha256_json(identity)}
    write_json(output, receipt)
    return {
        "ok": True,
        "receiptSha256": receipt["receiptSha256"],
        "contractRoot": receipt["contractRoot"],
        "snapshotRoot": receipt["snapshotRoot"],
        "output": str(output.resolve()),
        "measurements": measurements,
    }
