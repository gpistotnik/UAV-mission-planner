"""Samodejni testni polet: vzlet, lebdenje, pristanek.

Namen je najpreprostejsi mozen avtonomni manever --- dron se dvigne nekaj
metrov, obvisi in pristane --- ki pa preveri **celotno verigo**: predpoletne
preverbe, preklop nacina, armanje, ukaz za vzlet, spremljanje visine,
pristanek in dis-arm. To je prvi test, ki ga je smiselno pognati po
zakljuceni nastavitvi krmilnika, in hkrati najmanj tvegan.

Zaporedje (nacin ``GUIDED``)::

    PREFLIGHT -> COUNTDOWN -> MODE(GUIDED) -> ARM -> TAKEOFF
              -> CLIMB -> HOVER -> LAND -> DISARM -> DONE

Zasnova
-------
Celotno zaporedje je **stroj stanj** v ozadnji niti, ki komunicira samo prek
ozkega vmesnika do mostu (``get_snapshot``, ``set_mode``, ``arm``,
``takeoff``). Zaradi tega ga je mogoce v celoti testirati z dvojnikom mostu,
brez letalnika --- kar je pri kodi, ki zavrti propelerje, edini sprejemljiv
nacin razvoja.

Varnostna nacela, vgrajena v stroj stanj:

1. **Nobenega ukaza brez predpoletnih preverb.** Zaporedje se ne premakne iz
   ``PREFLIGHT``, dokler niso vse obvezne preverbe zelene.
2. **Vsak korak ima timeout.** Ce se dron ne dvigne, ne armira ali ne
   pristane v pricakovanem casu, se zaporedje ne obesi --- ukrepa.
3. **Odpoved v zraku pomeni pristanek, ne ustavitev.** Ce katerikoli korak
   po armanju spodleti, stroj stanj preklopi v ``LAND``. Edini nacin, da
   koda pusti dron v zraku brez nadzora, bi bil, da ob napaki ne stori nic.
4. **Prekinitev je vedno na voljo.** ``abort()`` deluje v vsaki fazi; na
   tleh ustavi zaporedje, v zraku sprozi pristanek.
5. **Omejena visina.** Ciljna visina je strojno omejena na
   ``MAX_ALTITUDE_M`` --- to ni orodje za misije.
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

# Trdna zgornja meja za testni poskok. Namenoma nizka: cilj je preveriti
# verigo ukazov, ne leteti misije.
MAX_ALTITUDE_M = 10.0
MIN_ALTITUDE_M = 1.0
MAX_HOVER_S = 60.0


class Phase:
    """Faze zaporedja. Navadne konstante, da so neposredno JSON-serializabilne."""
    IDLE = "IDLE"
    PREFLIGHT = "PREFLIGHT"
    COUNTDOWN = "COUNTDOWN"
    MODE = "MODE"
    ARM = "ARM"
    TAKEOFF = "TAKEOFF"
    CLIMB = "CLIMB"
    HOVER = "HOVER"
    LAND = "LAND"
    DISARM = "DISARM"
    DONE = "DONE"
    ABORTED = "ABORTED"
    FAILED = "FAILED"

    TERMINAL = frozenset({DONE, ABORTED, FAILED})
    #: Faze, v katerih je letalnik lahko ze armiran --- odpoved pomeni pristanek.
    AIRBORNE = frozenset({ARM, TAKEOFF, CLIMB, HOVER, LAND, DISARM})

    LABELS = {
        IDLE: "pripravljen",
        PREFLIGHT: "predpoletne preverbe",
        COUNTDOWN: "odstevanje",
        MODE: "preklop v GUIDED",
        ARM: "armanje",
        TAKEOFF: "ukaz za vzlet",
        CLIMB: "vzpenjanje",
        HOVER: "lebdenje",
        LAND: "pristajanje",
        DISARM: "cakanje na dis-arm",
        DONE: "koncano",
        ABORTED: "prekinjeno",
        FAILED: "neuspesno",
    }


@dataclass
class TestFlightParams:
    """Parametri testnega poleta. Vse meje so preverjene v :meth:`clamp`."""
    altitude_m: float = 3.0
    hover_s: float = 5.0
    countdown_s: float = 5.0

    # Predpoletne zahteve
    min_satellites: int = 10
    max_hdop: float = 1.5
    min_voltage_v: Optional[float] = None
    require_gps: bool = True

    # Timeouti [s]
    preflight_timeout_s: float = 60.0
    mode_timeout_s: float = 10.0
    climb_timeout_s: float = 30.0
    hover_extra_s: float = 5.0
    disarm_timeout_s: float = 120.0

    # Delez ciljne visine, ki se steje za "dosezeno"
    reach_fraction: float = 0.95
    tick_s: float = 0.25

    def clamp(self) -> "TestFlightParams":
        """Vrne kopijo z vrednostmi v dovoljenih mejah."""
        self.altitude_m = max(MIN_ALTITUDE_M, min(MAX_ALTITUDE_M,
                                                  float(self.altitude_m)))
        self.hover_s = max(0.0, min(MAX_HOVER_S, float(self.hover_s)))
        self.countdown_s = max(0.0, min(30.0, float(self.countdown_s)))
        self.min_satellites = max(0, int(self.min_satellites))
        self.max_hdop = max(0.1, float(self.max_hdop))
        self.tick_s = max(0.01, min(1.0, float(self.tick_s)))
        return self


@dataclass
class Check:
    """Ena predpoletna preverba."""
    key: str
    label: str
    ok: bool
    value: str = ""
    required: bool = True


def evaluate_checks(snap: dict[str, Any], p: TestFlightParams) -> list[Check]:
    """Oceni predpoletne pogoje iz snapshota telemetrije.

    Cista funkcija --- vhod je slovar, izhod seznam. Zato jo je mogoce
    testirati z izmisljenimi snapshoti brez mostu in brez niti.
    """
    hb = snap.get("heartbeat") or {}
    gps = snap.get("gps") or {}
    bat = snap.get("battery") or {}

    checks: list[Check] = []

    connected = bool(snap.get("connected"))
    checks.append(Check("connected", "Povezava s krmilnikom", connected,
                        snap.get("port") or "—"))

    age = hb.get("age_s")
    fresh = connected and age is not None and age < 3.0
    checks.append(Check("heartbeat", "Svez HEARTBEAT", fresh,
                        "—" if age is None else f"{age:.1f} s"))

    if p.require_gps:
        fix = gps.get("fix_type")
        checks.append(Check("gps_fix", "GPS 3D fix",
                            fix is not None and int(fix) >= 3,
                            _fix_label(fix)))

        sats = gps.get("satellites")
        checks.append(Check("satellites", f"Satelitov >= {p.min_satellites}",
                            sats is not None and int(sats) >= p.min_satellites,
                            "—" if sats is None else str(sats)))

        hdop = gps.get("hdop")
        checks.append(Check("hdop", f"HDOP <= {p.max_hdop}",
                            hdop is not None and float(hdop) <= p.max_hdop,
                            "—" if hdop is None else f"{hdop:.2f}"))

    status = hb.get("system_status")
    checks.append(Check("status", "Krmilnik v STANDBY",
                        status in (None, "STANDBY"),
                        status or "—", required=status is not None))

    armed = bool(hb.get("armed"))
    checks.append(Check("disarmed", "Letalnik ni armiran", not armed,
                        "ARMED" if armed else "DISARMED"))

    if p.min_voltage_v is not None:
        v = bat.get("voltage_v")
        checks.append(Check("battery", f"Baterija >= {p.min_voltage_v} V",
                            v is not None and float(v) >= p.min_voltage_v,
                            "—" if v is None else f"{v:.2f} V"))
    return checks


def _fix_label(fix: Optional[int]) -> str:
    return {0: "brez", 1: "brez", 2: "2D", 3: "3D",
            4: "DGPS", 5: "RTK-Float", 6: "RTK"}.get(
        -1 if fix is None else int(fix), "—")


def checks_pass(checks: list[Check]) -> bool:
    return all(c.ok for c in checks if c.required)


# ---------------------------------------------------------------------------
# Stroj stanj
# ---------------------------------------------------------------------------
class TestFlightRunner:
    """Izvede zaporedje vzlet--lebdenje--pristanek v ozadnji niti."""

    def __init__(self, bridge: Any) -> None:
        self.bridge = bridge
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._abort = threading.Event()
        self._run_id = 0
        self._phase = Phase.IDLE
        self._phase_since = time.time()
        self._started_at: Optional[float] = None
        self._message = ""
        self._events: list[dict[str, Any]] = []
        self._checks: list[Check] = []
        self._params = TestFlightParams()
        self._peak_alt = 0.0

    # -------- stanje --------

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
                self._phase not in Phase.TERMINAL
                and self._phase != Phase.IDLE
            )
            return {
                "profile": "hover",
                "active": self.active,
                "phase": self._phase,
                "phase_label": Phase.LABELS.get(self._phase, self._phase),
                "phase_elapsed_s": round(now - self._phase_since, 1),
                "elapsed_s": (None if self._started_at is None
                              else round(now - self._started_at, 1)),
                "message": self._message,
                "terminal": self._phase in Phase.TERMINAL,
                "abortable": self.active or stuck or bool(hb.get("armed")),
                "force_cancellable": self.active or stuck or bool(hb.get("armed")),
                "events": list(self._events),
                "checks": [asdict(c) for c in self._checks],
                "checks_pass": checks_pass(self._checks) if self._checks else False,
                "params": asdict(self._params),
                "altitude_m": gps.get("rel_alt_m"),
                "target_altitude_m": self._params.altitude_m,
                "peak_altitude_m": round(self._peak_alt, 2),
                "armed": bool(hb.get("armed")),
                "mode": hb.get("mode"),
            }

    # -------- krmiljenje --------

    def start(self, params: Optional[TestFlightParams] = None) -> dict[str, Any]:
        """Zazene zaporedje. Vrne stanje ali napako, ce ze tece."""
        with self._lock:
            if self.active:
                return {"ok": False, "error": "Testni polet ze tece.",
                        "status": self.status()}
            if not self.bridge.is_connected():
                return {"ok": False, "error": "Ni MAVLink povezave s Pixhawkom."}

            self._params = (params or TestFlightParams()).clamp()
            self._run_id += 1
            run_id = self._run_id
            self._abort = threading.Event()
            self._events = []
            self._checks = []
            self._peak_alt = 0.0
            self._started_at = time.time()
            self._set_phase(Phase.PREFLIGHT, "Preverjam predpoletne pogoje.",
                            run_id=run_id)

            self._thread = threading.Thread(
                target=self._run, args=(run_id,),
                daemon=True, name="test-flight")
            self._thread.start()
        return {"ok": True, "status": self.status()}

    def abort(self, reason: str = "prekinil uporabnik") -> dict[str, Any]:
        """Prekine zaporedje. V zraku sprozi pristanek."""
        with self._lock:
            if not self.active:
                # Tudi ce zaporedje ne tece: ce je letalnik armiran, je
                # zahteva po prekinitvi zahteva po pristanku.
                if bool((self.bridge.get_snapshot().get("heartbeat") or {})
                        .get("armed")):
                    res = self.bridge.set_mode("LAND")
                    self._log("abort", f"Ni aktivnega zaporedja; ukaz LAND ({reason}).",
                              ok=bool(res.get("ok")))
                    return {"ok": bool(res.get("ok")), "landing": True,
                            "error": res.get("error"), "status": self.status()}
                if self._phase not in Phase.TERMINAL and self._phase != Phase.IDLE:
                    self._phase = Phase.ABORTED
                    self._phase_since = time.time()
                    self._message = f"Prekinjeno ({reason})."
                    self._log("abort", f"Prekinitev brez žive niti: {reason}.")
                    return {"ok": True, "status": self.status()}
                return {"ok": False, "error": "Testni polet ne tece.",
                        "status": self.status()}
            self._log("abort", f"Zahtevana prekinitev: {reason}.")
            # Pred armanjem takoj prestavi fazo — sicer UI še nekaj časa
            # kaže PREFLIGHT, čeprav je prekinitev že zahtevana.
            if self._phase not in Phase.AIRBORNE and self._phase not in Phase.TERMINAL:
                self._phase = Phase.ABORTED
                self._phase_since = time.time()
                self._message = "Prekinjeno pred armanjem."
            self._abort.set()
        return {"ok": True, "status": self.status()}

    def force_abort(self, reason: str = "force cancel") -> dict[str, Any]:
        """Takoj odklene UI in prekine zaporedje, tudi ce je nit obvisela.

        Invalidira tekočo nit (``_run_id``), pošlje LAND, po potrebi
        force-disarm. Uporabi, ko običajni PREKINI ne premakne faze.
        """
        with self._lock:
            snap = self.bridge.get_snapshot()
            hb = snap.get("heartbeat") or {}
            armed = bool(hb.get("armed"))
            was_busy = (
                self.active
                or (self._phase not in Phase.TERMINAL and self._phase != Phase.IDLE)
                or armed
            )
            if not was_busy:
                return {"ok": False, "error": "Ni kaj prekiniti.",
                        "forced": True, "status": self.status()}

            self._run_id += 1
            self._abort.set()
            self._thread = None
            self._phase = Phase.ABORTED
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
            # Če smo še armirani (npr. obviselo v DISARM), vsili disarm.
            if bool((self.bridge.get_snapshot().get("heartbeat") or {})
                    .get("armed")):
                dres = self.bridge.arm(False, force=True)
                disarm = True
                self._log("force_abort",
                          f"Force disarm ({'ok' if dres.get('ok') else dres.get('error')}).",
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

    def _set_phase(self, phase: str, message: str = "",
                   *, run_id: Optional[int] = None) -> None:
        with self._lock:
            if run_id is not None and run_id != self._run_id:
                return
            self._phase = phase
            self._phase_since = time.time()
            if message:
                self._message = message
        if run_id is None or run_id == self._run_id:
            self._log(phase, message or Phase.LABELS.get(phase, phase))

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
        """Spi in vrne ``True``, ce je bila med spanjem zahtevana prekinitev."""
        return self._abort.wait(timeout=seconds)

    def _is_current(self, run_id: int) -> bool:
        return run_id == self._run_id and not self._abort.is_set()

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

    def _fail(self, message: str, *, run_id: Optional[int] = None) -> None:
        """Konca zaporedje. V zraku najprej sprozi pristanek."""
        if run_id is not None and run_id != self._run_id:
            return
        if self._phase in Phase.AIRBORNE and self._armed():
            self._log(self._phase, f"{message} -> sprozam pristanek.", ok=False)
            self._land_and_wait(reason=message, run_id=run_id)
            self._set_phase(Phase.FAILED, message, run_id=run_id)
        else:
            self._set_phase(Phase.FAILED, message, run_id=run_id)

    # -------- zaporedje --------

    def _run(self, run_id: int) -> None:
        p = self._params
        try:
            if not self._phase_preflight(p, run_id):
                return
            if not self._phase_countdown(p, run_id):
                return
            if not self._phase_mode(p, run_id):
                return
            if not self._phase_arm(p, run_id):
                return
            if not self._phase_takeoff(p, run_id):
                return
            if not self._phase_climb(p, run_id):
                return
            if not self._phase_hover(p, run_id):
                return
            self._land_and_wait(reason="konec zaporedja", run_id=run_id)
            if run_id != self._run_id:
                return
            if self._abort.is_set():
                self._set_phase(Phase.ABORTED,
                                "Zaporedje prekinjeno; dron pristal.",
                                run_id=run_id)
            else:
                self._set_phase(
                    Phase.DONE,
                    f"Testni polet koncan. Najvisja visina "
                    f"{self._peak_alt:.1f} m.",
                    run_id=run_id)
        except Exception as exc:  # pragma: no cover --- zadnja varovalka
            if run_id != self._run_id:
                return
            self._log(self._phase, f"Nepricakovana napaka: {exc}", ok=False)
            try:
                if self._armed():
                    self.bridge.set_mode("LAND")
            finally:
                self._set_phase(Phase.FAILED, f"Nepricakovana napaka: {exc}",
                                run_id=run_id)

    def _phase_preflight(self, p: TestFlightParams, run_id: int) -> bool:
        self._set_phase(Phase.PREFLIGHT, "Cakam na izpolnjene predpoletne pogoje.",
                        run_id=run_id)
        deadline = time.time() + p.preflight_timeout_s
        while time.time() < deadline:
            if not self._is_current(run_id):
                self._set_phase(Phase.ABORTED, "Prekinjeno pred armanjem.",
                                run_id=run_id)
                return False
            with self._lock:
                self._checks = evaluate_checks(self.bridge.get_snapshot(), p)
                ok = checks_pass(self._checks)
            if ok:
                self._log(Phase.PREFLIGHT, "Vse predpoletne preverbe uspesne.",
                          ok=True)
                return True
            if self._sleep(p.tick_s):
                self._set_phase(Phase.ABORTED, "Prekinjeno pred armanjem.",
                                run_id=run_id)
                return False
        failed = [c.label for c in self._checks if c.required and not c.ok]
        self._set_phase(
            Phase.FAILED,
            "Predpoletne preverbe niso uspele v "
            f"{p.preflight_timeout_s:.0f} s: {', '.join(failed) or 'neznano'}.",
            run_id=run_id)
        return False

    def _phase_countdown(self, p: TestFlightParams, run_id: int) -> bool:
        if p.countdown_s <= 0:
            return True
        self._set_phase(Phase.COUNTDOWN,
                        f"Vzlet cez {p.countdown_s:.0f} s. Odmakni se.",
                        run_id=run_id)
        end = time.time() + p.countdown_s
        while time.time() < end:
            if self._sleep(min(p.tick_s, max(0.0, end - time.time()))):
                self._set_phase(Phase.ABORTED, "Prekinjeno med odstevanjem.",
                                run_id=run_id)
                return False
            if not self._is_current(run_id):
                return False
        return True

    def _phase_mode(self, p: TestFlightParams, run_id: int) -> bool:
        if not self._is_current(run_id):
            return False
        self._set_phase(Phase.MODE, "Preklapljam v GUIDED.", run_id=run_id)
        res = self.bridge.set_mode("GUIDED")
        if not self._is_current(run_id):
            return False
        if not res.get("ok"):
            self._set_phase(Phase.FAILED,
                            f"Preklop v GUIDED ni uspel: {res.get('error')}",
                            run_id=run_id)
            return False
        self._log(Phase.MODE, "Nacin GUIDED sprejet.", ok=True)
        return True

    def _phase_arm(self, p: TestFlightParams, run_id: int) -> bool:
        if not self._is_current(run_id):
            return False
        self._set_phase(Phase.ARM, "Armiram.", run_id=run_id)
        res = self.bridge.arm(True)
        if not self._is_current(run_id):
            if self._armed():
                self.bridge.set_mode("LAND")
            return False
        if not res.get("ok"):
            # Ni armiran -> ni v zraku -> ni pristanka, samo napaka.
            texts = "; ".join(s.get("text", "") for s in (res.get("statustexts") or []))
            self._set_phase(
                Phase.FAILED,
                f"Armanje zavrnjeno: {res.get('error')}"
                + (f" ({texts})" if texts else ""),
                run_id=run_id)
            return False
        self._log(Phase.ARM, "Armirano.", ok=True)
        return True

    def _phase_takeoff(self, p: TestFlightParams, run_id: int) -> bool:
        if not self._is_current(run_id):
            return False
        self._set_phase(Phase.TAKEOFF, f"Ukaz za vzlet na {p.altitude_m:.1f} m.",
                        run_id=run_id)
        res = self.bridge.takeoff(p.altitude_m)
        if not self._is_current(run_id):
            if self._armed():
                self.bridge.set_mode("LAND")
            return False
        if not res.get("ok"):
            self._fail(f"Ukaz za vzlet zavrnjen: {res.get('error')}",
                       run_id=run_id)
            return False
        self._log(Phase.TAKEOFF, "Ukaz za vzlet sprejet.", ok=True)
        return True

    def _phase_climb(self, p: TestFlightParams, run_id: int) -> bool:
        target = p.altitude_m * p.reach_fraction
        self._set_phase(Phase.CLIMB, f"Vzpenjam se na {p.altitude_m:.1f} m.",
                        run_id=run_id)
        deadline = time.time() + p.climb_timeout_s
        while time.time() < deadline:
            if not self._is_current(run_id):
                if self._armed():
                    self._land_and_wait(reason="prekinitev med vzpenjanjem",
                                        run_id=run_id)
                self._set_phase(Phase.ABORTED, "Prekinjeno med vzpenjanjem.",
                                run_id=run_id)
                return False
            alt = self._alt()
            if alt is not None and alt >= target:
                self._log(Phase.CLIMB, f"Dosezena visina {alt:.2f} m.", ok=True)
                return True
            if self._sleep(p.tick_s):
                self._land_and_wait(reason="prekinitev med vzpenjanjem",
                                    run_id=run_id)
                self._set_phase(Phase.ABORTED, "Prekinjeno med vzpenjanjem.",
                                run_id=run_id)
                return False
        self._fail(f"Ciljna visina ni bila dosezena v {p.climb_timeout_s:.0f} s.",
                   run_id=run_id)
        return False

    def _phase_hover(self, p: TestFlightParams, run_id: int) -> bool:
        if p.hover_s <= 0:
            return True
        self._set_phase(Phase.HOVER, f"Lebdim {p.hover_s:.0f} s.", run_id=run_id)
        end = time.time() + p.hover_s
        while time.time() < end:
            if not self._is_current(run_id):
                if self._armed():
                    self._land_and_wait(reason="prekinitev med lebdenjem",
                                        run_id=run_id)
                self._set_phase(Phase.ABORTED, "Prekinjeno med lebdenjem.",
                                run_id=run_id)
                return False
            if self._sleep(min(p.tick_s, max(0.0, end - time.time()))):
                self._log(Phase.HOVER, "Prekinitev med lebdenjem.", ok=False)
                self._land_and_wait(reason="prekinitev med lebdenjem",
                                    run_id=run_id)
                self._set_phase(Phase.ABORTED, "Prekinjeno med lebdenjem.",
                                run_id=run_id)
                return False
        return True

    def _land_and_wait(self, reason: str,
                       *, run_id: Optional[int] = None) -> bool:
        """Preklopi v LAND in caka na dis-arm. Vrne ``True``, ce je dis-armal."""
        if run_id is not None and run_id != self._run_id:
            return False
        p = self._params
        self._set_phase(Phase.LAND, f"Pristajam ({reason}).", run_id=run_id)
        res = self.bridge.set_mode("LAND")
        if not res.get("ok"):
            # Zadnja moznost: RTL. Ce tudi to ne uspe, ostane rocni poseg.
            self._log(Phase.LAND,
                      f"LAND zavrnjen ({res.get('error')}), poskusam RTL.",
                      ok=False)
            res = self.bridge.set_mode("RTL")
            if not res.get("ok"):
                self._log(Phase.LAND,
                          "Tudi RTL zavrnjen --- potreben je rocni poseg s "
                          "cetrtim kanalom RC oddajnika.", ok=False)
                return False

        self._set_phase(Phase.DISARM, "Cakam na dis-arm po pristanku.",
                        run_id=run_id)
        deadline = time.time() + p.disarm_timeout_s
        while time.time() < deadline:
            # Force-cancel lahko invalidira run_id med cakanjem na disarm.
            if run_id is not None and run_id != self._run_id:
                return False
            if not self._armed():
                self._log(Phase.DISARM, "Letalnik dis-armiran.", ok=True)
                return True
            # Običajne prekinitve tu namenoma NE upostevamo: pristanek je ze
            # v teku. Force-cancel (nov run_id) pa zanka prekine zgoraj.
            time.sleep(p.tick_s)
        self._log(Phase.DISARM,
                  f"Ni dis-arma v {p.disarm_timeout_s:.0f} s --- preveri dron.",
                  ok=False)
        return False


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_runner: Optional[TestFlightRunner] = None
_runner_lock = threading.Lock()


def get_runner() -> TestFlightRunner:
    """Vrne (in po potrebi ustvari) singleton, vezan na singleton mostu."""
    global _runner
    with _runner_lock:
        if _runner is None:
            from .mavlink_bridge import get_bridge
            _runner = TestFlightRunner(get_bridge())
        return _runner
