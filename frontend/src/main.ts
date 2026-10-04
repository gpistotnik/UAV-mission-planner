/** Vstopna tocka — dvostopenjska: planner ali dashboard. */
import L from "leaflet";

import { createMap } from "./map";
import { PlannerState } from "./state";
import { ElementLayer } from "./map_layers";
import { attachTools } from "./tools";
import { renderSidebar } from "./sidebar";
import { saveMission } from "./api";
import { TestFlight, formatTime } from "./test_flight";
import { setupTelemetry } from "./telemetry";
import { setupControl, setupLogging } from "./control";
import { setupAutoTest } from "./auto_test";
import { setupMagCal } from "./mag_cal";
import { setupNetworkBanner } from "./network_banner";
import { setupSettings } from "./settings";
import { setupGallery } from "./gallery";

/* ===========================================================================
 * Dashboard
 * ===========================================================================
 */
function bootstrapDashboard() {
  const cfg = window.PLANNER_BOOTSTRAP;
  if (!cfg) { console.error("PLANNER_BOOTSTRAP missing"); return; }

  const map = createMap("map", 46.0569, 14.5058);

  // Telemetrija + misija takoj; kamera / stats / auto-connect po prvem paintu,
  // da ne tekmujejo z Leaflet CSS in mrezo.
  setupTelemetry(map, cfg.csrfToken);
  setupMissionSelector(map, cfg.csrfToken);
  setupControl(cfg.csrfToken);
  setupLogging(cfg.csrfToken);
  setupCameraShutter(cfg.csrfToken);

  const defer = (fn: () => void, ms: number) => window.setTimeout(fn, ms);
  defer(() => setupSystemStats(), 800);
  defer(() => setupCameraStream(cfg.cameraUrl || "auto"), 1200);
  defer(() => setupPixhawkAutoConnect(cfg.csrfToken), 2000);
}

// ---------------------------------------------------------------------------
// Sistem stats panel
// ---------------------------------------------------------------------------
function setupSystemStats() {
  const hostEl  = document.getElementById("sys-host");
  const cpuEl   = document.getElementById("sys-cpu");
  const ramEl   = document.getElementById("sys-ram");
  const diskEl  = document.getElementById("sys-disk");
  const tempEl  = document.getElementById("sys-temp");
  const voltEl  = document.getElementById("sys-volt");
  const loadEl  = document.getElementById("sys-load");
  const upEl    = document.getElementById("sys-uptime");
  const thrEl   = document.getElementById("sys-throttled");
  const warnEl  = document.getElementById("sys-warning");
  if (!cpuEl) return;

  const fmtBytes = (b: number): string => {
    if (b >= 1e9) return `${(b/1e9).toFixed(1)}G`;
    if (b >= 1e6) return `${(b/1e6).toFixed(0)}M`;
    return `${(b/1e3).toFixed(0)}K`;
  };
  const fmtUptime = (s: number): string => {
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    if (d > 0) return `${d}d ${h}h ${m}m`;
    if (h > 0) return `${h}h ${m}m`;
    return `${m}m`;
  };
  const cls = (pct: number, warn: number, crit: number): string => {
    if (pct >= crit) return "crit";
    if (pct >= warn) return "warn";
    return "";
  };

  async function refresh() {
    try {
      const r = await fetch("/api/system/stats/", { credentials: "same-origin" });
      if (!r.ok) return;
      const s = await r.json();

      if (hostEl) hostEl.textContent = s.hostname ? `(${s.hostname})` : "";

      // CPU
      const cpu = s.cpu_percent ?? 0;
      cpuEl!.textContent = `${cpu.toFixed(0)}%`;
      cpuEl!.className = cls(cpu, 70, 90);

      // RAM
      const m = s.memory || {};
      if (typeof m.percent === "number") {
        ramEl!.textContent = `${m.percent.toFixed(0)}% (${fmtBytes(m.used_b)}/${fmtBytes(m.total_b)})`;
        ramEl!.className = cls(m.percent, 80, 95);
      }

      // Disk
      const d = s.disk || {};
      if (typeof d.percent === "number") {
        diskEl!.textContent = `${d.percent.toFixed(0)}% (${fmtBytes(d.free_b)} free)`;
        diskEl!.className = cls(d.percent, 80, 95);
      }

      // Temp
      const t = s.temperature_c;
      if (t !== null && t !== undefined) {
        tempEl!.textContent = `${t.toFixed(1)}°C`;
        tempEl!.className = cls(t, 70, 80);
      } else {
        tempEl!.textContent = "—";
      }

      // Voltage — core voltage je DVFS-scaled (0.7 V idle → 1.0 V load),
      // zato sama vrednost ni signal podnapajanja. Pravi signal je
      // 'throttled' bitmask. Tu samo informativno prikazemo.
      const v = s.voltage_core_v;
      if (v !== null && v !== undefined) {
        voltEl!.textContent = `${v.toFixed(3)} V`;
        voltEl!.className = "";  // vedno nevtralno
      } else {
        voltEl!.textContent = "—";
      }

      // Load
      const la = s.loadavg || {};
      if (typeof la["1m"] === "number") {
        loadEl!.textContent = `${la["1m"].toFixed(2)} / ${la["5m"].toFixed(2)}`;
      }

      // Uptime
      const up = s.uptime || {};
      if (typeof up.seconds === "number") {
        upEl!.textContent = fmtUptime(up.seconds);
      }

      // Throttled — najpomembnejši signal
      const thr = s.throttled || {};
      if (thr.available) {
        const sev = thr.severity || "ok";
        thrEl!.textContent = `${thr.raw} (${sev.toUpperCase()})`;
        thrEl!.className = sev === "critical" ? "crit" : sev === "warning" ? "warn" : "ok";

        // Vidno opozorilo pri throttled
        if (warnEl) {
          if (sev === "critical") {
            warnEl.style.display = "block";
            warnEl.className = "sys-warning crit";
            const flags = thr.flags || {};
            const issues: string[] = [];
            if (flags.under_voltage_now) issues.push("PODNAPAJANJE");
            if (flags.throttled_now) issues.push("THROTTLE");
            if (flags.soft_temp_limit_now) issues.push("VROČINA");
            warnEl.textContent = `⚠ ${issues.join(", ")} (zdaj)! Zamenjaj napajalnik ali zniži obremenitev.`;
          } else if (sev === "warning") {
            warnEl.style.display = "block";
            warnEl.className = "sys-warning";
            warnEl.textContent = `⚠ Brownout v zgodovini (od boot-a). Spremljaj napajanje.`;
          } else {
            warnEl.style.display = "none";
          }
        }
      } else {
        thrEl!.textContent = "n/a";
      }
    } catch (e) {
      // tiho
    }
  }

  refresh();
  setInterval(refresh, 5000);
}

