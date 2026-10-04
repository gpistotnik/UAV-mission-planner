"""Unit testi za gradnik MAVLink misije.

Testiramo *zaporedje ukazov*, ne bajtov na vodilu --- gradnik je namenoma
locen od prenosa, zato ne potrebujemo ne pymavlinka ne strojne opreme.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import pytest

from missions.services.mavlink_mission import (
    MAV_CMD_DO_CHANGE_SPEED, MAV_CMD_DO_SET_CAM_TRIGG_DIST,
    MAV_CMD_IMAGE_START_CAPTURE, MAV_CMD_NAV_LAND,
    MAV_CMD_NAV_RETURN_TO_LAUNCH, MAV_CMD_NAV_TAKEOFF, MAV_CMD_NAV_WAYPOINT,
    MAV_FRAME_GLOBAL_RELATIVE_ALT_INT, MAX_MISSION_ITEMS,
    build_mission_items, describe_items,
)


@dataclass
class P:
    """Minimalen nadomestek FlightPoint (utestitveni dvojnik)."""
    lat: float
    lon: float
    altitude_m: float = 50.0
    speed_ms: Optional[float] = None
    heading_mode: str = "AUTO"
    heading_deg: Optional[float] = None
    hover_time_s: float = 0.0
    action_type: str = "NONE"
    source: str = "WP"
    map_block: Optional[int] = None
    trigger_distance_m: Optional[float] = None
    order: int = 1


def cmds(items) -> list[int]:
    return [it.command for it in items]


def test_empty_points_gives_no_items() -> None:
    assert build_mission_items([]).items == []


def test_sequence_has_home_speed_takeoff_waypoints_and_rtl() -> None:
    pts = [P(46.05, 14.50), P(46.051, 14.501)]
    res = build_mission_items(
        pts, default_speed_ms=6.0, takeoff_altitude_m=20.0, finish_action="RTH")
    assert cmds(res.items) == [
        MAV_CMD_NAV_WAYPOINT,            # seq 0: home
        MAV_CMD_DO_CHANGE_SPEED,
        MAV_CMD_NAV_TAKEOFF,
        MAV_CMD_NAV_WAYPOINT,
        MAV_CMD_NAV_WAYPOINT,
        MAV_CMD_NAV_RETURN_TO_LAUNCH,
    ]
    # Zaporedne stevilke morajo biti 0..N-1 brez luknje.
    assert [it.seq for it in res.items] == list(range(len(res.items)))
    # Samo prvi item je "current".
    assert [it.current for it in res.items] == [1] + [0] * (len(res.items) - 1)


def test_takeoff_uses_relative_frame_and_altitude() -> None:
    res = build_mission_items([P(46.05, 14.50, altitude_m=42.0)],
                              takeoff_altitude_m=15.0)
    takeoff = next(i for i in res.items if i.command == MAV_CMD_NAV_TAKEOFF)
    assert takeoff.frame == MAV_FRAME_GLOBAL_RELATIVE_ALT_INT
    assert takeoff.z == pytest.approx(15.0)


def test_takeoff_altitude_defaults_to_first_point() -> None:
    res = build_mission_items([P(46.05, 14.50, altitude_m=33.0)])
    takeoff = next(i for i in res.items if i.command == MAV_CMD_NAV_TAKEOFF)
    assert takeoff.z == pytest.approx(33.0)


def test_finish_action_land_lands_at_home() -> None:
    res = build_mission_items(
        [P(46.05, 14.50)], home_lat=46.10, home_lon=14.60,
        finish_action="LAND")
    last = res.items[-1]
    assert last.command == MAV_CMD_NAV_LAND
    assert last.lat == pytest.approx(46.10, abs=1e-7)
    assert last.lon == pytest.approx(14.60, abs=1e-7)


def test_finish_action_hover_adds_no_command_but_warns() -> None:
    res = build_mission_items([P(46.05, 14.50)], finish_action="HOVER")
    assert res.items[-1].command == MAV_CMD_NAV_WAYPOINT
    assert any("HOVER" in w for w in res.warnings)


def test_speed_change_emitted_only_when_speed_differs() -> None:
    pts = [P(46.05, 14.50, speed_ms=5.0),
           P(46.051, 14.50, speed_ms=5.0),
           P(46.052, 14.50, speed_ms=9.0)]
    res = build_mission_items(pts, default_speed_ms=5.0)
    speed_items = [i for i in res.items if i.command == MAV_CMD_DO_CHANGE_SPEED]
    # Ena za privzeto hitrost + ena za spremembo na 9 m/s.
    assert len(speed_items) == 2
    assert speed_items[0].param2 == pytest.approx(5.0)
    assert speed_items[1].param2 == pytest.approx(9.0)
    # param1 = 1 pomeni talno hitrost (ne zracno).
    assert all(i.param1 == pytest.approx(1.0) for i in speed_items)


def test_map_block_uses_trigger_distance_and_turns_it_off() -> None:
    pts = [
        P(46.05, 14.50, source="WP"),
        P(46.051, 14.50, source="MAP", map_block=7,
          trigger_distance_m=12.5, action_type="PHOTO"),
        P(46.052, 14.50, source="MAP", map_block=7,
          trigger_distance_m=12.5, action_type="PHOTO"),
        P(46.053, 14.50, source="WP"),
    ]
    res = build_mission_items(pts)
    trigg = [i for i in res.items if i.command == MAV_CMD_DO_SET_CAM_TRIGG_DIST]
    assert len(trigg) == 2, "pricakovan vklop na zacetku in izklop na koncu bloka"
    assert trigg[0].param1 == pytest.approx(12.5)
    assert trigg[1].param1 == pytest.approx(0.0)
    # Znotraj bloka NE sme biti posamicnih ukazov za slikanje --- to je
    # bistvo optimizacije (2 ukaza namesto N).
    assert MAV_CMD_IMAGE_START_CAPTURE not in cmds(res.items)


def test_trigger_distance_turned_off_at_mission_end() -> None:
    pts = [P(46.05, 14.50, source="MAP", map_block=1,
             trigger_distance_m=10.0, action_type="PHOTO")]
    res = build_mission_items(pts, finish_action="RTH")
    trigg = [i for i in res.items if i.command == MAV_CMD_DO_SET_CAM_TRIGG_DIST]
    assert [t.param1 for t in trigg] == pytest.approx([10.0, 0.0])


def test_waypoint_photo_action_emits_image_capture() -> None:
    res = build_mission_items([P(46.05, 14.50, action_type="PHOTO")])
    caps = [i for i in res.items if i.command == MAV_CMD_IMAGE_START_CAPTURE]
    assert len(caps) == 1
    assert caps[0].param3 == pytest.approx(1.0)


def test_burst_action_requests_multiple_frames() -> None:
    res = build_mission_items([P(46.05, 14.50, action_type="CAPTURE_BURST")])
    caps = [i for i in res.items if i.command == MAV_CMD_IMAGE_START_CAPTURE]
    assert caps[0].param3 > 1


def test_camera_disabled_suppresses_all_camera_commands() -> None:
    pts = [P(46.05, 14.50, action_type="PHOTO"),
           P(46.051, 14.50, source="MAP", map_block=3,
             trigger_distance_m=8.0, action_type="PHOTO")]
    res = build_mission_items(pts, camera_enabled=False)
    assert MAV_CMD_IMAGE_START_CAPTURE not in cmds(res.items)
    assert MAV_CMD_DO_SET_CAM_TRIGG_DIST not in cmds(res.items)


def test_fixed_heading_sets_yaw_auto_leaves_nan() -> None:
    res = build_mission_items([
        P(46.05, 14.50, heading_mode="FIXED", heading_deg=270.0),
        P(46.051, 14.50, heading_mode="AUTO"),
    ])
    wps = [i for i in res.items
           if i.command == MAV_CMD_NAV_WAYPOINT and i.seq > 0]
    assert wps[0].param4 == pytest.approx(270.0)
    assert math.isnan(wps[1].param4)


def test_hover_time_and_accept_radius_land_in_params() -> None:
    res = build_mission_items([P(46.05, 14.50, hover_time_s=4.0)],
                              accept_radius_m=2.5)
    wp = [i for i in res.items
          if i.command == MAV_CMD_NAV_WAYPOINT and i.seq > 0][0]
    assert wp.param1 == pytest.approx(4.0)
    assert wp.param2 == pytest.approx(2.5)


def test_coordinates_are_scaled_to_1e7_integers() -> None:
    res = build_mission_items([P(46.0569123, 14.5058456)])
    wp = [i for i in res.items
          if i.command == MAV_CMD_NAV_WAYPOINT and i.seq > 0][0]
    assert wp.x == 460569123
    assert wp.y == 145058456
    assert isinstance(wp.x, int) and isinstance(wp.y, int)


def test_warns_when_mission_exceeds_controller_capacity() -> None:
    pts = [P(46.0 + i * 1e-5, 14.5) for i in range(MAX_MISSION_ITEMS + 5)]
    res = build_mission_items(pts)
    assert any("presega" in w for w in res.warnings)


def test_describe_items_is_human_readable() -> None:
    res = build_mission_items([P(46.05, 14.50, action_type="PHOTO")],
                              default_speed_ms=5.0)
    text = describe_items(res.items)
    assert "NAV_TAKEOFF" in text and "DO_CHANGE_SPEED" in text
    assert len(text.splitlines()) == len(res.items) + 1  # + glava
