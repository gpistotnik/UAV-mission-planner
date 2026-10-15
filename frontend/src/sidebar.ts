/** Master/detail sidebar: toolbar + seznam gradnikov + detail. */
import type { MissionElement, MissionPayload, WaypointElement, MapElement, DronePayload } from "./types";
import { PlannerState } from "./state";
import { getLiveGps } from "./telemetry";
import { t } from "./i18n";

export function renderSidebar(state: PlannerState, drones: DronePayload[]): void {
  renderToolbar(state);
  renderMissionHeader(state, drones);
  renderElementList(state);
  renderDetail(state);
  renderSaveButton(state);
}

function el<T extends HTMLElement>(id: string): T | null {
  return document.getElementById(id) as T | null;
}

function renderToolbar(state: PlannerState): void {
  const pairs: Array<[string, boolean]> = [
    ["btn-add-wp", state.toolMode === "add_wp"],
    ["btn-add-map", state.toolMode === "add_map"],
    ["fab-add-wp", state.toolMode === "add_wp"],
    ["fab-add-map", state.toolMode === "add_map"],
  ];
  for (const [id, on] of pairs) {
    el<HTMLButtonElement>(id)?.classList.toggle("active", on);
  }
  const canDel = state.selectedId !== null;
  for (const id of ["btn-delete", "fab-delete"]) {
    const b = el<HTMLButtonElement>(id);
    if (b) b.disabled = !canDel;
  }
}

function renderMissionHeader(state: PlannerState, drones: DronePayload[]): void {
  const m = state.mission;
  const wrap = el<HTMLDivElement>("mission-header");
  if (!wrap) return;
  if (wrap.dataset.rendered === "1") return;
  wrap.dataset.rendered = "1";
  wrap.innerHTML = `
    <label>${t("side.name")}
      <input type="text" id="f-name" value="${escapeAttr(m.name)}" maxlength="120">
    </label>
    <label>${t("side.drone")}
      <select id="f-drone">${drones.map(d =>
        `<option value="${d.id}" ${d.id===m.drone_id?"selected":""}>${escapeHtml(d.display_name)}</option>`
      ).join("")}</select>
    </label>
    <div class="row">
      <label>${t("side.altitude_m")}<input type="number" id="f-alt" value="${m.default_altitude_m}" min="1" max="500"></label>
      <label>${t("side.speed_ms")}<input type="number" id="f-spd" value="${m.default_speed_ms}" step="0.5" min="1" max="20"></label>
    </div>
    <label>${t("side.finish")}
      <select id="f-finish">
        <option value="HOVER" ${m.finish_action==="HOVER"?"selected":""}>${t("side.finish_hover")}</option>
        <option value="RTH" ${m.finish_action==="RTH"?"selected":""}>${t("side.finish_rth")}</option>
        <option value="LAND" ${m.finish_action==="LAND"?"selected":""}>${t("side.finish_land")}</option>
      </select>
    </label>
  `;
  el<HTMLInputElement>("f-name")?.addEventListener("input",    e => { m.name = (e.target as HTMLInputElement).value; state.markDirty(); });
  el<HTMLSelectElement>("f-drone")?.addEventListener("change", e => { m.drone_id = Number((e.target as HTMLSelectElement).value); state.markDirty(); });
  el<HTMLInputElement>("f-alt")?.addEventListener("input",     e => { m.default_altitude_m = Number((e.target as HTMLInputElement).value); state.markDirty(); });
  el<HTMLInputElement>("f-spd")?.addEventListener("input",     e => { m.default_speed_ms = Number((e.target as HTMLInputElement).value); state.markDirty(); });
  el<HTMLSelectElement>("f-finish")?.addEventListener("change",e => { m.finish_action = (e.target as HTMLSelectElement).value as MissionPayload["finish_action"]; state.markDirty(); });
}

let _listSignature = "";
function _signatureFor(state: PlannerState): string {
  return state.mission.elements.map(e =>
    `${e.id}|${e.order}|${e.element_type}|${e.name}`).join(";") + `#${state.selectedId}`;
}

