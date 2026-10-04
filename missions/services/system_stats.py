"""Sistem stats — CPU/RAM/disk/temp/voltage/throttled za nadzorno ploščo.

Bere podatke iz ``/proc`` in ``/sys`` (portabilno Linux) ter ``vcgencmd``
(RPi-specific). Vse vrne v enem JSON dict-u.

Posebej pomembno za UAV stack: ``throttled`` bitmask iz ``vcgencmd
get_throttled``, ki pove, ali je bila brownout zaščita aktivirana
(zgodovina ali zdaj). To je glavni signal, da je napajalnik prešibek.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Any, Optional


# ---------------------------------------------------------------------------
# CPU delta tracking
# ---------------------------------------------------------------------------
class _CpuState:
    """Drzi prejsnje branje /proc/stat za izracun delta %."""
    def __init__(self) -> None:
        self.idle = 0
        self.total = 0
        self.ts = 0.0
        self.last_pct = 0.0

    def read(self) -> float:
        try:
            with open("/proc/stat") as f:
                fields = f.readline().split()
            # cpu user nice system idle iowait irq softirq steal guest guest_nice
            vals = [int(x) for x in fields[1:8]]
            idle = vals[3] + vals[4]
            total = sum(vals)
            if self.total != 0 and total > self.total:
                dt_idle = idle - self.idle
                dt_total = total - self.total
                pct = 100.0 * (1.0 - dt_idle / dt_total) if dt_total else 0.0
                self.last_pct = max(0.0, min(100.0, pct))
            self.idle = idle
            self.total = total
            self.ts = time.time()
        except Exception:
            pass
        return self.last_pct


_cpu = _CpuState()
_cpu_lock = threading.Lock()


# ---------------------------------------------------------------------------
# /proc parsers
# ---------------------------------------------------------------------------
def _memory() -> dict[str, Any]:
    try:
        with open("/proc/meminfo") as f:
            data = {}
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    key = parts[0].strip()
                    val = parts[1].strip().split()[0]
                    data[key] = int(val) * 1024  # kB → B
        total = data.get("MemTotal", 0)
        available = data.get("MemAvailable", 0)
        used = total - available
        pct = 100.0 * used / total if total else 0.0
        return {
            "total_b": total,
            "used_b": used,
            "available_b": available,
            "percent": round(pct, 1),
        }
    except Exception as exc:
        return {"error": str(exc)}


def _disk(mount: str = "/") -> dict[str, Any]:
    try:
        st = os.statvfs(mount)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - free
        pct = 100.0 * used / total if total else 0.0
        return {
            "mount": mount,
            "total_b": total,
            "used_b": used,
            "free_b": free,
            "percent": round(pct, 1),
        }
    except Exception as exc:
        return {"error": str(exc)}


def _uptime() -> dict[str, Any]:
    try:
        with open("/proc/uptime") as f:
            secs = float(f.read().split()[0])
        return {"seconds": int(secs)}
    except Exception as exc:
        return {"error": str(exc)}


def _loadavg() -> dict[str, Any]:
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
        return {"1m": float(parts[0]), "5m": float(parts[1]), "15m": float(parts[2])}
    except Exception as exc:
        return {"error": str(exc)}


def _temperature() -> Optional[float]:
    """Vrne CPU temp v °C, ali None."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read().strip()) / 1000.0
    except Exception:
        pass
    # Fallback: vcgencmd
    try:
        out = subprocess.check_output(
            ["vcgencmd", "measure_temp"], text=True, timeout=2,
        ).strip()
        # "temp=42.0'C"
        if "=" in out:
            return float(out.split("=")[1].rstrip("'C"))
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# vcgencmd parsers (RPi-specific)
# ---------------------------------------------------------------------------
def _vcgencmd(cmd: str) -> Optional[str]:
    try:
        out = subprocess.check_output(
            ["vcgencmd", *cmd.split()], text=True, timeout=2,
        ).strip()
        return out
    except Exception:
        return None


def _voltage() -> Optional[float]:
    """Core voltage v V, ali None."""
    out = _vcgencmd("measure_volts core")
    if not out or "=" not in out:
        return None
    # "volt=0.8638V"
    try:
        return float(out.split("=")[1].rstrip("V"))
    except Exception:
        return None


# Bit pomeni iz `vcgencmd get_throttled` (RPi firmware)
_THROTTLE_BITS = [
    (0x1,     "under_voltage_now", True),       # bit 0 — pomembno
    (0x2,     "freq_capped_now", True),
    (0x4,     "throttled_now", True),
    (0x8,     "soft_temp_limit_now", True),
    (0x10000, "under_voltage_history", False),  # high bits = zgodovina
    (0x20000, "freq_capped_history", False),
    (0x40000, "throttled_history", False),
    (0x80000, "soft_temp_history", False),
]


def _throttled() -> dict[str, Any]:
    out = _vcgencmd("get_throttled")
    if not out or "=" not in out:
        return {"available": False, "raw": None}
    try:
        raw = int(out.split("=")[1], 16)
    except Exception:
        return {"available": False, "raw": out}
    flags: dict[str, bool] = {}
    severity = "ok"
    for bit, name, is_now in _THROTTLE_BITS:
        flag = bool(raw & bit)
        flags[name] = flag
        if flag:
            if is_now:
                severity = "critical"  # zdaj se dogaja
            elif severity == "ok":
                severity = "warning"   # bilo se je v preteklosti
    return {
        "available": True,
        "raw": f"0x{raw:x}",
        "value": raw,
        "severity": severity,
        "flags": flags,
    }


# ---------------------------------------------------------------------------
# Glavna funkcija — snapshot vsega
# ---------------------------------------------------------------------------
_cache: dict[str, Any] = {}
_cache_ts: float = 0.0
_cache_ttl: float = 1.0  # sekund


def get_system_stats(force: bool = False) -> dict[str, Any]:
    """Vrne sistem stats. Cache-ano za 1 s, ker so vcgencmd klici drazji."""
    global _cache, _cache_ts
    now = time.time()
    if not force and (now - _cache_ts) < _cache_ttl and _cache:
        return _cache

    with _cpu_lock:
        cpu_pct = _cpu.read()

    stats = {
        "ts": now,
        "cpu_percent": round(cpu_pct, 1),
        "memory": _memory(),
        "disk": _disk("/"),
        "temperature_c": _temperature(),
        "voltage_core_v": _voltage(),
        "throttled": _throttled(),
        "uptime": _uptime(),
        "loadavg": _loadavg(),
        "hostname": (
            os.uname().nodename if hasattr(os, "uname")
            else (os.environ.get("COMPUTERNAME") or "")
        ),
    }
    _cache = stats
    _cache_ts = now
    return stats
