/** Risanje gradnikov na karto + interaktivno urejanje. */
import L from "leaflet";
import type { MissionElement, WaypointElement, MapElement, DronePayload } from "./types";
import { PlannerState } from "./state";
import { planGridClient } from "./grid_path";
import { invalidateDetail, showMapStats } from "./sidebar";

const WP_COLOR = "#1f6feb";
const MAP_COLOR = "#d97706";
const SEQ_COLOR = "#6b7280";
const PATH_COLOR = "#10b981";

/** Večji markerji na touch napravah (min. ~44 px zadetek). */
const TOUCH = L.Browser.touch;
const WP_SIZE = TOUCH ? 44 : 32;
const VERTEX_SIZE = TOUCH ? 40 : 26;
const EDGE_SIZE = TOUCH ? 36 : 22;

export class ElementLayer {
  private wpMarkers: Map<number, L.Marker> = new Map();
  private mapPolygons: Map<number, L.Polygon> = new Map();
  private mapHandles: Map<number, L.LayerGroup> = new Map();
  private mapPaths: Map<number, L.Polyline> = new Map();
  private sequencePolyline: L.Polyline;

  constructor(
    private map: L.Map,
    private state: PlannerState,
    private drones: DronePayload[],
  ) {
    this.sequencePolyline = L.polyline([], {
      color: SEQ_COLOR, weight: 2, dashArray: "4,6", opacity: 0.7,
    }).addTo(map);
    this.redrawAll();
  }

  redrawAll(): void {
    const liveIds = new Set(this.state.mission.elements.map(e => e.id!));
    for (const [id, m] of this.wpMarkers)   if (!liveIds.has(id)) { m.remove(); this.wpMarkers.delete(id); }
    for (const [id, p] of this.mapPolygons) if (!liveIds.has(id)) { p.remove(); this.mapPolygons.delete(id); }
    for (const [id, h] of this.mapHandles)  if (!liveIds.has(id)) { h.remove(); this.mapHandles.delete(id); }
    for (const [id, l] of this.mapPaths)    if (!liveIds.has(id)) { l.remove(); this.mapPaths.delete(id); }

    for (const e of this.state.mission.elements) {
      if (e.element_type === "WP") this.drawWP(e);
      else this.drawMap(e);
    }
    this.updateSequence();
  }

  // -------------------------------------------------------------------------
  private drawWP(wp: WaypointElement): void {
    const existing = this.wpMarkers.get(wp.id!);
    const isSel = wp.id === this.state.selectedId;
    const icon = L.divIcon({
      className: "wp-marker",
      html: `<div class="wp-num ${isSel?"selected":""}">${wp.order}</div>`,
      iconSize: [WP_SIZE, WP_SIZE],
      iconAnchor: [WP_SIZE / 2, WP_SIZE / 2],
    });
    if (existing) {
      existing.setLatLng([wp.lat, wp.lon]);
      existing.setIcon(icon);
      return;
    }
    const m = L.marker([wp.lat, wp.lon], { icon, draggable: true }).addTo(this.map);
    m.on("drag", () => {
      const ll = m.getLatLng();
      wp.lat = ll.lat; wp.lon = ll.lng;
      this.updateSequence();
    });
    m.on("dragend", () => this.state.markDirty());
    m.on("click", () => this.state.select(wp.id!));
    this.wpMarkers.set(wp.id!, m);
  }

  // -------------------------------------------------------------------------
  private drawMap(me: MapElement): void {
    const ring = me.polygon_geojson.coordinates[0];
    const latlngs = ring.slice(0, -1).map(([lon, lat]) => L.latLng(lat, lon));

    const isSel = me.id === this.state.selectedId;
    const style: L.PathOptions = {
      color: MAP_COLOR, weight: 2,
      fillColor: isSel ? "#fbbf24" : "#fde68a",
      fillOpacity: isSel ? 0.25 : 0.15,
    };

    let poly = this.mapPolygons.get(me.id!);
    if (poly) { poly.setLatLngs(latlngs); poly.setStyle(style); }
    else {
      poly = L.polygon(latlngs, style).addTo(this.map);
      poly.on("click", () => this.state.select(me.id!));
      this.mapPolygons.set(me.id!, poly);
    }

    // Rocice samo ce je izbran
    let group = this.mapHandles.get(me.id!);
    if (group) group.clearLayers();
    else {
      group = L.layerGroup();
      this.mapHandles.set(me.id!, group);
    }
    if (isSel) {
      group.addTo(this.map);
      this.attachVertexHandles(me, group);
      this.attachEdgePlusHandles(me, group);
    } else {
      group.remove();
    }

    // Grid pot (zelena polilinija znotraj poligona)
    if (!this.mapPaths.has(me.id!)) {
      const line = L.polyline([], { color: PATH_COLOR, weight: 2, opacity: 0.9 }).addTo(this.map);
      this.mapPaths.set(me.id!, line);
    }
    this.refreshGridPathLive(me);
  }

