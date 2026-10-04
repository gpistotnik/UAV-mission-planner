"""Testi MAVLink protokola brez strojne opreme.

Namesto pymavlink povezave uporabimo dvojnik (:class:`FakeMav`), ki se
odziva tako, kot bi se ArduCopter: na ``MISSION_COUNT`` zahteva vse iteme,
po zadnjem poslje ``MISSION_ACK``, na ``COMMAND_LONG`` pa ``COMMAND_ACK``.
Tako je mogoce preveriti tudi obnasanje ob napakah (zavrnitev, timeout,
ponovljena zahteva), kar na pravem dronu ni ponovljivo.
"""
from __future__ import annotations

import pytest

from missions.services.mavlink_bridge import MavlinkBridge, mavlink_available
from missions.services.mavlink_mission import build_mission_items

pytestmark = pytest.mark.skipif(
    not mavlink_available(), reason="pymavlink ni namescen")


class FakeMsg:
    def __init__(self, mtype: str, **fields) -> None:
        self._type = mtype
        for k, v in fields.items():
            setattr(self, k, v)

    def get_type(self) -> str:
        return self._type

    def get_fieldnames(self) -> list[str]:
        return []


class FakeMavSender:
    """Nadomestek ``mav.mav`` --- objekta, ki serializira sporocila."""

    def __init__(self, parent: "FakeMav") -> None:
        self.parent = parent

    def mission_count_send(self, _sys, _comp, count, _mtype) -> None:
        self.parent.requested_count = count
        # Krmilnik takoj zahteva vse iteme po vrsti.
        for seq in range(count):
            self.parent.push(FakeMsg("MISSION_REQUEST_INT", seq=seq))

    def mission_item_int_send(self, _sys, _comp, seq, frame, command, current,
                              autocont, p1, p2, p3, p4, x, y, z, _mtype) -> None:
        self.parent.sent.append({
            "seq": seq, "frame": frame, "command": command,
            "current": current, "param1": p1, "param2": p2,
            "x": x, "y": y, "z": z,
        })
        if len(self.parent.sent) >= (self.parent.requested_count or 0):
            self.parent.push(FakeMsg("MISSION_ACK",
                                     type=self.parent.ack_code))

    def command_long_send(self, _sys, _comp, command, _conf,
                          p1, p2, p3, p4, p5, p6, p7) -> None:
        self.parent.commands.append({"command": command, "param1": p1, "param2": p2})
        if (self.parent.command_result is not None
                and len(self.parent.commands) >= self.parent.command_response_after):
            self.parent.push_command(FakeMsg(
                "COMMAND_ACK", command=command,
                result=self.parent.command_result))

    def request_data_stream_send(self, *_a, **_kw) -> None:
        pass

    def param_request_read_send(self, _sys, _comp, param_id, _index) -> None:
        name = param_id.split(b"\x00", 1)[0].decode("ascii") if isinstance(
            param_id, bytes) else str(param_id).split("\x00", 1)[0]
        self.parent.param_reads.append(name.upper())
        if (self.parent.param_values is not None
                and len(self.parent.param_reads) >= self.parent.param_response_after
                and name.upper() in self.parent.param_values):
            val = self.parent.param_values[name.upper()]
            self.parent.push_param(FakeMsg(
                "PARAM_VALUE", param_id=name.upper().encode("ascii"),
                param_value=float(val), param_type=9,
                param_index=0, param_count=1))

    def param_set_send(self, _sys, _comp, param_id, value, ptype) -> None:
        name = param_id.split(b"\x00", 1)[0].decode("ascii") if isinstance(
            param_id, bytes) else str(param_id).split("\x00", 1)[0]
        self.parent.param_sets.append({"name": name.upper(), "value": float(value),
                                       "type": int(ptype)})
        if self.parent.param_set_ok:
            self.parent.param_values[name.upper()] = float(value)
            self.parent.push_param(FakeMsg(
                "PARAM_VALUE", param_id=name.upper().encode("ascii"),
                param_value=float(value), param_type=int(ptype),
                param_index=0, param_count=1))


