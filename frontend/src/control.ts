/**
 * Kontrola letalnika in zapis telemetrije z nadzorne plosce.
 *
 * Vsi klici gredo na konciscima, ki jih varuje `control_required`; ce
 * uporabnik ni prijavljen, streznik vrne 403 z `auth_required: true` in
 * tu prikazemo povezavo na prijavo namesto splosne napake.
 *
 * Nepovratna dejanja (ARM, zagon misije) posiljajo `confirm: true` in imajo
 * pred tem se brskalnikov `confirm()` dialog --- dvojna varovalka je pri
 * fizicni napravi s propelerji smiselna.
 */

interface CmdResult {
  ok?: boolean;
  error?: string | null;
  result_name?: string;
  label?: string;
  auth_required?: boolean;
  login_url?: string;
  steps?: Array<{ step: string; ok?: boolean; error?: string | null }>;
  statustexts?: Array<{ severity: number; text: string }>;
}

interface LogSession {
  name: string;
  started_at: string | null;
  duration_s: number | null;
  mission_name: string | null;
  messages: number | null;
  running: boolean;
  size_b: number;
  has_plan: boolean;
  has_captures: boolean;
}

function post(url: string, csrf: string, body: unknown): Promise<CmdResult> {
  return fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
    body: JSON.stringify(body ?? {}),
  }).then(async (r) => {
    const data = await r.json().catch(() => ({}));
    return { ok: r.ok && data.ok !== false, ...data } as CmdResult;
  });
}

function fmtBytes(b: number): string {
  if (b >= 1e6) return `${(b / 1e6).toFixed(1)} MB`;
  if (b >= 1e3) return `${(b / 1e3).toFixed(0)} kB`;
  return `${b} B`;
}

function fmtDuration(s: number | null): string {
  if (s === null || s === undefined) return "—";
  const m = Math.floor(s / 60);
  const sec = Math.round(s % 60);
  return m > 0 ? `${m} min ${sec} s` : `${sec} s`;
}

export function setupControl(csrf: string): void {
  const modeSel = document.getElementById("ctl-mode") as HTMLSelectElement | null;
  const modeSet = document.getElementById("ctl-mode-set") as HTMLButtonElement | null;
  const btnArm = document.getElementById("ctl-arm") as HTMLButtonElement | null;
  const btnDisarm = document.getElementById("ctl-disarm") as HTMLButtonElement | null;
  const btnStart = document.getElementById("ctl-start") as HTMLButtonElement | null;
  const btnRtl = document.getElementById("ctl-rtl") as HTMLButtonElement | null;
  const resultEl = document.getElementById("ctl-result") as HTMLDivElement | null;
  const stEl = document.getElementById("ctl-statustext") as HTMLDivElement | null;

  if (!resultEl) return;

  function show(res: CmdResult, okLabel: string): void {
    if (res.auth_required) {
      resultEl!.className = "ctl-result err";
      resultEl!.innerHTML =
        `✗ ${res.error || "Potrebna je prijava."} ` +
        `<a href="${res.login_url || "/sl/prijava/"}">Prijava</a>`;
      return;
    }
    if (res.ok) {
      resultEl!.className = "ctl-result ok";
      resultEl!.textContent = `✓ ${okLabel}`;
    } else {
      resultEl!.className = "ctl-result err";
      const steps = (res.steps || [])
        .map((s) => `${s.step}: ${s.ok ? "ok" : s.error || "napaka"}`)
        .join(" · ");
      resultEl!.textContent = `✗ ${res.error || "Neznana napaka"}${steps ? ` — ${steps}` : ""}`;
    }
    if (stEl && res.statustexts && res.statustexts.length > 0) {
      stEl.textContent = res.statustexts.map((s) => s.text).join(" | ");
    }
  }

  async function run(
    btn: HTMLButtonElement | null,
    busyLabel: string,
    url: string,
    body: unknown,
    okLabel: string,
  ): Promise<void> {
    const orig = btn?.textContent || "";
    if (btn) { btn.disabled = true; btn.textContent = busyLabel; }
    resultEl!.className = "ctl-result";
    resultEl!.textContent = busyLabel;
    try {
      show(await post(url, csrf, body), okLabel);
    } catch (e) {
      show({ ok: false, error: (e as Error).message }, okLabel);
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = orig; }
    }
  }

  modeSet?.addEventListener("click", () => {
    const mode = modeSel?.value;
    if (!mode) { show({ ok: false, error: "Izberi letalni način." }, ""); return; }
    void run(modeSet, "Preklapljam…", "/api/mavlink/mode/", { mode }, `Način ${mode}`);
  });

  btnArm?.addEventListener("click", () => {
    if (!window.confirm(
      "ARM zavrti motorje. Je območje prosto in so propelerji preverjeni?")) return;
    void run(btnArm, "Armiram…", "/api/mavlink/arm/",
      { arm: true, confirm: true }, "Armirano");
  });

  btnDisarm?.addEventListener("click", () => {
    void run(btnDisarm, "Dis-armiram…", "/api/mavlink/arm/",
      { arm: false, confirm: true }, "Dis-armirano");
  });

  btnStart?.addEventListener("click", () => {
    if (!window.confirm(
      "Zagon misije: ARM → AUTO → MISSION_START.\n" +
      "Po potrebi najprej GUIDED (ArduCopter ne arma v AUTO).\n" +
      "Dron bo vzletel. Nadaljujem?")) return;
    void run(btnStart, "Zaganjam…", "/api/mavlink/start/",
      { confirm: true }, "Misija zagnana");
  });

  btnRtl?.addEventListener("click", () => {
    if (!window.confirm("Preklapljam v RTL — dron se bo vrnil na vzletišče. Nadaljujem?")) return;
    void run(btnRtl, "RTL…", "/api/mavlink/mode/", { mode: "RTL" }, "RTL aktiviran");
  });
}