// ---------------------------------------------------------------------------
// UI auto-connect: ce streznik se ni povezal, poskusi isto kot gumb Poveži.
// Ponavlja nekajkrat — Pixhawk je lahko se v bootu ob prvem loadu.
// ---------------------------------------------------------------------------
function setupPixhawkAutoConnect(csrf: string) {
  const KEY = "pixhawk_auto_disabled";
  const cfg = window.PLANNER_BOOTSTRAP as {
    serialDevice?: string;
    serialBaud?: number;
  };
  let tries = 0;
  const MAX_TRIES = 8;
  const RETRY_MS = 3000;
  let timer: number | null = null;

  function baud(): number {
    const baudSel = document.getElementById("tm-baud") as HTMLSelectElement | null;
    const b = cfg.serialBaud
      || parseInt(baudSel?.value || "115200", 10)
      || 115200;
    if (baudSel && String(b) !== baudSel.value) {
      const opt = Array.from(baudSel.options).find(o => o.value === String(b));
      if (opt) baudSel.value = String(b);
    }
    return b;
  }

  async function pickDevice(): Promise<string | null> {
    const preferred = (cfg.serialDevice || "auto").trim();
    if (preferred && preferred.toLowerCase() !== "auto"
        && !preferred.startsWith("udp:") && !preferred.startsWith("tcp:")) {
      return preferred;
    }
    try {
      const data = await (await fetch("/api/serial-ports/")).json();
      const ports: { device: string }[] = data.ports || [];
      const cand = ports.find(p => /tty(ACM|USB)|COM\d+/i.test(p.device));
      return cand ? cand.device : null;
    } catch {
      return null;
    }
  }

  async function tryAutoConnect(): Promise<void> {
    if (localStorage.getItem(KEY) === "1") return;

    try {
      const tel = await (await fetch("/api/telemetry/", { credentials: "same-origin" })).json();
      if (tel.connected) {
        if (timer !== null) { window.clearInterval(timer); timer = null; }
        return;
      }
      // Ne vmesaj samo med aktivnim connectom — FAILED attempts streznika
      // ne smejo blokirati UI (to je bil bug: attempts>0 → nikoli Poveži).
      if (tel.connecting) return;
    } catch { return; }

    const device = await pickDevice();
    if (!device) return;

    tries += 1;
    try {
      const r = await fetch("/api/mavlink/connect/", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
        body: JSON.stringify({ device, baud: baud() }),
      });
      const snap = await r.json().catch(() => ({}));
      if (snap.connected) {
        console.log(`[auto-connect] Connected to ${device}`);
        if (timer !== null) { window.clearInterval(timer); timer = null; }
        return;
      }
      console.warn(`[auto-connect] try ${tries}:`, snap.error || r.status);
    } catch (e) {
      console.warn("[auto-connect] failed:", e);
    }

    if (tries >= MAX_TRIES && timer !== null) {
      window.clearInterval(timer);
      timer = null;
    }
  }

  void tryAutoConnect();
  timer = window.setInterval(() => { void tryAutoConnect(); }, RETRY_MS);

  document.getElementById("tm-disconnect")?.addEventListener("click", () => {
    localStorage.setItem(KEY, "1");
    if (timer !== null) { window.clearInterval(timer); timer = null; }
  });
  document.getElementById("tm-connect")?.addEventListener("click", () => {
    localStorage.removeItem(KEY);
  });
}

