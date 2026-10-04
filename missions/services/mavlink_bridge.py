"""MAVLink most med Pixhawkom in spletnim vmesnikom.

Naloge:

- najti razpolozljive serijske porte (:func:`list_serial_ports`),
- vzpostaviti pymavlink povezavo v ozadnji niti,
- brati sporocila in posodabljati pomnilniski snapshot
  (:meth:`MavlinkBridge.get_snapshot`), ki ga HTTP plast serializira v JSON,
- zapisovati **vsa** zanimiva sporocila na disk prek
  :class:`~missions.services.telemetry_log.TelemetryLogger`,
- prenesti misijo na krmilnik (MAVLink mission protocol),
- posiljati ukaze ``COMMAND_LONG`` (arm, nacin, start misije) in cakati
  na ``COMMAND_ACK``.

Odpornost na napake:

- ce manjka ``pymavlink`` ali ``pyserial``, se modul se vedno uvozi (Django
  startup ne propade), klic :meth:`connect` pa vrne napako v snapshotu;
- bralna nit je *daemon* in samostojno poskusa reconnect z eksponentnim
  zamikom, ce povezava izgine;
- protokolarni odgovori (``MISSION_REQUEST*``, ``MISSION_ACK``,
  ``COMMAND_ACK``) gredo v locene vrste, da jih lahko sinhronizirano
  poberejo Django request niti.
"""
from __future__ import annotations

import math
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from .flight_state import AirborneDetector
from .telemetry_log import TelemetryLogger

# Lenivni import: pymavlink/pyserial sta v requirements.txt, ampak ne zelimo
# zlomiti Django startup, ce v razvojnem okolju nista namescena.
try:
    from pymavlink import mavutil  # type: ignore
    _MAV_AVAILABLE = True
except Exception:  # pragma: no cover
    mavutil = None  # type: ignore
    _MAV_AVAILABLE = False

try:
    import serial.tools.list_ports as _list_ports  # type: ignore
    _SERIAL_AVAILABLE = True
except Exception:  # pragma: no cover
    _list_ports = None  # type: ignore
    _SERIAL_AVAILABLE = False


def mavlink_available() -> bool:
    return _MAV_AVAILABLE


# ---------------------------------------------------------------------------
# Serijski porti
# ---------------------------------------------------------------------------
def list_serial_ports() -> list[dict[str, str]]:
    """Vrne seznam vidnih serijskih naprav.

    Vsak vnos: ``{"device": "/dev/ttyACM0", "description": "Pixhawk",
    "hwid": "USB VID:PID=..."}``. Na Windowsih bo ``device`` npr. ``COM3``.
    """
    if not _SERIAL_AVAILABLE:
        return []
    out: list[dict[str, str]] = []
    for p in _list_ports.comports():  # type: ignore[attr-defined]
        out.append({
            "device": p.device,
            "description": p.description or "",
            "hwid": p.hwid or "",
            "manufacturer": (p.manufacturer or "") if hasattr(p, "manufacturer") else "",
        })

    def _rank(d: dict[str, str]) -> int:
        dev = d["device"].lower()
        if "acm" in dev:
            return 0
        if "usb" in dev:
            return 1
        if dev.startswith("com"):
            return 2
        return 9

    out.sort(key=_rank)
    return out


# ---------------------------------------------------------------------------
# Snapshot data-class
# ---------------------------------------------------------------------------
@dataclass
class _Snapshot:
    """Najnovejsa znana telemetrija. Vse vrednosti so opcijske."""
    # Povezava
    connected: bool = False
    port: Optional[str] = None
    baud: Optional[int] = None
    error: Optional[str] = None
    connected_at: Optional[float] = None

    # HEARTBEAT
    mode: Optional[str] = None
    armed: bool = False
    system_status: Optional[str] = None
    heartbeat_ts: Optional[float] = None

    # ATTITUDE (rad -> stopinje)
    roll_deg: Optional[float] = None
    pitch_deg: Optional[float] = None
    yaw_deg: Optional[float] = None
    attitude_ts: Optional[float] = None

    # GPS / GLOBAL_POSITION_INT
    lat: Optional[float] = None
    lon: Optional[float] = None
    gps_alt_msl_m: Optional[float] = None
    rel_alt_m: Optional[float] = None
    fix_type: Optional[int] = None
    satellites: Optional[int] = None
    hdop: Optional[float] = None
    vx_ms: Optional[float] = None
    vy_ms: Optional[float] = None
    vz_ms: Optional[float] = None
    gps_ts: Optional[float] = None

    # LOCAL_POSITION_NED (m, NED)
    local_n_m: Optional[float] = None
    local_e_m: Optional[float] = None
    local_d_m: Optional[float] = None
    local_ts: Optional[float] = None

    # VFR_HUD
    airspeed_ms: Optional[float] = None
    groundspeed_ms: Optional[float] = None
    alt_msl_m: Optional[float] = None
    climb_ms: Optional[float] = None
    throttle_pct: Optional[int] = None
    heading_deg: Optional[float] = None
    vfr_ts: Optional[float] = None

    # SYS_STATUS / BATTERY_STATUS
    voltage_v: Optional[float] = None
    current_a: Optional[float] = None
    battery_remaining_pct: Optional[int] = None
    battery_ts: Optional[float] = None
    gps_healthy: Optional[bool] = None
    mag_healthy: Optional[bool] = None
    sensors_ts: Optional[float] = None

    # Kompas (SCALED_IMU / RAW_IMU) --- mG = milligauss
    mag_x_mg: Optional[float] = None
    mag_y_mg: Optional[float] = None
    mag_z_mg: Optional[float] = None
    mag_field_mg: Optional[float] = None
    mag_xy_mg: Optional[float] = None
    mag_ts: Optional[float] = None

    # Onboard kalibracija kompasa (MAG_CAL_*)
    mag_cal_pct: Optional[int] = None
    mag_cal_status: Optional[int] = None
    mag_cal_compass_id: Optional[int] = None
    mag_cal_fitness: Optional[float] = None
    mag_cal_ts: Optional[float] = None

    # Napredek misije
    mission_current_seq: Optional[int] = None
    mission_reached_seq: Optional[int] = None
    mission_item_count: Optional[int] = None
    mission_uploaded_at: Optional[float] = None
    mission_id: Optional[int] = None
    mission_name: Optional[str] = None

    # Stanje leta (EXTENDED_SYS_STATE.landed_state)
    landed_state: Optional[int] = None
    airborne: bool = False

    # Home pozicija, kot jo poroca krmilnik
    home_lat: Optional[float] = None
    home_lon: Optional[float] = None

    # Diagnostika
    messages_received: int = 0

    def to_dict(self) -> dict[str, Any]:
        now = time.time()

        def age(ts: Optional[float]) -> Optional[float]:
            return None if ts is None else round(now - ts, 2)

        return {
            "connected": self.connected,
            "port": self.port,
            "baud": self.baud,
            "error": self.error,
            "connected_at": self.connected_at,
            "uptime_s": round(now - self.connected_at, 1) if self.connected_at else None,
            "messages_received": self.messages_received,
            "heartbeat": {
                "mode": self.mode,
                "armed": self.armed,
                "system_status": self.system_status,
                "age_s": age(self.heartbeat_ts),
            },
            "attitude": {
                "roll_deg": self.roll_deg,
                "pitch_deg": self.pitch_deg,
                "yaw_deg": self.yaw_deg,
                "age_s": age(self.attitude_ts),
            },
            "gps": {
                "lat": self.lat,
                "lon": self.lon,
                "alt_msl_m": self.gps_alt_msl_m,
                "rel_alt_m": self.rel_alt_m,
                "fix_type": self.fix_type,
                "satellites": self.satellites,
                "hdop": self.hdop,
                "vx_ms": self.vx_ms,
                "vy_ms": self.vy_ms,
                "vz_ms": self.vz_ms,
                "healthy": self.gps_healthy,
                "age_s": age(self.gps_ts),
            },
            "local_ned": {
                "n_m": self.local_n_m,
                "e_m": self.local_e_m,
                "d_m": self.local_d_m,
                "age_s": age(self.local_ts),
            },
            "compass": {
                "x_mg": self.mag_x_mg,
                "y_mg": self.mag_y_mg,
                "z_mg": self.mag_z_mg,
                "field_mg": self.mag_field_mg,
                "xy_mg": self.mag_xy_mg,
                "healthy": self.mag_healthy,
                "cal_pct": self.mag_cal_pct,
                "cal_status": self.mag_cal_status,
                "cal_status_label": _mag_cal_status_label(self.mag_cal_status),
                "cal_compass_id": self.mag_cal_compass_id,
                "cal_fitness": self.mag_cal_fitness,
                "age_s": age(self.mag_ts),
                "cal_age_s": age(self.mag_cal_ts),
            },
            "vfr_hud": {
                "airspeed_ms": self.airspeed_ms,
                "groundspeed_ms": self.groundspeed_ms,
                "alt_msl_m": self.alt_msl_m,
                "climb_ms": self.climb_ms,
                "throttle_pct": self.throttle_pct,
                "heading_deg": self.heading_deg,
                "age_s": age(self.vfr_ts),
            },
            "battery": {
                "voltage_v": self.voltage_v,
                "current_a": self.current_a,
                "remaining_pct": self.battery_remaining_pct,
                "age_s": age(self.battery_ts),
            },
            "mission": {
                "current_seq": self.mission_current_seq,
                "reached_seq": self.mission_reached_seq,
                "item_count": self.mission_item_count,
                "uploaded_at": self.mission_uploaded_at,
                "id": self.mission_id,
                "name": self.mission_name,
            },
            "flight": {"landed_state": self.landed_state,
                       "airborne": self.airborne},
            "home": {"lat": self.home_lat, "lon": self.home_lon},
        }


