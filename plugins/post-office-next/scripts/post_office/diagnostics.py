# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


DIAGNOSTICS: dict[str, dict[str, Any]] = {
    "PON_CONTRACT_INVALID": {"severity": "ERROR", "retryable": False},
    "PON_CONTRACT_FIXTURE_UNEXPECTED": {"severity": "ERROR", "retryable": False},
    "PON_INPUT_INVALID": {"severity": "ERROR", "retryable": False},
    "PON_PATH_NOT_FOUND": {"severity": "ERROR", "retryable": False},
    "PON_OUTPUT_EXISTS": {"severity": "ERROR", "retryable": False},
    "PON_PATH_CONFLICT": {"severity": "ERROR", "retryable": False},
    "PON_LEGACY_SCHEMA_UNSUPPORTED": {"severity": "ERROR", "retryable": False},
    "PON_SNAPSHOT_SOURCE_CHANGED": {"severity": "ERROR", "retryable": True},
    "PON_SNAPSHOT_INTEGRITY_FAILURE": {"severity": "ERROR", "retryable": False},
    "PON_RECONCILIATION_AMBIGUOUS": {"severity": "WARNING", "retryable": False},
    "PON_RECONCILIATION_HASH_MISMATCH": {"severity": "ERROR", "retryable": False},
    "PON_RECONCILIATION_DUPLICATE": {"severity": "WARNING", "retryable": False},
    "PON_RECONCILIATION_ORPHAN": {"severity": "WARNING", "retryable": False},
    "PON_PRODUCTION_MUTATION_FORBIDDEN": {"severity": "ERROR", "retryable": False},
    "PON_DATABASE_INVALID": {"severity": "ERROR", "retryable": False},
    "PON_MIGRATION_MISMATCH": {"severity": "ERROR", "retryable": False},
    "PON_INTERNAL_ERROR": {"severity": "ERROR", "retryable": False},
}


@dataclass(slots=True)
class PostOfficeError(Exception):
    code: str
    message: str
    details: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.code not in DIAGNOSTICS:
            raise ValueError(f"Unknown public diagnostic: {self.code}")
        Exception.__init__(self, self.message)

    def as_result(self) -> dict[str, Any]:
        spec = DIAGNOSTICS[self.code]
        return {
            "ok": False,
            "diagnostic": {
                "schemaVersion": "1",
                "code": self.code,
                "message": self.message,
                "severity": spec["severity"],
                "retryable": spec["retryable"],
                "details": self.details or {},
            },
        }
