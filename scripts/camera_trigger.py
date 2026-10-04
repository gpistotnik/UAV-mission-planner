#!/usr/bin/env python3
"""Deterministicni zajem slik, sinhroniziran z MAVLink dogodki.

Peti deklarirani prispevek magistrske naloge: vgrajeni racunalnik zajema
slikovne podatke *v sodelovanju z avtopilotom*, ne po lastnem uri. Skript
poslusa MAVLink tok in ob dogodkih zajema posnetke:

===========================  =================================================
Dogodek                      Pomen
===========================  =================================================
``CAMERA_TRIGGER``           avtopilot je sprozil kamero (posledica
                             ``DO_SET_CAM_TRIGG_DIST`` v mapping bloku) ---
                             to je *primarni*, deterministicni vir
``MISSION_ITEM_REACHED``     dosezen je waypoint --- zajem na tocki
``COMMAND_LONG``             rocna sprozitev (``IMAGE_START_CAPTURE``)
===========================  =================================================

Determinizem in casovna negotovost
----------------------------------
Med prejemom dogodka in dejansko ekspozicijo mine 50--200 ms (prenos,
priprava senzorja, JPEG kodiranje). Tega ni mogoce odpraviti, mogoce pa ga
je **izmeriti in kompenzirati**, zato za vsak posnetek zapisemo:

* ``t_event``   --- cas prejema MAVLink dogodka (UTC epoch),
* ``t_capture`` --- cas neposredno pred zajemom,
* ``t_done``    --- cas po vrnitvi iz zajema,
* ``pos_age_s`` --- starost pozicije, uporabljene za georeferenco,
* celotno pozicijsko in kotno stanje ob dogodku.

V poletni analizi (``analysis/analyze_flight.py``) se dejanska pozicija
kamere ob ekspoziciji interpolira iz zapisa telemetrije, cas ekspozicije pa
oceni kot sredina intervala ``[t_capture, t_done]``.

Brez kamere
-----------
Ce ``picamera2`` ni na voljo (razvojni racunalnik, RPi brez modula) ali ce
kamere ni mogoce odpreti, skript **ne odpove**: tece v ``dry-run`` nacinu,
kjer zapisuje dogodke in georeferenco, slik pa ne zajema. Tako je mogoce
celotno verigo testirati v SITL simulatorju.

Uporaba
-------
::

    # na dronu (samodejno zazna USB, mavlink-router izpostavi UDP endpoint)
    python3 scripts/camera_trigger.py --connect udp:127.0.0.1:14551

    # brez kamere, samo preverjanje verige
    python3 scripts/camera_trigger.py --connect udp:127.0.0.1:14551 --dry-run
"""
from __future__ import annotations

import argparse
import json
import math
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.storage import (
    StorageUnavailableError, active_session_dir, ensure_writable,
    flight_log_dir,
)

# --- Neobvezne odvisnosti ---------------------------------------------------
try:
    from pymavlink import mavutil  # type: ignore
except Exception:  # pragma: no cover
    mavutil = None  # type: ignore

try:
    from picamera2 import Picamera2  # type: ignore
    _CAMERA_LIB = True
except Exception:  # pragma: no cover
    Picamera2 = None  # type: ignore
    _CAMERA_LIB = False

try:
    import piexif  # type: ignore
    _EXIF_LIB = True
except Exception:  # pragma: no cover
    piexif = None  # type: ignore
    _EXIF_LIB = False


# ---------------------------------------------------------------------------
# EXIF pomozne funkcije
# ---------------------------------------------------------------------------
def deg_to_dms_rational(value: float) -> tuple[tuple[int, int], ...]:
    """Pretvori stopinje v EXIF DMS obliko (trije racionalni ulomki).

    EXIF GPS polja hranijo stopinje/minute/sekunde kot pare (stevec,
    imenovalec). Sekunde zapisemo z imenovalcem 10000, kar da locljivost
    priblizno 3 mm --- vec, kot jih zmore GPS brez RTK.
    """
    value = abs(float(value))
    deg = int(value)
    minutes_full = (value - deg) * 60.0
    minutes = int(minutes_full)
    seconds = (minutes_full - minutes) * 60.0
    return ((deg, 1), (minutes, 1), (int(round(seconds * 10000)), 10000))