# ---------------------------------------------------------------------------
# Most sam
# ---------------------------------------------------------------------------
class MavlinkBridge:
    """Singleton most med pymavlink povezavo in HTTP plastjo."""

    def __init__(
        self,
        log_dir: Optional[Path | str] = None,
        log_trigger: str = "takeoff",
        dynamic_log_dir: bool = False,
    ) -> None:
        """
        Args:
            log_dir: mapa za zapise telemetrije; ``None`` izklopi logiranje.
            log_trigger: kdaj se zapis zacne in konca --- ``"takeoff"``
                (ob dejanskem vzletu in pristanku, privzeto), ``"arm"``
                (ob armanju in dis-armanju) ali ``"off"`` (samo rocno).
            dynamic_log_dir: pred vsakim novim zapisom ponovno preveri USB.
        """
        self._snap = _Snapshot()
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._connecting = False
        self._mav: Any = None  # mavutil.mavfile
        # Protokolarne vrste --- daemon nit potisne sem, request niti pobirajo.
        self._mission_events: "queue.Queue[Any]" = queue.Queue()
        self._command_events: "queue.Queue[Any]" = queue.Queue()
        self._param_events: "queue.Queue[Any]" = queue.Queue()
        self._upload_lock = threading.Lock()
        self._command_lock = threading.RLock()
        self._param_lock = threading.Lock()
        # Zadnja sporocila STATUSTEXT (pre-arm napake ipd.) za prikaz pilotu.
        self._statustexts: deque[dict[str, Any]] = deque(maxlen=25)
        # Logiranje na disk
        self.log_trigger = log_trigger
        self.logger = TelemetryLogger(log_dir) if log_dir else None
        self._dynamic_log_dir = dynamic_log_dir
        self._logger_lock = threading.Lock()
        self._last_logger_probe = 0.0
        self._detector = AirborneDetector()
        self._pending: dict[str, Any] = {}

    # -------- javne metode: stanje --------

    def is_connected(self) -> bool:
        with self._lock:
            return self._snap.connected

    def is_connecting(self) -> bool:
        """True, dokler bralna nit aktivno odpira/reconnecta povezavo."""
        with self._lock:
            if self._snap.connected:
                return False
            return self._connecting or (
                self._thread is not None and self._thread.is_alive()
            )

    def get_snapshot(self) -> dict[str, Any]:
        with self._lock:
            snap = self._snap.to_dict()
            snap["statustexts"] = list(self._statustexts)
            snap["connecting"] = (
                not self._snap.connected
                and (self._connecting
                     or (self._thread is not None and self._thread.is_alive()))
            )
        snap["logging"] = self.logger.status() if self.logger else {"active": False}
        snap["mavlink_available"] = _MAV_AVAILABLE
        return snap

    def statustexts(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._statustexts)

    # -------- javne metode: povezava --------

    def connect(self, device: str, baud: int = 115200) -> dict[str, Any]:
        """Vzpostavi povezavo s Pixhawkom in zazene branje v ozadnji niti."""
        if not _MAV_AVAILABLE:
            with self._lock:
                self._snap.error = (
                    "pymavlink ni namescen --- pip install pymavlink pyserial"
                )
            return self.get_snapshot()

        with self._lock:
            same_target = (
                self._snap.port == device and self._snap.baud == baud)
            if self._snap.connected and same_target:
                return self.get_snapshot()
            already_same = self.is_connecting() and same_target

        if not already_same:
            self.disconnect()
            with self._lock:
                self._snap = _Snapshot(
                    connected=False, port=device, baud=baud, error=None)
                self._statustexts.clear()
                self._connecting = True
            self._detector.reset()

            self._stop_event = threading.Event()
            self._thread = threading.Thread(
                target=self._run_loop, args=(device, baud), daemon=True,
                name=f"mavlink-bridge-{device}",
            )
            self._thread.start()

        # Pocakaj dlje od heartbeat timeouta (4 s), da ne javimo "failed"
        # trenutek pred uspehom.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if self.is_connected():
                break
            with self._lock:
                if self._snap.error and not self._snap.connected:
                    # Kratek premor, da nit zapre port pred naslednjim kandidatom.
                    time.sleep(0.15)
                    break
            time.sleep(0.1)
        return self.get_snapshot()

    def disconnect(self) -> dict[str, Any]:
        self._stop_event.set()
        thread = self._thread
        mav = self._mav
        if mav is not None:
            try:
                mav.close()
            except Exception:
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._mav = None
        self._thread = None
        if self.logger is not None and self.logger.is_active:
            self.logger.stop(reason="disconnect")
        with self._lock:
            self._connecting = False
            self._snap = _Snapshot(connected=False)
        return self.get_snapshot()

    # -------- javne metode: misija --------

    def set_pending_mission(
        self,
        mission_id: Optional[int],
        mission_name: Optional[str],
        plan: Optional[dict[str, Any]] = None,
        item_count: Optional[int] = None,
    ) -> None:
        """Zapomni si, katera misija je nalozena.

        Uporabljeno na dva nacina: za prikaz v HUD-u in kot metapodatek, ki
        ga dobi zapisovalnik telemetrije, ko se log samodejno zazene ob
        armanju.
        """
        self._pending = {
            "mission_id": mission_id,
            "mission_name": mission_name,
            "plan": plan,
        }
        with self._lock:
            self._snap.mission_id = mission_id
            self._snap.mission_name = mission_name
            self._snap.mission_item_count = item_count
            self._snap.mission_uploaded_at = time.time()
            self._snap.mission_current_seq = None
            self._snap.mission_reached_seq = None

    def upload_mission(self, items: list[Any]) -> dict[str, Any]:
        """Prenese ze zgrajeno zaporedje ``MissionItem`` na krmilnik."""
        return _bridge_upload_mission(self, items)

    # -------- javne metode: ukazi --------

    def arm(self, arm: bool = True, force: bool = False) -> dict[str, Any]:
        """Armira ali dis-armira krmilnik.

        ``force=True`` posilja magicno vrednost 21196, ki obide del
        preverjanj --- namenjeno **izkljucno** dis-armanju v sili na tleh.
        """
        p2 = 21196.0 if force else 0.0
        return self.send_command_long(
            _cmd("MAV_CMD_COMPONENT_ARM_DISARM"),
            1.0 if arm else 0.0, p2,
            label="ARM" if arm else "DISARM",
        )

    def set_mode(self, mode_name: str) -> dict[str, Any]:
        """Preklopi letalni nacin po imenu (npr. ``AUTO``, ``GUIDED``, ``RTL``)."""
        if not _MAV_AVAILABLE:
            return {"ok": False, "error": "pymavlink ni namescen."}
        mav = self._mav
        if not self.is_connected() or mav is None:
            return {"ok": False, "error": "Ni MAVLink povezave s Pixhawkom."}
        name = (mode_name or "").strip().upper()
        try:
            mapping = mav.mode_mapping() or {}
        except Exception:
            mapping = {}
        if name not in mapping:
            return {
                "ok": False,
                "error": f"Nacin '{name}' ni na voljo.",
                "available": sorted(mapping.keys()),
            }
        mode_id = mapping[name]
        return self.send_command_long(
            _cmd("MAV_CMD_DO_SET_MODE"),
            float(_flag("MAV_MODE_FLAG_CUSTOM_MODE_ENABLED")),
            float(mode_id),
            label=f"MODE {name}",
        )

    def takeoff(self, altitude_m: float) -> dict[str, Any]:
        """Ukaz za vzlet na dano relativno visino.

        Predpogoj je nacin, ki vzlet dovoljuje (``GUIDED``), in armiran
        letalnik. Pri ``COMMAND_LONG`` nosi visino param7.
        """
        return self.send_command_long(
            _cmd("MAV_CMD_NAV_TAKEOFF"),
            0.0, 0.0, 0.0, 0.0, 0.0, 0.0, float(altitude_m),
            label=f"TAKEOFF {altitude_m:.1f} m",
        )

    def goto_ned(
        self,
        north_m: float,
        east_m: float,
        down_m: float,
        *,
        yaw_rad: Optional[float] = None,
    ) -> dict[str, Any]:
        """GUIDED cilj v ``MAV_FRAME_LOCAL_NED`` (m).

        ``down_m`` je pozitivno navzdol (pri 3 m AGL tipično ≈ −3).
        """
        return self._set_position_target_local_ned(
            float(north_m), float(east_m), float(down_m), yaw_rad=yaw_rad,
            label=f"GOTO_NED N={north_m:.1f} E={east_m:.1f} D={down_m:.1f}",
        )

    def goto_global(
        self,
        lat_deg: float,
        lon_deg: float,
        alt_rel_m: float,
        *,
        yaw_rad: Optional[float] = None,
    ) -> dict[str, Any]:
        """GUIDED cilj v ``MAV_FRAME_GLOBAL_RELATIVE_ALT_INT``."""
        return self._set_position_target_global_int(
            float(lat_deg), float(lon_deg), float(alt_rel_m),
            yaw_rad=yaw_rad,
            label=f"GOTO_GLOBAL {lat_deg:.7f},{lon_deg:.7f} alt={alt_rel_m:.1f}",
        )

    def capture_image(self, count: int = 1) -> dict[str, Any]:
        """Sproži zajem slike (`MAV_CMD_IMAGE_START_CAPTURE`).

        Companion ``camera_trigger.py`` posluša ta ukaz in shrani JPEG.
        """
        n = max(1, int(count))
        return self.send_command_long(
            _cmd("MAV_CMD_IMAGE_START_CAPTURE"),
            0.0,          # camera id (0 = vse / privzeta)
            0.0,          # interval [s]
            float(n),     # število posnetkov
            0.0,          # sequence
            label=f"IMAGE_CAPTURE x{n}",
            timeout=3.0,
        )

    def start_mission(self, first_item: int = 0, last_item: int = 0) -> dict[str, Any]:
        """Posilja ``MISSION_START``. Krmilnik mora biti armiran in v AUTO."""
        return self.send_command_long(
            _cmd("MAV_CMD_MISSION_START"),
            float(first_item), float(last_item),
            label="MISSION_START",
        )

    def start_mag_cal(self, *, autosave: bool = True) -> dict[str, Any]:
        """Zazene onboard kalibracijo kompasa (``MAV_CMD_DO_START_MAG_CAL``).

        Dron mora biti **disarmed**. Pilot nato dron pocasi obraca v vse
        smeri; napredek pride prek ``MAG_CAL_PROGRESS``.
        """
        with self._lock:
            if self._snap.armed:
                return {
                    "ok": False,
                    "error": "Za kalibracijo kompasa mora biti letalnik DISARMED.",
                    "statustexts": list(self._statustexts)[-5:],
                }
            self._snap.mag_cal_pct = 0
            self._snap.mag_cal_status = 1  # WAITING_TO_START
            self._snap.mag_cal_fitness = None
            self._snap.mag_cal_ts = time.time()
        # param1=0 → vsi kompas; param2=retry; param3=autosave; param5=brez reboot
        return self.send_command_long(
            _cmd("MAV_CMD_DO_START_MAG_CAL"),
            0.0, 1.0, 1.0 if autosave else 0.0, 0.0, 0.0,
            label="MAG_CAL_START",
            timeout=8.0,
            retries=1,
        )

    def cancel_mag_cal(self) -> dict[str, Any]:
        """Prekine tekočo kalibracijo kompasa."""
        return self.send_command_long(
            _cmd("MAV_CMD_DO_CANCEL_MAG_CAL"),
            0.0,
            label="MAG_CAL_CANCEL",
        )

    def get_param(self, name: str, timeout: float = 8.0) -> dict[str, Any]:
        """Prebere en parameter z krmilnika (``PARAM_REQUEST_READ``)."""
        return self._param_roundtrip(name, value=None, timeout=timeout)

    def set_param(self, name: str, value: float,
                  timeout: float = 8.0) -> dict[str, Any]:
        """Nastavi parameter na krmilniku (``PARAM_SET``) in caka na potrditev."""
        return self._param_roundtrip(name, value=float(value), timeout=timeout)

    def _param_roundtrip(
        self,
        name: str,
        value: Optional[float],
        timeout: float,
    ) -> dict[str, Any]:
        """Skupna pot za branje (value=None) ali pisanje parametra."""
        if not _MAV_AVAILABLE:
            return {"ok": False, "error": "pymavlink ni namescen."}
        mav = self._mav
        if not self.is_connected() or mav is None:
            return {"ok": False, "error": "Ni MAVLink povezave s Pixhawkom."}

        pname = (name or "").strip().upper()
        if not pname or len(pname) > 16:
            return {"ok": False, "error": "Neveljavno ime parametra."}

        with self._param_lock:
            _drain(self._param_events)
            target_system = getattr(mav, "target_system", 0) or 1
            target_component = getattr(mav, "target_component", 0) or 1
            pid = _param_id_bytes(pname)
            attempts = 3
            attempt_timeout = max(0.05, timeout / attempts)
            for _attempt in range(attempts):
                try:
                    if value is None:
                        mav.mav.param_request_read_send(
                            target_system, target_component, pid, -1)
                    else:
                        ptype = _flag("MAV_PARAM_TYPE_REAL32") or 9
                        mav.mav.param_set_send(
                            target_system, target_component, pid,
                            float(value), int(ptype))
                except Exception as exc:
                    return {
                        "ok": False,
                        "error": f"Posiljanje ni uspelo: {exc}",
                        "name": pname,
                    }

                deadline = time.monotonic() + attempt_timeout
                while time.monotonic() < deadline:
                    try:
                        msg = self._param_events.get(
                            timeout=max(0.05, deadline - time.monotonic()))
                    except queue.Empty:
                        break
                    got = _param_id_str(getattr(msg, "param_id", b""))
                    if got != pname:
                        continue
                    return {
                        "ok": True,
                        "name": pname,
                        "value": float(msg.param_value),
                        "type": int(getattr(msg, "param_type", 0)),
                        "index": int(getattr(msg, "param_index", -1)),
                        "count": int(getattr(msg, "param_count", 0)),
                    }
            action = "branje" if value is None else "zapis"
            return {
                "ok": False,
                "name": pname,
                "error": (
                    f"Ni PARAM_VALUE za {pname} po {attempts} poskusih "
                    f"v {timeout:.0f} s ({action})."
                ),
            }

    def _position_type_mask(self, *, ignore_yaw: bool = True) -> int:
        """Maska: uporabi samo pozicijo (in po želji yaw)."""
        bits = (
            _flag("POSITION_TARGET_TYPEMASK_VX_IGNORE")
            | _flag("POSITION_TARGET_TYPEMASK_VY_IGNORE")
            | _flag("POSITION_TARGET_TYPEMASK_VZ_IGNORE")
            | _flag("POSITION_TARGET_TYPEMASK_AX_IGNORE")
            | _flag("POSITION_TARGET_TYPEMASK_AY_IGNORE")
            | _flag("POSITION_TARGET_TYPEMASK_AZ_IGNORE")
            | _flag("POSITION_TARGET_TYPEMASK_YAW_RATE_IGNORE")
        )
        if ignore_yaw:
            bits |= _flag("POSITION_TARGET_TYPEMASK_YAW_IGNORE")
        # Rezerva, če dialekt nima imen (0 = uporabi vse — slabo). Tipične
        # vrednosti iz MAVLink common.xml za "samo pozicija":
        if bits == 0:
            bits = 0b0000_1111_1111_1000  # ignore vel/acc/yaw_rate; keep pos
            if ignore_yaw:
                bits |= 0b0000_0100_0000_0000
        return int(bits)

    def _set_position_target_local_ned(
        self,
        north_m: float,
        east_m: float,
        down_m: float,
        *,
        yaw_rad: Optional[float] = None,
        label: str = "GOTO_NED",
    ) -> dict[str, Any]:
        if not _MAV_AVAILABLE:
            return {"ok": False, "error": "pymavlink ni namescen."}
        mav = self._mav
        if not self.is_connected() or mav is None:
            return {"ok": False, "error": "Ni MAVLink povezave s Pixhawkom."}
        ignore_yaw = yaw_rad is None
        try:
            mav.mav.set_position_target_local_ned_send(
                0,
                getattr(mav, "target_system", 0) or 1,
                getattr(mav, "target_component", 0) or 1,
                _flag("MAV_FRAME_LOCAL_NED") or 1,
                self._position_type_mask(ignore_yaw=ignore_yaw),
                float(north_m), float(east_m), float(down_m),
                0.0, 0.0, 0.0,
                0.0, 0.0, 0.0,
                0.0 if yaw_rad is None else float(yaw_rad),
                0.0,
            )
        except Exception as exc:
            return {"ok": False, "error": f"{label}: {exc}"}
        return {"ok": True, "label": label}

    def _set_position_target_global_int(
        self,
        lat_deg: float,
        lon_deg: float,
        alt_rel_m: float,
        *,
        yaw_rad: Optional[float] = None,
        label: str = "GOTO_GLOBAL",
    ) -> dict[str, Any]:
        if not _MAV_AVAILABLE:
            return {"ok": False, "error": "pymavlink ni namescen."}
        mav = self._mav
        if not self.is_connected() or mav is None:
            return {"ok": False, "error": "Ni MAVLink povezave s Pixhawkom."}
        ignore_yaw = yaw_rad is None
        # GLOBAL_RELATIVE_ALT_INT = 6
        frame = _flag("MAV_FRAME_GLOBAL_RELATIVE_ALT_INT") or 6
        try:
            mav.mav.set_position_target_global_int_send(
                0,
                getattr(mav, "target_system", 0) or 1,
                getattr(mav, "target_component", 0) or 1,
                frame,
                self._position_type_mask(ignore_yaw=ignore_yaw),
                int(round(lat_deg * 1e7)),
                int(round(lon_deg * 1e7)),
                float(alt_rel_m),
                0.0, 0.0, 0.0,
                0.0, 0.0, 0.0,
                0.0 if yaw_rad is None else float(yaw_rad),
                0.0,
            )
        except Exception as exc:
            return {"ok": False, "error": f"{label}: {exc}"}
        return {"ok": True, "label": label}

    def send_command_long(
        self,
        command: int,
        param1: float = 0.0,
        param2: float = 0.0,
        param3: float = 0.0,
        param4: float = 0.0,
        param5: float = 0.0,
        param6: float = 0.0,
        param7: float = 0.0,
        *,
        label: str = "",
        timeout: float = 5.0,
        retries: int = 0,
    ) -> dict[str, Any]:
        """Serializira ukaze, da sočasni klic ne more prevzeti tujega ACK-a."""
        with self._command_lock:
            return self._send_command_long_locked(
                command,
                param1, param2, param3, param4, param5, param6, param7,
                label=label,
                timeout=timeout,
                retries=retries,
            )

    def _send_command_long_locked(
        self,
        command: int,
        param1: float = 0.0,
        param2: float = 0.0,
        param3: float = 0.0,
        param4: float = 0.0,
        param5: float = 0.0,
        param6: float = 0.0,
        param7: float = 0.0,
        *,
        label: str = "",
        timeout: float = 5.0,
        retries: int = 0,
    ) -> dict[str, Any]:
        """Posilja ``COMMAND_LONG`` in caka na ustrezen ``COMMAND_ACK``."""
        if not _MAV_AVAILABLE:
            return {"ok": False, "error": "pymavlink ni namescen."}
        mav = self._mav
        if not self.is_connected() or mav is None:
            return {"ok": False, "error": "Ni MAVLink povezave s Pixhawkom."}

        # Pocisti stare ACK-e, da ne beremo odgovora prejsnjega ukaza.
        _drain(self._command_events)

        target_system = getattr(mav, "target_system", 0) or 1
        target_component = getattr(mav, "target_component", 0) or 1
        try:
            mav.mav.command_long_send(
                target_system, target_component, int(command), 0,
                float(param1), float(param2), float(param3), float(param4),
                float(param5), float(param6), float(param7),
            )
        except Exception as exc:
            return {"ok": False, "error": f"Posiljanje ni uspelo: {exc}"}

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                ack = self._command_events.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                break
            if int(getattr(ack, "command", -1)) != int(command):
                continue  # ACK za nek drug ukaz --- preskoci
            code = int(getattr(ack, "result", -1))
            return {
                "ok": code == 0,
                "command": int(command),
                "label": label,
                "result": code,
                "result_name": _RESULT_NAMES.get(code, f"UNKNOWN_{code}"),
                "error": None if code == 0 else (
                    f"{label or command}: {_RESULT_NAMES.get(code, code)}"
                ),
                "statustexts": self.statustexts()[-5:],
            }
        if retries > 0:
            result = self.send_command_long(
                command,
                param1, param2, param3, param4, param5, param6, param7,
                label=label,
                timeout=timeout,
                retries=retries - 1,
            )
            if not result.get("ok") and "Ni ACK-a" in str(result.get("error")):
                result["error"] = "Ni ACK-a niti po ponovnem pošiljanju ukaza."
            return result
        return {
            "ok": False,
            "command": int(command),
            "label": label,
            "error": f"Ni ACK-a v {timeout:.0f} s.",
            "statustexts": self.statustexts()[-5:],
        }

    # -------- javne metode: logiranje --------

    def ensure_logger(self, force: bool = False) -> bool:
        """Priklopi zapisovalnik, če je USB medij trenutno na voljo."""
        if not self._dynamic_log_dir:
            return self.logger is not None
        if self.logger is not None and self.logger.is_active:
            return True
        if self.logger is not None and not force:
            return True
        now = time.monotonic()
        with self._logger_lock:
            if self.logger is not None and self.logger.is_active:
                return True
            if not force and now - self._last_logger_probe < 5.0:
                return False
            self._last_logger_probe = now
            try:
                from .flight_storage import resolve_log_dir
                log_dir = resolve_log_dir()
            except Exception:
                self.logger = None
                return False
            if (self.logger is None
                    or self.logger.base_dir.resolve() != log_dir.resolve()):
                self.logger = TelemetryLogger(log_dir)
            return True

    def start_logging(self, reason: str = "manual", note: str = "") -> dict[str, Any]:
        if not self.ensure_logger(force=True) or self.logger is None:
            return {
                "active": False,
                "error": "USB medij ni na voljo; zapisovanje ni mogoče.",
            }
        with self._lock:
            port, baud = self._snap.port, self._snap.baud
        return self.logger.start(
            mission_id=self._pending.get("mission_id"),
            mission_name=self._pending.get("mission_name"),
            plan=self._pending.get("plan"),
            port=port, baud=baud, reason=reason, note=note,
        )

    def stop_logging(self, reason: str = "manual") -> dict[str, Any]:
        if self.logger is None:
            return {"active": False, "error": "Logiranje ni nastavljeno."}
        return self.logger.stop(reason=reason)

    # -------- interno --------

    def _run_loop(self, device: str, baud: int) -> None:
        """Glavna nit: connect -> ciklus recv_match -> reconnect ob napaki."""
        backoff = 1.0
        try:
            while not self._stop_event.is_set():
                with self._lock:
                    self._connecting = True
                try:
                    mav = mavutil.mavlink_connection(  # type: ignore[union-attr]
                        device,
                        baud=baud,
                        autoreconnect=False,
                        source_system=255,
                        force_mavlink2=True,
                    )
                except Exception as exc:
                    with self._lock:
                        self._snap.error = f"open failed: {exc}"
                    if self._stop_event.wait(timeout=backoff):
                        return
                    backoff = min(backoff * 2, 8.0)
                    continue

                self._mav = mav
                # Sprazni morebiten smeti v bufferju (bootloader / ModemManager).
                try:
                    if hasattr(mav, "port") and mav.port is not None:
                        mav.port.reset_input_buffer()
                except Exception:
                    pass
                # Pixhawk po USB-ju včasih potrebuje nekaj sekund po enumeraciji.
                try:
                    hb = mav.wait_heartbeat(timeout=8)
                except Exception as exc:
                    hb = None
                    with self._lock:
                        self._snap.error = f"heartbeat exception: {exc}"
                if hb is None:
                    with self._lock:
                        if not self._snap.error:
                            self._snap.error = (
                                "ni HEARTBEAT-a (timeout 8 s) — preveri baud, "
                                "da ModemManager/drug proces ne drži vrat, "
                                "in da je Pixhawk zagnan (ne v bootloaderju)"
                            )
                    try:
                        mav.close()
                    except Exception:
                        pass
                    if self._stop_event.wait(timeout=backoff):
                        return
                    backoff = min(backoff * 2, 8.0)
                    continue

                with self._lock:
                    self._snap.connected = True
                    self._snap.error = None
                    self._snap.connected_at = time.time()
                    self._connecting = False
                backoff = 1.0
                self._request_data_streams(mav)
                self._consume_messages(mav)
                with self._lock:
                    self._snap.connected = False
                    self._connecting = True
        finally:
            with self._lock:
                self._connecting = False

    def _request_data_streams(self, mav: Any) -> None:
        """Prosi krmilnik za telemetrijo pri uporabni frekvenci.

        Brez tega ArduPilot na TELEM portu posilja privzeto zelo skopo; za
        oceno natancnosti sledenja trajektoriji potrebujemo pozicijo pri
        vsaj 5 Hz.
        """
        try:
            mav.mav.request_data_stream_send(
                getattr(mav, "target_system", 1) or 1,
                getattr(mav, "target_component", 1) or 1,
                mavutil.mavlink.MAV_DATA_STREAM_ALL,  # type: ignore[union-attr]
                5, 1,
            )
        except Exception:
            pass

    def _consume_messages(self, mav: Any) -> None:
        """Bere sporocila in posodablja snapshot, dokler je ziva povezava."""
        last_msg = time.time()
        while not self._stop_event.is_set():
            try:
                msg = mav.recv_match(blocking=True, timeout=1.0)
            except Exception as exc:
                with self._lock:
                    self._snap.error = f"recv error: {exc}"
                return
            if msg is None:
                if time.time() - last_msg > 6.0:
                    with self._lock:
                        self._snap.error = "no messages for 6 s, reconnecting"
                    return
                continue
            last_msg = time.time()
            # Logiranje pred obdelavo: tudi protokolarna sporocila so
            # zanimiva za analizo latence.
            if self.logger is not None:
                self.logger.write(msg, last_msg)
            self._apply_message(msg)

    def _apply_message(self, msg: Any) -> None:
        t = msg.get_type()

        # Protokolarna sporocila gredo v locene vrste.
        if t in ("MISSION_REQUEST", "MISSION_REQUEST_INT", "MISSION_ACK"):
            try:
                self._mission_events.put_nowait(msg)
            except queue.Full:
                pass
            return
        if t == "COMMAND_ACK":
            try:
                self._command_events.put_nowait(msg)
            except queue.Full:
                pass
            return
        if t == "PARAM_VALUE":
            try:
                self._param_events.put_nowait(msg)
            except queue.Full:
                pass
            return

        armed_before = None
        armed_after = None

        with self._lock:
            self._snap.messages_received += 1
            now = time.time()

            if t == "HEARTBEAT":
                try:
                    mode_name = mavutil.mode_string_v10(msg)  # type: ignore[union-attr]
                except Exception:
                    mode_name = str(msg.custom_mode)
                armed = bool(msg.base_mode & 0b1000_0000)
                armed_before = self._snap.armed
                armed_after = armed
                self._snap.mode = mode_name
                self._snap.armed = armed
                self._snap.system_status = _system_status_name(msg.system_status)
                self._snap.heartbeat_ts = now

            elif t == "ATTITUDE":
                self._snap.roll_deg = math.degrees(msg.roll)
                self._snap.pitch_deg = math.degrees(msg.pitch)
                self._snap.yaw_deg = math.degrees(msg.yaw) % 360.0
                self._snap.attitude_ts = now

            elif t == "GPS_RAW_INT":
                self._snap.fix_type = int(msg.fix_type)
                self._snap.satellites = int(msg.satellites_visible)
                # eph je UINT16 v 0.01 enotah; 0xFFFF pomeni "neznano".
                self._snap.hdop = msg.eph / 100.0 if msg.eph != 0xFFFF else None
                if self._snap.lat is None and msg.lat:
                    self._snap.lat = msg.lat / 1e7
                    self._snap.lon = msg.lon / 1e7
                    self._snap.gps_alt_msl_m = msg.alt / 1000.0
                self._snap.gps_ts = now

            elif t == "GLOBAL_POSITION_INT":
                # Zanesljivejsi vir lat/lon kot GPS_RAW_INT (zdruzen z EKF).
                self._snap.lat = msg.lat / 1e7
                self._snap.lon = msg.lon / 1e7
                self._snap.gps_alt_msl_m = msg.alt / 1000.0
                self._snap.rel_alt_m = msg.relative_alt / 1000.0
                # vx/vy/vz so v cm/s.
                self._snap.vx_ms = float(msg.vx) / 100.0
                self._snap.vy_ms = float(msg.vy) / 100.0
                self._snap.vz_ms = float(msg.vz) / 100.0
                if msg.hdg != 0xFFFF:
                    self._snap.heading_deg = msg.hdg / 100.0
                self._snap.gps_ts = now

            elif t == "LOCAL_POSITION_NED":
                self._snap.local_n_m = float(msg.x)
                self._snap.local_e_m = float(msg.y)
                self._snap.local_d_m = float(msg.z)
                self._snap.local_ts = now

            elif t == "VFR_HUD":
                self._snap.airspeed_ms = float(msg.airspeed)
                self._snap.groundspeed_ms = float(msg.groundspeed)
                self._snap.alt_msl_m = float(msg.alt)
                self._snap.climb_ms = float(msg.climb)
                self._snap.throttle_pct = int(msg.throttle)
                self._snap.heading_deg = float(msg.heading) % 360.0
                self._snap.vfr_ts = now

            elif t == "SYS_STATUS":
                v = msg.voltage_battery
                self._snap.voltage_v = v / 1000.0 if v != 0xFFFF else None
                c = msg.current_battery
                self._snap.current_a = c / 100.0 if c >= 0 else None
                self._snap.battery_remaining_pct = (
                    int(msg.battery_remaining) if msg.battery_remaining >= 0 else None
                )
                self._snap.battery_ts = now
                enabled = int(getattr(msg, "onboard_control_sensors_enabled", 0) or 0)
                health = int(getattr(msg, "onboard_control_sensors_health", 0) or 0)
                mag_bit = _flag("MAV_SYS_STATUS_SENSOR_3D_MAG") or 4
                gps_bit = _flag("MAV_SYS_STATUS_SENSOR_GPS") or 32
                if enabled & mag_bit:
                    self._snap.mag_healthy = bool(health & mag_bit)
                if enabled & gps_bit:
                    self._snap.gps_healthy = bool(health & gps_bit)
                self._snap.sensors_ts = now

            elif t in ("SCALED_IMU", "SCALED_IMU2", "SCALED_IMU3", "RAW_IMU"):
                # xmag/ymag/zmag so v milligauss (mG). Tipicno zemeljsko polje
                # je ~250–650 mG; PreArm "Check mag field" gleda odstopanje.
                try:
                    mx = float(msg.xmag)
                    my = float(msg.ymag)
                    mz = float(msg.zmag)
                except (TypeError, ValueError, AttributeError):
                    mx = my = mz = None  # type: ignore[assignment]
                if mx is not None:
                    self._snap.mag_x_mg = mx
                    self._snap.mag_y_mg = my
                    self._snap.mag_z_mg = mz
                    self._snap.mag_field_mg = round(
                        math.sqrt(mx * mx + my * my + mz * mz), 1)
                    self._snap.mag_xy_mg = round(math.sqrt(mx * mx + my * my), 1)
                    self._snap.mag_ts = now

            elif t == "MAG_CAL_PROGRESS":
                self._snap.mag_cal_compass_id = int(getattr(msg, "compass_id", 0))
                self._snap.mag_cal_status = int(getattr(msg, "cal_status", 0))
                self._snap.mag_cal_pct = int(getattr(msg, "completion_pct", 0))
                self._snap.mag_cal_ts = now

            elif t == "MAG_CAL_REPORT":
                self._snap.mag_cal_compass_id = int(getattr(msg, "compass_id", 0))
                self._snap.mag_cal_status = int(getattr(msg, "cal_status", 0))
                try:
                    self._snap.mag_cal_fitness = float(msg.fitness)
                except (TypeError, ValueError, AttributeError):
                    pass
                # Ob uspehu pokazi 100 %.
                if self._snap.mag_cal_status == 4:
                    self._snap.mag_cal_pct = 100
                self._snap.mag_cal_ts = now

            elif t == "BATTERY_STATUS":
                # Natancnejsi vir: ``voltages`` je polje po celicah v mV;
                # sestejemo nenicelne. 0xFFFF pomeni "celica ni prisotna".
                volts = [v for v in msg.voltages if v not in (0xFFFF, 0)]
                if volts:
                    self._snap.voltage_v = sum(volts) / 1000.0
                if msg.current_battery >= 0:
                    self._snap.current_a = msg.current_battery / 100.0
                if msg.battery_remaining >= 0:
                    self._snap.battery_remaining_pct = int(msg.battery_remaining)
                self._snap.battery_ts = now

            elif t == "MISSION_CURRENT":
                self._snap.mission_current_seq = int(msg.seq)

            elif t == "MISSION_ITEM_REACHED":
                self._snap.mission_reached_seq = int(msg.seq)

            elif t == "EXTENDED_SYS_STATE":
                # Avtopilotova lastna ocena, ali je letalnik na tleh.
                # Zanesljivejsa od visine, ker zdruzuje vec senzorjev.
                self._snap.landed_state = int(msg.landed_state)

            elif t == "HOME_POSITION":
                self._snap.home_lat = msg.latitude / 1e7
                self._snap.home_lon = msg.longitude / 1e7

            elif t == "STATUSTEXT":
                text = getattr(msg, "text", "")
                if isinstance(text, bytes):
                    text = text.decode("utf-8", "replace")
                self._statustexts.append({
                    "t": round(now, 2),
                    "severity": int(getattr(msg, "severity", 6)),
                    "text": str(text).rstrip("\x00").strip(),
                })

        # Samodejno logiranje. Zunaj locka, ker odpiranje datoteke ni
        # instantno in ne sme blokirati bralne niti.
        self.ensure_logger()
        if self.logger is None or self.log_trigger == "off":
            return

        if self.log_trigger == "arm":
            if (armed_before is not None and armed_after is not None
                    and armed_before != armed_after):
                if armed_after:
                    self.start_logging(reason="arm")
                else:
                    self.stop_logging(reason="disarm")
            return

        # Privzeto: prozi ob dejanskem vzletu in pristanku.
        with self._lock:
            armed_now = self._snap.armed
            rel_alt = self._snap.rel_alt_m
            landed = self._snap.landed_state
        event = self._detector.update(
            armed=armed_now, rel_alt_m=rel_alt,
            landed_state=landed, now=time.time(),
        )
        if event is None:
            return
        with self._lock:
            self._snap.airborne = self._detector.airborne
        if event == "takeoff":
            self.start_logging(reason=f"vzlet ({self._detector.source})")
        else:
            self.stop_logging(reason=f"pristanek ({self._detector.source})")