class FakeMav:
    """Minimalen dvojnik ``mavutil.mavfile``."""

    target_system = 1
    target_component = 1

    def __init__(self, bridge: MavlinkBridge, ack_code: int = 0,
                 command_result: int | None = 0) -> None:
        self.bridge = bridge
        self.mav = FakeMavSender(self)
        self.sent: list[dict] = []
        self.commands: list[dict] = []
        self.requested_count: int | None = None
        self.ack_code = ack_code
        self.command_result = command_result
        self.command_response_after = 1
        self.param_reads: list[str] = []
        self.param_response_after = 1
        self.param_sets: list[dict] = []
        self.param_values: dict[str, float] = {"ARMING_MAGTHRESH": 100.0}
        self.param_set_ok = True

    def push(self, msg: FakeMsg) -> None:
        self.bridge._mission_events.put_nowait(msg)

    def push_command(self, msg: FakeMsg) -> None:
        self.bridge._command_events.put_nowait(msg)

    def push_param(self, msg: FakeMsg) -> None:
        self.bridge._param_events.put_nowait(msg)

    def mode_mapping(self) -> dict[str, int]:
        return {"STABILIZE": 0, "AUTO": 3, "GUIDED": 4, "RTL": 6, "LAND": 9}

    def close(self) -> None:
        pass


@pytest.fixture
def bridge() -> MavlinkBridge:
    b = MavlinkBridge(log_dir=None, log_trigger="off")
    b._snap.connected = True
    return b


def _items(n: int = 3):
    from dataclasses import dataclass

    @dataclass
    class P:
        lat: float
        lon: float
        altitude_m: float = 50.0
        speed_ms = None
        heading_mode: str = "AUTO"
        heading_deg = None
        hover_time_s: float = 0.0
        action_type: str = "NONE"
        map_block = None
        trigger_distance_m = None

    pts = [P(46.05 + i * 0.001, 14.50) for i in range(n)]
    return build_mission_items(pts, finish_action="RTH").items


# ---------------------------------------------------------------------------
# Prenos misije
# ---------------------------------------------------------------------------
def test_upload_without_connection_fails(bridge: MavlinkBridge) -> None:
    bridge._snap.connected = False
    res = bridge.upload_mission(_items())
    assert res["ok"] is False
    assert "povezave" in res["error"]


def test_upload_empty_mission_fails(bridge: MavlinkBridge) -> None:
    res = bridge.upload_mission([])
    assert res["ok"] is False


def test_successful_upload_sends_every_item_once(bridge: MavlinkBridge) -> None:
    items = _items(4)
    fake = FakeMav(bridge)
    bridge._mav = fake

    res = bridge.upload_mission(items)

    assert res["ok"] is True, res
    assert res["count"] == len(items)
    assert res["uploaded"] == len(items)
    assert res["ack"] == "ACCEPTED"
    assert [s["seq"] for s in fake.sent] == list(range(len(items)))
    # Vsebina prvega poslanega itema se mora ujemati z zgrajenim.
    assert fake.sent[0]["command"] == items[0].command
    assert fake.sent[-1]["command"] == items[-1].command


def test_upload_reports_rejection_with_ack_name(bridge: MavlinkBridge) -> None:
    bridge._mav = FakeMav(bridge, ack_code=4)  # NO_SPACE
    res = bridge.upload_mission(_items(2))
    assert res["ok"] is False
    assert res["ack"] == "NO_SPACE"
    assert "zavrnil" in res["error"]


