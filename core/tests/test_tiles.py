"""Lokalne OSM ploščice na disku."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from django.test import Client


@pytest.fixture
def tiles_root(tmp_path: Path) -> Path:
    osm = tmp_path / "osm"
    (osm / "10" / "548").mkdir(parents=True)
    # Različne vsebine (ne enake → ne „poisoned“).
    png_a = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40 + b"A"
    png_b = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40 + b"B"
    (osm / "10" / "548" / "367.png").write_bytes(png_a)
    (osm / "10" / "549").mkdir(parents=True)
    (osm / "10" / "549" / "367.png").write_bytes(png_b)
    (osm / ".ready").write_text(
        json.dumps({
            "provider": "osm",
            "min_zoom": 7,
            "max_zoom": 14,
            "bounds": {
                "south": 45.42, "west": 13.38,
                "north": 46.88, "east": 16.61,
            },
        }),
        encoding="utf-8",
    )
    return tmp_path


def test_tiles_status_missing(client: Client, tmp_path: Path, settings):
    settings.MAP_TILES_ROOT = tmp_path / "empty"
    r = client.get("/tiles/status/")
    assert r.status_code == 200
    assert r.json()["available"] is False


def test_tiles_status_and_serve(client: Client, tiles_root: Path, settings):
    settings.MAP_TILES_ROOT = tiles_root
    settings.MAP_TILES_FETCH_ON_MISS = False
    st = client.get("/tiles/status/")
    assert st.status_code == 200
    body = st.json()
    assert body["available"] is True
    assert body["max_zoom"] == 14
    assert body.get("fetch_on_miss") is False

    tile = client.get("/tiles/osm/10/548/367.png")
    assert tile.status_code == 200
    assert tile["Content-Type"].startswith("image/png")
    data = tile.content
    assert data[:4] == b"\x89PNG"

    missing = client.get("/tiles/osm/10/548/999.png")
    assert missing.status_code == 404


def test_tiles_status_poisoned(client: Client, tmp_path: Path, settings):
    """Enake 6987 B PNG-je (OSM 403) označimo kot nedosegljive."""
    osm = tmp_path / "osm"
    payload = b"\x89PNG\r\n\x1a\n" + b"X" * 6979  # len 6987
    assert len(payload) == 6987
    for i in range(5):
        d = osm / "10" / str(500 + i)
        d.mkdir(parents=True)
        (d / "360.png").write_bytes(payload)
    (osm / ".ready").write_text(
        json.dumps({"provider": "osm", "min_zoom": 7, "max_zoom": 14}),
        encoding="utf-8",
    )
    settings.MAP_TILES_ROOT = tmp_path
    st = client.get("/tiles/status/")
    assert st.status_code == 200
    body = st.json()
    assert body["available"] is False
    assert body.get("poisoned") is True


def test_tiles_status_partial_without_ready(client: Client, tmp_path: Path, settings):
    """Med prenosom (.ready še ni) so ploščice že uporabne."""
    osm = tmp_path / "osm"
    (osm / "10" / "548").mkdir(parents=True)
    (osm / "10" / "548" / "367.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"A" * 40)
    (osm / "10" / "549").mkdir(parents=True)
    (osm / "10" / "549" / "367.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"B" * 40)
    settings.MAP_TILES_ROOT = tmp_path
    st = client.get("/tiles/status/")
    assert st.status_code == 200
    body = st.json()
    assert body["available"] is True
    assert body.get("partial") is True


def test_tiles_fetch_on_miss(client: Client, tmp_path: Path, settings, monkeypatch):
    """Manjkajoča ploščica → 302 na OSM + cache v ozadju (ne blokira runserver)."""
    settings.MAP_TILES_ROOT = tmp_path
    settings.MAP_TILES_FETCH_ON_MISS = True
    png = b"\x89PNG\r\n\x1a\n" + b"Z" * 900

    class _Resp:
        def read(self):
            return png

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        "core.tile_fetch.urllib.request.urlopen",
        lambda *a, **k: _Resp(),
    )

    r = client.get("/tiles/osm/15/100/200.png")
    assert r.status_code in (301, 302)
    assert "openstreetmap.de" in r["Location"]
    assert "15/100/200.png" in r["Location"]

    # Počakaj background cache.
    import time
    cached = tmp_path / "osm" / "15" / "100" / "200.png"
    for _ in range(50):
        if cached.is_file():
            break
        time.sleep(0.05)
    assert cached.is_file()
    assert cached.read_bytes() == png

    # Drugi request gre z diska.
    r2 = client.get("/tiles/osm/15/100/200.png")
    assert r2.status_code == 200
    assert r2.content == png
