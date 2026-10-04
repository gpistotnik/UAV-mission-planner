"""Unit testi za metrike poletne analize.

Sinteticni podatki z znanim odgovorom: ce dron leti vzporedno z nacrtovano
crto 2 m stran, mora biti RMS odstopanja 2 m. Brez takega testa je celotna
tabela v poglavju Rezultati nepreverjena.
"""
from __future__ import annotations

import json
import math

import pytest

from analysis.metrics import (
    LocalPlane, TrackPoint, altitude_stats, armed_intervals, capture_accuracy,
    cross_track_errors, cross_track_stats, describe, extract_track,
    filter_airborne, flown_length_m, haversine_m, load_jsonl,
    mission_completion, planned_length_m, planned_points_from_plan,
    point_segment_distance, telemetry_rate_stats,
)

LAT0, LON0 = 46.05, 14.50


# ---------------------------------------------------------------------------
# Projekcija in geometrija
# ---------------------------------------------------------------------------
def test_local_plane_roundtrip() -> None:
    plane = LocalPlane(LAT0, LON0)
    lat, lon = plane.to_latlon(*plane.to_xy(46.0512, 14.5031))
    assert lat == pytest.approx(46.0512, abs=1e-9)
    assert lon == pytest.approx(14.5031, abs=1e-9)


def test_local_plane_matches_haversine_over_short_distance() -> None:
    plane = LocalPlane(LAT0, LON0)
    x, y = plane.to_xy(LAT0 + 0.0009, LON0)  # ~100 m proti severu
    d_plane = math.hypot(x, y)
    d_hav = haversine_m(LAT0, LON0, LAT0 + 0.0009, LON0)
    assert d_plane == pytest.approx(d_hav, rel=1e-4)


def test_point_segment_distance_perpendicular_and_endpoints() -> None:
    # Pravokotna razdalja do daljice na x osi.
    assert point_segment_distance(5, 3, 0, 0, 10, 0) == pytest.approx(3.0)
    # Za tocko izven razpona daljice velja razdalja do krajisca, ne do premice.
    assert point_segment_distance(-4, 3, 0, 0, 10, 0) == pytest.approx(5.0)
    # Degenerirana daljica (dve enaki tocki).
    assert point_segment_distance(3, 4, 1, 1, 1, 1) == pytest.approx(
        math.hypot(2, 3))


# ---------------------------------------------------------------------------
# Natancnost sledenja
# ---------------------------------------------------------------------------
def _offset_track(offset_m: float, n: int = 20) -> list[TrackPoint]:
    """Sled, vzporedna z nacrtovano crto, zamaknjena proti vzhodu."""
    plane = LocalPlane(LAT0, LON0)
    pts = []
    for i in range(n):
        y = i * 5.0
        lat, lon = plane.to_latlon(offset_m, y)
        pts.append(TrackPoint(t=float(i), lat=lat, lon=lon, alt_rel_m=50.0))
    return pts


def _straight_plan(length_m: float = 100.0) -> list[tuple[float, float]]:
    plane = LocalPlane(LAT0, LON0)
    return [plane.to_latlon(0.0, 0.0), plane.to_latlon(0.0, length_m)]


def test_cross_track_error_equals_known_offset() -> None:
    errs = cross_track_errors(_offset_track(2.0), _straight_plan())
    assert all(e == pytest.approx(2.0, abs=0.01) for e in errs)


def test_cross_track_rms_of_constant_offset_is_the_offset() -> None:
    stats = cross_track_stats(_offset_track(3.0), _straight_plan())
    assert stats["rms"] == pytest.approx(3.0, abs=0.02)
    assert stats["mean"] == pytest.approx(3.0, abs=0.02)
    assert stats["n"] == 20


def test_perfect_tracking_gives_zero_error() -> None:
    stats = cross_track_stats(_offset_track(0.0), _straight_plan())
    assert stats["rms"] == pytest.approx(0.0, abs=1e-6)


def test_cross_track_returns_empty_without_plan() -> None:
    assert cross_track_errors(_offset_track(1.0), []) == []
    assert cross_track_stats(_offset_track(1.0), [])["n"] == 0


# ---------------------------------------------------------------------------
# Statistika
# ---------------------------------------------------------------------------
def test_describe_known_series() -> None:
    d = describe([1.0, 2.0, 3.0, 4.0])
    assert d["n"] == 4
    assert d["mean"] == pytest.approx(2.5)
    # describe() zaokrozi na 3 decimalke, zato primerjamo s toleranco.
    assert d["rms"] == pytest.approx(math.sqrt((1 + 4 + 9 + 16) / 4), abs=1e-3)
    assert d["max"] == pytest.approx(4.0)
    assert d["p50"] == pytest.approx(2.5)


def test_describe_empty_series() -> None:
    assert describe([]) == {"n": 0}


# ---------------------------------------------------------------------------
# Branje zapisa
# ---------------------------------------------------------------------------
def test_extract_track_scales_from_e7() -> None:
    rows = [
        {"t": 1.0, "type": "GLOBAL_POSITION_INT",
         "lat": 460500000, "lon": 145000000, "relative_alt": 50000, "alt": 350000},
        {"t": 2.0, "type": "ATTITUDE", "roll": 0.0},
    ]
    track = extract_track(rows)
    assert len(track) == 1
    assert track[0].lat == pytest.approx(46.05)
    assert track[0].alt_rel_m == pytest.approx(50.0)
    assert track[0].alt_msl_m == pytest.approx(350.0)


def test_load_jsonl_skips_corrupt_lines(tmp_path) -> None:
    p = tmp_path / "t.jsonl"
    p.write_text('{"a": 1}\nto ni json\n{"b": 2}\n', encoding="utf-8")
    assert load_jsonl(p) == [{"a": 1}, {"b": 2}]


def test_load_jsonl_missing_file_is_empty() -> None:
    assert load_jsonl("/ni/take/datoteke.jsonl") == []


def test_armed_intervals_from_heartbeats() -> None:
    rows = [
        {"t": 10.0, "type": "HEARTBEAT", "base_mode": 0},
        {"t": 20.0, "type": "HEARTBEAT", "base_mode": 128},
        {"t": 30.0, "type": "HEARTBEAT", "base_mode": 128},
        {"t": 40.0, "type": "HEARTBEAT", "base_mode": 0},
    ]
    assert armed_intervals(rows) == [(20.0, 40.0)]


def test_armed_interval_left_open_closes_at_last_message() -> None:
    rows = [
        {"t": 5.0, "type": "HEARTBEAT", "base_mode": 128},
        {"t": 9.0, "type": "GLOBAL_POSITION_INT", "lat": 0, "lon": 0},
    ]
    assert armed_intervals(rows) == [(5.0, 9.0)]


def test_filter_airborne_drops_ground_points() -> None:
    track = [
        TrackPoint(t=1.0, lat=LAT0, lon=LON0, alt_rel_m=0.2),
        TrackPoint(t=2.0, lat=LAT0, lon=LON0, alt_rel_m=30.0),
        TrackPoint(t=99.0, lat=LAT0, lon=LON0, alt_rel_m=30.0),
    ]
    kept = filter_airborne(track, [(0.0, 50.0)])
    assert [p.t for p in kept] == [2.0]


def test_telemetry_rate_stats_computes_hz() -> None:
    rows = [{"t": i * 0.2, "type": "GLOBAL_POSITION_INT"} for i in range(11)]
    stats = telemetry_rate_stats(rows)
    assert stats["mean"] == pytest.approx(200.0, abs=1e-6)
    assert stats["hz"] == pytest.approx(5.0, abs=0.01)


def test_mission_completion_counts_unique_reached() -> None:
    rows = [
        {"t": 1.0, "type": "MISSION_ITEM_REACHED", "seq": 1},
        {"t": 2.0, "type": "MISSION_ITEM_REACHED", "seq": 2},
        {"t": 3.0, "type": "MISSION_ITEM_REACHED", "seq": 2},
        {"t": 4.0, "type": "STATUSTEXT", "severity": 2, "text": "PreArm: GPS"},
    ]
    res = mission_completion(rows, item_count=5)
    assert res["reached_count"] == 2
    assert res["max_seq_reached"] == 2
    assert res["error_count"] == 1
    assert res["completion_pct"] == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# Tocnost zajema
# ---------------------------------------------------------------------------
def test_capture_accuracy_measures_distance_to_nearest_planned() -> None:
    plane = LocalPlane(LAT0, LON0)
    planned = [plane.to_latlon(0.0, 0.0), plane.to_latlon(0.0, 50.0)]
    off_lat, off_lon = plane.to_latlon(3.0, 0.0)  # 3 m stran od prve tocke
    captures = [
        {"lat": off_lat, "lon": off_lon, "captured": True,
         "trigger_to_capture_ms": 120.0},
        {"lat": None, "lon": None, "captured": False},
    ]
    res = capture_accuracy(captures, planned)
    assert res["captures"] == 2
    assert res["captured_files"] == 1
    assert res["without_position"] == 1
    assert res["distance_to_planned"]["max"] == pytest.approx(3.0, abs=0.02)
    assert res["trigger_to_capture_ms"]["mean"] == pytest.approx(120.0)


