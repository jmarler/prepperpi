"""config_io — pure builders/parsers for the v1 config export tarball.

The export bundle is a single-file gzipped tarball containing
`manifest.json`. We keep the build/parse logic pure (no filesystem,
no system calls) so it can be unit-tested without a live appliance.
The route handlers in main.py do the I/O — read the live network
conf, read the installed-bundles registry, write the response, etc.

Schema (v1):
  {
    "schema_version": 1,
    "created_at": "2026-04-27T12:34:56Z",
    "host": "prepperpi-abcd",
    "network": {
      "ssid": "...",
      "wifi_password": "...",
      "channel": "auto" | 1..13 (int or str),
      "country": "US"
    },
    "bundles": ["official:starter", ...]
  }

Per-field semantic validation (SSID character class, channel 1-13,
FCC channel ranges, etc.) lives in main.validate_locally and the
privileged `apply-network-config` worker. parse_tarball only checks
the wrapping shape so callers can produce the same field-specific
error messages users already see in the network form.
"""
from __future__ import annotations

import io
import json
import tarfile
import time

CURRENT_SCHEMA_VERSION = 1
MANIFEST_NAME = "manifest.json"


class ConfigIOError(Exception):
    """Anything wrong with the import payload — bad tarball, bad JSON,
    schema mismatch, or shape errors. The caller surfaces .args[0] as
    the flash message, so phrasing should be user-readable."""


def build_manifest(*, network: dict, bundles: list[str], host: str,
                   now: str | None = None) -> dict:
    """Pure: assemble the v1 manifest dict. `now` lets tests pin the
    timestamp for golden-file comparisons."""
    if now is None:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "created_at": now,
        "host": host,
        "network": {
            "ssid": str(network.get("ssid", "")),
            "wifi_password": str(network.get("wifi_password", "")),
            "channel": network.get("channel", "auto"),
            "country": str(network.get("country", "US")),
        },
        "bundles": list(bundles),
    }


def manifest_to_tarball_bytes(manifest: dict, *, mtime: int | None = None) -> bytes:
    """Pure: render the manifest as a single-file gzipped tarball.
    `mtime` is overridable so tests can produce byte-identical output."""
    body = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    if mtime is None:
        mtime = int(time.time())
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name=MANIFEST_NAME)
        info.size = len(body)
        info.mtime = mtime
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def parse_tarball(blob: bytes) -> dict:
    """Pure: extract `manifest.json` from a gzipped tarball, parse, and
    validate the wrapping shape. Raises ConfigIOError with a
    user-readable message on any failure.

    Validation enforces:
      - Tarball is openable as gzipped tar.
      - manifest.json exists and is a regular file.
      - manifest.json is valid UTF-8 JSON producing an object.
      - schema_version is an int and <= CURRENT_SCHEMA_VERSION.
      - 'network' is an object; 'bundles' is a list of strings.

    Per-field semantic validation (SSID character class, channel range,
    country/FCC compatibility) is left to the apply path so users see
    the same field-specific errors they get in the network form."""
    try:
        tar = tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz")
    except (tarfile.TarError, OSError) as exc:
        raise ConfigIOError(f"Not a valid .tar.gz: {exc}") from exc
    try:
        try:
            member = tar.getmember(MANIFEST_NAME)
        except KeyError:
            raise ConfigIOError(f"Archive is missing {MANIFEST_NAME}.")
        if not member.isfile():
            raise ConfigIOError(f"{MANIFEST_NAME} is not a regular file.")
        fh = tar.extractfile(member)
        if fh is None:
            raise ConfigIOError(f"{MANIFEST_NAME} could not be read.")
        try:
            text = fh.read().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConfigIOError(f"{MANIFEST_NAME} is not UTF-8: {exc}") from exc
    finally:
        tar.close()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigIOError(f"{MANIFEST_NAME} is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigIOError(f"{MANIFEST_NAME} must be a JSON object.")

    sv = data.get("schema_version")
    if not isinstance(sv, int):
        raise ConfigIOError(f"{MANIFEST_NAME} is missing schema_version.")
    if sv > CURRENT_SCHEMA_VERSION:
        raise ConfigIOError(
            f"This export uses schema_version={sv}, but this PrepperPi "
            f"only understands schema_version<={CURRENT_SCHEMA_VERSION}. "
            "Upgrade PrepperPi before importing."
        )

    network = data.get("network")
    if not isinstance(network, dict):
        raise ConfigIOError(f"{MANIFEST_NAME} is missing the 'network' object.")
    bundles = data.get("bundles")
    if not isinstance(bundles, list) or not all(isinstance(b, str) for b in bundles):
        raise ConfigIOError(
            f"{MANIFEST_NAME} 'bundles' must be a list of strings."
        )

    return data
