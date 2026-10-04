"""On-demand prenos OSM XYZ ploščic (samo manjkajoče, ob pogledu karte)."""
from __future__ import annotations

import hashlib
import logging
import threading
import urllib.error
import urllib.request
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "MissionPlanner/1.0 (UAV thesis on-demand tiles; "
    "contact: local-rpi; educational use)"
)
_BLOCKED_MD5 = {"c069a15b2cc2d6b6f527ad09eb93c61a"}
_locks_guard = threading.Lock()
_path_locks: dict[str, threading.Lock] = {}
_fetch_sema = threading.Semaphore(6)


def _path_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _locks_guard:
        lock = _path_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _path_locks[key] = lock
        return lock


def _is_blocked(data: bytes) -> bool:
    if len(data) < 50 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return True
    if hashlib.md5(data).hexdigest() in _BLOCKED_MD5:
        return True
    low = data.lower()
    return b"API KEY" in data or b"apikey" in low or b"carto.com" in low


def fetch_enabled() -> bool:
    return bool(getattr(settings, "MAP_TILES_FETCH_ON_MISS", True))


def fetch_max_zoom() -> int:
    return int(getattr(settings, "MAP_TILES_FETCH_MAX_Z", 20))


def fetch_url_template() -> str:
    return str(
        getattr(
            settings,
            "MAP_TILES_FETCH_URL",
            "https://tile.openstreetmap.de/{z}/{x}/{y}.png",
        )
    )


def remote_tile_url(z: int, x: int, y: int) -> str:
    return fetch_url_template().format(z=z, x=x, y=y)


def fetch_and_cache(dest: Path, z: int, x: int, y: int) -> bytes | None:
    """Prenesi ploščico z interneta, shrani na disk, vrni bytes (ali None)."""
    if not fetch_enabled():
        return None
    if z > fetch_max_zoom() or z < 0:
        return None

    lock = _path_lock(dest)
    with lock:
        if dest.is_file():
            try:
                existing = dest.read_bytes()
            except OSError:
                existing = b""
            if existing and not _is_blocked(existing):
                return existing

        if not _fetch_sema.acquire(blocking=False):
            # Preveč vzporednih prenosov — naj brskalnik uporabi 302.
            return None
        try:
            url = remote_tile_url(z, x, y)
            timeout = float(getattr(settings, "MAP_TILES_FETCH_TIMEOUT", 8.0))
            req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = resp.read()
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                logger.info("tile fetch failed z=%s x=%s y=%s: %s", z, x, y, exc)
                return None

            if _is_blocked(data) or len(data) < 800:
                logger.info(
                    "tile fetch rejected (blocked/small) z=%s x=%s y=%s", z, x, y,
                )
                return None

            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".part")
            try:
                tmp.write_bytes(data)
                tmp.replace(dest)
            except OSError as exc:
                logger.warning("tile cache write failed %s: %s", dest, exc)
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
            return data
        finally:
            _fetch_sema.release()


def cache_in_background(dest: Path, z: int, x: int, y: int) -> None:
    """Zaženi prenos v ozadju (ne blokira HTTP odgovora)."""
    if dest.is_file():
        return

    def _run() -> None:
        try:
            fetch_and_cache(dest, z, x, y)
        except Exception:
            logger.exception("background tile cache failed z=%s x=%s y=%s", z, x, y)

    threading.Thread(
        target=_run,
        name=f"tile-cache-{z}-{x}-{y}",
        daemon=True,
    ).start()
