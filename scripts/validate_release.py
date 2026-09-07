# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PLUGINS = {
    "post-office-next",
    "automatic-code-review",
    "playwright-browser-bridge",
}
REQUIRED_DOCS = {
    "README.md",
    "LICENSE",
    "SECURITY.md",
    "CHANGELOG.md",
    "THIRD_PARTY_NOTICES.md",
    "docs/INSTALLATION.md",
    "docs/MAILBOX-LIFECYCLE.md",
    "docs/AUTOMATIC-REVIEW-BROWSER.md",
    "docs/BROWSER-AND-CODEX-SETTINGS.md",
}
SOURCE_SUFFIXES = {".py", ".ps1", ".mjs", ".sql"}


def fail(errors: list[str], message: str) -> None:
    errors.append(message)


def load_json(path: Path, errors: list[str]) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(errors, f"invalid JSON: {path.relative_to(ROOT)}: {exc}")
        return None


def main() -> int:
    errors: list[str] = []

    for relative in sorted(REQUIRED_DOCS):
        if not (ROOT / relative).is_file():
            fail(errors, f"missing required release file: {relative}")

    marketplace_path = ROOT / ".agents" / "plugins" / "marketplace.json"
    marketplace = load_json(marketplace_path, errors)
    entries = marketplace.get("plugins", []) if isinstance(marketplace, dict) else []
    names = {entry.get("name") for entry in entries if isinstance(entry, dict)}
    if names != EXPECTED_PLUGINS:
        fail(errors, f"marketplace plugin set is {sorted(names)}, expected {sorted(EXPECTED_PLUGINS)}")

    for name in sorted(EXPECTED_PLUGINS):
        plugin_root = ROOT / "plugins" / name
        manifest_path = plugin_root / ".codex-plugin" / "plugin.json"
        manifest = load_json(manifest_path, errors)
        if not isinstance(manifest, dict):
            continue
        if manifest.get("name") != name:
            fail(errors, f"manifest name mismatch for {name}")
        if manifest.get("version") != "0.1.0":
            fail(errors, f"{name} must publish stable preview version 0.1.0")
        if manifest.get("license") != "MPL-2.0":
            fail(errors, f"{name} must declare MPL-2.0")
        repository = manifest.get("repository")
        if repository != "https://github.com/Treatid2/codex-post-office":
            fail(errors, f"{name} has unexpected repository URL")
        skill_path = manifest.get("skills")
        if skill_path and not (plugin_root / str(skill_path)).is_dir():
            fail(errors, f"{name} declares missing skill directory: {skill_path}")

    for path in ROOT.rglob("*.json"):
        if "node_modules" not in path.parts:
            load_json(path, errors)

    forbidden = {
        re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+", re.IGNORECASE): "personal Windows profile path",
        re.compile(re.escape("L:" + "\\Codex"), re.IGNORECASE): "installation-specific storage path",
        re.compile("LicenseRef-" + "Proprietary", re.IGNORECASE): "proprietary license marker",
        re.compile(re.escape("0.1.0" + "+codex"), re.IGNORECASE): "development cachebuster version",
    }
    scan_suffixes = {".md", ".py", ".ps1", ".mjs", ".sql", ".json"}
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in scan_suffixes or ".git" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern, label in forbidden.items():
            if pattern.search(text):
                fail(errors, f"{path.relative_to(ROOT)} contains {label}")

    for path in ROOT.joinpath("plugins").rglob("*"):
        if not path.is_file() or path.suffix.lower() not in SOURCE_SUFFIXES:
            continue
        head = "\n".join(path.read_text(encoding="utf-8").splitlines()[:6])
        if "SPDX-License-Identifier: MPL-2.0" not in head:
            fail(errors, f"missing MPL-2.0 SPDX header: {path.relative_to(ROOT)}")

    package = load_json(ROOT / "plugins" / "playwright-browser-bridge" / "package.json", errors)
    if isinstance(package, dict) and package.get("license") != "MPL-2.0":
        fail(errors, "browser bridge package.json must declare MPL-2.0")

    if errors:
        for error in sorted(set(errors)):
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Codex Post Office release validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
