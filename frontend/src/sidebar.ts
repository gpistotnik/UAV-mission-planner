/** Master/detail sidebar: toolbar + seznam gradnikov + detail. */
import type { MissionElement, MissionPayload, WaypointElement, MapElement, DronePayload } from "./types";
import { PlannerState } from "./state";
import { getLiveGps } from "./telemetry";

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
    <label>Ime
      <input type="text" id="f-name" value="${escapeAttr(m.name)}" maxlength="120">
    </label>
    <label>Drone
      <select id="f-drone">${drones.map(d =>
        `<option value="${d.id}" ${d.id===m.drone_id?"selected":""}>${escapeHtml(d.display_name)}</option>`
      ).join("")}</select>
    </label>
    <div class="row">
      <label>Visina [m]<input type="number" id="f-alt" value="${m.default_altitude_m}" min="1" max="500"></label>
      <label>Hitrost [m/s]<input type="number" id="f-spd" value="${m.default_speed_ms}" step="0.5" min="1" max="20"></label>
    </div>
    <label>Konec
      <select id="f-finish">
        <option value="HOVER" ${m.finish_action==="HOVER"?"selected":""}>Lebdenje</option>
        <option value="RTH" ${m.finish_action==="RTH"?"selected":""}>Vrnitev domov</option>
        <option value="LAND" ${m.finish_action==="LAND"?"selected":""}>Pristanek</option>
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
    wrap.innerHTML = `<p class="hint">Brez gradnikov. Klikni "+ Waypoint" ali "+ Map" zgoraj.</p>`;
    return;
  }
  wrap.innerHTML = state.mission.elements.map(e => {
    const sel = e.id === state.selectedId ? "selected" : "";
    const cls = e.element_type === "WP" ? "wp" : "map";
    const label = e.name || (e.element_type === "WP" ? `Waypoint #${e.order}` : `Map #${e.order}`);
    return `
      <div class="element-row ${cls} ${sel}" data-id="${e.id}">
        <span class="badge">${e.element_type}</span>
        <span class="ord">#${e.order}</span>
        <span class="lbl">${escapeHtml(label)}</span>
        <span class="actions">
          <button data-act="up" title="Gor">&uarr;</button>
          <button data-act="dn" title="Dol">&darr;</button>
          <button data-act="del" title="Brisi">&times;</button>
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
    alert("GPS drona ni na voljo. Povezi Pixhawk in pocakaj na fix.");
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
    wrap.innerHTML = `<p class="hint">Klikni gradnik v seznamu za nastavitve.</p>`;
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
    <label>Oznaka<input type="text" id="d-name" value="${escapeAttr(e.name)}" maxlength="64"></label>
    <div class="row">
      <label>Visina [m]<input type="number" id="d-alt" value="${e.altitude_m}" min="1" max="500"></label>
      <label>Hitrost [m/s]<input type="number" id="d-spd" value="${e.speed_ms ?? ""}" step="0.5" min="1" max="20" placeholder="privzeta"></label>
    </div>
    <div class="coords-row">
      <p class="coords" id="d-coords">${e.lat.toFixed(6)}, ${e.lon.toFixed(6)}</p>
      <button type="button" id="d-gps" class="btn-gps" title="Nastavi na trenutni GPS drona">Nastavi GPS</button>
    </div>
    <label>Pot do tocke
      <select id="d-path">
        <option value="STRAIGHT" ${e.path_style==="STRAIGHT"?"selected":""}>Ravno (ustavi)</option>
        <option value="CURVED"   ${e.path_style==="CURVED"?"selected":""}>Krivo (zaobide)</option>
      </select>
    </label>
    <label>Dejanje v tocki
      <select id="d-act">
        <option value="NONE"          ${e.action_type==="NONE"?"selected":""}>Brez</option>
        <option value="PHOTO"         ${e.action_type==="PHOTO"?"selected":""}>Foto</option>
        <option value="VIDEO_START"   ${e.action_type==="VIDEO_START"?"selected":""}>Zacni snemanje</option>
        <option value="VIDEO_STOP"    ${e.action_type==="VIDEO_STOP"?"selected":""}>Ustavi snemanje</option>
        <option value="CAPTURE_BURST" ${e.action_type==="CAPTURE_BURST"?"selected":""}>Burst</option>
      </select>
    </label>
    <div class="row">
      <label>Smer
        <select id="d-hm">
          <option value="AUTO"    ${e.heading_mode==="AUTO"?"selected":""}>Auto</option>
          <option value="FIXED"   ${e.heading_mode==="FIXED"?"selected":""}>Fiksna</option>
          <option value="POI"     ${e.heading_mode==="POI"?"selected":""}>POI</option>
          <option value="NEXT_WP" ${e.heading_mode==="NEXT_WP"?"selected":""}>Naslednji</option>
        </select>
      </label>
      <label>Smer [&deg;]<input type="number" id="d-hd" value="${e.heading_deg ?? ""}" min="0" max="360" placeholder="auto"></label>
    </div>
    <div class="row">
      <label>Gimbal [&deg;]<input type="number" id="d-gp" value="${e.gimbal_pitch_deg}" min="-90" max="30"></label>
      <label>Cas lebd. [s]<input type="number" id="d-ht" value="${e.hover_time_s}" step="0.1" min="0"></label>
    </div>
  `;
}

function renderMapDetail(e: MapElement): string {
  const ring = e.polygon_geojson.coordinates[0];
  const verts = ring.slice(0, -1);
  const vertexRows = verts.map(([lon, lat], i) => `
    <div class="vertex-row">
      <span class="coords">V${i + 1}: ${lat.toFixed(6)}, ${lon.toFixed(6)}</span>
      <button type="button" class="btn-gps" data-gps-vertex="${i}" title="Nastavi vogal na trenutni GPS">Nastavi GPS</button>
    </div>`).join("");
  return `
    <label>Oznaka<input type="text" id="d-name" value="${escapeAttr(e.name)}" maxlength="64"></label>
    <div class="row">
      <label>Visina [m]<input type="number" id="d-alt" value="${e.altitude_m}" min="1" max="500"></label>
      <label>Smer prog [&deg;]<input type="number" id="d-trk" value="${e.track_angle_deg}" min="0" max="180"></label>
    </div>
    <label>Vzorec
      <select id="d-pat">
        <option value="GRID"       ${e.pattern==="GRID"?"selected":""}>Obicajen</option>
        <option value="CROSSHATCH" ${e.pattern==="CROSSHATCH"?"selected":""}>Mreza</option>
      </select>
    </label>
    <label>Prekrivanje [%]<input type="number" id="d-ov" value="${e.front_overlap_pct}" min="0" max="95" step="5"></label>
    <p class="hint">Vogali poligona (${verts.length}) — GPS nastavi izbrani vogal:</p>
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
  btn.textContent = state.dirty ? "Shrani (neshranjeno)" : "Shrani";
}

export function showMapStats(s: {
  gsd_cm_per_px:number; line_spacing_m:number; trigger_distance_m:number;
  total_distance_m:number; num_lines:number; estimated_images:number;
}): void {
  const el2 = document.getElementById("grid-stats");
  if (!el2) return;
  el2.innerHTML = `
    GSD: <b>${s.gsd_cm_per_px}</b> cm/px<br>
    Razmik prog: <b>${s.line_spacing_m}</b> m<br>
    Razdalja med posnetki: <b>${s.trigger_distance_m}</b> m<br>
    Skupna razdalja: <b>${s.total_distance_m}</b> m<br>
    Prog: <b>${s.num_lines}</b> | Ocena slik: <b>${s.estimated_images}</b>
  `;
}

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]!);
}
function escapeAttr(s: string): string { return escapeHtml(s); }
