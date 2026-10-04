/** Orodja za dodajanje gradnikov: klik=WP, drag/touch=MAP. */
import L from "leaflet";
import type { WaypointElement, MapElement } from "./types";
import { PlannerState } from "./state";

function eventLatLng(map: L.Map, ev: MouseEvent | TouchEvent | PointerEvent): L.LatLng | null {
  let clientX: number;
  let clientY: number;
  if ("touches" in ev && ev.touches.length > 0) {
    clientX = ev.touches[0].clientX;
    clientY = ev.touches[0].clientY;
  } else if ("changedTouches" in ev && ev.changedTouches.length > 0) {
    clientX = ev.changedTouches[0].clientX;
    clientY = ev.changedTouches[0].clientY;
  } else if ("clientX" in ev) {
    clientX = ev.clientX;
    clientY = ev.clientY;
  } else {
    return null;
  }
  const pt = map.mouseEventToContainerPoint({ clientX, clientY } as MouseEvent);
  return map.containerPointToLatLng(pt);
}

export function attachTools(map: L.Map, state: PlannerState): void {
  const container = map.getContainer();
  let dragStart: L.LatLng | null = null;
  let dragRect: L.Rectangle | null = null;
  let drawing = false;
  let pointerId: number | null = null;

  map.on("click", (e: L.LeafletMouseEvent) => {
    if (state.toolMode === "add_wp") {
      const wp: WaypointElement = {
        order: 0, element_type: "WP", name: "",
        altitude_m: state.mission.default_altitude_m,
        speed_ms: null,
        lat: e.latlng.lat, lon: e.latlng.lng,
        heading_mode: "AUTO", heading_deg: null,
        gimbal_pitch_deg: -90, path_style: "STRAIGHT",
        action_type: "PHOTO", hover_time_s: 0,
      };
      state.addElement(wp);
      state.setToolMode("select");
    }
  });

  const finishDraw = (end: L.LatLng | null) => {
    if (!dragStart) return;
    const start = dragStart;
    if (dragRect) { dragRect.remove(); dragRect = null; }
    dragStart = null;
    drawing = false;
    pointerId = null;
    map.dragging.enable();
    map.touchZoom.enable();

    if (!end) {
      state.setToolMode("select");
      return;
    }
    const bounds = L.latLngBounds(start, end);
    if (bounds.getSouthWest().distanceTo(bounds.getNorthEast()) < 10) {
      state.setToolMode("select");
      return;
    }
    const sw = bounds.getSouthWest(), ne = bounds.getNorthEast();
    const me: MapElement = {
      order: 0, element_type: "MAP", name: "",
      altitude_m: state.mission.default_altitude_m,
      speed_ms: null,
      polygon_geojson: { type: "Polygon", coordinates: [[
        [sw.lng, sw.lat], [ne.lng, sw.lat],
        [ne.lng, ne.lat], [sw.lng, ne.lat],
        [sw.lng, sw.lat],
      ]]},
      pattern: "GRID",
      front_overlap_pct: 75, side_overlap_pct: 75, track_angle_deg: 0,
    };
    state.addElement(me);
    state.setToolMode("select");
  };

  const onPointerDown = (ev: PointerEvent) => {
    if (state.toolMode !== "add_map") return;
    // Samo primarni kazalec (en prst / levi gumb) — dva prsta naj ostaneta za zoom.
    if (!ev.isPrimary) return;
    const t = ev.target as Element | null;
    if (t?.closest?.(".leaflet-control, .leaflet-popup, button, a, input, select, textarea")) return;
    const ll = eventLatLng(map, ev);
    if (!ll) return;
    drawing = true;
    pointerId = ev.pointerId;
    dragStart = ll;
    map.dragging.disable();
    map.touchZoom.disable();
    try { container.setPointerCapture?.(ev.pointerId); } catch { /* */ }
    ev.preventDefault();
  };

  const onPointerMove = (ev: PointerEvent) => {
    if (!drawing || !dragStart) return;
    if (pointerId !== null && ev.pointerId !== pointerId) return;
    const ll = eventLatLng(map, ev);
    if (!ll) return;
    const bounds = L.latLngBounds(dragStart, ll);
    if (!dragRect) {
      dragRect = L.rectangle(bounds, {
        color: "#d97706", weight: 2, fillColor: "#fbbf24", fillOpacity: 0.25,
        dashArray: "4,4",
      }).addTo(map);
    } else {
      dragRect.setBounds(bounds);
    }
    ev.preventDefault();
  };

  const onPointerUp = (ev: PointerEvent) => {
    if (!drawing || !dragStart) return;
    if (pointerId !== null && ev.pointerId !== pointerId) return;
    const ll = eventLatLng(map, ev);
    finishDraw(ll);
    ev.preventDefault();
  };

  container.addEventListener("pointerdown", onPointerDown, { passive: false });
  container.addEventListener("pointermove", onPointerMove, { passive: false });
  container.addEventListener("pointerup", onPointerUp, { passive: false });
  container.addEventListener("pointercancel", () => finishDraw(null), { passive: true });

  // Ob preklopu iz add_map — prekini nedokončan vlek.
  state.subscribe(() => {
    if (state.toolMode !== "add_map" && drawing) {
      finishDraw(null);
    }
    container.classList.toggle("tool-add-map", state.toolMode === "add_map");
    container.classList.toggle("tool-add-wp", state.toolMode === "add_wp");
  });
}
