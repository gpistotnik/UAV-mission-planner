# Poletna analiza

Cevovod od surovega zapisa leta do tabele in grafov za poglavje Rezultati.

## Vhod

Mapa seje na USB ključku, kot jo ustvari zapisovalnik telemetrije:

```
<USB>/mission-planner/flightlogs/20260725-181203_misija-3/
├── meta.json          # kdaj, kateri port, katera misija, statistika
├── plan.json          # načrtovana misija (zapisana ob začetku seje)
├── telemetry.jsonl    # ena vrstica na MAVLink sporočilo
├── captures.jsonl     # zapisnik zajema kamere
└── images/            # JPEG-i z EXIF georeferenco
```

`captures.jsonl` in slike nastanejo neposredno v aktivni seji prek
`scripts/camera_trigger.py`. Analiza deluje tudi brez zajemov — takrat metrika
točnosti zajema ostane prazna.

## Zagon

```bash
pip install -r ../requirements-analysis.txt

# en let
python analyze_flight.py ../flightlogs/20260725-181203_misija-3

# vsi leti + zbirna tabela kampanje
python analyze_flight.py ../flightlogs/*/ --campaign-out ../kampanja.md

# brez grafov (hitro, npr. na RPi)
python analyze_flight.py ../flightlogs/*/ --no-plots
```

## Izhod

V `<seja>/analysis/`:

| Datoteka         | Vsebina                                                    |
|------------------|------------------------------------------------------------|
| `metrics.json`   | vse metrike strojno berljivo                               |
| `metrics.md`     | tabela za neposredno vstavitev v besedilo naloge           |
| `track.png`      | načrtovana in dejanska pot + lokacije posnetkov            |
| `crosstrack.png` | odstopanje od poti v odvisnosti od časa, z označenim RMS   |
| `altitude.png`   | profil višine proti načrtovani                             |
| `capture.png`    | histogram odstopanja lokacij posnetkov                     |

## Metrike

| Metrika                             | Funkcija v `metrics.py`     | Enota |
|-------------------------------------|-----------------------------|-------|
| natančnost sledenja trajektorijam   | `cross_track_stats`         | m     |
| odstopanje višine                   | `altitude_stats`            | m     |
| doslednost telemetrije              | `telemetry_rate_stats`      | ms/Hz |
| zanesljivost izvedbe misije         | `mission_completion`        | %     |
| točnost zajema + zakasnitev         | `capture_accuracy`          | m, ms |

Odstopanje od trajektorije je razdalja do **poti** (poligonalne črte med
načrtovanimi točkami), ne do najbližje točke. V izračun vstopajo samo
pozicije med armanjem in nad 1 m višine (`filter_airborne`), sicer bi RMS
kvarile pozicije z tal.

## Zasnova

`metrics.py` ne uvaža ne `numpy` ne `matplotlib` ne `pyproj` — vse metrike so
osnovna matematika. Zato so enotno testljive brez dodatnih paketov in jih je
mogoče pognati tudi na dronu. Risanje je ločeno v `analyze_flight.py`; če
`matplotlib` ni namenjen, se grafi preskočijo, metrike pa se vseeno izračunajo.

Projekcija je ekvirektangularna okoli izhodišča (`LocalPlane`). Na območju
nekaj sto metrov je njena napaka pod milimetrom, torej dva velikostna razreda
pod natančnostjo GPS brez RTK.

## Testi

```bash
python -m pytest test_metrics.py -v
```

25 testov na sintetičnih podatkih z znanim odgovorom — npr. sled, vzporedna z
načrtovano črto 3 m stran, mora dati RMS natanko 3 m.
