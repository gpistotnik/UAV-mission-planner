"""Podatkovni model misije / Mission data model.

Nov pristop (v2): misija je urejeno zaporedje gradnikov (``MissionElement``).
Vsak gradnik je bodisi **waypoint** (ena točka z dejanjem) ali **mapping zone**
(območje za fotogrametričen prelet). Dron leti gradnike v vrstnem redu polja
``order``; za mapping zone se ob izvozu generira interno zaporedje točk
(boustrofedon) in se spoji s sosednjima gradnikoma.
"""
from __future__ import annotations

from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse


# ---------------------------------------------------------------------------
# Naštevanja / Enums
# ---------------------------------------------------------------------------
class AltitudeMode(models.TextChoices):
    AGL = "AGL", "Relativno glede na vzletisce (AGL)"
    AMSL = "AMSL", "Absolutno nad morsko gladino (AMSL)"


class HeadingMode(models.TextChoices):
    AUTO = "AUTO", "Sledi smeri leta"
    FIXED = "FIXED", "Fiksna smer"
    POI = "POI", "Cilj zanimanja (POI)"
    NEXT_WP = "NEXT_WP", "Proti naslednjemu waypointu"


class FinishAction(models.TextChoices):
    HOVER = "HOVER", "Lebdenje na zadnji tocki"
    RTH = "RTH", "Vrnitev domov (RTH)"
    LAND = "LAND", "Pristanek"


class ElementType(models.TextChoices):
    WAYPOINT = "WP", "Waypoint"
    MAP = "MAP", "Mapping zone"


class PathStyle(models.TextChoices):
    STRAIGHT = "STRAIGHT", "Ravno (ustavi v tocki)"
    CURVED = "CURVED", "Krivo (zaobide tocko)"


class ActionType(models.TextChoices):
    NONE = "NONE", "Brez dejanja"
    PHOTO = "PHOTO", "Foto"
    VIDEO_START = "VIDEO_START", "Zacni snemanje"
    VIDEO_STOP = "VIDEO_STOP", "Ustavi snemanje"
    CAPTURE_BURST = "CAPTURE_BURST", "Burst zajem"


class MapPattern(models.TextChoices):
    GRID = "GRID", "Obicajen"          # enosmerna proga (lawnmower)
    CROSSHATCH = "CROSSHATCH", "Mreza" # dvosmerno, pravokotno (crosshatch)


# ---------------------------------------------------------------------------
# Profili dronov / Drone profiles
# ---------------------------------------------------------------------------
class DroneProfile(models.Model):
    slug = models.SlugField("Identifikator", max_length=64, unique=True)
    display_name = models.CharField("Prikazno ime", max_length=120)
    manufacturer = models.CharField("Proizvajalec", max_length=64, blank=True)
    max_speed_ms = models.DecimalField(
        "Najv. hitrost [m/s]", max_digits=5, decimal_places=2, default=Decimal("15"))
    max_altitude_m = models.DecimalField(
        "Najv. visina [m]", max_digits=6, decimal_places=2, default=Decimal("120"))
    sensor_width_mm = models.DecimalField(
        "Sirina senzorja [mm]", max_digits=6, decimal_places=3)
    sensor_height_mm = models.DecimalField(
        "Visina senzorja [mm]", max_digits=6, decimal_places=3)
    focal_length_mm = models.DecimalField(
        "Goriscna razdalja [mm]", max_digits=6, decimal_places=3)
    image_width_px = models.PositiveIntegerField("Sirina slike [px]")
    image_height_px = models.PositiveIntegerField("Visina slike [px]")
    class Meta:
        verbose_name = "Profil drona"
        verbose_name_plural = "Profili dronov"
        ordering = ["display_name"]

    def __str__(self) -> str:
        return self.display_name


# ---------------------------------------------------------------------------
# Misija / Mission (glava)
# ---------------------------------------------------------------------------
class Mission(models.Model):
    name = models.CharField("Ime misije", max_length=120)
    description = models.TextField("Opis", blank=True)
    drone = models.ForeignKey(
        DroneProfile, verbose_name="Drone",
        on_delete=models.PROTECT, related_name="missions")
    home_lat = models.DecimalField(
        "Vzletisce sirina", max_digits=10, decimal_places=7,
        null=True, blank=True,
        validators=[MinValueValidator(Decimal("-90")), MaxValueValidator(Decimal("90"))])
    home_lon = models.DecimalField(
        "Vzletisce dolzina", max_digits=10, decimal_places=7,
        null=True, blank=True,
        validators=[MinValueValidator(Decimal("-180")), MaxValueValidator(Decimal("180"))])
    home_alt_amsl_m = models.DecimalField(
        "Vzletisce AMSL [m]", max_digits=8, decimal_places=2, null=True, blank=True)
    altitude_mode = models.CharField(
        "Referenca visine", max_length=4,
        choices=AltitudeMode.choices, default=AltitudeMode.AGL)
    default_altitude_m = models.DecimalField(
        "Privzeta visina [m]", max_digits=6, decimal_places=2, default=Decimal("50"))
    default_speed_ms = models.DecimalField(
        "Privzeta hitrost [m/s]", max_digits=5, decimal_places=2, default=Decimal("5"))
    finish_action = models.CharField(
        "Dejanje ob koncu", max_length=8,
        choices=FinishAction.choices, default=FinishAction.RTH)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Misija"
        verbose_name_plural = "Misije"
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return self.name

    def get_absolute_url(self) -> str:
        return reverse("missions:detail", args=[self.pk])