def test_upload_times_out_when_controller_is_silent(
    bridge: MavlinkBridge, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SilentSender(FakeMavSender):
        def mission_count_send(self, _sys, _comp, count, _mtype) -> None:
            self.parent.requested_count = count  # brez zahtev

    fake = FakeMav(bridge)
    fake.mav = SilentSender(fake)
    bridge._mav = fake

    from missions.services import mavlink_bridge as mb
    res = mb._bridge_upload_mission(bridge, _items(2), timeout_per_step=0.2)
    assert res["ok"] is False
    assert "Timeout" in res["error"]


def test_concurrent_upload_is_refused(bridge: MavlinkBridge) -> None:
    bridge._mav = FakeMav(bridge)
    bridge._upload_lock.acquire()
    try:
        res = bridge.upload_mission(_items(2))
        assert res["ok"] is False
        assert "v teku" in res["error"]
    finally:
        bridge._upload_lock.release()


def test_repeated_request_is_answered_again(bridge: MavlinkBridge) -> None:
    """Krmilnik lahko ponovi zahtevo, ce se je paket izgubil."""
    class RepeatingSender(FakeMavSender):
        def mission_count_send(self, _sys, _comp, count, _mtype) -> None:
            self.parent.requested_count = count
            for seq in range(count):
                self.parent.push(FakeMsg("MISSION_REQUEST_INT", seq=seq))
            # Podvojena zahteva za seq 0.
            self.parent.push(FakeMsg("MISSION_REQUEST_INT", seq=0))

    fake = FakeMav(bridge)
    fake.mav = RepeatingSender(fake)
    bridge._mav = fake

    res = bridge.upload_mission(_items(3))
    assert res["ok"] is True
    # Vsi seq so bili potrjeni, tudi ce je bil kaksen poslan dvakrat.
    assert res["uploaded"] == res["count"]


# ---------------------------------------------------------------------------
# Ukazi
# ---------------------------------------------------------------------------
def test_arm_sends_command_and_reads_ack(bridge: MavlinkBridge) -> None:
    fake = FakeMav(bridge, command_result=0)
    bridge._mav = fake

    res = bridge.arm(True)
    assert res["ok"] is True
    assert res["result_name"] == "ACCEPTED"
    assert fake.commands[0]["param1"] == pytest.approx(1.0)


def test_disarm_sends_zero_param(bridge: MavlinkBridge) -> None:
    fake = FakeMav(bridge, command_result=0)
    bridge._mav = fake
    bridge.arm(False)
    assert fake.commands[0]["param1"] == pytest.approx(0.0)


def test_force_disarm_uses_magic_value(bridge: MavlinkBridge) -> None:
    fake = FakeMav(bridge, command_result=0)
    bridge._mav = fake
    bridge.arm(False, force=True)
    assert fake.commands[0]["param2"] == pytest.approx(21196.0)


def test_rejected_command_reports_result_name(bridge: MavlinkBridge) -> None:
    bridge._mav = FakeMav(bridge, command_result=4)  # FAILED
    res = bridge.arm(True)
    assert res["ok"] is False
    assert res["result_name"] == "FAILED"


def test_command_timeout_without_ack(bridge: MavlinkBridge) -> None:
    bridge._mav = FakeMav(bridge, command_result=None)
    res = bridge.send_command_long(400, 1.0, timeout=0.2, label="ARM")
    assert res["ok"] is False
    assert "ACK" in res["error"]


def test_command_can_retry_missing_ack(bridge: MavlinkBridge) -> None:
    fake = FakeMav(bridge, command_result=0)
    fake.command_response_after = 2
    bridge._mav = fake

    res = bridge.send_command_long(
        42424, timeout=0.05, label="RETRY", retries=1)

    assert res["ok"] is True
    assert len(fake.commands) == 2


def test_set_mode_rejects_unknown_mode(bridge: MavlinkBridge) -> None:
    bridge._mav = FakeMav(bridge)
    res = bridge.set_mode("TELEPORT")
    assert res["ok"] is False
    assert "AUTO" in res["available"]


def test_set_mode_sends_custom_mode_id(bridge: MavlinkBridge) -> None:
    fake = FakeMav(bridge, command_result=0)
    bridge._mav = fake
    res = bridge.set_mode("auto")
    assert res["ok"] is True
    assert fake.commands[0]["param2"] == pytest.approx(3.0)


def test_mission_start_sends_command(bridge: MavlinkBridge) -> None:
    fake = FakeMav(bridge, command_result=0)
    bridge._mav = fake
    assert bridge.start_mission()["ok"] is True
    assert len(fake.commands) == 1


def test_command_without_connection_fails(bridge: MavlinkBridge) -> None:
    bridge._snap.connected = False
    assert bridge.arm(True)["ok"] is False


# ---------------------------------------------------------------------------
# Obdelava telemetrije
# ---------------------------------------------------------------------------
def test_battery_status_updates_snapshot(bridge: MavlinkBridge) -> None:
    """Regresijski test: podvojena veja je to sporocilo prej ignorirala."""
    msg = FakeMsg("BATTERY_STATUS",
                  voltages=[3700, 3702, 3698, 0xFFFF, 0xFFFF, 0xFFFF],
                  current_battery=1250, battery_remaining=87)
    bridge._apply_message(msg)
    snap = bridge.get_snapshot()
    assert snap["battery"]["voltage_v"] == pytest.approx(11.100, abs=1e-3)
    assert snap["battery"]["current_a"] == pytest.approx(12.5)
    assert snap["battery"]["remaining_pct"] == 87


def test_mission_progress_from_messages(bridge: MavlinkBridge) -> None:
    bridge._apply_message(FakeMsg("MISSION_CURRENT", seq=5))
    bridge._apply_message(FakeMsg("MISSION_ITEM_REACHED", seq=4))
    mission = bridge.get_snapshot()["mission"]
    assert mission["current_seq"] == 5
    assert mission["reached_seq"] == 4


def test_statustext_is_decoded_and_kept(bridge: MavlinkBridge) -> None:
    bridge._apply_message(FakeMsg("STATUSTEXT", severity=2,
                                  text=b"PreArm: GPS ni pripravljen\x00"))
    texts = bridge.get_snapshot()["statustexts"]
    assert texts[-1]["text"] == "PreArm: GPS ni pripravljen"
    assert texts[-1]["severity"] == 2


def test_scaled_imu_and_sys_status_compass(bridge: MavlinkBridge) -> None:
    bridge._apply_message(FakeMsg("SCALED_IMU", xmag=300, ymag=400, zmag=0))
    # MAG=4, GPS=32 — enabled in healthy.
    bridge._apply_message(FakeMsg(
        "SYS_STATUS",
        voltage_battery=12600, current_battery=50, battery_remaining=90,
        onboard_control_sensors_enabled=4 | 32,
        onboard_control_sensors_health=4 | 32,
    ))
    snap = bridge.get_snapshot()
    assert snap["compass"]["field_mg"] == pytest.approx(500.0)
    assert snap["compass"]["xy_mg"] == pytest.approx(500.0)
    assert snap["compass"]["healthy"] is True
    assert snap["gps"]["healthy"] is True


def test_mag_cal_progress_updates_snapshot(bridge: MavlinkBridge) -> None:
    bridge._apply_message(FakeMsg(
        "MAG_CAL_PROGRESS", compass_id=0, cal_status=2,
        attempt=1, completion_pct=42))
    cal = bridge.get_snapshot()["compass"]
    assert cal["cal_pct"] == 42
    assert cal["cal_status"] == 2
    assert cal["cal_status_label"] == "korak 1"


def test_start_mag_cal_rejects_when_armed(bridge: MavlinkBridge) -> None:
    bridge._snap.connected = True
    bridge._snap.armed = True
    res = bridge.start_mag_cal()
    assert res["ok"] is False
    assert "DISARMED" in (res.get("error") or "")


def test_get_param_reads_value(bridge: MavlinkBridge) -> None:
    fake = FakeMav(bridge)
    bridge._mav = fake
    res = bridge.get_param("ARMING_MAGTHRESH")
    assert res["ok"] is True
    assert res["value"] == pytest.approx(100.0)
    assert fake.param_reads == ["ARMING_MAGTHRESH"]


def test_get_param_retries_dropped_request(bridge: MavlinkBridge) -> None:
    fake = FakeMav(bridge)
    fake.param_response_after = 2
    bridge._mav = fake

    res = bridge.get_param("ARMING_MAGTHRESH", timeout=0.15)

    assert res["ok"] is True
    assert fake.param_reads == ["ARMING_MAGTHRESH", "ARMING_MAGTHRESH"]


def test_set_param_writes_and_confirms(bridge: MavlinkBridge) -> None:
    fake = FakeMav(bridge)
    bridge._mav = fake
    res = bridge.set_param("ARMING_MAGTHRESH", 150)
    assert res["ok"] is True
    assert res["value"] == pytest.approx(150.0)
    assert fake.param_sets[0]["value"] == pytest.approx(150.0)


def test_param_value_routed_to_queue_not_snapshot(bridge: MavlinkBridge) -> None:
    before = bridge.get_snapshot()["messages_received"]
    bridge._apply_message(FakeMsg(
        "PARAM_VALUE", param_id=b"ARMING_MAGTHRESH", param_value=120.0,
        param_type=9, param_index=0, param_count=1))
    assert bridge.get_snapshot()["messages_received"] == before
    assert not bridge._param_events.empty()


def test_command_ack_is_routed_to_queue_not_snapshot(bridge: MavlinkBridge) -> None:
    before = bridge.get_snapshot()["messages_received"]
    bridge._apply_message(FakeMsg("COMMAND_ACK", command=400, result=0))
    assert bridge.get_snapshot()["messages_received"] == before
    assert not bridge._command_events.empty()


# ---------------------------------------------------------------------------
# Samodejno prozenje zapisa telemetrije
# ---------------------------------------------------------------------------
def _bridge_with_log(tmp_path, trigger: str) -> MavlinkBridge:
    b = MavlinkBridge(log_dir=tmp_path, log_trigger=trigger)
    b._snap.connected = True
    return b


def _hb(armed: bool) -> FakeMsg:
    return FakeMsg("HEARTBEAT", base_mode=128 if armed else 0,
                   custom_mode=0, system_status=3)


def _pos(rel_alt_m: float) -> FakeMsg:
    return FakeMsg("GLOBAL_POSITION_INT", lat=460500000, lon=145000000,
                   alt=350000, relative_alt=int(rel_alt_m * 1000), hdg=9000)


def _ess(landed_state: int) -> FakeMsg:
    return FakeMsg("EXTENDED_SYS_STATE", landed_state=landed_state,
                   vtol_state=0)


def test_arming_alone_does_not_start_log(tmp_path) -> None:
    """Test motorjev na tleh ne sme ustvariti seje."""
    b = _bridge_with_log(tmp_path, "takeoff")
    b._apply_message(_hb(True))
    b._apply_message(_pos(0.0))
    b._apply_message(_ess(1))            # ON_GROUND
    assert b.logger is not None and not b.logger.is_active
    assert list(tmp_path.iterdir()) == []


def test_log_starts_on_takeoff_and_stops_on_landing(tmp_path) -> None:
    b = _bridge_with_log(tmp_path, "takeoff")
    b._apply_message(_hb(True))
    b._apply_message(_pos(0.0))
    b._apply_message(_ess(1))
    assert not b.logger.is_active

    b._apply_message(_ess(3))            # TAKEOFF
    assert b.logger.is_active, "zapis se mora zaceti ob vzletu"
    assert b.get_snapshot()["flight"]["airborne"] is True

    b._apply_message(_pos(3.0))
    b._apply_message(_ess(2))            # IN_AIR
    assert b.logger.is_active

    b._apply_message(_ess(1))            # ON_GROUND
    assert not b.logger.is_active, "zapis se mora koncati ob pristanku"
    assert b.get_snapshot()["flight"]["airborne"] is False

    sessions = [d for d in tmp_path.iterdir() if d.is_dir()]
    assert len(sessions) == 1
    assert (sessions[0] / "telemetry.jsonl").is_file()


def test_takeoff_detected_from_altitude_without_extended_state(tmp_path) -> None:
    b = _bridge_with_log(tmp_path, "takeoff")
    b._apply_message(_hb(True))
    b._apply_message(_pos(0.1))
    assert not b.logger.is_active
    b._apply_message(_pos(2.0))
    assert b.logger.is_active


def test_disarm_closes_log_even_without_landed_state(tmp_path) -> None:
    b = _bridge_with_log(tmp_path, "takeoff")
    b._apply_message(_hb(True))
    b._apply_message(_pos(3.0))
    assert b.logger.is_active
    b._apply_message(_hb(False))
    assert not b.logger.is_active


def test_arm_trigger_mode_keeps_old_behaviour(tmp_path) -> None:
    b = _bridge_with_log(tmp_path, "arm")
    b._apply_message(_hb(True))
    assert b.logger.is_active, "v nacinu 'arm' se zapis zacne ze ob armanju"
    b._apply_message(_hb(False))
    assert not b.logger.is_active


def test_off_trigger_never_starts_log(tmp_path) -> None:
    b = _bridge_with_log(tmp_path, "off")
    b._apply_message(_hb(True))
    b._apply_message(_pos(5.0))
    b._apply_message(_ess(2))
    assert not b.logger.is_active


def test_extended_sys_state_is_exposed_in_snapshot(tmp_path) -> None:
    b = _bridge_with_log(tmp_path, "off")
    b._apply_message(_ess(2))
    assert b.get_snapshot()["flight"]["landed_state"] == 2
