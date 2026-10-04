#!/usr/bin/env python3
"""Prenesi XYZ ploščice za Slovenijo na disk (za RPi offline karto).

Ploščice se shranijo v ``data/tiles/osm/{z}/{x}/{y}.png`` in jih Django
servira na ``/tiles/osm/{z}/{x}/{y}.png``.

**Pozor:** uradni ``tile.openstreetmap.org`` pogosto vrne PNG z napisom
„403 Access blocked“ pri množičnem prenosu. Skripta to zazna in zavrne.
Privzeti vir je OSM France (ulična karta, brez API ključa).

Uporaba::

    python scripts/download_slovenia_tiles.py --dry-run
    python scripts/download_slovenia_tiles.py --purge-blocked
    python scripts/download_slovenia_tiles.py --max-zoom 12
    rsync -av data/tiles/ dron@dron.local:~/mission_planner/data/tiles/
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

# Meje usklajene s frontend/src/tile_preload.ts
SLOVENIA = dict(south=45.42, west=13.38, north=46.88, east=16.61)

USER_AGENT = (
    "MissionPlanner/1.0 (UAV thesis offline tiles; "
    "contact: local-rpi; educational use)"
)

# Privzeto OSM.de — ulična karta, brez API ključa (osm.org bulk → 403;
# tile.openstreetmap.fr brez poddomene → SSL napaka).
DEFAULT_URL = "https://tile.openstreetmap.de/{z}/{x}/{y}.png"
OSM_ORG_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"

# Znani „403 Access blocked“ PNG z OSM.org (enaka vsebina za vse z/x/y).
BLOCKED_MD5 = {
    "c069a15b2cc2d6b6f527ad09eb93c61a",
}

REPO = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO / "data" / "tiles" / "osm"


def lon2tile(lon: float, z: int) -> int:
    return int(math.floor((lon + 180.0) / 360.0 * (1 << z)))


def lat2tile(lat: float, z: int) -> int:
    rad = math.radians(lat)
    return int(
        math.floor(
            (1.0 - math.log(math.tan(rad) + 1.0 / math.cos(rad)) / math.pi)
            / 2.0
            * (1 << z)
        )
    )


def clamp_tile(n: int, z: int) -> int:
    return max(0, min((1 << z) - 1, n))


def iter_tiles(min_z: int, max_z: int):
    b = SLOVENIA
    for z in range(min_z, max_z + 1):
        x0 = clamp_tile(lon2tile(b["west"], z), z)
        x1 = clamp_tile(lon2tile(b["east"], z), z)
        y0 = clamp_tile(lat2tile(b["north"], z), z)
        y1 = clamp_tile(lat2tile(b["south"], z), z)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                yield z, x, y


def md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def is_blocked_png(data: bytes) -> bool:
    if len(data) < 50 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return True
    if b"API KEY" in data or b"apikey" in data.lower():
        return True
    return md5_bytes(data) in BLOCKED_MD5


def purge_blocked(out: Path) -> int:
    """Izbriši znane 403 / enake blokirane PNG-je. Vrne število zbrisanih."""
    removed = 0
    if not out.is_dir():
        return 0
    for path in out.rglob("*.png"):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if is_blocked_png(data):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    ready = out / ".ready"
    if ready.is_file() and removed:
        try:
            ready.unlink()
        except OSError:
            pass
    return removed


def download_one(
    out: Path,
    z: int,
    x: int,
    y: int,
    timeout: float,
    url_tmpl: str,
    retries: int = 3,
) -> str:
    """Vrni 'ok' | 'skip' | 'err'."""
    dest = out / str(z) / str(x) / f"{y}.png"
    if dest.is_file() and dest.stat().st_size > 0:
        try:
            existing = dest.read_bytes()
        except OSError:
            existing = b""
        if existing and not is_blocked_png(existing):
            return "skip"
        try:
            dest.unlink()
        except OSError:
            pass

    dest.parent.mkdir(parents=True, exist_ok=True)
    url = url_tmpl.format(z=z, x=x, y=y)
    tmp = dest.with_suffix(".part")

    for attempt in range(max(1, retries)):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            if is_blocked_png(data):
                raise OSError("blokiran / neveljaven PNG (403 tile)")
            # Realne karte so po navadi večje od 403 paletne slike (~7 KB).
            if len(data) < 800:
                raise OSError(f"sumljivo majhen PNG ({len(data)} B)")
            tmp.write_bytes(data)
            tmp.replace(dest)
            return "ok"
        except (urllib.error.URLError, TimeoutError, OSError):
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            if attempt + 1 < retries:
                time.sleep(0.8 * (attempt + 1))
    return "err"


def write_ready(
    out: Path,
    min_z: int,
    max_z: int,
    downloaded: int,
    skipped: int,
    *,
    provider: str,
    url: str,
) -> None:
    meta = {
        "provider": provider,
        "url_template": url,
        "bounds": SLOVENIA,
        "min_zoom": min_z,
        "max_zoom": max_z,
        "downloaded": downloaded,
        "skipped": skipped,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    (out / ".ready").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--min-zoom", type=int, default=7)
    p.add_argument("--max-zoom", type=int, default=14)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--timeout", type=float, default=45.0)
    p.add_argument("--retries", type=int, default=4)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true",
                   help="Ponovno prenesi tudi obstoječe (ne-blokirane)")
    p.add_argument("--missing-only", action="store_true")
    p.add_argument(
        "--purge-blocked",
        action="store_true",
        help="Izbriši znane 403 PNG-je in končaj (ali nadaljuj s prenosom)",
    )
    p.add_argument(
        "--url",
        default=DEFAULT_URL,
        help="URL predloga z {z}/{x}/{y} (privzeto Carto Voyager)",
    )
    p.add_argument(
        "--provider",
        default="osm-de",
        help="Ime ponudnika za .ready / attribution",
    )
    args = p.parse_args()

    if args.min_zoom < 0 or args.max_zoom > 19 or args.min_zoom > args.max_zoom:
        print("Neveljaven zoom razpon.", file=sys.stderr)
        return 2

    if args.purge_blocked:
        n = purge_blocked(args.out)
        print(f"Zbrisanih blokiranih ploščic: {n}")

    all_tiles = list(iter_tiles(args.min_zoom, args.max_zoom))
    print(f"Slovenija z{args.min_zoom}–z{args.max_zoom}: {len(all_tiles)} ploščic")
    print(f"Izhod: {args.out}")
    print(f"Vir: {args.url}")
    if args.dry_run:
        mb = len(all_tiles) * 20 / 1024
        print(f"Ocena velikosti: ~{mb:.0f}–{mb * 1.5:.0f} MB")
        return 0

    # Samo purge?
    if args.purge_blocked and args.missing_only is False and not args.force:
        # Če uporabnik poda samo --purge-blocked, vseeno nadaljuj s prenosom
        # manjkajočih (po brisanju so vse manjkajoče).
        pass

    args.out.mkdir(parents=True, exist_ok=True)
    if args.force:
        for z, x, y in all_tiles:
            f = args.out / str(z) / str(x) / f"{y}.png"
            if f.is_file():
                f.unlink()

    if args.missing_only or args.purge_blocked:
        tiles = [
            (z, x, y) for z, x, y in all_tiles
            if not (args.out / str(z) / str(x) / f"{y}.png").is_file()
        ]
        print(f"Manjkajočih: {len(tiles)}")
        if not tiles:
            write_ready(
                args.out, args.min_zoom, args.max_zoom, 0, len(all_tiles),
                provider=args.provider, url=args.url,
            )
            print("Nič za prenesti.")
            return 0
    else:
        tiles = all_tiles

    total = len(tiles)
    ok = skip = err = 0
    failed: list[tuple[int, int, int]] = []
    t0 = time.time()
    workers = max(1, min(args.workers, 4))

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {
            ex.submit(
                download_one, args.out, z, x, y, args.timeout,
                args.url, args.retries,
            ): (z, x, y)
            for z, x, y in tiles
        }
        done = 0
        for fut in as_completed(futs):
            zxy = futs[fut]
            status = fut.result()
            done += 1
            if status == "ok":
                ok += 1
            elif status == "skip":
                skip += 1
            else:
                err += 1
                failed.append(zxy)
            if done % 50 == 0 or done == total:
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed > 0 else 0
                eta = (total - done) / rate if rate > 0 else 0
                print(
                    f"  {done}/{total}  ok={ok} skip={skip} err={err}  "
                    f"{rate:.1f} tile/s  ETA {eta / 60:.0f} min",
                    flush=True,
                )

    if failed:
        fail_log = args.out / "failed.txt"
        fail_log.write_text(
            "\n".join(f"{z}/{x}/{y}" for z, x, y in sorted(failed)) + "\n",
            encoding="utf-8",
        )
        print(f"Seznam napak: {fail_log} ({len(failed)})")

    write_ready(
        args.out, args.min_zoom, args.max_zoom, ok, skip,
        provider=args.provider, url=args.url,
    )
    print(f"Končano: ok={ok} skip={skip} err={err} → {args.out / '.ready'}")
    if err:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
