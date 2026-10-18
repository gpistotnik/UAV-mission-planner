"""Privzeti profili dronov, ki se ob zagonu/plannerju vedno zagotovijo."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from .models import DroneProfile

DEFAULT_DRONE_SLUG = "f450-pixhawk-rpi-imx219"

# Sony IMX219 (Raspberry Pi Camera Module 2): aktivno polje 3280×2464,
# velikost piksla 1,12 µm, objektiv modula 3,04 mm.
DEFAULT_DRONE_FIELDS: dict[str, Any] = {
    "display_name": "F450 + Pixhawk 2.4.8 + RPi (Sony IMX219)",
    "manufacturer": "Custom",
    "max_speed_ms": Decimal("12.00"),
    "max_altitude_m": Decimal("120.00"),
    "sensor_width_mm": Decimal("3.674"),
    "sensor_height_mm": Decimal("2.760"),
    "focal_length_mm": Decimal("3.040"),
    "image_width_px": 3280,
    "image_height_px": 2464,
}

REMOVED_DRONE_SLUGS = ("dji-mavic-3",)


def ensure_default_drone() -> DroneProfile:
    """Ustvari privzeti F450 profil (Sony IMX219), če še ne obstaja."""
    drone, _ = DroneProfile.objects.get_or_create(
        slug=DEFAULT_DRONE_SLUG,
        defaults=DEFAULT_DRONE_FIELDS,
    )
    return drone
