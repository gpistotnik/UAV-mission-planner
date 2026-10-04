"""Lociranje in preverjanje USB shrambe za podatke letov.

Modul nima odvisnosti od Djanga, zato ga uporabljata tako spletna aplikacija
kot samostojni proces kamere. Podatkovne baze in kratkotrajnega sistemskega
stanja namenoma ne upravlja.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


APP_DIRECTORY = "mission-planner"
ACTIVE_SESSION_FILE = ".active-session"
PREFERRED_MOUNT_POINT = Path("/mnt/uav-data")
_MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")


class StorageUnavailableError(RuntimeError):
    """USB shramba manjka, je dvoumna ali ni zapisljiva."""


@dataclass(frozen=True)
class MountedFilesystem:
    source: str
    mount_point: Path
    options: frozenset[str]


def _unescape_mount_field(value: str) -> str:
    return _MOUNT_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), value)


def parse_mountinfo(text: str) -> list[MountedFilesystem]:
    """Pretvori Linux ``mountinfo`` v zapise, potrebne za USB zaznavanje."""
    mounts: list[MountedFilesystem] = []
    for line in text.splitlines():
        fields = line.split()
        try:
            separator = fields.index("-")
            mount_point = Path(_unescape_mount_field(fields[4]))
            options = frozenset(fields[5].split(","))
            source = _unescape_mount_field(fields[separator + 2])
        except (ValueError, IndexError):
            continue
        if source.startswith("/dev/"):
            mounts.append(MountedFilesystem(source, mount_point, options))
    return mounts


def _root_block_device(device_name: str, sys_class_block: Path) -> str:
    current = sys_class_block / device_name
    if (current / "partition").exists():
        try:
            return current.resolve().parent.name
        except OSError:
            pass
    return device_name


def _is_usb_block_device(device_name: str, sys_class_block: Path) -> bool:
    root_name = _root_block_device(device_name, sys_class_block)
    root = sys_class_block / root_name
    try:
        if (root / "removable").read_text(encoding="ascii").strip() == "1":
            return True
    except OSError:
        pass
    try:
        parts = str((root / "device").resolve()).lower().split(os.sep)
        return any(part.startswith("usb") for part in parts)
    except OSError:
        return False


def find_usb_mounts(
    *,
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
    sys_class_block: Path = Path("/sys/class/block"),
) -> list[Path]:
    """Vrne zapisljive mount točke fizičnih USB blokovnih naprav."""
    try:
        mounts = parse_mountinfo(mountinfo_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise StorageUnavailableError(
            f"Ni mogoče prebrati Linux mountov: {exc}") from exc

    candidates: list[Path] = []
    for mount in mounts:
        device_name = Path(mount.source).name
        if "rw" not in mount.options:
            continue
        if _is_usb_block_device(device_name, sys_class_block):
            candidates.append(mount.mount_point)
    return sorted(set(candidates), key=str)


def ensure_writable(path: Path) -> Path:
    """Ustvari mapo in z dejanskim zapisom preveri njeno uporabnost."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=".write-test-",
            dir=path, delete=True,
        ) as probe:
            probe.write("ok")
            probe.flush()
    except OSError as exc:
        raise StorageUnavailableError(
            f"Shramba '{path}' ni zapisljiva: {exc}") from exc
    return path


def resolve_storage_root(
    configured_root: str | Path | None = None,
    *,
    mounts: Iterable[Path] | None = None,
) -> Path:
    """Razreši aplikacijski koren na ključku in zahteva en sam kandidat."""
    configured = str(configured_root or "").strip()
    if configured and configured.lower() != "auto":
        return ensure_writable(Path(configured).expanduser().resolve())

    if mounts is None and not sys.platform.startswith("linux"):
        raise StorageUnavailableError(
            "Samodejno zaznavanje USB shrambe je podprto samo na Linuxu.")

    detected = list(mounts) if mounts is not None else find_usb_mounts()
    candidates: list[Path] = []
    for candidate in detected:
        path = Path(candidate)
        if not path.is_dir():
            continue
        try:
            ensure_writable(path)
        except StorageUnavailableError:
            continue
        candidates.append(path)

    if not candidates:
        raise StorageUnavailableError(
            "Ni priklopljenega in zapisljivega USB ključka.")
    for candidate in candidates:
        if candidate.resolve() == PREFERRED_MOUNT_POINT:
            return ensure_writable(candidate / APP_DIRECTORY)
    if len(candidates) > 1:
        joined = ", ".join(str(path) for path in candidates)
        raise StorageUnavailableError(
            f"Zaznanih je več USB shramb ({joined}); nastavi UAV_STORAGE_ROOT.")
    return ensure_writable(Path(candidates[0]) / APP_DIRECTORY)


def flight_log_dir(configured_root: str | Path | None = None) -> Path:
    """Vrne korensko mapo vseh podatkov letov na USB ključku."""
    return ensure_writable(resolve_storage_root(configured_root) / "flightlogs")


def is_raspberry_pi(model_path: Path = Path("/proc/device-tree/model")) -> bool:
    try:
        return "raspberry pi" in model_path.read_text(
            encoding="utf-8", errors="ignore").lower()
    except OSError:
        return False


def publish_active_session(base_dir: Path, session_dir: Path) -> None:
    """Atomsko objavi ime trenutno aktivne telemetrijske seje."""
    base = base_dir.resolve()
    session = session_dir.resolve()
    if session.parent != base:
        raise ValueError("Aktivna seja mora biti neposredno pod UAV_LOG_DIR.")
    temporary = base / f"{ACTIVE_SESSION_FILE}.tmp"
    temporary.write_text(session.name, encoding="utf-8")
    temporary.replace(base / ACTIVE_SESSION_FILE)


def active_session_dir(base_dir: Path) -> Path | None:
    """Varno razreši trenutno aktivno sejo ali vrne ``None``."""
    base = base_dir.resolve()
    marker = base / ACTIVE_SESSION_FILE
    try:
        name = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not name or Path(name).name != name:
        return None
    session = base / name
    return session if session.is_dir() else None


def clear_active_session(base_dir: Path, session_dir: Path) -> None:
    """Odstrani marker, vendar samo če še kaže na podano sejo."""
    marker = base_dir.resolve() / ACTIVE_SESSION_FILE
    current = active_session_dir(base_dir)
    if current is None or current.resolve() != session_dir.resolve():
        return
    try:
        marker.unlink()
    except FileNotFoundError:
        pass


def main() -> int:
    """Systemd pre-check: 78 pomeni trajno konfiguracijsko napako."""
    try:
        path = flight_log_dir(os.environ.get("UAV_STORAGE_ROOT") or "auto")
    except StorageUnavailableError as exc:
        print(f"USB shramba ni na voljo: {exc}", file=sys.stderr)
        return 78
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
