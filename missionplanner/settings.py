"""
Django settings for the UAV mission planner.

Konfiguracija se bere iz `.env` (`python-decouple`). Podatkovna baza se
določa z eno samo `DATABASE_URL` spremenljivko (`dj-database-url`).
"""
from pathlib import Path

import dj_database_url
from decouple import Csv, config

from core.storage import is_raspberry_pi

BASE_DIR = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Varnost
# ---------------------------------------------------------------------------
SECRET_KEY = config("DJANGO_SECRET_KEY", default="dev-insecure-secret-key")
DEBUG = config("DJANGO_DEBUG", default=False, cast=bool)
ALLOWED_HOSTS = config(
    "DJANGO_ALLOWED_HOSTS",
    default="localhost,127.0.0.1,0.0.0.0",
    cast=Csv(),
)
# Potrebno za POST iz LAN-a (npr. shranjevanje misije, jezikovni preklop).
# Required for POSTs from LAN (e.g. mission save, language switch).
CSRF_TRUSTED_ORIGINS = config("CSRF_TRUSTED_ORIGINS", default="", cast=Csv())

# ---------------------------------------------------------------------------
# Aplikacije
# ---------------------------------------------------------------------------
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core.apps.CoreConfig",
    "missions.apps.MissionsConfig",
]

