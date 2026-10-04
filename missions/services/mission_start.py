"""Zagon naložene misije na krmilniku (auto-arm).

ArduCopter **privzeto ne dovoli armanja v načinu AUTO** (razen če je
vklopljen bit v ``AUTO_OPTIONS``). Zato je vrstni red:

1. po potrebi preklop v način, ki armanje dovoljuje (``GUIDED``),
2. ``ARM``,
3. ``AUTO``,
4. ``MISSION_START``.

Vsak korak je ločen, da vmesnik pokaže, kje se je zataknilo.
"""
from __future__ import annotations

import time
from typing import Any, Optional

# Načini, v katerih ArduCopter tipično dovoli armanje brez AUTO_OPTIONS.
ARMABLE_MODES = frozenset({
    "STABILIZE", "ACRO", "ALTHOLD", "LOITER", "GUIDED", "GUIDED_NOGPS",
    "POSHOLD", "SPORT", "FLOWHOLD", "DRIFT", "BRAKE", "THROW",
})

# Pred armanjem greš v ta način, če trenutni ni armable (npr. AUTO/RTL).
ARM_PREP_MODE = "GUIDED"


def _snap_mode_armed(bridge: Any) -> tuple[Optional[str], bool]:
    snap = bridge.get_snapshot() if hasattr(bridge, "get_snapshot") else {}
    hb = (snap or {}).get("heartbeat") or {}
    mode = hb.get("mode")
    return (
        str(mode).upper() if mode else None,
        bool(hb.get("armed")),
    )


def _wait_armed(bridge: Any, *, timeout_s: float = 3.0,
                tick_s: float = 0.15) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        _, armed = _snap_mode_armed(bridge)
        if armed:
            return True
        time.sleep(tick_s)
    _, armed = _snap_mode_armed(bridge)
    return armed


def start_uploaded_mission(
    bridge: Any,
    *,
    skip_arm: bool = False,
    skip_mode: bool = False,
    arm_prep_mode: str = ARM_PREP_MODE,
) -> dict[str, Any]:
    """Izvede zaporedje za zagon naložene misije.

    Vrne ``{"ok": bool, "steps": [...], "error": optional}``.
    """
    steps: list[dict[str, Any]] = []

    def run(name: str, res: dict[str, Any]) -> bool:
        entry = {"step": name, **res}
        steps.append(entry)
        return bool(res.get("ok"))

    if not bridge.is_connected():
        return {
            "ok": False,
            "error": "Ni MAVLink povezave s Pixhawkom.",
            "steps": [],
        }

    mode, armed = _snap_mode_armed(bridge)

    if not skip_arm and not armed:
        # 1) Armable način — sicer ArduCopter zavrne ARM v AUTO/RTL/...
        if mode not in ARMABLE_MODES:
            if not run(f"mode_{arm_prep_mode.lower()}",
                       bridge.set_mode(arm_prep_mode)):
                return {
                    "ok": False,
                    "steps": steps,
                    "error": steps[-1].get("error"),
                }
            time.sleep(0.3)

        # 2) ARM
        if not run("arm", bridge.arm(True)):
            return {
                "ok": False,
                "steps": steps,
                "error": steps[-1].get("error"),
            }
        if not _wait_armed(bridge):
            return {
                "ok": False,
                "steps": steps + [{
                    "step": "arm_confirm",
                    "ok": False,
                    "error": "ARM sprejet, a krmilnik ni javil armed v 3 s.",
                }],
                "error": "ARM sprejet, a krmilnik ni javil armed v 3 s.",
            }

    # 3) AUTO (misija)
    if not skip_mode:
        if not run("mode_auto", bridge.set_mode("AUTO")):
            return {
                "ok": False,
                "steps": steps,
                "error": steps[-1].get("error"),
            }
        time.sleep(0.2)

    # 4) MISSION_START
    if not run("mission_start", bridge.start_mission()):
        return {
            "ok": False,
            "steps": steps,
            "error": steps[-1].get("error"),
        }

    return {"ok": True, "steps": steps}
