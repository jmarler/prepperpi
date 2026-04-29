"""Reader for `/etc/prepperpi/image.version`, baked into every image.

The file is a tiny `key=value` text format written by the pi-gen stage
(images/stage-prepperpi/00-install-prepperpi/00-run.sh). Keys today:
`image_version`, `git_commit`, `pigen_rev`, `built_at`. Future keys are
ignored by `parse_image_version` so the parser stays forward-compatible.

Pure parser is separated from the I/O wrapper so it's testable without a
file on disk.
"""
from __future__ import annotations

from pathlib import Path

IMAGE_VERSION_PATH = Path("/etc/prepperpi/image.version")


def parse_image_version(text: str) -> dict[str, str]:
    """Parse the `key=value` lines of /etc/prepperpi/image.version.

    Blank lines and lines without `=` are skipped. Values are returned
    verbatim (no whitespace stripping beyond newline removal) so an
    operator who edits the file manually doesn't get silently
    canonicalized output.
    """
    result: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            result[key] = value
    return result


def read_image_version(path: Path = IMAGE_VERSION_PATH) -> dict[str, str]:
    """Read the on-disk image.version. Returns {} if missing/unreadable.

    A missing file is the expected case during host-side dev (running
    the admin app from a checkout), so swallowing the error is correct.
    """
    try:
        return parse_image_version(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, PermissionError, UnicodeDecodeError):
        return {}
