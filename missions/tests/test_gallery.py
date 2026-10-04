"""Testi galerije in dashboard shutter zajema."""
from __future__ import annotations

import json
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from core.storage import publish_active_session
from missions.services.gallery import (
    build_zip, collect_keys, list_gallery, resolve_capture_dir,
    resolve_image, save_capture, snapshot_url,
)

# Minimalen veljaven 1×1 JPEG.
MINI_JPEG = (
    b"\xff\xd8\xff\xdb\x00C\x00"
    + bytes([8] * 64)
    + b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
    + b"\xff\xc4\x00\x14\x00\x01\x00\x00\x00\x00\x00\x00\x00"
      b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    + b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00\x7f\xff\xd9"
)


def test_snapshot_url_from_auto() -> None:
    assert snapshot_url("auto") == "http://127.0.0.1:8090/snapshot.jpg"


def test_snapshot_url_rewrites_host_to_local() -> None:
    assert snapshot_url("http://dron.local:8090/stream.mjpg") == (
        "http://127.0.0.1:8090/snapshot.jpg")


def test_save_capture_uses_active_session(tmp_path: Path) -> None:
    session = tmp_path / "20260802-120000_test"
    session.mkdir()
    (session / "meta.json").write_text(
        json.dumps({"mission_id": 3, "mission_name": "Survey"}),
        encoding="utf-8")
    publish_active_session(tmp_path, session)

    result = save_capture(MINI_JPEG, tmp_path, mission_id=99, mission_name="X")
    assert result["ok"] is True
    assert (session / "images" / result["file"]).is_file()
    assert "gallery" not in result["session"]


def test_save_capture_mission_gallery_folder(tmp_path: Path) -> None:
    result = save_capture(
        MINI_JPEG, tmp_path, mission_id=7, mission_name="Mapiranje A")
    assert result["session"].startswith("gallery/m7_")
    path = resolve_image(tmp_path, result["session"], result["file"])
    assert path.read_bytes()[:2] == b"\xff\xd8"
    meta = json.loads(
        (tmp_path / result["session"] / "meta.json").read_text(encoding="utf-8"))
    assert meta["mission_id"] == 7


def test_save_capture_unassigned_without_mission(tmp_path: Path) -> None:
    result = save_capture(MINI_JPEG, tmp_path)
    assert result["session"].startswith("unassigned-captures/")


def _photo(size: int = 40_000) -> bytes:
    return MINI_JPEG + b"\x00" * (size - len(MINI_JPEG))


def test_list_gallery_groups_by_mission(tmp_path: Path) -> None:
    save_capture(_photo(), tmp_path, mission_id=1, mission_name="A")
    save_capture(_photo(), tmp_path, mission_id=1, mission_name="A")
    save_capture(_photo(), tmp_path)  # other

    data = list_gallery(tmp_path)
    assert data["total_count"] == 3
    assert data["mission_count"] == 1
    assert data["missions"][0]["mission_id"] == 1
    assert data["missions"][0]["count"] == 2
    assert data["other_count"] == 1


def test_list_gallery_skips_placeholder_jpegs(tmp_path: Path) -> None:
    save_capture(MINI_JPEG, tmp_path, mission_id=4, mission_name="Fake")
    save_capture(_photo(), tmp_path, mission_id=4, mission_name="Fake")
    data = list_gallery(tmp_path)
    assert data["total_count"] == 1
    assert data["missions"][0]["count"] == 1


def test_list_gallery_does_not_infer_far_unassigned(tmp_path: Path) -> None:
    session = tmp_path / "20260811-191444_waypoint-test"
    session.mkdir()
    (session / "meta.json").write_text(
        json.dumps({"mission_id": 2, "mission_name": "Waypoint test"}),
        encoding="utf-8")
    loose = tmp_path / "unassigned-captures" / "20260811" / "images"
    loose.mkdir(parents=True)
    (loose / "img_00001_20260811-150000.jpg").write_bytes(_photo())
    data = list_gallery(tmp_path)
    assert data["mission_count"] == 0
    assert data["other_count"] == 1


def test_list_gallery_infers_mission_from_nearby_session(tmp_path: Path) -> None:
    session = tmp_path / "20260811-191444_waypoint-test"
    session.mkdir()
    (session / "meta.json").write_text(
        json.dumps({"mission_id": 2, "mission_name": "Waypoint test"}),
        encoding="utf-8")
    loose = tmp_path / "unassigned-captures" / "20260811" / "images"
    loose.mkdir(parents=True)
    (loose / "img_00001_20260811-191436.jpg").write_bytes(_photo())
    data = list_gallery(tmp_path)
    assert data["mission_count"] == 1
    assert data["missions"][0]["mission_id"] == 2
    assert data["other_count"] == 0


