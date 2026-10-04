"""Glavni URL konfigurator.

Vmesnik je enojezicen, zato URL-ji nimajo jezikovne predpone: nadzorna
plosca je na ``/nadzor/`` in ne na ``/sl/nadzor/``. API koncisca so locena
pod ``/api/`` --- so strojna in jih ne bere clovek.
"""
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from core import tiles as tile_views
from missions import views as mviews

urlpatterns = [

    # --- lokalne karte (OSM na disku RPi) ---
    path("tiles/status/", tile_views.tiles_status_api, name="tiles_status"),
    path("tiles/osm/<int:z>/<int:x>/<int:y>.png", tile_views.osm_tile,
         name="tiles_osm"),

    # --- misije ---
    path("api/missions/", mviews.mission_api_save, name="api_missions_create"),
    path("api/missions/<int:pk>/", mviews.mission_api_save, name="api_missions_save"),
    path("api/missions/<int:pk>/get/", mviews.mission_api_get, name="api_missions_get"),
    path("api/missions/<int:pk>/items/", mviews.mission_items_api,
         name="api_missions_items"),
    path("api/missions/<int:pk>/upload/", mviews.mission_upload_api,
         name="api_missions_upload"),
    path("api/grid/plan/", mviews.grid_plan_api, name="api_grid_plan"),

    # --- povezava in telemetrija ---
    path("api/serial-ports/", mviews.serial_ports_api, name="api_serial_ports"),
    path("api/mavlink/connect/", mviews.mavlink_connect_api,
         name="api_mavlink_connect"),
    path("api/mavlink/disconnect/", mviews.mavlink_disconnect_api,
         name="api_mavlink_disconnect"),
    path("api/telemetry/", mviews.telemetry_api, name="api_telemetry"),
    path("api/system/stats/", mviews.system_stats_api, name="api_system_stats"),
    path("api/system/poweroff/", mviews.system_poweroff_api,
         name="api_system_poweroff"),

    # --- kontrola letalnika ---
    path("api/mavlink/arm/", mviews.mavlink_arm_api, name="api_mavlink_arm"),
    path("api/mavlink/mode/", mviews.mavlink_mode_api, name="api_mavlink_mode"),
    path("api/mavlink/capture/", mviews.mavlink_capture_api,
         name="api_mavlink_capture"),
    path("api/mavlink/mag-cal/", mviews.mavlink_mag_cal_api, name="api_mavlink_mag_cal"),
    path("api/mavlink/param/", mviews.mavlink_param_api, name="api_mavlink_param"),
    path("api/mavlink/param/set/", mviews.mavlink_param_set_api,
         name="api_mavlink_param_set"),
    path("api/mavlink/start/", mviews.mission_start_api, name="api_mission_start"),

    # --- samodejni testni polet ---
    path("api/testflight/status/", mviews.test_flight_status_api,
         name="api_testflight_status"),
    path("api/testflight/params/", mviews.test_flight_params_api,
         name="api_testflight_params"),
    path("api/testflight/params/save/", mviews.test_flight_params_save_api,
         name="api_testflight_params_save"),
    path("api/testflight/start/", mviews.test_flight_start_api,
         name="api_testflight_start"),
    path("api/testflight/abort/", mviews.test_flight_abort_api,
         name="api_testflight_abort"),

    # --- letalni logi ---
    path("api/storage/devices/", mviews.storage_devices_api,
         name="api_storage_devices"),
    path("api/storage/select/", mviews.storage_select_api,
         name="api_storage_select"),
    path("api/logs/", mviews.logs_api, name="api_logs"),
    path("api/logs/start/", mviews.logging_start_api, name="api_logs_start"),
    path("api/logs/stop/", mviews.logging_stop_api, name="api_logs_stop"),
    path("api/logs/<str:name>/<str:filename>", mviews.log_download_api,
         name="api_log_download"),

    # --- kamera / galerija ---
    path("api/camera/capture/", mviews.camera_capture_api,
         name="api_camera_capture"),
    path("api/gallery/", mviews.gallery_list_api, name="api_gallery_list"),
    path("api/gallery/download/", mviews.gallery_download_api,
         name="api_gallery_download"),
    path("api/gallery/image/<path:session>/<str:filename>",
         mviews.gallery_image_api, name="api_gallery_image"),

    # --- preklop Wi-Fi omrezja prek fizicnega stikala ---
    path("api/network/status/", mviews.network_status_api,
         name="api_network_status"),
    path("api/network/force/", mviews.network_force_switch_api,
         name="api_network_force"),
]

urlpatterns += [
    path("admin/", admin.site.urls),
    path("prijava/", auth_views.LoginView.as_view(
        template_name="core/login.html"), name="login"),
    path("odjava/", auth_views.LogoutView.as_view(next_page="/"), name="logout"),
    path("test-polet/", mviews.test_flight_page, name="test_flight"),
    path("nastavitve/", mviews.app_settings_page, name="app_settings"),
    path("kompas/", mviews.mag_cal_page, name="mag_cal"),
    path("galerija/", mviews.gallery_page, name="gallery"),
    path("", include("core.urls")),
    path("misije/", include("missions.urls")),
]