def build_exif(
    lat: Optional[float],
    lon: Optional[float],
    alt_m: Optional[float],
    yaw_deg: Optional[float],
    when: float,
) -> Optional[bytes]:
    """Zgradi EXIF blok z GPS georeferenco. Vrne ``None``, ce ni podatkov."""
    if not _EXIF_LIB or lat is None or lon is None:
        return None
    dt = datetime.fromtimestamp(when, timezone.utc)
    gps: dict[int, Any] = {
        piexif.GPSIFD.GPSVersionID: (2, 3, 0, 0),
        piexif.GPSIFD.GPSLatitudeRef: "N" if lat >= 0 else "S",
        piexif.GPSIFD.GPSLatitude: deg_to_dms_rational(lat),
        piexif.GPSIFD.GPSLongitudeRef: "E" if lon >= 0 else "W",
        piexif.GPSIFD.GPSLongitude: deg_to_dms_rational(lon),
        piexif.GPSIFD.GPSDateStamp: dt.strftime("%Y:%m:%d"),
        piexif.GPSIFD.GPSTimeStamp: (
            (dt.hour, 1), (dt.minute, 1), (int(round(dt.second * 100)), 100),
        ),
    }
    if alt_m is not None:
        # GPSAltitude je absolutna nadmorska visina (MSL).
        # GPSAltitudeRef: 0 = nad morsko gladino, 1 = pod njo.
        gps[piexif.GPSIFD.GPSAltitudeRef] = 0 if alt_m >= 0 else 1
        gps[piexif.GPSIFD.GPSAltitude] = (int(round(abs(alt_m) * 100)), 100)
    if yaw_deg is not None:
        gps[piexif.GPSIFD.GPSImgDirectionRef] = "T"  # true north
        gps[piexif.GPSIFD.GPSImgDirection] = (int(round(yaw_deg * 100)), 100)

    exif_ifd = {
        piexif.ExifIFD.DateTimeOriginal: dt.strftime("%Y:%m:%d %H:%M:%S"),
        piexif.ExifIFD.SubSecTimeOriginal: f"{when % 1:.3f}"[2:],
    }
    zeroth = {
        piexif.ImageIFD.Make: "Raspberry Pi",
        piexif.ImageIFD.Software: "camera_trigger.py (magistrska UAV)",
        piexif.ImageIFD.DateTime: dt.strftime("%Y:%m:%d %H:%M:%S"),
    }
    try:
        return piexif.dump({"0th": zeroth, "Exif": exif_ifd, "GPS": gps,
                            "1st": {}, "thumbnail": None})
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Stanje letalnika (posodablja ga bralna nit)
# ---------------------------------------------------------------------------
class VehicleState:
    """Zadnja znana pozicija in orientacija, varno za branje iz vec niti."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.lat: Optional[float] = None
        self.lon: Optional[float] = None
        self.alt_rel_m: Optional[float] = None
        self.alt_msl_m: Optional[float] = None
        self.yaw_deg: Optional[float] = None
        self.roll_deg: Optional[float] = None
        self.pitch_deg: Optional[float] = None
        self.pos_ts: Optional[float] = None
        self.att_ts: Optional[float] = None
        self.armed: bool = False

    def update_position(self, msg: Any, now: float) -> None:
        with self._lock:
            self.lat = msg.lat / 1e7
            self.lon = msg.lon / 1e7
            self.alt_msl_m = msg.alt / 1000.0
            self.alt_rel_m = msg.relative_alt / 1000.0
            self.pos_ts = now

    def update_attitude(self, msg: Any, now: float) -> None:
        with self._lock:
            self.roll_deg = math.degrees(msg.roll)
            self.pitch_deg = math.degrees(msg.pitch)
            self.yaw_deg = math.degrees(msg.yaw) % 360.0
            self.att_ts = now

    def snapshot(self, now: float) -> dict[str, Any]:
        with self._lock:
            return {
                "lat": self.lat, "lon": self.lon,
                "alt_rel_m": self.alt_rel_m, "alt_msl_m": self.alt_msl_m,
                "yaw_deg": self.yaw_deg, "roll_deg": self.roll_deg,
                "pitch_deg": self.pitch_deg,
                "pos_age_s": None if self.pos_ts is None else round(now - self.pos_ts, 3),
                "att_age_s": None if self.att_ts is None else round(now - self.att_ts, 3),
                "armed": self.armed,
            }


# ---------------------------------------------------------------------------
# Kamera --- z ali brez naprave
# ---------------------------------------------------------------------------
class Camera:
    """Ovoj nad zajemom slik.

    Prednost ima ``drone-camera`` ``/snapshot.jpg`` (preview okvir iz streama,
    isti kot rocni shutter). Ob zagonu caka, da stream postane dosegljiv, da
    ne ukrade Picamera2 in s tem ubije zivi predogled. Ce stream po cakanju
    se vedno ne tece, odpre neposredni ``picamera2``. ``available`` je
    ``False`` le, ce nobena pot ne dela.
    """

    def __init__(
        self,
        width: int = 0,
        height: int = 0,
        camera_num: int = 0,
        dry_run: bool = False,
        stream_url: str = "http://127.0.0.1:8090/snapshot.jpg",
        stream_wait_s: float = 20.0,
    ) -> None:
        self.width, self.height = width, height
        self.available = False
        self._cam: Any = None
        self._stream_url = stream_url
        self._mode = "none"
        self.reason = ""

        if dry_run:
            self.reason = "izbran --dry-run"
            return

        # Skupna pot z dashboardom: stream drzi kamero, snapshot = preview.
        if self._wait_for_stream(stream_wait_s):
            self._mode = "stream"
            self.available = True
            if not self.width or not self.height:
                self.width, self.height = 1280, 720
            print(f"[camera] uporabljam stream preview {self._stream_url}",
                  flush=True)
            return

        if _CAMERA_LIB:
            try:
                cam = Picamera2(camera_num)  # type: ignore[misc]
                props = getattr(cam, "camera_properties", None) or {}
                native = props.get("PixelArraySize")
                if (not width or not height) and native is not None:
                    width = int(native[0])
                    height = int(native[1])
                    self.width, self.height = width, height
                if not width or not height:
                    width, height = 3280, 2464
                    self.width, self.height = width, height
                cam.configure(cam.create_still_configuration(
                    main={"size": (width, height)}))
                cam.start()
                time.sleep(1.5)  # senzor potrebuje cas za auto-exposure
                self._cam = cam
                self._mode = "picamera2"
                self.available = True
                return
            except Exception as exc:
                self.reason = f"picamera2: {exc}"
        else:
            self.reason = ("picamera2 ni na voljo "
                           "(sudo apt install python3-picamera2)")

        if not self.reason:
            self.reason = "kamera ni na voljo"

    def _probe_stream(self) -> bool:
        try:
            import urllib.request
            with urllib.request.urlopen(self._stream_url, timeout=2.0) as r:
                data = r.read(16)
            return len(data) >= 2 and data[:2] == b"\xff\xd8"
        except Exception:
            return False

    def _wait_for_stream(self, timeout_s: float) -> bool:
        """Cakaj na drone-camera, da ne gre takoj v picamera2 (Device busy)."""
        deadline = time.time() + max(0.0, timeout_s)
        while True:
            if self._probe_stream():
                return True
            if time.time() >= deadline:
                return False
            time.sleep(0.5)

    def capture(self, path: Path) -> Optional[dict[str, float]]:
        """Zajame posnetek. Vrne case zajema ali ``None``, ce kamere ni."""
        if not self.available:
            return None
        t0 = time.time()
        try:
            if self._mode == "picamera2" and self._cam is not None:
                self._cam.capture_file(str(path))
            elif self._mode == "stream":
                import urllib.request
                with urllib.request.urlopen(self._stream_url, timeout=3.0) as r:
                    data = r.read()
                if len(data) < 2 or data[:2] != b"\xff\xd8":
                    raise RuntimeError("stream ni vrnil JPEG")
                path.write_bytes(data)
            else:
                return None
        except Exception as exc:
            print(f"[camera] zajem ni uspel: {exc}", file=sys.stderr)
            return None
        t1 = time.time()
        return {"t_capture": t0, "t_done": t1,
                "latency_ms": round((t1 - t0) * 1000.0, 1)}

    def close(self) -> None:
        if self._cam is not None:
            try:
                self._cam.stop()
                self._cam.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Glavna zanka
# ---------------------------------------------------------------------------
class CaptureAgent:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.state = VehicleState()
        configured_out = getattr(args, "out", None)
        self.configured_out = configured_out
        if configured_out:
            self.log_root: Optional[Path] = ensure_writable(
                Path(configured_out).expanduser())
        else:
            self.log_root = self._resolve_log_root()
        self.camera = Camera(
            width=args.width, height=args.height,
            camera_num=args.camera, dry_run=args.dry_run,
        )
        self.counter = 0
        self.stop = threading.Event()
        self._log_lock = threading.Lock()

    # -------- zapisnik --------

    def _resolve_log_root(self) -> Optional[Path]:
        if self.configured_out:
            try:
                return ensure_writable(Path(self.configured_out).expanduser())
            except StorageUnavailableError:
                return None
        try:
            return flight_log_dir(os.environ.get("UAV_STORAGE_ROOT") or "auto")
        except StorageUnavailableError:
            return None

    def _output_dir(self, when: float) -> Optional[Path]:
        self.log_root = self._resolve_log_root()
        if self.log_root is None:
            return None
        session = active_session_dir(self.log_root)
        if session is not None:
            return session
        day = datetime.fromtimestamp(when, timezone.utc).strftime("%Y%m%d")
        return ensure_writable(self.log_root / "unassigned-captures" / day)

    def record(self, log_path: Path, row: dict[str, Any]) -> None:
        with self._log_lock:
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    # -------- zajem --------

    def on_trigger(self, trigger: str, seq: Optional[int], t_event: float,
                   extra: Optional[dict[str, Any]] = None) -> None:
        out_dir = self._output_dir(t_event)
        if out_dir is None:
            print("[camera] zajem preskočen: USB medij ni na voljo.",
                  file=sys.stderr, flush=True)
            return
        self.counter += 1
        idx = self.counter
        stamp = datetime.fromtimestamp(t_event, timezone.utc).strftime("%Y%m%d-%H%M%S")
        name = f"img_{idx:05d}_{stamp}.jpg"
        image_dir = ensure_writable(out_dir / "images")
        path = image_dir / name
        log_path = out_dir / "captures.jsonl"

        st = self.state.snapshot(t_event)
        times = self.camera.capture(path)

        row: dict[str, Any] = {
            "index": idx,
            "trigger": trigger,
            "seq": seq,
            "t_event": round(t_event, 3),
            "file": f"images/{name}" if times else None,
            "captured": bool(times),
            **st,
        }
        if times:
            row.update({
                "t_capture": round(times["t_capture"], 3),
                "t_done": round(times["t_done"], 3),
                "capture_latency_ms": times["latency_ms"],
                "trigger_to_capture_ms": round((times["t_capture"] - t_event) * 1000, 1),
            })
            exif = build_exif(
                st["lat"], st["lon"], st["alt_msl_m"], st["yaw_deg"],
                times["t_capture"],
            )
            if exif is not None:
                try:
                    piexif.insert(exif, str(path))  # type: ignore[union-attr]
                    row["exif"] = "gps"
                except Exception as exc:
                    row["exif"] = f"napaka: {exc}"
            else:
                row["exif"] = "brez (ni piexif ali ni pozicije)"
        else:
            row["skip_reason"] = self.camera.reason or "kamera ni na voljo"
        if extra:
            row.update(extra)

        self.record(log_path, row)
        pos = (f"{st['lat']:.7f},{st['lon']:.7f}"
               if st["lat"] is not None else "brez pozicije")
        mark = "*" if times else "-"
        print(f"[{mark}] #{idx} {trigger} seq={seq} {pos} "
              f"alt={st['alt_rel_m']} yaw={st['yaw_deg']}", flush=True)

    # -------- MAVLink zanka --------

    def run(self) -> int:
        if mavutil is None:
            print("pymavlink ni namescen: pip install pymavlink", file=sys.stderr)
            return 2

        if not self.camera.available:
            print(f"[camera] zajem slik IZKLOPLJEN --- {self.camera.reason}. "
                  "Dogodki se bodo zapisovali, ko bo USB medij na voljo.",
                  flush=True)
        elif self.log_root is None:
            print("[camera] USB medij ni na voljo; zajem je začasno izklopljen.",
                  file=sys.stderr, flush=True)
        else:
            wh = f"{self.camera.width}x{self.camera.height}"
            if not self.camera.width or not self.camera.height:
                wh = "full-sensor"
            print(
                f"[camera] pripravljena {wh} (vir={self.camera._mode})",
                flush=True,
            )

        print(f"[mavlink] povezujem se na {self.args.connect} ...", flush=True)
        try:
            mav = mavutil.mavlink_connection(
                self.args.connect, baud=self.args.baud, source_system=200)
        except Exception as exc:
            print(f"povezava ni uspela: {exc}", file=sys.stderr)
            return 2

        if mav.wait_heartbeat(timeout=30) is None:
            print("ni HEARTBEAT-a v 30 s", file=sys.stderr)
            return 3
        print(f"[mavlink] heartbeat: sistem {mav.target_system}", flush=True)

        try:
            mav.mav.request_data_stream_send(
                mav.target_system, mav.target_component,
                mavutil.mavlink.MAV_DATA_STREAM_ALL, 5, 1)
        except Exception:
            pass

        seen_reached: set[int] = set()

        while not self.stop.is_set():
            try:
                msg = mav.recv_match(blocking=True, timeout=1.0)
            except Exception as exc:
                print(f"[mavlink] napaka pri branju: {exc}", file=sys.stderr)
                time.sleep(1.0)
                continue
            if msg is None:
                continue

            now = time.time()
            t = msg.get_type()

            if t == "GLOBAL_POSITION_INT":
                self.state.update_position(msg, now)
            elif t == "ATTITUDE":
                self.state.update_attitude(msg, now)
            elif t == "HEARTBEAT":
                armed = bool(msg.base_mode & 0b1000_0000)
                if armed != self.state.armed:
                    self.state.armed = armed
                    # Nov let -> nova stevilcna serija, da se imena ne
                    # prekrivajo z prejsnjim letom.
                    if armed:
                        seen_reached.clear()
                    print(f"[mavlink] {'ARMED' if armed else 'DISARMED'}", flush=True)

            elif t == "CAMERA_TRIGGER":
                # Primarni, deterministicni vir: avtopilot je sprozil kamero.
                self.on_trigger("CAMERA_TRIGGER", int(getattr(msg, "seq", -1)), now)

            elif t == "MISSION_ITEM_REACHED":
                seq = int(msg.seq)
                if self.args.on_waypoint and seq not in seen_reached:
                    seen_reached.add(seq)
                    self.on_trigger("MISSION_ITEM_REACHED", seq, now)

            elif t == "COMMAND_LONG":
                if int(getattr(msg, "command", 0)) == \
                        mavutil.mavlink.MAV_CMD_IMAGE_START_CAPTURE:
                    count = int(getattr(msg, "param3", 1) or 1)
                    for _ in range(max(1, count)):
                        self.on_trigger("IMAGE_START_CAPTURE", None, time.time())

        self.camera.close()
        print(f"[konec] {self.counter} shranjenih sprožitev",
              flush=True)
        return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Deterministicni zajem slik, sinhroniziran z MAVLink.")
    ap.add_argument("--connect", default="udp:127.0.0.1:14551",
                    help="MAVLink endpoint (privzeto udp:127.0.0.1:14551)")
    ap.add_argument("--baud", type=int, default=921600,
                    help="hitrost, ce je endpoint serijski")
    ap.add_argument(
        "--out", default=None,
        help="koren flightlogs (privzeto: samodejno zaznan USB ključek)")
    ap.add_argument("--width", type=int, default=0,
                    help="širina stilla (0 = PixelArraySize senzorja)")
    ap.add_argument("--height", type=int, default=0,
                    help="višina stilla (0 = PixelArraySize senzorja)")
    ap.add_argument("--camera", type=int, default=0,
                    help="indeks kamere (0 ali 1 na RPi 5)")
    ap.add_argument("--dry-run", action="store_true",
                    help="ne zajemaj slik, samo zapisuj dogodke")
    ap.add_argument("--no-waypoint-trigger", dest="on_waypoint",
                    action="store_false",
                    help="ne zajemaj ob MISSION_ITEM_REACHED (samo CAMERA_TRIGGER)")
    ap.set_defaults(on_waypoint=True)
    args = ap.parse_args(argv)

    agent = CaptureAgent(args)

    def _sig(_signum: int, _frame: Any) -> None:
        print("\n[signal] zakljucujem ...", flush=True)
        agent.stop.set()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    return agent.run()


if __name__ == "__main__":
    sys.exit(main())
