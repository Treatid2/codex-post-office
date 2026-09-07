# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import re
from typing import Any


class ValidationFailure(ValueError):
    pass


def _type_matches(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(expected, False)


def validate(instance: Any, schema: dict[str, Any], path: str = "$") -> None:
    if "const" in schema and instance != schema["const"]:
        raise ValidationFailure(f"{path}: expected constant {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        raise ValidationFailure(f"{path}: value is not in enum")
    if "not" in schema:
        try:
            validate(instance, schema["not"], path)
        except ValidationFailure:
            pass
        else:
            raise ValidationFailure(f"{path}: prohibited schema matched")
    if "oneOf" in schema:
        matches = 0
        for branch in schema["oneOf"]:
            try:
                validate(instance, branch, path)
                matches += 1
            except ValidationFailure:
                pass
        if matches != 1:
            raise ValidationFailure(f"{path}: expected exactly one matching schema, found {matches}")
    if "anyOf" in schema:
        for branch in schema["anyOf"]:
            try:
                validate(instance, branch, path)
                break
            except ValidationFailure:
                continue
        else:
            raise ValidationFailure(f"{path}: no anyOf schema matched")
    for branch in schema.get("allOf", []):
        validate(instance, branch, path)
    expected = schema.get("type")
    if expected:
        expected_types = [expected] if isinstance(expected, str) else expected
        if not any(_type_matches(instance, item) for item in expected_types):
            raise ValidationFailure(f"{path}: expected type {expected_types}")
    if isinstance(instance, dict):
        for name in schema.get("required", []):
            if name not in instance:
                raise ValidationFailure(f"{path}: missing required property {name}")
        properties = schema.get("properties", {})
        for name, value in instance.items():
            if name in properties:
                validate(value, properties[name], f"{path}.{name}")
            elif schema.get("additionalProperties") is False:
                raise ValidationFailure(f"{path}: unexpected property {name}")
        if "minProperties" in schema and len(instance) < schema["minProperties"]:
            raise ValidationFailure(f"{path}: too few properties")
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            raise ValidationFailure(f"{path}: too few items")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            raise ValidationFailure(f"{path}: too many items")
        if schema.get("uniqueItems") and len({repr(item) for item in instance}) != len(instance):
            raise ValidationFailure(f"{path}: duplicate array item")
        if "items" in schema:
            for index, value in enumerate(instance):
                validate(value, schema["items"], f"{path}[{index}]")
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            raise ValidationFailure(f"{path}: string is too short")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            raise ValidationFailure(f"{path}: string is too long")
        if "pattern" in schema and re.fullmatch(schema["pattern"], instance) is None:
            raise ValidationFailure(f"{path}: string does not match pattern")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            raise ValidationFailure(f"{path}: value is below minimum")
        if "maximum" in schema and instance > schema["maximum"]:
            raise ValidationFailure(f"{path}: value is above maximum")


def check_schema_shape(schema: dict[str, Any]) -> None:
    required_top = {"$schema", "$id", "title", "type"}
    missing = sorted(required_top - set(schema))
    if missing:
        raise ValidationFailure(f"schema missing top-level fields: {missing}")
    if schema["$schema"] != "http://json-schema.org/draft-07/schema#":
        raise ValidationFailure("schema must declare JSON Schema draft-07")
