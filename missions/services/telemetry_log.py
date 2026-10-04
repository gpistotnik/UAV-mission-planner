"""Zapisovanje telemetrije na disk (JSONL).

Snapshot v :mod:`missions.services.mavlink_bridge` je namenjen *zivemu*
prikazu v brskalniku in hrani samo zadnjo znano vrednost. Za validacijsko
kampanjo iz prijave teme (natancnost sledenja trajektoriji, latenca,
zanesljivost, tocnost zajema) pa so potrebni **surovi podatki celega
leta**. Ta modul jih zapise v obliki, ki je berljiva brez posebnih orodij.

Struktura mape ene seje::

    <UAV_LOG_DIR>/20260725-181203_misija-3/
        meta.json         # kdaj, kateri port, katera misija, statistika
        plan.json         # nacrtovana misija (za primerjavo v analizi)
        telemetry.jsonl   # ena vrstica na MAVLink sporocilo
        captures.jsonl    # (opcijsko) zapisnik zajema kamere
        images/           # JPEG-i z EXIF georeferenco

Vsaka vrstica ``telemetry.jsonl`` je samostojen JSON objekt::

    {"t": 1769365923.412, "type": "GLOBAL_POSITION_INT",
     "lat": 461234567, "lon": 145678901, "relative_alt": 50120, ...}

``t`` je UTC epoch sekunda ob **prejemu** sporocila na zemeljski postaji.
Casovni zig posiljatelja (``time_boot_ms``) ostane v polju sporocila, tako
da je mogoce v analizi oceniti tudi zakasnitev povezave.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from core.storage import clear_active_session, publish_active_session

# Tipi sporocil, ki jih zapisujemo. Namenoma **ni** vseh --- npr.
# ``SERVO_OUTPUT_RAW`` in ``RC_CHANNELS`` prideta pri 50 Hz in bi log
# napihnila brez koristi za nase metrike.
DEFAULT_LOGGED_TYPES = frozenset({
    "HEARTBEAT",
    "ATTITUDE",
    "GLOBAL_POSITION_INT",
    "LOCAL_POSITION_NED",
    "GPS_RAW_INT",
    "VFR_HUD",
    "SYS_STATUS",
    "BATTERY_STATUS",
    "SCALED_IMU",
    "RAW_IMU",
    "MAG_CAL_PROGRESS",
    "MAG_CAL_REPORT",

    "NAV_CONTROLLER_OUTPUT",
    "EXTENDED_SYS_STATE",
    "MISSION_CURRENT",
    "MISSION_ITEM_REACHED",
    "MISSION_ACK",
    "COMMAND_ACK",
    "STATUSTEXT",
    "EKF_STATUS_REPORT",
    "HOME_POSITION",
    "CAMERA_TRIGGER",
    "CAMERA_IMAGE_CAPTURED",
    "CAMERA_FEEDBACK",
    "VIBRATION",
    "WIND",
})

# HEARTBEAT pride 1 Hz in je za analizo zanimiv le ob spremembi nacina,
# vendar ga pustimo --- 1 Hz je zanemarljivo.

_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


def _slug(text: str, fallback: str = "seja") -> str:
    s = _SAFE.sub("-", (text or "").strip().lower()).strip("-")
    return s[:48] or fallback


def _sanitize(value: Any) -> Any:
    """Pretvori MAVLink vrednosti v JSON-serializabilne."""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").rstrip("\x00")
    if isinstance(value, (list, tuple)):
        return [_sanitize(v) for v in value]
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    return str(value)


def msg_to_row(msg: Any, ts: Optional[float] = None) -> dict[str, Any]:
    """Pretvori pymavlink sporocilo v vrstico za JSONL."""
    row: dict[str, Any] = {"t": round(ts if ts is not None else time.time(), 3)}
    try:
        row["type"] = msg.get_type()
    except Exception:
        row["type"] = "UNKNOWN"
    fields: Iterable[str]
    try:
        fields = msg.get_fieldnames()
    except Exception:
        fields = ()
    for name in fields:
        try:
            row[name] = _sanitize(getattr(msg, name))
        except Exception:
            continue
    return row


class TelemetryLogger:
    """Zapisovalnik ene seje. Varen za klic iz vec niti."""

    def __init__(
        self,
        base_dir: Path | str,
        logged_types: Optional[frozenset[str]] = None,
        flush_interval_s: float = 2.0,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.logged_types = logged_types or DEFAULT_LOGGED_TYPES
        self.flush_interval_s = flush_interval_s
        self._lock = threading.Lock()
        self._fh = None
        self._dir: Optional[Path] = None
        self._meta: dict[str, Any] = {}
        self._counts: dict[str, int] = {}
        self._total = 0
        self._skipped = 0
        self._last_flush = 0.0

    # -------- stanje --------

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._fh is not None

    @property
    def session_dir(self) -> Optional[Path]:
        return self._dir

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self._fh is None:
                return {"active": False}
            return {
                "active": True,
                "dir": str(self._dir),
                "name": self._dir.name if self._dir else None,
                "started_at": self._meta.get("started_at"),
                "duration_s": round(time.time() - self._meta.get("started_ts", 0), 1),
                "messages": self._total,
                "mission_id": self._meta.get("mission_id"),
                "mission_name": self._meta.get("mission_name"),
                "reason": self._meta.get("reason"),
            }

    # -------- zivljenjski cikel --------

    def start(
        self,
        *,
        mission_id: Optional[int] = None,
        mission_name: Optional[str] = None,
        plan: Optional[dict[str, Any]] = None,
        port: Optional[str] = None,
        baud: Optional[int] = None,
        reason: str = "manual",
        note: str = "",
    ) -> dict[str, Any]:
        """Odpre novo sejo. Ce je ze aktivna, vrne obstojece stanje."""
        with self._lock:
            if self._fh is not None:
                return self.__status_locked()

            now = time.time()
            stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%d-%H%M%S")
            label = _slug(mission_name or "brez-misije")
            d = self.base_dir / f"{stamp}_{label}"
            d.mkdir(parents=True, exist_ok=True)

            self._dir = d
            self._counts = {}
            self._total = 0
            self._skipped = 0
            self._fh = (d / "telemetry.jsonl").open("a", encoding="utf-8")
            self._last_flush = now
            self._meta = {
                "started_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                "started_ts": now,
                "ended_at": None,
                "mission_id": mission_id,
                "mission_name": mission_name,
                "port": port,
                "baud": baud,
                "reason": reason,
                "note": note,
                "logged_types": sorted(self.logged_types),
            }
            if plan is not None:
                (d / "plan.json").write_text(
                    json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
            self.__write_meta_locked()
            try:
                publish_active_session(self.base_dir, d)
            except Exception:
                self._fh.close()
                self._fh = None
                self._dir = None
                raise
            return self.__status_locked()

    def write(self, msg: Any, ts: Optional[float] = None) -> None:
        """Zapise sporocilo, ce je aktivna seja in je tip na seznamu."""
        with self._lock:
            if self._fh is None:
                return
            try:
                mtype = msg.get_type()
            except Exception:
                return
            if mtype not in self.logged_types:
                self._skipped += 1
                return
            row = msg_to_row(msg, ts)
            try:
                self._fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            except Exception:
                return
            self._total += 1
            self._counts[mtype] = self._counts.get(mtype, 0) + 1
            now = time.time()
            if now - self._last_flush >= self.flush_interval_s:
                try:
                    self._fh.flush()
                except Exception:
                    pass
                self._last_flush = now

    def stop(self, reason: str = "manual") -> dict[str, Any]:
        """Zapre sejo in dokonca ``meta.json``."""
        with self._lock:
            if self._fh is None:
                return {"active": False}
            out = self.__status_locked()
            now = time.time()
            self._meta["ended_at"] = datetime.fromtimestamp(
                now, timezone.utc).isoformat()
            self._meta["ended_ts"] = now
            self._meta["duration_s"] = round(now - self._meta.get("started_ts", now), 1)
            self._meta["messages"] = self._total
            self._meta["skipped"] = self._skipped
            self._meta["counts"] = dict(sorted(self._counts.items()))
            self._meta["stop_reason"] = reason
            try:
                self._fh.flush()
                self._fh.close()
            except Exception:
                pass
            self._fh = None
            self.__write_meta_locked()
            if self._dir is not None:
                clear_active_session(self.base_dir, self._dir)
            out["active"] = False
            out["messages"] = self._total
            out["duration_s"] = self._meta["duration_s"]
            return out

    # -------- interno (klicano ze pod lockom) --------

    def __write_meta_locked(self) -> None:
        if self._dir is None:
            return
        try:
            (self._dir / "meta.json").write_text(
                json.dumps(self._meta, indent=2, ensure_ascii=False),
                encoding="utf-8")
        except Exception:
            pass

    def __status_locked(self) -> dict[str, Any]:
        return {
            "active": self._fh is not None,
            "dir": str(self._dir) if self._dir else None,
            "name": self._dir.name if self._dir else None,
            "started_at": self._meta.get("started_at"),
            "messages": self._total,
            "mission_id": self._meta.get("mission_id"),
            "mission_name": self._meta.get("mission_name"),
            "reason": self._meta.get("reason"),
        }


# ---------------------------------------------------------------------------
# Pregled zapisanih sej (brez podatkovne baze --- disk je vir resnice)
# ---------------------------------------------------------------------------
def list_sessions(base_dir: Path | str) -> list[dict[str, Any]]:
    """Prebere vse seje v mapi in vrne povzetke, najnovejsa prva."""
    base = Path(base_dir)
    if not base.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for d in sorted(base.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        # Galerija / nezaupani zajemi niso telemetrijske seje.
        if d.name in ("gallery", "unassigned-captures"):
            continue
        tel = d / "telemetry.jsonl"
        meta: dict[str, Any] = {}
        mf = d / "meta.json"
        if mf.is_file():
            try:
                meta = json.loads(mf.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        out.append({
            "name": d.name,
            "dir": str(d),
            "started_at": meta.get("started_at"),
            "ended_at": meta.get("ended_at"),
            "duration_s": meta.get("duration_s"),
            "mission_id": meta.get("mission_id"),
            "mission_name": meta.get("mission_name"),
            "reason": meta.get("reason"),
            "messages": meta.get("messages"),
            "running": meta.get("ended_at") is None,
            "size_b": tel.stat().st_size if tel.is_file() else 0,
            "has_plan": (d / "plan.json").is_file(),
            "has_captures": (d / "captures.jsonl").is_file(),
        })
    return out