MIDDLEWARE = [
    # Pred CommonMiddleware / ALLOWED_HOSTS — glej core.captive.
    "core.captive.CaptivePortalMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "missionplanner.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "missionplanner.wsgi.application"

# ---------------------------------------------------------------------------
# Podatkovna baza
# ---------------------------------------------------------------------------
DATABASES = {
    "default": dj_database_url.parse(
        config("DATABASE_URL", default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}"),
        conn_max_age=600,
        conn_health_checks=True,
    ),
}

# ---------------------------------------------------------------------------
# Validacija gesel
# ---------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ---------------------------------------------------------------------------
# Jezik in casovni pas
# ---------------------------------------------------------------------------
# Vmesnik je enojezicen (slovenscina). Prevajalski sloj je bil odstranjen:
# besedila so neposredno v predlogah in modelih, URL-ji nimajo jezikovne
# predpone. USE_I18N=False izklopi Djangov prevajalski stroj, kar odpravi
# odvecno delo ob vsakem izrisu predloge.
LANGUAGE_CODE = "sl"
TIME_ZONE = config("DJANGO_TIME_ZONE", default="Europe/Ljubljana")
USE_I18N = False
USE_TZ = True

# ---------------------------------------------------------------------------
# Statika
# ---------------------------------------------------------------------------
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# Lokalne OSM XYZ ploščice (Slovenija) — niso v git; glej
# scripts/download_slovenia_tiles.py. Servira core.tiles.
MAP_TILES_ROOT = Path(config(
    "MAP_TILES_ROOT", default=str(BASE_DIR / "data" / "tiles")))
# Ob manjkajoči ploščici (npr. zoom > lokalnega paketa) prenesi z interneta
# in shrani na disk — samo za trenutni pogled, ne celo SI.
MAP_TILES_FETCH_ON_MISS = config("MAP_TILES_FETCH_ON_MISS", default=True, cast=bool)
MAP_TILES_FETCH_URL = config(
    "MAP_TILES_FETCH_URL",
    default="https://tile.openstreetmap.de/{z}/{x}/{y}.png",
)
MAP_TILES_FETCH_MAX_Z = config("MAP_TILES_FETCH_MAX_Z", default=20, cast=int)
MAP_TILES_FETCH_TIMEOUT = config("MAP_TILES_FETCH_TIMEOUT", default=8.0, cast=float)

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# UAV: kontrola letalnika in logiranje
# ---------------------------------------------------------------------------
# Koncisca, ki spremenijo stanje letalnika (povezava, prenos misije, arm,
# nacin, start, logiranje), zahtevajo prijavljenega uporabnika. Privzeto je
# to vezano na DEBUG: med razvojem odprto, v polju zaprto. Za bench teste
# brez racuna nastavi UAV_REQUIRE_AUTH=False.
UAV_REQUIRE_AUTH = config("UAV_REQUIRE_AUTH", default=not DEBUG, cast=bool)

LOGIN_URL = "/prijava/"
LOGIN_REDIRECT_URL = "/"

# Pot se razrešuje šele ob uporabi: odsoten ključek ne sme preprečiti migracij
# ali zagona strežnika. Na RPi je privzeto samodejno zaznavanje, drugje pa
# lokalna razvojna mapa. UAV_STORAGE_ROOT lahko poda eksplicitno USB pot.
UAV_STORAGE_ROOT = config(
    "UAV_STORAGE_ROOT", default="auto" if is_raspberry_pi() else "").strip()
UAV_LOG_DIR = Path(config(
    "UAV_LOG_DIR", default=str(BASE_DIR / "flightlogs")))
if not UAV_STORAGE_ROOT:
    UAV_LOG_DIR.mkdir(parents=True, exist_ok=True)

# Kdaj se zapis telemetrije zacne in konca:
#   "takeoff" (privzeto) --- ob dejanskem vzletu in pristanku,
#   "arm"                --- ob armanju in dis-armanju (sirsi interval),
#   "off"                --- samo rocno prek vmesnika.
# Samodejno prozenje je pomembno: zapis, ki ga je treba sprozit rocno, je
# zapis, ki bo kdaj pozabljen --- in pozabljen zapis pomeni ponovljen let.
UAV_LOG_TRIGGER = config("UAV_LOG_TRIGGER", default="takeoff")

# ---------------------------------------------------------------------------
# UAV: samodejna povezava s krmilnikom
# ---------------------------------------------------------------------------
# Na dronu nihce ne klikne "Povezi" --- streznik se mora povezati sam ob
# zagonu. V razvoju (DEBUG=True) je to izklopljeno, da lokalni runserver ne
# odpira serijskih vrat. Na Pi/dronu nastavi UAV_AUTOCONNECT=True (ali
# DJANGO_DEBUG=False).
UAV_AUTOCONNECT = config("UAV_AUTOCONNECT", default=not DEBUG, cast=bool)

# "auto" najprej USB (ttyACM*/ttyUSB*/COM*), nato UART (/dev/serial0) in
# ob neuspehu preklopi na naslednji kandidat. Trenutna faza (USB CDC):
# privzeta hitrost 115200. Za UART TELEM2 nastavi npr.
# UAV_SERIAL_DEVICE=/dev/serial0 in UAV_SERIAL_BAUD=921600.
# SITL: udp:127.0.0.1:14551
UAV_SERIAL_DEVICE = config("UAV_SERIAL_DEVICE", default="auto")
UAV_SERIAL_BAUD = config("UAV_SERIAL_BAUD", default=115200, cast=int)

# ---------------------------------------------------------------------------
# UAV: preklop Wi-Fi omrežja prek fizičnega stikala (GPIO19 -> GND)
# ---------------------------------------------------------------------------
# Mapa za kratkotrajno stanje, ki si ga delita samostojni GPIO demon
# (scripts/wifi_gpio_switch.py) in ta streznik: trenutni nacin omrezja,
# "GUI je aktiven" utrip in zastavica za takojsnjo izvedbo.
#
# Privzeto v repo mapi (kot UAV_LOG_DIR), NE v /run: mission-planner.service
# in demon oba techeta kot navaden uporabnik ($USER), /run pa je v pisanju
# omejen na root. Ce zelis pravi tmpfs, nastavi UAV_RUNTIME_DIR na
# /run/user/<uid>/uav-network (ta podmapa je v lasti uporabnika).
UAV_RUNTIME_DIR = Path(config("UAV_RUNTIME_DIR", default=str(BASE_DIR / "run")))
UAV_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

# Koliko sekund ima nekdo, ki gleda vmesnik, casa, preden se WiFi preklopi
# sam. Med tem lahko klikne "Preklopi zdaj" in prekinitev sprozi sam.
UAV_NETWORK_GRACE_S = config("UAV_NETWORK_GRACE_S", default=30.0, cast=float)

# Captive portal (DNS hijack + ta URL): probe zahteve → 302 sem.
# Privzeto mDNS ime; na AP omrežju dnsmasq že resolva dron.local → 10.0.0.1.
UAV_CAPTIVE_REDIRECT_URL = config(
    "UAV_CAPTIVE_REDIRECT_URL",
    default="http://dron.local/nadzor/",
)

# URL MJPEG streama iz kamere na dronu; "auto" sestavi
# http://<hostname>:8090/stream.mjpg iz naslova, s katerega je prisla zahteva.
DRONE_CAMERA_URL = config("DRONE_CAMERA_URL", default="auto")

