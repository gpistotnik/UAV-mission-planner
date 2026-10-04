"""Testi zaznave vzleta in pristanka.

Cel let se odigra v nekaj vrsticah, ker je detektor cista funkcija stanja:
cas in senzorske vrednosti dobi kot argumente.
"""
from __future__ import annotations

import pytest

from missions.services.autoconnect import (
    list_candidate_devices,
    pick_device,
    should_autoconnect,
)
from missions.services.flight_state import (
    LANDED_IN_AIR, LANDED_LANDING, LANDED_ON_GROUND, LANDED_TAKEOFF,
    LANDED_UNDEFINED, AirborneDetector,
)


def det(**kw) -> AirborneDetector:
    return AirborneDetector(**kw)


# ---------------------------------------------------------------------------
# EXTENDED_SYS_STATE --- primarni vir
# ---------------------------------------------------------------------------
def test_landed_state_drives_transitions() -> None:
    d = det()
    assert d.update(armed=True, rel_alt_m=0.0,
                    landed_state=LANDED_ON_GROUND, now=0) is None
    assert d.update(armed=True, rel_alt_m=0.1,
                    landed_state=LANDED_TAKEOFF, now=1) == "takeoff"
    assert d.airborne is True
    assert d.update(armed=True, rel_alt_m=3.0,
                    landed_state=LANDED_IN_AIR, now=2) is None
    assert d.update(armed=True, rel_alt_m=0.2,
                    landed_state=LANDED_ON_GROUND, now=3) == "landing"
    assert d.airborne is False


def test_landing_state_is_still_airborne() -> None:
    """Med pristajanjem let se ni koncan --- zapis mora teci naprej."""
    d = det()
    d.update(armed=True, rel_alt_m=3.0, landed_state=LANDED_IN_AIR, now=0)
    assert d.update(armed=True, rel_alt_m=1.0,
                    landed_state=LANDED_LANDING, now=1) is None
    assert d.airborne is True


def test_landed_state_wins_over_altitude() -> None:
    """Ce krmilnik pravi 'na tleh', visoka visina tega ne preglasi."""
    d = det()
    assert d.update(armed=True, rel_alt_m=50.0,
                    landed_state=LANDED_ON_GROUND, now=0) is None
    assert d.airborne is False


def test_undefined_landed_state_falls_back_to_altitude() -> None:
    d = det(takeoff_alt_m=0.8)
    assert d.update(armed=True, rel_alt_m=1.5,
                    landed_state=LANDED_UNDEFINED, now=0) == "takeoff"
    assert d.source == "visina"


# ---------------------------------------------------------------------------
# Rezervna pot: visina
# ---------------------------------------------------------------------------
def test_altitude_takeoff_threshold() -> None:
    d = det(takeoff_alt_m=0.8)
    assert d.update(armed=True, rel_alt_m=0.5, landed_state=None, now=0) is None
    assert d.update(armed=True, rel_alt_m=0.9, landed_state=None, now=1) == "takeoff"


def test_landing_requires_low_altitude_to_persist() -> None:
    d = det(takeoff_alt_m=0.8, land_alt_m=0.4, land_settle_s=3.0)
    d.update(armed=True, rel_alt_m=3.0, landed_state=None, now=0)
    assert d.airborne
    # Nizko, a se ne dovolj dolgo.
    assert d.update(armed=True, rel_alt_m=0.2, landed_state=None, now=10) is None
    assert d.update(armed=True, rel_alt_m=0.2, landed_state=None, now=12) is None
    # Po treh sekundah mirovanja.
    assert d.update(armed=True, rel_alt_m=0.2,
                    landed_state=None, now=13.1) == "landing"


def test_bounce_above_threshold_resets_settle_timer() -> None:
    """Odboj ob pristanku ne sme prezgodaj zakljuciti leta."""
    d = det(land_alt_m=0.4, land_settle_s=3.0)
    d.update(armed=True, rel_alt_m=3.0, landed_state=None, now=0)
    d.update(armed=True, rel_alt_m=0.2, landed_state=None, now=10)
    d.update(armed=True, rel_alt_m=1.2, landed_state=None, now=11)   # odboj
    assert d.update(armed=True, rel_alt_m=0.2, landed_state=None, now=12) is None
    assert d.update(armed=True, rel_alt_m=0.2, landed_state=None, now=13) is None
    assert d.update(armed=True, rel_alt_m=0.2,
                    landed_state=None, now=15.1) == "landing"