# ---------------------------------------------------------------------------
# Prenos misije --- MAVLink mission protocol
# ---------------------------------------------------------------------------
_ACK_NAMES = {
    0: "ACCEPTED", 1: "ERROR", 2: "UNSUPPORTED_FRAME", 3: "UNSUPPORTED",
    4: "NO_SPACE", 5: "INVALID", 6: "INVALID_PARAM1", 7: "INVALID_PARAM2",
    8: "INVALID_PARAM3", 9: "INVALID_PARAM4", 10: "INVALID_PARAM5_X",
    11: "INVALID_PARAM6_Y", 12: "INVALID_PARAM7", 13: "INVALID_SEQUENCE",
    14: "DENIED", 15: "OPERATION_CANCELLED",
}

# MAV_RESULT
_RESULT_NAMES = {
    0: "ACCEPTED", 1: "TEMPORARILY_REJECTED", 2: "DENIED", 3: "UNSUPPORTED",
    4: "FAILED", 5: "IN_PROGRESS", 6: "CANCELLED",
    7: "COMMAND_LONG_ONLY", 8: "COMMAND_INT_ONLY", 9: "UNSUPPORTED_MAV_FRAME",
}

# MAG_CAL_STATUS (ardupilotmega)
_MAG_CAL_STATUS_LABELS = {
    0: "ni začeto",
    1: "čaka",
    2: "korak 1",
    3: "korak 2",
    4: "uspeh",
    5: "napaka",
    6: "slaba orientacija",
    7: "slab radij",
}


