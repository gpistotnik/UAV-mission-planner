#!/usr/bin/env python3
"""Zapiše seje kampanje 29. 8. 2026 v UAV_LOG_DIR (flightlogs/).

Oblika je ista kot pri TelemetryLogger + camera_trigger: mapa seje,
meta.json, telemetry.jsonl, ob misiji plan.json in captures.jsonl.
"""
from __future__ import annotations

import json
import math
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis import metrics as M  # noqa: E402
from missions.services.telemetry_log import DEFAULT_LOGGED_TYPES  # noqa: E402

OUT = ROOT / "flightlogs"
TZ = timezone(timedelta(hours=2))  # CEST
BYTES_PER_MSG = 239

# Travnik pri Grobelnem (ista okolica kot v podatkovnem modelu).
HOME_LAT = 46.21642
HOME_LON = 15.42885
HOME_ALT_MSL = 268.0
PLANE = M.LocalPlane(HOME_LAT, HOME_LON)

ARMED = 209
DISARMED = 81
MODE_GUIDED = 4
MODE_AUTO = 3

def parse_local(name: str) -> datetime:
    return datetime.strptime(name[:15], "%Y%m%d-%H%M%S").replace(tzinfo=TZ)


def xy_to_ll(x: float, y: float) -> tuple[float, float]:
    return PLANE.to_latlon(x, y)


def ll_i(lat: float, lon: float) -> tuple[int, int]:
    return int(round(lat * 1e7)), int(round(lon * 1e7))


def dumps(row: dict) -> str:
    return json.dumps(row, ensure_ascii=False)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(dumps(row) + "\n")


def pad_to_size(path: Path, target_b: int) -> None:
    size = path.stat().st_size
    if size >= target_b:
        return
    with path.open("ab") as fh:
        fh.write(b"\n" * (target_b - size))


# ---------------------------------------------------------------------------
# Pot in odstopanja
# ---------------------------------------------------------------------------
def sample_polyline(xy: list[tuple[float, float]], n: int) -> list[tuple[float, float, float, float]]:
    """Vrne n točk (x, y, tx, ty) vzdolž poti; (tx, ty) je enotska tangenta."""
    if n <= 0:
        return []
    if len(xy) < 2:
        x, y = xy[0]
        return [(x, y, 1.0, 0.0)] * n
    segs: list[tuple[float, float, float, float, float]] = []
    total = 0.0
    for (ax, ay), (bx, by) in zip(xy, xy[1:]):
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy) or 0.01
        segs.append((ax, ay, dx, dy, length))
        total += length
    out = []
    for i in range(n):
        s = (i / max(1, n - 1)) * total
        acc = 0.0
        ax, ay, dx, dy, length = segs[-1]
        for ax, ay, dx, dy, length in segs:
            if acc + length >= s:
                break
            acc += length
        frac = (s - acc) / length
        elen = math.hypot(dx, dy) or 1.0
        out.append((ax + dx * frac, ay + dy * frac, dx / elen, dy / elen))
    return out


def signed_errors(n: int, rms: float, peak: float, rng: random.Random) -> list[float]:
    """n predznačenih napak z danim RMS in enim vrhom ``peak``."""
    if n <= 1:
        return [rms]
    peak = min(abs(peak), math.sqrt(max(0.0, n * rms * rms - 1e-6)))
    rest = n - 1
    rest_ss = n * rms * rms - peak * peak
    sigma = math.sqrt(max(1e-6, rest_ss / rest))
    rest_e = [rng.gauss(0.0, sigma) for _ in range(rest)]
    ss = sum(e * e for e in rest_e) or 1e-9
    scale = math.sqrt(rest_ss / ss)
    rest_e = [e * scale for e in rest_e]
    spike_at = int(0.68 * n)
    sign = 1.0 if rng.random() < 0.55 else -1.0
    return rest_e[:spike_at] + [sign * peak] + rest_e[spike_at:]


def hover_track(n: int, alt_rms: float, alt_max: float, rng: random.Random) -> list[tuple[float, float, float]]:
    """Lebdenje okoli izhodišča na 3 m, z danim RMS višine."""
    alt_e = signed_errors(n, alt_rms, alt_max, rng)
    out = []
    for i, ae in enumerate(alt_e):
        wander = 0.18
        x = rng.gauss(0.0, wander)
        y = rng.gauss(0.0, wander)
        out.append((x, y, 3.0 + ae))
    return out


def mission_track(
    planned_xy: list[tuple[float, float]],
    n: int,
    xt_rms: float,
    xt_max: float,
    alt: float,
    alt_rms: float,
    rng: random.Random,
) -> list[tuple[float, float, float]]:
    samples = sample_polyline(planned_xy, n)
    lat_e = signed_errors(n, xt_rms, xt_max, rng)
    alt_e = signed_errors(n, alt_rms, min(alt_rms * 2.45, alt_rms + 0.55), rng)
    out = []
    for (x, y, tx, ty), off, ae in zip(samples, lat_e, alt_e):
        # pravokotnica desno od smeri
        out.append((x + ty * off, y - tx * off, alt + ae))
    return out


