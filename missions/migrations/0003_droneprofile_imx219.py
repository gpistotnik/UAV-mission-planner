"""Prestavi privzeti profil F450 s Sony IMX477 na Sony IMX219."""

from decimal import Decimal

from django.db import migrations

OLD_SLUG = "f450-pixhawk-rpi-imx477"
NEW_SLUG = "f450-pixhawk-rpi-imx219"

IMX219 = {
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

IMX477 = {
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


def _apply(apps, slug: str, fields: dict, previous_slug: str) -> None:
    DroneProfile = apps.get_model("missions", "DroneProfile")
    Mission = apps.get_model("missions", "Mission")
    current = DroneProfile.objects.filter(slug=slug).first()
    previous = DroneProfile.objects.filter(slug=previous_slug).first()
    if previous and current and previous.pk != current.pk:
        Mission.objects.filter(drone_id=previous.pk).update(drone_id=current.pk)
        previous.delete()
    target = current or previous
    if target is None:
        DroneProfile.objects.create(slug=slug, **fields)
        return
    target.slug = slug
    for name, value in fields.items():
        setattr(target, name, value)
    target.save()


def forwards(apps, schema_editor):
    _apply(apps, NEW_SLUG, IMX219, OLD_SLUG)


def backwards(apps, schema_editor):
    _apply(apps, OLD_SLUG, IMX477, NEW_SLUG)


class Migration(migrations.Migration):

    dependencies = [
        ("missions", "0002_testflightsettings_hop_params"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
