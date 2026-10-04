/** Inicializacija Leaflet karte — lokalne ploščice; višji zoom on-demand. */
import L from "leaflet";
import {
  LOCAL_OSM_URL,
  SLOVENIA_BOUNDS,
  fetchTilesStatus,
} from "./tile_preload";

/** Privzeti zoom prednaloženega paketa (download_slovenia_tiles.py). */
const DEFAULT_NATIVE_MIN_Z = 7;
const DEFAULT_NATIVE_MAX_Z = 14;
/** Max zoom: manjkajoče z15+ grejo prek RPi (302 → OSM) + cache. */
const MAX_VIEW_ZOOM = 20;

function showTilesHint(map: L.Map, text: string): void {
  const el = L.DomUtil.create("div", "map-preload-badge");
  el.textContent = text;
  const ctrl = new (L.Control.extend({
    onAdd() { return el; },
  }))({ position: "bottomleft" });
  ctrl.addTo(map);
}

/** Meje karte: Slovenija + rob, da ne odplava v ocean. */
export function sloveniaLatLngBounds(): L.LatLngBounds {
  const b = SLOVENIA_BOUNDS;
  return L.latLngBounds([b.south, b.west], [b.north, b.east]);
}

export function createMap(elId: string, centerLat: number, centerLon: number): L.Map {
  const si = sloveniaLatLngBounds().pad(0.08);
  const map = L.map(elId, {
    zoomControl: true,
    maxBounds: si,
    maxBoundsViscosity: 0.85,
    minZoom: DEFAULT_NATIVE_MIN_Z,
    maxZoom: MAX_VIEW_ZOOM,
    bounceAtZoomLimits: true,
  }).setView([centerLat, centerLon], DEFAULT_NATIVE_MAX_Z);

  if (!si.contains(map.getCenter())) {
    map.fitBounds(sloveniaLatLngBounds(), { maxZoom: 10 });
  }

  // Ob miss RPi takoj 302 → OSM.de (vzporedno v brskalniku), lokalni paket
  // ostane hitra pot. Overzoom z14, če internet/ploščica odpove.
  const localOsm = L.tileLayer(LOCAL_OSM_URL, {
    minZoom: DEFAULT_NATIVE_MIN_Z,
    maxZoom: MAX_VIEW_ZOOM,
    minNativeZoom: DEFAULT_NATIVE_MIN_Z,
    maxNativeZoom: MAX_VIEW_ZOOM,
    attribution: "© OpenStreetMap (lokalno + on-demand)",
    errorTileUrl: "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7",
  }).addTo(map);

  // Če več zaporednih napak (offline), začasno overzoomaj lokalni paket.
  let errStreak = 0;
  localOsm.on("tileerror", () => {
    errStreak += 1;
    if (errStreak >= 4 && (localOsm.options.maxNativeZoom ?? 0) > DEFAULT_NATIVE_MAX_Z) {
      localOsm.options.maxNativeZoom = DEFAULT_NATIVE_MAX_Z;
      localOsm.redraw();
    }
  });
  localOsm.on("tileload", () => {
    errStreak = 0;
  });

  void (async () => {
    const tiles = await fetchTilesStatus();
    if (tiles.available) {
      if (tiles.partial) {
        showTilesHint(map, "Prenos lokalne karte še poteka…");
      }
      return;
    }
    // Brez paketa: ostani na on-demand do fetch_max, sicer soft overzoom.
    localOsm.options.maxNativeZoom = tiles.fetch_on_miss
      ? (tiles.fetch_max_zoom ?? MAX_VIEW_ZOOM)
      : DEFAULT_NATIVE_MAX_Z;
    localOsm.redraw();
    const hint = tiles.poisoned
      ? "Lokalne ploščice neveljavne — poteka ponovni prenos"
      : "Ni lokalne karte — počakaj na konec prenosa na RPi";
    showTilesHint(map, hint);
  })();

  return map;
}
