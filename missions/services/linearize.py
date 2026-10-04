"""Linearizacija misije v zaporedje letalnih tock.

Misija je v podatkovnem modelu **urejeno zaporedje gradnikov**
(:class:`missions.models.MissionElement`): waypoint (``WP``) ali mapping
zona (``MAP``). Krmilnik letenja pa zna izvesti samo *plosko* zaporedje
tock. Ta modul je edino mesto, kjer se ta pretvorba zgodi.

Modul je zavestno neodvisen od katerega koli izhodnega formata --- vrne
seznam :class:`FlightPoint` objektov, ki jih nato porabnik (MAVLink
gradnik misije, analitika, test let) pretvori v svojo predstavitev.
Zaradi tega je tudi trivialno enotno testljiv brez MAVLink odvisnosti.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from .grid_planner import CameraSpec, GridParams, plan_grid_mission

if TYPE_CHECKING:  # pragma: no cover
    from ..models import Mission


@dataclass
class FlightPoint:
    """Ena tocka poti, ki jo bo dron dejansko preletel.

    ``source`` loci, ali tocka izhaja iz rocno postavljenega waypointa
    (``"WP"``) ali je bila generirana iz mapping zone (``"MAP"``). Pri
    ``MAP`` je izpolnjen tudi ``map_block`` (ID gradnika, ki jo je
    generiral) in ``trigger_distance_m`` (razdalja med posnetki, ki jo
    izracuna generator mreze) --- oboje potrebuje gradnik MAVLink misije,
    da namesto tisocih posameznih ukazov za slikanje uporabi enega samega
    ``DO_SET_CAM_TRIGG_DIST`` na zacetku bloka.
    """
    order: int
    lat: float
    lon: float
    altitude_m: float
    speed_ms: Optional[float] = None
    heading_mode: str = "AUTO"
    heading_deg: Optional[float] = None
    gimbal_pitch_deg: float = -90.0
    hover_time_s: float = 0.0
    action_type: str = "NONE"
    source: str = "WP"
    element_id: Optional[int] = None
    map_block: Optional[int] = None
    trigger_distance_m: Optional[float] = None


def camera_spec_for(drone) -> CameraSpec:
    """Zgradi :class:`CameraSpec` iz profila drona."""
    return CameraSpec(
        sensor_width_mm=float(drone.sensor_width_mm),
        sensor_height_mm=float(drone.sensor_height_mm),
        focal_length_mm=float(drone.focal_length_mm),
        image_width_px=int(drone.image_width_px),
        image_height_px=int(drone.image_height_px),
    )


def linearize_mission(mission: "Mission") -> list[FlightPoint]:
    """Pretvori gradnike misije v ravno zaporedje :class:`FlightPoint`.

    Waypointi se prenesejo neposredno. Za vsako mapping zono se pozene
    generator mreze; pri vzorcu ``CROSSHATCH`` dvakrat, drugic pod kotom
    +90 stopinj. Zone brez poligona ali z neveljavnimi parametri se
    preskocijo (generator vrze ``ValueError``).
    """
    # Uvoz tu, da modul ostane uvozljiv brez naloženega Django app registra.
    from ..models import MapPattern

    out: list[FlightPoint] = []
    order = 1
    cam = camera_spec_for(mission.drone)

    for e in mission.elements.all().order_by("order"):
        if e.is_waypoint:
            if e.lat is None or e.lon is None:
                continue
            out.append(FlightPoint(
                order=order, lat=float(e.lat), lon=float(e.lon),
                altitude_m=float(e.altitude_m),
                speed_ms=float(e.speed_ms) if e.speed_ms is not None else None,
                heading_mode=e.heading_mode or "AUTO",
                heading_deg=float(e.heading_deg) if e.heading_deg is not None else None,
                gimbal_pitch_deg=float(e.gimbal_pitch_deg),
                hover_time_s=float(e.hover_time_s),
                action_type=e.action_type or "NONE",
                source="WP",
                element_id=e.pk,
            ))
            order += 1
            continue

        if not e.polygon_geojson:
            continue

        angles: list[float] = [float(e.track_angle_deg)]
        if e.pattern == MapPattern.CROSSHATCH:
            angles.append(float(e.track_angle_deg) + 90.0)

        for ang in angles:
            params = GridParams(
                altitude_m=float(e.altitude_m),
                front_overlap_pct=float(e.front_overlap_pct),
                side_overlap_pct=float(e.side_overlap_pct),
                track_angle_deg=ang,
            )
            try:
                plan = plan_grid_mission(e.polygon_geojson, cam, params)
            except ValueError:
                continue
            for lon, lat in plan.waypoints:
                out.append(FlightPoint(
                    order=order, lat=lat, lon=lon,
                    altitude_m=float(e.altitude_m),
                    speed_ms=float(e.speed_ms) if e.speed_ms is not None else None,
                    heading_mode="AUTO",
                    gimbal_pitch_deg=-90.0,
                    hover_time_s=0.0,
                    action_type="PHOTO",
                    source="MAP",
                    element_id=e.pk,
                    map_block=e.pk,
                    trigger_distance_m=plan.trigger_distance_m or None,
                ))
                order += 1
    return out