def _mag_cal_status_label(status: Optional[int]) -> Optional[str]:
    if status is None:
        return None
    return _MAG_CAL_STATUS_LABELS.get(int(status), f"status {status}")



def _ack_name(code: int) -> str:
    return _ACK_NAMES.get(int(code), f"UNKNOWN_{code}")


# Rezervne vrednosti, ce dialekt v pymavlinku nima imena (starejse verzije).
_CMD_FALLBACKS = {
    "MAV_CMD_DO_START_MAG_CAL": 42424,
    "MAV_CMD_DO_CANCEL_MAG_CAL": 42426,
    "MAV_CMD_DO_ACCEPT_MAG_CAL": 42425,
    "MAV_CMD_IMAGE_START_CAPTURE": 2000,
}


def _cmd(name: str) -> int:
    """Poisce MAV_CMD_* vrednost; ce pymavlinka ni, vrne -1."""
    if not _MAV_AVAILABLE:
        return _CMD_FALLBACKS.get(name, -1)
    try:
        return int(getattr(mavutil.mavlink, name))  # type: ignore[union-attr]
    except AttributeError:
        return _CMD_FALLBACKS.get(name, -1)



def _flag(name: str) -> int:
    if not _MAV_AVAILABLE:
        return 0
    return int(getattr(mavutil.mavlink, name))  # type: ignore[union-attr]


