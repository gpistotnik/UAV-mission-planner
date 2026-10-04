"""E2E testi HTTP sloja.

Ti testi so nastali kot posledica napake, ki jo enotni testi ciste logike
niso mogli ujeti: pogled za izvoz je klical nedefinirano spremenljivko in je
odpovedal ob vsaki zahtevi, medtem ko so vsi testi izvoznika prehajali. Zato
je odslej pokrit *vsak* pogled --- vsaj z enim klicem in preverjanjem
statusne kode.
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest
from django.contrib.auth.models import User

from core.storage import StorageUnavailableError
from missions.models import DroneProfile, ElementType, Mission, MissionElement

pytestmark = pytest.mark.django_db

SQUARE = {
    "type": "Polygon",
    "coordinates": [[
        [14.5000, 46.0500], [14.5026, 46.0500],
        [14.5026, 46.0518], [14.5000, 46.0518], [14.5000, 46.0500],
    ]],
}


@pytest.fixture(autouse=True)
def open_control(settings, tmp_path):
    """Privzeto za teste: kontrola odprta, logi in runtime v zacasni mapi."""
    settings.UAV_REQUIRE_AUTH = False
    settings.UAV_STORAGE_ROOT = ""
    settings.UAV_LOG_DIR = tmp_path / "flightlogs"
    settings.UAV_LOG_DIR.mkdir(parents=True, exist_ok=True)
    settings.UAV_RUNTIME_DIR = tmp_path / "run"
    return settings


@pytest.fixture
def drone() -> DroneProfile:
    return DroneProfile.objects.create(
        slug="f450", display_name="F450 + RPi",
        sensor_width_mm=Decimal("6.287"), sensor_height_mm=Decimal("4.712"),
        focal_length_mm=Decimal("6.000"),
        image_width_px=4056, image_height_px=3040,
    )


@pytest.fixture
def mission(drone: DroneProfile) -> Mission:
    m = Mission.objects.create(
        name="Testni let", drone=drone,
        home_lat=Decimal("46.0500000"), home_lon=Decimal("14.5000000"),
        default_altitude_m=Decimal("50"), default_speed_ms=Decimal("5"),
        finish_action="RTH",
    )
    MissionElement.objects.create(
        mission=m, order=1, element_type=ElementType.WAYPOINT,
        altitude_m=Decimal("40"), lat=Decimal("46.0510000"),
        lon=Decimal("14.5010000"), action_type="PHOTO")
    MissionElement.objects.create(
        mission=m, order=2, element_type=ElementType.MAP,
        altitude_m=Decimal("60"), polygon_geojson=SQUARE)
    return m


# ---------------------------------------------------------------------------
# HTML strani
# ---------------------------------------------------------------------------
def test_home_renders(client) -> None:
    """Glavna stran (/) je nadzorna plosca."""
    r = client.get("/")
    assert r.status_code == 200
    assert r.context["can_control"] is True
    assert "dashboard" in r.templates[0].name


def test_mission_list(client, mission: Mission) -> None:
    r = client.get("/misije/")
    assert r.status_code == 200
    assert "Testni let" in r.content.decode()


def test_mission_detail_shows_counts(client, mission: Mission) -> None:
    r = client.get(f"/misije/{mission.pk}/")
    assert r.status_code == 200
    # Podrobnost izracuna linearizacijo in stevilo MAVLink ukazov.
    assert r.context["point_count"] > 1
    assert r.context["item_count"] > r.context["point_count"]


def test_mission_detail_404_for_unknown(client) -> None:
    assert client.get("/misije/9999/").status_code == 404


def test_planner_new_and_edit(client, mission: Mission) -> None:
    assert client.get("/misije/nova/").status_code == 200
    r = client.get(f"/misije/{mission.pk}/urejanje/")
    assert r.status_code == 200
    assert json.loads(r.context["mission_json"])["id"] == mission.pk


def test_planner_ensures_default_f450_drone(client) -> None:
    DroneProfile.objects.all().delete()
    r = client.get("/misije/nova/")
    assert r.status_code == 200
    drone = DroneProfile.objects.get()
    assert drone.slug == "f450-pixhawk-rpi-imx477"
    payload = json.loads(r.context["mission_json"])
    assert payload["drone_id"] == drone.pk


def test_dashboard_renders(client, mission: Mission) -> None:
    r = client.get("/nadzor/")
    assert r.status_code == 200
    assert r.context["can_control"] is True


def test_dashboard_flags_login_when_auth_required(client, settings, mission) -> None:
    settings.UAV_REQUIRE_AUTH = True
    r = client.get("/nadzor/")
    assert r.context["can_control"] is False


def test_login_page_renders(client) -> None:
    assert client.get("/prijava/").status_code == 200


# ---------------------------------------------------------------------------
# API --- misije
# ---------------------------------------------------------------------------
def test_create_and_read_mission_roundtrip(client, drone: DroneProfile) -> None:
    payload = {
        "name": "Nova iz testa", "drone_id": drone.pk,
        "home_lat": 46.05, "home_lon": 14.50,
        "default_altitude_m": 45, "default_speed_ms": 4,
        "finish_action": "LAND",
        "elements": [
            {"element_type": "WP", "order": 1, "altitude_m": 45,
             "lat": 46.051, "lon": 14.501, "action_type": "PHOTO"},
            {"element_type": "MAP", "order": 2, "altitude_m": 60,
             "polygon_geojson": SQUARE, "pattern": "GRID"},
        ],
    }
    r = client.post("/api/missions/", data=json.dumps(payload),
                    content_type="application/json")
    assert r.status_code == 200
    created = r.json()
    assert created["id"]
    assert len(created["elements"]) == 2

    r2 = client.get(f"/api/missions/{created['id']}/get/")
    assert r2.status_code == 200
    fetched = r2.json()
    assert fetched["name"] == "Nova iz testa"
    assert fetched["finish_action"] == "LAND"
    assert fetched["elements"][1]["polygon_geojson"] == SQUARE


def test_save_replaces_elements(client, mission: Mission) -> None:
    payload = {
        "name": mission.name, "drone_id": mission.drone_id,
        "elements": [{"element_type": "WP", "order": 1, "altitude_m": 20,
                      "lat": 46.06, "lon": 14.52}],
    }
    r = client.post(f"/api/missions/{mission.pk}/", data=json.dumps(payload),
                    content_type="application/json")
    assert r.status_code == 200
    assert len(r.json()["elements"]) == 1
    assert mission.elements.count() == 1


def test_save_rejects_broken_json(client) -> None:
    r = client.post("/api/missions/", data="{ni json", content_type="application/json")
    assert r.status_code == 400


def test_save_requires_drone_id(client) -> None:
    r = client.post("/api/missions/", data=json.dumps({"name": "x"}),
                    content_type="application/json")
    assert r.status_code == 400


def test_get_requires_get_method(client, mission: Mission) -> None:
    assert client.post(f"/api/missions/{mission.pk}/get/").status_code == 405


# ---------------------------------------------------------------------------
# API --- zgrajena MAVLink misija
# ---------------------------------------------------------------------------
def test_items_endpoint_returns_full_command_sequence(
    client, mission: Mission,
) -> None:
    r = client.get(f"/api/missions/{mission.pk}/items/")
    assert r.status_code == 200
    data = r.json()
    names = [i["command_name"] for i in data["items"]]
    assert names[0] == "NAV_WAYPOINT"          # home
    assert "NAV_TAKEOFF" in names
    assert "DO_CHANGE_SPEED" in names
    assert names[-1] == "NAV_RETURN_TO_LAUNCH"
    assert data["count"] == len(data["items"])


def test_items_endpoint_text_format(client, mission: Mission) -> None:
    r = client.get(f"/api/missions/{mission.pk}/items/?format=text")
    assert r.status_code == 200
    assert r["Content-Type"].startswith("text/plain")
    assert "NAV_TAKEOFF" in r.content.decode()


def test_grid_plan_endpoint(client, drone: DroneProfile) -> None:
    body = {"drone_id": drone.pk, "map_element": {
        "altitude_m": 60, "front_overlap_pct": 80, "side_overlap_pct": 70,
        "track_angle_deg": 0, "polygon_geojson": SQUARE}}
    r = client.post("/api/grid/plan/", data=json.dumps(body),
                    content_type="application/json")
    assert r.status_code == 200
    data = r.json()
    assert len(data["waypoints_lonlat"]) > 2
    assert data["stats"]["gsd_cm_per_px"] > 0


def test_grid_plan_rejects_degenerate_polygon(client, drone: DroneProfile) -> None:
    body = {"drone_id": drone.pk, "map_element": {
        "altitude_m": 60, "front_overlap_pct": 80, "side_overlap_pct": 70,
        "polygon_geojson": {"type": "Polygon", "coordinates": [[
            [14.5, 46.05], [14.5, 46.05], [14.5, 46.05], [14.5, 46.05]]]}}}
    r = client.post("/api/grid/plan/", data=json.dumps(body),
                    content_type="application/json")
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# API --- telemetrija in diagnostika
# ---------------------------------------------------------------------------
def test_telemetry_endpoint_without_connection(client) -> None:
    r = client.get("/api/telemetry/")
    assert r.status_code == 200
    data = r.json()
    assert data["connected"] is False
    assert "autoconnect" in data
    assert "attempts" in data["autoconnect"]
    assert "baud" in data["autoconnect"]


def test_serial_ports_endpoint(client) -> None:
    r = client.get("/api/serial-ports/")
    assert r.status_code == 200
    assert isinstance(r.json()["ports"], list)


def test_system_stats_endpoint(client) -> None:
    r = client.get("/api/system/stats/")
    assert r.status_code == 200
    assert "memory" in r.json()


def test_poweroff_requires_confirmation(client) -> None:
    r = client.post("/api/system/poweroff/", data="{}",
                    content_type="application/json")
    assert r.status_code == 400
    assert r.json()["confirm_required"] is True


def test_poweroff_schedules_when_confirmed(client, monkeypatch) -> None:
    from missions.services import system_power

    called: list[bool] = []
    monkeypatch.setattr(system_power, "sync_filesystems", lambda: None)
    monkeypatch.setattr(
        system_power, "schedule_poweroff",
        lambda **_kw: called.append(True),
    )
    monkeypatch.setattr(system_power, "_is_armed", lambda: False)

    r = client.post("/api/system/poweroff/",
                    data=json.dumps({"confirm": True}),
                    content_type="application/json")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert called == [True]


def test_poweroff_refuses_when_armed(client, monkeypatch) -> None:
    from missions.services import system_power

    monkeypatch.setattr(system_power, "_is_armed", lambda: True)
    r = client.post("/api/system/poweroff/",
                    data=json.dumps({"confirm": True}),
                    content_type="application/json")
    assert r.status_code == 400
    assert r.json()["armed"] is True


def test_poweroff_requires_login_when_enabled(client, settings) -> None:
    settings.UAV_REQUIRE_AUTH = True
    r = client.post("/api/system/poweroff/",
                    data=json.dumps({"confirm": True}),
                    content_type="application/json")
    assert r.status_code == 403
    assert r.json()["auth_required"] is True


# ---------------------------------------------------------------------------
# API --- kontrola (brez povezave s krmilnikom mora vrniti razlozljivo napako)
# ---------------------------------------------------------------------------
def test_upload_without_connection_returns_error(client, mission: Mission) -> None:
    r = client.post(f"/api/missions/{mission.pk}/upload/", data="{}",
                    content_type="application/json")
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_upload_of_empty_mission(client, drone: DroneProfile) -> None:
    empty = Mission.objects.create(name="Prazna", drone=drone)
    r = client.post(f"/api/missions/{empty.pk}/upload/", data="{}",
                    content_type="application/json")
    assert r.status_code == 400
    assert "tocke" in r.json()["error"]


def test_connect_requires_device(client) -> None:
    r = client.post("/api/mavlink/connect/", data="{}",
                    content_type="application/json")
    assert r.status_code == 400


def test_arm_requires_confirmation(client) -> None:
    r = client.post("/api/mavlink/arm/", data=json.dumps({"arm": True}),
                    content_type="application/json")
    assert r.status_code == 400
    assert r.json()["confirm_required"] is True


def test_arm_with_confirmation_but_no_link(client) -> None:
    r = client.post("/api/mavlink/arm/",
                    data=json.dumps({"arm": True, "confirm": True}),
                    content_type="application/json")
    assert r.status_code == 400
    assert "povezave" in r.json()["error"]


def test_mode_requires_mode_name(client) -> None:
    r = client.post("/api/mavlink/mode/", data="{}",
                    content_type="application/json")
    assert r.status_code == 400


def test_mission_start_requires_confirmation(client) -> None:
    r = client.post("/api/mavlink/start/", data="{}",
                    content_type="application/json")
    assert r.status_code == 400
    assert r.json()["confirm_required"] is True


def test_mission_start_without_link(client) -> None:
    r = client.post("/api/mavlink/start/", data=json.dumps({"confirm": True}),
                    content_type="application/json")
    assert r.status_code == 400
    assert "povezave" in r.json()["error"]


def test_control_endpoints_reject_get(client) -> None:
    for url in ("/api/mavlink/connect/", "/api/mavlink/arm/",
                "/api/mavlink/mode/", "/api/mavlink/start/",
                "/api/logs/start/", "/api/logs/stop/"):
        assert client.get(url).status_code == 405, url


# ---------------------------------------------------------------------------
# Avtentikacija kontrolnih koncisc
# ---------------------------------------------------------------------------
def test_control_blocked_for_anonymous_when_auth_required(
    client, settings, mission: Mission,
) -> None:
    settings.UAV_REQUIRE_AUTH = True
    for url, body in (
        ("/api/mavlink/connect/", {"device": "/dev/ttyACM0"}),
        ("/api/mavlink/mode/", {"mode": "AUTO"}),
        ("/api/mavlink/arm/", {"arm": True, "confirm": True}),
        ("/api/mavlink/start/", {"confirm": True}),
        (f"/api/missions/{mission.pk}/upload/", {}),
        ("/api/logs/start/", {}),
    ):
        r = client.post(url, data=json.dumps(body),
                        content_type="application/json")
        assert r.status_code == 403, url
        assert r.json()["auth_required"] is True


def test_reading_stays_open_when_auth_required(client, settings, mission) -> None:
    settings.UAV_REQUIRE_AUTH = True
    assert client.get("/api/telemetry/").status_code == 200
    assert client.get(f"/api/missions/{mission.pk}/get/").status_code == 200
    assert client.get("/api/logs/").status_code == 200


def test_logged_in_user_may_control(client, settings) -> None:
    settings.UAV_REQUIRE_AUTH = True
    User.objects.create_user("pilot", password="tajno-geslo-123")
    assert client.login(username="pilot", password="tajno-geslo-123")
    r = client.post("/api/mavlink/mode/", data=json.dumps({"mode": "AUTO"}),
                    content_type="application/json")
    # Prijava je uspela --> ni vec 403; napaka je zdaj vsebinska (ni povezave).
    assert r.status_code == 400
    assert "povezave" in r.json()["error"]


# ---------------------------------------------------------------------------
# Letalni logi
# ---------------------------------------------------------------------------
def test_storage_devices_lists_usb_partitions(client, monkeypatch) -> None:
    monkeypatch.setattr("missions.views.list_usb_partitions", lambda: [{
        "path": "/dev/sda1",
        "label": "UAV",
        "filesystem": "vfat",
        "size_b": 32_000_000_000,
        "mountpoints": [],
        "selected": False,
        "usable": True,
    }])
    response = client.get("/api/storage/devices/")
    assert response.status_code == 200
    assert response.json()["devices"][0]["path"] == "/dev/sda1"


def test_storage_select_requires_confirmation(client) -> None:
    response = client.post(
        "/api/storage/select/",
        data=json.dumps({"device": "/dev/sda1"}),
        content_type="application/json",
    )
    assert response.status_code == 400
    assert response.json()["confirm_required"] is True


def test_storage_select_mounts_device_persistently(
    client, monkeypatch,
) -> None:
    monkeypatch.setattr(
        "missions.views.select_usb_partition",
        lambda device: {
            "ok": True,
            "device": device,
            "mount_point": "/mnt/uav-data",
            "persistent": True,
        },
    )
    response = client.post(
        "/api/storage/select/",
        data=json.dumps({"device": "/dev/sda1", "confirm": True}),
        content_type="application/json",
    )
    assert response.status_code == 200
    assert response.json()["persistent"] is True


def test_logs_list_is_empty_initially(client) -> None:
    r = client.get("/api/logs/")
    assert r.status_code == 200
    assert r.json()["storage"]["available"] is True
    assert r.json()["sessions"] == []


def test_logs_report_missing_usb_without_breaking_app(
    client, monkeypatch,
) -> None:
    def unavailable():
        raise StorageUnavailableError("Ni USB ključka.")

    monkeypatch.setattr(
        "missions.services.flight_storage.resolve_log_dir", unavailable)

    status = client.get("/api/logs/")
    assert status.status_code == 200
    assert status.json()["storage"]["available"] is False
    assert status.json()["sessions"] == []

    start = client.post(
        "/api/logs/start/",
        data=json.dumps({"reason": "manual"}),
        content_type="application/json",
    )
    assert start.status_code == 503
    assert "USB" in start.json()["error"]


def test_log_download_rejects_unknown_file(client) -> None:
    assert client.get("/api/logs/seja/passwd").status_code == 404


def test_log_download_rejects_path_traversal(client, settings) -> None:
    r = client.get("/api/logs/..%2f..%2fetc/telemetry.jsonl")
    assert r.status_code == 404


def test_log_download_serves_existing_file(client, settings) -> None:
    session = settings.UAV_LOG_DIR / "20260725-120000_test"
    session.mkdir(parents=True)
    (session / "telemetry.jsonl").write_text('{"t":1,"type":"HEARTBEAT"}\n',
                                             encoding="utf-8")
    r = client.get(f"/api/logs/{session.name}/telemetry.jsonl")
    assert r.status_code == 200
    assert b"HEARTBEAT" in b"".join(r.streaming_content)


# ---------------------------------------------------------------------------
# Samodejni testni polet
# ---------------------------------------------------------------------------
def test_test_flight_page_renders(client) -> None:
    r = client.get("/test-polet/")
    assert r.status_code == 200
    body = r.content.decode()
    assert "Samodejni testni polet" in body
    assert "PREKINI" in body                    # gumb za prekinitev je vedno tam
    assert "tf-start-hover" in body
    assert "tf-start-hop" in body
    assert "Test 2" in body
    assert r.context["can_control"] is True


def test_settings_page_groups_editable_values(client) -> None:
    response = client.get("/nastavitve/")
    assert response.status_code == 200
    body = response.content.decode()
    assert "ARMING_MAGTHRESH" in body
    assert "RTL_ALT" in body
    assert 'id="settings-rtl-alt"' in body
    assert "settings-tf-save" in body
    assert 'id="settings-tf1-sats"' in body
    assert 'id="settings-tf2-hdop"' in body
    assert "settings-storage" in body
    assert body.index('id="wifi-status"') < body.index("Načrtovalnik")
    assert body.index('href="/test-polet/"') < body.index(
        'href="/nastavitve/"')


def test_test_flight_page_has_separate_profile_params(client) -> None:
    body = client.get("/test-polet/").content.decode()
    assert 'id="tf1-sats"' in body
    assert 'id="tf2-sats"' in body
    assert 'id="tf1-hdop"' in body
    assert 'id="tf2-hdop"' in body
    assert "Test 1 — vzlet" in body
    assert "Test 2 — skok" in body


def test_test_flight_params_save_keeps_profiles_separate(client) -> None:
    r = client.post(
        "/api/testflight/params/save/",
        data=json.dumps({
            "hover": {
                "altitude_m": 3, "hover_s": 5, "countdown_s": 5,
                "min_satellites": 10, "max_hdop": 1.5, "require_gps": True,
            },
            "hop": {
                "altitude_m": 2, "leg_m": 2, "countdown_s": 5,
                "min_satellites": 14, "max_hdop": 1.1, "require_gps": True,
            },
        }),
        content_type="application/json",
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["params"]["min_satellites"] == 10
    assert data["params"]["max_hdop"] == pytest.approx(1.5)
    assert data["params"]["hop"]["min_satellites"] == 14
    assert data["params"]["hop"]["max_hdop"] == pytest.approx(1.1)

    again = client.get("/api/testflight/params/").json()
    assert again["hover"]["min_satellites"] == 10
    assert again["hop"]["min_satellites"] == 14
    assert again["hop"]["max_hdop"] == pytest.approx(1.1)


def test_test_flight_page_flags_login(client, settings) -> None:
    settings.UAV_REQUIRE_AUTH = True
    assert client.get("/test-polet/").context["can_control"] is False


def test_test_flight_status_reports_checks_without_connection(client) -> None:
    r = client.get("/api/testflight/status/")
    assert r.status_code == 200
    data = r.json()
    assert data["phase"] == "IDLE"
    assert data["active"] is False
    assert data["checks_pass"] is False
    # Brez povezave mora prva preverba pasti in to mora biti vidno.
    conn = next(c for c in data["checks"] if c["key"] == "connected")
    assert conn["ok"] is False


def test_test_flight_start_requires_confirmation(client) -> None:
    r = client.post("/api/testflight/start/", data=json.dumps({"altitude_m": 3}),
                    content_type="application/json")
    assert r.status_code == 400
    assert r.json()["confirm_required"] is True


def test_test_flight_start_refused_without_connection(client) -> None:
    r = client.post("/api/testflight/start/",
                    data=json.dumps({"confirm": True, "altitude_m": 3}),
                    content_type="application/json")
    assert r.status_code == 400
    assert "povezave" in r.json()["error"]


def test_test_flight_abort_needs_no_confirmation(client) -> None:
    """Prekinitev mora biti en klik --- zato brez confirm."""
    r = client.post("/api/testflight/abort/", data="{}",
                    content_type="application/json")
    # Ni aktivnega zaporedja in dron ni armiran -> 400, a NE zaradi confirm.
    assert r.status_code == 400
    assert "confirm_required" not in r.json()
    assert "ne tece" in r.json()["error"]


def test_test_flight_endpoints_reject_get(client) -> None:
    for url in ("/api/testflight/start/", "/api/testflight/abort/"):
        assert client.get(url).status_code == 405, url


def test_test_flight_control_requires_login_when_enabled(client, settings) -> None:
    settings.UAV_REQUIRE_AUTH = True
    for url, body in (("/api/testflight/start/", {"confirm": True}),
                      ("/api/testflight/abort/", {})):
        r = client.post(url, data=json.dumps(body),
                        content_type="application/json")
        assert r.status_code == 403, url
        assert r.json()["auth_required"] is True
    # Branje stanja ostane odprto.
    assert client.get("/api/testflight/status/").status_code == 200


# ---------------------------------------------------------------------------
# Preklop Wi-Fi omrezja prek fizicnega stikala
# ---------------------------------------------------------------------------
def test_network_status_without_daemon(client) -> None:
    """Ce demon ne tece (stikalo ni namesceno), koncisce to pove razlozljivo."""
    r = client.get("/api/network/status/")
    assert r.status_code == 200
    data = r.json()
    assert data["available"] is False
    assert "error" in data


def test_network_status_reads_daemon_state(client, settings) -> None:
    from missions.services.network_switch import CLIENT, write_state
    write_state(settings.UAV_RUNTIME_DIR,
               {"mode": CLIENT, "pending": None}, reason="gpio19 low")
    r = client.get("/api/network/status/")
    assert r.status_code == 200
    data = r.json()
    assert data["available"] is True
    assert data["mode"] == CLIENT
    assert data["pending"] is None
    assert data["reason"] == "gpio19 low"


def test_network_status_reports_pending_switch(client, settings) -> None:
    from missions.services.network_switch import AP, CLIENT, write_state
    write_state(settings.UAV_RUNTIME_DIR, {
        "mode": CLIENT,
        "pending": {"target": AP, "requested_at": 0, "deadline": 30,
                    "remaining_s": 12.3},
    })
    data = client.get("/api/network/status/").json()
    assert data["pending"]["target"] == AP
    assert data["pending"]["remaining_s"] == pytest.approx(12.3)


def test_network_status_touches_heartbeat(client, settings) -> None:
    """Vsak klic stanja je hkrati utrip, ki ga demon bere kot 'GUI aktiven'."""
    from missions.services.network_switch import heartbeat_age_s
    client.get("/api/network/status/")
    assert heartbeat_age_s(settings.UAV_RUNTIME_DIR, __import__("time").time()) < 2.0


def test_network_force_requires_post(client) -> None:
    assert client.get("/api/network/force/").status_code == 405


def test_network_force_writes_flag(client, settings) -> None:
    from missions.services.network_switch import consume_force
    r = client.post("/api/network/force/", data="{}",
                    content_type="application/json")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert consume_force(settings.UAV_RUNTIME_DIR) is True


def test_network_force_needs_no_confirmation(client) -> None:
    """Za razliko od arm/start je to samo pospesitev, ne nova nevarna akcija."""
    r = client.post("/api/network/force/", data="{}",
                    content_type="application/json")
    assert r.status_code == 200
    assert "confirm_required" not in r.json()


def test_network_force_requires_login_when_enabled(client, settings) -> None:
    settings.UAV_REQUIRE_AUTH = True
    r = client.post("/api/network/force/", data="{}",
                    content_type="application/json")
    assert r.status_code == 403
    assert r.json()["auth_required"] is True
    # Branje stanja ostane odprto tudi tu.
    assert client.get("/api/network/status/").status_code == 200
