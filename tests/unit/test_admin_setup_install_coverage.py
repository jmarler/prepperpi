"""Guards against the install-script allowlist drifting from the
on-disk app/ tree.

The v0.99.0-rc.1 admin daemon crashed at startup with
`ModuleNotFoundError: No module named 'version_info'` because
services/prepperpi-admin/setup.sh used a per-file allowlist that
silently dropped any new module. The fix was to switch to glob-
based install. This test asserts that every .py / .html / .css /
.js file that exists under the in-repo app/ tree also matches one
of the install commands in setup.sh — so reverting to a brittle
allowlist (or adding a new subdirectory the installer doesn't
know about) fails CI instead of fresh-flashed Pis.

Run with:
    python3 tests/unit/test_admin_setup_install_coverage.py
"""
from __future__ import annotations

import fnmatch
import sys
import unittest
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[2]
ADMIN_DIR = REPO_DIR / "services" / "prepperpi-admin"
APP_DIR = ADMIN_DIR / "app"
SETUP_SH = ADMIN_DIR / "setup.sh"

SRC_MARKER = "${SRC_DIR}/app/"


def collect_install_patterns(setup_sh: Path) -> list[str]:
    """Return the set of `app/<glob>` patterns covered by setup.sh's
    install commands. Patterns are relative to app/.

    Handles both quoting forms used in the script:
      Form A: "${SRC_DIR}/app/main.py"
      Form B: "${SRC_DIR}/app/"*.py

    The strategy is to strip double-quotes (we have no escaped quotes
    or whitespace inside our install args), split on whitespace, and
    pick out tokens starting with the SRC marker.
    """
    patterns: list[str] = []
    text = setup_sh.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("install "):
            continue
        for tok in stripped.replace('"', "").split():
            if tok.startswith(SRC_MARKER):
                rest = tok[len(SRC_MARKER):]
                if rest:
                    patterns.append(rest)
    return patterns


def in_repo_app_files() -> list[str]:
    """Every .py/.html/.css/.js file that the admin daemon would need
    at runtime, expressed as a path relative to app/."""
    extensions = {".py", ".html", ".css", ".js"}
    files: list[str] = []
    for path in APP_DIR.rglob("*"):
        if not path.is_file():
            continue
        # Skip dev-only artifacts that should never reach the runner.
        if "__pycache__" in path.parts:
            continue
        if path.suffix not in extensions:
            continue
        files.append(str(path.relative_to(APP_DIR)))
    return sorted(files)


class InstallCoverageTests(unittest.TestCase):
    def test_every_app_file_matches_an_install_pattern(self) -> None:
        patterns = collect_install_patterns(SETUP_SH)
        self.assertTrue(
            patterns,
            "setup.sh has no `install ... ${SRC_DIR}/app/...` lines — "
            "is the regex still right?",
        )
        files = in_repo_app_files()
        self.assertTrue(files, "no app/ files found — repo layout changed?")
        # fnmatch handles bash-style * globs.
        uncovered = [
            rel for rel in files
            if not any(fnmatch.fnmatchcase(rel, p) for p in patterns)
        ]
        self.assertFalse(
            uncovered,
            "setup.sh would not install these app/ files on a fresh Pi:\n  "
            + "\n  ".join(uncovered)
            + "\nadd a glob to install_files() in setup.sh.",
        )


if __name__ == "__main__":
    unittest.main()