function setupCameraStream(configuredUrl: string) {
  const img = document.getElementById("cam-stream") as HTMLImageElement | null;
  const urlInput = document.getElementById("cam-url") as HTMLInputElement | null;
  const reload   = document.getElementById("cam-reload") as HTMLButtonElement | null;
  const status   = document.getElementById("cam-status") as HTMLDivElement | null;
  if (!img || !urlInput) return;

  const resolveUrl = (u: string): string => {
    if (u && u !== "auto") return u;
    const host = window.location.hostname || "localhost";
    return `http://${host}:8090/stream.mjpg`;
  };

  const initialUrl = resolveUrl(configuredUrl);
  urlInput.value = initialUrl;

  let started = false;

  function loadStream(url: string) {
    started = true;
    if (status) {
      status.textContent = "Povezujem se s kamero...";
      status.className = "cam-status";
    }
    img!.onload  = () => { if (status) { status.className = "cam-status ok"; } };
    img!.onerror = () => {
      if (status) {
        status.textContent = "Kamera ni dosegljiva na " + url;
        status.className = "cam-status error";
      }
    };
    img!.src = url + (url.indexOf("?") < 0 ? "?t=" : "&t=") + Date.now();
  }

  // Odlozi MJPEG, dokler je video panel v viewportu (ali uporabnik klikne).
  const startIfVisible = () => {
    if (started) return;
    const frame = img.closest(".dash-video") || img;
    if ("IntersectionObserver" in window) {
      const obs = new IntersectionObserver((entries) => {
        if (entries.some(e => e.isIntersecting)) {
          obs.disconnect();
          loadStream(urlInput.value.trim() || initialUrl);
        }
      }, { threshold: 0.15 });
      obs.observe(frame);
      // Varnostni fallback, ce observer ne sprozi (skrit layout).
      window.setTimeout(() => {
        if (!started) loadStream(urlInput.value.trim() || initialUrl);
      }, 4000);
    } else {
      loadStream(initialUrl);
    }
  };

  if (status) {
    status.textContent = "Kamera se naloži, ko je vidna…";
    status.className = "cam-status";
  }
  startIfVisible();
  reload?.addEventListener("click", () => loadStream(urlInput!.value.trim()));
  urlInput.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") loadStream(urlInput!.value.trim());
  });
}

