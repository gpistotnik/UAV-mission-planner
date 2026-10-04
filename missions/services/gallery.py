"""Galerija slik in ročni zajem iz MJPEG streama.

Slike živijo na USB/log disku poleg sej telemetrije:

* aktivna seja → ``<seja>/images/``
* izbrana misija (brez aktivne seje) → ``gallery/m{id}_{slug}/images/``
* sicer → ``unassigned-captures/<YYYYMMDD>/images/``

Skupina »po misijah« bere ``mission_id`` / ``mission_name`` iz ``meta.json``.
"""
from __future__ import annotations

import io
import json
import re
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence
from urllib.parse import urlparse, urlunparse

from core.storage import active_session_dir, ensure_writable

_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")
_IMG_NAME_RE = re.compile(r"^[\w.-]+\.jpe?g$", re.IGNORECASE)
_SESSION_NAME_RE = re.compile(r"^[\w.-]+$")
_STAMP_RE = re.compile(r"(\d{8}-\d{6})")
# Placeholder / testni JPEG-i (1×1 px, thumbnail) niso terenski posnetki.
MIN_GALLERY_IMAGE_BYTES = 20_000
_INFER_WINDOW_S = 180.0


def _slug(text: str) -> str:
    cleaned = _SLUG_RE.sub("-", (text or "").strip())[:48].strip("-")
    return cleaned or "brez-imena"


def snapshot_url(stream_url: str, *, local_host: str = "127.0.0.1") -> str:
    """Iz MJPEG URL-ja sestavi lokalni ``/snapshot.jpg`` za strežniški fetch."""
    raw = (stream_url or "").strip()
    if not raw or raw == "auto":
        return f"http://{local_host}:8090/snapshot.jpg"
    parsed = urlparse(raw)
    host = local_host
    port = parsed.port or 8090
    return urlunparse(("http", f"{host}:{port}", "/snapshot.jpg", "", "", ""))