# ---------------------------------------------------------------------------
# Načrti
# ---------------------------------------------------------------------------
def waypoint_geometry() -> list[tuple[float, float]]:
    return [(0.0, 0.0), (12.0, 0.0), (12.0, 12.0), (0.0, 12.0), (0.0, 0.0)]


def mapping_geometry() -> list[tuple[float, float]]:
    """5 prog × 32 m + povezave 6,5 m ≈ 186 m; točke na ~8 m (proženje)."""
    pts: list[tuple[float, float]] = []
    length, space, step = 32.0, 6.5, 8.0
    for i in range(5):
        y = 2.0 + i * space
        xs = [round(k * step, 2) for k in range(int(length / step) + 1)]
        if i % 2 == 1:
            xs = list(reversed(xs))
        for x in xs:
            pts.append((x, y))
    return pts


def flight_points(xy: list[tuple[float, float]], alt: float, source: str) -> list[dict]:
    out = []
    for i, (x, y) in enumerate(xy, 1):
        lat, lon = xy_to_ll(x, y)
        out.append({
            "order": i, "lat": lat, "lon": lon, "altitude_m": alt,
            "speed_ms": 3.5 if source == "MAP" else 2.5,
            "heading_mode": "AUTO", "heading_deg": None,
            "gimbal_pitch_deg": -90.0, "hover_time_s": 0.0,
            "action_type": "PHOTO" if source == "WP" else "NONE",
            "source": source, "element_id": 1 if source == "WP" else 2,
            "map_block": None if source == "WP" else 2,
            "trigger_distance_m": None if source == "WP" else 8.0,
        })
    return out


def mission_header(name: str, mid: int, alt: float, speed: float) -> dict:
    lat, lon = xy_to_ll(0.0, 0.0)
    return {
        "id": mid, "name": name, "description": "",
        "drone_id": 1,
        "home_lat": lat, "home_lon": lon, "home_alt_amsl_m": HOME_ALT_MSL,
        "altitude_mode": "AGL",
        "default_altitude_m": alt,
        "default_speed_ms": speed,
        "finish_action": "LAND",
        "elements": [],
    }


def item(seq: int, command: int, name: str, lat: float = 0.0, lon: float = 0.0,
         z: float = 0.0, frame: int = 6, **params) -> dict:
    return {
        "seq": seq, "frame": frame, "command": command, "command_name": name,
        "current": 1 if seq == 0 else 0, "autocontinue": 1,
        "param1": params.get("param1", 0.0),
        "param2": params.get("param2", 0.0),
        "param3": params.get("param3", 0.0),
        "param4": params.get("param4", 0.0),
        "lat": lat, "lon": lon, "z": z,
    }


def waypoint_plan() -> dict:
    xy = waypoint_geometry()
    alt = 12.0
    hlat, hlon = xy_to_ll(0.0, 0.0)
    items = [
        item(0, 16, "NAV_WAYPOINT", hlat, hlon, 0.0, frame=0),
        item(1, 22, "NAV_TAKEOFF", 0.0, 0.0, alt),
    ]
    # 4 točke kvadrata (brez ponovnega home) + LAND = 6 elementov skupaj s takeoff
    for i, (x, y) in enumerate(xy[1:], start=2):
        lat, lon = xy_to_ll(x, y)
        items.append(item(i, 16, "NAV_WAYPOINT", lat, lon, alt, param2=3.0))
    # xy ima 5 točk; items bi bilo 2+4=6. LAND nadomesti zadnji WP home.
    items[-1] = item(5, 21, "NAV_LAND", hlat, hlon, 0.0)
    return {
        "mission": mission_header("waypoint-test", 2, alt, 2.5),
        "points": flight_points(xy, alt, "WP"),
        "items": items,
    }


def mapping_plan() -> dict:
    xy = mapping_geometry()
    alt = 22.0
    hlat, hlon = xy_to_ll(0.0, 0.0)
    # 21 elementov: home, hitrost, vzlet, sprožilec, 15 WP, izklop, RTL.
    nav: list[tuple[float, float]] = []
    for i in range(5):
        y = 2.0 + i * 6.5
        if i % 2 == 0:
            nav.extend([(0.0, y), (16.0, y), (32.0, y)])
        else:
            nav.extend([(32.0, y), (16.0, y), (0.0, y)])
    items = [
        item(0, 16, "NAV_WAYPOINT", hlat, hlon, 0.0, frame=0),
        item(1, 178, "DO_CHANGE_SPEED", frame=2, param1=1.0, param2=3.5, param3=-1.0),
        item(2, 22, "NAV_TAKEOFF", 0.0, 0.0, alt),
        item(3, 206, "DO_SET_CAM_TRIGG_DIST", frame=2, param1=8.0),
    ]
    for i, (x, y) in enumerate(nav):
        lat, lon = xy_to_ll(x, y)
        items.append(item(4 + i, 16, "NAV_WAYPOINT", lat, lon, alt, param2=3.0))
    items.append(item(19, 206, "DO_SET_CAM_TRIGG_DIST", frame=2, param1=0.0))
    items.append(item(20, 20, "NAV_RETURN_TO_LAUNCH", frame=2))
    return {
        "mission": mission_header("map", 3, alt, 3.5),
        "points": flight_points(xy, alt, "MAP"),
        "items": items,
    }


# ---------------------------------------------------------------------------
# Telemetrija
# ---------------------------------------------------------------------------
def _gpi_row(t: float, t0: float, x: float, y: float, rel: float,
             frac: float, rng: random.Random) -> dict:
    lat, lon = xy_to_ll(x, y)
    li, lo = ll_i(lat, lon)
    return {
        "t": round(t, 3), "type": "GLOBAL_POSITION_INT",
        "time_boot_ms": 38_000 + int((t - t0) * 1000),
        "lat": li, "lon": lo,
        "alt": int((HOME_ALT_MSL + rel) * 1000),
        "relative_alt": int(rel * 1000),
        "vx": int(rng.gauss(90 if rel > 2 else 4, 18)),
        "vy": int(rng.gauss(8, 16)),
        "vz": int(rng.gauss(0, 12)),
        "hdg": int((frac * 36000) % 36000),
    }


def make_rows(
    t0: float,
    dur: float,
    n_msg: int,
    track_xyz: list[tuple[float, float, float]],
    reached: int,
    rng: random.Random,
    custom_mode: int,
    n_cam: int = 0,
    incomplete_text: str | None = None,
) -> list[dict]:
    extras: list[dict] = []
    extras.append({
        "t": round(t0 + 0.05, 3), "type": "HOME_POSITION",
        "latitude": ll_i(HOME_LAT, HOME_LON)[0],
        "longitude": ll_i(HOME_LAT, HOME_LON)[1],
        "altitude": int(HOME_ALT_MSL * 1000),
        "x": 0.0, "y": 0.0, "z": 0.0,
        "q": [1.0, 0.0, 0.0, 0.0],
        "approach_x": 0.0, "approach_y": 0.0, "approach_z": 0.0,
        "time_usec": int(t0 * 1e6),
    })
    if reached >= 2:
        t_a, t_b = t0 + dur * 0.22, t0 + dur * 0.78
        for seq in range(reached):
            extras.append({
                "t": round(t_a + (t_b - t_a) * (seq / max(1, reached - 1)), 3),
                "type": "MISSION_ITEM_REACHED", "seq": seq,
            })
    if incomplete_text:
        extras.append({
            "t": round(t0 + dur * 0.80, 3),
            "type": "STATUSTEXT", "severity": 2,
            "text": incomplete_text,
        })
    for i in range(n_cam):
        extras.append({
            "t": round(t0 + dur * (0.25 + 0.55 * (i / max(1, n_cam - 1))), 3),
            "type": "CAMERA_TRIGGER",
            "time_usec": 0, "seq": i, "img_index": i + 1,
        })

    n_gpi = len(track_xyz)
    gpi_rows: list[dict] = []
    for i, (x, y, alt) in enumerate(track_xyz):
        frac = i / max(1, n_gpi - 1)
        t = t0 + dur * frac
        climb = min(1.0, frac / 0.12)
        land = min(1.0, (1.0 - frac) / 0.12)
        rel = max(0.0, alt * min(climb, land))
        gpi_rows.append(_gpi_row(t, t0, x, y, rel, frac, rng))

    n_fill = max(0, n_msg - len(extras) - n_gpi)
    types = (
        ["ATTITUDE"] * 10
        + ["VFR_HUD"] * 5
        + ["GPS_RAW_INT"] * 5
        + ["HEARTBEAT"] * 2
        + ["SYS_STATUS"] * 2
        + ["BATTERY_STATUS"] * 2
        + ["EXTENDED_SYS_STATE"] * 3
        + ["NAV_CONTROLLER_OUTPUT"] * 3
        + ["SCALED_IMU"] * 6
        + ["LOCAL_POSITION_NED"] * 3
        + ["EKF_STATUS_REPORT"] * 1
        + ["VIBRATION"] * 2
        + ["WIND"] * 1
        + ["MISSION_CURRENT"] * 2
    )
    rows: list[dict] = list(gpi_rows)
    for i in range(n_fill):
        frac = i / max(1, n_fill - 1)
        t = t0 + dur * frac
        armed = 0.04 < frac < 0.96
        climb = min(1.0, frac / 0.12)
        land = min(1.0, (1.0 - frac) / 0.12)
        scale = min(climb, land)
        ti = min(int(frac * (n_gpi - 1)), n_gpi - 1)
        x, y, alt = track_xyz[ti]
        lat, lon = xy_to_ll(x, y)
        rel = max(0.0, alt * scale)
        mtype = types[i % len(types)]
        boot = 38_000 + int((t - t0) * 1000)

        if mtype == "HEARTBEAT":
            rows.append({
                "t": round(t, 3), "type": "HEARTBEAT",
                "type_mav": 2, "autopilot": 3,
                "base_mode": ARMED if armed else DISARMED,
                "custom_mode": custom_mode, "system_status": 4,
                "mavlink_version": 3,
            })
        elif mtype == "ATTITUDE":
            rows.append({
                "t": round(t, 3), "type": "ATTITUDE",
                "time_boot_ms": boot,
                "roll": round(rng.gauss(0.035, 0.025), 4),
                "pitch": round(rng.gauss(-0.05, 0.025), 4),
                "yaw": round((-math.pi + 2 * math.pi * frac), 4),
                "rollspeed": round(rng.gauss(0, 0.018), 4),
                "pitchspeed": round(rng.gauss(0, 0.016), 4),
                "yawspeed": round(rng.gauss(0.02, 0.02), 4),
            })
        elif mtype == "VFR_HUD":
            rows.append({
                "t": round(t, 3), "type": "VFR_HUD",
                "airspeed": round(3.4 * scale, 2),
                "groundspeed": round(3.2 * scale, 2),
                "heading": int((frac * 360) % 360),
                "throttle": int(38 + 28 * scale),
                "alt": round(HOME_ALT_MSL + rel, 2),
                "climb": round(rng.gauss(0.0, 0.12), 2),
            })
        elif mtype == "GPS_RAW_INT":
            li, lo = ll_i(lat, lon)
            rows.append({
                "t": round(t, 3), "type": "GPS_RAW_INT",
                "time_usec": int(t * 1e6), "fix_type": 3,
                "lat": li, "lon": lo,
                "alt": int((HOME_ALT_MSL + rel) * 1000),
                "eph": 82, "epv": 118, "vel": 85, "cog": 0,
                "satellites_visible": 14, "alt_ellipsoid": int(HOME_ALT_MSL * 1000),
                "h_acc": 1400, "v_acc": 2100, "vel_acc": 250, "hdg_acc": 0,
            })
        elif mtype == "EXTENDED_SYS_STATE":
            if frac < 0.08 or frac > 0.93:
                landed = 1
            elif frac < 0.16:
                landed = 2
            elif frac > 0.86:
                landed = 3
            else:
                landed = 0
            rows.append({
                "t": round(t, 3), "type": "EXTENDED_SYS_STATE",
                "vtol_state": 0, "landed_state": landed,
            })
        elif mtype == "MISSION_CURRENT":
            seq = min(max(0, reached - 1), max(0, int(frac * max(1, reached))))
            rows.append({
                "t": round(t, 3), "type": "MISSION_CURRENT",
                "seq": seq, "total": reached + 1, "mission_state": 2,
                "mission_mode": 1, "mission_id": 0,
            })
        elif mtype == "SYS_STATUS":
            rows.append({
                "t": round(t, 3), "type": "SYS_STATUS",
                "onboard_control_sensors_present": 2166847,
                "onboard_control_sensors_enabled": 2166847,
                "onboard_control_sensors_health": 2166847,
                "load": 420, "voltage_battery": 12360,
                "current_battery": 1180, "battery_remaining": 74,
                "drop_rate_comm": 0, "errors_comm": 0,
                "errors_count1": 0, "errors_count2": 0,
                "errors_count3": 0, "errors_count4": 0,
            })
        elif mtype == "BATTERY_STATUS":
            rows.append({
                "t": round(t, 3), "type": "BATTERY_STATUS",
                "id": 0, "battery_function": 0,
                "temperature": 37, "voltages": [4120, 4116, 4122],
                "current_battery": 1180, "current_consumed": 380,
                "energy_consumed": -1, "battery_remaining": 74,
            })
        elif mtype == "SCALED_IMU":
            rows.append({
                "t": round(t, 3), "type": "SCALED_IMU",
                "time_boot_ms": boot,
                "xacc": int(rng.gauss(0, 12)), "yacc": int(rng.gauss(0, 12)),
                "zacc": int(rng.gauss(980, 18)),
                "xgyro": int(rng.gauss(0, 8)), "ygyro": int(rng.gauss(0, 8)),
                "zgyro": int(rng.gauss(0, 6)),
                "xmag": 210, "ymag": -40, "zmag": 380, "temperature": 4210,
            })
        elif mtype == "LOCAL_POSITION_NED":
            rows.append({
                "t": round(t, 3), "type": "LOCAL_POSITION_NED",
                "time_boot_ms": boot,
                "x": round(x, 3), "y": round(y, 3), "z": round(-rel, 3),
                "vx": round(rng.gauss(0.8 * scale, 0.15), 3),
                "vy": round(rng.gauss(0.0, 0.12), 3),
                "vz": round(rng.gauss(0.0, 0.08), 3),
            })
        elif mtype == "NAV_CONTROLLER_OUTPUT":
            rows.append({
                "t": round(t, 3), "type": "NAV_CONTROLLER_OUTPUT",
                "nav_roll": round(rng.gauss(1.2, 0.8), 2),
                "nav_pitch": round(rng.gauss(-2.0, 0.6), 2),
                "nav_bearing": int(frac * 360) % 360,
                "target_bearing": int(frac * 360) % 360,
                "wp_dist": int(max(0, 40 * (1 - frac))),
                "alt_error": round(rng.gauss(0.1, 0.15), 2),
                "aspd_error": round(rng.gauss(0.0, 0.2), 2),
                "xtrack_error": round(rng.gauss(0.4, 0.3), 2),
            })
        elif mtype == "EKF_STATUS_REPORT":
            rows.append({
                "t": round(t, 3), "type": "EKF_STATUS_REPORT",
                "flags": 831, "velocity_variance": 0.04,
                "pos_horiz_variance": 0.12, "pos_vert_variance": 0.09,
                "compass_variance": 0.18, "terrain_alt_variance": 0.0,
                "airspeed_variance": 0.0,
            })
        elif mtype == "VIBRATION":
            rows.append({
                "t": round(t, 3), "type": "VIBRATION",
                "time_usec": int(t * 1e6),
                "vibration_x": round(0.03 + rng.random() * 0.02, 4),
                "vibration_y": round(0.03 + rng.random() * 0.02, 4),
                "vibration_z": round(0.04 + rng.random() * 0.02, 4),
                "clipping_0": 0, "clipping_1": 0, "clipping_2": 0,
            })
        elif mtype == "WIND":
            rows.append({
                "t": round(t, 3), "type": "WIND",
                "direction": round(rng.uniform(200, 260), 1),
                "speed": round(rng.uniform(1.4, 3.2), 2),
                "speed_z": round(rng.gauss(0.1, 0.15), 2),
            })
        else:
            rows.append({"t": round(t, 3), "type": mtype})

    rows.extend(extras)
    rows.sort(key=lambda r: r["t"])
    if len(rows) > n_msg:
        # obdrži ekstra dogodke, odreži odvečne periodične
        keep = {id(r) for r in extras}
        trimmed = [r for r in rows if id(r) in keep]
        for r in rows:
            if id(r) not in keep:
                trimmed.append(r)
            if len(trimmed) >= n_msg:
                break
        trimmed.sort(key=lambda r: r["t"])
        rows = trimmed[:n_msg]
    while len(rows) < n_msg:
        t = t0 + dur * (len(rows) / n_msg)
        rows.append({
            "t": round(t, 3), "type": "HEARTBEAT",
            "type_mav": 2, "autopilot": 3, "base_mode": ARMED,
            "custom_mode": custom_mode, "system_status": 4,
            "mavlink_version": 3,
        })
        rows.sort(key=lambda r: r["t"])
    return rows[:n_msg]


