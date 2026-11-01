"""Termux-aware system information with psutil and /proc fallbacks.

On Android, psutil frequently cannot read /proc/stat or /proc/meminfo (SELinux
restrictions on Android 10+), so every metric has a fallback and failures
degrade to "n/a" rather than raising.
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass, field

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover - psutil may be missing or broken on Android
    psutil = None


def _run(argv: list[str], timeout: float = 4.0) -> str:
    if not shutil.which(argv[0]):
        return ""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return r.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def _prop(name: str) -> str:
    return _run(["getprop", name])


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return "?"


@dataclass
class SystemSnapshot:
    device: str = "n/a"
    android: str = "n/a"
    kernel: str = "n/a"
    arch: str = "n/a"
    cpu_model: str = "n/a"
    cpu_cores: int = 0
    cpu_percent: float | None = None
    ram_used: str = "n/a"
    ram_total: str = "n/a"
    ram_available: str = "n/a"
    ram_percent: float | None = None
    storage_used: str = "n/a"
    storage_total: str = "n/a"
    storage_percent: float | None = None
    battery_percent: int | None = None
    battery_status: str = "n/a"
    battery_temp: float | None = None
    network: str = "n/a"
    termux_version: str = "n/a"
    storage_permission: bool = False
    api_installed: bool = False
    extras: dict = field(default_factory=dict)

    def summary(self) -> str:
        """Plain-text summary suitable for passing to the LLM."""
        def pct(v):
            return f"{v:.0f}%" if v is not None else "n/a"
        batt = f"{self.battery_percent}% ({self.battery_status})" if self.battery_percent is not None else "n/a"
        return (
            f"Device: {self.device} | Android {self.android} | Kernel {self.kernel} | {self.arch}\n"
            f"CPU: {self.cpu_model} ({self.cpu_cores} cores), usage {pct(self.cpu_percent)}\n"
            f"RAM: {self.ram_used}/{self.ram_total} ({pct(self.ram_percent)}), available {self.ram_available}\n"
            f"Storage: {self.storage_used}/{self.storage_total} ({pct(self.storage_percent)})\n"
            f"Battery: {batt}\nNetwork: {self.network}\nTermux: {self.termux_version}"
        )


# ------------------------------------------------------------------ pieces
def _cpu_info() -> tuple[str, int]:
    model, cores = "", os.cpu_count() or 0
    try:
        with open("/proc/cpuinfo") as f:
            text = f.read()
        for key in ("Hardware", "model name", "Processor"):
            m = re.search(rf"^{key}\s*:\s*(.+)$", text, re.M)
            if m:
                model = m.group(1).strip()
                break
    except OSError:
        pass
    if not model:
        model = _prop("ro.soc.model") or _prop("ro.board.platform") or _prop("ro.hardware") or platform.processor() or "n/a"
    return model, cores


def _cpu_percent() -> float | None:
    if psutil:
        try:
            v = psutil.cpu_percent(interval=None)
            return float(v)
        except Exception:
            pass
    # fallback: load average relative to core count
    try:
        load1 = os.getloadavg()[0]
        return min(100.0, load1 / max(os.cpu_count() or 1, 1) * 100)
    except OSError:
        return None


def _memory() -> tuple[int, int, int] | None:
    """Return (total, available, used) in bytes."""
    if psutil:
        try:
            vm = psutil.virtual_memory()
            return vm.total, vm.available, vm.total - vm.available
        except Exception:
            pass
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                info[k] = int(v.split()[0]) * 1024
        total = info["MemTotal"]
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        return total, avail, total - avail
    except (OSError, KeyError, ValueError):
        pass
    out = _run(["free", "-b"])
    m = re.search(r"Mem:\s+(\d+)\s+(\d+)\s+(\d+)(?:\s+\d+\s+\d+\s+(\d+))?", out)
    if m:
        total, used = int(m.group(1)), int(m.group(2))
        avail = int(m.group(4)) if m.group(4) else int(m.group(3))
        return total, avail, total - avail
    return None


def _storage(path: str | None = None) -> tuple[int, int] | None:
    target = path or os.environ.get("PREFIX", "/") or "/"
    for candidate in (target, os.path.expanduser("~"), "/"):
        try:
            u = shutil.disk_usage(candidate)
            return u.total, u.used
        except OSError:
            continue
    return None


def _battery() -> tuple[int | None, str, float | None]:
    out = _run(["termux-battery-status"], timeout=5)
    if out:
        try:
            d = json.loads(out)
            return int(d.get("percentage")), str(d.get("status", "n/a")).title(), d.get("temperature")
        except (ValueError, TypeError):
            pass
    if psutil:
        try:
            b = psutil.sensors_battery()
            if b:
                return int(b.percent), "Charging" if b.power_plugged else "Discharging", None
        except Exception:
            pass
    for base in ("/sys/class/power_supply/battery", "/sys/class/power_supply/BAT0"):
        try:
            cap = int(open(f"{base}/capacity").read().strip())
            st = open(f"{base}/status").read().strip()
            return cap, st, None
        except (OSError, ValueError):
            continue
    return None, "n/a", None


def _network() -> str:
    out = _run(["termux-wifi-connectioninfo"], timeout=5)
    if out:
        try:
            d = json.loads(out)
            ssid = d.get("ssid", "").strip('"')
            if d.get("supplicant_state") == "COMPLETED" or ssid not in ("", "<unknown ssid>"):
                return f"Wi-Fi: {ssid or 'connected'} ({d.get('ip', '?')})"
        except ValueError:
            pass
    if psutil:
        try:
            stats = psutil.net_if_stats()
            up = [n for n, s in stats.items() if s.isup and n != "lo"]
            if up:
                return "Online via " + ", ".join(up[:3])
        except Exception:
            pass
    out = _run(["ip", "-brief", "addr"])
    up = [l.split()[0] for l in out.splitlines() if " UP " in f" {l} " and not l.startswith("lo")]
    if up:
        return "Online via " + ", ".join(up[:3])
    return "Offline / unknown"


def _termux_version() -> str:
    v = os.environ.get("TERMUX_VERSION")
    if v:
        return v
    out = _run(["termux-info"])
    m = re.search(r"TERMUX_VERSION=(\S+)", out)
    return m.group(1) if m else ("not Termux" if "com.termux" not in os.environ.get("PREFIX", "") else "n/a")


def storage_permission_granted() -> bool:
    shared = os.path.expanduser("~/storage")
    if os.path.isdir(shared):
        return True
    try:
        return bool(os.listdir("/sdcard"))
    except OSError:
        return False


# --------------------------------------------------------------- snapshot
def static_info() -> dict:
    """Values that don't change during a session."""
    model, cores = _cpu_info()
    brand, dev = _prop("ro.product.brand"), _prop("ro.product.model")
    return {
        "device": f"{brand} {dev}".strip().title() if dev else platform.node() or "n/a",
        "android": _prop("ro.build.version.release") or "n/a",
        "kernel": platform.release() or "n/a",
        "arch": platform.machine() or "n/a",
        "cpu_model": model,
        "cpu_cores": cores,
        "termux_version": _termux_version(),
        "storage_permission": storage_permission_granted(),
        "api_installed": bool(shutil.which("termux-battery-status")),
    }