function renderElementList(state: PlannerState): void {
  const wrap = el<HTMLDivElement>("element-list");
  if (!wrap) return;
  const sig = _signatureFor(state);
  if (sig === _listSignature) return;
  _listSignature = sig;

  if (state.mission.elements.length === 0) {
    wrap.innerHTML = `<p class="hint">${t("side.no_elements")}</p>`;
    return;
  }
  wrap.innerHTML = state.mission.elements.map(e => {
    const sel = e.id === state.selectedId ? "selected" : "";
    const cls = e.element_type === "WP" ? "wp" : "map";
    const label = e.name || (e.element_type === "WP"
      ? t("side.wp_label", { order: e.order })
      : t("side.map_label", { order: e.order }));
    return `
      <div class="element-row ${cls} ${sel}" data-id="${e.id}">
        <span class="badge">${e.element_type}</span>
        <span class="ord">#${e.order}</span>
        <span class="lbl">${escapeHtml(label)}</span>
        <span class="actions">
          <button data-act="up" title="${t("side.move_up")}">&uarr;</button>
          <button data-act="dn" title="${t("side.move_down")}">&darr;</button>
          <button data-act="del" title="${t("side.delete")}">&times;</button>
        </span>
      </div>`;
  }).join("");
  wrap.querySelectorAll(".element-row").forEach(row => {
    const id = Number((row as HTMLElement).dataset.id);
    row.addEventListener("click", (ev) => {
      const target = ev.target as HTMLElement;
      if (target.tagName === "BUTTON") {
        const act = target.dataset.act;
        if (act === "up") state.moveElement(id, -1);
        else if (act === "dn") state.moveElement(id, 1);
        else if (act === "del") state.removeElement(id);
      } else {
        state.select(id);
      }
    });
  });
}

let _detailRenderedFor: number | null = null;
let _detailRenderedType: string | null = null;
/** Podpis geometrije (št. vogalov) — ob spremembi poligona osveži GPS seznam. */
let _detailGeomSig = "";

/** Prisili ponovni izris detail panela (npr. po GPS / novem vogalu). */
export function invalidateDetail(): void {
  _detailRenderedFor = null;
  _detailRenderedType = null;
  _detailGeomSig = "";
}

function detailGeomSig(e: MissionElement | undefined): string {
  if (!e) return "";
  if (e.element_type === "MAP") {
    const n = e.polygon_geojson.coordinates[0].length;
    return `MAP:${n}`;
  }
  return `WP:${e.lat.toFixed(6)},${e.lon.toFixed(6)}`;
}

function requireLiveGps(): { lat: number; lon: number } | null {
  const gps = getLiveGps();
  if (!gps) {
    alert(t("side.gps_unavailable"));
    return null;
  }
  return gps;
}

function renderDetail(state: PlannerState): void {
  const wrap = el<HTMLDivElement>("element-detail");
  if (!wrap) return;
  const e = state.selectedElement();
  const currentId = (e?.id as number) ?? null;
  const currentType = e?.element_type ?? null;
  const geomSig = detailGeomSig(e);

  if (
    currentId === _detailRenderedFor
    && currentType === _detailRenderedType
    && geomSig === _detailGeomSig
  ) {
    return;
  }

  if (!e) {
    wrap.innerHTML = `<p class="hint">${t("side.select_hint")}</p>`;
  } else {
    wrap.innerHTML = e.element_type === "WP" ? renderWPDetail(e) : renderMapDetail(e);
    bindDetailInputs(state, e);
  }
  _detailRenderedFor = currentId;
  _detailRenderedType = currentType;
  _detailGeomSig = geomSig;
}