# ---------------------------------------------------------------------------
# Gradnik misije / Mission element
# ---------------------------------------------------------------------------
class MissionElement(models.Model):
    """Polimorfen gradnik: waypoint (WP) ali mapping zone (MAP).

    Enotna tabela z nullable polji za posamezen tip; zelo enostavna shema, ki
    omogoča preprosto prerazporejanje (samo posodobiš ``order``).
    """
    mission = models.ForeignKey(
        Mission, related_name="elements", on_delete=models.CASCADE,
        verbose_name="Misija")
    order = models.PositiveIntegerField("Vrstni red")
    element_type = models.CharField(
        "Tip", max_length=4, choices=ElementType.choices)
    name = models.CharField("Oznaka", max_length=64, blank=True)

    # --- Skupno za oba tipa ---
    altitude_m = models.DecimalField(
        "Visina [m]", max_digits=6, decimal_places=2)
    speed_ms = models.DecimalField(
        "Hitrost [m/s]", max_digits=5, decimal_places=2, null=True, blank=True)

    # --- WAYPOINT polja ---
    lat = models.DecimalField(
        "Sirina", max_digits=10, decimal_places=7, null=True, blank=True,
        validators=[MinValueValidator(Decimal("-90")), MaxValueValidator(Decimal("90"))])
    lon = models.DecimalField(
        "Dolzina", max_digits=10, decimal_places=7, null=True, blank=True,
        validators=[MinValueValidator(Decimal("-180")), MaxValueValidator(Decimal("180"))])
    heading_mode = models.CharField(
        "Nacin smeri", max_length=8,
        choices=HeadingMode.choices, default=HeadingMode.AUTO, blank=True)
    heading_deg = models.DecimalField(
        "Smer [°]", max_digits=5, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("360"))])
    gimbal_pitch_deg = models.DecimalField(
        "Naklon gimbala [°]", max_digits=5, decimal_places=2,
        default=Decimal("-90"),
        validators=[MinValueValidator(Decimal("-90")), MaxValueValidator(Decimal("30"))])
    path_style = models.CharField(
        "Pot do tocke", max_length=8,
        choices=PathStyle.choices, default=PathStyle.STRAIGHT, blank=True)
    action_type = models.CharField(
        "Dejanje", max_length=16,
        choices=ActionType.choices, default=ActionType.NONE, blank=True)
    hover_time_s = models.DecimalField(
        "Cas lebdenja [s]", max_digits=5, decimal_places=1,
        default=Decimal("0"))

    # --- MAP polja ---
    polygon_geojson = models.JSONField(
        "Poligon (GeoJSON)", null=True, blank=True)
    pattern = models.CharField(
        "Vzorec", max_length=10,
        choices=MapPattern.choices, default=MapPattern.GRID, blank=True)
    front_overlap_pct = models.PositiveSmallIntegerField(
        "Vzd. prekrivanje [%]", default=80,
        validators=[MinValueValidator(0), MaxValueValidator(95)])
    side_overlap_pct = models.PositiveSmallIntegerField(
        "Prec. prekrivanje [%]", default=70,
        validators=[MinValueValidator(0), MaxValueValidator(95)])
    track_angle_deg = models.DecimalField(
        "Smer prog [°]", max_digits=5, decimal_places=2, default=Decimal("0"))

    class Meta:
        verbose_name = "Gradnik"
        verbose_name_plural = "Gradniki"
        ordering = ["mission_id", "order"]
        constraints = [
            models.UniqueConstraint(
                fields=["mission", "order"], name="uniq_element_order_per_mission"),
        ]

    def __str__(self) -> str:
        return f"{self.mission.name} · #{self.order} ({self.element_type})"

    @property
    def is_waypoint(self) -> bool:
        return self.element_type == ElementType.WAYPOINT

    @property
    def is_map(self) -> bool:
        return self.element_type == ElementType.MAP


# ---------------------------------------------------------------------------
# Nastavitve testnega poleta (singleton na napravi)
# ---------------------------------------------------------------------------
class TestFlightSettings(models.Model):
    """Trajne nastavitve testov 1 (hover) in 2 (hop); ena vrstica, pk=1."""

    # Test 1 — vzlet / lebdenje
    altitude_m = models.FloatField("Test 1 višina [m]", default=3.0)
    hover_s = models.FloatField("Test 1 lebdenje [s]", default=5.0)
    countdown_s = models.FloatField("Test 1 odštevanje [s]", default=5.0)
    min_satellites = models.PositiveSmallIntegerField(
        "Test 1 najmanj satelitov", default=10)
    max_hdop = models.FloatField("Test 1 največji HDOP", default=1.5)
    require_gps = models.BooleanField("Test 1 zahtevaj GPS", default=True)

    # Test 2 — skok (strožji GPS privzetki)
    hop_altitude_m = models.FloatField("Test 2 višina [m]", default=2.0)
    hop_leg_m = models.FloatField("Test 2 odmik [m]", default=2.0)
    hop_countdown_s = models.FloatField("Test 2 odštevanje [s]", default=5.0)
    hop_min_satellites = models.PositiveSmallIntegerField(
        "Test 2 najmanj satelitov", default=12)
    hop_max_hdop = models.FloatField("Test 2 največji HDOP", default=1.2)
    hop_require_gps = models.BooleanField("Test 2 zahtevaj GPS", default=True)

    updated_at = models.DateTimeField("Posodobljeno", auto_now=True)

    class Meta:
        verbose_name = "Nastavitve testnega poleta"
        verbose_name_plural = "Nastavitve testnega poleta"

    def __str__(self) -> str:
        return (f"TestFlightSettings "
                f"(T1 {self.altitude_m} m / T2 {self.hop_altitude_m} m)")

    @classmethod
    def load(cls) -> "TestFlightSettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj
