/**
 * Telemetry modul.
 *
 * - Pridobi seznam serijskih portov in ga prikaze v <select>.
 * - Connect / disconnect gumba klicejo Django API.
 * - Ko je povezava aktivna, polling vsako 500 ms pridobi snapshot in
 *   posodobi HUD panel + Leaflet marker (pozicija + heading).
 */
import L from "leaflet";

const POLL_MS = 500;
const PORTS_REFRESH_MS = 5000;

/** Zadnja veljavna GPS pozicija drona (null, ce ni povezave / fix-a). */
export interface LiveGps {
  lat: number;
  lon: number;
  alt_msl_m: number | null;
}

let lastLiveGps: LiveGps | null = null;

/** Trenutna GPS pozicija iz telemetrije, ali null ce ni na voljo. */
export function getLiveGps(): LiveGps | null {
  return lastLiveGps;
}

interface SerialPort {
  device: string;
  description: string;
  hwid: string;
  manufacturer?: string;
}

interface Snapshot {
  connected: boolean;
  port: string | null;
  baud: number | null;
  error: string | null;
  uptime_s: number | null;
  messages_received: number;
  heartbeat: { mode: string | null; armed: boolean; system_status: string | null; age_s: number | null };
  attitude: { roll_deg: number | null; pitch_deg: number | null; yaw_deg: number | null; age_s: number | null };
  gps: {
    lat: number | null; lon: number | null; alt_msl_m: number | null;
    fix_type: number | null; satellites: number | null; hdop: number | null;
    healthy?: boolean | null; age_s: number | null;
  };
  compass?: {
    x_mg: number | null; y_mg: number | null; z_mg: number | null;
    field_mg: number | null; xy_mg: number | null; healthy: boolean | null;
    cal_pct: number | null; cal_status: number | null;
    cal_status_label: string | null; cal_fitness: number | null;
    age_s: number | null;
  };
  vfr_hud: {
    airspeed_ms: number | null; groundspeed_ms: number | null; alt_msl_m: number | null;
    climb_ms: number | null; throttle_pct: number | null; heading_deg: number | null; age_s: number | null;
  };
  battery: { voltage_v: number | null; current_a: number | null; remaining_pct: number | null; age_s: number | null };
  mission: {
    current_seq: number | null; reached_seq: number | null;
    item_count: number | null; uploaded_at: number | null;
    id: number | null; name: string | null;
  };
  home: { lat: number | null; lon: number | null };
  statustexts?: Array<{ t: number; severity: number; text: string }>;
  logging?: { active: boolean; messages?: number };
  mavlink_available?: boolean;
}

function jget(url: string): Promise<any> {
  return fetch(url, { credentials: "same-origin" }).then(r => {
    if (!r.ok) throw new Error(`${url} -> ${r.status}`);
    return r.json();
  });
}

function jpost(url: string, csrf: string, body: unknown): Promise<any> {
  return fetch(url, {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
    body: JSON.stringify(body),
  }).then(r => r.json().catch(() => ({})));
}

function fmt(n: number | null | undefined, digits: number = 1, suffix: string = ""): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return n.toFixed(digits) + suffix;
}

function fixTypeLabel(fix: number | null | undefined): string {
  // Standardni MAVLink GPS_FIX_TYPE enum
  switch (fix) {
    case 0: case 1: return "ne";
    case 2: return "2D";
    case 3: return "3D";
    case 4: return "DGPS";
    case 5: return "RTK-Float";
    case 6: return "RTK";
    default: return "—";
  }
}

// ---------------------------------------------------------------------------
// Leaflet marker za pozicijo drona
// ---------------------------------------------------------------------------
function makeDroneIcon(yaw: number): L.DivIcon {
  return L.divIcon({
    className: "live-drone-marker",
    iconSize: [40, 40],
    iconAnchor: [20, 20],
    html: `<div class="ld-arrow" style="transform: rotate(${yaw}deg)">➤</div>`,
  });
}

