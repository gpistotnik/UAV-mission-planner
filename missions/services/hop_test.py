"""Samodejni testni skok: vzlet → 2 m sever → slika → nazaj → slika → pristanek.

GUIDED zaporedje ob obstoječem hover-testu. Namen: preveriti lateralni
position hold in sprožitev kamere na dveh točkah, brez polne misije.

Varnost: med vzpenjanjem in GOTO se spremlja horizontalna hitrost ter
odmik od pričakovane točke; ob prekoračitvi → LAND.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Optional

from .test_flight import (
    MAX_ALTITUDE_M, MIN_ALTITUDE_M, Check, checks_pass, evaluate_checks,
)

MAX_LEG_M = 5.0
MIN_LEG_M = 0.5


class HopPhase:
    IDLE = "IDLE"
    PREFLIGHT = "PREFLIGHT"
    COUNTDOWN = "COUNTDOWN"
    MODE = "MODE"
    ARM = "ARM"
    TAKEOFF = "TAKEOFF"
    CLIMB = "CLIMB"
    GOTO_P1 = "GOTO_P1"
    CAPTURE_1 = "CAPTURE_1"
    GOTO_HOME = "GOTO_HOME"
    CAPTURE_2 = "CAPTURE_2"
    LAND = "LAND"
    DISARM = "DISARM"
    DONE = "DONE"
    ABORTED = "ABORTED"
    FAILED = "FAILED"

    TERMINAL = frozenset({DONE, ABORTED, FAILED})
    AIRBORNE = frozenset({
        ARM, TAKEOFF, CLIMB, GOTO_P1, CAPTURE_1, GOTO_HOME, CAPTURE_2,
        LAND, DISARM,
    })

    LABELS = {
        IDLE: "pripravljen",
        PREFLIGHT: "predpoletne preverbe",
        COUNTDOWN: "odštevanje",
        MODE: "preklop v GUIDED",
        ARM: "armanje",
        TAKEOFF: "ukaz za vzlet",
        CLIMB: "vzpenjanje",
        GOTO_P1: "let na točko 1",
        CAPTURE_1: "slika na točki 1",
        GOTO_HOME: "vrnitev na izhodišče",
        CAPTURE_2: "slika na izhodišču",
        LAND: "pristajanje",
        DISARM: "čakanje na dis-arm",
        DONE: "končano",
        ABORTED: "prekinjeno",
        FAILED: "neuspešno",
    }


@dataclass
class HopTestParams:
    """Parametri skoka. Privzeti GPS pragovi so strožji kot pri hover-testu."""
    altitude_m: float = 2.0
    leg_m: float = 2.0
    countdown_s: float = 5.0

    min_satellites: int = 12
    max_hdop: float = 1.2
    min_voltage_v: Optional[float] = None
    require_gps: bool = True

    preflight_timeout_s: float = 60.0
    mode_timeout_s: float = 10.0
    climb_timeout_s: float = 30.0
    leg_timeout_s: float = 45.0
    disarm_timeout_s: float = 120.0
    capture_settle_s: float = 1.0

    arrive_radius_m: float = 0.5
    arrive_hold_s: float = 0.6
    reach_fraction: float = 0.95
    tick_s: float = 0.25

    # Drift abort
    max_groundspeed_ms: float = 1.5
    max_offtrack_m: float = 3.0

    def clamp(self) -> "HopTestParams":
        self.altitude_m = max(MIN_ALTITUDE_M, min(MAX_ALTITUDE_M,
                                                  float(self.altitude_m)))
        self.leg_m = max(MIN_LEG_M, min(MAX_LEG_M, float(self.leg_m)))
        self.countdown_s = max(0.0, min(30.0, float(self.countdown_s)))
        self.min_satellites = max(0, int(self.min_satellites))
        self.max_hdop = max(0.1, float(self.max_hdop))
        self.arrive_radius_m = max(0.2, min(2.0, float(self.arrive_radius_m)))
        self.tick_s = max(0.01, min(1.0, float(self.tick_s)))
        self.max_groundspeed_ms = max(0.3, float(self.max_groundspeed_ms))
        self.max_offtrack_m = max(1.0, float(self.max_offtrack_m))
        return self


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(min(1.0, a)))


def offset_lat_lon(lat: float, lon: float,
                   north_m: float, east_m: float) -> tuple[float, float]:
    """Odmik v metrih → nova lat/lon (približek za kratke razdalje)."""
    dlat = north_m / 111_320.0
    cos_lat = math.cos(math.radians(lat))
    dlon = east_m / (111_320.0 * cos_lat) if abs(cos_lat) > 1e-6 else 0.0
    return lat + dlat, lon + dlon


class HopTestRunner:
    """Izvede skok vzlet–P1–slika–home–slika–pristanek."""

    PROFILE = "hop"
    _tls = threading.local()

    def __init__(self, bridge: Any) -> None:
        self.bridge = bridge
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._abort = threading.Event()
        self._run_id = 0
        self._phase = HopPhase.IDLE
        self._phase_since = time.time()
        self._started_at: Optional[float] = None
        self._message = ""
        self._events: list[dict[str, Any]] = []
        self._checks: list[Check] = []
        self._params = HopTestParams()
        self._peak_alt = 0.0
        self._origin_lat: Optional[float] = None
        self._origin_lon: Optional[float] = None
        self._origin_alt: Optional[float] = None

    @property
    def active(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def status(self) -> dict[str, Any]:
        with self._lock:
            snap = self.bridge.get_snapshot()
            gps = snap.get("gps") or {}
            hb = snap.get("heartbeat") or {}
            now = time.time()
            stuck = (
                self._phase not in HopPhase.TERMINAL
                and self._phase != HopPhase.IDLE
            )
            return {
                "profile": self.PROFILE,
                "active": self.active,
                "phase": self._phase,
                "phase_label": HopPhase.LABELS.get(self._phase, self._phase),
                "phase_elapsed_s": round(now - self._phase_since, 1),
                "elapsed_s": (None if self._started_at is None
                              else round(now - self._started_at, 1)),
                "message": self._message,
                "terminal": self._phase in HopPhase.TERMINAL,
                "abortable": self.active or stuck or bool(hb.get("armed")),
                "force_cancellable": self.active or stuck or bool(hb.get("armed")),
                "events": list(self._events),
                "checks": [asdict(c) for c in self._checks],
                "checks_pass": (checks_pass(self._checks)
                                if self._checks else False),
                "params": asdict(self._params),
                "altitude_m": gps.get("rel_alt_m"),
                "target_altitude_m": self._params.altitude_m,
                "peak_altitude_m": round(self._peak_alt, 2),
                "armed": bool(hb.get("armed")),
                "mode": hb.get("mode"),
                "origin": {
                    "lat": self._origin_lat,
                    "lon": self._origin_lon,
                    "alt_m": self._origin_alt,
                },
            }

    def start(self, params: Optional[HopTestParams] = None) -> dict[str, Any]:
        with self._lock:
            if self.active:
                return {"ok": False, "error": "Testni skok že teče.",
                        "status": self.status()}
            if not self.bridge.is_connected():
                return {"ok": False, "error": "Ni MAVLink povezave s Pixhawkom."}

            self._params = (params or HopTestParams()).clamp()
            self._run_id += 1
            run_id = self._run_id
            self._abort = threading.Event()
            self._events = []
            self._checks = []
            self._peak_alt = 0.0
            self._origin_lat = self._origin_lon = self._origin_alt = None
            self._started_at = time.time()
            self._set_phase(HopPhase.PREFLIGHT, "Preverjam predpoletne pogoje.")
            self._thread = threading.Thread(
                target=self._run, args=(run_id,),
                daemon=True, name="hop-test")
            self._thread.start()
        return {"ok": True, "status": self.status()}

    def abort(self, reason: str = "prekinil uporabnik") -> dict[str, Any]:
        with self._lock:
            if not self.active:
                if bool((self.bridge.get_snapshot().get("heartbeat") or {})
                        .get("armed")):
                    res = self.bridge.set_mode("LAND")
                    self._log("abort",
                              f"Ni aktivnega skoka; ukaz LAND ({reason}).",
                              ok=bool(res.get("ok")))
                    return {"ok": bool(res.get("ok")), "landing": True,
                            "error": res.get("error"), "status": self.status()}
                # Če je nit že mrtev, faza pa še kaže PREFLIGHT ipd.,
                # vseeno odklepaj UI — sicer ostane "stuck v loopu".
                if self._phase not in HopPhase.TERMINAL and self._phase != HopPhase.IDLE:
                    self._phase = HopPhase.ABORTED
                    self._phase_since = time.time()
                    self._message = f"Prekinjeno ({reason})."
                    self._log("abort", f"Prekinitev brez žive niti: {reason}.")
                    return {"ok": True, "status": self.status()}
                return {"ok": False, "error": "Testni skok ne teče.",
                        "status": self.status()}
            self._log("abort", f"Zahtevana prekinitev: {reason}.")
            # Pred armanjem takoj prestavi fazo, da polling ne kaže več
            # večne PREFLIGHT zanke, medtem ko nit še spi na tick.
            if self._phase not in HopPhase.AIRBORNE and self._phase not in HopPhase.TERMINAL:
                self._phase = HopPhase.ABORTED
                self._phase_since = time.time()
                self._message = "Prekinjeno pred armanjem."
            # set znotraj locka: sicer lahko preflight med sprostitvijo
            # locka in set() še enkrat uspešno prestane preverbe.
            self._abort.set()
        return {"ok": True, "status": self.status()}

    def force_abort(self, reason: str = "force cancel") -> dict[str, Any]:
        """Takoj odklene UI in prekine zaporedje, tudi ce je nit obvisela."""
        with self._lock:
            snap = self.bridge.get_snapshot()
            hb = snap.get("heartbeat") or {}
            armed = bool(hb.get("armed"))
            was_busy = (
                self.active
                or (self._phase not in HopPhase.TERMINAL
                    and self._phase != HopPhase.IDLE)
                or armed
            )
            if not was_busy:
                return {"ok": False, "error": "Ni kaj prekiniti.",
                        "forced": True, "status": self.status()}

            self._run_id += 1
            self._abort.set()
            self._thread = None
            self._phase = HopPhase.ABORTED
            self._phase_since = time.time()
            self._message = f"Vsili prekinitev ({reason})."
            self._log("force_abort", self._message)

        landing = False
        disarm = False
        land_err = None
        if armed:
            res = self.bridge.set_mode("LAND")
            landing = True
            if not res.get("ok"):
                land_err = res.get("error")
                self._log("force_abort",
                          f"LAND zavrnjen: {land_err} — poskušam force disarm.",
                          ok=False)
            if bool((self.bridge.get_snapshot().get("heartbeat") or {})
                    .get("armed")):
                dres = self.bridge.arm(False, force=True)
                disarm = True
                self._log(
                    "force_abort",
                    f"Force disarm "
                    f"({'ok' if dres.get('ok') else dres.get('error')}).",
                    ok=bool(dres.get("ok")))

        return {
            "ok": True,
            "forced": True,
            "landing": landing,
            "force_disarm": disarm,
            "error": land_err,
            "status": self.status(),
        }

    # -------- interno --------

    def _set_phase(self, phase: str, message: str = "") -> None:
        with self._lock:
            rid = getattr(self._tls, "run_id", None)
            if rid is not None and rid != self._run_id:
                return
            self._phase = phase
            self._phase_since = time.time()
            if message:
                self._message = message
        rid = getattr(self._tls, "run_id", None)
        if rid is None or rid == self._run_id:
            self._log(phase, message or HopPhase.LABELS.get(phase, phase))

    def _log(self, phase: str, message: str, ok: Optional[bool] = None) -> None:
        with self._lock:
            self._events.append({
                "t": round(time.time(), 2),
                "since_start_s": (None if self._started_at is None
                                  else round(time.time() - self._started_at, 1)),
                "phase": phase,
                "message": message,
                "ok": ok,
            })

    def _sleep(self, seconds: float) -> bool:
        return self._abort.wait(timeout=seconds)

    def _is_current(self) -> bool:
        rid = getattr(self._tls, "run_id", None)
        return rid is not None and rid == self._run_id and not self._abort.is_set()

    def _alt(self) -> Optional[float]:
        gps = self.bridge.get_snapshot().get("gps") or {}
        a = gps.get("rel_alt_m")
        if a is not None:
            with self._lock:
                self._peak_alt = max(self._peak_alt, float(a))
        return a

    def _armed(self) -> bool:
        hb = self.bridge.get_snapshot().get("heartbeat") or {}
        return bool(hb.get("armed"))

    def _lat_lon_alt(self) -> tuple[Optional[float], Optional[float],
                                    Optional[float]]:
        gps = self.bridge.get_snapshot().get("gps") or {}
        return gps.get("lat"), gps.get("lon"), gps.get("rel_alt_m")

    def _groundspeed(self) -> Optional[float]:
        snap = self.bridge.get_snapshot()
        vfr = snap.get("vfr_hud") or {}
        gs = vfr.get("groundspeed_ms")
        if gs is not None:
            return float(gs)
        gps = snap.get("gps") or {}
        vx, vy = gps.get("vx_ms"), gps.get("vy_ms")
        if vx is not None and vy is not None:
            return math.hypot(float(vx), float(vy))
        return None

    def _fail(self, message: str) -> None:
        if self._phase in HopPhase.AIRBORNE and self._armed():
            self._log(self._phase, f"{message} → sprožam pristanek.", ok=False)
            self._land_and_wait(reason=message)
            self._set_phase(HopPhase.FAILED, message)
        else:
            self._set_phase(HopPhase.FAILED, message)

    def _run(self, run_id: int) -> None:
        self._tls.run_id = run_id
        p = self._params
        try:
            if not self._phase_preflight(p):
                return
            if not self._phase_countdown(p):
                return
            if not self._phase_mode(p):
                return
            if not self._phase_arm(p):
                return
            if not self._phase_takeoff(p):
                return
            if not self._phase_climb(p):
                return
            if not self._phase_goto_p1(p):
                return
            if not self._phase_capture(HopPhase.CAPTURE_1, "točki 1", p):
                return
            if not self._phase_goto_home(p):
                return
            if not self._phase_capture(HopPhase.CAPTURE_2, "izhodišču", p):
                return
            self._land_and_wait(reason="konec skoka")
            if run_id != self._run_id:
                return
            if self._abort.is_set():
                self._set_phase(HopPhase.ABORTED,
                                "Zaporedje prekinjeno; dron pristal.")
            else:
                self._set_phase(
                    HopPhase.DONE,
                    f"Skok končan. Najvišja višina {self._peak_alt:.1f} m, "
                    f"odmik {p.leg_m:.1f} m sever.")
        except Exception as exc:  # pragma: no cover
            if run_id != self._run_id:
                return
            self._log(self._phase, f"Nepričakovana napaka: {exc}", ok=False)
            try:
                if self._armed():
                    self.bridge.set_mode("LAND")
            finally:
                self._set_phase(HopPhase.FAILED,
                                f"Nepričakovana napaka: {exc}")
        finally:
            if getattr(self._tls, "run_id", None) == run_id:
                self._tls.run_id = None

    def _phase_preflight(self, p: HopTestParams) -> bool:
        self._set_phase(HopPhase.PREFLIGHT,
                        "Čakam na izpolnjene predpoletne pogoje.")
        # evaluate_checks pričakuje TestFlightParams-kompatibilna polja
        from .test_flight import TestFlightParams
        tf = TestFlightParams(
            altitude_m=p.altitude_m,
            countdown_s=p.countdown_s,
            min_satellites=p.min_satellites,
            max_hdop=p.max_hdop,
            min_voltage_v=p.min_voltage_v,
            require_gps=p.require_gps,
        )
        deadline = time.time() + p.preflight_timeout_s
        while time.time() < deadline:
            if self._abort.is_set() or not self._is_current():
                self._set_phase(HopPhase.ABORTED, "Prekinjeno pred armanjem.")
                return False
            with self._lock:
                self._checks = evaluate_checks(
                    self.bridge.get_snapshot(), tf)
                ok = checks_pass(self._checks)
            if ok:
                self._log(HopPhase.PREFLIGHT,
                          "Vse predpoletne preverbe uspešne.", ok=True)
                return True
            if self._sleep(p.tick_s):
                self._set_phase(HopPhase.ABORTED, "Prekinjeno pred armanjem.")
                return False
        failed = [c.label for c in self._checks if c.required and not c.ok]
        self._set_phase(
            HopPhase.FAILED,
            "Predpoletne preverbe niso uspele v "
            f"{p.preflight_timeout_s:.0f} s: {', '.join(failed) or 'neznano'}.")
        return False

    def _phase_countdown(self, p: HopTestParams) -> bool:
        if p.countdown_s <= 0:
            return True
        self._set_phase(HopPhase.COUNTDOWN,
                        f"Vzlet čez {p.countdown_s:.0f} s. Odmakni se.")
        end = time.time() + p.countdown_s
        while time.time() < end:
            if self._sleep(min(p.tick_s, max(0.0, end - time.time()))):
                self._set_phase(HopPhase.ABORTED, "Prekinjeno med odštevanjem.")
                return False
        return True

    def _phase_mode(self, p: HopTestParams) -> bool:
        if self._abort.is_set():
            self._set_phase(HopPhase.ABORTED, "Prekinjeno pred armanjem.")
            return False
        self._set_phase(HopPhase.MODE, "Preklapljam v GUIDED.")
        res = self.bridge.set_mode("GUIDED")
        if self._abort.is_set():
            self._set_phase(HopPhase.ABORTED, "Prekinjeno pred armanjem.")
            return False
        if not res.get("ok"):
            self._set_phase(HopPhase.FAILED,
                            f"Preklop v GUIDED ni uspel: {res.get('error')}")
            return False
        self._log(HopPhase.MODE, "Način GUIDED sprejet.", ok=True)
        return True

    def _phase_arm(self, p: HopTestParams) -> bool:
        if self._abort.is_set():
            self._set_phase(HopPhase.ABORTED, "Prekinjeno pred armanjem.")
            return False
        self._set_phase(HopPhase.ARM, "Armiram.")
        res = self.bridge.arm(True)
        if not res.get("ok"):
            texts = "; ".join(
                s.get("text", "") for s in (res.get("statustexts") or []))
            self._set_phase(
                HopPhase.FAILED,
                f"Armanje zavrnjeno: {res.get('error')}"
                + (f" ({texts})" if texts else ""))
            return False
        if self._abort.is_set():
            self._land_and_wait(reason="prekinitev takoj po armanju")
            self._set_phase(HopPhase.ABORTED, "Prekinjeno po armanju.")
            return False
        self._log(HopPhase.ARM, "Armirano.", ok=True)
        return True

    def _phase_takeoff(self, p: HopTestParams) -> bool:
        self._set_phase(HopPhase.TAKEOFF,
                        f"Ukaz za vzlet na {p.altitude_m:.1f} m.")
        res = self.bridge.takeoff(p.altitude_m)
        if not res.get("ok"):
            self._fail(f"Ukaz za vzlet zavrnjen: {res.get('error')}")
            return False
        self._log(HopPhase.TAKEOFF, "Ukaz za vzlet sprejet.", ok=True)
        return True

    def _phase_climb(self, p: HopTestParams) -> bool:
        target = p.altitude_m * p.reach_fraction
        self._set_phase(HopPhase.CLIMB, f"Vzpenjam se na {p.altitude_m:.1f} m.")
        deadline = time.time() + p.climb_timeout_s
        while time.time() < deadline:
            if self._abort.is_set():
                self._land_and_wait(reason="prekinitev med vzpenjanjem")
                self._set_phase(HopPhase.ABORTED, "Prekinjeno med vzpenjanjem.")
                return False
            if self._drift_unsafe(p, track_lat=None, track_lon=None):
                return False
            alt = self._alt()
            if alt is not None and alt >= target:
                lat, lon, _ = self._lat_lon_alt()
                if lat is None or lon is None:
                    self._fail("Ni GPS pozicije ob doseženi višini.")
                    return False
                with self._lock:
                    self._origin_lat = float(lat)
                    self._origin_lon = float(lon)
                    self._origin_alt = float(alt)
                self._log(HopPhase.CLIMB,
                          f"Dosežena višina {alt:.2f} m; izhodišče "
                          f"{lat:.7f},{lon:.7f}.", ok=True)
                return True
            if self._sleep(p.tick_s):
                self._land_and_wait(reason="prekinitev med vzpenjanjem")
                self._set_phase(HopPhase.ABORTED, "Prekinjeno med vzpenjanjem.")
                return False
        self._fail(f"Ciljna višina ni bila dosežena v {p.climb_timeout_s:.0f} s.")
        return False

    def _phase_goto_p1(self, p: HopTestParams) -> bool:
        assert self._origin_lat is not None and self._origin_lon is not None
        tlat, tlon = offset_lat_lon(
            self._origin_lat, self._origin_lon, p.leg_m, 0.0)
        alt = self._origin_alt if self._origin_alt is not None else p.altitude_m
        self._set_phase(
            HopPhase.GOTO_P1,
            f"Letim {p.leg_m:.1f} m sever (točka 1).")
        return self._goto_and_arrive(
            tlat, tlon, alt, p,
            phase=HopPhase.GOTO_P1,
            track_lat=self._origin_lat,
            track_lon=self._origin_lon,
            offtrack_budget_m=p.leg_m + p.max_offtrack_m,
        )

    def _phase_goto_home(self, p: HopTestParams) -> bool:
        assert self._origin_lat is not None and self._origin_lon is not None
        alt = self._origin_alt if self._origin_alt is not None else p.altitude_m
        self._set_phase(HopPhase.GOTO_HOME, "Vračam se na izhodišče (točka 2).")
        # Med vrnitvijo dovolimo odmik do P1 + buffer
        return self._goto_and_arrive(
            self._origin_lat, self._origin_lon, alt, p,
            phase=HopPhase.GOTO_HOME,
            track_lat=self._origin_lat,
            track_lon=self._origin_lon,
            offtrack_budget_m=p.leg_m + p.max_offtrack_m,
        )

    def _goto_and_arrive(
        self,
        tlat: float,
        tlon: float,
        alt_m: float,
        p: HopTestParams,
        *,
        phase: str,
        track_lat: Optional[float],
        track_lon: Optional[float],
        offtrack_budget_m: Optional[float] = None,
    ) -> bool:
        res = self.bridge.goto_global(tlat, tlon, alt_m)
        if not res.get("ok"):
            self._fail(f"GOTO zavrnjen: {res.get('error')}")
            return False
        # ArduPilot včasih potrebuje ponavljanje setpointa
        deadline = time.time() + p.leg_timeout_s
        inside_since: Optional[float] = None
        last_send = 0.0
        while time.time() < deadline:
            if self._abort.is_set():
                self._land_and_wait(reason=f"prekinitev v {phase}")
                self._set_phase(HopPhase.ABORTED, f"Prekinjeno v {phase}.")
                return False
            budget = offtrack_budget_m if offtrack_budget_m is not None \
                else p.max_offtrack_m
            if self._drift_unsafe(p, track_lat=track_lat, track_lon=track_lon,
                                  offtrack_m=budget):
                return False
            now = time.time()
            if now - last_send >= 1.0:
                self.bridge.goto_global(tlat, tlon, alt_m)
                last_send = now
            lat, lon, _ = self._lat_lon_alt()
            if lat is not None and lon is not None:
                dist = haversine_m(lat, lon, tlat, tlon)
                if dist <= p.arrive_radius_m:
                    if inside_since is None:
                        inside_since = now
                    elif now - inside_since >= p.arrive_hold_s:
                        self._log(phase, f"Prispel ({dist:.2f} m od cilja).",
                                  ok=True)
                        return True
                else:
                    inside_since = None
            if self._sleep(p.tick_s):
                self._land_and_wait(reason=f"prekinitev v {phase}")
                self._set_phase(HopPhase.ABORTED, f"Prekinjeno v {phase}.")
                return False
        self._fail(f"Cilj ni dosežen v {p.leg_timeout_s:.0f} s ({phase}).")
        return False

    def _phase_capture(self, phase: str, where: str, p: HopTestParams) -> bool:
        self._set_phase(phase, f"Zajemam sliko na {where}.")
        if p.capture_settle_s > 0 and self._sleep(p.capture_settle_s):
            self._land_and_wait(reason=f"prekinitev pred sliko na {where}")
            self._set_phase(HopPhase.ABORTED, "Prekinjeno pred zajemom.")
            return False
        res = self.bridge.capture_image(1)
        # Zajem dela companion (camera_trigger), ne FC. Ukaz gre na žico;
        # FC brez CAM backend-a pogosto vrne FAILED / ne ACK-a — to ni napaka.
        if not res.get("ok"):
            err = str(res.get("error") or "")
            result_name = str(res.get("result_name") or "")
            soft = result_name in (
                "FAILED", "UNSUPPORTED", "DENIED", "TEMPORARILY_REJECTED",
            ) or "Ni ACK" in err
            if soft:
                self._log(phase, f"Slika sprožena (FC: {err}).", ok=True)
            else:
                self._fail(f"Zajem slike na {where} zavrnjen: {err}")
                return False
        else:
            self._log(phase, f"Slika na {where} sprožena.", ok=True)
        # Kratek čas za dejanski capture
        if self._sleep(0.5):
            self._land_and_wait(reason="prekinitev po zajemu")
            self._set_phase(HopPhase.ABORTED, "Prekinjeno po zajemu.")
            return False
        return True

    def _drift_unsafe(
        self,
        p: HopTestParams,
        *,
        track_lat: Optional[float],
        track_lon: Optional[float],
        offtrack_m: Optional[float] = None,
    ) -> bool:
        """Vrne True in sproži fail, če je drift prevelik."""
        gs = self._groundspeed()
        if gs is not None and gs > p.max_groundspeed_ms:
            self._fail(
                f"Previsoka horizontalna hitrost {gs:.1f} m/s "
                f"(max {p.max_groundspeed_ms:.1f}) — pristanek.")
            return True
        if track_lat is None or track_lon is None:
            return False
        lat, lon, _ = self._lat_lon_alt()
        if lat is None or lon is None:
            return False
        # Med GOTO_P1: razdalja od izhodišča ne sme močno preseči leg+budget
        dist = haversine_m(lat, lon, track_lat, track_lon)
        limit = offtrack_m if offtrack_m is not None else p.max_offtrack_m
        # V climb fazi je track None — tukaj za GOTO: če smo preveč stran
        # od referenčne točke (home) onkraj leg+offtrack
        if dist > limit:
            self._fail(
                f"Prevelik odmik od poti ({dist:.1f} m > {limit:.1f} m) "
                "— pristanek.")
            return True
        return False

    def _land_and_wait(self, reason: str) -> bool:
        p = self._params
        self._set_phase(HopPhase.LAND, f"Pristajam ({reason}).")
        res = self.bridge.set_mode("LAND")
        if not res.get("ok"):
            self._log(HopPhase.LAND,
                      f"LAND zavrnjen ({res.get('error')}), poskušam RTL.",
                      ok=False)
            res = self.bridge.set_mode("RTL")
            if not res.get("ok"):
                self._log(HopPhase.LAND,
                          "Tudi RTL zavrnjen — potreben je ročni poseg.",
                          ok=False)
                return False
        self._set_phase(HopPhase.DISARM, "Čakam na dis-arm po pristanku.")
        deadline = time.time() + p.disarm_timeout_s
        while time.time() < deadline:
            rid = getattr(self._tls, "run_id", None)
            if rid is not None and rid != self._run_id:
                return False
            if not self._armed():
                self._log(HopPhase.DISARM, "Letalnik dis-armiran.", ok=True)
                return True
            time.sleep(p.tick_s)
        self._log(HopPhase.DISARM,
                  f"Ni dis-arma v {p.disarm_timeout_s:.0f} s.", ok=False)
        return False


_hop_runner: Optional[HopTestRunner] = None
_hop_lock = threading.Lock()


def get_hop_runner() -> HopTestRunner:
    global _hop_runner
    with _hop_lock:
        if _hop_runner is None:
            from .mavlink_bridge import get_bridge
            _hop_runner = HopTestRunner(get_bridge())
        return _hop_runner
