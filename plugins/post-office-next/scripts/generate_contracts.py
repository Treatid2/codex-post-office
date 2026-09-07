# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from post_office.contracts import generate_contracts  # noqa: E402


if __name__ == "__main__":
    print(json.dumps(generate_contracts(SCRIPT_DIR.parent), sort_keys=True))
