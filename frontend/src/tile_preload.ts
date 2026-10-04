/** Meje Slovenije + URL lokalnih ploščic na RPi. */

/** Meje Slovenije (WGS84) z majhnim robom. */
export const SLOVENIA_BOUNDS = {
  south: 45.42,
  west: 13.38,
  north: 46.88,
  east: 16.61,
} as const;

/** Lokalne OSM ploščice s strežnika (data/tiles/osm na RPi).
 *  ``?v=`` zamenja brskalniški cache po zamenjavi vira (npr. Carto → OSM.de). */
export const LOCAL_OSM_URL = "/tiles/osm/{z}/{x}/{y}.png?v=osmde1";

export type TilesStatus = {
  available: boolean;
  partial?: boolean;
  poisoned?: boolean;
  provider?: string;
  min_zoom?: number | null;
  max_zoom?: number | null;
  fetch_on_miss?: boolean;
  fetch_max_zoom?: number | null;
  error?: string;
};

/** Preveri, ali so ploščice naložene na RPi. */
export async function fetchTilesStatus(): Promise<TilesStatus> {
  try {
    const r = await fetch("/tiles/status/", { credentials: "same-origin" });
    if (!r.ok) return { available: false };
    return (await r.json()) as TilesStatus;
  } catch {
    return { available: false };
  }
}