function renderWPDetail(e: WaypointElement): string {
  return `
    <label>${t("side.wp_marker")}<input type="text" id="d-name" value="${escapeAttr(e.name)}" maxlength="64"></label>
    <div class="row">
      <label>${t("side.altitude_m")}<input type="number" id="d-alt" value="${e.altitude_m}" min="1" max="500"></label>
      <label>${t("side.speed_ms")}<input type="number" id="d-spd" value="${e.speed_ms ?? ""}" step="0.5" min="1" max="20" placeholder="${t("side.default_ph")}"></label>
    </div>
    <div class="coords-row">
      <p class="coords" id="d-coords">${e.lat.toFixed(6)}, ${e.lon.toFixed(6)}</p>
      <button type="button" id="d-gps" class="btn-gps" title="${t("side.set_gps_title")}">${t("side.set_gps")}</button>
    </div>
    <label>${t("side.path_to_point")}
      <select id="d-path">
        <option value="STRAIGHT" ${e.path_style==="STRAIGHT"?"selected":""}>${t("side.path_straight")}</option>
        <option value="CURVED"   ${e.path_style==="CURVED"?"selected":""}>${t("side.path_curved")}</option>
      </select>
    </label>
    <label>${t("side.action_at_point")}
      <select id="d-act">
        <option value="NONE"          ${e.action_type==="NONE"?"selected":""}>${t("side.action_none")}</option>
        <option value="PHOTO"         ${e.action_type==="PHOTO"?"selected":""}>${t("side.action_photo")}</option>
        <option value="VIDEO_START"   ${e.action_type==="VIDEO_START"?"selected":""}>${t("side.action_video_start")}</option>
        <option value="VIDEO_STOP"    ${e.action_type==="VIDEO_STOP"?"selected":""}>${t("side.action_video_stop")}</option>
        <option value="CAPTURE_BURST" ${e.action_type==="CAPTURE_BURST"?"selected":""}>${t("side.action_burst")}</option>
      </select>
    </label>
    <div class="row">
      <label>${t("side.heading")}
        <select id="d-hm">
          <option value="AUTO"    ${e.heading_mode==="AUTO"?"selected":""}>${t("side.heading_auto")}</option>
          <option value="FIXED"   ${e.heading_mode==="FIXED"?"selected":""}>${t("side.heading_fixed")}</option>
          <option value="POI"     ${e.heading_mode==="POI"?"selected":""}>${t("side.heading_poi")}</option>
          <option value="NEXT_WP" ${e.heading_mode==="NEXT_WP"?"selected":""}>${t("side.heading_next")}</option>
        </select>
      </label>
      <label>${t("side.heading_deg")}<input type="number" id="d-hd" value="${e.heading_deg ?? ""}" min="0" max="360" placeholder="auto"></label>
    </div>
    <div class="row">
      <label>${t("side.gimbal_deg")}<input type="number" id="d-gp" value="${e.gimbal_pitch_deg}" min="-90" max="30"></label>
      <label>${t("side.hover_time_s")}<input type="number" id="d-ht" value="${e.hover_time_s}" step="0.1" min="0"></label>
    </div>
  `;
}

function renderMapDetail(e: MapElement): string {
  const ring = e.polygon_geojson.coordinates[0];
  const verts = ring.slice(0, -1);
  const vertexRows = verts.map(([lon, lat], i) => `
    <div class="vertex-row">
      <span class="coords">V${i + 1}: ${lat.toFixed(6)}, ${lon.toFixed(6)}</span>
      <button type="button" class="btn-gps" data-gps-vertex="${i}" title="${t("side.set_vertex_gps_title")}">${t("side.set_gps")}</button>
    </div>`).join("");
  return `
    <label>${t("side.map_marker")}<input type="text" id="d-name" value="${escapeAttr(e.name)}" maxlength="64"></label>
    <div class="row">
      <label>${t("side.altitude_m")}<input type="number" id="d-alt" value="${e.altitude_m}" min="1" max="500"></label>
      <label>${t("side.track_angle_deg")}<input type="number" id="d-trk" value="${e.track_angle_deg}" min="0" max="180"></label>
    </div>
    <label>${t("side.pattern")}
      <select id="d-pat">
        <option value="GRID"       ${e.pattern==="GRID"?"selected":""}>${t("side.pattern_grid")}</option>
        <option value="CROSSHATCH" ${e.pattern==="CROSSHATCH"?"selected":""}>${t("side.pattern_crosshatch")}</option>
      </select>
    </label>
    <label>${t("side.overlap_pct")}<input type="number" id="d-ov" value="${e.front_overlap_pct}" min="0" max="95" step="5"></label>
    <p class="hint">${t("side.vertices_hint", { count: verts.length })}</p>
    <div class="vertex-list">${vertexRows}</div>
    <div id="grid-stats" class="stats"></div>
  `;
}

