"""Testi stroja stanj samodejnega testnega poleta.

Cela poanta locitve zaporedja od mostu je, da je mogoce preveriti *vse*
poti --- tudi tiste, ki jih na pravem dronu ne moremo ali ne smemo
sprozati: zavrnjeno armanje, dron, ki se ne dvigne, zavrnjen pristanek,
prekinitev sredi vzpenjanja. Dvojnik mostu (:class:`FakeBridge`) simulira
letalnik, ki se na ukaze odziva s spremembo snapshota.
"""
from __future__ import annotations

import threading
import time

import pytest

from missions.services.test_flight import (
    MAX_ALTITUDE_M, MIN_ALTITUDE_M, Phase, TestFlightParams, TestFlightRunner,
    checks_pass, evaluate_checks,
)

FAST = dict(countdown_s=0.0, hover_s=0.05, tick_s=0.01,
            preflight_timeout_s=1.0, climb_timeout_s=1.0,
            disarm_timeout_s=1.0)


def params(**kw) -> TestFlightParams:
    return TestFlightParams(**{**FAST, **kw})


def good_snapshot(**over) -> dict:
    snap = {
        "connected": True,
        "port": "/dev/ttyACM0",
        "heartbeat": {"armed": False, "age_s": 0.4, "mode": "STABILIZE",
                      "system_status": "STANDBY"},
        "gps": {"fix_type": 3, "satellites": 14, "hdop": 0.8, "rel_alt_m": 0.0},
        "battery": {"voltage_v": 12.2},
    }
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(snap.get(k), dict):
            snap[k] = {**snap[k], **v}
        else:
            snap[k] = v
    return snap


