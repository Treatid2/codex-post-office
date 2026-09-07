# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import sys
import re
import unittest
from copy import deepcopy
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from post_office.contracts import _diagnostic_schema, validate_contracts  # noqa: E402
from post_office.contract_specs import (  # noqa: E402
    DATETIME_PATTERN,
    ID_PATTERN,
    LOCAL_EXTENSION_OPERATIONS,
    SAFE_FILENAME_PATTERN,
    SAFE_RELATIVE_PATH_PATTERN,
    SHA256_PATTERN,
    build_entity_schemas,
    build_operation_specs,
    build_request_schema,
    build_result_schema,
)
from post_office.diagnostics import DIAGNOSTICS  # noqa: E402
from post_office.mini_schema import ValidationFailure, validate  # noqa: E402


FGPM_WISHLIST_OPERATIONS = {
    "hub.status", "hub.reconcile.preview", "hub.reconcile.apply", "authority.inspect", "authority.grant",
    "authority.revoke", "project.planCreate", "project.create", "project.update", "project.pause",
    "project.archive", "package.register", "package.registerVersion", "package.rehome.preview", "package.rehome",
    "package.deprecate", "interface.register", "interface.deprecate", "capabilityRequest.create",
    "capabilityRequest.triage", "capabilityRequest.fulfil", "task.planCreate", "task.create", "task.activate",
    "task.block", "task.recordResponse", "task.review", "task.close", "task.moveProject", "changeSet.create",
    "changeSet.addTask", "changeSet.startIntegration", "changeSet.decide", "integration.record",
    "endpoint.allocate", "endpoint.revoke", "mailbox.allocate", "mailbox.rotateGeneration", "contextBundle.build",
    "message.plan", "message.register", "message.route", "message.acknowledge", "message.review", "message.close",
    "bundle.verify", "bundle.supersedeBeforeRegistration", "transport.inspect", "transport.retry",
    "transport.tombstoneDuplicate", "transport.quarantine", "cycle.open", "cycle.markAwaitingReview",
    "cycle.accept", "cycle.close",
}