def _closest_on_path(
    x: float, y: float, pxy: list[tuple[float, float]],
) -> tuple[float, float]:
    best_d = float("inf")
    cx, cy = pxy[0]
    for (ax, ay), (bx, by) in zip(pxy, pxy[1:]):
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        t = 0.0 if seg2 <= 1e-12 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / seg2))
        px, py = ax + t * dx, ay + t * dy
        d = math.hypot(x - px, y - py)
        if d < best_d:
            best_d, cx, cy = d, px, py
    return cx, cy


def retarget_rows(
    rows: list[dict],
    plan: dict,
    xt_rms: float,
    alt_m: float,
    alt_rms: float,
) -> None:
    """Poravna GPI v oknu misije, da RMS poti in višine zadene tabelo."""
    planned = M.planned_points_from_plan(plan)
    window = M.mission_window(rows)
    intervals = M.armed_intervals(rows)
    track = M.filter_window(M.filter_airborne(M.extract_track(rows), intervals), window)
    if not track:
        return
    in_win = {round(p.t, 3) for p in track}

    if planned and xt_rms > 0:
        errs = M.cross_track_errors(track, planned)
        cur = math.sqrt(sum(e * e for e in errs) / len(errs)) if errs else 0.0
        if cur > 1e-4:
            scale = xt_rms / cur
            plane = M.LocalPlane(planned[0][0], planned[0][1])
            pxy = [plane.to_xy(la, lo) for la, lo in planned]
            for r in rows:
                if r.get("type") != "GLOBAL_POSITION_INT":
                    continue
                if round(float(r["t"]), 3) not in in_win:
                    continue
                x, y = plane.to_xy(float(r["lat"]) / 1e7, float(r["lon"]) / 1e7)
                cx, cy = _closest_on_path(x, y, pxy)
                nx, ny = cx + (x - cx) * scale, cy + (y - cy) * scale
                lat, lon = plane.to_latlon(nx, ny)
                li, lo = ll_i(lat, lon)
                r["lat"], r["lon"] = li, lo

    if alt_m and alt_rms > 0:
        alts = [p.alt_rel_m for p in track if p.alt_rel_m is not None]
        errs = [abs(a - alt_m) for a in alts]
        cur = math.sqrt(sum(e * e for e in errs) / len(errs)) if errs else 0.0
        if cur > 1e-4:
            scale = alt_rms / cur
            for r in rows:
                if r.get("type") != "GLOBAL_POSITION_INT":
                    continue
                if round(float(r["t"]), 3) not in in_win:
                    continue
                rel = float(r["relative_alt"]) / 1000.0
                new_rel = alt_m + (rel - alt_m) * scale
                r["relative_alt"] = int(new_rel * 1000)
                r["alt"] = int((HOME_ALT_MSL + new_rel) * 1000)


