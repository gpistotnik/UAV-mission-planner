# Načrtovalnik misij UAV

Spletni vmesnik za načrtovanje, prenos in nadzor avtonomnih misij za
platformo **F450 + Pixhawk 2.4.8 + Raspberry Pi**. Misija se sestavi v
brskalniku (waypointi, mapping mreža, parametri kamere), pretvori v
MAVLink ukaze in prek Wi-Fi pošlje neposredno na krmilnik letenja — brez
namiznih orodij kot Mission Planner ali QGroundControl.

Magistrska naloga, FRI UL, 2026. Avtor: Gašper Pistotnik.
Mentor: doc. dr. Octavian Mihai Machidon.

Javni repozitorij: <https://github.com/gpistotnik/UAV-mission-planner>.
Licenca: [MIT](LICENSE).

## Veriga »načrtuj → preveri → pošlji → zaženi → izmeri«

```
brskalnik            Django (RPi ali laptop)          Pixhawk           RPi kamera
─────────            ───────────────────────          ───────           ──────────
Leaflet + TS   ──►   MissionElement (DB)
                     linearize_mission()      ──►  zaporedje točk
                     build_mission_items()    ──►  MISSION_ITEM_INT  ──►  naloženo
                     bridge.arm/mode/start()  ──►  COMMAND_LONG      ──►  AUTO let
                     TelemetryLogger          ◄──  telemetrija 5 Hz
                                                   CAMERA_TRIGGER    ──►  zajem + EXIF
                     analysis/analyze_flight.py  ◄─ telemetry.jsonl + captures.jsonl
```

## Stanje

| Korak | Vsebina                                                | Koda | Testi |
|-------|--------------------------------------------------------|:----:|:-----:|
| 1     | Django skelet (slovenščina) + `DATABASE_URL`            | ✅  | E2E   |
| 2     | Podatkovni model (misija = zaporedje gradnikov)         | ✅  | E2E   |
| 3     | Leaflet vmesnik v TypeScript + Save API                 | ✅  | E2E   |
| 4     | Generator mapping mreže (GSD, boustrofedon)             | ✅  | 6     |
| 5     | Linearizacija gradnikov v letalne točke                 | ✅  | 8     |
| 6     | Gradnik MAVLink misije (takeoff, speed, kamera, finish) | ✅  | 17    |
| 7     | Prenos na Pixhawk + kontrola (arm / mode / start)       | ✅  | 21    |
| 8     | Telemetrija, nadzorna plošča, video stream              | ✅  | E2E   |
| 9     | Zapis telemetrije na disk (JSONL)                       | ✅  | 11    |
| 10    | Časovno sledljiv zajem slik + EXIF georeferenca         | ✅  | 4     |
| 11    | Poletna analiza in metrike validacije                   | ✅  | 30    |
| 12    | Samodejni testni polet (vzlet, lebdenje, pristanek)     | ✅  | 39    |
| 13    | Preklop Wi-Fi prek fizičnega stikala (GPIO19)           | ✅  | 24    |

Skupno **225 testov**, vsi prehajajo. ¹ `scripts/camera_trigger.py` teče na
Raspberry Pi-ju in ga pokriva integracijski test na napravi, ne enotni test;
brez kamere teče v `--dry-run` načinu, kar je uporabljeno za preverjanje
verige v SITL simulatorju.

## Hitri zagon

```bash
# 1. Backend
python -m venv .venv
.venv\Scripts\activate                  # Linux: source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt      # za teste
copy .env.example .env                   # Linux: cp

python manage.py migrate
python manage.py loaddata missions/fixtures/drones.json
python manage.py createsuperuser         # potreben za kontrolo v polju

# 2. Frontend
cd frontend
npm install
npm run build                            # ali `npm run watch`
npm run typecheck                        # tsc --noEmit

# 3. Zagon
cd ..
python manage.py runserver               # samo lokalno
python manage.py runserver 0.0.0.0:8000  # dostop iz LAN / hotspota
```

Odpri:

| Pot              | Vsebina                                         |
|------------------|-------------------------------------------------|
| `/`              | domača stran                                    |
| `/misije/`       | seznam misij                                    |
| `/misije/nova/`  | načrtovalnik (karta)                            |
| `/nadzor/`       | nadzorna plošča: HUD, kontrola, video           |
| `/test-polet/`   | samodejni testni polet (vzlet in pristanek)     |
| `/nastavitve/`   | nastavitve krmilnika, testnega poleta in zajema |
| `/prijava/`      | prijava (potrebna za kontrolo letalnika)        |
| `/admin/`        | Django admin                                    |

## Kontrola letalnika in varnost

Koncišča so razdeljena na **branje** (odprto) in **kontrolo** (zaščiteno):

