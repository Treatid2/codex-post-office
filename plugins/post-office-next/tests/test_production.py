# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from post_office.database import initialize_database, inspect_database
from post_office.production import prepare_production_root, production_status


class ProductionPreparationTests(unittest.TestCase):
    def test_prepare_copies_before_bootstrap_and_excludes_secrets_from_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            imported = root / "imported"
            imported.mkdir()
            source_database = imported / "post-office-next.sqlite3"
            initialize_database(PLUGIN_ROOT, source_database)
            source_root = inspect_database(source_database, PLUGIN_ROOT)["logicalStateRoot"]
            prepared = root / "prepared"
            receipt_path = root / "receipt.json"
            result = prepare_production_root(imported, prepared, receipt_path, PLUGIN_ROOT)
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertTrue(result["ok"])
            self.assertEqual(source_root, receipt["sourceLogicalStateRoot"])
            self.assertEqual(source_root, receipt["copiedLogicalStateRoot"])
            self.assertEqual(0, receipt["taskCredentialCount"])
            self.assertTrue(receipt["secretsExcluded"])
            self.assertNotIn('"secret":', json.dumps(receipt))
            self.assertTrue((prepared / "caller-secrets" / "author.token").is_file())
            self.assertTrue((prepared / "caller-secrets" / "courier.token").is_file())
            status = production_status(prepared / "post-office-next.sqlite3", PLUGIN_ROOT)
            self.assertEqual(status["kernel"]["authority_state"], "PREVIEW")
            self.assertEqual(status["backupReceiptCount"], 0)
            self.assertEqual(status["migrationEvidence"]["actionableOpenCount"], 0)
            self.assertEqual(status["migrationEvidence"]["historicalOpenCount"], 0)
            self.assertFalse(status["secretsIncluded"])
            self.assertNotIn('"secret"', json.dumps(status))


if __name__ == "__main__":
    unittest.main()
