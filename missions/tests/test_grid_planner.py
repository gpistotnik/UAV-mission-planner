"""Unit testi za generator mapping mreže."""
from __future__ import annotations

import math

import pytest

from missions.services.grid_planner import (
    CameraSpec, GridParams, footprint_dimensions_m, gsd_cm_per_px,
    plan_grid_mission,
)

# DJI Mavic 3 (Hasselblad L2D-20c)
MAVIC3 = CameraSpec(
    sensor_width_mm=17.4, sensor_height_mm=13.0,
    focal_length_mm=12.29, image_width_px=5280, image_height_px=3956,
)


def test_gsd_formula_known_value() -> None:
    """GSD pri 60 m višine, Mavic 3:
    (17.4 × 60 × 100) / (12.29 × 5280) ≈ 1.609 cm/px."""
    g = gsd_cm_per_px(MAVIC3, altitude_m=60)
    assert math.isclose(g, 1.6092, rel_tol=1e-3)


def test_footprint_scales_linearly_with_altitude() -> None:
    fw100, fh100 = footprint_dimensions_m(MAVIC3, 100)
    fw50, fh50 = footprint_dimensions_m(MAVIC3, 50)
    assert math.isclose(fw100, 2 * fw50, rel_tol=1e-9)
    assert math.isclose(fh100, 2 * fh50, rel_tol=1e-9)


def test_grid_covers_simple_square() -> None:
    """Kvadrat 200×200 m: pričakovano je ≥2 progi pri 60 m AGL Mavic 3."""
    poly = {
        "type": "Polygon",
        "coordinates": [[
            [14.5000, 46.0500], [14.5026, 46.0500],
            [14.5026, 46.0518], [14.5000, 46.0518],
            [14.5000, 46.0500],
        ]],
    }
    params = GridParams(altitude_m=60, front_overlap_pct=80,
                        side_overlap_pct=70, track_angle_deg=0)
    plan = plan_grid_mission(poly, MAVIC3, params)
    assert plan.num_lines >= 2
    assert plan.total_distance_m > 100
    assert plan.estimated_images > 0
    # GSD pri 60 m mora ustrezati formuli
    assert math.isclose(plan.gsd_cm_per_px, 1.6092, rel_tol=1e-3)
    # Vse koordinate v ali blizu poligona
    for lon, lat in plan.waypoints:
        assert 14.499 < lon < 14.503
        assert 46.049 < lat < 46.052


def test_boustrophedon_alternates_direction() -> None:
    """Pri kvadratnem poligonu se mora smer prog zaporedno obračati."""
    poly = {
        "type": "Polygon",
        "coordinates": [[
            [14.5000, 46.0500], [14.5040, 46.0500],
            [14.5040, 46.0530], [14.5000, 46.0530],
            [14.5000, 46.0500],
        ]],
    }
    params = GridParams(altitude_m=80, front_overlap_pct=75,
                        side_overlap_pct=65, track_angle_deg=0)
    plan = plan_grid_mission(poly, MAVIC3, params)
    # waypoints prihajajo v parih (start, end). Smer = predznak razlike v dolžini.
    pairs = list(zip(plan.waypoints[::2], plan.waypoints[1::2]))
    directions = [b[0] - a[0] for a, b in pairs]
    # Zaporedne smeri morajo imeti različen predznak (boustrophedon).
    for d1, d2 in zip(directions, directions[1:]):
        assert d1 * d2 < 0, f"Pričakovan boustrophedon, dobil {directions}"


def test_invalid_overlap_raises() -> None:
    poly = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [0.001, 0], [0.001, 0.001], [0, 0.001], [0, 0]]],
    }
    with pytest.raises(ValueError):
        plan_grid_mission(poly, MAVIC3, GridParams(
            altitude_m=50, front_overlap_pct=100,
            side_overlap_pct=80, track_angle_deg=0,
        ))


def test_track_angle_rotates_lines() -> None:
    """Pri istem poligonu mora drugačen kot povzročiti drugačno število prog."""
    poly = {
        "type": "Polygon",
        "coordinates": [[
            [14.5000, 46.0500], [14.5050, 46.0500],
            [14.5050, 46.0510], [14.5000, 46.0510],
            [14.5000, 46.0500],
        ]],
    }
    p0 = plan_grid_mission(poly, MAVIC3, GridParams(
        altitude_m=60, front_overlap_pct=80, side_overlap_pct=70,
        track_angle_deg=0,
    ))
    p90 = plan_grid_mission(poly, MAVIC3, GridParams(
        altitude_m=60, front_overlap_pct=80, side_overlap_pct=70,
        track_angle_deg=90,
    ))
    # Pravokotni poligon → 0° in 90° vrnejo različno število prog.
    assert p0.num_lines != p90.num_lines