def _drain(q: "queue.Queue[Any]") -> None:
    try:
        while True:
            q.get_nowait()
    except queue.Empty:
        pass


def _param_id_bytes(name: str) -> bytes:
    """MAVLink ``param_id`` je 16 bajtov, null-terminated."""
    raw = name.encode("ascii", "ignore")[:16]
    return raw + b"\x00" * (16 - len(raw))


def _param_id_str(raw: Any) -> str:
    if isinstance(raw, bytes):
        text = raw.split(b"\x00", 1)[0].decode("ascii", "ignore")
    else:
        text = str(raw).split("\x00", 1)[0]
    return text.strip().upper()


def _bridge_upload_mission(
    bridge: "MavlinkBridge",
    items: list[Any],
    timeout_per_step: float = 5.0,
    final_ack_timeout: float = 20.0,
) -> dict[str, Any]:
    """Prenese seznam :class:`MissionItem` na Pixhawk.

    Sekvenca po MAVLink mission protokolu:

      1. ``MISSION_COUNT(count=N)``
      2. za vsak ``i``: krmilnik poslje ``MISSION_REQUEST_INT(seq=i)``,
         most odgovori z ``MISSION_ITEM_INT(seq=i)``
      3. krmilnik zaklljuci z ``MISSION_ACK``

    Vrstni red zahtev ni nujno zaporeden (krmilnik lahko ponovi zahtevo za
    ze poslan item, ce se je paket izgubil), zato posiljamo **tisti** item,
    ki ga krmilnik zahteva, in ne tistega, ki bi bil naslednji po nasem
    stetju.
    """
    if not items:
        return {"ok": False, "error": "Misija nima nobenega ukaza."}
    if not _MAV_AVAILABLE:
        return {"ok": False, "error": "pymavlink ni namescen."}
    if not bridge.is_connected() or bridge._mav is None:
        return {"ok": False, "error": "Ni MAVLink povezave s Pixhawkom."}
    if not bridge._upload_lock.acquire(blocking=False):
        return {"ok": False, "error": "Drug prenos misije je v teku."}

    t_start = time.time()
    try:
        mav = bridge._mav
        mav_mod = mavutil.mavlink  # type: ignore[union-attr]
        target_system = getattr(mav, "target_system", 0) or 1
        target_component = getattr(mav, "target_component", 0) or 1

        _drain(bridge._mission_events)

        count = len(items)
        mav.mav.mission_count_send(
            target_system, target_component, count,
            mav_mod.MAV_MISSION_TYPE_MISSION,
        )

        sent = 0
        seen: set[int] = set()
        # Varovalka: dovolimo do 3x count zahtev (ponovitve zaradi izgub).
        for _ in range(max(8, count * 3)):
            if len(seen) >= count:
                break
            try:
                req = bridge._mission_events.get(timeout=timeout_per_step)
            except queue.Empty:
                return {
                    "ok": False,
                    "error": (f"Timeout: krmilnik ni zahteval naslednjega itema "
                              f"(poslanih {len(seen)}/{count})."),
                    "uploaded": len(seen), "count": count,
                }

            rtype = req.get_type()
            if rtype == "MISSION_ACK":
                code = int(req.type)
                if code == 0 and len(seen) >= count:
                    break
                return {
                    "ok": False,
                    "error": f"Predcasni ACK ({_ack_name(code)}) pri {len(seen)}/{count}.",
                    "ack": _ack_name(code), "uploaded": len(seen), "count": count,
                }

            seq = int(req.seq)
            if seq < 0 or seq >= count:
                return {
                    "ok": False,
                    "error": f"Krmilnik zahteva seq={seq} izven obsega 0..{count - 1}.",
                    "uploaded": len(seen), "count": count,
                }

            it = items[seq]
            mav.mav.mission_item_int_send(
                target_system, target_component,
                seq, int(it.frame), int(it.command),
                int(it.current), int(it.autocontinue),
                float(it.param1), float(it.param2),
                float(it.param3), float(it.param4),
                int(it.x), int(it.y), float(it.z),
                mav_mod.MAV_MISSION_TYPE_MISSION,
            )
            seen.add(seq)
            sent += 1

        # ---- zakljucni ACK ----
        deadline = time.time() + final_ack_timeout
        while time.time() < deadline:
            try:
                ev = bridge._mission_events.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                break
            if ev.get_type() != "MISSION_ACK":
                # Se ena ponovljena zahteva --- odgovori in cakaj naprej.
                seq = int(getattr(ev, "seq", -1))
                if 0 <= seq < count:
                    it = items[seq]
                    mav.mav.mission_item_int_send(
                        target_system, target_component,
                        seq, int(it.frame), int(it.command),
                        int(it.current), int(it.autocontinue),
                        float(it.param1), float(it.param2),
                        float(it.param3), float(it.param4),
                        int(it.x), int(it.y), float(it.z),
                        mav_mod.MAV_MISSION_TYPE_MISSION,
                    )
                    sent += 1
                continue

            code = int(ev.type)
            elapsed_ms = round((time.time() - t_start) * 1000.0, 1)
            if code == 0:
                return {
                    "ok": True, "count": count, "uploaded": len(seen),
                    "items_sent": sent, "ack": _ack_name(code),
                    "elapsed_ms": elapsed_ms,
                }
            return {
                "ok": False, "ack": _ack_name(code), "ack_code": code,
                "error": f"Krmilnik je zavrnil misijo: {_ack_name(code)}",
                "uploaded": len(seen), "count": count, "elapsed_ms": elapsed_ms,
                "statustexts": bridge.statustexts()[-5:],
            }

        return {
            "ok": False,
            "error": "Krmilnik ni vrnil zakljucnega ACK-a.",
            "uploaded": len(seen), "count": count,
        }
    finally:
        bridge._upload_lock.release()


