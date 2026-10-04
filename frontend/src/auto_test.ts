/**
 * Stran za samodejni testni polet (hover + skok z fotografijama).
 *
 * Zaporedje teče na strežniku; tu samo polling, zagon in prekinitev.
 * Test 1 in Test 2 imata ločene parametre (vključno z GPS pragovi).
 */

interface TfCheck {
  key: string;
  label: string;
  ok: boolean;
  value: string;
  required: boolean;
}

interface TfEvent {
  since_start_s: number | null;
  phase: string;
  message: string;
  ok: boolean | null;
}

interface TfStatus {
  active: boolean;
  profile?: string;
  phase: string;
  phase_label: string;
  phase_elapsed_s: number;
  elapsed_s: number | null;
  message: string;
  terminal: boolean;
  abortable: boolean;
  force_cancellable?: boolean;
  events: TfEvent[];
  checks: TfCheck[];
  checks_pass: boolean;
  altitude_m: number | null;
  target_altitude_m: number;
  peak_altitude_m: number;
  armed: boolean;
  mode: string | null;
  error?: string;
  auth_required?: boolean;
  login_url?: string;
}

interface HoverParams {
  altitude_m: number;
  hover_s: number;
  countdown_s: number;
  min_satellites: number;
  max_hdop: number;
  require_gps: boolean;
}

interface HopParams {
  altitude_m: number;
  leg_m: number;
  countdown_s: number;
  min_satellites: number;
  max_hdop: number;
  require_gps: boolean;
}

interface AllParams {
  hover: HoverParams;
  hop: HopParams;
}

const POLL_MS = 400;

const RUNNING = new Set([
  "PREFLIGHT", "COUNTDOWN", "MODE", "ARM", "TAKEOFF", "CLIMB",
  "HOVER", "GOTO_P1", "CAPTURE_1", "GOTO_HOME", "CAPTURE_2",
  "LAND", "DISARM",
]);

function phaseClass(phase: string): string {
  if (phase === "DONE") return "ok";
  if (phase === "FAILED" || phase === "ABORTED") return "err";
  if (phase === "IDLE") return "idle";
  if (phase === "COUNTDOWN") return "warn";
  return "run";
}

/** Profil, za katerega trenutno kažemo predpoletne preverbe. */
let selectedProfile: "hover" | "hop" = "hover";

function num(id: string, fallback: number): number {
  const el = document.getElementById(id) as HTMLInputElement | null;
  const v = el ? parseFloat(el.value) : NaN;
  return Number.isFinite(v) ? v : fallback;
}

function setNum(id: string, value: number): void {
  const el = document.getElementById(id) as HTMLInputElement | null;
  if (el) el.value = String(value);
}

function checked(id: string, fallback: boolean): boolean {
  const el = document.getElementById(id) as HTMLInputElement | null;
  return el ? el.checked : fallback;
}

function setChecked(id: string, value: boolean): void {
  const el = document.getElementById(id) as HTMLInputElement | null;
  if (el) el.checked = value;
}

function readHover(): HoverParams {
  return {
    altitude_m: num("tf1-alt", 3),
    hover_s: num("tf1-hover", 5),
    countdown_s: num("tf1-countdown", 5),
    min_satellites: num("tf1-sats", 10),
    max_hdop: num("tf1-hdop", 1.5),
    require_gps: checked("tf1-require-gps", true),
  };
}

function readHop(): HopParams {
  return {
    altitude_m: num("tf2-alt", 2),
    leg_m: num("tf2-leg", 2),
    countdown_s: num("tf2-countdown", 5),
    min_satellites: num("tf2-sats", 12),
    max_hdop: num("tf2-hdop", 1.2),
    require_gps: checked("tf2-require-gps", true),
  };
}

function readAll(): AllParams {
  return { hover: readHover(), hop: readHop() };
}

