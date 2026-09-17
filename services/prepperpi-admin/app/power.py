"""Power page helpers — read-only view of the physical-button state.

The admin daemon only *reads* /boot/firmware/config.txt here; every
write goes through `sudo -n apply-power-action`. The constants below
are deliberately duplicated from that worker rather than imported:
the worker runs as root and must not import anything from the app
tree, on the same principle that makes `validate_locally()` in main.py
a mirror of apply-network-config's validation rather than a shared
module. `tests/unit/test_admin_power.py` asserts the two copies stay
in sync, so the duplication can't silently drift.
"""
from __future__ import annotations

from pathlib import Path

CONFIG_TXT_CANDIDATES = (
    Path("/boot/firmware/config.txt"),
    Path("/boot/config.txt"),
)

BLOCK_BEGIN = "# >>> prepperpi power button (managed) >>>"
BLOCK_END = "# <<< prepperpi power button (managed) <<<"

ALLOWED_GPIOS = (3, 4, 5, 6, 12, 13, 16, 17, 22, 23, 24, 25, 26, 27)
DEFAULT_GPIO = 3

# Header pin numbers, for the "wire it here" hint in the UI. Only the
# pins we offer; GPIO -> physical pin on the 40-pin header.
GPIO_TO_HEADER_PIN = {
    3: 5, 4: 7, 5: 29, 6: 31, 12: 32, 13: 33, 16: 36,
    17: 11, 22: 15, 23: 16, 24: 18, 25: 22, 26: 37, 27: 13,
}


def config_txt_path() -> Path | None:
    for candidate in CONFIG_TXT_CANDIDATES:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def parse_block(text: str) -> dict:
    """Extract the managed block's state from config.txt contents.

    Returns {"enabled": bool, "gpio": int}. A block whose dtoverlay
    line we can't parse reads as disabled — the UI then offers to
    (re)write it, which is the recoverable outcome. We deliberately
    do NOT look at dtoverlay=gpio-shutdown lines outside our block:
    those are the operator's own, and reporting them as "enabled"
    would invite the admin console to overwrite hand-rolled config.
    """
    state = {"enabled": False, "gpio": DEFAULT_GPIO}
    inside = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == BLOCK_BEGIN:
            inside = True
            continue
        if stripped == BLOCK_END:
            inside = False
            continue
        if not inside or not stripped.startswith("dtoverlay=gpio-shutdown"):
            continue
        state["enabled"] = True
        for param in stripped.split(",")[1:]:
            key, _, value = param.partition("=")
            if key.strip() == "gpio_pin":
                try:
                    state["gpio"] = int(value.strip())
                except ValueError:
                    pass
    return state


def read_button_state() -> dict:
    """Current physical-button configuration, plus whether it's live.

    `pending_reboot` is the honest bit of this page: config.txt is read
    by the firmware at boot, so a change made here does nothing until
    the Pi restarts. We can't diff against the running overlay without
    parsing the device tree, so we report the file state and say plainly
    that it applies on next boot.
    """
    path = config_txt_path()
    if path is None:
        return {
            "enabled": False,
            "gpio": DEFAULT_GPIO,
            "available": False,
            "config_path": None,
        }
    try:
        text = path.read_text(encoding="utf-8", errors="surrogateescape")
    except OSError:
        return {
            "enabled": False,
            "gpio": DEFAULT_GPIO,
            "available": False,
            "config_path": str(path),
        }
    state = parse_block(text)
    state["available"] = True
    state["config_path"] = str(path)
    return state


def header_pin(gpio: int) -> int | None:
    return GPIO_TO_HEADER_PIN.get(gpio)


def gpio_choices() -> list[dict]:
    """(gpio, header pin, note) triples for the select box."""
    out = []
    for gpio in ALLOWED_GPIOS:
        note = ""
        if gpio == DEFAULT_GPIO:
            note = " — also wakes the Pi from off"
        out.append({
            "gpio": gpio,
            "pin": GPIO_TO_HEADER_PIN[gpio],
            "label": f"GPIO{gpio} (header pin {GPIO_TO_HEADER_PIN[gpio]}){note}",
        })
    return out
