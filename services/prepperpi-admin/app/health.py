"""System-health helpers for the admin Storage panel.

Read-only views over /proc, /sys, and a couple of files our own
services maintain. Pure parsers are factored out of I/O so they
can be unit-tested without a Linux box (the unit tests just feed
strings through `parse_*` and assert on the result).

CPU % needs a delta between successive samples; we keep the last
sample in a module-level dict. With uvicorn running a single
worker (the default for our service), one shared dict is fine.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

# ---------- /proc/meminfo ----------

def parse_meminfo(text: str) -> dict:
    """Return {total, available, free} in bytes. MemAvailable is what
    'real' free memory looks like to userspace (free + buffers/cache
    that can be reclaimed); we surface that as the headline number."""
    fields: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        rest = rest.strip()
        if not rest:
            continue
        # Lines look like "MemTotal:        7822336 kB"
        parts = rest.split()
        try:
            value_kb = int(parts[0])
        except (ValueError, IndexError):
            continue
        # Everything in /proc/meminfo is kB; convert to bytes.
        fields[key] = value_kb * 1024
    total = fields.get("MemTotal", 0)
    available = fields.get("MemAvailable", fields.get("MemFree", 0))
    used = max(total - available, 0)
    percent = (used / total * 100.0) if total else 0.0
    return {
        "total_bytes": total,
        "available_bytes": available,
        "used_bytes": used,
        "percent": round(percent, 1),
    }


# ---------- /proc/stat (CPU %) ----------

def parse_cpu_total(text: str) -> Optional[tuple[int, int]]:
    """Return (idle, total) jiffies from the aggregate `cpu` line, or
    None if the line can't be parsed.

    /proc/stat first line:
        cpu  user nice system idle iowait irq softirq steal guest guest_nice
    Total = sum of all fields. Idle = idle + iowait."""
    for line in text.splitlines():
        if not line.startswith("cpu "):
            continue
        parts = line.split()
        try:
            values = [int(x) for x in parts[1:]]
        except ValueError:
            return None
        if len(values) < 4:
            return None
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return idle, sum(values)
    return None


def cpu_percent_from_samples(prev: tuple[int, int], now: tuple[int, int]) -> float:
    """Delta-based CPU %. Returns 0.0 if the totals didn't move (would
    be a divide by zero) or went backwards (counter wrap)."""
    prev_idle, prev_total = prev
    now_idle, now_total = now
    total_delta = now_total - prev_total
    idle_delta = now_idle - prev_idle
    if total_delta <= 0:
        return 0.0
    busy = max(total_delta - idle_delta, 0)
    return round(busy / total_delta * 100.0, 1)


# ---------- /proc/uptime ----------

def parse_uptime(text: str) -> int:
    """Return uptime in whole seconds. /proc/uptime is two floats."""
    try:
        return int(float(text.split()[0]))
    except (ValueError, IndexError):
        return 0


# ---------- /sys/class/thermal ----------

def parse_thermal_millideg(text: str) -> Optional[float]:
    """thermal_zone*/temp is millidegree C as an integer string."""
    try:
        return int(text.strip()) / 1000.0
    except (ValueError, AttributeError):
        return None


# ---------- dnsmasq leases ----------

def parse_dnsmasq_leases(text: str, subnet_prefix: str = "10.42.0.") -> int:
    """Count active leases on the AP subnet. dnsmasq.leases lines look like:
        <expiry> <mac> <ip> <hostname> <client-id>
    We only count rows whose IP starts with the AP subnet prefix; that
    excludes any leases dnsmasq has handed out on other interfaces (we
    don't currently, but the filter is cheap and futureproof)."""
    count = 0
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        if parts[2].startswith(subnet_prefix):
            count += 1
    return count


# ---------- I/O wrappers (the parts the tests skip) ----------

_LAST_CPU_SAMPLE: dict[str, tuple[int, int]] = {}

PROC_STAT = Path("/proc/stat")
PROC_MEMINFO = Path("/proc/meminfo")
PROC_UPTIME = Path("/proc/uptime")
THERMAL_PATH = Path("/sys/class/thermal/thermal_zone0/temp")
DNSMASQ_LEASES = Path("/var/lib/misc/dnsmasq.leases")


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text()
    except OSError:
        return None


def cpu_percent() -> float:
    text = _read(PROC_STAT)
    if text is None:
        return 0.0
    sample = parse_cpu_total(text)
    if sample is None:
        return 0.0
    prev = _LAST_CPU_SAMPLE.get("cpu")
    _LAST_CPU_SAMPLE["cpu"] = sample
    if prev is None:
        return 0.0
    return cpu_percent_from_samples(prev, sample)


def memory() -> dict:
    text = _read(PROC_MEMINFO)
    return parse_meminfo(text or "")


def uptime_seconds() -> int:
    text = _read(PROC_UPTIME)
    return parse_uptime(text or "")


def temp_celsius() -> Optional[float]:
    text = _read(THERMAL_PATH)
    if text is None:
        return None
    return parse_thermal_millideg(text)


def connected_clients() -> int:
    text = _read(DNSMASQ_LEASES)
    return parse_dnsmasq_leases(text or "")


# ---------- disk usage ----------

def _is_real_mount(path: str, source: str) -> bool:
    """Filter /proc/mounts down to mounts the user cares about: skip
    pseudo-filesystems, container overlays, and the /tmp /var/tmp bind
    mounts that systemd's PrivateTmp= injects into our sandbox (those
    show up as the same backing device as /, which is just noise on the
    storage page)."""
    if path.startswith(("/proc", "/sys", "/dev", "/run",
                        "/tmp", "/var/tmp", "/var/lib/docker")):
        return False
    if source.startswith(("tmpfs", "devtmpfs", "overlay", "cgroup")):
        return False
    return True


def disks() -> list[dict]:
    """Return one entry per real mount: {mount, device, total, used,
    free, percent, low_space}."""
    results: list[dict] = []
    seen = set()
    # /proc/1/mounts is PID 1's (the host's) view of mounts. /proc/mounts
    # is the calling process's view, which inside our private mount
    # namespace doesn't reflect rw remounts done in the host (slave
    # propagation only carries new mounts, not option changes). Reading
    # PID 1's table sidesteps that and shows actual host writability.
    text = _read(Path("/proc/1/mounts"))
    if text is None:
        return results
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        source, mount, _fstype = parts[0], parts[1], parts[2]
        if not _is_real_mount(mount, source):
            continue
        if mount in seen:
            continue
        seen.add(mount)
        try:
            stats = os.statvfs(mount)
        except OSError:
            continue
        total = stats.f_blocks * stats.f_frsize
        free = stats.f_bavail * stats.f_frsize
        used = total - (stats.f_bfree * stats.f_frsize)
        if total == 0:
            continue
        percent = used / total * 100.0
        results.append({
            "mount": mount,
            "device": source,
            "total_bytes": total,
            "used_bytes": used,
            "free_bytes": free,
            "percent": round(percent, 1),
            "low_space": (free / total) < 0.05,
        })
    return results


# ---------- USB drive snapshot (for the toggle UI) ----------

USB_BASE = Path("/srv/prepperpi/user-usb")


def usb_drives() -> list[dict]:
    """Each entry: {name, path, writable, total, used, free, percent}.
    `name` is the directory name under /srv/prepperpi/user-usb/, which
    is the sanitized label that prepperpi-usb-mount.sh chose."""
    results: list[dict] = []
    if not USB_BASE.is_dir():
        return results
    # /proc/1/mounts is PID 1's (the host's) view of mounts. /proc/mounts
    # is the calling process's view, which inside our private mount
    # namespace doesn't reflect rw remounts done in the host (slave
    # propagation only carries new mounts, not option changes). Reading
    # PID 1's table sidesteps that and shows actual host writability.
    text = _read(Path("/proc/1/mounts"))
    rw_state: dict[str, bool] = {}
    if text is not None:
        for line in text.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            mount, options = parts[1], parts[3]
            if mount.startswith(str(USB_BASE) + "/"):
                rw_state[mount] = "rw" in options.split(",")
    try:
        children = sorted(USB_BASE.iterdir())
    except OSError:
        return results
    for child in children:
        if not child.is_dir():
            continue
        mount = str(child)
        try:
            stats = os.statvfs(mount)
        except OSError:
            continue
        total = stats.f_blocks * stats.f_frsize
        free = stats.f_bavail * stats.f_frsize
        used = total - (stats.f_bfree * stats.f_frsize)
        if total == 0:
            # Probably an empty placeholder dir (drive was just unplugged).
            continue
        percent = used / total * 100.0
        results.append({
            "name": child.name,
            "path": mount,
            "writable": rw_state.get(mount, False),
            "total_bytes": total,
            "used_bytes": used,
            "free_bytes": free,
            "percent": round(percent, 1),
        })
    return results


# ---------- top-level snapshot ----------

TEMP_WARN_C = 80.0


def snapshot() -> dict:
    """Single dict consumed by both the request-time render and the
    /admin/health JSON endpoint."""
    temp = temp_celsius()
    return {
        "cpu_percent": cpu_percent(),
        "memory": memory(),
        "uptime_seconds": uptime_seconds(),
        "temp_celsius": temp,
        "temp_warn": (temp is not None and temp >= TEMP_WARN_C),
        "clients": connected_clients(),
        "disks": disks(),
        "usb_drives": usb_drives(),
    }


def format_uptime(seconds: int) -> str:
    """Human-readable uptime: '3d 4h 12m' or '4h 12m' or '12m'."""
    if seconds < 60:
        return f"{seconds}s"
    minutes, _ = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_bytes(n: int) -> str:
    """1.2 GB / 340 MB / etc. Decimal SI units (matches what disk
    vendors print on the box)."""
    units = ["B", "kB", "MB", "GB", "TB"]
    value = float(n)
    for unit in units:
        if value < 1000 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1000
    return f"{value:.1f} {units[-1]}"  # pragma: no cover
