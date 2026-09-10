# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from .canonical import read_json, sha256_file, sha256_json, write_json
from .contract_specs import (
    DRAFT,
    SCHEMA_PREFIX,
    LOCAL_EXTENSION_OPERATIONS,
    build_entity_schemas,
    build_operation_specs,
    build_request_schema,
    build_result_schema,
)
from .diagnostics import DIAGNOSTICS, PostOfficeError
from .mini_schema import ValidationFailure, check_schema_shape, validate


def contract_root(plugin_root: Path) -> Path:
    return plugin_root / "contracts" / "v1"


def _diagnostic_schema() -> dict[str, Any]:
    example_code = sorted(DIAGNOSTICS)[0]
    return {
        "$schema": DRAFT,
        "$id": f"{SCHEMA_PREFIX}/diagnostic.schema.json",
        "title": "Post Office Next diagnostic",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "schemaVersion": {"const": "1"},
            "code": {"enum": sorted(DIAGNOSTICS)},
            "message": {"type": "string", "minLength": 1},
            "severity": {"enum": ["INFO", "WARNING", "ERROR", "CRITICAL"]},
            "retryable": {"type": "boolean"},
            "details": {"type": "object"},
        },
        "required": ["schemaVersion", "code", "message", "severity", "retryable", "details"],
        "examples": [{
            "schemaVersion": "1",
            "code": example_code,
            "message": "Example diagnostic",
            "severity": DIAGNOSTICS[example_code]["severity"],
            "retryable": DIAGNOSTICS[example_code]["retryable"],
            "details": {},
        }],
    }


def _common_envelope_schema(operations: list[str]) -> dict[str, Any]:
    return {
        "$schema": DRAFT,
        "$id": f"{SCHEMA_PREFIX}/operation-envelope.schema.json",
        "title": "Post Office Next common operation envelope",
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "schemaVersion": {"const": "1"},
            "operation": {"enum": operations},
            "requestId": {"type": "string", "minLength": 3},
            "actor": {"type": "object", "minProperties": 1},
            "authority": {
                "oneOf": [
                    {"type": "object", "additionalProperties": False,
                     "properties": {"capabilityId": {"type": "string", "minLength": 3}},
                     "required": ["capabilityId"]},
                    {"type": "object", "additionalProperties": False,
                     "properties": {"capabilityId": {"type": "string", "minLength": 3},
                                    "authorityGrantId": {"type": "string", "minLength": 3}},
                     "required": ["capabilityId", "authorityGrantId"]},
                    {"type": "object", "additionalProperties": False,
                     "properties": {"capabilityId": {"type": "string", "minLength": 3},
                                    "exactAuthorActionId": {"type": "string", "minLength": 3}},
                     "required": ["capabilityId", "exactAuthorActionId"]},
                ],
            },
            "aggregate": {"type": "object"},
            "parameters": {"type": "object"},
        },
        "required": ["schemaVersion", "operation", "requestId", "actor", "authority", "parameters"],
        "examples": [{
            "schemaVersion": "1",
            "operation": "hub.status",
            "requestId": "PON-REQUEST-001",
            "actor": {"id": "PON-ACTOR-001", "kind": "HUMAN"},
            "authority": {"capabilityId": "PON-CAPABILITY-001"},
            "parameters": {},
        }],
    }