class FakeBridge:
    """Letalnik, ki se odziva na ukaze: armanje, vzlet, vzpenjanje, pristanek."""

    def __init__(self, *, climb: bool = True, arm_ok: bool = True,
                 mode_ok: bool = True, takeoff_ok: bool = True,
                 land_ok: bool = True, disarm_on_land: bool = True,
                 climb_rate_ms: float = 200.0) -> None:
        self.lock = threading.Lock()
        self.snap = good_snapshot()
        self.calls: list[str] = []
        self.climb = climb
        self.arm_ok = arm_ok
        self.mode_ok = mode_ok
        self.takeoff_ok = takeoff_ok
        self.land_ok = land_ok
        self.disarm_on_land = disarm_on_land
        self.climb_rate_ms = climb_rate_ms
        self._target = 0.0
        self._climber: threading.Thread | None = None
        self._stop = threading.Event()

    # -- vmesnik, ki ga runner pricakuje --
    def is_connected(self) -> bool:
        return bool(self.snap["connected"])

    def get_snapshot(self) -> dict:
        with self.lock:
            import copy
            return copy.deepcopy(self.snap)

    def set_mode(self, name: str) -> dict:
        self.calls.append(f"mode:{name}")
        if name == "LAND":
            if not self.land_ok:
                return {"ok": False, "error": "LAND zavrnjen"}
            self._begin_descent()
            self._set(heartbeat={"mode": "LAND"})
            return {"ok": True}
        if not self.mode_ok:
            return {"ok": False, "error": f"{name} zavrnjen"}
        self._set(heartbeat={"mode": name})
        return {"ok": True}

    def arm(self, arm: bool = True, force: bool = False) -> dict:
        self.calls.append(f"arm:{arm}")
        if arm and not self.arm_ok:
            return {"ok": False, "error": "pre-arm neuspesen",
                    "statustexts": [{"text": "PreArm: GPS ni pripravljen"}]}
        self._set(heartbeat={"armed": bool(arm)})
        return {"ok": True}

    def takeoff(self, altitude_m: float) -> dict:
        self.calls.append(f"takeoff:{altitude_m}")
        if not self.takeoff_ok:
            return {"ok": False, "error": "vzlet zavrnjen"}
        if self.climb:
            self._begin_climb(altitude_m)
        return {"ok": True}

    def set_pending_mission(self, *a, **kw) -> None:
        pass

    # -- simulacija gibanja --
    def _set(self, **groups) -> None:
        with self.lock:
            for k, v in groups.items():
                if isinstance(v, dict):
                    self.snap[k] = {**self.snap[k], **v}
                else:
                    self.snap[k] = v

    def _begin_climb(self, target: float) -> None:
        self._target = target

        def run() -> None:
            while not self._stop.is_set():
                with self.lock:
                    a = self.snap["gps"]["rel_alt_m"]
                    if a >= self._target:
                        return
                    self.snap["gps"]["rel_alt_m"] = min(self._target, a + 0.5)
                time.sleep(self.climb_rate_ms / 1000.0 / 10)

        self._climber = threading.Thread(target=run, daemon=True)
        self._climber.start()

    def _begin_descent(self) -> None:
        self._target = 0.0

        def run() -> None:
            while not self._stop.is_set():
                with self.lock:
                    a = self.snap["gps"]["rel_alt_m"]
                    if a <= 0.0:
                        if self.disarm_on_land:
                            self.snap["heartbeat"]["armed"] = False
                        return
                    self.snap["gps"]["rel_alt_m"] = max(0.0, a - 0.5)
                time.sleep(self.climb_rate_ms / 1000.0 / 10)

        threading.Thread(target=run, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


def wait_terminal(runner: TestFlightRunner, timeout: float = 8.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        st = runner.status()
        if not st["active"] and st["terminal"]:
            return st
        time.sleep(0.01)
    raise AssertionError(f"zaporedje se ni koncalo; faza={runner.status()['phase']}")


@pytest.fixture
def bridge():
    b = FakeBridge()
    yield b
    b.stop()


# ---------------------------------------------------------------------------
# Predpoletne preverbe (ciste funkcije)
# ---------------------------------------------------------------------------
def test_good_snapshot_passes_all_checks() -> None:
    checks = evaluate_checks(good_snapshot(), TestFlightParams())
    assert checks_pass(checks)


def test_no_connection_fails() -> None:
    assert not checks_pass(evaluate_checks(
        good_snapshot(connected=False), TestFlightParams()))


def test_stale_heartbeat_fails() -> None:
    checks = evaluate_checks(good_snapshot(heartbeat={"age_s": 9.0}),
                             TestFlightParams())
    assert not checks_pass(checks)
    assert not next(c for c in checks if c.key == "heartbeat").ok


def test_no_gps_fix_fails() -> None:
    checks = evaluate_checks(good_snapshot(gps={"fix_type": 1}),
                             TestFlightParams())
    assert not next(c for c in checks if c.key == "gps_fix").ok


def test_too_few_satellites_fails() -> None:
    checks = evaluate_checks(good_snapshot(gps={"satellites": 5}),
                             TestFlightParams(min_satellites=10))
    assert not next(c for c in checks if c.key == "satellites").ok


def test_high_hdop_fails() -> None:
    checks = evaluate_checks(good_snapshot(gps={"hdop": 4.0}),
                             TestFlightParams(max_hdop=1.5))
    assert not next(c for c in checks if c.key == "hdop").ok


def test_already_armed_blocks_start() -> None:
    checks = evaluate_checks(good_snapshot(heartbeat={"armed": True}),
                             TestFlightParams())
    assert not next(c for c in checks if c.key == "disarmed").ok


def test_gps_checks_skipped_when_not_required() -> None:
    checks = evaluate_checks(good_snapshot(gps={"fix_type": 0, "satellites": 0}),
                             TestFlightParams(require_gps=False))
    assert {c.key for c in checks}.isdisjoint({"gps_fix", "satellites", "hdop"})
    assert checks_pass(checks)


def test_low_battery_fails_when_threshold_set() -> None:
    checks = evaluate_checks(good_snapshot(battery={"voltage_v": 10.9}),
                             TestFlightParams(min_voltage_v=11.1))
    assert not next(c for c in checks if c.key == "battery").ok


# ---------------------------------------------------------------------------
# Meje parametrov
# ---------------------------------------------------------------------------
def test_altitude_is_clamped_to_safe_range() -> None:
    assert TestFlightParams(altitude_m=500).clamp().altitude_m == MAX_ALTITUDE_M
    assert TestFlightParams(altitude_m=0.1).clamp().altitude_m == MIN_ALTITUDE_M
    assert TestFlightParams(altitude_m=-5).clamp().altitude_m == MIN_ALTITUDE_M


def test_hover_and_countdown_are_clamped() -> None:
    p = TestFlightParams(hover_s=9999, countdown_s=-3).clamp()
    assert p.hover_s == 60.0
    assert p.countdown_s == 0.0


# ---------------------------------------------------------------------------
# Uspesno zaporedje
# ---------------------------------------------------------------------------
def test_happy_path_runs_full_sequence(bridge: FakeBridge) -> None:
    runner = TestFlightRunner(bridge)
    assert runner.start(params(altitude_m=3.0))["ok"]
    st = wait_terminal(runner)

    assert st["phase"] == Phase.DONE, st["message"]
    assert bridge.calls == [
        "mode:GUIDED", "arm:True", "takeoff:3.0", "mode:LAND",
    ]
    assert st["peak_altitude_m"] >= 3.0 * 0.95
    assert st["armed"] is False


def test_sequence_records_ordered_events(bridge: FakeBridge) -> None:
    runner = TestFlightRunner(bridge)
    runner.start(params())
    st = wait_terminal(runner)
    phases = [e["phase"] for e in st["events"]]
    for expected in (Phase.PREFLIGHT, Phase.MODE, Phase.ARM, Phase.TAKEOFF,
                     Phase.CLIMB, Phase.HOVER, Phase.LAND, Phase.DISARM):
        assert expected in phases, f"manjka faza {expected}"
    assert phases.index(Phase.ARM) < phases.index(Phase.TAKEOFF)
    assert phases.index(Phase.TAKEOFF) < phases.index(Phase.LAND)


def test_takeoff_uses_requested_altitude(bridge: FakeBridge) -> None:
    runner = TestFlightRunner(bridge)
    runner.start(params(altitude_m=5.0))
    wait_terminal(runner)
    assert "takeoff:5.0" in bridge.calls


def test_altitude_above_limit_is_clamped_before_takeoff(bridge: FakeBridge) -> None:
    runner = TestFlightRunner(bridge)
    runner.start(params(altitude_m=250.0))
    wait_terminal(runner)
    assert f"takeoff:{MAX_ALTITUDE_M}" in bridge.calls


# ---------------------------------------------------------------------------
# Zavrnitve --- na tleh se ne sme nadaljevati
# ---------------------------------------------------------------------------
def test_preflight_timeout_never_arms() -> None:
    b = FakeBridge()
    b.snap = good_snapshot(gps={"fix_type": 1, "satellites": 3})
    runner = TestFlightRunner(b)
    runner.start(params())
    st = wait_terminal(runner)
    assert st["phase"] == Phase.FAILED
    assert "predpoletne" in st["message"].lower()
    assert b.calls == [], "ob neuspelih preverbah ne sme biti nobenega ukaza"
    b.stop()


def test_mode_rejection_stops_before_arming() -> None:
    b = FakeBridge(mode_ok=False)
    runner = TestFlightRunner(b)
    runner.start(params())
    st = wait_terminal(runner)
    assert st["phase"] == Phase.FAILED
    assert "arm:True" not in b.calls
    b.stop()


def test_arm_rejection_reports_prearm_text() -> None:
    b = FakeBridge(arm_ok=False)
    runner = TestFlightRunner(b)
    runner.start(params())
    st = wait_terminal(runner)
    assert st["phase"] == Phase.FAILED
    assert "PreArm" in st["message"]
    assert "takeoff" not in " ".join(b.calls)
    b.stop()


def test_start_refused_without_connection() -> None:
    b = FakeBridge()
    b.snap = good_snapshot(connected=False)
    runner = TestFlightRunner(b)
    res = runner.start(params())
    assert res["ok"] is False
    assert "povezave" in res["error"]
    b.stop()


def test_second_start_is_refused_while_running(bridge: FakeBridge) -> None:
    runner = TestFlightRunner(bridge)
    runner.start(params(hover_s=0.4))
    second = runner.start(params())
    assert second["ok"] is False
    assert "ze tece" in second["error"]
    wait_terminal(runner)


# ---------------------------------------------------------------------------
# Odpovedi v zraku --- vedno konca s pristankom
# ---------------------------------------------------------------------------
def test_takeoff_rejection_after_arming_triggers_landing() -> None:
    b = FakeBridge(takeoff_ok=False)
    runner = TestFlightRunner(b)
    runner.start(params())
    st = wait_terminal(runner)
    assert st["phase"] == Phase.FAILED
    assert "mode:LAND" in b.calls, "po armanju mora odpoved sprozit pristanek"
    b.stop()


def test_climb_timeout_triggers_landing() -> None:
    b = FakeBridge(climb=False)          # dron se ne dvigne
    runner = TestFlightRunner(b)
    runner.start(params(climb_timeout_s=0.2))
    st = wait_terminal(runner)
    assert st["phase"] == Phase.FAILED
    assert "visina ni bila dosezena" in st["message"].lower()
    assert "mode:LAND" in b.calls
    b.stop()


def test_land_rejection_falls_back_to_rtl() -> None:
    b = FakeBridge(land_ok=False)
    runner = TestFlightRunner(b)
    runner.start(params())
    wait_terminal(runner)
    assert "mode:LAND" in b.calls
    assert "mode:RTL" in b.calls, "ob zavrnjenem LAND mora poskusiti RTL"
    b.stop()


def test_missing_disarm_is_reported_not_silently_ignored() -> None:
    b = FakeBridge(disarm_on_land=False)
    runner = TestFlightRunner(b)
    runner.start(params(disarm_timeout_s=0.2))
    st = wait_terminal(runner)
    msgs = " ".join(e["message"] for e in st["events"])
    assert "dis-arma" in msgs
    b.stop()


# ---------------------------------------------------------------------------
# Prekinitev
# ---------------------------------------------------------------------------
def test_abort_before_arming_issues_no_commands() -> None:
    b = FakeBridge()
    b.snap = good_snapshot(gps={"fix_type": 1})   # obtici v PREFLIGHT
    runner = TestFlightRunner(b)
    runner.start(params(preflight_timeout_s=5.0))
    time.sleep(0.05)
    assert runner.abort()["ok"]
    st = wait_terminal(runner)
    assert st["phase"] == Phase.ABORTED
    assert b.calls == []
    b.stop()


def test_abort_during_countdown_does_not_arm() -> None:
    b = FakeBridge()
    runner = TestFlightRunner(b)
    runner.start(params(countdown_s=2.0))
    time.sleep(0.1)
    runner.abort()
    st = wait_terminal(runner)
    assert st["phase"] == Phase.ABORTED
    assert b.calls == []
    b.stop()


def test_abort_during_climb_lands() -> None:
    b = FakeBridge(climb_rate_ms=4000.0)   # pocasno vzpenjanje
    runner = TestFlightRunner(b)
    runner.start(params(altitude_m=8.0, climb_timeout_s=5.0))
    # Pocakaj, da je res armiran in v fazi vzpenjanja.
    end = time.time() + 2.0
    while time.time() < end and runner.status()["phase"] != Phase.CLIMB:
        time.sleep(0.01)
    assert runner.status()["phase"] == Phase.CLIMB
    runner.abort()
    st = wait_terminal(runner)
    assert st["phase"] == Phase.ABORTED
    assert "mode:LAND" in b.calls
    b.stop()


def test_abort_without_running_sequence_lands_armed_vehicle() -> None:
    b = FakeBridge()
    b.snap = good_snapshot(heartbeat={"armed": True}, gps={"rel_alt_m": 4.0})
    runner = TestFlightRunner(b)
    res = runner.abort("rocna zahteva")
    assert res["ok"] is True
    assert res["landing"] is True
    assert "mode:LAND" in b.calls
    b.stop()


def test_abort_without_running_sequence_on_ground_is_noop(bridge: FakeBridge) -> None:
    res = TestFlightRunner(bridge).abort()
    assert res["ok"] is False
    assert bridge.calls == []


def test_force_abort_during_climb_unlocks_immediately() -> None:
    b = FakeBridge(climb_rate_ms=4000.0)
    runner = TestFlightRunner(b)
    runner.start(params(altitude_m=8.0, climb_timeout_s=30.0))
    end = time.time() + 2.0
    while time.time() < end and runner.status()["phase"] != Phase.CLIMB:
        time.sleep(0.01)
    assert runner.status()["phase"] == Phase.CLIMB
    res = runner.force_abort("gumb FORCE CANCEL")
    assert res["ok"] is True
    assert res["forced"] is True
    assert res["status"]["phase"] == Phase.ABORTED
    assert "mode:LAND" in b.calls
    # UI je odklenjen tudi, ce bi stara nit se tekla.
    assert res["status"]["active"] is False
    b.stop()


def test_force_abort_unlocks_stuck_disarm_and_allows_restart() -> None:
    b = FakeBridge(disarm_on_land=False)
    runner = TestFlightRunner(b)
    runner.start(params(hover_s=0.0, altitude_m=2.0, disarm_timeout_s=60.0))
    end = time.time() + 5.0
    while time.time() < end and runner.status()["phase"] != Phase.DISARM:
        time.sleep(0.02)
    assert runner.status()["phase"] == Phase.DISARM
    res = runner.force_abort("stuck disarm")
    assert res["ok"] is True
    assert res["forced"] is True
    assert res["status"]["phase"] == Phase.ABORTED
    assert "arm:False" in b.calls
    # Nova izvedba mora biti mozna takoj.
    assert runner.start(params(hover_s=0.0, altitude_m=2.0))["ok"]
    st = wait_terminal(runner)
    assert st["phase"] in (Phase.DONE, Phase.ABORTED, Phase.FAILED)
    b.stop()


# ---------------------------------------------------------------------------
# Stanje za vmesnik
# ---------------------------------------------------------------------------
def test_idle_status_shape(bridge: FakeBridge) -> None:
    st = TestFlightRunner(bridge).status()
    assert st["phase"] == Phase.IDLE
    assert st["active"] is False
    assert st["abortable"] is False
    assert st["force_cancellable"] is False
    assert st["events"] == []


def test_status_exposes_phase_label_in_slovene(bridge: FakeBridge) -> None:
    runner = TestFlightRunner(bridge)
    runner.start(params(hover_s=0.3))
    time.sleep(0.05)
    assert runner.status()["phase_label"] in Phase.LABELS.values()
    wait_terminal(runner)