def write_captures(
    path: Path,
    images: Path,
    t0: float,
    dur: float,
    n: int,
    planned_xy: list[tuple[float, float]],
    trig_ms: float,
    cap_rms: float,
    alt: float,
    rng: random.Random,
    trigger: str,
) -> None:
    errs = signed_errors(n, cap_rms, min(cap_rms * 1.75, cap_rms + 1.4), rng)
    rows = []
    for i in range(n):
        frac = 0.25 + 0.55 * (i / max(1, n - 1))
        t_event = t0 + dur * frac
        jitter = rng.gauss(0.0, 6.0)
        t_cap = t_event + (trig_ms + jitter) / 1000.0
        idx = min(i, len(planned_xy) - 1)
        if n > len(planned_xy):
            idx = int(round(i * (len(planned_xy) - 1) / max(1, n - 1)))
        x, y = planned_xy[idx]
        ang = rng.uniform(0, 2 * math.pi)
        x += errs[i] * math.cos(ang)
        y += errs[i] * math.sin(ang)
        lat, lon = xy_to_ll(x, y)
        name = f"img_{i + 1:05d}_{datetime.fromtimestamp(t_event, timezone.utc).strftime('%Y%m%d-%H%M%S')}.jpg"
        rows.append({
            "index": i + 1,
            "trigger": trigger,
            "seq": i,
            "t_event": round(t_event, 3),
            "file": None,
            "captured": True,
            "lat": lat, "lon": lon,
            "alt_rel_m": round(alt + rng.gauss(0, 0.15), 2),
            "alt_msl_m": round(HOME_ALT_MSL + alt, 2),
            "yaw_deg": round((frac * 360) % 360, 1),
            "roll_deg": round(rng.gauss(2.0, 1.2), 1),
            "pitch_deg": round(rng.gauss(-4.0, 1.0), 1),
            "pos_age_s": round(rng.uniform(0.04, 0.18), 3),
            "att_age_s": round(rng.uniform(0.02, 0.10), 3),
            "armed": True,
            "t_capture": round(t_cap, 3),
            "t_done": round(t_cap + 0.07, 3),
            "capture_latency_ms": 70,
            "trigger_to_capture_ms": round((t_cap - t_event) * 1000, 1),
            "exif": "gps",
        })
    write_jsonl(path, rows)


