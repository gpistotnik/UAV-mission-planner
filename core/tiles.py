"""Serviranje lokalnih OSM XYZ ploščic z RPi diska (+ on-demand fetch)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from django.conf import settings
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse

from core.tile_fetch import (
    cache_in_background,
    fetch_enabled,
    fetch_max_zoom,
    remote_tile_url,
)

# OSM.org „403 Access blocked“ PNG (enaka datoteka za vse koordinate).
_BLOCKED_MD5 = {"c069a15b2cc2d6b6f527ad09eb93c61a"}


def _osm_root() -> Path:
    return Path(settings.MAP_TILES_ROOT) / "osm"


def _md5_file(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _cache_looks_poisoned(root: Path, *, samples: int = 8) -> bool:
    """True, če vzorčne ploščice kažejo na OSM 403 / enake fake PNG-je."""
    try:
        pngs = list(root.rglob("*.png"))
    except OSError:
        return True
    if not pngs:
        return True
    # Hitra pot: če je veliko datotek enake velikosti 6987 B (znani 403).
    sizes: list[int] = []
    for p in pngs[:200]:
        try:
            sizes.append(p.stat().st_size)
        except OSError:
            continue
    if sizes and sizes.count(6987) >= max(3, len(sizes) // 2):
        return True
    digests: list[str] = []
    step = max(1, len(pngs) // samples)
    for path in pngs[::step][:samples]:
        try:
            digests.append(_md5_file(path))
        except OSError:
            continue
    if not digests:
        return True
    if any(d in _BLOCKED_MD5 for d in digests):
        return True
    # Vse vzorčne enake → sum na blokirano paleto.
    return len(set(digests)) == 1 and len(digests) >= 3


def _count_pngs(root: Path, *, limit: int = 50) -> int:
    n = 0
    try:
        for _ in root.rglob("*.png"):
            n += 1
            if n >= limit:
                break
    except OSError:
        return 0
    return n


def _is_blocked_bytes(data: bytes) -> bool:
    if hashlib.md5(data).hexdigest() in _BLOCKED_MD5:
        return True
    low = data.lower()
    return b"API KEY" in data or b"apikey" in low or b"carto.com" in low


def tiles_status_api(_request: HttpRequest) -> JsonResponse:
    """Ali so lokalne ploščice Slovenije na disku (in niso 403 smeti)."""
    root = _osm_root()
    ready = root / ".ready"
    meta: dict = {}
    if ready.is_file():
        try:
            meta = json.loads(ready.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}

    has_tiles = _count_pngs(root) > 0
    poisoned = _cache_looks_poisoned(root) if has_tiles else False
    if poisoned:
        return JsonResponse({
            "available": False,
            "poisoned": True,
            "provider": meta.get("provider", "osm"),
            "error": "Lokalne ploščice so neveljavne (OSM 403). "
                     "Zaženi: python scripts/download_slovenia_tiles.py "
                     "--purge-blocked",
            "min_zoom": meta.get("min_zoom"),
            "max_zoom": meta.get("max_zoom"),
            "fetch_on_miss": fetch_enabled(),
            "fetch_max_zoom": fetch_max_zoom(),
        })

    # Med prenosom še ni .ready, ampak del ploščic že obstaja.
    if not ready.is_file():
        if has_tiles:
            return JsonResponse({
                "available": True,
                "partial": True,
                "poisoned": False,
                "provider": "osm",
                "min_zoom": 7,
                "max_zoom": 14,
                "fetch_on_miss": fetch_enabled(),
                "fetch_max_zoom": fetch_max_zoom(),
            })
        return JsonResponse({
            "available": False,
            "provider": "osm",
            "fetch_on_miss": fetch_enabled(),
            "fetch_max_zoom": fetch_max_zoom(),
        })

    return JsonResponse({
        "available": True,
        "partial": False,
        "poisoned": False,
        "provider": meta.get("provider", "osm"),
        "min_zoom": meta.get("min_zoom"),
        "max_zoom": meta.get("max_zoom"),
        "bounds": meta.get("bounds"),
        "updated_at": meta.get("updated_at"),
        "fetch_on_miss": fetch_enabled(),
        "fetch_max_zoom": fetch_max_zoom(),
    })


def osm_tile(request: HttpRequest, z: int, x: int, y: int) -> HttpResponse:
    """GET /tiles/osm/<z>/<x>/<y>.png — lokalno, sicer 302 + cache v ozadju.

    Django runserver je enoniten: sinhroni fetch bi zablokiral telemetrijo
    in pustil sive luknje. Zato ob miss takoj preusmerimo brskalnik na OSM.de
    (vzporedni prenosi), RPi pa v ozadju shrani ploščico za offline.
    """
    if not (0 <= z <= 22 and x >= 0 and y >= 0):
        raise Http404("neveljavne koordinate")
    max_xy = 1 << z
    if x >= max_xy or y >= max_xy:
        raise Http404("neveljavne koordinate")

    root = _osm_root().resolve()
    path = (root / str(z) / str(x) / f"{y}.png").resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise Http404("neveljavna pot") from exc

    if path.is_file():
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise Http404("ploščica ni berljiva") from exc
        if data and not _is_blocked_bytes(data):
            resp = HttpResponse(data, content_type="image/png")
            resp["Cache-Control"] = "public, max-age=86400"
            return resp

    if fetch_enabled() and z <= fetch_max_zoom():
        cache_in_background(path, z, x, y)
        return HttpResponseRedirect(remote_tile_url(z, x, y))

    raise Http404("ploščica ni na disku")
