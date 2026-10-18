/**
 * Stran za onboard kalibracijo kompasa — samo napredek in ukazi.
 */

import { t } from "./i18n";

interface CompassSnap {
  connected: boolean;
  port?: string | null;
  error?: string | null;
  heartbeat: { armed: boolean; mode: string | null };
  compass?: {
    field_mg: number | null;
    xy_mg: number | null;
    healthy: boolean | null;
    cal_pct: number | null;
    cal_status: number | null;
    cal_status_label: string | null;
    cal_fitness: number | null;
  };
  statustexts?: Array<{ severity: number; text: string }>;
}

interface CmdResult {
  ok?: boolean;
  error?: string | null;
  auth_required?: boolean;
  login_url?: string;
  value?: number;
  name?: string;
  statustexts?: Array<{ severity: number; text: string }>;
}

const POLL_MS = 400;
const MAGTHRESH_NAME = "ARMING_MAGTHRESH";

/** MAG_CAL_STATUS: 2/3 = teče, 4 = uspeh, ≥5 = napaka */
function isRunning(status: number | null | undefined, pct: number | null | undefined): boolean {
  if (status === 1 || status === 2 || status === 3) return true;
  if (status === 0 && pct !== null && pct !== undefined && pct > 0 && pct < 100) return true;
  return false;
}

function isSuccess(status: number | null | undefined): boolean {
  return status === 4;
}

function isFailed(status: number | null | undefined): boolean {
  return status !== null && status !== undefined && status >= 5;
}