// ---------------------------------------------------------------------------
// Panel za zapis telemetrije
// ---------------------------------------------------------------------------
export function setupLogging(csrf: string): void {
  const statusEl = document.getElementById("log-status") as HTMLDivElement | null;
  const listEl = document.getElementById("log-list") as HTMLDivElement | null;
  const btnStart = document.getElementById("log-start") as HTMLButtonElement | null;
  const btnStop = document.getElementById("log-stop") as HTMLButtonElement | null;
  if (!statusEl) return;

  async function refresh(): Promise<void> {
    try {
      const r = await fetch("/api/logs/", { credentials: "same-origin" });
      if (!r.ok) return;
      const data = await r.json();
      const storage = data.storage || { available: true };
      if (!storage.available) {
        statusEl!.className = "log-status error";
        statusEl!.textContent =
          `⚠ zapisovanje ni na voljo — ${storage.error || "USB medij ni priklopljen"}`;
        if (btnStart) btnStart.disabled = true;
        if (btnStop) btnStop.disabled = true;
        if (listEl) {
          listEl.innerHTML =
            "<div class=\"log-empty\">Priklopi USB ključek za telemetrijo in slike.</div>";
        }
        return;
      }
      const active = data.active || {};
      if (active.active) {
        statusEl!.className = "log-status rec";
        statusEl!.textContent =
          `● zapisujem — ${active.messages ?? 0} sporočil` +
          (active.mission_name ? ` · ${active.mission_name}` : "") +
          ` (${active.reason ?? "?"})`;
      } else {
        statusEl!.className = "log-status";
        statusEl!.textContent = "○ ni aktivnega zapisa";
      }
      if (btnStart) btnStart.disabled = !!active.active;
      if (btnStop) btnStop.disabled = !active.active;

      const sessions: LogSession[] = (data.sessions || []).slice(0, 64);
      if (listEl) {
        listEl.innerHTML = sessions.length === 0
          ? "<div class=\"log-empty\">Še ni zapisanih letov.</div>"
          : sessions.map((s) => {
            const links = [
              `<a href="/api/logs/${encodeURIComponent(s.name)}/telemetry.jsonl">telemetrija</a>`,
              s.has_plan
                ? `<a href="/api/logs/${encodeURIComponent(s.name)}/plan.json">načrt</a>` : "",
              s.has_captures
                ? `<a href="/api/logs/${encodeURIComponent(s.name)}/captures.jsonl">zajem</a>` : "",
            ].filter(Boolean).join(" · ");
            return `<div class="log-item${s.running ? " running" : ""}">
              <div class="log-item-name">${s.name}</div>
              <div class="log-item-meta">${fmtDuration(s.duration_s)} ·
                ${s.messages ?? 0} sporočil · ${fmtBytes(s.size_b)}</div>
              <div class="log-item-links">${links}</div>
            </div>`;
          }).join("");
      }
    } catch {
      /* tiho — panel ni kriticen */
    }
  }

  btnStart?.addEventListener("click", async () => {
    const result = await post("/api/logs/start/", csrf, { reason: "manual" });
    if (!result.ok) {
      statusEl!.className = "log-status error";
      statusEl!.textContent =
        `⚠ ${result.error || "Zapisa ni bilo mogoče zagnati."}`;
      return;
    }
    await refresh();
  });
  btnStop?.addEventListener("click", async () => {
    await post("/api/logs/stop/", csrf, {});
    void refresh();
  });

  void refresh();
  setInterval(refresh, 4000);
}