def test_collect_keys_and_zip(tmp_path: Path) -> None:
    a = save_capture(_photo(), tmp_path, mission_id=2, mission_name="B")
    b = save_capture(_photo(), tmp_path)
    keys = collect_keys(tmp_path, mission_id=2)
    assert a["rel"] in keys
    assert b["rel"] not in keys

    other = collect_keys(tmp_path, scope="other")
    assert b["rel"] in other

    buf = build_zip(tmp_path, keys)
    with zipfile.ZipFile(BytesIO(buf.getvalue())) as zf:
        names = zf.namelist()
    assert any(a["file"] in n for n in names)


def test_resolve_image_rejects_traversal(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        resolve_image(tmp_path, "../etc", "passwd.jpg")


def test_resolve_capture_dir_prefers_active(tmp_path: Path) -> None:
    session = tmp_path / "seja"
    session.mkdir()
    publish_active_session(tmp_path, session)
    assert resolve_capture_dir(tmp_path, mission_id=1) == session


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def open_control(settings, tmp_path):
    settings.UAV_REQUIRE_AUTH = False
    settings.UAV_STORAGE_ROOT = ""
    settings.UAV_LOG_DIR = tmp_path / "flightlogs"
    settings.UAV_LOG_DIR.mkdir(parents=True, exist_ok=True)
    settings.UAV_RUNTIME_DIR = tmp_path / "run"
    return settings


@pytest.fixture
def mission(db):
    from decimal import Decimal
    from missions.models import DroneProfile, Mission
    drone = DroneProfile.objects.create(
        slug="gal-test", display_name="Gal Test",
        sensor_width_mm=Decimal("6.17"), sensor_height_mm=Decimal("4.55"),
        focal_length_mm=Decimal("3.6"),
        image_width_px=4000, image_height_px=3000,
    )
    return Mission.objects.create(name="GalTest", drone=drone)


def test_gallery_page_renders(client) -> None:
    r = client.get("/galerija/")
    assert r.status_code == 200
    assert "Galerija" in r.content.decode()


def test_gallery_list_api(client, settings, tmp_path) -> None:
    settings.UAV_STORAGE_ROOT = ""
    settings.UAV_LOG_DIR = tmp_path
    save_capture(_photo(), tmp_path, mission_id=5, mission_name="X")
    # mission_id 5 ni v DB — list bere samo disk/meta
    r = client.get("/api/gallery/")
    assert r.status_code == 200
    data = r.json()
    assert data["total_count"] == 1
    assert data["missions"][0]["mission_id"] == 5


def test_gallery_image_and_download(client, settings, tmp_path, mission) -> None:
    settings.UAV_STORAGE_ROOT = ""
    settings.UAV_LOG_DIR = tmp_path
    saved = save_capture(
        _photo(), tmp_path,
        mission_id=mission.pk, mission_name=mission.name)

    img = client.get(f"/api/gallery/image/{saved['session']}/{saved['file']}")
    assert img.status_code == 200
    assert "image/" in img["Content-Type"]

    z = client.post(
        "/api/gallery/download/",
        data=json.dumps({"mission_id": mission.pk}),
        content_type="application/json",
    )
    assert z.status_code == 200
    assert z["Content-Type"] == "application/zip"
    with zipfile.ZipFile(BytesIO(z.content)) as zf:
        assert len(zf.namelist()) == 1


def test_camera_capture_api(client, settings, tmp_path, mission, monkeypatch) -> None:
    settings.UAV_STORAGE_ROOT = ""
    settings.UAV_LOG_DIR = tmp_path
    settings.UAV_REQUIRE_AUTH = False

    monkeypatch.setattr(
        "missions.views.fetch_snapshot", lambda url: MINI_JPEG)

    r = client.post(
        "/api/camera/capture/",
        data=json.dumps({"mission_id": mission.pk}),
        content_type="application/json",
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["mission_id"] == mission.pk
    assert (tmp_path / data["session"] / "images" / data["file"]).is_file()


def test_camera_capture_requires_auth(client, settings, tmp_path, monkeypatch) -> None:
    settings.UAV_STORAGE_ROOT = ""
    settings.UAV_LOG_DIR = tmp_path
    settings.UAV_REQUIRE_AUTH = True
    monkeypatch.setattr(
        "missions.views.fetch_snapshot", lambda url: MINI_JPEG)
    r = client.post(
        "/api/camera/capture/",
        data="{}",
        content_type="application/json",
    )
    assert r.status_code == 403
    assert r.json().get("auth_required") is True


def test_nav_includes_gallery(client) -> None:
    body = client.get("/").content.decode()
    assert 'href="/galerija/"' in body
    assert body.index('href="/nadzor/"') < body.index('href="/galerija/"')
    assert body.index('href="/galerija/"') < body.index('href="/test-polet/"')