| Skupina  | Koncišča                                                        |
|----------|-----------------------------------------------------------------|
| branje   | `/api/telemetry/`, `/api/missions/*/get/`, `/api/missions/*/items/`, `/api/logs/`, `/api/testflight/status/`, `/api/system/stats/`, `/api/serial-ports/`, `/api/network/status/` |
| kontrola | `/api/mavlink/{connect,disconnect,arm,mode,start}/`, `/api/missions/*/upload/`, `/api/logs/{start,stop}/`, `/api/testflight/{start,abort}/`, `/api/network/force/` |

Kontrola zahteva prijavljenega uporabnika, ko je `UAV_REQUIRE_AUTH`
vklopljen — privzeto je to `not DEBUG`, torej v polju zaprto. Brez tega bi
lahko kdorkoli v dosegu hotspota `dron-F450` armiral letalnik. Za bench
teste brez računa nastavi `UAV_REQUIRE_AUTH=False` v `.env`.

`arm`, `start` in zagon testnega poleta dodatno zahtevajo
`{"confirm": true}` v telesu zahteve, vmesnik pa pred njimi prikaže še
brskalnikov `confirm()` dialog. **Prekinitev je izjema** — `/api/testflight/abort/`
potrditve ne zahteva, ker mora biti ustavitev en sam klik. Enako velja za
`/api/network/force/` (»Preklopi zdaj«) — glej spodaj.

## Preklop Wi-Fi prek fizičnega stikala

Mehansko stikalo med GPIO19 in GND (glej `docs/magistrska/11_wifi_fallback.md`,
razdelek 11.10) omogoča preklop med klient in AP načinom brez SSH ali
reboot-a: sklenjeno na GND → klient, odklopljeno → AP. Standalone demon
(`scripts/wifi_gpio_switch.py`) bere pin in kliče `nmcli`; odločitveno
logiko (`missions/services/network_switch.py`, brez GPIO/Django
odvisnosti, 24 testov) si deli z Django strežnikom prek datotek v
`UAV_RUNTIME_DIR`.

Če je nekdo ravno aktiven na dashboard/planer/test-polet strani, se
preklop **odloži za `UAV_NETWORK_GRACE_S`** (privzeto 30 s) — v vmesniku
se prikaže pasica z odštevanjem in gumbom »Preklopi zdaj«, ki prekinitev
sproži takoj namesto da uporabnika preseneti sredi klika.

## Samodejni testni polet

`/test-polet/` izvede najpreprostejši avtonomni manever: počaka na GPS,
se dvigne na nastavljeno višino, nekaj sekund lebdi in pristane. Namen je
preveriti celotno verigo ukazov po nastavitvi krmilnika, ne leteti misije.

```
PREFLIGHT → COUNTDOWN → MODE(GUIDED) → ARM → TAKEOFF
          → CLIMB → HOVER → LAND → DISARM → DONE
```

Zaporedje je stroj stanj na **strežniku** (`missions/services/test_flight.py`),
ne v brskalniku. Če brskalnik med letom zamrzne, se osveži ali izgubi Wi-Fi,
zaporedje teče naprej in dron pripelje do pristanka.

Varovalke:

| Varovalka | Ravnanje |
|---|---|
| Predpoletne preverbe | Brez zelenega GPS fixa, dovolj satelitov, sprejemljivega HDOP-a, svežega HEARTBEAT-a in dis-armiranega letalnika se ne pošlje **noben** ukaz. |
| Timeout na vsakem koraku | Če se dron ne dvigne, ne armira ali ne dis-armira v pričakovanem času, zaporedje ukrepa namesto da bi čakalo. |
| Odpoved v zraku → pristanek | Vsaka napaka po armanju sproži `LAND`; če je `LAND` zavrnjen, sledi `RTL`. |
| Prekinitev kadarkoli | Gumb PREKINI deluje v vsaki fazi: na tleh ustavi zaporedje, v zraku sproži pristanek. Deluje tudi, kadar zaporedje ne teče, a je dron armiran. |
| Omejena višina | Strojna meja 10 m (`MAX_ALTITUDE_M`), priporočeno 2–3 m za prvi poskus. |
| Pristanek se ne prekine | Med `LAND`/`DISARM` se zahteva po prekinitvi namenoma ignorira — ustavitev pristanka bi pustila dron v zraku. |

Vse te poti pokriva 31 testov z dvojnikom krmilnika
(`missions/tests/test_testflight.py`), vključno z zavrnjenim armanjem,
dronom, ki se ne dvigne, zavrnjenim pristankom in prekinitvijo sredi
vzpenjanja — torej scenariji, ki jih na pravem dronu ni mogoče varno
sprožati. Pred prvim letom priporočam zagon proti simulatorju ArduPilot
SITL (`--connect udp:127.0.0.1:14550`).

