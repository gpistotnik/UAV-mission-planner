"""Testi zaporedja zagon misije (auto-arm)."""
from __future__ import annotations

from typing import Any

from missions.services.mission_start import start_uploaded_mission


class FakeMissionBridge:
    def __init__(
        self,
        *,
        mode: str = "STABILIZE",
        armed: bool = False,
        connected: bool = True,
        arm_ok: bool = True,
        mode_ok: bool = True,
        start_ok: bool = True,
    ) -> None:
        self.mode = mode
        self.armed = armed
        self.connected = connected
        self.arm_ok = arm_ok
        self.mode_ok = mode_ok
        self.start_ok = start_ok
        self.calls: list[str] = []

    def is_connected(self) -> bool:
        return self.connected

    def get_snapshot(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "heartbeat": {"mode": self.mode, "armed": self.armed},
        }

    def set_mode(self, name: str) -> dict[str, Any]:
        self.calls.append(f"mode:{name}")
        if not self.mode_ok:
            return {"ok": False, "error": f"{name} zavrnjen"}
        self.mode = name.upper()
        return {"ok": True, "label": f"MODE {name}"}

    def arm(self, arm: bool = True, force: bool = False) -> dict[str, Any]:
        self.calls.append(f"arm:{arm}")
        if arm and not self.arm_ok:
            return {
                "ok": False,
                "error": "ARM: DENIED",
                "statustexts": [{"text": "PreArm: Need AltEstimate"}],
            }
        self.armed = bool(arm)
        return {"ok": True, "label": "ARM" if arm else "DISARM"}

    def start_mission(self, first_item: int = 0, last_item: int = 0) -> dict[str, Any]:
        self.calls.append("mission_start")
        if not self.start_ok:
            return {"ok": False, "error": "MISSION_START: FAILED"}
        return {"ok": True, "label": "MISSION_START"}


def test_disarmed_in_auto_goes_guided_then_arm_then_auto_start() -> None:
    """Klasična past: ARM v AUTO je zavrnjen → najprej GUIDED."""
    b = FakeMissionBridge(mode="AUTO", armed=False)
    res = start_uploaded_mission(b)
    assert res["ok"] is True
    assert b.calls == [
        "mode:GUIDED",
        "arm:True",
        "mode:AUTO",
        "mission_start",
    ]


def test_disarmed_in_loiter_arms_directly() -> None:
    b = FakeMissionBridge(mode="LOITER", armed=False)
    res = start_uploaded_mission(b)
    assert res["ok"] is True
    assert b.calls == ["arm:True", "mode:AUTO", "mission_start"]


def test_already_armed_skips_arm() -> None:
    b = FakeMissionBridge(mode="LOITER", armed=True)
    res = start_uploaded_mission(b)
    assert res["ok"] is True
    assert b.calls == ["mode:AUTO", "mission_start"]
    assert all(c != "arm:True" for c in b.calls)


def test_arm_failure_stops_before_auto() -> None:
    b = FakeMissionBridge(mode="GUIDED", armed=False, arm_ok=False)
    res = start_uploaded_mission(b)
    assert res["ok"] is False
    assert "arm:True" in b.calls
    assert "mode:AUTO" not in b.calls
    assert "mission_start" not in b.calls
    assert "DENIED" in (res.get("error") or "")


def test_skip_arm_only_auto_and_start() -> None:
    b = FakeMissionBridge(mode="STABILIZE", armed=False)
    res = start_uploaded_mission(b, skip_arm=True)
    assert res["ok"] is True
    assert b.calls == ["mode:AUTO", "mission_start"]


def test_no_connection() -> None:
    b = FakeMissionBridge(connected=False)
    res = start_uploaded_mission(b)
    assert res["ok"] is False
    assert res["steps"] == []
