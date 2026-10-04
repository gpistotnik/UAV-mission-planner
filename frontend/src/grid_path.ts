/** Client-side generator grid poti (brez HTTP latence).
 *
 * Algoritem je enak kot v ``missions/services/grid_planner.py``, le da
 * dela v lokalnem equirectangular prostoru (cos(lat) korekcija) namesto
 * v UTM. Za poligone velikosti nekaj sto metrov je natancnost popolnoma
 * dovolj za zivi predogled; koncno pot za krmilnik vedno generira backend.
 */
import L from "leaflet";
import type { DronePayload, MapElement } from "./types";

const M_PER_DEG_LAT = 111_320;
const ε = 1e-9;

interface Pt { x: number; y: number; }

function projectToLocal(coords: number[][]): { pts: Pt[]; cx: number; cy: number; mLat: number; mLon: number } {
  const cx = coords.reduce((s, p) => s + p[0], 0) / coords.length;
  const cy = coords.reduce((s, p) => s + p[1], 0) / coords.length;
  const mLon = M_PER_DEG_LAT * Math.cos((cy * Math.PI) / 180);
  const pts = coords.map(([lon, lat]) => ({
    x: (lon - cx) * mLon,
    y: (lat - cy) * M_PER_DEG_LAT,
  }));
  return { pts, cx, cy, mLat: M_PER_DEG_LAT, mLon };
}

function unproject(p: Pt, cx: number, cy: number, mLat: number, mLon: number): L.LatLng {
  return L.latLng(cy + p.y / mLat, cx + p.x / mLon);
}

function rotate(p: Pt, deg: number): Pt {
  const r = (deg * Math.PI) / 180;
  const c = Math.cos(r), s = Math.sin(r);
  return { x: p.x * c - p.y * s, y: p.x * s + p.y * c };
}

function bbox(pts: Pt[]): [number, number, number, number] {
  let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
  for (const p of pts) {
    if (p.x < minx) minx = p.x;
    if (p.y < miny) miny = p.y;
    if (p.x > maxx) maxx = p.x;
    if (p.y > maxy) maxy = p.y;
  }
  return [minx, miny, maxx, maxy];
}

/** Vrne presek vodoravne crte y=y0 z poligonom kot urejen seznam x-presekov. */
function horizontalIntersections(poly: Pt[], y0: number): number[] {
  const xs: number[] = [];
  for (let i = 0; i < poly.length; i++) {
    const a = poly[i];
    const b = poly[(i + 1) % poly.length];
    if (Math.abs(a.y - b.y) < ε) continue;
    const tmin = Math.min(a.y, b.y), tmax = Math.max(a.y, b.y);
    if (y0 < tmin - ε || y0 > tmax + ε) continue;
    const t = (y0 - a.y) / (b.y - a.y);
    xs.push(a.x + t * (b.x - a.x));
  }
  return xs.sort((u, v) => u - v);
}

export interface GridParams {
  altitudeM: number;
  sideOverlapPct: number;
  frontOverlapPct: number;
  trackAngleDeg: number;
  pattern: "GRID" | "CROSSHATCH";
}

export interface GridResult {
  waypoints: L.LatLng[];
  lineSpacingM: number;
  triggerDistanceM: number;
  gsdCmPerPx: number;
  footprintAcrossM: number;
  footprintAlongM: number;
  totalDistanceM: number;
  numLines: number;
}

export function planGridClient(map: MapElement, drone: DronePayload): GridResult {
  const params: GridParams = {
    altitudeM: map.altitude_m,
    sideOverlapPct: map.side_overlap_pct,
    frontOverlapPct: map.front_overlap_pct,
    trackAngleDeg: map.track_angle_deg,
    pattern: map.pattern,
  };
  // Tloris ene slike (m) — daljsa stranica preko, krajsa vzdolz.
  const fw = (drone.sensor_width_mm / drone.focal_length_mm) * params.altitudeM;
  const fh = (drone.sensor_height_mm / drone.focal_length_mm) * params.altitudeM;
  const across = Math.max(fw, fh);
  const along = Math.min(fw, fh);
  const lineSpacing = across * (1 - params.sideOverlapPct / 100);
  const triggerDist = along * (1 - params.frontOverlapPct / 100);
  const gsd = (drone.sensor_width_mm * params.altitudeM * 100) /
              (drone.focal_length_mm * drone.image_width_px);

  const coords = map.polygon_geojson.coordinates[0];
  const closed = coords[coords.length - 1][0] === coords[0][0] &&
                 coords[coords.length - 1][1] === coords[0][1];
  const ring = closed ? coords.slice(0, -1) : coords.slice();
  if (ring.length < 3 || lineSpacing <= 0 || triggerDist <= 0) {
    return {
      waypoints: [], lineSpacingM: lineSpacing, triggerDistanceM: triggerDist,
      gsdCmPerPx: gsd, footprintAcrossM: across, footprintAlongM: along,
      totalDistanceM: 0, numLines: 0,
    };
  }

  const proj = projectToLocal(ring);

  // Za vsak kot (en za GRID, dva za CROSSHATCH) izracunaj proge in jih dodaj.
  const angles = params.pattern === "CROSSHATCH"
    ? [params.trackAngleDeg, params.trackAngleDeg + 90]
    : [params.trackAngleDeg];

  const allLocal: Pt[] = [];
  let numLines = 0;

  for (const alpha of angles) {
    const rotated = proj.pts.map(p => rotate(p, -alpha));
    const [minx, miny, maxx, maxy] = bbox(rotated);
    let y = miny + lineSpacing / 2;
    let lineIdx = 0;
    while (y <= maxy + ε) {
      const xs = horizontalIntersections(rotated, y);
      // Vzemi pare (vstop, izstop) sosednjih presekov.
      for (let i = 0; i + 1 < xs.length; i += 2) {
        const ax = xs[i], bx = xs[i + 1];
        let p1: Pt, p2: Pt;
        if (lineIdx % 2 === 0) { p1 = { x: ax, y }; p2 = { x: bx, y }; }
        else                    { p1 = { x: bx, y }; p2 = { x: ax, y }; }
        // Rotacija nazaj v poligonski referencni sistem
        allLocal.push(rotate(p1, alpha), rotate(p2, alpha));
        numLines++;
      }
      y += lineSpacing;
      lineIdx++;
    }
  }

  const waypoints = allLocal.map(p => unproject(p, proj.cx, proj.cy, proj.mLat, proj.mLon));
  let total = 0;
  for (let i = 1; i < allLocal.length; i++) {
    total += Math.hypot(allLocal[i].x - allLocal[i - 1].x,
                        allLocal[i].y - allLocal[i - 1].y);
  }

  return {
    waypoints, lineSpacingM: lineSpacing, triggerDistanceM: triggerDist,
    gsdCmPerPx: gsd, footprintAcrossM: across, footprintAlongM: along,
    totalDistanceM: total, numLines,
  };
}