def generate_contracts(plugin_root: Path) -> dict[str, Any]:
    root = contract_root(plugin_root)
    entity_dir = root / "entities"
    request_dir = root / "operations" / "requests"
    result_dir = root / "operations" / "results"
    fixture_good_dir = root / "fixtures" / "good"
    fixture_bad_dir = root / "fixtures" / "bad"
    for directory in (entity_dir, request_dir, result_dir, fixture_good_dir, fixture_bad_dir):
        directory.mkdir(parents=True, exist_ok=True)

    entity_schemas = build_entity_schemas()
    operation_specs = build_operation_specs()
    written: list[Path] = []
    for name, schema in sorted(entity_schemas.items()):
        path = entity_dir / f"{name}.schema.json"
        write_json(path, schema, overwrite=True)
        written.append(path)
        good = deepcopy(schema["examples"][0])
        bad = deepcopy(good)
        bad.pop(schema["required"][0])
        write_json(fixture_good_dir / f"entity.{name}.json", good, overwrite=True)
        write_json(fixture_bad_dir / f"entity.{name}.json", bad, overwrite=True)

    operation_catalogue: list[dict[str, Any]] = []
    for operation, spec in sorted(operation_specs.items()):
        request_schema = build_request_schema(operation, spec)
        result_schema = build_result_schema(operation, spec)
        request_path = request_dir / f"{operation}.schema.json"
        result_path = result_dir / f"{operation}.schema.json"
        write_json(request_path, request_schema, overwrite=True)
        write_json(result_path, result_schema, overwrite=True)
        written.extend([request_path, result_path])
        operation_catalogue.append({
            "operation": operation,
            "category": spec["category"],
            "mutates": spec["mutates"],
            "minimumAuthority": spec["minimumAuthority"],
            "contractStatus": spec["contractStatus"],
            "catalogueSource": spec["catalogueSource"],
            "concurrency": "PER_AGGREGATE_VERSION_OR_ROOT" if spec["mutates"] else "NONE_READ_ONLY",
            "idempotency": "REQUEST_ID_AND_CANONICAL_REQUEST_HASH",
            "requestSchema": f"operations/requests/{operation}.schema.json",
            "resultSchema": f"operations/results/{operation}.schema.json",
        })
        good = deepcopy(request_schema["examples"][0])
        bad = deepcopy(good)
        bad.pop("requestId")
        write_json(fixture_good_dir / f"operation.{operation}.request.json", good, overwrite=True)
        write_json(fixture_bad_dir / f"operation.{operation}.request.json", bad, overwrite=True)
        success = deepcopy(result_schema["examples"][0])
        failure = deepcopy(result_schema["examples"][1])
        bad_success = deepcopy(success)
        bad_success["receipt"].pop("mutationEventId" if spec["mutates"] else "stateRoot")
        bad_failure = deepcopy(failure)
        bad_failure["receipt"] = deepcopy(success["receipt"])
        for polarity, value in (("success", success), ("failure", failure)):
            write_json(fixture_good_dir / f"operation.{operation}.result.{polarity}.json", value, overwrite=True)
        write_json(fixture_bad_dir / f"operation.{operation}.result.success.json", bad_success, overwrite=True)
        write_json(fixture_bad_dir / f"operation.{operation}.result.failure.json", bad_failure, overwrite=True)

    diagnostic_schema = _diagnostic_schema()
    envelope_schema = _common_envelope_schema(sorted(operation_specs))
    write_json(root / "diagnostic.schema.json", diagnostic_schema, overwrite=True)
    write_json(root / "operation-envelope.schema.json", envelope_schema, overwrite=True)
    written.extend([root / "diagnostic.schema.json", root / "operation-envelope.schema.json"])
    write_json(root / "operation-catalogue.json", {
        "schemaVersion": "1",
        "operations": operation_catalogue,
        "invariants": [
            "default authority is deny",
            "read operations never mutate legacy state",
            "mutations use per-aggregate concurrency",
            "global snapshot roots are audit/reconciliation inputs only",
            "semantic identity is independent of transport identity",
            "only exact author authority closes a semantic cycle",
            "every mutation requires caller capability and either scoped semantic authority or an exact author action",
            "Google Drive is a transient browser bridge and never durable retention or authority",
        ],
    }, overwrite=True)
    write_json(root / "minimum-slice.operations.json", {
        "schemaVersion": "1",
        "operations": sorted(operation_specs),
        "fgpmWishlistOperations": sorted(set(operation_specs) - LOCAL_EXTENSION_OPERATIONS),
        "postOfficeNextExtensions": sorted(LOCAL_EXTENSION_OPERATIONS),
        "implementedInP0P1": ["hub.snapshot", "hub.reconcile.preview"],
        "implementedInP3_1": ["hub.status"],
        "contractOnlyUntilLaterSlice": sorted(
            set(operation_specs) - {"hub.snapshot", "hub.reconcile.preview", "hub.status"}
        ),
    }, overwrite=True)
    write_json(root / "diagnostics.json", {
        "schemaVersion": "1",
        "diagnostics": [{"code": code, **details} for code, details in sorted(DIAGNOSTICS.items())],
    }, overwrite=True)

    manifest_entries = []
    for path in sorted(root.rglob("*.json")):
        if path.name == "manifest.json":
            continue
        manifest_entries.append({
            "path": path.relative_to(root).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        })
    manifest = {
        "schemaVersion": "1",
        "files": manifest_entries,
        "contractRoot": sha256_json(manifest_entries),
        "entityCount": len(entity_schemas),
        "operationCount": len(operation_specs),
    }
    write_json(root / "manifest.json", manifest, overwrite=True)
    return {"ok": True, "contractRoot": manifest["contractRoot"], **manifest}