def test_hysteresis_prevents_flapping_at_threshold() -> None:
    """Lebdenje okoli praga ne sme sprozati zaporedja vzlet-pristanek."""
    d = det(takeoff_alt_m=0.8, land_alt_m=0.4, land_settle_s=3.0)
    assert d.update(armed=True, rel_alt_m=0.85, landed_state=None, now=0) == "takeoff"
    events = [d.update(armed=True, rel_alt_m=alt, landed_state=None, now=t)
              for t, alt in enumerate([0.7, 0.6, 0.75, 0.65, 0.7], start=1)]
    assert events == [None] * 5, "med pragoma ne sme biti nobenega prehoda"


def test_missing_altitude_produces_no_transition() -> None:
    d = det()
    assert d.update(armed=True, rel_alt_m=None, landed_state=None, now=0) is None


def test_land_threshold_must_be_below_takeoff_threshold() -> None:
    with pytest.raises(ValueError):
        AirborneDetector(takeoff_alt_m=0.5, land_alt_m=0.5)


# ---------------------------------------------------------------------------
# Dis-arm
# ---------------------------------------------------------------------------
def test_disarm_always_ends_flight() -> None:
    d = det()
    d.update(armed=True, rel_alt_m=5.0, landed_state=LANDED_IN_AIR, now=0)
    assert d.update(armed=False, rel_alt_m=5.0,
                    landed_state=LANDED_IN_AIR, now=1) == "landing"
    assert d.source == "dis-armiran"


def test_disarmed_vehicle_never_takes_off() -> None:
    d = det()
    assert d.update(armed=False, rel_alt_m=30.0,
                    landed_state=LANDED_IN_AIR, now=0) is None


def test_arm_without_takeoff_produces_no_events() -> None:
    """Test motorjev na tleh ne sme ustvariti seje."""
    d = det()
    events = [d.update(armed=True, rel_alt_m=0.0, landed_state=None, now=t)
              for t in range(5)]
    events.append(d.update(armed=False, rel_alt_m=0.0, landed_state=None, now=5))
    assert events == [None] * 6


def test_reset_clears_state() -> None:
    d = det()
    d.update(armed=True, rel_alt_m=5.0, landed_state=LANDED_IN_AIR, now=0)
    d.reset()
    assert d.airborne is False


def test_full_flight_produces_exactly_one_pair() -> None:
    """Celoten let: en vzlet, en pristanek, nic vmes."""
    d = det()
    profile = [(0, False, 0.0), (1, True, 0.0), (2, True, 0.5), (3, True, 2.0),
               (4, True, 3.0), (5, True, 3.0), (6, True, 1.0), (7, True, 0.1),
               (11, True, 0.05), (12, False, 0.0)]
    events = [e for t, armed, alt in profile
              if (e := d.update(armed=armed, rel_alt_m=alt,
                                landed_state=None, now=t)) is not None]
    assert events == ["takeoff", "landing"]


# ---------------------------------------------------------------------------
# Samodejna povezava
# ---------------------------------------------------------------------------
def test_explicit_device_is_returned_unchanged() -> None:
    assert pick_device("/dev/ttyACM3") == "/dev/ttyACM3"
    assert pick_device("udp:127.0.0.1:14551") == "udp:127.0.0.1:14551"


def test_auto_prefers_serial0_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    import missions.services.autoconnect as ac
    monkeypatch.setattr(ac.os.path, "exists", lambda p: p == "/dev/serial0")
    monkeypatch.setattr(
        "missions.services.mavlink_bridge.list_serial_ports", lambda: [])
    assert pick_device("auto") == "/dev/serial0"


def test_auto_falls_back_to_usb(monkeypatch: pytest.MonkeyPatch) -> None:
    import missions.services.autoconnect as ac
    monkeypatch.setattr(ac.os.path, "exists", lambda p: False)
    monkeypatch.setattr(
        "missions.services.mavlink_bridge.list_serial_ports",
        lambda: [{"device": "/dev/ttyACM0", "description": "Pixhawk"}])
    assert pick_device("auto") == "/dev/ttyACM0"


def test_auto_lists_usb_then_uart(monkeypatch: pytest.MonkeyPatch) -> None:
    """Oboje obstaja: USB najprej (trenutna faza), nato UART."""
    import missions.services.autoconnect as ac
    monkeypatch.setattr(
        ac.os.path, "exists",
        lambda p: p in ("/dev/serial0", "/dev/ttyAMA0"))
    monkeypatch.setattr(
        ac.os.path, "realpath",
        lambda p: {"/dev/serial0": "/dev/serial0",
                   "/dev/ttyAMA0": "/dev/ttyAMA0"}.get(p, p))
    monkeypatch.setattr(
        "missions.services.mavlink_bridge.list_serial_ports",
        lambda: [
            {"device": "/dev/ttyACM0", "description": "Pixhawk"},
            {"device": "/dev/ttyUSB0", "description": "FTDI"},
        ])
    assert list_candidate_devices("auto") == [
        "/dev/ttyACM0", "/dev/ttyUSB0", "/dev/serial0", "/dev/ttyAMA0",
    ]
    assert pick_device("auto") == "/dev/ttyACM0"


def test_auto_dedupes_uart_symlinks(monkeypatch: pytest.MonkeyPatch) -> None:
    import missions.services.autoconnect as ac
    monkeypatch.setattr(
        ac.os.path, "exists",
        lambda p: p in ("/dev/serial0", "/dev/ttyAMA0"))
    monkeypatch.setattr(
        ac.os.path, "realpath",
        lambda p: "/dev/ttyAMA0" if p in ("/dev/serial0", "/dev/ttyAMA0") else p)
    monkeypatch.setattr(
        "missions.services.mavlink_bridge.list_serial_ports",
        lambda: [{"device": "/dev/ttyACM0", "description": "Pixhawk"}])
    assert list_candidate_devices("auto") == ["/dev/ttyACM0", "/dev/serial0"]


def test_auto_includes_windows_com_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    import missions.services.autoconnect as ac
    monkeypatch.setattr(ac.os.path, "exists", lambda p: False)
    monkeypatch.setattr(
        "missions.services.mavlink_bridge.list_serial_ports",
        lambda: [{"device": "COM3", "description": "USB Serial"}])
    assert list_candidate_devices("auto") == ["COM3"]


def test_auto_returns_none_without_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    import missions.services.autoconnect as ac
    monkeypatch.setattr(ac.os.path, "exists", lambda p: False)
    monkeypatch.setattr(
        "missions.services.mavlink_bridge.list_serial_ports", lambda: [])
    assert pick_device("auto") is None


def test_autoconnect_advances_candidate_after_failed_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Po neuspehu na USB mora AutoConnector poskusiti UART."""
    import missions.services.autoconnect as ac

    class FakeBridge:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []
            self._connected = False

        def is_connected(self) -> bool:
            return self._connected

        def is_connecting(self) -> bool:
            return False

        def connect(self, device: str, baud: int):
            self.calls.append((device, baud))
            if device == "/dev/serial0":
                self._connected = True
                return {"connected": True, "port": device, "error": None}
            return {"connected": False, "port": device, "error": "ni HEARTBEAT-a"}

        def disconnect(self):
            self._connected = False
            return {"connected": False}

    monkeypatch.setattr(
        ac.os.path, "exists", lambda p: p == "/dev/serial0")
    monkeypatch.setattr(
        "missions.services.mavlink_bridge.list_serial_ports",
        lambda: [{"device": "/dev/ttyACM0", "description": "Pixhawk"}])

    bridge = FakeBridge()
    conn = ac.AutoConnector(bridge, device="auto", baud=115200, retry_s=0.01)
    conn._stop.set()

    candidates = ac.list_candidate_devices("auto")
    assert candidates[0] == "/dev/ttyACM0"
    assert "/dev/serial0" in candidates

    for idx, device in enumerate(candidates):
        snap = bridge.connect(device, 115200)
        if snap.get("connected"):
            break
        bridge.disconnect()
        conn._cand_index = idx + 1
    assert bridge.calls == [
        ("/dev/ttyACM0", 115200),
        ("/dev/serial0", 115200),
    ]
    assert bridge.is_connected()


def test_management_commands_do_not_open_serial_port() -> None:
    for cmd in ("migrate", "collectstatic", "test", "makemigrations"):
        assert should_autoconnect(["manage.py", cmd]) is False, cmd


def test_runserver_connects_only_in_worker_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import missions.services.autoconnect as ac
    monkeypatch.setattr(ac.sys, "modules", {})   # skrij pytest
    monkeypatch.delenv("RUN_MAIN", raising=False)
    assert should_autoconnect(["manage.py", "runserver"]) is False
    monkeypatch.setenv("RUN_MAIN", "true")
    assert should_autoconnect(["manage.py", "runserver"]) is True


def test_runserver_noreload_connects_without_run_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import missions.services.autoconnect as ac
    monkeypatch.setattr(ac.sys, "modules", {})
    monkeypatch.delenv("RUN_MAIN", raising=False)
    assert should_autoconnect(
        ["manage.py", "runserver", "0.0.0.0:80", "--noreload"]) is True


def test_pytest_never_autoconnects() -> None:
    # pytest je v sys.modules, ker ta test tece pod njim.
    assert should_autoconnect(["gunicorn", "missionplanner.wsgi"]) is False
