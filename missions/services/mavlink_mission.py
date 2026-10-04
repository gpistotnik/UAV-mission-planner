"""Gradnik MAVLink misije --- cista pretvorba poti v ``MISSION_ITEM_INT``.

Ta modul **ne uvaza pymavlinka**. Vse potrebne konstante so definirane
lokalno (MAVLink common message set), ukazi pa se vrnejo kot seznam
:class:`MissionItem` dataclass objektov. Prenos po zici je locena skrb
(:mod:`missions.services.mavlink_bridge`).

Locitev je namerna in ima dva ucinka:

1. gradnik misije je enotno testljiv brez strojne opreme in brez
   pymavlinka --- test primerja zaporedje ukazov, ne bajtov na vodilu;
2. isto zaporedje se lahko izpise v citljivi obliki za dokumentacijo
   magistrske naloge (glej :func:`describe_items`).

Zgradba zaporedja, ki jo ArduCopter pricakuje:

    seq 0   NAV_WAYPOINT       (rezerviran za home; krmilnik ga prepise)
    seq 1   DO_CHANGE_SPEED    (privzeta potovalna hitrost misije)
    seq 2   NAV_TAKEOFF        (vzlet na visino prve tocke)
    seq 3.. NAV_WAYPOINT ...   (tocke poti; vmes DO_CHANGE_SPEED in
                                DO_SET_CAM_TRIGG_DIST po potrebi)
    seq N   NAV_LAND / NAV_RETURN_TO_LAUNCH  (glede na finish_action)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# ---------------------------------------------------------------------------
# MAVLink konstante (common.xml). Namenoma prepisane, da modul nima
# odvisnosti na pymavlink --- vrednosti so del stabilnega protokola.
# ---------------------------------------------------------------------------
MAV_FRAME_GLOBAL = 0
MAV_FRAME_MISSION = 2
MAV_FRAME_GLOBAL_RELATIVE_ALT_INT = 6

MAV_CMD_NAV_WAYPOINT = 16
MAV_CMD_NAV_RETURN_TO_LAUNCH = 20
MAV_CMD_NAV_LAND = 21
MAV_CMD_NAV_TAKEOFF = 22
MAV_CMD_DO_CHANGE_SPEED = 178
MAV_CMD_DO_SET_CAM_TRIGG_DIST = 206
MAV_CMD_IMAGE_START_CAPTURE = 2000

MAV_MISSION_TYPE_MISSION = 0

# Berljiva imena za diagnostiko in izpis v nalogi.
CMD_NAMES = {
    MAV_CMD_NAV_WAYPOINT: "NAV_WAYPOINT",
    MAV_CMD_NAV_RETURN_TO_LAUNCH: "NAV_RETURN_TO_LAUNCH",
    MAV_CMD_NAV_LAND: "NAV_LAND",
    MAV_CMD_NAV_TAKEOFF: "NAV_TAKEOFF",
    MAV_CMD_DO_CHANGE_SPEED: "DO_CHANGE_SPEED",
    MAV_CMD_DO_SET_CAM_TRIGG_DIST: "DO_SET_CAM_TRIGG_DIST",
    MAV_CMD_IMAGE_START_CAPTURE: "IMAGE_START_CAPTURE",
}

# Dejanja gradnika, ki pomenijo "na tej tocki slikaj".
PHOTO_ACTIONS = frozenset({"PHOTO", "CAPTURE_BURST"})

# Privzeta velikost burst zajema, ce je dejanje CAPTURE_BURST.
BURST_COUNT = 3

# Pixhawk 2.4.8 (FMUv2) ima omejen prostor za misijo. ArduCopter na tej
# plosci prijavi ~700 itemov; nad tem upload zavrne z NO_SPACE.
MAX_MISSION_ITEMS = 700


@dataclass
class MissionItem:
    """Ena vrstica misije, pripravljena za ``mission_item_int_send``."""
    seq: int
    frame: int
    command: int
    current: int = 0
    autocontinue: int = 1
    param1: float = 0.0
    param2: float = 0.0
    param3: float = 0.0
    param4: float = 0.0
    x: int = 0            # lat * 1e7 (int32)
    y: int = 0            # lon * 1e7 (int32)
    z: float = 0.0        # visina [m]
    mission_type: int = MAV_MISSION_TYPE_MISSION

    @property
    def command_name(self) -> str:
        return CMD_NAMES.get(self.command, f"CMD_{self.command}")

    @property
    def lat(self) -> float:
        return self.x / 1e7

    @property
    def lon(self) -> float:
        return self.y / 1e7

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq, "frame": self.frame,
            "command": self.command, "command_name": self.command_name,
            "current": self.current, "autocontinue": self.autocontinue,
            "param1": self.param1, "param2": self.param2,
            "param3": self.param3, "param4": self.param4,
            "lat": self.lat, "lon": self.lon, "z": self.z,
        }


@dataclass
class BuildResult:
    items: list[MissionItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.items)


def _e7(v: float) -> int:
    return int(round(v * 1e7))


def build_mission_items(
    points: Iterable[Any],
    *,
    home_lat: Optional[float] = None,
    home_lon: Optional[float] = None,
    takeoff_altitude_m: Optional[float] = None,
    default_speed_ms: Optional[float] = None,
    finish_action: str = "RTH",
    accept_radius_m: float = 3.0,
    camera_enabled: bool = True,
) -> BuildResult:
    """Zgradi zaporedje ``MISSION_ITEM_INT`` iz linearizirane poti.

    Args:
        points: zaporedje :class:`~missions.services.linearize.FlightPoint`.
        home_lat / home_lon: vzletisce; ce ni podano, se uporabi prva tocka.
        takeoff_altitude_m: visina vzleta; privzeto visina prve tocke.
        default_speed_ms: potovalna hitrost misije (``DO_CHANGE_SPEED``).
        finish_action: ``"RTH"``, ``"LAND"`` ali ``"HOVER"``.
        accept_radius_m: polmer sprejemljivosti tocke (param2).
        camera_enabled: ce ``False``, se ukazi za kamero ne generirajo
            (uporabno za bench teste brez naprave na krovu).

    Returns:
        :class:`BuildResult` z zaporedjem itemov in morebitnimi opozorili.
    """
    pts = list(points)
    res = BuildResult()
    if not pts:
        return res

    first = pts[0]
    h_lat = home_lat if home_lat is not None else float(first.lat)
    h_lon = home_lon if home_lon is not None else float(first.lon)
    takeoff_alt = (
        float(takeoff_altitude_m) if takeoff_altitude_m is not None
        else float(first.altitude_m)
    )
    if takeoff_alt <= 0:
        takeoff_alt = 10.0
        res.warnings.append(
            "Visina vzleta ni bila podana ali je <= 0; uporabljenih 10 m."
        )

    items: list[MissionItem] = []
    seq = 0

    def add(item: MissionItem) -> None:
        nonlocal seq
        item.seq = seq
        item.current = 1 if seq == 0 else 0
        items.append(item)
        seq += 1

    # --- seq 0: home ---------------------------------------------------
    # ArduPilot to vrstico obravnava posebej in jo prepise z dejansko
    # home pozicijo ob armanju. Poslati jo je vseeno treba.
    add(MissionItem(
        seq=0, frame=MAV_FRAME_GLOBAL, command=MAV_CMD_NAV_WAYPOINT,
        x=_e7(h_lat), y=_e7(h_lon), z=0.0,
    ))

    # --- privzeta hitrost ----------------------------------------------
    current_speed: Optional[float] = None
    if default_speed_ms and default_speed_ms > 0:
        add(_speed_item(float(default_speed_ms)))
        current_speed = float(default_speed_ms)

    # --- vzlet ---------------------------------------------------------
    # ArduCopter pri NAV_TAKEOFF ignorira lat/lon (vzleti navpicno nad
    # trenutno pozicijo), zato posljemo nicli --- tako je nedvoumno.
    add(MissionItem(
        seq=0, frame=MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
        command=MAV_CMD_NAV_TAKEOFF, param1=0.0, x=0, y=0, z=takeoff_alt,
    ))

    # --- tocke poti ----------------------------------------------------
    trigger_active = False
    prev_block: Optional[int] = None

    for p in pts:
        block = getattr(p, "map_block", None)

        # Konec mapping bloka -> ugasni intervalno slikanje.
        if trigger_active and block != prev_block:
            add(_trigg_dist_item(0.0))
            trigger_active = False

        # Sprememba hitrosti na tocki.
        p_speed = getattr(p, "speed_ms", None)
        if p_speed and float(p_speed) > 0 and (
            current_speed is None or abs(float(p_speed) - current_speed) > 1e-6
        ):
            add(_speed_item(float(p_speed)))
            current_speed = float(p_speed)

        # Zacetek mapping bloka -> prizgi intervalno slikanje po razdalji.
        if (
            camera_enabled and block is not None and block != prev_block
            and getattr(p, "trigger_distance_m", None)
        ):
            dist = float(p.trigger_distance_m)
            if dist > 0:
                add(_trigg_dist_item(dist))
                trigger_active = True

        yaw = _yaw_param(p)
        add(MissionItem(
            seq=0, frame=MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
            command=MAV_CMD_NAV_WAYPOINT,
            param1=float(getattr(p, "hover_time_s", 0.0) or 0.0),
            param2=float(accept_radius_m),
            param3=0.0,
            param4=yaw,
            x=_e7(float(p.lat)), y=_e7(float(p.lon)),
            z=float(p.altitude_m),
        ))

        # Posamicno slikanje na waypointu (samo ce ni ze aktiven
        # intervalni sprozilec za ta blok).
        action = str(getattr(p, "action_type", "NONE") or "NONE")
        if camera_enabled and not trigger_active and action in PHOTO_ACTIONS:
            count = BURST_COUNT if action == "CAPTURE_BURST" else 1
            add(MissionItem(
                seq=0, frame=MAV_FRAME_MISSION,
                command=MAV_CMD_IMAGE_START_CAPTURE,
                param2=0.0,            # interval [s]
                param3=float(count),   # stevilo posnetkov
                param4=0.0,            # sequence number (0 = ni pomembno)
            ))

        prev_block = block

    if trigger_active:
        add(_trigg_dist_item(0.0))

    # --- zakljucek -----------------------------------------------------
    fa = (finish_action or "RTH").upper()
    if fa == "RTH":
        add(MissionItem(
            seq=0, frame=MAV_FRAME_MISSION,
            command=MAV_CMD_NAV_RETURN_TO_LAUNCH,
        ))
    elif fa == "LAND":
        add(MissionItem(
            seq=0, frame=MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
            command=MAV_CMD_NAV_LAND,
            x=_e7(h_lat), y=_e7(h_lon), z=0.0,
        ))
    else:
        # HOVER: brez zakljucnega ukaza. Dron obvisi na zadnji tocki, dokler
        # ne posege pilot ali ne sprozi failsafe.
        res.warnings.append(
            "Dejanje ob koncu je HOVER: misija se konca brez pristanka. "
            "Dron bo lebdel na zadnji tocki, dokler ne posege pilot."
        )

    if len(items) > MAX_MISSION_ITEMS:
        res.warnings.append(
            f"Misija ima {len(items)} itemov, kar presega priporoceno mejo "
            f"{MAX_MISSION_ITEMS} za Pixhawk 2.4.8. Zmanjsaj prekrivanje "
            f"ali razdeli obmocje na vec misij."
        )

    res.items = items
    return res


def _speed_item(speed_ms: float) -> MissionItem:
    """``DO_CHANGE_SPEED`` --- param1=1 pomeni talno hitrost."""
    return MissionItem(
        seq=0, frame=MAV_FRAME_MISSION, command=MAV_CMD_DO_CHANGE_SPEED,
        param1=1.0, param2=float(speed_ms), param3=-1.0, param4=0.0,
    )


def _trigg_dist_item(distance_m: float) -> MissionItem:
    """``DO_SET_CAM_TRIGG_DIST`` --- 0 ugasne intervalno slikanje."""
    return MissionItem(
        seq=0, frame=MAV_FRAME_MISSION,
        command=MAV_CMD_DO_SET_CAM_TRIGG_DIST,
        param1=float(distance_m),
    )


def _yaw_param(p: Any) -> float:
    """Vrne param4 (yaw) za waypoint.

    ``NaN`` pomeni "ne spreminjaj smeri" in je pravilna vrednost za
    ``AUTO`` / ``NEXT_WP``. Pri ``FIXED`` posljemo zeljeno smer.
    """
    mode = str(getattr(p, "heading_mode", "AUTO") or "AUTO").upper()
    hdg = getattr(p, "heading_deg", None)
    if mode == "FIXED" and hdg is not None:
        return float(hdg) % 360.0
    return float("nan")


def describe_items(items: Iterable[MissionItem]) -> str:
    """Citljiv izpis zaporedja --- za diagnostiko in prilogo naloge."""
    lines = [f"{'seq':>4}  {'ukaz':<24} {'frame':>5}  parametri"]
    for it in items:
        if it.command in (MAV_CMD_NAV_WAYPOINT, MAV_CMD_NAV_TAKEOFF,
                          MAV_CMD_NAV_LAND):
            yaw = "auto" if math.isnan(it.param4) else f"{it.param4:.0f} deg"
            detail = (f"lat={it.lat:.7f} lon={it.lon:.7f} z={it.z:.1f} m "
                      f"hold={it.param1:.0f} s r={it.param2:.1f} m yaw={yaw}")
        elif it.command == MAV_CMD_DO_CHANGE_SPEED:
            detail = f"v={it.param2:.1f} m/s"
        elif it.command == MAV_CMD_DO_SET_CAM_TRIGG_DIST:
            detail = ("izklop" if it.param1 == 0
                      else f"vsakih {it.param1:.1f} m")
        elif it.command == MAV_CMD_IMAGE_START_CAPTURE:
            detail = f"n={it.param3:.0f}"
        else:
            detail = ""
        lines.append(f"{it.seq:>4}  {it.command_name:<24} {it.frame:>5}  {detail}")
    return "\n".join(lines)
