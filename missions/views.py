"""Pogledi za misije / Mission views.

Koncisca so razdeljena na tri skupine:

* **HTML** --- seznam, podrobnost, nacrtovalnik;
* **JSON, branje** --- misija, telemetrija, sistemska diagnostika, seznam
  letalnih logov;
* **JSON, kontrola** --- povezava s krmilnikom, prenos misije, arm, nacin,
  start, logiranje. Vse zascitene z :func:`~missions.decorators.control_required`.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import transaction
from django.http import (
    FileResponse, Http404, HttpRequest, HttpResponse, JsonResponse,
)
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET, require_POST

from core.storage import StorageUnavailableError

from .decorators import control_required, require_confirm
from .defaults import DEFAULT_DRONE_SLUG, ensure_default_drone
from .models import (
    ActionType, DroneProfile, ElementType, HeadingMode, MapPattern, Mission,
    MissionElement, PathStyle, TestFlightSettings,
)
from .services.flight_storage import resolve_log_dir, storage_status
from .services.gallery import (
    build_zip, collect_keys, fetch_snapshot, list_gallery, resolve_image,
    save_capture, snapshot_url,
)
from .services.grid_planner import CameraSpec, GridParams, plan_grid_mission
from .services.linearize import camera_spec_for, linearize_mission
from .services.mavlink_bridge import get_bridge, list_serial_ports
from .services.mavlink_mission import build_mission_items, describe_items
from .services.network_switch import read_state, request_force, touch_heartbeat
from .services.system_power import request_poweroff
from .services.system_stats import get_system_stats
from .services.telemetry_log import list_sessions
from .services.auto_tests import abort_test, start_test, status_test
from .services.hop_test import MAX_LEG_M, MIN_LEG_M, HopTestParams
from .services.test_flight import (
    MAX_ALTITUDE_M, MIN_ALTITUDE_M, TestFlightParams, evaluate_checks,
)
from .services.usb_devices import (
    UsbDeviceError, list_usb_partitions, select_usb_partition,
)


# ---------------------------------------------------------------------------
# Pretvorbe v JSON
# ---------------------------------------------------------------------------
def _element_to_json(e: MissionElement) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": e.pk, "order": e.order, "element_type": e.element_type,
        "name": e.name,
        "altitude_m": float(e.altitude_m),
        "speed_ms": float(e.speed_ms) if e.speed_ms is not None else None,
    }
    if e.is_waypoint:
        base.update({
            "lat": float(e.lat) if e.lat is not None else None,
            "lon": float(e.lon) if e.lon is not None else None,
            "heading_mode": e.heading_mode,
            "heading_deg": float(e.heading_deg) if e.heading_deg is not None else None,
            "gimbal_pitch_deg": float(e.gimbal_pitch_deg),
            "path_style": e.path_style,
            "action_type": e.action_type,
            "hover_time_s": float(e.hover_time_s),
        })
    else:
        base.update({
            "polygon_geojson": e.polygon_geojson,
            "pattern": e.pattern,
            "front_overlap_pct": e.front_overlap_pct,
            "side_overlap_pct": e.side_overlap_pct,
            "track_angle_deg": float(e.track_angle_deg),
        })
    return base


def _mission_to_payload(m: Mission) -> dict[str, Any]:
    return {
        "id": m.pk, "name": m.name, "description": m.description,
        "drone_id": m.drone_id,
        "home_lat": float(m.home_lat) if m.home_lat is not None else None,
        "home_lon": float(m.home_lon) if m.home_lon is not None else None,
        "home_alt_amsl_m": (float(m.home_alt_amsl_m)
                            if m.home_alt_amsl_m is not None else None),
        "altitude_mode": m.altitude_mode,
        "default_altitude_m": float(m.default_altitude_m),
        "default_speed_ms": float(m.default_speed_ms),
        "finish_action": m.finish_action,
        "elements": [_element_to_json(e) for e in m.elements.all().order_by("order")],
    }


def _empty_payload(default_drone: DroneProfile) -> dict[str, Any]:
    return {
        "id": None, "name": "Nova misija", "description": "",
        "drone_id": default_drone.pk,
        "home_lat": None, "home_lon": None, "home_alt_amsl_m": None,
        "altitude_mode": "AGL", "default_altitude_m": 50.0,
        "default_speed_ms": 5.0, "finish_action": "RTH",
        "elements": [],
    }


def _dec(v: Any) -> Decimal | None:
    return None if v is None else Decimal(str(v))


def _body(request: HttpRequest) -> dict[str, Any]:
    """Prebere JSON telo; prazno telo vrne prazen slovar."""
    data = getattr(request, "confirmed_data", None)
    if isinstance(data, dict):
        return data
    try:
        return json.loads((request.body or b"{}").decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------------------
# Seznam in podrobnost
# ---------------------------------------------------------------------------
def mission_list(request: HttpRequest) -> HttpResponse:
    missions = Mission.objects.select_related("drone").all()
    return render(request, "missions/list.html", {"missions": missions})


def mission_detail(request: HttpRequest, pk: int) -> HttpResponse:
    mission = get_object_or_404(
        Mission.objects.select_related("drone").prefetch_related("elements"), pk=pk)
    points = linearize_mission(mission)
    built = build_mission_items(
        points,
        home_lat=float(mission.home_lat) if mission.home_lat is not None else None,
        home_lon=float(mission.home_lon) if mission.home_lon is not None else None,
        takeoff_altitude_m=float(mission.default_altitude_m),
        default_speed_ms=float(mission.default_speed_ms),
        finish_action=mission.finish_action,
    )
    return render(request, "missions/detail.html", {
        "mission": mission,
        "elements": mission.elements.all().order_by("order"),
        "points": points,
        "point_count": len(points),
        "item_count": built.count,
        "warnings": built.warnings,
    })


# ---------------------------------------------------------------------------
# Kartni vmesnik
# ---------------------------------------------------------------------------
def mission_planner(request: HttpRequest, pk: int | None = None) -> HttpResponse:
    default_drone = ensure_default_drone()
    drones = list(DroneProfile.objects.all())
    if pk is not None:
        mission = get_object_or_404(
            Mission.objects.select_related("drone").prefetch_related("elements"), pk=pk)
        payload = _mission_to_payload(mission)
        api_url = f"/api/missions/{pk}/"
    else:
        preferred = next(
            (d for d in drones if d.slug == DEFAULT_DRONE_SLUG), default_drone)
        payload = _empty_payload(preferred)
        api_url = "/api/missions/"
    drones_payload = [
        {"id": d.pk, "display_name": d.display_name,
         "max_speed_ms": float(d.max_speed_ms),
         "max_altitude_m": float(d.max_altitude_m),
         "sensor_width_mm": float(d.sensor_width_mm),
         "sensor_height_mm": float(d.sensor_height_mm),
         "focal_length_mm": float(d.focal_length_mm),
         "image_width_px": d.image_width_px, "image_height_px": d.image_height_px}
        for d in drones
    ]
    return render(request, "missions/planner.html", {
        "mission_json": json.dumps(payload),
        "drones_json": json.dumps(drones_payload),
        "csrf_token": get_token(request),
        "api_url": api_url, "is_new": pk is None,
    })


# ---------------------------------------------------------------------------
# JSON API --- misije
# ---------------------------------------------------------------------------
def _apply_mission_header(m: Mission, data: dict, drone: DroneProfile) -> None:
    m.name = (data.get("name") or "Nova misija")[:120]
    m.description = data.get("description", "")
    m.drone = drone
    m.home_lat = _dec(data.get("home_lat"))
    m.home_lon = _dec(data.get("home_lon"))
    m.home_alt_amsl_m = _dec(data.get("home_alt_amsl_m"))
    m.altitude_mode = data.get("altitude_mode", "AGL")
    m.default_altitude_m = _dec(data.get("default_altitude_m") or 50)
    m.default_speed_ms = _dec(data.get("default_speed_ms") or 5)
    m.finish_action = data.get("finish_action", "RTH")


@require_POST
def mission_api_save(request: HttpRequest, pk: int | None = None) -> JsonResponse:
    try:
        data = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    if not data.get("drone_id"):
        return JsonResponse({"error": "Manjka 'drone_id'."}, status=400)
    drone = get_object_or_404(DroneProfile, pk=data["drone_id"])
    with transaction.atomic():
        if pk:
            mission = get_object_or_404(Mission, pk=pk)
        else:
            mission = Mission()
        _apply_mission_header(mission, data, drone)
        mission.save()

        mission.elements.all().delete()
        elements = data.get("elements", [])
        to_create = []
        for i, el in enumerate(elements):
            order = el.get("order", i + 1)
            t = el.get("element_type", "WP")
            common = dict(
                mission=mission, order=order, element_type=t,
                name=(el.get("name") or "")[:64],
                altitude_m=_dec(el.get("altitude_m") or 50),
                speed_ms=_dec(el.get("speed_ms")),
            )
            if t == ElementType.WAYPOINT:
                common.update(
                    lat=_dec(el.get("lat")), lon=_dec(el.get("lon")),
                    heading_mode=el.get("heading_mode") or HeadingMode.AUTO,
                    heading_deg=_dec(el.get("heading_deg")),
                    gimbal_pitch_deg=_dec(el.get("gimbal_pitch_deg") or -90),
                    path_style=el.get("path_style") or PathStyle.STRAIGHT,
                    action_type=el.get("action_type") or ActionType.NONE,
                    hover_time_s=_dec(el.get("hover_time_s") or 0),
                )
            else:
                common.update(
                    polygon_geojson=el.get("polygon_geojson"),
                    pattern=el.get("pattern") or MapPattern.GRID,
                    front_overlap_pct=int(el.get("front_overlap_pct") or 80),
                    side_overlap_pct=int(el.get("side_overlap_pct") or 70),
                    track_angle_deg=_dec(el.get("track_angle_deg") or 0),
                )
            to_create.append(MissionElement(**common))
        MissionElement.objects.bulk_create(to_create)

    mission = (Mission.objects.select_related("drone")
               .prefetch_related("elements").get(pk=mission.pk))
    return JsonResponse(_mission_to_payload(mission))


@require_GET
def mission_api_get(request: HttpRequest, pk: int) -> JsonResponse:
    mission = get_object_or_404(
        Mission.objects.select_related("drone").prefetch_related("elements"), pk=pk)
    return JsonResponse(_mission_to_payload(mission))


# ---------------------------------------------------------------------------
# Predogled zgrajene MAVLink misije (brez posiljanja)
# ---------------------------------------------------------------------------
def _build_for(mission: Mission, camera_enabled: bool = True):
    points = linearize_mission(mission)
    built = build_mission_items(
        points,
        home_lat=float(mission.home_lat) if mission.home_lat is not None else None,
        home_lon=float(mission.home_lon) if mission.home_lon is not None else None,
        takeoff_altitude_m=float(mission.default_altitude_m),
        default_speed_ms=float(mission.default_speed_ms),
        finish_action=mission.finish_action,
        camera_enabled=camera_enabled,
    )
    return points, built


@require_GET
def mission_items_api(request: HttpRequest, pk: int) -> JsonResponse:
    """Vrne zaporedje ``MISSION_ITEM_INT``, kot bi bilo poslano krmilniku.

    Uporabno za preverjanje pred letom in kot izpis za prilogo naloge ---
    ``?format=text`` vrne citljivo tabelo.
    """
    mission = get_object_or_404(
        Mission.objects.select_related("drone").prefetch_related("elements"), pk=pk)
    points, built = _build_for(mission)
    if request.GET.get("format") == "text":
        return HttpResponse(
            describe_items(built.items), content_type="text/plain; charset=utf-8")
    return JsonResponse({
        "mission_id": mission.pk,
        "mission_name": mission.name,
        "point_count": len(points),
        "count": built.count,
        "warnings": built.warnings,
        "items": [it.to_dict() for it in built.items],
    })


# ---------------------------------------------------------------------------
# Grid plan endpoint (predogled mreze za odjemalca)
# ---------------------------------------------------------------------------
@require_POST
def grid_plan_api(request: HttpRequest) -> JsonResponse:
    try:
        body = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    drone = get_object_or_404(DroneProfile, pk=body["drone_id"])
    grid = body["map_element"]
    cam = camera_spec_for(drone)
    params = GridParams(
        altitude_m=float(grid["altitude_m"]),
        front_overlap_pct=float(grid["front_overlap_pct"]),
        side_overlap_pct=float(grid["side_overlap_pct"]),
        track_angle_deg=float(grid.get("track_angle_deg", 0)),
    )
    try:
        plan = plan_grid_mission(grid["polygon_geojson"], cam, params)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    if not plan.waypoints:
        # Degeneriran ali prevec majhen poligon: generator vrne prazno progo.
        # Za odjemalca je to napaka, ne uspesen prazen rezultat.
        return JsonResponse(
            {"error": "Poligon je prevec majhen ali degeneriran --- "
                      "mreza nima nobene tocke."},
            status=400,
        )
    return JsonResponse({
        "waypoints_lonlat": [[lon, lat] for lon, lat in plan.waypoints],
        "stats": {
            "gsd_cm_per_px": round(plan.gsd_cm_per_px, 3),
            "footprint_across_m": round(plan.footprint_across_m, 2),
            "footprint_along_m": round(plan.footprint_along_m, 2),
            "line_spacing_m": round(plan.line_spacing_m, 2),
            "trigger_distance_m": round(plan.trigger_distance_m, 2),
            "total_distance_m": round(plan.total_distance_m, 1),
            "num_lines": plan.num_lines,
            "estimated_images": plan.estimated_images,
        },
    })


# ---------------------------------------------------------------------------
# MAVLink --- branje
# ---------------------------------------------------------------------------
@require_GET
def serial_ports_api(request: HttpRequest) -> JsonResponse:
    """Vrne seznam vidnih serijskih naprav (USB tty, COM)."""
    return JsonResponse({"ports": list_serial_ports()})


@require_GET
def telemetry_api(request: HttpRequest) -> JsonResponse:
    """Najnovejsa telemetrija (snapshot) + diagnostika samodejne povezave."""
    from .services.autoconnect import autoconnect_status
    snap = get_bridge().get_snapshot()
    snap["autoconnect"] = autoconnect_status()
    return JsonResponse(snap)


@require_GET
def system_stats_api(request: HttpRequest) -> JsonResponse:
    """Sistem stats: CPU, RAM, disk, temp, voltage, throttled, uptime."""
    return JsonResponse(get_system_stats())


@require_POST
@control_required
@require_confirm
def system_poweroff_api(request: HttpRequest) -> JsonResponse:
    """Varna zaustavitev RPi: sync + zakasnjen poweroff.

    Prepreci korupcijo SD/gita ob izklopu napajanja. Ce je Pixhawk
    armiran, zavrne (razen ``force=true``).
    """
    data = _body(request)
    result = request_poweroff(force=bool(data.get("force")))
    return JsonResponse(result, status=200 if result.get("ok") else 400)


# ---------------------------------------------------------------------------
# MAVLink --- kontrola
# ---------------------------------------------------------------------------
@require_POST
@control_required
def mavlink_connect_api(request: HttpRequest) -> JsonResponse:
    """Vzpostavi povezavo z izbrano napravo / hitrostjo."""
    data = _body(request)
    device = (data.get("device") or "").strip()
    baud = int(data.get("baud") or 115200)
    if not device:
        return JsonResponse({"error": "manjka 'device'"}, status=400)
    return JsonResponse(get_bridge().connect(device, baud))


@require_POST
@control_required
def mavlink_disconnect_api(request: HttpRequest) -> JsonResponse:
    return JsonResponse(get_bridge().disconnect())


@require_POST
@control_required
def mission_upload_api(request: HttpRequest, pk: int) -> JsonResponse:
    """Zgradi in poslje misijo na krmilnik po MAVLink mission protokolu."""
    mission = get_object_or_404(
        Mission.objects.select_related("drone").prefetch_related("elements"), pk=pk)
    data = _body(request)
    camera_enabled = bool(data.get("camera_enabled", True))

    points, built = _build_for(mission, camera_enabled=camera_enabled)
    if not points:
        return JsonResponse(
            {"ok": False, "error": "Misija nima nobene letalne tocke."}, status=400)

    bridge = get_bridge()
    result = bridge.upload_mission(built.items)
    result.update({
        "mission_id": mission.pk,
        "mission_name": mission.name,
        "point_count": len(points),
        "item_count": built.count,
        "warnings": built.warnings,
    })
    if result.get("ok"):
        # Zapomni si nalozeno misijo: HUD jo prikaze, zapisovalnik
        # telemetrije pa ob armanju shrani nacrt ob log.
        bridge.set_pending_mission(
            mission.pk, mission.name,
            plan={
                "mission": _mission_to_payload(mission),
                "points": [p.__dict__ for p in points],
                "items": [it.to_dict() for it in built.items],
            },
            item_count=built.count,
        )
    return JsonResponse(result, status=200 if result.get("ok") else 400)


@require_POST
@control_required
@require_confirm
def mavlink_arm_api(request: HttpRequest) -> JsonResponse:
    """Armira ali dis-armira krmilnik. Zahteva ``confirm=true``."""
    data = _body(request)
    arm = bool(data.get("arm", True))
    force = bool(data.get("force", False))
    result = get_bridge().arm(arm, force=force)
    return JsonResponse(result, status=200 if result.get("ok") else 400)


@require_POST
@control_required
def mavlink_mode_api(request: HttpRequest) -> JsonResponse:
    """Preklopi letalni nacin (npr. ``AUTO``, ``GUIDED``, ``RTL``, ``LAND``)."""
    data = _body(request)
    mode = (data.get("mode") or "").strip()
    if not mode:
        return JsonResponse({"ok": False, "error": "Manjka 'mode'."}, status=400)
    result = get_bridge().set_mode(mode)
    return JsonResponse(result, status=200 if result.get("ok") else 400)


@require_POST
@control_required
def mavlink_capture_api(request: HttpRequest) -> JsonResponse:
    """Sproži ``IMAGE_START_CAPTURE`` prek MAVLink (test na tleh / hop).

    Companion ``camera_trigger`` ujame ukaz in shrani JPEG. FC lahko vrne
    FAILED — to je pričakovano brez CAM backend-a; ``ok`` v odgovoru zato
    sledi mostu, ne nujno zapisu datoteke.
    """
    data = _body(request)
    try:
        count = int(data.get("count") or 1)
    except (TypeError, ValueError):
        return JsonResponse({"ok": False, "error": "Neveljaven 'count'."},
                            status=400)
    count = max(1, min(count, 10))
    result = get_bridge().capture_image(count)
    # Mehki FC odgovori (FAILED/UNSUPPORTED/…) — hop jih obravnava kot OK.
    if not result.get("ok"):
        name = str(result.get("result_name") or "")
        err = str(result.get("error") or "")
        soft = name in (
            "FAILED", "UNSUPPORTED", "DENIED", "TEMPORARILY_REJECTED",
        ) or "Ni ACK" in err
        if soft:
            result = {
                **result,
                "ok": True,
                "soft_fc_ack": True,
                "note": "Ukaz poslan; JPEG shrani camera_trigger (če teče).",
            }
    return JsonResponse(result, status=200 if result.get("ok") else 400)


@require_POST
@control_required
@require_confirm
def mavlink_mag_cal_api(request: HttpRequest) -> JsonResponse:
    """Zazene ali prekine onboard kalibracijo kompasa.

    Telo: ``{"action": "start"|"cancel", "confirm": true}``.
    """
    data = _body(request)
    action = (data.get("action") or "start").strip().lower()
    if action == "cancel":
        result = get_bridge().cancel_mag_cal()
    elif action == "start":
        result = get_bridge().start_mag_cal(
            autosave=bool(data.get("autosave", True)))
    else:
        return JsonResponse(
            {"ok": False, "error": "action mora biti 'start' ali 'cancel'."},
            status=400)
    return JsonResponse(result, status=200 if result.get("ok") else 400)


# Dovoljeni FC parametri iz GUI (ime → (min, max)).
# RTL_ALT je v centimetrih (ArduCopter): 300 = 3 m.
_PARAM_WHITELIST: dict[str, tuple[float, float]] = {
    "ARMING_MAGTHRESH": (0.0, 500.0),
    "RTL_ALT": (0.0, 8000.0),
}


def _clamp_param(name: str, value: float) -> float:
    lo, hi = _PARAM_WHITELIST[name]
    return max(lo, min(hi, float(value)))


@require_GET
def mavlink_param_api(request: HttpRequest) -> JsonResponse:
    """Prebere en dovoljen parameter s krmilnika."""
    name = (request.GET.get("name") or "").strip().upper()
    if name not in _PARAM_WHITELIST:
        return JsonResponse({
            "ok": False,
            "error": f"Parameter '{name}' ni dovoljen.",
            "allowed": sorted(_PARAM_WHITELIST),
        }, status=400)
    result = get_bridge().get_param(name)
    return JsonResponse(result, status=200 if result.get("ok") else 400)


@require_POST
@control_required
@require_confirm
def mavlink_param_set_api(request: HttpRequest) -> JsonResponse:
    """Nastavi dovoljen parameter na krmilniku.

    Telo: ``{"name": "ARMING_MAGTHRESH", "value": 150, "confirm": true}``.
    """
    data = _body(request)
    name = (data.get("name") or "").strip().upper()
    if name not in _PARAM_WHITELIST:
        return JsonResponse({
            "ok": False,
            "error": f"Parameter '{name}' ni dovoljen.",
            "allowed": sorted(_PARAM_WHITELIST),
        }, status=400)
    try:
        value = float(data.get("value"))
    except (TypeError, ValueError):
        return JsonResponse({"ok": False, "error": "Manjka veljavna 'value'."},
                            status=400)
    value = _clamp_param(name, value)
    result = get_bridge().set_param(name, value)
    if result.get("ok"):
        result["value"] = float(result.get("value", value))
    return JsonResponse(result, status=200 if result.get("ok") else 400)


@require_POST
@control_required
@require_confirm
def mission_start_api(request: HttpRequest) -> JsonResponse:
    """Sprozi nalozeno misijo: ARM (v GUIDED po potrebi) → AUTO → START.

    ArduCopter privzeto ne dovoli armanja v AUTO, zato najprej armatiramo
    v dovoljenem načinu, šele nato preklopimo v AUTO. Glej
    ``missions.services.mission_start``.
    """
    from .services.mission_start import start_uploaded_mission

    data = _body(request)
    result = start_uploaded_mission(
        get_bridge(),
        skip_arm=data.get("skip_arm") is True,
        skip_mode=data.get("skip_mode") is True,
    )
    return JsonResponse(result, status=200 if result.get("ok") else 400)


# ---------------------------------------------------------------------------
# Letalni logi
# ---------------------------------------------------------------------------
@require_GET
def storage_devices_api(request: HttpRequest) -> JsonResponse:
    """Seznam USB particij, ki jih lahko uporabnik izbere za podatke letov."""
    try:
        devices = list_usb_partitions()
    except UsbDeviceError as exc:
        return JsonResponse(
            {"ok": False, "error": str(exc), "devices": []}, status=503)
    return JsonResponse({"ok": True, "devices": devices})


@require_POST
@control_required
@require_confirm
def storage_select_api(request: HttpRequest) -> JsonResponse:
    """Trajno izbere in mounta preverjeno USB particijo."""
    bridge = get_bridge()
    if bridge.logger is not None and bridge.logger.is_active:
        return JsonResponse({
            "ok": False,
            "error": "USB-ja ni mogoče zamenjati med aktivnim zapisovanjem.",
        }, status=409)
    device = str(_body(request).get("device") or "").strip()
    try:
        result = select_usb_partition(device)
    except UsbDeviceError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    bridge.ensure_logger(force=True)
    return JsonResponse(result)


def _log_dir() -> Path:
    return resolve_log_dir()


@require_GET
def logs_api(request: HttpRequest) -> JsonResponse:
    """Seznam zapisanih sej telemetrije (disk je vir resnice)."""
    storage = storage_status()
    if not storage["available"]:
        return JsonResponse({
            "storage": storage,
            "log_dir": None,
            "active": {"active": False},
            "sessions": [],
        })
    log_dir = Path(str(storage["path"]))
    bridge = get_bridge()
    bridge.ensure_logger(force=True)
    return JsonResponse({
        "storage": storage,
        "log_dir": str(log_dir),
        "active": bridge.logger.status() if bridge.logger else {"active": False},
        "sessions": list_sessions(log_dir),
    })


@require_POST
@control_required
def logging_start_api(request: HttpRequest) -> JsonResponse:
    data = _body(request)
    result = get_bridge().start_logging(
        reason=str(data.get("reason") or "manual"),
        note=str(data.get("note") or ""),
    )
    return JsonResponse(result, status=200 if "error" not in result else 503)


@require_POST
@control_required
def logging_stop_api(request: HttpRequest) -> JsonResponse:
    return JsonResponse(get_bridge().stop_logging(reason="manual"))


_ALLOWED_LOG_FILES = {
    "telemetry.jsonl": "application/x-ndjson",
    "captures.jsonl": "application/x-ndjson",
    "plan.json": "application/json",
    "meta.json": "application/json",
}


@require_GET
def log_download_api(request: HttpRequest, name: str, filename: str) -> HttpResponse:
    """Prenese eno datoteko iz seje.

    Ime seje in datoteke se preverita proti belemu seznamu, pot pa se
    razresi in primerja s korensko mapo --- brez tega bi bil ``..`` v imenu
    dovolj za branje poljubne datoteke na Raspberry Pi-ju.
    """
    if filename not in _ALLOWED_LOG_FILES:
        raise Http404("Neznana datoteka.")
    try:
        base = _log_dir().resolve()
    except (OSError, StorageUnavailableError):
        raise Http404("USB medij ni na voljo.")
    target = (base / name / filename).resolve()
    if base not in target.parents or not target.is_file():
        raise Http404("Datoteka ne obstaja.")
    return FileResponse(
        target.open("rb"),
        content_type=_ALLOWED_LOG_FILES[filename],
        as_attachment=True,
        filename=f"{name}_{filename}",
    )


# ---------------------------------------------------------------------------
# Samodejni testni polet (vzlet --- lebdenje --- pristanek)
# ---------------------------------------------------------------------------
def _hover_params_from_db() -> TestFlightParams:
    """Naloži shranjene parametre Testa 1 (hover)."""
    s = TestFlightSettings.load()
    return TestFlightParams(
        altitude_m=s.altitude_m,
        hover_s=s.hover_s,
        countdown_s=s.countdown_s,
        min_satellites=s.min_satellites,
        max_hdop=s.max_hdop,
        require_gps=s.require_gps,
    ).clamp()


def _hop_params_from_db() -> HopTestParams:
    """Naloži shranjene parametre Testa 2 (hop)."""
    s = TestFlightSettings.load()
    return HopTestParams(
        altitude_m=s.hop_altitude_m,
        leg_m=s.hop_leg_m,
        countdown_s=s.hop_countdown_s,
        min_satellites=s.hop_min_satellites,
        max_hdop=s.hop_max_hdop,
        require_gps=s.hop_require_gps,
    ).clamp()


# Staro ime — settings page in starejši klici.
def _test_flight_params_from_db() -> TestFlightParams:
    return _hover_params_from_db()


def _hover_params_to_json(p: TestFlightParams) -> dict[str, Any]:
    return {
        "altitude_m": p.altitude_m,
        "hover_s": p.hover_s,
        "countdown_s": p.countdown_s,
        "min_satellites": p.min_satellites,
        "max_hdop": p.max_hdop,
        "require_gps": p.require_gps,
    }


def _hop_params_to_json(p: HopTestParams) -> dict[str, Any]:
    return {
        "altitude_m": p.altitude_m,
        "leg_m": p.leg_m,
        "countdown_s": p.countdown_s,
        "min_satellites": p.min_satellites,
        "max_hdop": p.max_hdop,
        "require_gps": p.require_gps,
    }


def _all_test_params_to_json() -> dict[str, Any]:
    return {
        "hover": _hover_params_to_json(_hover_params_from_db()),
        "hop": _hop_params_to_json(_hop_params_from_db()),
    }


def _test_flight_params_to_json(p: TestFlightParams) -> dict[str, Any]:
    """Združen JSON (hover + hop) za API združljivost z gnezdenim zapisom."""
    data = _all_test_params_to_json()
    # Flat ključi = Test 1 (hover), da stari odjemalci še berejo.
    data.update(_hover_params_to_json(p))
    return data


def _parse_hover_params(data: dict[str, Any],
                        defaults: TestFlightParams) -> TestFlightParams:
    src = data.get("hover") if isinstance(data.get("hover"), dict) else data
    return TestFlightParams(
        altitude_m=_float_or(src.get("altitude_m"), defaults.altitude_m),
        hover_s=_float_or(src.get("hover_s"), defaults.hover_s),
        countdown_s=_float_or(src.get("countdown_s"), defaults.countdown_s),
        min_satellites=int(_float_or(src.get("min_satellites"),
                                     defaults.min_satellites)),
        max_hdop=_float_or(src.get("max_hdop"), defaults.max_hdop),
        require_gps=bool(src.get("require_gps", defaults.require_gps)),
    ).clamp()


def _parse_test_flight_params(data: dict[str, Any],
                              defaults: TestFlightParams) -> TestFlightParams:
    return _parse_hover_params(data, defaults)


def _parse_hop_params(data: dict[str, Any],
                      defaults: HopTestParams) -> HopTestParams:
    src = data.get("hop") if isinstance(data.get("hop"), dict) else data
    return HopTestParams(
        altitude_m=_float_or(src.get("altitude_m"), defaults.altitude_m),
        leg_m=_float_or(src.get("leg_m"), defaults.leg_m),
        countdown_s=_float_or(src.get("countdown_s"), defaults.countdown_s),
        min_satellites=int(_float_or(src.get("min_satellites"),
                                     defaults.min_satellites)),
        max_hdop=_float_or(src.get("max_hdop"), defaults.max_hdop),
        require_gps=bool(src.get("require_gps", defaults.require_gps)),
    ).clamp()


def test_flight_page(request: HttpRequest) -> HttpResponse:
    """Stran za samodejni testni polet (hover + skok)."""
    require_auth = getattr(settings, "UAV_REQUIRE_AUTH", True)
    return render(request, "core/test_flight.html", {
        "csrf_token": get_token(request),
        "can_control": (not require_auth) or request.user.is_authenticated,
        "min_altitude_m": MIN_ALTITUDE_M,
        "max_altitude_m": MAX_ALTITUDE_M,
        "min_leg_m": MIN_LEG_M,
        "max_leg_m": MAX_LEG_M,
        "defaults": _hover_params_from_db(),
        "hop_defaults": _hop_params_from_db(),
    })


def mag_cal_page(request: HttpRequest) -> HttpResponse:
    """Celozaslonska stran za onboard kalibracijo kompasa."""
    require_auth = getattr(settings, "UAV_REQUIRE_AUTH", True)
    auto = request.GET.get("start") in ("1", "true", "yes")
    return render(request, "core/mag_cal.html", {
        "csrf_token": get_token(request),
        "can_control": (not require_auth) or request.user.is_authenticated,
        "auto_start": auto,
    })


def app_settings_page(request: HttpRequest) -> HttpResponse:
    """Centralna stran vseh uporabniško nastavljivih vrednosti."""
    require_auth = getattr(settings, "UAV_REQUIRE_AUTH", True)
    return render(request, "core/settings.html", {
        "csrf_token": get_token(request),
        "can_control": (not require_auth) or request.user.is_authenticated,
        "test_defaults": _hover_params_from_db(),
        "hop_defaults": _hop_params_from_db(),
        "serial_device": getattr(settings, "UAV_SERIAL_DEVICE", "auto"),
        "serial_baud": getattr(settings, "UAV_SERIAL_BAUD", 115200),
        "autoconnect": getattr(settings, "UAV_AUTOCONNECT", False),
        "log_trigger": getattr(settings, "UAV_LOG_TRIGGER", "takeoff"),
        "camera_url": getattr(settings, "DRONE_CAMERA_URL", "auto"),
    })


def gallery_page(request: HttpRequest) -> HttpResponse:
    """Galerija zajetih slik — po misijah in ostale po času."""
    require_auth = getattr(settings, "UAV_REQUIRE_AUTH", True)
    return render(request, "core/gallery.html", {
        "csrf_token": get_token(request),
        "can_control": (not require_auth) or request.user.is_authenticated,
    })


# ---------------------------------------------------------------------------
# Kamera — ročni zajem + galerija
# ---------------------------------------------------------------------------
@require_POST
@control_required
def camera_capture_api(request: HttpRequest) -> JsonResponse:
    """Zajame trenutni MJPEG okvir in ga shrani na shrambo letov."""
    data = _body(request)
    mission_id: int | None = None
    mission_name: str | None = None
    raw_mid = data.get("mission_id")
    if raw_mid not in (None, "", 0, "0"):
        try:
            mission_id = int(raw_mid)
        except (TypeError, ValueError):
            return JsonResponse(
                {"ok": False, "error": "Neveljaven mission_id."}, status=400)
        mission = Mission.objects.filter(pk=mission_id).first()
        if mission is None:
            return JsonResponse(
                {"ok": False, "error": "Misija ne obstaja."}, status=404)
        mission_name = mission.name

    try:
        log_dir = _log_dir()
    except StorageUnavailableError as exc:
        return JsonResponse(
            {"ok": False, "error": f"USB medij ni na voljo: {exc}"}, status=503)

    stream = str(getattr(settings, "DRONE_CAMERA_URL", "auto") or "auto")
    url = snapshot_url(stream)
    try:
        jpeg = fetch_snapshot(url)
    except RuntimeError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=503)

    gps: dict[str, Any] = {}
    try:
        snap = get_bridge().get_snapshot()
        pos = (snap.get("gps") or {}) if isinstance(snap, dict) else {}
        if pos.get("lat") is not None and pos.get("lon") is not None:
            gps = {
                "lat": pos.get("lat"),
                "lon": pos.get("lon"),
                "alt_rel_m": pos.get("rel_alt_m"),
                "yaw_deg": (snap.get("attitude") or {}).get("yaw_deg"),
            }
    except Exception:
        pass

    result = save_capture(
        jpeg, log_dir,
        mission_id=mission_id,
        mission_name=mission_name,
        gps=gps or None,
    )
    return JsonResponse(result)


@require_GET
def gallery_list_api(request: HttpRequest) -> JsonResponse:
    """Seznam slik, združen po misijah (+ ostale po času)."""
    storage = storage_status()
    if not storage["available"]:
        return JsonResponse({
            "storage": storage,
            "missions": [],
            "other": [],
            "mission_count": 0,
            "other_count": 0,
            "total_count": 0,
        })
    try:
        data = list_gallery(Path(str(storage["path"])))
    except OSError as exc:
        return JsonResponse({
            "storage": {**storage, "available": False, "error": str(exc)},
            "missions": [], "other": [],
            "mission_count": 0, "other_count": 0, "total_count": 0,
        })
    data["storage"] = storage
    return JsonResponse(data)


@require_GET
def gallery_image_api(
    request: HttpRequest, session: str, filename: str,
) -> HttpResponse:
    """Streže en JPEG (inline) iz galerije."""
    try:
        base = _log_dir().resolve()
        target = resolve_image(base, session, filename)
    except (OSError, StorageUnavailableError, FileNotFoundError):
        raise Http404("Slika ne obstaja.")
    return FileResponse(
        target.open("rb"),
        content_type="image/jpeg",
        as_attachment=False,
        filename=filename,
    )


@require_POST
@control_required
def gallery_download_api(request: HttpRequest) -> HttpResponse:
    """ZIP prenos izbranih slik, celotne misije ali ostalih.

    Telo: ``{"keys": [...]}`` | ``{"mission_id": N}`` | ``{"scope": "other"|"all"}``.
    """
    from datetime import datetime, timezone

    data = _body(request)
    try:
        log_dir = _log_dir()
    except StorageUnavailableError as exc:
        return JsonResponse(
            {"ok": False, "error": f"USB medij ni na voljo: {exc}"}, status=503)

    mid = data.get("mission_id")
    mission_id: int | None = None
    if mid not in (None, ""):
        try:
            mission_id = int(mid)
        except (TypeError, ValueError):
            return JsonResponse(
                {"ok": False, "error": "Neveljaven mission_id."}, status=400)

    keys = collect_keys(
        log_dir,
        keys=data.get("keys") if isinstance(data.get("keys"), list) else None,
        mission_id=mission_id,
        scope=str(data.get("scope") or "") or None,
    )
    if not keys:
        return JsonResponse(
            {"ok": False, "error": "Ni slik za prenos."}, status=400)

    buf = build_zip(log_dir, keys)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    response = HttpResponse(buf.getvalue(), content_type="application/zip")
    response["Content-Disposition"] = (
        f'attachment; filename="galerija-{stamp}.zip"')
    return response


@require_GET
def test_flight_status_api(request: HttpRequest) -> JsonResponse:
    """Stanje zaporedja + zive predpoletne preverbe.

    Preverbe se ocenijo tudi, kadar zaporedje ne tece --- tako pilot vidi,
    kdaj je dron pripravljen, se preden karkoli klikne.
    """
    status = status_test()
    if not status["checks"]:
        profile = (request.GET.get("profile") or "hover").strip().lower()
        if profile == "hop":
            base = _hop_params_from_db()
        else:
            profile = "hover"
            base = _hover_params_from_db()
        # evaluate_checks sprejme TestFlightParams; HopTestParams ima iste GPS polja.
        params = TestFlightParams(
            altitude_m=_float_or(request.GET.get("altitude_m"),
                                 base.altitude_m),
            min_satellites=int(_float_or(
                request.GET.get("min_satellites"), base.min_satellites)),
            max_hdop=_float_or(request.GET.get("max_hdop"), base.max_hdop),
            require_gps=base.require_gps,
        )
        if "require_gps" in request.GET:
            params.require_gps = request.GET.get("require_gps") not in (
                "0", "false", "False")
        checks = evaluate_checks(get_bridge().get_snapshot(), params.clamp())
        status["checks"] = [c.__dict__ for c in checks]
        status["checks_pass"] = all(c.ok for c in checks if c.required)
        status["profile"] = status.get("profile") or profile
    return JsonResponse(status)


def _float_or(raw: Any, default: float) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


@require_GET
def test_flight_params_api(request: HttpRequest) -> JsonResponse:
    """Vrni shranjene parametre Testa 1 in Testa 2 z naprave."""
    return JsonResponse(_test_flight_params_to_json(_hover_params_from_db()))


@require_POST
@control_required
def test_flight_params_save_api(request: HttpRequest) -> JsonResponse:
    """Shrani parametre Testa 1 in/ali Testa 2 v lokalno bazo."""
    data = _body(request)
    hover = _parse_hover_params(data, _hover_params_from_db())
    # Hop: gnezdeno ``hop`` ali flat ključi (hop_* / leg_m) ob shranjevanju obeh.
    hop_src = data.get("hop") if isinstance(data.get("hop"), dict) else None
    if hop_src is None and any(
        k in data for k in (
            "leg_m", "hop_altitude_m", "hop_min_satellites", "hop_max_hdop",
        )
    ):
        hop_src = {
            "altitude_m": data.get("hop_altitude_m", data.get("altitude_m")),
            "leg_m": data.get("leg_m"),
            "countdown_s": data.get(
                "hop_countdown_s", data.get("countdown_s")),
            "min_satellites": data.get(
                "hop_min_satellites", data.get("min_satellites")),
            "max_hdop": data.get("hop_max_hdop", data.get("max_hdop")),
            "require_gps": data.get(
                "hop_require_gps", data.get("require_gps")),
        }
    hop = (_parse_hop_params({"hop": hop_src}, _hop_params_from_db())
           if hop_src is not None
           else _hop_params_from_db())

    s = TestFlightSettings.load()
    s.altitude_m = hover.altitude_m
    s.hover_s = hover.hover_s
    s.countdown_s = hover.countdown_s
    s.min_satellites = hover.min_satellites
    s.max_hdop = hover.max_hdop
    s.require_gps = hover.require_gps
    s.hop_altitude_m = hop.altitude_m
    s.hop_leg_m = hop.leg_m
    s.hop_countdown_s = hop.countdown_s
    s.hop_min_satellites = hop.min_satellites
    s.hop_max_hdop = hop.max_hdop
    s.hop_require_gps = hop.require_gps
    s.save()
    return JsonResponse({
        "ok": True,
        "params": _test_flight_params_to_json(hover),
    })


@require_POST
@control_required
@require_confirm
def test_flight_start_api(request: HttpRequest) -> JsonResponse:
    """Zazene hover ali hop. Zahteva ``confirm=true``; ``profile`` = hover|hop."""
    data = _body(request)
    profile = str(data.get("profile") or "hover").strip().lower()
    if profile == "hop":
        params: TestFlightParams | HopTestParams = _parse_hop_params(
            data, _hop_params_from_db())
        label = (f"Testni skok {params.altitude_m:.1f} m / "
                 f"{params.leg_m:.1f} m N")
    else:
        profile = "hover"
        params = _parse_hover_params(data, _hover_params_from_db())
        label = f"Testni polet {params.altitude_m:.1f} m"
    result = start_test(profile, params)
    if result.get("ok"):
        get_bridge().set_pending_mission(None, label)
    return JsonResponse(result, status=200 if result.get("ok") else 400)


@require_POST
@control_required
def test_flight_abort_api(request: HttpRequest) -> JsonResponse:
    """Prekine zaporedje; ce je letalnik v zraku, sprozi pristanek.

    Namenoma **brez** zahteve po potrditvi: prekinitev mora biti en klik.
    Z ``force=true`` takoj odklene UI tudi, ce je ozadnja nit obvisela
    (vsili ABORTED + LAND / force-disarm).
    """
    body = _body(request)
    force = bool(body.get("force"))
    reason = str(body.get("reason") or (
        "force cancel" if force else "prekinil uporabnik"))
    result = abort_test(reason, force=force)
    return JsonResponse(result, status=200 if result.get("ok") else 400)


# ---------------------------------------------------------------------------
# Preklop Wi-Fi omrežja prek fizičnega stikala (GPIO19 -> GND)
# ---------------------------------------------------------------------------
# Ta streznik sam ne odloca o preklopu --- to pocne samostojni demon
# (scripts/wifi_gpio_switch.py), ki bere GPIO in poganja stroj stanj iz
# services/network_switch.py. Ta koncisca so samo okno vanj: preberejo
# stanje, ki ga je demon zapisal, in po potrebi zapisejo zahtevo, da naj
# odlozen preklop izvede takoj.
@require_GET
def network_status_api(request: HttpRequest) -> JsonResponse:
    """Trenutno stanje omrezja in morebiten odlozen preklop.

    Vsak klic hkrati sluzi kot "utrip": demon iz njegove sveznosti sklepa,
    ali nekdo trenutno gleda vmesnik, in glede na to odlozi preklop ali
    ga izvede takoj. Zato se ta GET namenoma dotakne datoteke na disku ---
    stranski ucinek je tu del nacrta, ne napaka.
    """
    runtime_dir = settings.UAV_RUNTIME_DIR
    try:
        touch_heartbeat(runtime_dir)
    except OSError:
        # Mapa run/ je lahko root-only (star demon) --- stanje vseeno vrnemo.
        pass
    state = read_state(runtime_dir)
    if state is None:
        return JsonResponse({
            "available": False,
            "error": "Demon za preklop WiFi ne tece (stikalo ni namesceno "
                     "ali servis ni zagnan).",
        })
    return JsonResponse({"available": True, **state})


@require_POST
@control_required
def network_force_switch_api(request: HttpRequest) -> JsonResponse:
    """Uporabnik zahteva takojsnjo izvedbo odlozenega preklopa.

    Namenoma **brez** ``require_confirm``, iz istega razloga kot prekinitev
    testnega poleta: to je pospesitev necesa, kar se bo tako ali tako zgodilo
    samo od sebe, ne nova nevarna akcija --- dodatna potrditev bi samo
    upocasnila edini gumb, ki obstaja prav zato, da je hiter.
    """
    request_force(settings.UAV_RUNTIME_DIR)
    return JsonResponse({"ok": True})