## Zapis leta in analiza

Zapis se **samodejno začne ob vzletu** in zaključi ob pristanku
(`UAV_LOG_TRIGGER=takeoff`). Vzlet zazna iz sporočila `EXTENDED_SYS_STATE`,
kjer avtopilot sam poroča, ali je na tleh; če tega sporočila ni, pade nazaj na
višino nad vzletiščem z ločenima pragoma za vzlet (0,8 m) in pristanek (0,4 m,
ki mora vztrajati 3 s). Zaradi tega armanje brez leta — na primer test
motorjev — ne ustvari prazne seje. Z `UAV_LOG_TRIGGER=arm` dobiš širši
interval od armanja do dis-armanja, z `off` pa samo ročno proženje.

Ena seja = ena podmapa:

```
<USB>/mission-planner/flightlogs/20260725-181203_misija-3/
├── meta.json          # kdaj, kateri port, katera misija, statistika
├── plan.json          # načrtovana misija (za primerjavo v analizi)
├── telemetry.jsonl    # ena vrstica na MAVLink sporočilo, UTC žig
├── captures.jsonl     # zapisnik zajema kamere
├── images/            # JPEG-i z EXIF georeferenco
└── analysis/          # metrics.json/.md in grafi
```

Na strani **Nastavitve → Zajem in shranjevanje** aplikacija izpiše vse USB
particije. Uporabnik izbere eno; root-owned helper jo mounta na
`/mnt/uav-data` in z UUID-jem trajno doda v `/etc/fstab`. Če izbrani medij ni
na voljo, aplikacija deluje naprej, vendar ne zapisuje telemetrije ali slik.
Lokalnega fallbacka ni. SQLite, runtime stanje Wi-Fi, koda in systemd journal
ostanejo na SD kartici.

Po letu (analizo zaženi neposredno nad sejo na USB):

```bash
pip install -r requirements-analysis.txt
python analysis/analyze_flight.py \
    /media/dron/USB/mission-planner/flightlogs/20260725-181203_misija-3
```

Izhod: `metrics.json`, `metrics.md` (tabela za poglavje Rezultati) in grafi
`track.png`, `crosstrack.png`, `altitude.png`, `capture.png`.

### Povezava s krmilnikom

Na dronu se strežnik s Pixhawkom poveže **sam ob zagonu** — nihče ne odpre
brskalnika, da bi kliknil »Poveži«, najbolj zanimive pa so prve sekunde po
vklopu. Nit poskuša z naraščajočim zamikom, dokler ne uspe, in se ne vmešava,
kadar je most že povezan.

| Nastavitev | Privzeto | Pomen |
|---|---|---|
| `UAV_AUTOCONNECT` | `not DEBUG` | v razvoju izklopljeno, na dronu vklopljeno |
| `UAV_SERIAL_DEVICE` | `auto` | `/dev/serial0` → `ttyACM*` → `ttyUSB*`; lahko tudi `udp:127.0.0.1:14551` za SITL |
| `UAV_SERIAL_BAUD` | `921600` | za TELEM2 prek GPIO UART |

Povezovanje se namenoma **ne** zgodi pri ukazih kot `migrate`, `test` ali
`collectstatic`, in pri `runserver` samo v delovnem procesu — sicer bi
avtomatski ponovni zagon odprl ista serijska vrata dvakrat.

## Na dronu (Raspberry Pi)

```bash
scripts/bootstrap_rpi.sh          # sistemska priprava
scripts/install_app.sh            # namestitev aplikacije + systemd
sudo apt install -y python3-picamera2
pip install -r requirements-rpi.txt

sudo cp scripts/systemd/*.service /etc/systemd/system/
sudo systemctl enable --now drone-camera drone-camera-trigger drone-wifi-fallback
# Ce je vgrajeno fizicno stikalo (11.10 v docs/magistrska/11_wifi_fallback.md):
#   sudo systemctl enable --now drone-wifi-gpio-switch
#   sudo systemctl disable --now drone-wifi-fallback   # ne oba hkrati
```

Servisi: `drone-camera` (MJPEG stream za nadzorno ploščo),
`drone-camera-trigger` (časovno sledljiv zajem), `drone-wifi-fallback`
(časovni preklop klient → AP ob boot-u), `drone-wifi-gpio-switch`
(preklop prek fizičnega stikala — glej zgoraj, ne poganjaj z
`drone-wifi-fallback` hkrati), `drone-captive-portal`.

USB izberi v GUI na `/nastavitve/`. Diagnostika:

```bash
lsblk -o NAME,TRAN,RM,FSTYPE,MOUNTPOINTS
sudo -u dron .venv/bin/python -m core.storage
```