def fetch_snapshot(url: str, *, timeout_s: float = 3.0) -> bytes:
    """Prenese en JPEG okvir. Vrže ``RuntimeError`` ob napaki."""
    req = urllib.request.Request(
        url,
        headers={"Accept": "image/jpeg", "User-Agent": "mission-planner/gallery"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = resp.read()
            ctype = (resp.headers.get("Content-Type") or "").lower()
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Kamera ni dosegljiva: {exc}") from exc
    if not data or data[:2] != b"\xff\xd8":
        raise RuntimeError("Odgovor kamere ni veljaven JPEG.")
    if ctype and "jpeg" not in ctype and "jpg" not in ctype:
        # Nekateri strežniki ne nastavijo Content-Type — dovolimo glede na magic.
        pass
    return data


def _read_meta(session_dir: Path) -> dict[str, Any]:
    mf = session_dir / "meta.json"
    if not mf.is_file():
        return {}
    try:
        data = json.loads(mf.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_meta(session_dir: Path, meta: dict[str, Any]) -> None:
    (session_dir / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def resolve_capture_dir(
    log_dir: Path,
    *,
    mission_id: Optional[int] = None,
    mission_name: Optional[str] = None,
    when: Optional[float] = None,
) -> Path:
    """Izbere mapo seje, kamor gre nova slika."""
    base = ensure_writable(log_dir)
    active = active_session_dir(base)
    if active is not None:
        return ensure_writable(active)

    t = when if when is not None else time.time()
    if mission_id is not None:
        label = _slug(mission_name or f"misija-{mission_id}")
        d = base / "gallery" / f"m{int(mission_id)}_{label}"
        ensure_writable(d)
        meta = _read_meta(d)
        if not meta:
            meta = {
                "mission_id": int(mission_id),
                "mission_name": mission_name,
                "reason": "gallery",
                "started_at": datetime.fromtimestamp(t, timezone.utc).isoformat(),
            }
            _write_meta(d, meta)
        elif meta.get("mission_id") is None:
            meta["mission_id"] = int(mission_id)
            meta["mission_name"] = mission_name
            _write_meta(d, meta)
        return d

    day = datetime.fromtimestamp(t, timezone.utc).strftime("%Y%m%d")
    return ensure_writable(base / "unassigned-captures" / day)


def save_capture(
    jpeg: bytes,
    log_dir: Path,
    *,
    mission_id: Optional[int] = None,
    mission_name: Optional[str] = None,
    trigger: str = "DASHBOARD_SHUTTER",
    gps: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Shrani JPEG in doda vrstico v ``captures.jsonl``. Vrne opis datoteke."""
    now = time.time()
    session = resolve_capture_dir(
        log_dir, mission_id=mission_id, mission_name=mission_name, when=now)
    image_dir = ensure_writable(session / "images")
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%d-%H%M%S")
    # Unikaten indeks glede na obstoječe datoteke.
    existing = sorted(image_dir.glob("img_*.jpg")) + sorted(image_dir.glob("img_*.jpeg"))
    idx = len(existing) + 1
    name = f"img_{idx:05d}_{stamp}.jpg"
    path = image_dir / name
    path.write_bytes(jpeg)

    rel = f"images/{name}"
    row: dict[str, Any] = {
        "index": idx,
        "trigger": trigger,
        "t_event": round(now, 3),
        "t_capture": round(now, 3),
        "file": rel,
        "captured": True,
        "mission_id": mission_id,
        "mission_name": mission_name,
    }
    if gps:
        row.update(gps)
    log_path = session / "captures.jsonl"
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    sk = _session_key(log_dir, session)
    try:
        rel_from_base = path.relative_to(log_dir.resolve()).as_posix()
    except ValueError:
        rel_from_base = f"{sk}/{rel}"

    return {
        "ok": True,
        "session": sk,
        "file": name,
        "rel": rel_from_base,
        "size_b": path.stat().st_size,
        "created_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "mission_id": mission_id,
        "mission_name": mission_name,
        "url": f"/api/gallery/image/{sk}/{name}",
    }


def _session_key(log_dir: Path, session: Path) -> str:
    """Ključ seje za URL (lahko vključuje ``gallery/...`` ali ``unassigned-captures/...``)."""
    base = log_dir.resolve()
    try:
        return session.resolve().relative_to(base).as_posix()
    except ValueError:
        return session.name


def resolve_image(log_dir: Path, session_key: str, filename: str) -> Path:
    """Varno razreši pot do JPEG-a pod ``log_dir``."""
    if not _IMG_NAME_RE.match(filename or ""):
        raise FileNotFoundError("Neveljavno ime slike.")
    parts = [p for p in (session_key or "").replace("\\", "/").split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        raise FileNotFoundError("Neveljavna seja.")
    if not all(_SESSION_NAME_RE.match(p) for p in parts):
        raise FileNotFoundError("Neveljavna seja.")
    base = log_dir.resolve()
    target = (base.joinpath(*parts) / "images" / filename).resolve()
    if base not in target.parents or not target.is_file():
        raise FileNotFoundError("Slika ne obstaja.")
    return target


def _parse_stamp(text: str) -> Optional[datetime]:
    m = _STAMP_RE.search(text or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d-%H%M%S")
    except ValueError:
        return None


def _is_real_photo(path: Path, min_bytes: int = MIN_GALLERY_IMAGE_BYTES) -> bool:
    """Galerija kaže samo dejanske posnetke, ne placeholderjev iz sej."""
    try:
        return path.stat().st_size >= min_bytes
    except OSError:
        return False


def _flight_catalog(log_dir: Path) -> list[tuple[datetime, int, str]]:
    """Seje z ``mission_id`` — za pripenjanje nedeležnih posnetkov k misiji."""
    base = Path(log_dir)
    if not base.is_dir():
        return []
    out: list[tuple[datetime, int, str]] = []
    for child in base.iterdir():
        if not child.is_dir() or child.name in ("gallery", "unassigned-captures"):
            continue
        meta = _read_meta(child)
        mid = meta.get("mission_id")
        if mid is None:
            continue
        ts = _parse_stamp(child.name)
        if ts is None:
            continue
        name = str(meta.get("mission_name") or child.name)
        out.append((ts, int(mid), name))
    out.sort(key=lambda row: row[0])
    return out


def _infer_mission(
    when: Optional[datetime],
    catalog: Sequence[tuple[datetime, int, str]],
    window_s: float = _INFER_WINDOW_S,
) -> Optional[tuple[int, str]]:
    if when is None or not catalog:
        return None
    best: Optional[tuple[float, int, str]] = None
    for ts, mid, name in catalog:
        dt = abs((when - ts).total_seconds())
        if dt > window_s:
            continue
        if best is None or dt < best[0]:
            best = (dt, mid, name)
    return None if best is None else (best[1], best[2])


def _iter_image_dirs(log_dir: Path) -> Iterable[Path]:
    base = Path(log_dir)
    if not base.is_dir():
        return
    # Neposredne seje + gallery/* + unassigned-captures/*
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        if child.name in ("gallery", "unassigned-captures"):
            for nested in sorted(child.iterdir()):
                if nested.is_dir():
                    yield nested
            continue
        yield child


def list_gallery(log_dir: Path) -> dict[str, Any]:
    """Združi slike po misijah; ostale vrne kronološko (najnovejše najprej)."""
    missions: dict[str, dict[str, Any]] = {}
    other: list[dict[str, Any]] = []
    catalog = _flight_catalog(log_dir)

    for session in _iter_image_dirs(log_dir):
        images_dir = session / "images"
        if not images_dir.is_dir():
            continue
        meta = _read_meta(session)
        mid = meta.get("mission_id")
        mname = meta.get("mission_name")
        # Fallback iz imena gallery/m12_slug
        if mid is None and session.parent.name == "gallery":
            m = re.match(r"^m(\d+)_", session.name)
            if m:
                mid = int(m.group(1))
                if not mname:
                    mname = session.name.split("_", 1)[-1]

        sk = _session_key(log_dir, session)
        for path in sorted(images_dir.iterdir()):
            if not path.is_file() or path.suffix.lower() not in (".jpg", ".jpeg"):
                continue
            if not _is_real_photo(path):
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            created = datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()
            item_mid, item_name = mid, mname
            if item_mid is None:
                inferred = _infer_mission(_parse_stamp(path.name), catalog)
                if inferred is not None:
                    item_mid, item_name = inferred
            item = {
                "session": sk,
                "file": path.name,
                "rel": f"{sk}/images/{path.name}",
                "size_b": st.st_size,
                "created_at": created,
                "created_ts": st.st_mtime,
                "url": f"/api/gallery/image/{sk}/{path.name}",
                "mission_id": item_mid,
                "mission_name": item_name,
            }
            if item_mid is not None:
                key = str(int(item_mid))
                group = missions.setdefault(key, {
                    "mission_id": int(item_mid),
                    "mission_name": item_name or f"Misija #{item_mid}",
                    "images": [],
                    "count": 0,
                    "total_b": 0,
                })
                if item_name and (
                    not group.get("mission_name")
                    or str(group["mission_name"]).startswith("Misija #")
                ):
                    group["mission_name"] = item_name
                group["images"].append(item)
                group["count"] += 1
                group["total_b"] += st.st_size
            else:
                other.append(item)

    mission_list = sorted(
        missions.values(),
        key=lambda g: (
            -max((i["created_ts"] for i in g["images"]), default=0),
            g["mission_name"] or "",
        ),
    )
    for g in mission_list:
        g["images"].sort(key=lambda i: -i["created_ts"])
        for i in g["images"]:
            i.pop("created_ts", None)
    other.sort(key=lambda i: -i["created_ts"])
    for i in other:
        i.pop("created_ts", None)

    return {
        "missions": mission_list,
        "other": other,
        "mission_count": len(mission_list),
        "other_count": len(other),
        "total_count": sum(g["count"] for g in mission_list) + len(other),
    }


def collect_keys(
    log_dir: Path,
    *,
    keys: Optional[Iterable[str]] = None,
    mission_id: Optional[int] = None,
    scope: Optional[str] = None,
) -> list[str]:
    """Razširi izbor (ključi / misija / other) v seznam ``session/images/file``."""
    gallery = list_gallery(log_dir)
    if keys is not None:
        out: list[str] = []
        for key in keys:
            k = str(key).replace("\\", "/").strip().lstrip("/")
            if not k or ".." in k.split("/"):
                continue
            out.append(k)
        return out
    if mission_id is not None:
        for g in gallery["missions"]:
            if g["mission_id"] == int(mission_id):
                return [i["rel"] for i in g["images"]]
        return []
    if scope == "other":
        return [i["rel"] for i in gallery["other"]]
    if scope == "all":
        out = []
        for g in gallery["missions"]:
            out.extend(i["rel"] for i in g["images"])
        out.extend(i["rel"] for i in gallery["other"])
        return out
    return []


def build_zip(log_dir: Path, keys: Iterable[str]) -> io.BytesIO:
    """Sestavi ZIP iz relativnih ključev ``…/images/file.jpg``."""
    buf = io.BytesIO()
    base = log_dir.resolve()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for key in keys:
            parts = [p for p in key.replace("\\", "/").split("/") if p]
            if len(parts) < 2 or parts[-2] != "images":
                continue
            filename = parts[-1]
            session_key = "/".join(parts[:-2])
            try:
                path = resolve_image(base, session_key, filename)
            except FileNotFoundError:
                continue
            arcname = f"{session_key}/{filename}"
            zf.write(path, arcname=arcname)
    buf.seek(0)
    return buf
