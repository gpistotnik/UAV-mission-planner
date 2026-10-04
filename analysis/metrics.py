"""Metrike za validacijsko kampanjo --- ciste funkcije brez odvisnosti.

Modul namenoma ne uvaza ne ``numpy`` ne ``matplotlib`` ne ``pyproj``: vse
metrike so izracunane z osnovno matematiko, da so enotno testljive in da jih
je mogoce pozeniti tudi na Raspberry Pi-ju brez dodatnih paketov. Risanje
grafov je locena skrb (:mod:`analysis.analyze_flight`).

Pet metrik iz prijave teme:

======================================  ===================================
Metrika                                 Funkcija
======================================  ===================================
natancnost sledenja trajektoriji        :func:`cross_track_stats`
latenca komunikacije                    :func:`telemetry_rate_stats`
zanesljivost izvedbe misije             :func:`mission_completion`
tocnost zajema podatkov                 :func:`capture_accuracy`
odpornost na motnje                     :func:`altitude_stats` +
                                        :func:`cross_track_stats` (primerjava
                                        letov pri razlicnem vetru)
======================================  ===================================
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

# Polmer Zemlje (WGS84 srednji), uporabljen za lokalno ravninsko projekcijo.
EARTH_R = 6371008.8


# ---------------------------------------------------------------------------
# Branje zapisov
# ---------------------------------------------------------------------------
def load_jsonl(path: Path | str) -> list[dict[str, Any]]:
    """Prebere JSONL; pokvarjene vrstice preskoci (log je lahko odrezan)."""
    rows: list[dict[str, Any]] = []
    p = Path(path)
    if not p.is_file():
        return rows
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


@dataclass(frozen=True)
class TrackPoint:
    """Ena tocka dejanskega sledu."""
    t: float
    lat: float
    lon: float
    alt_rel_m: Optional[float] = None
    alt_msl_m: Optional[float] = None


def extract_track(rows: Iterable[dict[str, Any]]) -> list[TrackPoint]:
    """Izlusci sled iz ``GLOBAL_POSITION_INT`` sporocil."""
    out: list[TrackPoint] = []
    for r in rows:
        if r.get("type") != "GLOBAL_POSITION_INT":
            continue
        try:
            out.append(TrackPoint(
                t=float(r["t"]),
                lat=float(r["lat"]) / 1e7,
                lon=float(r["lon"]) / 1e7,
                alt_rel_m=(float(r["relative_alt"]) / 1000.0
                           if r.get("relative_alt") is not None else None),
                alt_msl_m=(float(r["alt"]) / 1000.0
                           if r.get("alt") is not None else None),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda p: p.t)
    return out


def armed_intervals(rows: Iterable[dict[str, Any]]) -> list[tuple[float, float]]:
    """Vrne intervale ``[t_arm, t_disarm]`` iz ``HEARTBEAT.base_mode``.

    Ce se log konca med armanjem (npr. izgubljena povezava), se zadnji
    interval zakljuci s casom zadnjega sporocila.
    """
    intervals: list[tuple[float, float]] = []
    armed_since: Optional[float] = None
    last_t = 0.0
    for r in rows:
        t = float(r.get("t", 0.0) or 0.0)
        last_t = max(last_t, t)
        if r.get("type") != "HEARTBEAT":
            continue
        base = int(r.get("base_mode", 0) or 0)
        armed = bool(base & 0b1000_0000)
        if armed and armed_since is None:
            armed_since = t
        elif not armed and armed_since is not None:
            intervals.append((armed_since, t))
            armed_since = None
    if armed_since is not None:
        intervals.append((armed_since, last_t))
    return intervals


def filter_airborne(
    track: Sequence[TrackPoint],
    intervals: Sequence[tuple[float, float]],
    min_alt_m: float = 1.0,
) -> list[TrackPoint]:
    """Obdrzi samo tocke med armanjem in nad ``min_alt_m``.

    Brez tega bi v metriko sledenja vstopale tudi tocke z tal (pred vzletom
    in po pristanku), kjer je odstopanje od nacrtovane poti veliko, a
    nesmiselno.
    """
    if not intervals:
        base = list(track)
    else:
        base = [p for p in track
                if any(a <= p.t <= b for a, b in intervals)]
    return [p for p in base
            if p.alt_rel_m is None or p.alt_rel_m >= min_alt_m]


def mission_window(
    rows: Iterable[dict[str, Any]],
) -> Optional[tuple[float, float]]:
    """Vrne ``(t_prvi, t_zadnji)`` dosezenega itema misije.

    To je okno, v katerem letalnik dejansko *sledi* nacrtovani poti. Vzlet in
    pristanek sta izven njega: med vzletom je dron navpicno nad vzletiscem in
    se ne premika po progi, med pristankom pa se namenoma oddaljuje od
    nacrtovane visine. Ce bi ju vsteli v metriko, bi bila RMS napaka
    sistematicno precenjena --- in to tem bolj, krajsi je let.

    Vrne ``None``, ce sta zabelezena manj kot dva dogodka
    ``MISSION_ITEM_REACHED`` (npr. misija ni bila izvedena do konca).
    """
    ts = sorted(float(r["t"]) for r in rows
                if r.get("type") == "MISSION_ITEM_REACHED"
                and r.get("t") is not None)
    if len(ts) < 2:
        return None
    return ts[0], ts[-1]


def filter_window(
    track: Sequence[TrackPoint], window: Optional[tuple[float, float]],
) -> list[TrackPoint]:
    """Obdrzi tocke znotraj casovnega okna; brez okna vrne vse."""
    if window is None:
        return list(track)
    a, b = window
    return [p for p in track if a <= p.t <= b]


# ---------------------------------------------------------------------------
# Lokalna ravninska projekcija
# ---------------------------------------------------------------------------
class LocalPlane:
    """Ekvirektangularna projekcija okoli izhodisca.

    Za obmocja velikosti nekaj sto metrov je napaka te projekcije pod
    milimetrom, kar je dva velikostna razreda manj od natancnosti GPS brez
    RTK. Prednost pred UTM je, da ne potrebuje ``pyproj``.
    """

    def __init__(self, lat0: float, lon0: float) -> None:
        self.lat0 = lat0
        self.lon0 = lon0
        self._kx = EARTH_R * math.cos(math.radians(lat0))
        self._ky = EARTH_R

    def to_xy(self, lat: float, lon: float) -> tuple[float, float]:
        x = math.radians(lon - self.lon0) * self._kx
        y = math.radians(lat - self.lat0) * self._ky
        return x, y

    def to_latlon(self, x: float, y: float) -> tuple[float, float]:
        lat = self.lat0 + math.degrees(y / self._ky)
        lon = self.lon0 + math.degrees(x / self._kx)
        return lat, lon


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Razdalja po velikem krogu v metrih."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(a)))


# ---------------------------------------------------------------------------
# Geometrija: tocka -> daljica
# ---------------------------------------------------------------------------
def point_segment_distance(
    px: float, py: float, ax: float, ay: float, bx: float, by: float,
) -> float:
    """Najkrajsa razdalja med tocko in **daljico** (ne premico) AB."""
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    if seg2 <= 1e-12:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / seg2
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


def cross_track_errors(
    track: Sequence[TrackPoint],
    planned: Sequence[tuple[float, float]],
) -> list[float]:
    """Za vsako tocko sledu vrne razdaljo do najblizje nacrtovane daljice.

    ``planned`` je zaporedje ``(lat, lon)`` nacrtovanih tock. Napaka je
    definirana kot razdalja do *poti* (poligonalne crte), ne do najblizje
    tocke --- dron sme biti med dvema waypointoma poljubno dalec od obeh,
    ce le ostaja na zveznici.
    """
    if len(planned) < 2 or not track:
        return []
    plane = LocalPlane(planned[0][0], planned[0][1])
    pxy = [plane.to_xy(lat, lon) for lat, lon in planned]
    out: list[float] = []
    for p in track:
        x, y = plane.to_xy(p.lat, p.lon)
        best = float("inf")
        for (ax, ay), (bx, by) in zip(pxy, pxy[1:]):
            d = point_segment_distance(x, y, ax, ay, bx, by)
            if d < best:
                best = d
        out.append(best)
    return out


# ---------------------------------------------------------------------------
# Statistika
# ---------------------------------------------------------------------------
def _percentile(values: Sequence[float], q: float) -> float:
    """Percentil z linearno interpolacijo (kot ``numpy.percentile``)."""
    if not values:
        return float("nan")
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def describe(values: Sequence[float]) -> dict[str, Any]:
    """Osnovna statistika niza: n, mean, rms, sd, max, p50, p95."""
    n = len(values)
    if n == 0:
        return {"n": 0}
    mean = sum(values) / n
    rms = math.sqrt(sum(v * v for v in values) / n)
    var = sum((v - mean) ** 2 for v in values) / n
    return {
        "n": n,
        "mean": round(mean, 3),
        "rms": round(rms, 3),
        "sd": round(math.sqrt(var), 3),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
        "p50": round(_percentile(values, 0.50), 3),
        "p95": round(_percentile(values, 0.95), 3),
    }


def cross_track_stats(
    track: Sequence[TrackPoint],
    planned: Sequence[tuple[float, float]],
) -> dict[str, Any]:
    """Natancnost sledenja trajektoriji [m]."""
    errs = cross_track_errors(track, planned)
    return {"unit": "m", **describe(errs)}


def altitude_stats(
    track: Sequence[TrackPoint], planned_alt_m: float,
) -> dict[str, Any]:
    """Odstopanje relativne visine od nacrtovane [m]."""
    errs = [abs(p.alt_rel_m - planned_alt_m)
            for p in track if p.alt_rel_m is not None]
    return {"unit": "m", "planned_alt_m": planned_alt_m, **describe(errs)}


def telemetry_rate_stats(rows: Sequence[dict[str, Any]],
                         msg_type: str = "GLOBAL_POSITION_INT") -> dict[str, Any]:
    """Latenca / doslednost telemetrije: razmiki med prihodi sporocil [ms].

    To je *ena* od dveh komponent metrike "latenca komunikacije": meri, kako
    enakomerno tece tok podatkov z letalnika na zemeljsko postajo. Druga
    komponenta je cas prenosa misije (``elapsed_ms`` iz ``meta.json``), ki ga
    izmeri sam most ob prenosu.
    """
    ts = sorted(float(r["t"]) for r in rows
                if r.get("type") == msg_type and r.get("t") is not None)
    gaps = [(b - a) * 1000.0 for a, b in zip(ts, ts[1:])]
    stats = describe(gaps)
    if ts and len(ts) > 1:
        stats["hz"] = round((len(ts) - 1) / (ts[-1] - ts[0]), 2) if ts[-1] > ts[0] else None
    stats["unit"] = "ms"
    stats["msg_type"] = msg_type
    return stats


def mission_completion(
    rows: Sequence[dict[str, Any]], item_count: Optional[int] = None,
) -> dict[str, Any]:
    """Zanesljivost izvedbe: kateri ukazi misije so bili doseženi."""
    reached = sorted({int(r["seq"]) for r in rows
                      if r.get("type") == "MISSION_ITEM_REACHED"
                      and r.get("seq") is not None})
    out: dict[str, Any] = {
        "reached_count": len(reached),
        "max_seq_reached": reached[-1] if reached else None,
        "item_count": item_count,
    }
    if item_count:
        # Zadnji item je zakljucni ukaz (LAND/RTL) --- dosezenih je lahko
        # najvec item_count - 1 navigacijskih.
        out["completion_pct"] = round(100.0 * len(reached) / max(1, item_count - 1), 1)
    errors = [r for r in rows if r.get("type") == "STATUSTEXT"
              and int(r.get("severity", 6) or 6) <= 3]
    out["error_messages"] = [r.get("text") for r in errors][:20]
    out["error_count"] = len(errors)
    return out


def capture_accuracy(
    captures: Sequence[dict[str, Any]],
    planned: Sequence[tuple[float, float]],
) -> dict[str, Any]:
    """Tocnost zajema: razdalja med nacrtovano in dejansko foto lokacijo [m].

    Za vsak posnetek poisce **najblizjo nacrtovano tocko** in zabelezi
    razdaljo. Poroca tudi statistiko zakasnitve med MAVLink dogodkom in
    ekspozicijo, ki je pri fotogrametriji glavni sistematicni vir napake.
    """
    if not captures:
        return {"n": 0}
    dists: list[float] = []
    unmatched = 0
    for c in captures:
        lat, lon = c.get("lat"), c.get("lon")
        if lat is None or lon is None:
            unmatched += 1
            continue
        if not planned:
            continue
        dists.append(min(haversine_m(lat, lon, plat, plon)
                         for plat, plon in planned))
    lat_ms = [float(c["trigger_to_capture_ms"]) for c in captures
              if c.get("trigger_to_capture_ms") is not None]
    return {
        "unit": "m",
        "captures": len(captures),
        "captured_files": sum(1 for c in captures if c.get("captured")),
        "without_position": unmatched,
        "distance_to_planned": describe(dists),
        "trigger_to_capture_ms": describe(lat_ms),
    }


def planned_points_from_plan(plan: dict[str, Any]) -> list[tuple[float, float]]:
    """Izlusci nacrtovane ``(lat, lon)`` iz ``plan.json``.

    Podpira dve obliki: seznam ``points`` (linearizirana pot) in seznam
    ``items`` (MAVLink ukazi) --- v drugem primeru upostevamo samo
    navigacijske ukaze z veljavno koordinato.
    """
    pts = plan.get("points")
    if isinstance(pts, list) and pts:
        return [(float(p["lat"]), float(p["lon"])) for p in pts
                if p.get("lat") is not None and p.get("lon") is not None]
    items = plan.get("items")
    out: list[tuple[float, float]] = []
    if isinstance(items, list):
        for it in items:
            if it.get("command_name") not in ("NAV_WAYPOINT", "NAV_LAND"):
                continue
            lat, lon = it.get("lat"), it.get("lon")
            if lat and lon:  # 0.0 pomeni "ni koordinate" (npr. TAKEOFF)
                out.append((float(lat), float(lon)))
    # Prvi item je home; ce se ujema z drugim, ga izpustimo.
    return out[1:] if len(out) > 1 and out[0] == out[1] else out


def planned_length_m(planned: Sequence[tuple[float, float]]) -> float:
    """Dolzina nacrtovane poti [m]."""
    return sum(haversine_m(a[0], a[1], b[0], b[1])
               for a, b in zip(planned, planned[1:]))


def flown_length_m(track: Sequence[TrackPoint]) -> float:
    """Dolzina dejansko preletene poti [m]."""
    return sum(haversine_m(a.lat, a.lon, b.lat, b.lon)
               for a, b in zip(track, track[1:]))
