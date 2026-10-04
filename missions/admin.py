"""Django admin za misije."""
from django.contrib import admin

from .models import DroneProfile, Mission, MissionElement


class MissionElementInline(admin.TabularInline):
    model = MissionElement
    extra = 0
    fields = ("order", "element_type", "name", "altitude_m", "speed_ms",
              "lat", "lon", "action_type", "pattern")
    ordering = ("order",)


@admin.register(DroneProfile)
class DroneProfileAdmin(admin.ModelAdmin):
    list_display = ("display_name", "manufacturer", "max_speed_ms",
                    "max_altitude_m", "focal_length_mm")
    search_fields = ("display_name", "slug", "manufacturer")
    prepopulated_fields = {"slug": ("display_name",)}


@admin.register(Mission)
class MissionAdmin(admin.ModelAdmin):
    list_display = ("name", "drone", "altitude_mode",
                    "default_altitude_m", "default_speed_ms", "updated_at")
    list_filter = ("drone", "altitude_mode", "finish_action")
    search_fields = ("name", "description")
    inlines = [MissionElementInline]
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        (None, {"fields": ("name", "description", "drone")}),
        ("Vzletisce", {"fields": ("home_lat", "home_lon", "home_alt_amsl_m")}),
        ("Privzeti parametri", {
            "fields": ("altitude_mode", "default_altitude_m",
                       "default_speed_ms", "finish_action"),
        }),
        ("Sledenje", {"fields": ("created_at", "updated_at")}),
    )


@admin.register(MissionElement)
class MissionElementAdmin(admin.ModelAdmin):
    list_display = ("mission", "order", "element_type", "name",
                    "altitude_m", "action_type", "pattern")
    list_filter = ("element_type", "action_type", "pattern", "mission")
    ordering = ("mission_id", "order")
