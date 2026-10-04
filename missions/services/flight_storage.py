"""Dinamičen dostop do shrambe podatkov letov."""
from __future__ import annotations

from pathlib import Path

from core.storage import (
    StorageUnavailableError, ensure_writable, flight_log_dir,
)


def resolve_log_dir() -> Path:
    """Vrne trenutno dostopno mapo letov ali vrže opisno izjemo."""
    from django.conf import settings

    storage_root = str(getattr(settings, "UAV_STORAGE_ROOT", "") or "").strip()
    if storage_root:
        return flight_log_dir(storage_root)
    return ensure_writable(Path(settings.UAV_LOG_DIR))


def storage_status() -> dict[str, object]:
    try:
        path = resolve_log_dir()
    except StorageUnavailableError as exc:
        return {
            "available": False,
            "error": f"USB medij ni na voljo: {exc}",
        }
    return {"available": True, "path": str(path), "error": None}
