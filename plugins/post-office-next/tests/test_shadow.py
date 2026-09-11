# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from post_office.database import initialize_database  # noqa: E402
from post_office.diagnostics import PostOfficeError  # noqa: E402
from post_office.kernel import (  # noqa: E402
    bootstrap_kernel,
    create_kernel_actor,
    create_kernel_credential,
)
from post_office.shadow import (  # noqa: E402
    create_cutover_dossier,
    execute_cutover,
    finish_prepared_cutover,
    record_cutover_rehearsal,
    record_shadow_observation,
)


class ShadowCutoverTests(unittest.TestCase):
    def test_clean_rehearsal_dossier_and_one_time_cutover(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "post-office-next.sqlite3"
            credential = root / "author.json"
            initialize_database(PLUGIN_ROOT, database)
            create_kernel_credential(credential, "PON-CAPABILITY-AUTHOR")
            bootstrap_kernel(
                database, credential, actor_id="PON-ACTOR-AUTHOR", actor_kind="HUMAN",
                actor_role="author", mode="SHADOW", plugin_root=PLUGIN_ROOT,
            )
            create_kernel_actor(
                database, credential, action_id="PON-ACTION-CREATE-COURIER",
                actor_id="PON-ACTOR-COURIER", actor_kind="COURIER", actor_role="courier",
                plugin_root=PLUGIN_ROOT,
            )
            observation = record_shadow_observation(
                database, credential, PLUGIN_ROOT, source_kind="LIVE_PROJECTION",
                source_reference="test:projection", expected_root="a" * 64,
                observed_root="a" * 64, evidence={"bounded": True},
            )
            con = sqlite3.connect(database)
            try:
                con.execute(
                    "UPDATE shadow_observations SET state='RESOLVED',resolved_at='2026-09-11T00:00:00Z' WHERE observation_id=?",
                    (observation["observationId"],),
                )
                con.commit()
            finally:
                con.close()
            evidence = {
                "captureRoot": "1" * 64, "deltaRoot": "2" * 64,
                "importRoot": "3" * 64, "replayRoot": "3" * 64,
                "backupRoot": "4" * 64, "restoreRoot": "5" * 64,
                "performanceRoot": "6" * 64,
                "checks": {
                    "captureStable": True, "restoreVerified": True,
                    "performanceWithinBudget": True, "openWorkRetained": True,
                    "reviewWorkRetained": True, "continuationWorkRetained": True,
                },
            }
            rehearsal = record_cutover_rehearsal(database, credential, PLUGIN_ROOT, evidence=evidence)
            self.assertTrue(rehearsal["passed"])
            dossier_path = root / "cutover-dossier.json"
            dossier = create_cutover_dossier(
                database, credential, PLUGIN_ROOT, legacy_final_root="7" * 64,
                output=dossier_path,
            )
            self.assertTrue(dossier["ready"])
            self.assertTrue(all(dossier["checks"]["assertions"].values()))
            legacy = root / "legacy-state"
            legacy.mkdir()
            pointer = root / "active-post-office.json"
            result = execute_cutover(
                database, credential, PLUGIN_ROOT, dossier_id=dossier["dossierId"],
                dossier_root=dossier["dossierRoot"], legacy_state_root=legacy,
                pointer_output=pointer, action_id="PON-ACTION-CUTOVER",
            )
            self.assertEqual(result["state"], "COMMITTED")
            self.assertEqual(json.loads(pointer.read_text(encoding="utf-8"))["state"], "COMMITTED")
            con = sqlite3.connect(database)
            try:
                self.assertEqual(con.execute("SELECT authority_state FROM kernel_instances").fetchone()[0], "AUTHORITATIVE")
                self.assertEqual(con.execute("SELECT state FROM authority_transfers").fetchone()[0], "COMMITTED")
            finally:
                con.close()

    def test_interrupted_pointer_publication_can_finish_exact_prepared_transfer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "post-office-next.sqlite3"
            credential = root / "author.json"
            initialize_database(PLUGIN_ROOT, database)
            create_kernel_credential(credential, "PON-CAPABILITY-AUTHOR")
            bootstrap_kernel(
                database, credential, actor_id="PON-ACTOR-AUTHOR", actor_kind="HUMAN",
                actor_role="author", mode="SHADOW", plugin_root=PLUGIN_ROOT,
            )
            create_kernel_actor(
                database, credential, action_id="PON-ACTION-CREATE-COURIER",
                actor_id="PON-ACTOR-COURIER", actor_kind="COURIER", actor_role="courier",
                plugin_root=PLUGIN_ROOT,
            )
            evidence = {
                "captureRoot": "1" * 64, "deltaRoot": "2" * 64,
                "importRoot": "3" * 64, "replayRoot": "3" * 64,
                "backupRoot": "4" * 64, "restoreRoot": "5" * 64,
                "performanceRoot": "6" * 64,
                "checks": {"allVerified": True, "openWorkRetained": True,
                           "reviewWorkRetained": True, "continuationWorkRetained": True},
            }
            record_cutover_rehearsal(database, credential, PLUGIN_ROOT, evidence=evidence)
            dossier = create_cutover_dossier(
                database, credential, PLUGIN_ROOT, legacy_final_root="7" * 64,
                output=root / "cutover-dossier.json",
            )
            legacy = root / "legacy-state"
            legacy.mkdir()
            pointer = root / "active-post-office.json"
            with patch("post_office.shadow._finish_prepared_transfer", side_effect=RuntimeError("simulated interruption")):
                with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                    execute_cutover(
                        database, credential, PLUGIN_ROOT, dossier_id=dossier["dossierId"],
                        dossier_root=dossier["dossierRoot"], legacy_state_root=legacy,
                        pointer_output=pointer, action_id="PON-ACTION-CUTOVER",
                    )
            con = sqlite3.connect(database)
            try:
                transfer_id, state = con.execute("SELECT transfer_id,state FROM authority_transfers").fetchone()
                self.assertEqual(state, "PREPARED")
                self.assertEqual(con.execute("SELECT authority_state FROM kernel_instances").fetchone()[0], "PREVIEW")
            finally:
                con.close()
            with self.assertRaisesRegex(PostOfficeError, "prepared authority transfer fences"):
                record_shadow_observation(
                    database,
                    credential,
                    PLUGIN_ROOT,
                    source_kind="LIVE_PROJECTION",
                    source_reference="must-be-fenced",
                    expected_root="a" * 64,
                    observed_root="a" * 64,
                    evidence={"bounded": True},
                )
            recovered = finish_prepared_cutover(
                database, credential, PLUGIN_ROOT, transfer_id=transfer_id,
            )
            self.assertTrue(recovered["recovered"])
            self.assertEqual(recovered["state"], "COMMITTED")
            con = sqlite3.connect(database)
            try:
                self.assertEqual(con.execute("SELECT state FROM authority_transfers").fetchone()[0], "COMMITTED")
                self.assertEqual(con.execute("SELECT authority_state FROM kernel_instances").fetchone()[0], "AUTHORITATIVE")
            finally:
                con.close()


if __name__ == "__main__":
    unittest.main()
