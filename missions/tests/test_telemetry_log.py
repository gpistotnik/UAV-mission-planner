"""Testi zapisovalnika telemetrije."""
from __future__ import annotations

import json

import pytest

from missions.services.telemetry_log import (
    TelemetryLogger, list_sessions, msg_to_row,
)


class FakeMsg:
    """Nadomestek pymavlink sporocila."""

    def __init__(self, mtype: str, **fields) -> None:
        self._type = mtype
        self._fields = fields
        for k, v in fields.items():
            setattr(self, k, v)

    def get_type(self) -> str:
        return self._type

    def get_fieldnames(self) -> list[str]:
        return list(self._fields)


def test_msg_to_row_includes_type_and_fields() -> None:
    row = msg_to_row(FakeMsg("ATTITUDE", roll=0.1, pitch=-0.2), ts=1000.0)
    assert row["type"] == "ATTITUDE"
    assert row["t"] == pytest.approx(1000.0)
    assert row["roll"] == pytest.approx(0.1)


def test_msg_to_row_decodes_bytes_and_lists() -> None:
    row = msg_to_row(FakeMsg("STATUSTEXT", text=b"PreArm: GPS\x00",
                             voltages=[3700, 3702]))
    assert row["text"] == "PreArm: GPS"
    assert row["voltages"] == [3700, 3702]


def test_write_before_start_is_ignored(tmp_path) -> None:
    log = TelemetryLogger(tmp_path)
    log.write(FakeMsg("ATTITUDE", roll=0.0))
    assert not log.is_active
    assert list(tmp_path.iterdir()) == []


def test_session_writes_only_whitelisted_types(tmp_path) -> None:
    log = TelemetryLogger(tmp_path)
    log.start(mission_id=3, mission_name="Test misija", reason="test")
    log.write(FakeMsg("ATTITUDE", roll=0.1))
    log.write(FakeMsg("SERVO_OUTPUT_RAW", servo1_raw=1500))  # ni na seznamu
    log.write(FakeMsg("GLOBAL_POSITION_INT", lat=460500000, lon=145000000))
    stats = log.stop()

    assert stats["messages"] == 2
    d = log.session_dir
    lines = (d / "telemetry.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert {json.loads(l)["type"] for l in lines} == {
        "ATTITUDE", "GLOBAL_POSITION_INT"}


def test_meta_and_plan_are_written(tmp_path) -> None:
    log = TelemetryLogger(tmp_path)
    log.start(mission_id=7, mission_name="Mreza", plan={"points": [{"lat": 1}]},
              port="/dev/ttyACM0", baud=115200, reason="arm")
    assert (tmp_path / ".active-session").read_text(
        encoding="utf-8") == log.session_dir.name
    log.write(FakeMsg("HEARTBEAT", base_mode=128))
    log.stop(reason="disarm")

    d = log.session_dir
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    assert meta["mission_id"] == 7
    assert meta["reason"] == "arm"
    assert meta["stop_reason"] == "disarm"
    assert meta["ended_at"] is not None
    assert meta["counts"]["HEARTBEAT"] == 1
    assert json.loads((d / "plan.json").read_text(encoding="utf-8"))["points"]
    assert not (tmp_path / ".active-session").exists()


def test_double_start_does_not_open_second_session(tmp_path) -> None:
    log = TelemetryLogger(tmp_path)
    first = log.start(mission_name="a")
    second = log.start(mission_name="b")
    assert second["dir"] == first["dir"]
    assert len([p for p in tmp_path.iterdir() if p.is_dir()]) == 1
    log.stop()


def test_stop_without_start_is_noop(tmp_path) -> None:
    assert TelemetryLogger(tmp_path).stop() == {"active": False}


def test_list_sessions_reports_summaries(tmp_path) -> None:
    log = TelemetryLogger(tmp_path)
    log.start(mission_name="Let 1", plan={"points": []})
    log.write(FakeMsg("HEARTBEAT", base_mode=0))
    log.stop()

    sessions = list_sessions(tmp_path)
    assert len(sessions) == 1
    s = sessions[0]
    assert s["mission_name"] == "Let 1"
    assert s["running"] is False
    assert s["has_plan"] is True
    assert s["has_captures"] is False
    assert s["size_b"] > 0


def test_list_sessions_on_missing_dir() -> None:
    assert list_sessions("/ni/take/mape") == []


def test_session_name_is_filesystem_safe(tmp_path) -> None:
    log = TelemetryLogger(tmp_path)
    log.start(mission_name="Mreža / test #1: šumniki")
    name = log.session_dir.name
    log.stop()
    assert "/" not in name and " " not in name and ":" not in name