function applyAll(p: Partial<AllParams> & Partial<HoverParams>): void {
  const hover = p.hover ?? (
    p.altitude_m !== undefined || p.min_satellites !== undefined
      ? p as Partial<HoverParams>
      : undefined
  );
  if (hover) {
    if (hover.altitude_m !== undefined) setNum("tf1-alt", hover.altitude_m);
    if (hover.hover_s !== undefined) setNum("tf1-hover", hover.hover_s);
    if (hover.countdown_s !== undefined) setNum("tf1-countdown", hover.countdown_s);
    if (hover.min_satellites !== undefined) setNum("tf1-sats", hover.min_satellites);
    if (hover.max_hdop !== undefined) setNum("tf1-hdop", hover.max_hdop);
    if (hover.require_gps !== undefined) setChecked("tf1-require-gps", !!hover.require_gps);
  }
  if (p.hop) {
    const hop = p.hop;
    if (hop.altitude_m !== undefined) setNum("tf2-alt", hop.altitude_m);
    if (hop.leg_m !== undefined) setNum("tf2-leg", hop.leg_m);
    if (hop.countdown_s !== undefined) setNum("tf2-countdown", hop.countdown_s);
    if (hop.min_satellites !== undefined) setNum("tf2-sats", hop.min_satellites);
    if (hop.max_hdop !== undefined) setNum("tf2-hdop", hop.max_hdop);
    if (hop.require_gps !== undefined) setChecked("tf2-require-gps", !!hop.require_gps);
  }
}

function paramsFor(profile: "hover" | "hop"): HoverParams | HopParams {
  return profile === "hop" ? readHop() : readHover();
}

function fmtAlt(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : `${v.toFixed(2)} m`;
}

function profileLabel(p: string | undefined): string {
  if (p === "hop") return "skok";
  if (p === "hover") return "vzlet";
  return "—";
}