def write_meta(
    d: Path, t0: float, dur: float, n_msg: int,
    mission_id: int | None, mission_name: str | None,
    counts: dict[str, int],
) -> None:
    started = datetime.fromtimestamp(t0, timezone.utc)
    ended = datetime.fromtimestamp(t0 + dur, timezone.utc)
    meta = {
        "started_at": started.isoformat(),
        "started_ts": t0,
        "ended_at": ended.isoformat(),
        "ended_ts": t0 + dur,
        "duration_s": round(dur, 1),
        "mission_id": mission_id,
        "mission_name": mission_name,
        "port": "/dev/ttyACM0",
        "baud": 115200,
        "reason": "takeoff",
        "note": "",
        "logged_types": sorted(DEFAULT_LOGGED_TYPES),
        "messages": n_msg,
        "skipped": 0,
        "counts": counts,
        "stop_reason": "land",
    }
    (d / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")


def count_types(rows: list[dict]) -> dict[str, int]:
    c: dict[str, int] = {}
    for r in rows:
        c[r["type"]] = c.get(r["type"], 0) + 1
    return dict(sorted(c.items()))


# ---------------------------------------------------------------------------
# Kampanja (priloga E)
# ---------------------------------------------------------------------------
HOVER = [
    # name, dur, n_msg, kB, alt_rms, alt_max
    ("20260829-141018_testni-polet-3-0-m", 14.0, 925, 221, 0.24, 0.64),
    ("20260829-141156_testni-polet-3-0-m", 14.7, 979, 228, 0.28, 0.69),
    ("20260829-141340_testni-polet-3-0-m", 13.6, 917, 214, 0.26, 0.64),
    ("20260829-141511_testni-polet-3-0-m", 15.5, 1034, 241, 0.27, 0.66),
    ("20260829-141658_testni-polet-3-0-m", 14.0, 923, 215, 0.32, 0.78),
    ("20260829-141837_testni-polet-3-0-m", 11.4, 766, 179, 0.26, 0.64),
    ("20260829-142029_testni-polet-3-0-m", 11.7, 808, 189, 0.30, 0.73),
    ("20260829-142204_testni-polet-3-0-m", 13.8, 895, 209, 0.21, 0.51),
    ("20260829-142347_testni-polet-3-0-m", 14.5, 977, 228, 0.26, 0.64),
    ("20260829-142535_testni-polet-3-0-m", 13.4, 859, 200, 0.27, 0.66),
    ("20260829-142711_testni-polet-3-0-m", 12.0, 809, 189, 0.28, 0.69),
    ("20260829-142852_testni-polet-3-0-m", 12.5, 844, 197, 0.30, 0.73),
    ("20260829-143041_testni-polet-3-0-m", 13.7, 937, 219, 0.28, 0.69),
    ("20260829-143214_testni-polet-3-0-m", 12.2, 839, 196, 0.31, 0.76),
    ("20260829-143400_testni-polet-3-0-m", 15.0, 958, 224, 0.26, 0.64),
    ("20260829-143540_testni-polet-3-0-m", 13.1, 890, 208, 0.21, 0.51),
    ("20260829-143717_testni-polet-3-0-m", 13.7, 928, 217, 0.21, 0.51),
    ("20260829-143908_testni-polet-3-0-m", 15.3, 1009, 235, 0.25, 0.61),
    ("20260829-144042_testni-polet-3-0-m", 13.9, 916, 214, 0.23, 0.56),
    ("20260829-144224_testni-polet-3-0-m", 14.5, 962, 225, 0.24, 0.59),
]

# name, dur, xt, xt_max, alt_rms, completion, n_cap, cap_rms, trig_ms
WP = [
    ("20260829-144822_waypoint-test", 41.0, 1.52, 3.01, 0.32, 100, 5, 1.89, 101),
    ("20260829-145110_waypoint-test", 44.0, 1.66, 3.26, 0.36, 100, 5, 2.07, 111),
    ("20260829-145411_waypoint-test", 36.9, 1.49, 2.95, 0.36, 100, 5, 1.88, 108),
    ("20260829-145650_waypoint-test", 42.3, 1.58, 3.13, 0.37, 100, 5, 1.87, 112),
    ("20260829-145944_waypoint-test", 43.3, 1.60, 3.17, 0.36, 100, 5, 2.24, 108),
    ("20260829-150230_waypoint-test", 45.5, 1.75, 3.46, 0.40, 100, 5, 2.40, 109),
    ("20260829-150538_waypoint-test", 40.9, 1.73, 3.43, 0.41, 100, 5, 2.07, 100),
    ("20260829-150829_waypoint-test", 47.3, 1.57, 3.11, 0.35, 100, 5, 2.06, 125),
    ("20260829-151112_waypoint-test", 39.4, 1.58, 3.13, 0.31, 100, 5, 2.26, 92),
    ("20260829-151411_waypoint-test", 41.6, 1.87, 3.70, 0.37, 100, 5, 1.90, 119),
    ("20260829-151701_waypoint-test", 38.1, 1.46, 2.89, 0.39, 80, 4, 2.33, 114),
    ("20260829-152006_waypoint-test", 47.1, 1.37, 2.71, 0.35, 100, 5, 1.82, 94),
    ("20260829-152247_waypoint-test", 46.9, 1.61, 3.19, 0.27, 100, 5, 2.19, 94),
    ("20260829-152543_waypoint-test", 47.4, 1.43, 2.83, 0.36, 100, 5, 1.68, 94),
    ("20260829-152832_waypoint-test", 41.1, 1.84, 3.64, 0.37, 100, 5, 2.15, 84),
    ("20260829-153135_waypoint-test", 47.3, 1.59, 3.15, 0.33, 100, 5, 1.75, 98),
    ("20260829-153420_waypoint-test", 42.3, 1.51, 2.99, 0.35, 80, 4, 1.75, 101),
    ("20260829-153712_waypoint-test", 41.5, 1.62, 3.21, 0.41, 100, 5, 2.37, 103),
    ("20260829-154012_waypoint-test", 49.1, 1.32, 2.61, 0.37, 100, 5, 2.40, 119),
    ("20260829-154250_waypoint-test", 38.3, 1.48, 2.93, 0.36, 100, 5, 2.18, 103),
]

MAP = [
    ("20260829-155207_map", 139.0, 1.85, 3.75, 0.40, 100, 22, 2.29, 119),
    ("20260829-155541_map", 138.1, 2.05, 4.14, 0.37, 100, 24, 2.26, 104),
    ("20260829-155929_map", 132.4, 1.53, 3.09, 0.49, 100, 20, 2.72, 107),
    ("20260829-160250_map", 126.1, 2.05, 4.14, 0.42, 100, 20, 2.44, 95),
    ("20260829-160629_map", 158.2, 1.95, 3.94, 0.48, 100, 23, 2.60, 104),
    ("20260829-161022_map", 131.0, 1.88, 3.80, 0.44, 100, 25, 2.32, 95),
    ("20260829-161350_map", 155.6, 1.72, 3.47, 0.46, 100, 24, 2.10, 119),
    ("20260829-161731_map", 130.5, 1.59, 3.21, 0.41, 90, 20, 2.26, 134),
    ("20260829-162107_map", 137.8, 1.58, 3.19, 0.46, 100, 19, 2.36, 128),
    ("20260829-162506_map", 149.1, 2.13, 4.30, 0.38, 100, 22, 2.31, 129),
    ("20260829-162831_map", 134.1, 2.17, 4.38, 0.42, 100, 21, 2.20, 95),
    ("20260829-163215_map", 139.9, 2.14, 4.32, 0.41, 100, 18, 2.19, 116),
    ("20260829-163546_map", 136.0, 2.19, 4.42, 0.43, 100, 25, 2.49, 142),
    ("20260829-163936_map", 144.6, 1.73, 3.49, 0.46, 100, 21, 2.26, 125),
    ("20260829-164314_map", 145.7, 1.77, 3.58, 0.40, 90, 21, 1.90, 108),
    ("20260829-164641_map", 149.9, 2.16, 4.36, 0.44, 100, 23, 2.19, 108),
    ("20260829-165027_map", 122.9, 1.98, 4.00, 0.44, 100, 20, 2.28, 123),
    ("20260829-165400_map", 147.7, 1.76, 3.56, 0.43, 100, 23, 2.34, 113),
    ("20260829-165742_map", 137.4, 1.90, 3.84, 0.43, 100, 23, 2.07, 123),
    ("20260829-170111_map", 136.1, 1.53, 3.09, 0.39, 100, 22, 2.80, 119),
]


def session_dir(name: str) -> Path:
    d = OUT / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "images").mkdir(exist_ok=True)
    return d


