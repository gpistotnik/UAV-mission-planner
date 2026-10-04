"""Varna zaustavitev RPi (sync + poweroff).

Namen: prepreciti korupcijo SD/gita ob nenadnem izklopu napajanja.
Zaporedje:

1. zavrni, ce je Pixhawk armiran (razen ``force``),
2. ``sync`` --- flush datotecnega predpomnilnika na disk,
3. zakasnjen ``poweroff`` (da HTTP odgovor se lahko odide).
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
from typing import Any

LOGGER = logging.getLogger(__name__)

POWEROFF_DELAY_S = 2.0


def _is_armed() -> bool:
    try:
        from .mavlink_bridge import get_bridge
        snap = get_bridge().get_snapshot()
        hb = snap.get("heartbeat") or {}
        return bool(hb.get("armed"))
    except Exception:  # noqa: BLE001
        return False


def sync_filesystems() -> None:
    """Flush predpomnilnik na disk (best-effort)."""
    try:
        os.sync()
    except (AttributeError, OSError) as exc:
        # Windows nima os.sync; na RPi pa mora delati.
        LOGGER.warning("os.sync ni uspel: %s", exc)
    try:
        subprocess.run(["sync"], check=False, timeout=30,
                       capture_output=True)
    except (OSError, subprocess.SubprocessError) as exc:
        LOGGER.warning("ukaz sync ni uspel: %s", exc)


def _run_poweroff() -> None:
    sync_filesystems()
    # Najprej systemctl (systemd), nato /sbin/poweroff.
    candidates = [
        ["sudo", "-n", "systemctl", "poweroff"],
        ["sudo", "-n", "/sbin/poweroff"],
        ["sudo", "-n", "/usr/sbin/poweroff"],
    ]
    for cmd in candidates:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            LOGGER.warning("poweroff ukaz spodletel %s: %s", cmd, exc)
            continue
        if result.returncode == 0:
            LOGGER.info("poweroff sprozen prek %s", cmd)
            return
        LOGGER.warning(
            "poweroff %s → %s: %s",
            cmd, result.returncode, (result.stderr or "").strip(),
        )
    LOGGER.error("Nobeden poweroff ukaz ni uspel — preveri sudoers.")


def schedule_poweroff(*, delay_s: float = POWEROFF_DELAY_S) -> None:
    """V ozadju: pocakaj, sync, poweroff."""
    def _worker() -> None:
        import time
        time.sleep(max(0.0, delay_s))
        _run_poweroff()

    threading.Thread(target=_worker, name="poweroff", daemon=True).start()


def request_poweroff(*, force: bool = False) -> dict[str, Any]:
    """Zahteva varno zaustavitev. Vrne JSON-prijazen rezultat."""
    if not force and _is_armed():
        return {
            "ok": False,
            "error": "Pixhawk je ARMIRAN — najprej DISARM ali potrdi "
                     "force=true (nevarno).",
            "armed": True,
        }

    sync_filesystems()
    schedule_poweroff()
    return {
        "ok": True,
        "message": "Datoteke sinhronizirane. RPi se bo ugasnil cez nekaj sekund.",
        "delay_s": POWEROFF_DELAY_S,
        "armed": False,
    }