def validate_contracts(plugin_root: Path) -> dict[str, Any]:
    root = contract_root(plugin_root)
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        raise PostOfficeError("PON_PATH_NOT_FOUND", "Contract manifest does not exist", {"path": str(manifest_path)})
    manifest = read_json(manifest_path)
    errors: list[dict[str, Any]] = []
    schemas: dict[str, dict[str, Any]] = {}
    for entry in manifest["files"]:
        path = root / entry["path"]
        if not path.exists() or path.stat().st_size != entry["bytes"] or sha256_file(path) != entry["sha256"]:
            errors.append({"path": entry["path"], "error": "manifest mismatch"})
        if entry["path"].endswith(".schema.json"):
            try:
                schema = read_json(path)
                check_schema_shape(schema)
                schemas[entry["path"]] = schema
                for example in schema.get("examples", []):
                    validate(example, schema)
            except (ValidationFailure, ValueError) as exc:
                errors.append({"path": entry["path"], "error": str(exc)})

    entity_names = sorted(path.stem.replace(".schema", "") for path in (root / "entities").glob("*.schema.json"))
    operation_specs = build_operation_specs()
    good_count = 0
    bad_count = 0
    for name in entity_names:
        schema = schemas[f"entities/{name}.schema.json"]
        good = read_json(root / "fixtures" / "good" / f"entity.{name}.json")
        bad = read_json(root / "fixtures" / "bad" / f"entity.{name}.json")
        try:
            validate(good, schema)
            good_count += 1
        except ValidationFailure as exc:
            errors.append({"path": f"good/entity.{name}.json", "error": str(exc)})
        try:
            validate(bad, schema)
            errors.append({"path": f"bad/entity.{name}.json", "error": "invalid fixture unexpectedly passed"})
        except ValidationFailure:
            bad_count += 1
    for operation in sorted(operation_specs):
        schema = schemas[f"operations/requests/{operation}.schema.json"]
        good = read_json(root / "fixtures" / "good" / f"operation.{operation}.request.json")
        bad = read_json(root / "fixtures" / "bad" / f"operation.{operation}.request.json")
        try:
            validate(good, schema)
            good_count += 1
        except ValidationFailure as exc:
            errors.append({"path": f"good/operation.{operation}.request.json", "error": str(exc)})
        try:
            validate(bad, schema)
            errors.append({"path": f"bad/operation.{operation}.request.json", "error": "invalid fixture unexpectedly passed"})
        except ValidationFailure:
            bad_count += 1
        result_schema = schemas[f"operations/results/{operation}.schema.json"]
        for polarity in ("success", "failure"):
            good_result = read_json(root / "fixtures" / "good" / f"operation.{operation}.result.{polarity}.json")
            bad_result = read_json(root / "fixtures" / "bad" / f"operation.{operation}.result.{polarity}.json")
            try:
                validate(good_result, result_schema)
                good_count += 1
            except ValidationFailure as exc:
                errors.append({"path": f"good/operation.{operation}.result.{polarity}.json", "error": str(exc)})
            try:
                validate(bad_result, result_schema)
                errors.append({"path": f"bad/operation.{operation}.result.{polarity}.json", "error": "invalid fixture unexpectedly passed"})
            except ValidationFailure:
                bad_count += 1

    catalogue = read_json(root / "operation-catalogue.json")
    minimum = read_json(root / "minimum-slice.operations.json")
    catalogue_names = sorted(item["operation"] for item in catalogue["operations"])
    if catalogue_names != minimum["operations"] or catalogue_names != sorted(operation_specs):
        errors.append({"path": "operation-catalogue.json", "error": "operation name sets differ"})
    recalculated_root = sha256_json(manifest["files"])
    if recalculated_root != manifest["contractRoot"]:
        errors.append({"path": "manifest.json", "error": "contract root mismatch"})
    if errors:
        raise PostOfficeError("PON_CONTRACT_INVALID", "Contract validation failed", {"errors": errors})
    return {
        "ok": True,
        "contractRoot": manifest["contractRoot"],
        "schemasValidated": len(schemas),
        "goodFixturesValidated": good_count,
        "badFixturesRejected": bad_count,
        "operationCount": len(operation_specs),
        "entityCount": len(entity_names),
    }