/** Shutter — zajame okvir iz streama in shrani na USB/log shrambo. */
function setupCameraShutter(csrf: string) {
  const btn = document.getElementById("cam-shutter") as HTMLButtonElement | null;
  const flash = document.getElementById("cam-flash");
  const msg = document.getElementById("cam-shutter-msg");
  if (!btn) return;

  let busy = false;
  const setMsg = (text: string, cls = "") => {
    if (!msg) return;
    msg.textContent = text;
    msg.className = "cam-shutter-msg" + (cls ? " " + cls : "");
  };

  btn.addEventListener("click", async () => {
    if (busy) return;
    busy = true;
    btn.disabled = true;
    btn.classList.add("firing");
    flash?.classList.add("on");
    window.setTimeout(() => flash?.classList.remove("on"), 180);
    setMsg("Shranjujem…");

    const sel = document.getElementById("mission-select") as HTMLSelectElement | null;
    const missionId = sel?.value ? Number(sel.value) : null;
    const body: Record<string, unknown> = {};
    if (missionId) body.mission_id = missionId;

    try {
      const r = await fetch("/api/camera/capture/", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": csrf,
        },
        body: JSON.stringify(body),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok || data.ok === false) {
        let err = data.error || `Napaka ${r.status}`;
        if (data.auth_required && data.login_url) {
          setMsg("");
          if (msg) {
            msg.innerHTML = `${err} — <a href="${data.login_url}">prijava</a>`;
            msg.className = "cam-shutter-msg error";
          }
        } else {
          setMsg(err, "error");
        }
        return;
      }
      setMsg(`Shranjeno: ${data.file || "slika"}`, "ok");
    } catch (e) {
      setMsg("Napaka: " + (e as Error).message, "error");
    } finally {
      busy = false;
      btn.disabled = false;
      btn.classList.remove("firing");
    }
  });
}

/**
 * Mission selector — read-only prikaz misije na karti.
 * Layer-i (markers + polygons) so shranjeni in ob menjavi misije
 * odstranjeni.
 */
function setupMissionSelector(map: L.Map, _csrf: string) {
  const sel  = document.getElementById("mission-select") as HTMLSelectElement | null;
  const info = document.getElementById("mission-info") as HTMLDivElement | null;
  const sendBtn = document.getElementById("send-mission") as HTMLButtonElement | null;
  if (!sel) return;

  const overlay: L.Layer[] = [];

  function clearOverlay() {
    overlay.forEach(l => map.removeLayer(l));
    overlay.length = 0;
  }

  function renderMission(mission: any) {
    clearOverlay();
    if (!mission || !mission.elements) return;
    const bounds: L.LatLng[] = [];

    let wpIndex = 1;
    mission.elements.forEach((el: any) => {
      if (el.element_type === "WP" && el.lat != null && el.lon != null) {
        const ll = L.latLng(el.lat, el.lon);
        bounds.push(ll);
        const marker = L.marker(ll, {
          icon: L.divIcon({
            className: "mview-wp",
            iconSize: [26, 26],
            iconAnchor: [13, 13],
            html: `<div class="mview-wp-num">${wpIndex}</div>`,
          }),
          interactive: false,
        }).addTo(map);
        overlay.push(marker);
        wpIndex++;
      } else if (el.element_type === "MAP" && el.polygon_geojson) {
        // GeoJSON je [lon,lat], Leaflet rabi [lat,lon]
        const coords = el.polygon_geojson.coordinates?.[0] || [];
        if (coords.length >= 3) {
          const latlngs = coords.map((c: number[]) => L.latLng(c[1], c[0]));
          const poly = L.polygon(latlngs, {
            color: "#d97706", weight: 2, fillOpacity: 0.1, interactive: false,
          }).addTo(map);
          overlay.push(poly);
          latlngs.forEach((l: L.LatLng) => bounds.push(l));
        }
      }
    });

    if (bounds.length > 0) {
      const b = L.latLngBounds(bounds);
      map.fitBounds(b.pad(0.1));
    }

    if (info) {
      const wpCount = mission.elements.filter((e: any) => e.element_type === "WP").length;
      const mapCount = mission.elements.filter((e: any) => e.element_type === "MAP").length;
      info.textContent =
        `${wpCount} waypoint(ov), ${mapCount} map gradnik(ov) · ` +
        `vzlet ${mission.default_altitude_m} m, hitr. ${mission.default_speed_ms} m/s`;
    }
    if (sendBtn) sendBtn.disabled = false;
  }

  sel.addEventListener("change", async () => {
    const id = sel.value;
    if (!id) {
      clearOverlay();
      if (info) info.textContent = "";
      if (sendBtn) sendBtn.disabled = true;
      return;
    }
    if (info) info.textContent = "Nalagam misijo...";
    try {
      const res = await fetch(`/api/missions/${id}/get/`, {
        credentials: "same-origin",
      });
      if (!res.ok) throw new Error(`${res.status}`);
      const mission = await res.json();
      renderMission(mission);
    } catch (e) {
      if (info) info.textContent = `Napaka pri nalaganju: ${(e as Error).message}`;
      if (sendBtn) sendBtn.disabled = true;
    }
  });

  if (sendBtn) {
    sendBtn.addEventListener("click", async () => {
      const id = sel.value;
      if (!id) return;
      const origLabel = sendBtn.textContent || "Pošlji na drona";
      const setBusy = (label: string) => { sendBtn.disabled = true; sendBtn.textContent = label; };
      const setIdle = () => { sendBtn.disabled = false; sendBtn.textContent = origLabel; };
      setBusy("Pošiljam…");
      if (info) info.textContent = "Pošiljam misijo na Pixhawk…";
      try {
        const csrf = (window.PLANNER_BOOTSTRAP as any).csrfToken;
        const camChk = document.getElementById("mission-camera") as HTMLInputElement | null;
        const res = await fetch(`/api/missions/${id}/upload/`, {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
          body: JSON.stringify({ camera_enabled: camChk ? camChk.checked : true }),
        });
        const data = await res.json().catch(() => ({}));
        if (data.ok) {
          const warn = (data.warnings || []).length > 0
            ? ` ⚠ ${(data.warnings || []).join(" ")}` : "";
          if (info) {
            info.textContent =
              `✓ Poslano: ${data.point_count} točk → ${data.count} MAVLink ukazov. ` +
              `ACK: ${data.ack}${data.elapsed_ms ? `, ${data.elapsed_ms} ms` : ""}.${warn}`;
          }
        } else if (data.auth_required) {
          if (info) {
            info.innerHTML = `✗ ${data.error} ` +
              `<a href="${data.login_url || "/sl/prijava/"}">Prijava</a>`;
          }
        } else {
          if (info) info.textContent = `✗ ${data.error || "Neznana napaka"}` +
            (data.uploaded !== undefined ? ` (poslanih ${data.uploaded}/${data.count})` : "");
        }
      } catch (e) {
        if (info) info.textContent = `✗ Napaka: ${(e as Error).message}`;
      } finally {
        setIdle();
      }
    });
  }
}