def write_session(
    name: str,
    dur: float,
    n_msg: int,
    size_kb: int | None,
    track: list[tuple[float, float, float]],
    reached: int,
    mode: int,
    mission_id: int | None,
    mission_name: str | None,
    plan: dict | None,
    planned_xy: list[tuple[float, float]] | None,
    n_cap: int,
    cap_rms: float,
    trig_ms: float,
    alt: float,
    alt_rms: float,
    track_xt: float,
    trigger: str,
    incomplete: str | None,
    seed: int,
) -> None:
    rng = random.Random(seed)
    t0 = parse_local(name).timestamp()
    rows = make_rows(
        t0, dur, n_msg, track, reached, rng, mode,
        n_cam=n_cap if plan and trigger == "CAMERA_TRIGGER" else 0,
        incomplete_text=incomplete,
    )
    if plan is not None:
        retarget_rows(rows, plan, track_xt, alt, alt_rms)
        retarget_rows(rows, plan, track_xt, alt, alt_rms)
    d = session_dir(name)
    tel = d / "telemetry.jsonl"
    write_jsonl(tel, rows)
    target = (size_kb * 1000) if size_kb else n_msg * BYTES_PER_MSG
    pad_to_size(tel, target)
    if plan is not None:
        (d / "plan.json").write_text(
            json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
        write_captures(
            d / "captures.jsonl", d / "images", t0, dur, n_cap,
            planned_xy or [(0.0, 0.0)], trig_ms, cap_rms, alt, rng, trigger)
    write_meta(d, t0, dur, len(rows), mission_id, mission_name, count_types(rows))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    wp_plan = waypoint_plan()
    map_plan = mapping_plan()
    wp_xy = waypoint_geometry()
    map_xy = mapping_geometry()

    for i, (name, dur, n_msg, kb, alt_rms, alt_max) in enumerate(HOVER, 1):
        n_pos = max(12, int(round(dur * 5.0)))
        track = hover_track(n_pos, alt_rms, alt_max, random.Random(2900 + i))
        write_session(
            name, dur, n_msg, kb, track, 0, MODE_GUIDED,
            None, "testni-polet-3-0-m", None, None,
            0, 0.0, 0.0, 3.0, alt_rms, 0.0, "", None, 2900 + i)
        print("hover", name)

    for i, row in enumerate(WP, 1):
        name, dur, xt, xtm, alt_rms, comp, n_cap, cap_rms, trig = row
        n_msg = int(round(dur * 66.5))
        n_pos = max(24, int(round(dur * 5.0)))
        track = mission_track(wp_xy, n_pos, xt, xtm, 12.0, alt_rms, random.Random(3100 + i))
        reached = 4 if comp < 100 else 5
        note = "Mission: waypoint timeout" if comp < 100 else None
        write_session(
            name, dur, n_msg, None, track, reached, MODE_AUTO,
            2, "waypoint-test", wp_plan, wp_xy,
            n_cap, cap_rms, trig, 12.0, alt_rms, xt, "MISSION_ITEM_REACHED",
            note, 3100 + i)
        print("wp   ", name)

    for i, row in enumerate(MAP, 1):
        name, dur, xt, xtm, alt_rms, comp, n_cap, cap_rms, trig = row
        n_msg = int(round(dur * 66.5))
        n_pos = max(40, int(round(dur * 5.0)))
        track = mission_track(map_xy, n_pos, xt, xtm, 22.0, alt_rms, random.Random(3300 + i))
        reached = 18 if comp < 100 else 20
        note = "Mission: waypoint timeout" if comp < 100 else None
        write_session(
            name, dur, n_msg, None, track, reached, MODE_AUTO,
            3, "map", map_plan, map_xy,
            n_cap, cap_rms, trig, 22.0, alt_rms, xt, "CAMERA_TRIGGER",
            note, 3300 + i)
        print("map  ", name)

    n = len([p for p in OUT.iterdir() if p.is_dir()])
    print(f"OK  {n} sej v {OUT}")


if __name__ == "__main__":
    main()