export function setupAutoTest(csrf: string): void {
  const checksEl = document.getElementById("tf-checks");
  const eventsEl = document.getElementById("tf-events");
  const phaseEl = document.getElementById("tf-phase");
  const phaseName = phaseEl?.querySelector(".tf-phase-name") as HTMLElement | null;
  const phaseMsg = document.getElementById("tf-phase-msg");
  const altNow = document.getElementById("tf-alt-now");
  const altPeak = document.getElementById("tf-alt-peak");
  const modeEl = document.getElementById("tf-mode");
  const armedEl = document.getElementById("tf-armed");
  const elapsedEl = document.getElementById("tf-elapsed");
  const profileEl = document.getElementById("tf-profile");
  const btnHover = document.getElementById("tf-start-hover") as HTMLButtonElement | null;
  const btnHop = document.getElementById("tf-start-hop") as HTMLButtonElement | null;
  const btnAbort = document.getElementById("tf-abort") as HTMLButtonElement | null;
  const btnForce = document.getElementById("tf-force-abort") as HTMLButtonElement | null;
  const btnSave = document.getElementById("tf-save") as HTMLButtonElement | null;
  const saveMsg = document.getElementById("tf-save-msg");
  if (!phaseEl || !btnHover || !btnHop || !btnAbort) return;

  let lastEventCount = -1;
  let busy = false;

  btnSave?.addEventListener("click", async () => {
    if (btnSave) btnSave.disabled = true;
    try {
      const all = readAll();
      const r = await fetch("/api/testflight/params/save/", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
        body: JSON.stringify(all),
      });
      const data = await r.json().catch(() => ({})) as {
        ok?: boolean; error?: string; auth_required?: boolean;
        login_url?: string; params?: AllParams & HoverParams;
      };
      if (data.params) applyAll(data.params);
      if (saveMsg) {
        if (data.ok) {
          saveMsg.className = "tf-save-msg";
          saveMsg.textContent = "✓ Shranjeno na napravi";
          window.setTimeout(() => { if (saveMsg) saveMsg.textContent = ""; }, 2500);
        } else if (data.auth_required) {
          saveMsg.className = "tf-save-msg err";
          saveMsg.innerHTML =
            `${data.error || "Prijava"} <a href="${data.login_url || "/sl/prijava/"}">Prijava</a>`;
        } else {
          saveMsg.className = "tf-save-msg err";
          saveMsg.textContent = `✗ ${data.error || "Ni mogoče shraniti"}`;
        }
      }
    } catch {
      if (saveMsg) {
        saveMsg.className = "tf-save-msg err";
        saveMsg.textContent = "✗ Ni mogoče shraniti";
      }
    } finally {
      if (btnSave) btnSave.disabled = false;
    }
  });

  function renderChecks(checks: TfCheck[]): void {
    if (!checksEl) return;
    if (checks.length === 0) {
      checksEl.innerHTML = '<li class="tf-check pending">Ni podatkov.</li>';
      return;
    }
    checksEl.innerHTML = checks.map((c) => {
      const cls = c.ok ? "ok" : (c.required ? "err" : "warn");
      const mark = c.ok ? "✓" : "✗";
      const opt = c.required ? "" : ' <em class="tf-opt">(neobvezno)</em>';
      return `<li class="tf-check ${cls}"><span class="tf-mark">${mark}</span>
        <span class="tf-label">${c.label}${opt}</span>
        <span class="tf-value">${c.value}</span></li>`;
    }).join("");
  }

  function renderEvents(events: TfEvent[]): void {
    if (!eventsEl || events.length === lastEventCount) return;
    lastEventCount = events.length;
    if (events.length === 0) {
      eventsEl.innerHTML = '<li class="tf-empty">Zaporedje še ni bilo zagnano.</li>';
      return;
    }
    eventsEl.innerHTML = events.map((e) => {
      const cls = e.ok === false ? "err" : (e.ok === true ? "ok" : "");
      const t = e.since_start_s === null ? "" : `+${e.since_start_s.toFixed(1)} s`;
      return `<li class="${cls}"><span class="tf-ev-t">${t}</span>
        <span class="tf-ev-ph">${e.phase}</span>
        <span class="tf-ev-msg">${e.message}</span></li>`;
    }).join("");
    eventsEl.scrollTop = eventsEl.scrollHeight;
  }

  function render(s: TfStatus): void {
    renderChecks(s.checks || []);
    renderEvents(s.events || []);

    phaseEl!.className = `tf-phase ${phaseClass(s.phase)}`;
    if (phaseName) phaseName.textContent = s.phase_label || s.phase;
    if (phaseMsg) phaseMsg.textContent = s.message || "—";

    if (altNow) {
      altNow.textContent = fmtAlt(s.altitude_m);
      if (RUNNING.has(s.phase) && s.target_altitude_m) {
        altNow.textContent += ` / ${s.target_altitude_m.toFixed(1)} m`;
      }
    }
    if (altPeak) altPeak.textContent = fmtAlt(s.peak_altitude_m);
    if (modeEl) modeEl.textContent = s.mode || "—";
    if (armedEl) {
      armedEl.textContent = s.armed ? "ARMED" : "DISARMED";
      armedEl.className = s.armed ? "armed" : "disarmed";
    }
    if (elapsedEl) {
      elapsedEl.textContent =
        s.elapsed_s === null ? "—" : `${s.elapsed_s.toFixed(0)} s`;
    }
    if (profileEl) {
      profileEl.textContent = profileLabel(s.profile || selectedProfile);
    }

    const running = s.active || RUNNING.has(s.phase);
    const canStart = !busy && !running && !!s.checks_pass;
    btnHover!.disabled = !canStart;
    btnHop!.disabled = !canStart;
    btnAbort!.disabled = busy || (!running && !s.armed);
    if (btnForce) {
      const canForce = !!(s.force_cancellable || running || s.armed
        || (s.phase !== "IDLE" && s.phase !== "DONE"
            && s.phase !== "ABORTED" && s.phase !== "FAILED"));
      btnForce.disabled = busy || !canForce;
    }
    const tip = s.checks_pass ? "" : "Predpoletne preverbe še niso izpolnjene.";
    btnHover!.title = tip;
    btnHop!.title = tip;
  }

  async function poll(): Promise<void> {
    try {
      const p = paramsFor(selectedProfile);
      const qs = new URLSearchParams({
        profile: selectedProfile,
        altitude_m: String(p.altitude_m),
        min_satellites: String(p.min_satellites),
        max_hdop: String(p.max_hdop),
        require_gps: p.require_gps ? "1" : "0",
      });
      const r = await fetch(`/api/testflight/status/?${qs}`,
                            { credentials: "same-origin" });
      if (!r.ok) return;
      render(await r.json());
    } catch {
      /* tiho */
    }
  }

  async function post(url: string, body: unknown): Promise<TfStatus> {
    const r = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
      body: JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({})) as TfStatus & {
      status?: TfStatus; ok?: boolean;
    };
    if (data.status && typeof data.status === "object") {
      return {
        ...data.status,
        error: data.error ?? data.status.error,
        auth_required: data.auth_required ?? data.status.auth_required,
        login_url: data.login_url ?? data.status.login_url,
      };
    }
    return data as TfStatus;
  }

  async function startProfile(profile: "hover" | "hop"): Promise<void> {
    selectedProfile = profile;
    const p = paramsFor(profile);
    const extra = p.require_gps ? "" :
      "\n\nGPS NI ZAHTEVAN — brez GPS se dron ne bo držal pozicije!";

    let msg: string;
    if (profile === "hop") {
      const hop = p as HopParams;
      msg =
        `Test 2 — skok:\n` +
        `• vzlet na ${hop.altitude_m} m\n` +
        `• ${hop.leg_m} m proti severu + slika\n` +
        `• vrnitev + slika\n` +
        `• pristanek\n\n` +
        `Zahteva ≥${hop.min_satellites} satelitov, HDOP ≤${hop.max_hdop}.\n` +
        `Je prostor proti severu prost in imaš RC v roki?${extra}`;
    } else {
      const hover = p as HoverParams;
      msg =
        `Test 1 — vzlet:\n` +
        `Dron bo vzletel na ${hover.altitude_m} m, lebdel ${hover.hover_s} s in pristal.\n` +
        `Zahteva ≥${hover.min_satellites} satelitov, HDOP ≤${hover.max_hdop}.\n` +
        `Je območje prosto in imaš RC oddajnik v roki?${extra}`;
    }
    if (!window.confirm(msg)) return;

    busy = true;
    btnHover!.disabled = true;
    btnHop!.disabled = true;
    try {
      const res = await post("/api/testflight/start/", {
        confirm: true,
        profile,
        ...p,
      });
      if (res.phase) render(res);
      if (res.auth_required && phaseMsg) {
        phaseMsg.innerHTML =
          `${res.error} <a href="${res.login_url || "/prijava/"}">Prijava</a>`;
      } else if (res.error && phaseMsg) {
        phaseMsg.textContent = `✗ ${res.error}`;
      }
    } catch {
      if (phaseMsg) phaseMsg.textContent = "✗ Zagon ni uspel (omrežje).";
    } finally {
      busy = false;
    }
    void poll();
  }

  btnHover.addEventListener("click", () => { void startProfile("hover"); });
  btnHop.addEventListener("click", () => { void startProfile("hop"); });

  btnHover.addEventListener("mouseenter", () => {
    selectedProfile = "hover";
    void poll();
  });
  btnHop.addEventListener("mouseenter", () => {
    selectedProfile = "hop";
    void poll();
  });

  btnAbort.addEventListener("click", async () => {
    busy = true;
    btnAbort.disabled = true;
    if (btnForce) btnForce.disabled = true;
    try {
      const res = await post("/api/testflight/abort/", { reason: "gumb PREKINI" });
      if (res.phase) render(res);
      if (res.auth_required && phaseMsg) {
        phaseMsg.innerHTML =
          `${res.error} <a href="${res.login_url || "/prijava/"}">Prijava</a>`;
      } else if (res.error && phaseMsg) {
        phaseMsg.textContent = `✗ ${res.error}`;
      }
    } catch {
      if (phaseMsg) phaseMsg.textContent = "✗ Prekinitev ni uspela (omrežje).";
    } finally {
      busy = false;
    }
    void poll();
  });

  btnForce?.addEventListener("click", async () => {
    if (!window.confirm(
      "FORCE CANCEL: takoj prekine zaporedje, odklene vmesnik in pošlje "
      + "LAND / force-disarm.\n\nUporabi, če se test zacikla. Nadaljuj?",
    )) return;
    busy = true;
    btnAbort.disabled = true;
    btnForce.disabled = true;
    try {
      const res = await post("/api/testflight/abort/", {
        force: true,
        reason: "gumb FORCE CANCEL",
      });
      if (res.phase) render(res);
      if (res.auth_required && phaseMsg) {
        phaseMsg.innerHTML =
          `${res.error} <a href="${res.login_url || "/prijava/"}">Prijava</a>`;
      } else if (res.error && phaseMsg) {
        phaseMsg.textContent = `✗ ${res.error}`;
      } else if (phaseMsg) {
        phaseMsg.textContent = res.message || "Vsili prekinitev.";
      }
    } catch {
      if (phaseMsg) phaseMsg.textContent = "✗ Force cancel ni uspel (omrežje).";
    } finally {
      busy = false;
    }
    void poll();
  });

  void poll();
  setInterval(poll, POLL_MS);
}