def test_capture_accuracy_without_captures() -> None:
    assert capture_accuracy([], [(LAT0, LON0)]) == {"n": 0}


# ---------------------------------------------------------------------------
# Nacrt
# ---------------------------------------------------------------------------
def test_planned_points_prefers_points_list() -> None:
    plan = {"points": [{"lat": 1.0, "lon": 2.0}, {"lat": 3.0, "lon": 4.0}]}
    assert planned_points_from_plan(plan) == [(1.0, 2.0), (3.0, 4.0)]


def test_planned_points_falls_back_to_items() -> None:
    plan = {"items": [
        {"command_name": "NAV_WAYPOINT", "lat": 46.05, "lon": 14.50},
        {"command_name": "NAV_TAKEOFF", "lat": 0.0, "lon": 0.0},
        {"command_name": "DO_CHANGE_SPEED", "lat": 0.0, "lon": 0.0},
        {"command_name": "NAV_WAYPOINT", "lat": 46.06, "lon": 14.51},
    ]}
    assert planned_points_from_plan(plan) == [(46.05, 14.50), (46.06, 14.51)]


def test_path_lengths() -> None:
    plane = LocalPlane(LAT0, LON0)
    planned = [plane.to_latlon(0.0, 0.0), plane.to_latlon(0.0, 100.0)]
    assert planned_length_m(planned) == pytest.approx(100.0, rel=1e-3)
    track = [TrackPoint(t=0.0, lat=planned[0][0], lon=planned[0][1]),
             TrackPoint(t=1.0, lat=planned[1][0], lon=planned[1][1])]
    assert flown_length_m(track) == pytest.approx(100.0, rel=1e-3)


# ---------------------------------------------------------------------------
# Okno izvajanja misije
# ---------------------------------------------------------------------------
def test_mission_window_spans_first_to_last_reached() -> None:
    from analysis.metrics import mission_window
    rows = [
        {"t": 5.0, "type": "HEARTBEAT", "base_mode": 128},
        {"t": 12.0, "type": "MISSION_ITEM_REACHED", "seq": 2},
        {"t": 30.0, "type": "MISSION_ITEM_REACHED", "seq": 3},
        {"t": 48.0, "type": "MISSION_ITEM_REACHED", "seq": 4},
        {"t": 60.0, "type": "HEARTBEAT", "base_mode": 0},
    ]
    assert mission_window(rows) == (12.0, 48.0)


def test_mission_window_none_when_mission_not_executed() -> None:
    from analysis.metrics import mission_window
    assert mission_window([{"t": 1.0, "type": "HEARTBEAT", "base_mode": 0}]) is None
    assert mission_window(
        [{"t": 1.0, "type": "MISSION_ITEM_REACHED", "seq": 1}]) is None


def test_filter_window_keeps_only_points_inside() -> None:
    from analysis.metrics import filter_window
    track = [TrackPoint(t=float(i), lat=LAT0, lon=LON0) for i in range(10)]
    kept = filter_window(track, (3.0, 6.0))
    assert [p.t for p in kept] == [3.0, 4.0, 5.0, 6.0]


def test_filter_window_without_window_keeps_all() -> None:
    from analysis.metrics import filter_window
    track = [TrackPoint(t=float(i), lat=LAT0, lon=LON0) for i in range(4)]
    assert len(filter_window(track, None)) == 4


def test_window_excludes_takeoff_from_altitude_metric() -> None:
    """Regresija: vzletna rampa je prej napihovala odstopanje visine."""
    from analysis.metrics import filter_window
    ramp = [TrackPoint(t=float(i), lat=LAT0, lon=LON0, alt_rel_m=i * 5.0)
            for i in range(11)]          # 0 -> 50 m
    cruise = [TrackPoint(t=20.0 + i, lat=LAT0, lon=LON0, alt_rel_m=50.0)
              for i in range(10)]
    full = altitude_stats(ramp + cruise, 50.0)
    windowed = altitude_stats(filter_window(ramp + cruise, (20.0, 30.0)), 50.0)
    assert full["rms"] > 10.0
    assert windowed["rms"] == pytest.approx(0.0, abs=1e-9)