export function setupMagCal(csrf: string, autoStart: boolean): void {
  const page = document.getElementById("mc-page");
  if (!page) return;

  const connEl = document.getElementById("mc-conn");
  const phaseEl = document.getElementById("mc-phase");
  const pctEl = document.getElementById("mc-pct");
  const statusEl = document.getElementById("mc-status");
  const fillEl = document.getElementById("mc-fill");
  const hintEl = document.getElementById("mc-hint");
  const magEl = document.getElementById("mc-mag");
  const magXyEl = document.getElementById("mc-mag-xy");
  const armedEl = document.getElementById("mc-armed");
  const msgEl = document.getElementById("mc-msg");
  const stEl = document.getElementById("mc-statustext");
  const btnStart = document.getElementById("mc-start") as HTMLButtonElement | null;
  const btnCancel = document.getElementById("mc-cancel") as HTMLButtonElement | null;
  const threshInput = document.getElementById("mc-magthresh") as HTMLInputElement | null;
  const btnThreshSave = document.getElementById("mc-magthresh-save") as HTMLButtonElement | null;
  const threshMsg = document.getElementById("mc-magthresh-msg");

  let busy = false;
  let startedOnce = false;
  let wasConnected = false;
  let threshLoaded = false;

  function setMsg(text: string, cls: string = ""): void {
    if (!msgEl) return;
    msgEl.className = `mc-msg${cls ? ` ${cls}` : ""}`;
    msgEl.textContent = text;
  }

  function setThreshMsg(text: string, cls: string = ""): void {
    if (!threshMsg) return;
    threshMsg.className = `mc-msg${cls ? ` ${cls}` : ""}`;
    threshMsg.textContent = text;
  }

  async function loadMagThresh(): Promise<void> {
    try {
      const r = await fetch(
        `/api/mavlink/param/?name=${encodeURIComponent(MAGTHRESH_NAME)}`,
        { credentials: "same-origin" },
      );
      const data = await r.json().catch(() => ({})) as CmdResult;
      if (data.ok && data.value !== undefined && threshInput) {
        threshInput.value = String(Math.round(data.value));
        threshLoaded = true;
        setThreshMsg(t("smc.thresh_on_fc", { value: Math.round(data.value) }));
      } else if (!data.ok) {
        setThreshMsg(`✗ ${data.error || t("smc.read_failed")}`, "err");
      }
    } catch (e) {
      setThreshMsg(`✗ ${(e as Error).message}`, "err");
    }
  }

  async function post(action: "start" | "cancel"): Promise<CmdResult> {
    const r = await fetch("/api/mavlink/mag-cal/", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
      body: JSON.stringify({ action, confirm: true, autosave: true }),
    });
    const data = await r.json().catch(() => ({}));
    return { ok: r.ok && data.ok !== false, ...data } as CmdResult;
  }

  function render(snap: CompassSnap): void {
    const c = snap.compass;
    const pct = c?.cal_pct ?? null;
    const status = c?.cal_status ?? null;
    const running = isRunning(status, pct);
    const success = isSuccess(status);
    const failed = isFailed(status);

    if (connEl) {
      if (snap.connected) {
        connEl.className = "mc-conn ok";
        connEl.textContent = t("smc.connected", { port: snap.port || t("smc.connected_fallback"), mode: snap.heartbeat.mode || "—" });
      } else {
        connEl.className = "mc-conn off";
        connEl.textContent = snap.error
          ? `✗ ${snap.error}`
          : t("smc.no_connection");
      }
    }

    if (armedEl) {
      armedEl.textContent = snap.heartbeat.armed ? "ARMED" : "DISARMED";
      armedEl.className = snap.heartbeat.armed ? "armed" : "disarmed";
    }

    if (magEl) {
      magEl.textContent = c?.field_mg == null ? "—" : `${c.field_mg.toFixed(0)} mG`;
    }
    if (magXyEl) {
      magXyEl.textContent = c?.xy_mg == null ? "—" : `${c.xy_mg.toFixed(0)} mG`;
    }

    const showPct = pct === null || pct === undefined ? 0 : Math.max(0, Math.min(100, pct));
    if (pctEl) {
      if (success) pctEl.textContent = "100%";
      else if (pct === null || pct === undefined) pctEl.textContent = "—";
      else pctEl.textContent = `${showPct}%`;
    }
    if (fillEl) fillEl.style.width = `${success ? 100 : showPct}%`;

    let label = c?.cal_status_label || t("smc.label_ready");
    let phaseCls = "idle";
    let hint = t("smc.hint_idle");

    if (!snap.connected) {
      label = t("smc.label_no_connection");
      phaseCls = "idle";
    } else if (snap.heartbeat.armed) {
      label = t("smc.label_armed_disarm_first");
      phaseCls = "fail";
      hint = t("smc.hint_armed");
    } else if (success) {
      label = t("smc.label_success");
      phaseCls = "success";
      hint = t("smc.hint_success");
    } else if (failed) {
      label = (c?.cal_status_label || t("smc.label_error")).toUpperCase();
      phaseCls = "fail";
      hint = t("smc.hint_failed");
    } else if (running) {
      label = (c?.cal_status_label || t("smc.label_calibration")).toUpperCase();
      phaseCls = "run";
      hint = t("smc.hint_running");
    }

    if (statusEl) statusEl.textContent = label;
    if (phaseEl) phaseEl.className = `mc-phase ${phaseCls}`;
    if (hintEl) hintEl.textContent = hint;

    if (btnStart) {
      btnStart.disabled = busy || !snap.connected || snap.heartbeat.armed || running || success;
    }
    if (btnCancel) {
      btnCancel.disabled = busy || !running;
    }

    if (stEl && snap.statustexts && snap.statustexts.length > 0) {
      const last = snap.statustexts[snap.statustexts.length - 1];
      stEl.textContent = last.text;
      stEl.className = last.severity <= 3 ? "mc-statustext err" : "mc-statustext";
    }
  }

  async function poll(): Promise<void> {
    try {
      const r = await fetch("/api/telemetry/", { credentials: "same-origin" });
      if (!r.ok) return;
      const snap = await r.json() as CompassSnap;
      render(snap);
      if (snap.connected && (!wasConnected || !threshLoaded)) {
        void loadMagThresh();
      }
      if (!snap.connected) threshLoaded = false;
      wasConnected = snap.connected;
    } catch {
      /* tiho */
    }
  }

  async function startCal(): Promise<void> {
    busy = true;
    setMsg(t("smc.starting"));
    try {
      const res = await post("start");
      if (res.auth_required) {
        setMsg(
          t("smc.auth_required", { error: res.error || t("smc.login") }),
          "err",
        );
      } else if (res.ok) {
        startedOnce = true;
        setMsg(t("smc.cal_running"), "ok");
      } else {
        setMsg(`✗ ${res.error || t("smc.start_failed")}`, "err");
      }
      if (res.statustexts?.length && stEl) {
        stEl.textContent = res.statustexts.map((s) => s.text).join(" | ");
      }
    } catch (e) {
      setMsg(`✗ ${(e as Error).message}`, "err");
    } finally {
      busy = false;
      void poll();
    }
  }

  btnStart?.addEventListener("click", () => {
    if (!window.confirm(t("smc.start_confirm"))) {
      return;
    }
    void startCal();
  });

  btnCancel?.addEventListener("click", async () => {
    busy = true;
    setMsg(t("smc.cancelling"));
    try {
      const res = await post("cancel");
      setMsg(res.ok ? t("smc.cancelled") : `✗ ${res.error || t("smc.error")}`,
             res.ok ? "" : "err");
    } catch (e) {
      setMsg(`✗ ${(e as Error).message}`, "err");
    } finally {
      busy = false;
      void poll();
    }
  });

  btnThreshSave?.addEventListener("click", async () => {
    const raw = threshInput ? parseFloat(threshInput.value) : NaN;
    if (!Number.isFinite(raw)) {
      setThreshMsg(`✗ ${t("smc.enter_number_range")}`, "err");
      return;
    }
    const value = Math.max(0, Math.min(500, Math.round(raw)));
    if (value === 0 && !window.confirm(t("smc.magthresh_zero_confirm"))) {
      return;
    }
    if (btnThreshSave) btnThreshSave.disabled = true;
    setThreshMsg(t("smc.saving"));
    try {
      const r = await fetch("/api/mavlink/param/set/", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
        body: JSON.stringify({
          name: MAGTHRESH_NAME, value, confirm: true,
        }),
      });
      const data = await r.json().catch(() => ({})) as CmdResult;
      if (data.auth_required) {
        setThreshMsg(`✗ ${data.error || t("smc.login")}`, "err");
      } else if (data.ok && data.value !== undefined) {
        if (threshInput) threshInput.value = String(Math.round(data.value));
        setThreshMsg(t("smc.saved_value", { value: Math.round(data.value) }), "ok");
      } else {
        setThreshMsg(`✗ ${data.error || t("smc.write_failed")}`, "err");
      }
    } catch (e) {
      setThreshMsg(`✗ ${(e as Error).message}`, "err");
    } finally {
      if (btnThreshSave) btnThreshSave.disabled = false;
    }
  });

  void poll();
  window.setInterval(() => { void poll(); }, POLL_MS);

  if (autoStart && !startedOnce) {
    window.setTimeout(() => {
      if (!busy) void startCal();
    }, 600);
  }
}