  /** Vlecljive oštevilčene vogalne ročice — *brez* `redrawAll` med vlekom. */
  private attachVertexHandles(me: MapElement, group: L.LayerGroup): void {
    const ring = me.polygon_geojson.coordinates[0];
    const latlngs = ring.slice(0, -1).map(([lon, lat]) => L.latLng(lat, lon));

    latlngs.forEach((ll, i) => {
      const marker = L.marker(ll, {
        draggable: true,
        zIndexOffset: 600,
        icon: L.divIcon({
          className: "map-vertex-marker",
          html: `<div class="map-vertex-num">${i + 1}</div>`,
          iconSize: [VERTEX_SIZE, VERTEX_SIZE],
          iconAnchor: [VERTEX_SIZE / 2, VERTEX_SIZE / 2],
        }),
      }).addTo(group);

      marker.on("drag", () => {
        const ll2 = marker.getLatLng();
        // Posodobi *samo* poligonsko geometrijo in pot — NE redrawAll.
        ring[i] = [ll2.lng, ll2.lat];
        if (i === 0) ring[ring.length - 1] = [ll2.lng, ll2.lat];
        const newLatlngs = ring.slice(0, -1).map(([lon, lat]) => L.latLng(lat, lon));
        this.mapPolygons.get(me.id!)?.setLatLngs(newLatlngs);
        this.updateEdgePlusPositions(me);
        this.refreshGridPathLive(me);
      });
      marker.on("dragend", () => {
        invalidateDetail();
        this.state.markDirty();
      });
      marker.on("click", (ev) => {
        L.DomEvent.stopPropagation(ev);
        this.state.select(me.id!);
      });
    });
  }

  private edgePlusMarkers: Map<number, L.Marker[]> = new Map();

  /** + ročice na sredinah robov (klik vstavi nov vogal). */
  private attachEdgePlusHandles(me: MapElement, group: L.LayerGroup): void {
    const ring = me.polygon_geojson.coordinates[0];
    const latlngs = ring.slice(0, -1).map(([lon, lat]) => L.latLng(lat, lon));
    const markers: L.Marker[] = [];
    for (let i = 0; i < latlngs.length; i++) {
      const a = latlngs[i];
      const b = latlngs[(i + 1) % latlngs.length];
      const mid = L.latLng((a.lat + b.lat) / 2, (a.lng + b.lng) / 2);
      const plus = L.marker(mid, {
        icon: L.divIcon({
          className: "edge-plus",
          html: "+",
          iconSize: [EDGE_SIZE, EDGE_SIZE],
          iconAnchor: [EDGE_SIZE / 2, EDGE_SIZE / 2],
        }),
      }).addTo(group);
      const insertIdx = i + 1;
      plus.on("click", (ev) => {
        L.DomEvent.stopPropagation(ev.originalEvent);
        ring.splice(insertIdx, 0, [mid.lng, mid.lat]);
        if (ring[ring.length - 1][0] !== ring[0][0] ||
            ring[ring.length - 1][1] !== ring[0][1]) {
          ring.push([ring[0][0], ring[0][1]]);
        }
        invalidateDetail();
        this.state.markDirty();
        this.redrawAll();
      });
      markers.push(plus);
    }
    this.edgePlusMarkers.set(me.id!, markers);
  }

  /** Med vlekom: samo prestavi + markerje, ne pa unici. */
  private updateEdgePlusPositions(me: MapElement): void {
    const markers = this.edgePlusMarkers.get(me.id!);
    if (!markers) return;
    const ring = me.polygon_geojson.coordinates[0];
    const latlngs = ring.slice(0, -1).map(([lon, lat]) => L.latLng(lat, lon));
    for (let i = 0; i < latlngs.length && i < markers.length; i++) {
      const a = latlngs[i];
      const b = latlngs[(i + 1) % latlngs.length];
      markers[i].setLatLng([(a.lat + b.lat) / 2, (a.lng + b.lng) / 2]);
    }
  }

  private updateSequence(): void {
    const pts: L.LatLngExpression[] = this.state.mission.elements.map(e => {
      if (e.element_type === "WP") return [e.lat, e.lon];
      const r = e.polygon_geojson.coordinates[0].slice(0, -1);
      const cx = r.reduce((s, p) => s + p[1], 0) / r.length;
      const cy = r.reduce((s, p) => s + p[0], 0) / r.length;
      return [cx, cy];
    });
    this.sequencePolyline.setLatLngs(pts);
  }

  /** Klient-side preracun grid poti (sinhroni, brez HTTP) za zivo posodobitev. */
  private refreshGridPathLive(me: MapElement): void {
    const line = this.mapPaths.get(me.id!);
    if (!line) return;
    const drone = this.drones.find(d => d.id === this.state.mission.drone_id);
    if (!drone) { line.setLatLngs([]); return; }
    try {
      const result = planGridClient(me, drone);
      line.setLatLngs(result.waypoints);
      if (me.id === this.state.selectedId) {
        showMapStats({
          gsd_cm_per_px: Math.round(result.gsdCmPerPx * 1000) / 1000,
          line_spacing_m: Math.round(result.lineSpacingM * 100) / 100,
          trigger_distance_m: Math.round(result.triggerDistanceM * 100) / 100,
          total_distance_m: Math.round(result.totalDistanceM * 10) / 10,
          num_lines: result.numLines,
          estimated_images: result.triggerDistanceM > 0
            ? Math.ceil(result.totalDistanceM / result.triggerDistanceM) : 0,
        });
      }
    } catch (e) {
      console.warn("grid path:", e);
    }
  }
}