/* ===========================================================================
 * Planner (originalno)
 * ===========================================================================
 */
function bootstrapPlanner() {
  const cfg = window.PLANNER_BOOTSTRAP;
  if (!cfg) { console.error("PLANNER_BOOTSTRAP missing"); return; }

  const lat0 = cfg.mission!.home_lat ?? 46.0569;
  const lon0 = cfg.mission!.home_lon ?? 14.5058;
  const map = createMap("map", lat0, lon0);
  map.doubleClickZoom.disable();

  const state = new PlannerState(cfg.mission!);
  state.mission.elements.forEach((e, i) => {
    if (e.id === undefined || e.id === null) e.id = state.newTempId();
    e.order = i + 1;
  });

  const layer = new ElementLayer(map, state, cfg.drones!);
  attachTools(map, state);

  setupTelemetry(map, cfg.csrfToken);

  const tfOverlay = document.getElementById("test-flight-overlay") as HTMLDivElement | null;
  const tfBar     = document.getElementById("tf-bar") as HTMLDivElement | null;
  const tfInfo    = document.getElementById("tf-info") as HTMLDivElement | null;
  const tfPlay    = document.getElementById("tf-play") as HTMLButtonElement | null;
  const tfPause   = document.getElementById("tf-pause") as HTMLButtonElement | null;
  const tfStop    = document.getElementById("tf-stop") as HTMLButtonElement | null;
  const tfSpeed   = document.getElementById("tf-speed") as HTMLSelectElement | null;

  const tf = new TestFlight(map, (info) => {
    if (!tfOverlay) return;
    if (tfBar) tfBar.style.width = `${info.progress * 100}%`;
    if (tfInfo) {
      tfInfo.textContent =
        `${formatTime(info.elapsedS)} / ${formatTime(info.totalS)}` +
        `  ·  ${(info.totalDistanceM/1000).toFixed(2)} km` +
        `  ·  ${info.currentIdx+1}/${info.totalPts}`;
    }
    if (tfPlay) tfPlay.style.display = info.playing ? "none" : "";
    if (tfPause) tfPause.style.display = info.playing ? "" : "none";
  });

  document.getElementById("btn-test-flight")?.addEventListener("click", () => {
    if (!tf.loadMission(state.mission, cfg.drones!)) {
      alert("Misija mora imeti vsaj 2 tocki za test let.");
      return;
    }
    if (tfOverlay) tfOverlay.style.display = "block";
    tf.start();
  });
  tfPlay?.addEventListener("click", () => tf.start());
  tfPause?.addEventListener("click", () => tf.pause());
  tfStop?.addEventListener("click", () => {
    tf.stop();
    if (tfOverlay) tfOverlay.style.display = "none";
  });
  tfSpeed?.addEventListener("change", () => { tf.setMultiplier(Number(tfSpeed!.value)); });

  const renderAll = () => {
    renderSidebar(state, cfg.drones!);
    layer.redrawAll();
    const c = map.getContainer();
    if (state.toolMode === "add_wp") c.style.cursor = "crosshair";
    else if (state.toolMode === "add_map") c.style.cursor = "cell";
    else c.style.cursor = "";
  };
  state.subscribe(renderAll);
  renderAll();

  const layout = document.querySelector(".planner-layout");
  const setPanelOpen = (open: boolean) => {
    layout?.classList.toggle("panel-open", open);
    document.body.classList.toggle("planner-panel-open", open);
    window.setTimeout(() => map.invalidateSize(), 240);
  };
  document.getElementById("btn-panel-toggle")?.addEventListener("click", () => {
    setPanelOpen(!layout?.classList.contains("panel-open"));
  });
  document.getElementById("btn-sidebar-close")?.addEventListener("click", () => setPanelOpen(false));

  const toggleTool = (mode: "add_wp" | "add_map") => {
    const next = state.toolMode === mode ? "select" : mode;
    state.setToolMode(next);
    // Ob risanju zapri panel, da je karta prosta za prst.
    if (next !== "select") setPanelOpen(false);
  };
  const deleteSelected = () => {
    if (state.selectedId !== null) state.removeElement(state.selectedId);
  };

  document.getElementById("btn-add-wp")?.addEventListener("click", () => toggleTool("add_wp"));
  document.getElementById("btn-add-map")?.addEventListener("click", () => toggleTool("add_map"));
  document.getElementById("fab-add-wp")?.addEventListener("click", () => toggleTool("add_wp"));
  document.getElementById("fab-add-map")?.addEventListener("click", () => toggleTool("add_map"));
  document.getElementById("btn-delete")?.addEventListener("click", deleteSelected);
  document.getElementById("fab-delete")?.addEventListener("click", deleteSelected);

  window.addEventListener("resize", () => {
    map.invalidateSize();
  });

  document.getElementById("save-btn")?.addEventListener("click", async () => {
    try {
      const result = await saveMission(cfg.apiUrl!, cfg.csrfToken, state.mission);
      state.mission.id = result.id;
      result.elements.forEach((e, i) => {
        const local = state.mission.elements[i];
        if (local) local.id = e.id;
      });
      state.dirty = false;
      state.notify();
      alert("Misija shranjena (#" + result.id + ").");
    } catch (e) {
      alert("Napaka pri shranjevanju: " + (e as Error).message);
    }
  });

  window.addEventListener("beforeunload", (e) => {
    if (state.dirty) { e.preventDefault(); e.returnValue = ""; }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") state.setToolMode("select");
    if (e.key === "Delete" && state.selectedId !== null) {
      state.removeElement(state.selectedId);
    }
  });
}

/* ===========================================================================
 * Router
 * ===========================================================================
 */
function bootstrap() {
  const cfg = window.PLANNER_BOOTSTRAP;
  if (!cfg) { console.error("PLANNER_BOOTSTRAP missing"); return; }

  // Pasica za preklop WiFi (fizicno stikalo) je skupna vsem trem nacinom:
  // ne glede na to, katero stran ima pilot odprto, mora vedeti, da se mu
  // bo omrezje kmalu spremenilo.
  setupNetworkBanner(cfg.csrfToken);

  const mode = (cfg as any).mode;
  if (mode === "dashboard") {
    bootstrapDashboard();
  } else if (mode === "testflight") {
    setupAutoTest(cfg.csrfToken);
  } else if (mode === "magcal") {
    setupMagCal(cfg.csrfToken, !!(cfg as { autoStart?: boolean }).autoStart);
  } else if (mode === "settings") {
    setupSettings(cfg.csrfToken);
  } else if (mode === "gallery") {
    setupGallery(
      cfg.csrfToken,
      !!(cfg as { canControl?: boolean }).canControl,
    );
  } else {
    bootstrapPlanner();
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", bootstrap);
} else { bootstrap(); }