// ---------------------------------------------------------------------------
// Glavni setup
// ---------------------------------------------------------------------------
export function setupTelemetry(map: L.Map, csrf: string): void {
  const portSel  = document.getElementById("tm-port") as HTMLSelectElement | null;
  const baudSel  = document.getElementById("tm-baud") as HTMLSelectElement | null;
  const refresh  = document.getElementById("tm-refresh") as HTMLButtonElement | null;
  const btnCon   = document.getElementById("tm-connect") as HTMLButtonElement | null;
  const btnDis   = document.getElementById("tm-disconnect") as HTMLButtonElement | null;
  const statusEl = document.getElementById("tm-status") as HTMLDivElement | null;
  const errEl    = document.getElementById("tm-error") as HTMLDivElement | null;

  // HUD elementi
  const hud      = document.getElementById("hud") as HTMLDivElement | null;
  const hudMode  = document.getElementById("hud-mode") as HTMLSpanElement | null;
  const hudArm   = document.getElementById("hud-armed") as HTMLSpanElement | null;
  const hudAlt   = document.getElementById("hud-alt") as HTMLSpanElement | null;
  const hudGspd  = document.getElementById("hud-gspd") as HTMLSpanElement | null;
  const hudVspd  = document.getElementById("hud-vspd") as HTMLSpanElement | null;
  const hudHdg   = document.getElementById("hud-hdg") as HTMLSpanElement | null;
  const hudRoll  = document.getElementById("hud-roll") as HTMLSpanElement | null;
  const hudPitch = document.getElementById("hud-pitch") as HTMLSpanElement | null;
  const hudBatV  = document.getElementById("hud-batv") as HTMLSpanElement | null;
  const hudBatP  = document.getElementById("hud-batp") as HTMLSpanElement | null;
  const hudGps   = document.getElementById("hud-gps") as HTMLSpanElement | null;
  const hudSats  = document.getElementById("hud-sats") as HTMLSpanElement | null;
  const hudHdop  = document.getElementById("hud-hdop") as HTMLSpanElement | null;
  const hudMag   = document.getElementById("hud-mag") as HTMLSpanElement | null;
  const hudMagXy = document.getElementById("hud-mag-xy") as HTMLSpanElement | null;
  const hudMagCal = document.getElementById("hud-mag-cal") as HTMLSpanElement | null;

  // Napredek nalozene misije (seq / stevilo ukazov).
  const mpWrap   = document.getElementById("mission-progress") as HTMLDivElement | null;
  const mpFill   = document.getElementById("mp-fill") as HTMLDivElement | null;
  const mpText   = document.getElementById("mp-text") as HTMLDivElement | null;
  const stEl     = document.getElementById("ctl-statustext") as HTMLDivElement | null;

  if (!portSel || !btnCon || !btnDis) {
    console.warn("[telemetry] manjkajo HTML elementi, preskakam setup.");
    return;
  }

  // Drone marker + trail polyline
  let droneMarker: L.Marker | null = null;
  const trail = L.polyline([], { color: "#dc2626", weight: 3, opacity: 0.6 }).addTo(map);
  const trailPoints: L.LatLng[] = [];

  let pollTimer: number | null = null;

  function setError(msg: string | null) {
    if (errEl) {
      errEl.textContent = msg || "";
      errEl.style.display = msg ? "block" : "none";
    }
  }

  function setStatus(connected: boolean, snap: Snapshot | null) {
    if (statusEl) {
      if (connected && snap) {
        statusEl.textContent = `✓ ${snap.port} @ ${snap.baud}`;
        statusEl.className = "tm-status ok";
      } else {
        statusEl.textContent = "● ni povezave";
        statusEl.className = "tm-status off";
      }
    }
    if (hud) hud.style.display = connected ? "block" : "none";
    btnCon!.disabled = connected;
    btnDis!.disabled = !connected;
  }

  function applySnapshot(snap: Snapshot) {
    setError(snap.error);
    setStatus(snap.connected, snap);
    if (!snap.connected) {
      lastLiveGps = null;
      return;
    }

    const hb = snap.heartbeat;
    if (hudMode) hudMode.textContent = hb.mode || "—";
    if (hudArm) {
      hudArm.textContent = hb.armed ? "ARMED" : "DISARM";
      hudArm.className = hb.armed ? "armed" : "disarmed";
    }

    const vfr = snap.vfr_hud;
    if (hudAlt) hudAlt.textContent = fmt(vfr.alt_msl_m, 1, " m");
    if (hudGspd) hudGspd.textContent = fmt(vfr.groundspeed_ms, 1, " m/s");
    if (hudVspd) hudVspd.textContent = fmt(vfr.climb_ms, 1, " m/s");
    if (hudHdg) hudHdg.textContent = fmt(vfr.heading_deg, 0, "°");

    const att = snap.attitude;
    if (hudRoll) hudRoll.textContent = fmt(att.roll_deg, 1, "°");
    if (hudPitch) hudPitch.textContent = fmt(att.pitch_deg, 1, "°");

    const bat = snap.battery;
    if (hudBatV) hudBatV.textContent = fmt(bat.voltage_v, 2, " V");
    if (hudBatP) {
      const p = bat.remaining_pct;
      hudBatP.textContent = p === null ? "—" : `${p}%`;
      hudBatP.className = (p !== null && p < 25) ? "warn" : "";
    }

    // Napredek misije
    const mis = snap.mission;
    if (mpWrap && mis && mis.item_count) {
      const cur = mis.current_seq ?? 0;
      const total = mis.item_count;
      const pct = total > 0 ? Math.min(100, (cur / total) * 100) : 0;
      mpWrap.style.display = "block";
      if (mpFill) mpFill.style.width = `${pct}%`;
      if (mpText) {
        mpText.textContent =
          `${mis.name ?? "misija"} · ukaz ${cur}/${total}` +
          (mis.reached_seq !== null ? ` · dosežen ${mis.reached_seq}` : "");
      }
    } else if (mpWrap) {
      mpWrap.style.display = "none";
    }

    // Zadnje sporocilo krmilnika (pre-arm napake so tu najbolj koristne).
    if (stEl && snap.statustexts && snap.statustexts.length > 0) {
      const last = snap.statustexts[snap.statustexts.length - 1];
      stEl.textContent = last.text;
      stEl.className = last.severity <= 3 ? "ctl-statustext err" : "ctl-statustext";
    }

    const gps = snap.gps;
    if (hudGps) {
      const fix = fixTypeLabel(gps.fix_type);
      const hl = gps.healthy === false ? " !" : "";
      hudGps.textContent = fix + hl;
      hudGps.className = gps.healthy === false ? "warn" : "";
    }
    if (hudSats) hudSats.textContent = gps.satellites === null ? "—" : String(gps.satellites);
    if (hudHdop) {
      hudHdop.textContent = fmt(gps.hdop, 2);
      hudHdop.className = (gps.hdop !== null && gps.hdop > 1.5) ? "warn" : "";
    }

    const mag = snap.compass;
    if (hudMag) {
      if (!mag || mag.field_mg === null || mag.field_mg === undefined) {
        hudMag.textContent = "—";
        hudMag.className = "";
      } else {
        const hl = mag.healthy === false ? " !" : "";
        hudMag.textContent = `${mag.field_mg.toFixed(0)} mG${hl}`;
        // Tipicno zemeljsko polje ~250–650 mG; zunaj tega je sum na interferenco.
        const odd = mag.field_mg < 200 || mag.field_mg > 800 || mag.healthy === false;
        hudMag.className = odd ? "warn" : "";
      }
    }
    if (hudMagXy) {
      hudMagXy.textContent = mag?.xy_mg == null ? "—" : `${mag.xy_mg.toFixed(0)} mG`;
    }
    if (hudMagCal) {
      if (!mag || mag.cal_pct === null || mag.cal_pct === undefined) {
        hudMagCal.textContent = "—";
        hudMagCal.className = "";
      } else {
        const label = mag.cal_status_label || "";
        hudMagCal.textContent = `${mag.cal_pct}%${label ? ` · ${label}` : ""}`;
        hudMagCal.className = mag.cal_status === 4 ? "ok"
          : (mag.cal_status !== null && mag.cal_status >= 5) ? "warn" : "";
      }
    }

    // Marker + deljena GPS pozicija za planner (WP/MAP → GPS).
    // fix_type < 2 = ni fixa (MAVLink: 0/1); (0,0) je pogost "prazni" paket.
    const hasFix = (
      gps.lat !== null && gps.lon !== null
      && (gps.fix_type ?? 0) >= 2
      && !(gps.lat === 0 && gps.lon === 0)
    );
    if (hasFix) {
      lastLiveGps = { lat: gps.lat!, lon: gps.lon!, alt_msl_m: gps.alt_msl_m };
      const ll = L.latLng(gps.lat!, gps.lon!);
      const yaw = att.yaw_deg ?? vfr.heading_deg ?? 0;
      if (!droneMarker) {
        droneMarker = L.marker(ll, { icon: makeDroneIcon(yaw), zIndexOffset: 1000 }).addTo(map);
      } else {
        droneMarker.setLatLng(ll);
        droneMarker.setIcon(makeDroneIcon(yaw));
      }
      // Trail samo ko je armed (med letom).
      if (hb.armed) {
        const last = trailPoints[trailPoints.length - 1];
        if (!last || last.distanceTo(ll) > 0.5) {
          trailPoints.push(ll);
          if (trailPoints.length > 2000) trailPoints.shift();
          trail.setLatLngs(trailPoints);
        }
      }
    } else {
      lastLiveGps = null;
    }
  }

  async function refreshPorts() {
    try {
      const data = await jget("/api/serial-ports/");
      const ports: SerialPort[] = data.ports || [];
      const cur = portSel!.value;
      portSel!.innerHTML = "";
      if (ports.length === 0) {
        const opt = document.createElement("option");
        opt.value = "";
        opt.textContent = "(ni vidnih naprav)";
        portSel!.appendChild(opt);
      }
      ports.forEach(p => {
        const opt = document.createElement("option");
        opt.value = p.device;
        const desc = p.description ? ` — ${p.description}` : "";
        opt.textContent = `${p.device}${desc}`;
        portSel!.appendChild(opt);
      });
      if (cur && ports.some(p => p.device === cur)) portSel!.value = cur;
    } catch (e) {
      setError(`Napaka pri branju portov: ${(e as Error).message}`);
    }
  }

  function startPolling() {
    if (pollTimer !== null) return;
    pollTimer = window.setInterval(async () => {
      try {
        const snap: Snapshot = await jget("/api/telemetry/");
        applySnapshot(snap);
        if (!snap.connected) {
          stopPolling();
        }
      } catch (e) {
        setError((e as Error).message);
        stopPolling();
      }
    }, POLL_MS);
  }
  function stopPolling() {
    if (pollTimer !== null) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  btnCon.addEventListener("click", async () => {
    const device = portSel!.value;
    const baud = parseInt(baudSel?.value || "115200", 10);
    if (!device) { setError("Izberi serijsko napravo."); return; }
    setError("Povezujem...");
    btnCon.disabled = true;
    const snap = await jpost("/api/mavlink/connect/", csrf, { device, baud });
    applySnapshot(snap);
    if (snap.connected) {
      startPolling();
    } else {
      btnCon.disabled = false;
    }
  });

  btnDis.addEventListener("click", async () => {
    stopPolling();
    const snap = await jpost("/api/mavlink/disconnect/", csrf, {});
    applySnapshot(snap);
    // Pocisti trail.
    trailPoints.length = 0;
    trail.setLatLngs([]);
    if (droneMarker) {
      map.removeLayer(droneMarker);
      droneMarker = null;
    }
  });

  refresh?.addEventListener("click", refreshPorts);

  // Telemetrija takoj; seznam portov po kratkem odlogu (manj konkurence ob loadu).
  jget("/api/telemetry/").then((snap: Snapshot) => {
    applySnapshot(snap);
    if (snap.connected) startPolling();
  }).catch(() => {});

  window.setTimeout(() => {
    void refreshPorts();
    setInterval(() => { if (pollTimer === null) void refreshPorts(); }, PORTS_REFRESH_MS);
  }, 600);
}
