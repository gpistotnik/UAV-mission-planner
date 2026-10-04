/** Test let — animacija drona po linearizirani poti misije. */
import L from "leaflet";
import type { MissionPayload, DronePayload } from "./types";
import { planGridClient } from "./grid_path";

export interface TestFlightInfo {
  playing: boolean;
  elapsedS: number;
  totalS: number;
  progress: number;
  totalDistanceM: number;
  multiplier: number;
  currentIdx: number;
  totalPts: number;
}

export class TestFlight {
  private marker: L.Marker | null = null;
  private pathLine: L.Polyline | null = null;
  private pts: L.LatLng[] = [];
  private segD: number[] = [];
  private totalD = 0;
  private speedMS = 5;
  private elapsedS = 0;
  private playing = false;
  private multiplier = 1;
  private lastFrame = 0;
  private rafId: number | null = null;

  constructor(
    private map: L.Map,
    private onUpdate: (info: TestFlightInfo) => void,
  ) {}

  /** Linearizira misijo (WP + razsirjeni MAP) v eno zaporedje tock. */
  loadMission(mission: MissionPayload, drones: DronePayload[]): boolean {
    this.stop();
    this.speedMS = mission.default_speed_ms || 5;
    const drone = drones.find(d => d.id === mission.drone_id);
    const pts: L.LatLng[] = [];

    for (const el of mission.elements) {
      if (el.element_type === "WP") {
        pts.push(L.latLng(el.lat, el.lon));
      } else if (drone) {
        const r = planGridClient(el, drone);
        for (const wp of r.waypoints) pts.push(wp);
      }
    }
    if (pts.length < 2) return false;

    this.pts = pts;
    this.segD = [];
    this.totalD = 0;
    for (let i = 1; i < pts.length; i++) {
      const d = pts[i - 1].distanceTo(pts[i]);
      this.segD.push(d);
      this.totalD += d;
    }
    this.elapsedS = 0;
    // Predogled celotne poti (svetlo modra polilinija)
    if (this.pathLine) this.pathLine.remove();
    this.pathLine = L.polyline(pts, {
      color: "#0ea5e9", weight: 3, opacity: 0.6, dashArray: "1,4",
    }).addTo(this.map);
    return true;
  }

  start(): void {
    if (this.pts.length < 2) return;
    if (!this.marker) {
      this.marker = L.marker(this.pts[0], {
        icon: this.makeIcon(0),
        interactive: false,
      }).addTo(this.map);
    }
    this.playing = true;
    this.lastFrame = performance.now();
    this.rafId = requestAnimationFrame(this.tick);
    this.onUpdate(this.info());
  }

  pause(): void {
    this.playing = false;
    if (this.rafId !== null) cancelAnimationFrame(this.rafId);
    this.rafId = null;
    this.onUpdate(this.info());
  }

  stop(): void {
    this.pause();
    this.elapsedS = 0;
    if (this.marker) { this.marker.remove(); this.marker = null; }
    if (this.pathLine) { this.pathLine.remove(); this.pathLine = null; }
    this.onUpdate(this.info());
  }

  setMultiplier(m: number): void {
    this.multiplier = m;
    this.onUpdate(this.info());
  }

  isRunning(): boolean { return this.pts.length >= 2; }

  private tick = (now: number): void => {
    if (!this.playing || !this.marker) return;
    const dt = (now - this.lastFrame) / 1000;
    this.lastFrame = now;
    this.elapsedS += dt * this.multiplier;

    const dist = this.elapsedS * this.speedMS;
    if (dist >= this.totalD) {
      this.marker.setLatLng(this.pts[this.pts.length - 1]);
      this.playing = false;
      this.onUpdate(this.info());
      return;
    }

    let acc = 0;
    let idx = 0;
    for (let i = 0; i < this.segD.length; i++) {
      if (dist <= acc + this.segD[i]) {
        idx = i;
        const t = this.segD[i] > 0 ? (dist - acc) / this.segD[i] : 0;
        const a = this.pts[i], b = this.pts[i + 1];
        const lat = a.lat + (b.lat - a.lat) * t;
        const lon = a.lng + (b.lng - a.lng) * t;
        this.marker.setLatLng([lat, lon]);
        const bearing = Math.atan2(b.lng - a.lng, b.lat - a.lat) * 180 / Math.PI;
        this.marker.setIcon(this.makeIcon(bearing));
        break;
      }
      acc += this.segD[i];
    }
    this.onUpdate({ ...this.info(), currentIdx: idx });
    this.rafId = requestAnimationFrame(this.tick);
  };

  private makeIcon(bearing: number): L.DivIcon {
    return L.divIcon({
      className: "test-drone",
      html: `<div class="td-arrow" style="transform: rotate(${bearing}deg)">&#x25B2;</div>`,
      iconSize: [36, 36], iconAnchor: [18, 18],
    });
  }

  info(): TestFlightInfo {
    const totalS = this.speedMS > 0 ? this.totalD / this.speedMS : 0;
    const progress = totalS > 0 ? Math.min(1, this.elapsedS / totalS) : 0;
    return {
      playing: this.playing,
      elapsedS: this.elapsedS,
      totalS,
      progress,
      totalDistanceM: this.totalD,
      multiplier: this.multiplier,
      currentIdx: 0,
      totalPts: this.pts.length,
    };
  }
}

export function formatTime(s: number): string {
  if (!isFinite(s)) return "—";
  const mm = Math.floor(s / 60);
  const ss = Math.floor(s % 60);
  return `${mm}:${ss.toString().padStart(2, "0")}`;
}