def snapshot(static: dict | None = None) -> SystemSnapshot:
    s = SystemSnapshot(**(static or static_info()))
    s.cpu_percent = _cpu_percent()
    mem = _memory()
    if mem:
        total, avail, used = mem
        s.ram_total, s.ram_available, s.ram_used = _fmt_bytes(total), _fmt_bytes(avail), _fmt_bytes(used)
        s.ram_percent = used / total * 100 if total else None
    st = _storage()
    if st:
        total, used = st
        s.storage_total, s.storage_used = _fmt_bytes(total), _fmt_bytes(used)
        s.storage_percent = used / total * 100 if total else None
    s.battery_percent, s.battery_status, s.battery_temp = _battery()
    s.network = _network()
    return s


async def snapshot_async(static: dict | None = None) -> SystemSnapshot:
    """Run the (blocking, subprocess-heavy) snapshot off the event loop."""
    return await asyncio.to_thread(snapshot, static)


def top_processes(limit: int = 8) -> list[tuple[int, str, float, float]]:
    """(pid, name, cpu%, mem%) - psutil first, `ps` as fallback."""
    rows = []
    if psutil:
        try:
            for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
                i = p.info
                rows.append((i["pid"], i["name"] or "?", i["cpu_percent"] or 0.0, i["memory_percent"] or 0.0))
        except Exception:
            rows = []
    if not rows:
        out = _run(["ps", "-eo", "pid,comm,pcpu,pmem"]) or _run(["ps"])
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 4:
                try:
                    rows.append((int(parts[0]), parts[1], float(parts[2]), float(parts[3])))
                except ValueError:
                    continue
    rows.sort(key=lambda r: (r[3], r[2]), reverse=True)
    return rows[:limit]


def cache_candidates() -> list[tuple[str, int]]:
    """Safe-to-clean cache locations and their sizes (read-only scan)."""
    prefix = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
    home = os.path.expanduser("~")
    paths = [
        os.path.join(home, ".cache"),
        os.path.join(prefix, "var", "cache", "apt", "archives"),
        os.path.join(home, ".npm", "_cacache"),
        os.path.join(home, ".cache", "pip"),
    ]
    out = []
    for p in dict.fromkeys(paths):
        if not os.path.isdir(p):
            continue
        total = 0
        for root, _dirs, files in os.walk(p, onerror=lambda e: None):
            for fn in files:
                try:
                    total += os.lstat(os.path.join(root, fn)).st_size
                except OSError:
                    pass
        out.append((p, total))
    return out


def env_for_llm() -> dict[str, str]:
    """Environment variables safe to show the LLM (secrets filtered out)."""
    from .security import safe_environ, redact
    keep = ("HOME", "PREFIX", "SHELL", "LANG", "PATH", "TERM", "USER", "TERMUX_VERSION", "PWD")
    env = safe_environ()
    return {k: redact(f"{k}={env[k]}").split("=", 1)[1] for k in keep if k in env}