function bindDetailInputs(state: PlannerState, e: MissionElement): void {
  el<HTMLInputElement>("d-name")?.addEventListener("input", ev => {
    e.name = (ev.target as HTMLInputElement).value; state.markDirty();
  });
  el<HTMLInputElement>("d-alt")?.addEventListener("input", ev => {
    e.altitude_m = Number((ev.target as HTMLInputElement).value); state.markDirty();
  });
  if (e.element_type === "WP") {
    const wp = e as WaypointElement;
    el<HTMLInputElement>("d-spd")?.addEventListener("input",   ev => { wp.speed_ms = (ev.target as HTMLInputElement).value ? Number((ev.target as HTMLInputElement).value) : null; state.markDirty(); });
    el<HTMLSelectElement>("d-path")?.addEventListener("change",ev => { wp.path_style = (ev.target as HTMLSelectElement).value as WaypointElement["path_style"]; state.markDirty(); });
    el<HTMLSelectElement>("d-act")?.addEventListener("change", ev => { wp.action_type = (ev.target as HTMLSelectElement).value as WaypointElement["action_type"]; state.markDirty(); });
    el<HTMLSelectElement>("d-hm")?.addEventListener("change",  ev => { wp.heading_mode = (ev.target as HTMLSelectElement).value as WaypointElement["heading_mode"]; state.markDirty(); });
    el<HTMLInputElement>("d-hd")?.addEventListener("input",    ev => { wp.heading_deg = (ev.target as HTMLInputElement).value ? Number((ev.target as HTMLInputElement).value) : null; state.markDirty(); });
    el<HTMLInputElement>("d-gp")?.addEventListener("input",    ev => { wp.gimbal_pitch_deg = Number((ev.target as HTMLInputElement).value); state.markDirty(); });
    el<HTMLInputElement>("d-ht")?.addEventListener("input",    ev => { wp.hover_time_s = Number((ev.target as HTMLInputElement).value); state.markDirty(); });
    el<HTMLButtonElement>("d-gps")?.addEventListener("click", () => {
      const gps = requireLiveGps();
      if (!gps) return;
      wp.lat = gps.lat;
      wp.lon = gps.lon;
      invalidateDetail();
      state.markDirty();
    });
  } else {
    const me = e as MapElement;
    el<HTMLInputElement>("d-trk")?.addEventListener("input",   ev => { me.track_angle_deg = Number((ev.target as HTMLInputElement).value); state.markDirty(); });
    el<HTMLSelectElement>("d-pat")?.addEventListener("change", ev => { me.pattern = (ev.target as HTMLSelectElement).value as MapElement["pattern"]; state.markDirty(); });
    el<HTMLInputElement>("d-ov")?.addEventListener("input",    ev => {
      const v = Number((ev.target as HTMLInputElement).value);
      me.front_overlap_pct = v;
      me.side_overlap_pct = v;
      state.markDirty();
    });
    document.querySelectorAll<HTMLButtonElement>("[data-gps-vertex]").forEach(btn => {
      btn.addEventListener("click", () => {
        const gps = requireLiveGps();
        if (!gps) return;
        const i = Number(btn.dataset.gpsVertex);
        const ring = me.polygon_geojson.coordinates[0];
        if (i < 0 || i >= ring.length - 1) return;
        ring[i] = [gps.lon, gps.lat];
        if (i === 0) ring[ring.length - 1] = [gps.lon, gps.lat];
        invalidateDetail();
        state.markDirty();
      });
    });
  }
}

function renderSaveButton(state: PlannerState): void {
  const btn = el<HTMLButtonElement>("save-btn");
  if (!btn) return;
  btn.classList.toggle("dirty", state.dirty);
  btn.textContent = state.dirty ? t("side.save_unsaved") : t("side.save");
}

export function showMapStats(s: {
  gsd_cm_per_px:number; line_spacing_m:number; trigger_distance_m:number;
  total_distance_m:number; num_lines:number; estimated_images:number;
}): void {
  const el2 = document.getElementById("grid-stats");
  if (!el2) return;
  el2.innerHTML = t("side.map_stats", {
    gsd: s.gsd_cm_per_px,
    spacing: s.line_spacing_m,
    trigger: s.trigger_distance_m,
    total: s.total_distance_m,
    lines: s.num_lines,
    images: s.estimated_images,
  });
}

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]!);
}
function escapeAttr(s: string): string { return escapeHtml(s); }
