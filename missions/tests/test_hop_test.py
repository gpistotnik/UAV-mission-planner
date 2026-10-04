"""Testi stroja stanj za testni skok (vzlet → 2 m → slika → nazaj → slika)."""
from __future__ import annotations

import copy
import math
import threading
import time

import pytest

from missions.services.hop_test import (
    MAX_LEG_M, MIN_LEG_M, HopPhase, HopTestParams, HopTestRunner,
    haversine_m, offset_lat_lon,
)
from missions.tests.test_testflight import good_snapshot


FAST = dict(
    countdown_s=0.0, tick_s=0.01,
    preflight_timeout_s=1.0, climb_timeout_s=2.0,
    leg_timeout_s=2.0, disarm_timeout_s=1.0,
    capture_settle_s=0.0, arrive_hold_s=0.02,
    arrive_radius_m=0.6, max_groundspeed_ms=50.0,
    max_offtrack_m=20.0,
)


def params(**kw) -> HopTestParams:
    return HopTestParams(**{**FAST, **kw})


def wait_terminal(runner: HopTestRunner, timeout: float = 10.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        st = runner.status()
        if not st["active"] and st["terminal"]:
            return st
        time.sleep(0.01)
    raise AssertionError(f"skok se ni končal; faza={runner.status()['phase']}")


class HopFakeBridge:
    """Simulira vzlet, lateralni GOTO in zajem."""

    def __init__(self, *, climb: bool = True, goto_ok: bool = True,
                 capture_ok: bool = True, capture_result: dict | None = None,
                 move_rate_ms: float = 50.0) -> None:
        self.lock = threading.Lock()
        # Ljubljana-ish start
        self.snap = good_snapshot(
            gps={"fix_type": 3, "satellites": 14, "hdop": 0.8,
                 "rel_alt_m": 0.0, "lat": 46.05, "lon": 14.5,
                 "vx_ms": 0.0, "vy_ms": 0.0})
        self.calls: list[str] = []
        self.climb = climb
        self.goto_ok = goto_ok
        self.capture_ok = capture_ok
        self.capture_result = capture_result
        self.move_rate_ms = move_rate_ms
        self._stop = threading.Event()
        self._target_lat: float | None = None
        self._target_lon: float | None = None
        self._target_alt = 0.0

    def is_connected(self) -> bool:
        return bool(self.snap["connected"])

    def get_snapshot(self) -> dict:
        with self.lock:
            return copy.deepcopy(self.snap)

    def set_mode(self, name: str) -> dict:
        self.calls.append(f"mode:{name}")
        if name == "LAND":
            self._begin_descent()
            self._set(heartbeat={"mode": "LAND"})
            return {"ok": True}
        self._set(heartbeat={"mode": name})
        return {"ok": True}

    def arm(self, arm: bool = True, force: bool = False) -> dict:
        self.calls.append(f"arm:{arm}")
        self._set(heartbeat={"armed": bool(arm)})
        return {"ok": True}

    def takeoff(self, altitude_m: float) -> dict:
        self.calls.append(f"takeoff:{altitude_m}")
        if self.climb:
            self._begin_climb(altitude_m)
        return {"ok": True}

    def goto_global(self, lat: float, lon: float, alt: float,
                    yaw_rad=None) -> dict:
        self.calls.append(f"goto:{lat:.6f},{lon:.6f},{alt:.1f}")
        if not self.goto_ok:
            return {"ok": False, "error": "goto zavrnjen"}
        self._target_lat = lat
        self._target_lon = lon
        self._target_alt = alt
        self._begin_move()
        return {"ok": True}

    def goto_ned(self, n, e, d, yaw_rad=None) -> dict:
        self.calls.append(f"goto_ned:{n},{e},{d}")
        return {"ok": True}

    def capture_image(self, count: int = 1) -> dict:
        self.calls.append(f"capture:{count}")
        if not self.capture_ok:
            # Privzeto: napaka povezave (trda). Za FC ACK FAILED glej
            # capture_result.
            if self.capture_result is not None:
                return dict(self.capture_result)
            return {"ok": False, "error": "Ni MAVLink povezave s Pixhawkom."}
        return {"ok": True}

    def set_pending_mission(self, *a, **kw) -> None:
        pass

    def _set(self, **groups) -> None:
        with self.lock:
            for k, v in groups.items():
                if isinstance(v, dict):
                    self.snap[k] = {**self.snap[k], **v}
                else:
                    self.snap[k] = v

    def _begin_climb(self, target: float) -> None:
        def run() -> None:
            while not self._stop.is_set():
                with self.lock:
                    a = self.snap["gps"]["rel_alt_m"]
                    if a >= target:
                        return
                    self.snap["gps"]["rel_alt_m"] = min(target, a + 0.5)
                time.sleep(self.move_rate_ms / 1000.0 / 5)

        threading.Thread(target=run, daemon=True).start()

    def _begin_move(self) -> None:
        def run() -> None:
            while not self._stop.is_set():
                with self.lock:
                    if self._target_lat is None or self._target_lon is None:
                        return
                    lat = self.snap["gps"]["lat"]
                    lon = self.snap["gps"]["lon"]
                    dist = haversine_m(lat, lon, self._target_lat,
                                       self._target_lon)
                    if dist < 0.15:
                        self.snap["gps"]["lat"] = self._target_lat
                        self.snap["gps"]["lon"] = self._target_lon
                        self.snap["gps"]["rel_alt_m"] = self._target_alt
                        return
                    # Premik ~0.4 m proti cilju
                    step = min(0.4, dist)
                    bearing_n = (self._target_lat - lat) * 111_320.0
                    bearing_e = ((self._target_lon - lon) * 111_320.0
                                 * math.cos(math.radians(lat)))
                    hyp = math.hypot(bearing_n, bearing_e) or 1.0
                    dn, de = bearing_n / hyp * step, bearing_e / hyp * step
                    nlat, nlon = offset_lat_lon(lat, lon, dn, de)
                    self.snap["gps"]["lat"] = nlat
                    self.snap["gps"]["lon"] = nlon
                    self.snap["gps"]["rel_alt_m"] = self._target_alt
                time.sleep(self.move_rate_ms / 1000.0 / 5)

        threading.Thread(target=run, daemon=True).start()

    def _begin_descent(self) -> None:
        def run() -> None:
            while not self._stop.is_set():
                with self.lock:
                    a = self.snap["gps"]["rel_alt_m"]
                    if a <= 0.0:
                        self.snap["heartbeat"]["armed"] = False
                        return
                    self.snap["gps"]["rel_alt_m"] = max(0.0, a - 0.5)
                time.sleep(self.move_rate_ms / 1000.0 / 5)

        threading.Thread(target=run, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


@pytest.fixture
def bridge():
    b = HopFakeBridge()
    yield b
    b.stop()


def test_offset_north_is_about_two_metres() -> None:
    lat2, lon2 = offset_lat_lon(46.0, 14.5, 2.0, 0.0)
    assert haversine_m(46.0, 14.5, lat2, lon2) == pytest.approx(2.0, abs=0.05)
    assert lon2 == pytest.approx(14.5)


def test_leg_is_clamped() -> None:
    assert HopTestParams(leg_m=99).clamp().leg_m == MAX_LEG_M
    assert HopTestParams(leg_m=0.1).clamp().leg_m == MIN_LEG_M


def test_happy_hop_sequence(bridge: HopFakeBridge) -> None:
    runner = HopTestRunner(bridge)
    assert runner.start(params(altitude_m=2.0, leg_m=2.0))["ok"]
    st = wait_terminal(runner)
    assert st["phase"] == HopPhase.DONE, st["message"]
    assert st["profile"] == "hop"
    # Ključni ukazi v vrstnem redu
    kinds = [c.split(":")[0] for c in bridge.calls]
    assert kinds[:3] == ["mode", "arm", "takeoff"]
    assert kinds.count("goto") >= 2
    assert kinds.count("capture") == 2
    assert "mode:LAND" in bridge.calls
    assert st["peak_altitude_m"] >= 2.0 * 0.95


def test_hop_records_capture_phases(bridge: HopFakeBridge) -> None:
    runner = HopTestRunner(bridge)
    runner.start(params())
    st = wait_terminal(runner)
    phases = [e["phase"] for e in st["events"]]
    for expected in (HopPhase.GOTO_P1, HopPhase.CAPTURE_1,
                     HopPhase.GOTO_HOME, HopPhase.CAPTURE_2):
        assert expected in phases


def test_goto_failure_lands(bridge: HopFakeBridge) -> None:
    bridge.goto_ok = False
    runner = HopTestRunner(bridge)
    runner.start(params())
    st = wait_terminal(runner)
    assert st["phase"] == HopPhase.FAILED
    assert "mode:LAND" in bridge.calls


def test_abort_during_climb_lands(bridge: HopFakeBridge) -> None:
    bridge.climb = False  # ostane na 0 → dolgo CLIMB
    runner = HopTestRunner(bridge)
    runner.start(params(climb_timeout_s=5.0))
    # Počakaj na ARM/TAKEOFF
    time.sleep(0.15)
    runner.abort("test")
    st = wait_terminal(runner, timeout=5.0)
    assert st["phase"] in (HopPhase.ABORTED, HopPhase.FAILED)
    assert "mode:LAND" in bridge.calls


def test_abort_during_preflight_is_immediate(bridge: HopFakeBridge) -> None:
    """PREFLIGHT zanka mora ob prekinitvi takoj javiti ABORTED (ne čakati timeout)."""
    bridge._set(gps={"fix_type": 3, "satellites": 5, "hdop": 0.8,
                     "rel_alt_m": 0.0, "lat": 46.05, "lon": 14.5})
    runner = HopTestRunner(bridge)
    assert runner.start(params(min_satellites=12, preflight_timeout_s=30.0))["ok"]
    time.sleep(0.05)
    res = runner.abort("gumb")
    assert res["ok"]
    assert res["status"]["phase"] == HopPhase.ABORTED
    st = wait_terminal(runner, timeout=2.0)
    assert st["phase"] == HopPhase.ABORTED


def test_force_abort_during_preflight_unlocks(bridge: HopFakeBridge) -> None:
    bridge._set(gps={"fix_type": 3, "satellites": 5, "hdop": 0.8,
                     "rel_alt_m": 0.0, "lat": 46.05, "lon": 14.5})
    runner = HopTestRunner(bridge)
    assert runner.start(params(min_satellites=12, preflight_timeout_s=60.0))["ok"]
    time.sleep(0.05)
    res = runner.force_abort("force")
    assert res["ok"] is True
    assert res["forced"] is True
    assert res["status"]["phase"] == HopPhase.ABORTED
    assert res["status"]["active"] is False
    # Takoj lahko znova zaženemo (po popravilu GPS).
    bridge._set(gps={"fix_type": 3, "satellites": 14, "hdop": 0.8,
                     "rel_alt_m": 0.0, "lat": 46.05, "lon": 14.5})
    assert runner.start(params())["ok"]
    st = wait_terminal(runner)
    assert st["phase"] in (HopPhase.DONE, HopPhase.ABORTED, HopPhase.FAILED)


def test_cannot_start_twice(bridge: HopFakeBridge) -> None:
    runner = HopTestRunner(bridge)
    assert runner.start(params())["ok"]
    r2 = runner.start(params())
    assert r2["ok"] is False
    wait_terminal(runner)


def test_fc_failed_capture_ack_continues(bridge: HopFakeBridge) -> None:
    """FC brez CAM backend-a vrne FAILED — companion še vedno ujame ukaz."""
    bridge.capture_ok = False
    bridge.capture_result = {
        "ok": False,
        "result_name": "FAILED",
        "error": "IMAGE_CAPTURE x1: FAILED",
    }
    runner = HopTestRunner(bridge)
    runner.start(params())
    st = wait_terminal(runner)
    assert st["phase"] == HopPhase.DONE, st["message"]
    assert bridge.calls.count("capture:1") == 2
    msgs = [e["message"] for e in st["events"] if e["phase"] == HopPhase.CAPTURE_1]
    assert any("FC:" in m for m in msgs)


def test_capture_link_error_fails(bridge: HopFakeBridge) -> None:
    """Brez MAVLink povezave je zajem trda napaka."""
    bridge.capture_ok = False
    bridge.capture_result = None
    runner = HopTestRunner(bridge)
    runner.start(params())
    st = wait_terminal(runner)
    assert st["phase"] == HopPhase.FAILED
    assert "zavrnjen" in st["message"].lower() or "MAVLink" in st["message"]
    assert "mode:LAND" in bridge.calls
