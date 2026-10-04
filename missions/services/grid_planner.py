"""Generator misije »lawnmower« iz poligona.

Vhod: poligon (GeoJSON), parametri kamere (širina/višina senzorja, goriščna
razdalja, ločljivost slike), višina leta, vzdolžno in prečno prekrivanje
ter smer prog. Izhod: zaporedje waypointov, ki pokrijejo poligon, in
spremljajoče statistike (talna ločljivost, razdalja, interval proženja
kamere, ocenjeno število slik).

Algoritem:

1. Poligon se projicira v lokalno ravnino UTM (metri), tako da vse
   geometrijske operacije potekajo v evklidskem prostoru.
2. Projicirani poligon se zavrti okoli središča za ``-track_angle``,
   tako da želena smer prog (npr. 0° = sever-jug) postane v lokalnem
   sistemu vodoravna.
3. Določi se mejni pravokotnik (bbox) rotiranega poligona. Skozi bbox
   se s konstantnim razmikom ``line_spacing`` vlečejo vodoravne črte.
4. Vsaka črta se preseka z (rotiranim) poligonom. Preseki — segmenti —
   tvorijo posamezno »progo«; vsaka naslednja proga se obrne v
   nasprotno smer (boustrophedon / »volovska steza«).
5. Vsi nastali vogali se zavrtijo nazaj in projicirajo v WGS84.

Vse formule sledijo standardni geometriji fotogrametrične podloge:

- ``GSD [cm/px] = (sensor_width [mm] × altitude [m] × 100) /
                  (focal_length [mm] × image_width [px])``
- Tloris ene slike: ``footprint = (sensor / focal) × altitude``.
- Razmik med progami (prečno): ``footprint × (1 − side_overlap/100)``.
- Razdalja med posnetki (vzdolžno): ``footprint × (1 − front_overlap/100)``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

from pyproj import Transformer
from shapely.affinity import rotate as shp_rotate
from shapely.geometry import LineString, MultiLineString, Polygon, mapping, shape
from shapely.ops import transform


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CameraSpec:
    """Lastnosti kamere, potrebne za izračun GSD in tlorisa."""
    sensor_width_mm: float
    sensor_height_mm: float
    focal_length_mm: float
    image_width_px: int
    image_height_px: int


@dataclass(frozen=True)
class GridParams:
    """Parametri mapping mreže."""
    altitude_m: float
    front_overlap_pct: float
    side_overlap_pct: float
    track_angle_deg: float = 0.0
    # Konvencija: krajša dimenzija senzorja določa "across-track" prog.
    # Privzeto landscape kamera (širina > višina) usmerjena tako, da je
    # daljša stranica vzporedna smeri leta → ožji swath, več prog.
    # Možnosti: "longer" (privzeto, daljša stranica = across-track, manj prog,
    # širši swath) ali "shorter".
    across_track_dim: Literal["longer", "shorter"] = "longer"


@dataclass
class GridPlan:
    """Rezultat planiranja."""
    waypoints: list[tuple[float, float]] = field(default_factory=list)  # (lon, lat)
    gsd_cm_per_px: float = 0.0
    footprint_across_m: float = 0.0
    footprint_along_m: float = 0.0
    line_spacing_m: float = 0.0
    trigger_distance_m: float = 0.0
    total_distance_m: float = 0.0
    num_lines: int = 0
    estimated_images: int = 0


# ---------------------------------------------------------------------------
# GSD in tloris / GSD and footprint
# ---------------------------------------------------------------------------
def gsd_cm_per_px(cam: CameraSpec, altitude_m: float) -> float:
    """Talna ločljivost v cm/px po klasični formuli središčne projekcije."""
    return (cam.sensor_width_mm * altitude_m * 100.0) / (
        cam.focal_length_mm * cam.image_width_px
    )


def footprint_dimensions_m(cam: CameraSpec, altitude_m: float) -> tuple[float, float]:
    """Vrne (širina_tlorisa, višina_tlorisa) v metrih."""
    fw = cam.sensor_width_mm / cam.focal_length_mm * altitude_m
    fh = cam.sensor_height_mm / cam.focal_length_mm * altitude_m
    return fw, fh


# ---------------------------------------------------------------------------
# UTM projekcija / UTM projection
# ---------------------------------------------------------------------------
def _utm_epsg_for(lon: float, lat: float) -> int:
    zone = int((lon + 180) // 6) + 1
    return (32600 if lat >= 0 else 32700) + zone


def _make_transformers(lon: float, lat: float):
    epsg = _utm_epsg_for(lon, lat)
    fwd = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    inv = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    return fwd.transform, inv.transform


# ---------------------------------------------------------------------------
# Glavni planer / Main planner
# ---------------------------------------------------------------------------
def plan_grid_mission(
    polygon_geojson: dict,
    cam: CameraSpec,
    params: GridParams,
) -> GridPlan:
    """Vrni :class:`GridPlan` z waypointi v polju ``(lon, lat)``."""
    poly = shape(polygon_geojson)
    if not isinstance(poly, Polygon):
        raise ValueError("Pričakovan je en Polygon (ne MultiPolygon).")

    # 1) Projekcija v UTM glede na centroidno koordinato
    centroid = poly.centroid
    to_utm, to_wgs = _make_transformers(centroid.x, centroid.y)
    poly_m = transform(to_utm, poly)

    # 2) Tloris in razmiki — v metrih
    fw, fh = footprint_dimensions_m(cam, params.altitude_m)
    if params.across_track_dim == "longer":
        footprint_across = max(fw, fh)
        footprint_along = min(fw, fh)
    else:
        footprint_across = min(fw, fh)
        footprint_along = max(fw, fh)

    line_spacing = footprint_across * (1 - params.side_overlap_pct / 100.0)
    trigger_dist = footprint_along * (1 - params.front_overlap_pct / 100.0)
    if line_spacing <= 0 or trigger_dist <= 0:
        raise ValueError("Prekrivanje 100% ali več — razmik je 0 ali negativen.")

    # 3) Rotacija poligona za -track_angle okoli centroida
    poly_rot = shp_rotate(poly_m, -params.track_angle_deg, origin=poly_m.centroid)

    # 4) Vzporedne črte skozi bbox
    minx, miny, maxx, maxy = poly_rot.bounds
    # Prva črta na pol razmika v notranjost (da prva proga ni na robu).
    y_start = miny + line_spacing / 2
    y_end = maxy
    y = y_start
    lines: list[LineString] = []
    while y <= y_end + 1e-6:
        lines.append(LineString([(minx - 1, y), (maxx + 1, y)]))
        y += line_spacing

    # 5) Presek z poligonom; sestava segmentov
    segments_rot: list[list[tuple[float, float]]] = []
    for i, ln in enumerate(lines):
        inter = ln.intersection(poly_rot)
        if inter.is_empty:
            continue
        # Možno je več kosov, če je poligon konkaven; vzamemo vse, urejene.
        pieces: list[LineString] = []
        if isinstance(inter, LineString):
            pieces = [inter]
        elif isinstance(inter, MultiLineString):
            pieces = sorted(list(inter.geoms), key=lambda g: g.coords[0][0])
        else:
            continue
        # Vsak kos: dva vogala (začetek + konec).
        for piece in pieces:
            x_coords = sorted([c[0] for c in piece.coords])
            ax, bx = x_coords[0], x_coords[-1]
            # Boustrophedon: pari prog (0, 2, 4 …) gredo levo→desno, lihe desno→levo.
            if i % 2 == 0:
                segments_rot.append([(ax, y_for(piece)), (bx, y_for(piece))])
            else:
                segments_rot.append([(bx, y_for(piece)), (ax, y_for(piece))])

    # 6) Razvrstitev: po vrstnem redu prog
    # (zdaj že iz urejene zanke ‘for i, ln in enumerate(lines)’)

    # 7) Rotacija nazaj + projekcija v WGS84
    waypoints_lonlat: list[tuple[float, float]] = []
    total = 0.0
    prev_pt = None
    for seg in segments_rot:
        for x, y_ in seg:
            # Rotacija nazaj okoli istega centra
            xr, yr = _rotate_point(x, y_, poly_m.centroid.x, poly_m.centroid.y,
                                   params.track_angle_deg)
            lon, lat = to_wgs(xr, yr)
            waypoints_lonlat.append((lon, lat))
            if prev_pt is not None:
                total += math.hypot(xr - prev_pt[0], yr - prev_pt[1])
            prev_pt = (xr, yr)

    return GridPlan(
        waypoints=waypoints_lonlat,
        gsd_cm_per_px=gsd_cm_per_px(cam, params.altitude_m),
        footprint_across_m=footprint_across,
        footprint_along_m=footprint_along,
        line_spacing_m=line_spacing,
        trigger_distance_m=trigger_dist,
        total_distance_m=total,
        num_lines=len(segments_rot),
        estimated_images=int(math.ceil(total / trigger_dist)) if trigger_dist > 0 else 0,
    )


def y_for(piece: LineString) -> float:
    """Vrne y koordinato vodoravne črte (vsi vogali imajo enak y)."""
    return piece.coords[0][1]


def _rotate_point(x: float, y: float, cx: float, cy: float, angle_deg: float) -> tuple[float, float]:
    """Rotacija točke okrog (cx, cy) za ``angle_deg``."""
    rad = math.radians(angle_deg)
    dx, dy = x - cx, y - cy
    return (cx + dx * math.cos(rad) - dy * math.sin(rad),
            cy + dx * math.sin(rad) + dy * math.cos(rad))
