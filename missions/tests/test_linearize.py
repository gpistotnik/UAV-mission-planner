"""Testi linearizacije misije v zaporedje letalnih tock.

Linearizacija je lepilo med podatkovnim modelom in krmilnikom: ce se tu
zmoti, se napaka pokaze v zraku, ne v vmesniku.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from missions.models import (
    ActionType, DroneProfile, ElementType, MapPattern, Mission, MissionElement,
)
from missions.services.linearize import linearize_mission

pytestmark = pytest.mark.django_db

# Kvadrat priblizno 200 x 200 m pri Ljubljani.
SQUARE = {
    "type": "Polygon",
    "coordinates": [[
        [14.5000, 46.0500], [14.5026, 46.0500],
        [14.5026, 46.0518], [14.5000, 46.0518], [14.5000, 46.0500],
    ]],
}


@pytest.fixture
def drone() -> DroneProfile:
    return DroneProfile.objects.create(
        slug="f450-test", display_name="F450 test",
        sensor_width_mm=Decimal("6.287"), sensor_height_mm=Decimal("4.712"),
        focal_length_mm=Decimal("6.000"),
        image_width_px=4056, image_height_px=3040,
    )


@pytest.fixture
def mission(drone: DroneProfile) -> Mission:
    return Mission.objects.create(
        name="Test misija", drone=drone,
        home_lat=Decimal("46.0500000"), home_lon=Decimal("14.5000000"),
        default_altitude_m=Decimal("50"), default_speed_ms=Decimal("5"),
    )


def _wp(mission: Mission, order: int, lat: str, lon: str, **kw) -> MissionElement:
    return MissionElement.objects.create(
        mission=mission, order=order, element_type=ElementType.WAYPOINT,
        altitude_m=Decimal(kw.pop("altitude_m", "50")),
        lat=Decimal(lat), lon=Decimal(lon), **kw,
    )


def _map(mission: Mission, order: int, **kw) -> MissionElement:
    return MissionElement.objects.create(
        mission=mission, order=order, element_type=ElementType.MAP,
        altitude_m=Decimal(kw.pop("altitude_m", "60")),
        polygon_geojson=kw.pop("polygon", SQUARE), **kw,
    )


def test_empty_mission_gives_no_points(mission: Mission) -> None:
    assert linearize_mission(mission) == []


def test_waypoints_keep_order_and_values(mission: Mission) -> None:
    _wp(mission, 1, "46.0510000", "14.5010000", altitude_m="30",
        hover_time_s=Decimal("2.5"), action_type=ActionType.PHOTO)
    _wp(mission, 2, "46.0520000", "14.5020000")
    pts = linearize_mission(mission)
    assert [p.order for p in pts] == [1, 2]
    assert pts[0].lat == pytest.approx(46.0510)
    assert pts[0].altitude_m == pytest.approx(30.0)
    assert pts[0].hover_time_s == pytest.approx(2.5)
    assert pts[0].action_type == "PHOTO"
    assert all(p.source == "WP" for p in pts)


def test_waypoint_without_coordinates_is_skipped(mission: Mission) -> None:
    MissionElement.objects.create(
        mission=mission, order=1, element_type=ElementType.WAYPOINT,
        altitude_m=Decimal("50"), lat=None, lon=None)
    _wp(mission, 2, "46.0510000", "14.5010000")
    pts = linearize_mission(mission)
    assert len(pts) == 1


def test_map_zone_expands_to_multiple_points(mission: Mission) -> None:
    _map(mission, 1)
    pts = linearize_mission(mission)
    assert len(pts) > 4
    assert all(p.source == "MAP" for p in pts)
    assert all(p.map_block is not None for p in pts)
    assert all(p.action_type == "PHOTO" for p in pts)
    assert all(p.trigger_distance_m and p.trigger_distance_m > 0 for p in pts)


def test_crosshatch_produces_more_points_than_grid(mission: Mission) -> None:
    _map(mission, 1, pattern=MapPattern.GRID)
    grid_count = len(linearize_mission(mission))

    mission.elements.all().delete()
    _map(mission, 1, pattern=MapPattern.CROSSHATCH)
    cross_count = len(linearize_mission(mission))

    assert cross_count > grid_count


def test_map_without_polygon_is_skipped(mission: Mission) -> None:
    MissionElement.objects.create(
        mission=mission, order=1, element_type=ElementType.MAP,
        altitude_m=Decimal("60"), polygon_geojson=None)
    assert linearize_mission(mission) == []


def test_invalid_polygon_does_not_raise(mission: Mission) -> None:
    """Degeneriran poligon (tri kolinearne tocke) mora biti tiho preskocen."""
    MissionElement.objects.create(
        mission=mission, order=1, element_type=ElementType.MAP,
        altitude_m=Decimal("60"),
        polygon_geojson={"type": "Polygon", "coordinates": [[
            [14.50, 46.05], [14.50, 46.05], [14.50, 46.05], [14.50, 46.05]]]})
    assert linearize_mission(mission) == []


def test_mixed_elements_are_ordered_and_numbered_continuously(
    mission: Mission,
) -> None:
    _wp(mission, 1, "46.0490000", "14.4990000")
    _map(mission, 2)
    _wp(mission, 3, "46.0530000", "14.5030000")
    pts = linearize_mission(mission)

    # Zaporedne stevilke brez luknje.
    assert [p.order for p in pts] == list(range(1, len(pts) + 1))
    # Prva in zadnja sta rocna waypointa, vmes je mapping blok.
    assert pts[0].source == "WP"
    assert pts[-1].source == "WP"
    assert any(p.source == "MAP" for p in pts[1:-1])
