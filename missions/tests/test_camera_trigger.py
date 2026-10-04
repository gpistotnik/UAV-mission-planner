"""Filesystem testi procesa za časovno sledljiv zajem slik."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

import pytest

from core.storage import StorageUnavailableError
from core.storage import publish_active_session
from scripts.camera_trigger import CaptureAgent
from scripts.camera_trigger import build_exif

try:
    import piexif
except ImportError:  # pragma: no cover
    piexif = None


def _args(out: Optional[Path]) -> argparse.Namespace:
    return argparse.Namespace(
        out=str(out) if out is not None else None,
        width=640,
        height=480,
        camera=0,
        dry_run=True,
        connect="udp:127.0.0.1:14551",
        baud=921600,
        on_waypoint=True,
    )


def test_capture_event_is_logged_in_active_flight(tmp_path: Path) -> None:
    session = tmp_path / "20260726-120000_test"
    session.mkdir()
    publish_active_session(tmp_path, session)
    agent = CaptureAgent(_args(tmp_path))

    agent.on_trigger("CAMERA_TRIGGER", 4, 1_774_000_000.0)

    rows = (session / "captures.jsonl").read_text(
        encoding="utf-8").splitlines()
    row = json.loads(rows[0])
    assert row["trigger"] == "CAMERA_TRIGGER"
    assert row["seq"] == 4
    assert row["captured"] is False
    assert (session / "images").is_dir()


def test_capture_without_active_flight_stays_on_usb_root(
    tmp_path: Path,
) -> None:
    agent = CaptureAgent(_args(tmp_path))

    agent.on_trigger("CAMERA_TRIGGER", 1, 1_774_000_000.0)

    logs = list((tmp_path / "unassigned-captures").glob(
        "*/captures.jsonl"))
    assert len(logs) == 1
    assert json.loads(logs[0].read_text(encoding="utf-8"))["seq"] == 1


def test_capture_is_skipped_when_usb_is_missing(
    monkeypatch, capsys,
) -> None:
    def unavailable(_root):
        raise StorageUnavailableError("Ni USB ključka.")

    monkeypatch.setattr("scripts.camera_trigger.flight_log_dir", unavailable)
    agent = CaptureAgent(_args(None))

    agent.on_trigger("CAMERA_TRIGGER", 1, 1_774_000_000.0)

    assert agent.counter == 0
    assert "USB medij ni na voljo" in capsys.readouterr().err


@pytest.mark.skipif(piexif is None, reason="piexif ni nameščen")
def test_build_exif_writes_msl_altitude_not_relative() -> None:
    """GPSAltitude mora vsebovati absolutno nadmorsko višino, ne AGL."""
    alt_msl_m = 312.4
    exif = build_exif(46.05, 14.51, alt_msl_m, 90.0, 1_774_000_000.0)
    assert exif is not None
    gps = piexif.load(exif)["GPS"]
    num, den = gps[piexif.GPSIFD.GPSAltitude]
    assert num / den == pytest.approx(alt_msl_m, abs=0.01)
    assert gps[piexif.GPSIFD.GPSAltitudeRef] == 0