Za običajno delovanje pusti `UAV_STORAGE_ROOT=auto`; izbrani medij ima
prednost tudi, če je priklopljenih več USB diskov.

Offline karta Slovenije (OSM na SD, ne v brskalniku) — enkrat z internetom:

```bash
python scripts/download_slovenia_tiles.py          # ~z7–z14, ~0.5 GB → data/tiles/
# ali na PC in nato: rsync -av data/tiles/ dron@dron.local:~/mission_planner/data/tiles/
```

Planner jih bere z `/tiles/osm/{z}/{x}/{y}.png` (deluje tudi v AP načinu).

## Izbira podatkovne baze

Nastavi `DATABASE_URL` v `.env`:

| Engine     | Connection string                                | Gonilnik                            |
|------------|--------------------------------------------------|-------------------------------------|
| SQLite     | `sqlite:///./db.sqlite3` *(privzeto)*            | vgrajen                             |
| PostgreSQL | `postgres://user:pass@host:5432/missionplanner`  | `requirements-postgres.txt`         |
| MariaDB    | `mysql://user:pass@host:3306/missionplanner`     | `requirements-mariadb.txt`          |

## Struktura

```
mission_planner/
├── manage.py
├── requirements.txt              # jedro
├── requirements-dev.txt          # pytest
├── requirements-analysis.txt     # numpy, matplotlib
├── requirements-rpi.txt          # piexif, gpiozero (picamera2 prek apt)
├── requirements-{postgres,mariadb}.txt
├── missionplanner/               # settings + root URLs
├── core/                         # domača stran, nadzorna plošča, prijava
├── missions/
│   ├── models.py                 # DroneProfile, Mission, MissionElement + 7 enumov
│   ├── views.py                  # HTML + JSON API + kontrola
│   ├── decorators.py             # control_required, require_confirm
│   ├── services/
│   │   ├── grid_planner.py       # generator vzporednih prog (čist)
│   │   ├── linearize.py          # gradniki → letalne točke (čist)
│   │   ├── mavlink_mission.py    # točke → MISSION_ITEM_INT (čist, brez pymavlink)
│   │   ├── mavlink_bridge.py     # povezava, telemetrija, prenos, ukazi
│   │   ├── telemetry_log.py      # zapis JSONL + pregled sej
│   │   ├── test_flight.py        # stroj stanj: vzlet, lebdenje, pristanek
│   │   ├── network_switch.py     # preklop Wi-Fi: čista logika + koordinacija (čist)
│   │   └── system_stats.py       # CPU, RAM, temp, throttled
│   └── tests/                    # 9 testnih datotek
├── frontend/src/                 # TypeScript + esbuild
│   ├── main.ts  map.ts  tools.ts  sidebar.ts  state.ts  waypoints.ts
│   ├── grid.ts  grid_path.ts  map_layers.ts  test_flight.ts  api.ts  types.ts
│   ├── telemetry.ts              # HUD, marker, sled, napredek misije
│   ├── control.ts                # arm / mode / start + panel zapisa
│   ├── auto_test.ts              # stran samodejnega testnega poleta
│   └── network_banner.ts         # pasica za odloženi preklop Wi-Fi
├── analysis/
│   ├── metrics.py                # metrike (čiste funkcije, brez odvisnosti)
│   ├── analyze_flight.py         # CLI: metrics.json + metrics.md + grafi
│   └── test_metrics.py
├── scripts/
│   ├── camera_stream.py          # MJPEG stream za nadzorno ploščo
│   ├── camera_trigger.py         # časovno sledljiv zajem + EXIF
│   ├── wifi_gpio_switch.py       # preklop Wi-Fi prek fizičnega stikala (GPIO19)
│   ├── bootstrap_rpi.sh  install_app.sh  update.sh
│   ├── setup_ap.sh  wifi_fallback.sh  add_wifi.sh  captive_portal.py
│   └── systemd/                  # 5 unit datotek
├── templates/  static/
├── thesis.tex                    # besedilo naloge
├── bibliography.bib              # viri, citirani v thesis.tex
└── docs/magistrska/              # delovno besedilo (glej README.md v mapi)
```

## Zagon testov

```bash
pip install -r requirements-dev.txt
python -m pytest                 # missions/tests + analysis
python -m pytest -k mavlink      # samo MAVLink
python manage.py check
cd frontend && npm run typecheck
```

Kaj je pokrito: generator mreže in linearizacija (geometrija), gradnik
MAVLink misije (zaporedje ukazov), protokol prenosa in ukazov prek
dvojnika krmilnika (vključno z zavrnitvijo, timeoutom in ponovljeno
zahtevo), zapisovalnik telemetrije, metrike analize (na sintetičnih
podatkih z znanim odgovorom) in **vsako HTTP koncišče**.
