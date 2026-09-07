# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from post_office.cli import main  # noqa: E402


class CliBoundaryTests(unittest.TestCase):
    def test_parse_failures_return_exactly_one_json_diagnostic(self) -> None:
        for arguments in ([], ["unknown"], ["database", "backup", "--path", "only-source"]):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                status = main(arguments)
            self.assertEqual(status, 2)
            self.assertEqual(stderr.getvalue(), "")
            lines = stdout.getvalue().splitlines()
            self.assertEqual(len(lines), 1)
            result = json.loads(lines[0])
            self.assertFalse(result["ok"])
            self.assertEqual(result["diagnostic"]["schemaVersion"], "1")
            self.assertEqual(result["diagnostic"]["code"], "PON_INPUT_INVALID")

    def test_help_retains_explicit_human_readable_success_path(self) -> None:
        stdout = io.StringIO()
        with self.assertRaises(SystemExit) as raised, redirect_stdout(stdout):
            main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("Post Office Next P0/P1 sidecar", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