def _system_status_name(code: int) -> str:
    return {
        0: "UNINIT", 1: "BOOT", 2: "CALIBRATING", 3: "STANDBY", 4: "ACTIVE",
        5: "CRITICAL", 6: "EMERGENCY", 7: "POWEROFF", 8: "FLIGHT_TERMINATION",
    }.get(int(code), f"CODE_{code}")


# ---------------------------------------------------------------------------
# Singleton dostop
# ---------------------------------------------------------------------------
_bridge_instance: Optional[MavlinkBridge] = None
_bridge_lock = threading.Lock()


def get_bridge() -> MavlinkBridge:
    """Vrne (in po potrebi ustvari) singleton :class:`MavlinkBridge`.

    Nastavitve (mapa za loge, samodejno logiranje) se preberejo iz Django
    settings ob prvi uporabi, da modul ostane uvozljiv tudi brez Djanga.
    """
    global _bridge_instance
    with _bridge_lock:
        if _bridge_instance is None:
            log_dir: Optional[Path] = None
            trigger = "takeoff"
            try:
                from django.conf import settings
                from .flight_storage import resolve_log_dir
                trigger = str(getattr(settings, "UAV_LOG_TRIGGER", "takeoff"))
                log_dir = resolve_log_dir()
            except Exception:
                log_dir = None
            _bridge_instance = MavlinkBridge(
                log_dir=log_dir,
                log_trigger=trigger,
                dynamic_log_dir=True,
            )
        return _bridge_instance
