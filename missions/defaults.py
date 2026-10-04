"""Privzeti profili dronov, ki se ob zagonu/plannerju vedno zagotovijo."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from .models import DroneProfile

DEFAULT_DRONE_SLUG = "f450-pixhawk-rpi-imx477"

DEFAULT_DRONE_FIELDS: dict[str, Any] = {
    "display_name": "F450 + Pixhawk 2.4.8 + RPi (Sony IMX477)",
    "manufacturer": "Custom",
    "max_speed_ms": Decimal("12.00"),
    "max_altitude_m": Decimal("120.00"),
    "sensor_width_mm": Decimal("6.287"),
    "sensor_height_mm": Decimal("4.712"),
    "focal_length_mm": Decimal("6.000"),
    "image_width_px": 4056,
    "image_height_px": 3040,
}

REMOVED_DRONE_SLUGS = ("dji-mavic-3",)


def ensure_default_drone() -> DroneProfile:
    """Ustvari privzeti F450 profil, če še ne obstaja."""
    drone, _ = DroneProfile.objects.get_or_create(
        slug=DEFAULT_DRONE_SLUG,
        defaults=DEFAULT_DRONE_FIELDS,
    )
    return drone