class ContractTests(unittest.TestCase):
    def test_generated_contracts_and_fixtures_validate(self) -> None:
        result = validate_contracts(PLUGIN_ROOT)
        self.assertTrue(result["ok"])
        self.assertEqual(result["entityCount"], 22)
        self.assertEqual(result["operationCount"], 64)
        self.assertEqual(result["goodFixturesValidated"], 214)
        self.assertEqual(result["badFixturesRejected"], 214)

    def test_full_fgpm_wishlist_and_extensions_are_explicit(self) -> None:
        operations = set(build_operation_specs())
        self.assertEqual(len(FGPM_WISHLIST_OPERATIONS), 55)
        self.assertEqual(len(LOCAL_EXTENSION_OPERATIONS), 9)
        self.assertEqual(operations, FGPM_WISHLIST_OPERATIONS | LOCAL_EXTENSION_OPERATIONS)

    def test_minimum_read_operations_are_present(self) -> None:
        operations = build_operation_specs()
        for required in (
            "hub.status", "hub.snapshot", "hub.reconcile.preview", "authority.inspect",
            "project.read", "task.read", "package.read", "interface.read",
            "capabilityRequest.read", "transport.inspect", "provisioning.inspect", "attention.list",
        ):
            self.assertIn(required, operations)
            self.assertFalse(operations[required]["mutates"])

    def test_task_schema_expresses_one_package_invariant(self) -> None:
        task = build_entity_schemas()["task"]
        self.assertEqual(len(task["oneOf"]), 3)
        package_branch = task["oneOf"][0]["properties"]["mutablePackageIds"]
        self.assertEqual(package_branch["minItems"], 1)
        self.assertEqual(package_branch["maxItems"], 1)

    def test_state_machines_and_hub_event_are_executable(self) -> None:
        entities = build_entity_schemas()
        self.assertIn("hub-event", entities)
        self.assertIn("CANCELLED", entities["task"]["properties"]["state"]["enum"])
        self.assertIn("TASKS_AUTHORISED", entities["change-set"]["properties"]["state"]["enum"])
        self.assertIn("VALIDATED", entities["integration-candidate"]["properties"]["state"]["enum"])
        message_states = entities["semantic-message"]["properties"]["state"]["enum"]
        self.assertIn("SUPERSEDED_BEFORE_REGISTRATION", message_states)
        self.assertIn("CANCELLED_BEFORE_DELIVERY", message_states)

    def test_google_drive_is_bridge_only_not_durable_storage(self) -> None:
        entities = build_entity_schemas()
        locations = entities["storage-copy"]["properties"]["locationKind"]["enum"]
        self.assertNotIn("DRIVE", locations)
        self.assertEqual(entities["browser-bridge-transfer"]["properties"]["provider"]["const"], "GOOGLE_DRIVE")
        self.assertNotIn("drive", entities["project"]["properties"]["roots"]["properties"])

    def test_bundle_paths_reject_traversal(self) -> None:
        bundle = build_entity_schemas()["message-bundle"]
        malicious = deepcopy(bundle["examples"][0])
        malicious["payloads"][0]["path"] = "../caller-secrets/token"
        with self.assertRaises(ValidationFailure):
            validate(malicious, bundle)

    def test_published_patterns_are_full_value_constraints(self) -> None:
        for pattern in (ID_PATTERN, SHA256_PATTERN, DATETIME_PATTERN, SAFE_RELATIVE_PATH_PATTERN, SAFE_FILENAME_PATTERN):
            self.assertTrue(pattern.startswith("^"))
            self.assertTrue(pattern.endswith("$"))
        adversarial = {
            SAFE_RELATIVE_PATH_PATTERN: "../caller-secrets/token",
            SAFE_FILENAME_PATTERN: "../evil.json",
            SHA256_PATTERN: "junk" + "a" * 64 + "junk",
            ID_PATTERN: "!PON-VALID-001!",
            DATETIME_PATTERN: "x2026-09-04T12:00:00Zx",
        }
        for pattern, value in adversarial.items():
            self.assertIsNone(re.search(pattern, value), (pattern, value))

    def test_mutation_requires_capability_and_one_semantic_authority(self) -> None:
        spec = build_operation_specs()["task.create"]
        schema = build_request_schema("task.create", spec)
        good = deepcopy(schema["examples"][0])
        validate(good, schema)
        missing_capability = deepcopy(good)
        missing_capability["authority"].pop("capabilityId")
        with self.assertRaises(ValidationFailure):
            validate(missing_capability, schema)
        ambiguous = deepcopy(good)
        ambiguous["authority"]["exactAuthorActionId"] = "PON-AUTHOR-ACTION-001"
        with self.assertRaises(ValidationFailure):
            validate(ambiguous, schema)

    def test_author_only_cycle_close_requires_exact_author_action(self) -> None:
        operations = build_operation_specs()
        for operation in ("authority.grant", "authority.revoke", "project.archive", "cycle.open", "cycle.accept", "cycle.close"):
            schema = build_request_schema(operation, operations[operation])
            self.assertEqual(schema["properties"]["authority"]["required"], ["capabilityId", "exactAuthorActionId"])
            ambiguous = deepcopy(schema["examples"][0])
            ambiguous["authority"]["authorityGrantId"] = "PON-GRANT-001"
            with self.assertRaises(ValidationFailure):
                validate(ambiguous, schema)

    def test_successful_mutation_requires_event_and_receipt(self) -> None:
        spec = build_operation_specs()["task.create"]
        schema = build_result_schema("task.create", spec)
        good = deepcopy(schema["examples"][0])
        validate(good, schema)
        without_event = deepcopy(good)
        without_event.pop("eventId")
        with self.assertRaises(ValidationFailure):
            validate(without_event, schema)
        without_receipt_event = deepcopy(good)
        without_receipt_event["receipt"].pop("mutationEventId")
        with self.assertRaises(ValidationFailure):
            validate(without_receipt_event, schema)

    def test_result_failure_and_diagnostic_use_one_versioned_shape(self) -> None:
        spec = build_operation_specs()["task.create"]
        schema = build_result_schema("task.create", spec)
        failure = deepcopy(schema["examples"][1])
        validate(failure, schema)
        validate(failure["diagnostic"], _diagnostic_schema())
        false_receipt = deepcopy(failure)
        false_receipt["receipt"] = deepcopy(schema["examples"][0]["receipt"])
        with self.assertRaises(ValidationFailure):
            validate(false_receipt, schema)

    def test_core_create_payloads_are_typed(self) -> None:
        operations = build_operation_specs()
        for operation, parameter in (
            ("authority.grant", "grant"),
            ("package.register", "package"),
            ("interface.register", "interface"),
            ("capabilityRequest.create", "request"),
            ("message.register", "semanticMessage"),
        ):
            payload = operations[operation]["parameters"]["properties"][parameter]
            self.assertFalse(payload["additionalProperties"])
            self.assertGreater(len(payload["required"]), 1)

    def test_diagnostics_are_namespaced_and_stable(self) -> None:
        self.assertGreaterEqual(len(DIAGNOSTICS), 10)
        self.assertTrue(all(code.startswith("PON_") for code in DIAGNOSTICS))


if __name__ == "__main__":
    unittest.main()
