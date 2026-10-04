/** Interakcije centralne strani nastavitev. */

interface ApiResult {
  ok?: boolean;
  error?: string;
  value?: number;
  auth_required?: boolean;
}

interface UsbDevice {
  path: string;
  label?: string | null;
  model?: string | null;
  filesystem?: string | null;
  size_b: number;
  mountpoints: string[];
  selected: boolean;
  usable: boolean;
}

function input(id: string): HTMLInputElement | null {
  return document.getElementById(id) as HTMLInputElement | null;
}

function numberValue(id: string, fallback: number): number {
  const value = Number.parseFloat(input(id)?.value || "");
  return Number.isFinite(value) ? value : fallback;
}

function message(id: string, text: string, kind = ""): void {
  const element = document.getElementById(id);
  if (!element) return;
  element.textContent = text;
  element.className = `settings-message${kind ? ` ${kind}` : ""}`;
}

function formatSize(bytes: number): string {
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(1)} GB`;
  if (bytes >= 1e6) return `${(bytes / 1e6).toFixed(0)} MB`;
  return `${bytes} B`;
}

async function jsonRequest(
  url: string,
  options: RequestInit = {},
): Promise<{ response: Response; data: any }> {
  const response = await fetch(url, {
    credentials: "same-origin",
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  return { response, data };
}

/** FC parameter z gumboma Preberi/Shrani. */
interface FcParamControl {
  name: string;
  inputId: string;
  readButtonId: string;
  saveButtonId: string;
  msgId: string;
  /** Privzeta vrednost v enoti UI. */
  defaultUi: number;
  clampUi: (v: number) => number;
  /** UI → vrednost na krmilniku. */
  toFc: (ui: number) => number;
  /** Vrednost s krmilnika → UI. */
  fromFc: (fc: number) => number;
  formatUi: (ui: number) => string;
  unitLabel: string;
  confirmSave?: (ui: number) => string | null;
}

function wireFcParam(
  csrf: string,
  control: FcParamControl,
): {
  read: () => Promise<void>;
  setConnected: (connected: boolean) => void;
} {
  const field = input(control.inputId);
  const readButton = document.getElementById(
    control.readButtonId) as HTMLButtonElement | null;
  const saveButton = document.getElementById(
    control.saveButtonId) as HTMLButtonElement | null;

  async function read(): Promise<void> {
    if (readButton) readButton.disabled = true;
    message(control.msgId, "Berem parameter…");
    try {
      const { data } = await jsonRequest(
        `/api/mavlink/param/?name=${encodeURIComponent(control.name)}`);
      if (data.ok && typeof data.value === "number") {
        const ui = control.clampUi(control.fromFc(data.value));
        if (field) field.value = control.formatUi(ui);
        message(
          control.msgId,
          `✓ Na krmilniku: ${control.formatUi(ui)} ${control.unitLabel}`,
          "ok",
        );
      } else {
        message(
          control.msgId,
          `✗ ${data.error || "Branje ni uspelo"}`,
          "err",
        );
      }
    } catch (error) {
      message(control.msgId, `✗ ${(error as Error).message}`, "err");
    } finally {
      if (readButton) readButton.disabled = false;
    }
  }

  readButton?.addEventListener("click", () => { void read(); });

  saveButton?.addEventListener("click", async () => {
    const ui = control.clampUi(numberValue(control.inputId, control.defaultUi));
    const confirmText = control.confirmSave?.(ui);
    if (confirmText && !window.confirm(confirmText)) return;
    saveButton.disabled = true;
    message(control.msgId, "Shranjujem…");
    try {
      const { data } = await jsonRequest("/api/mavlink/param/set/", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
        body: JSON.stringify({
          name: control.name,
          value: control.toFc(ui),
          confirm: true,
        }),
      });
      if (data.ok && typeof data.value === "number") {
        const savedUi = control.clampUi(control.fromFc(data.value));
        if (field) field.value = control.formatUi(savedUi);
        message(
          control.msgId,
          `✓ Shranjeno: ${control.formatUi(savedUi)} ${control.unitLabel}`,
          "ok",
        );
      } else {
        message(
          control.msgId,
          `✗ ${data.error || "Zapis ni uspel"}`,
          "err",
        );
      }
    } catch (error) {
      message(control.msgId, `✗ ${(error as Error).message}`, "err");
    } finally {
      saveButton.disabled = false;
    }
  });

  return {
    read,
    setConnected(connected: boolean) {
      if (readButton) readButton.disabled = !connected;
    },
  };
}

export function setupSettings(csrf: string): void {
  const fcStatus = document.getElementById("settings-fc-status");
  const testSaveButton = document.getElementById(
    "settings-tf-save") as HTMLButtonElement | null;
  const usbSelect = document.getElementById(
    "settings-usb-device") as HTMLSelectElement | null;
  const usbRefresh = document.getElementById(
    "settings-usb-refresh") as HTMLButtonElement | null;
  const usbUse = document.getElementById(
    "settings-usb-select") as HTMLButtonElement | null;

  const magthresh = wireFcParam(csrf, {
    name: "ARMING_MAGTHRESH",
    inputId: "settings-magthresh",
    readButtonId: "settings-magthresh-read",
    saveButtonId: "settings-magthresh-save",
    msgId: "settings-magthresh-msg",
    defaultUi: 100,
    clampUi: (v) => Math.max(0, Math.min(500, Math.round(v))),
    toFc: (ui) => ui,
    fromFc: (fc) => fc,
    formatUi: (ui) => String(Math.round(ui)),
    unitLabel: "mG",
    confirmSave: (ui) => (ui === 0
      ? "ARMING_MAGTHRESH = 0 izklopi PreArm preverjanje. Nadaljujem?"
      : null),
  });

  // ArduCopter RTL_ALT je v cm; UI prikazuje metre (privzeto 3 m).
  const rtlAlt = wireFcParam(csrf, {
    name: "RTL_ALT",
    inputId: "settings-rtl-alt",
    readButtonId: "settings-rtl-alt-read",
    saveButtonId: "settings-rtl-alt-save",
    msgId: "settings-rtl-alt-msg",
    defaultUi: 3,
    clampUi: (v) => Math.max(0, Math.min(80, Math.round(v * 2) / 2)),
    toFc: (ui) => Math.round(ui * 100),
    fromFc: (fc) => fc / 100,
    formatUi: (ui) => (Number.isInteger(ui) ? String(ui) : ui.toFixed(1)),
    unitLabel: "m",
  });

  const fcParams = [magthresh, rtlAlt];

  async function refreshConnection(): Promise<boolean> {
    try {
      const { response, data } = await jsonRequest("/api/telemetry/");
      const connected = response.ok && !!data.connected;
      if (fcStatus) {
        fcStatus.textContent = connected
          ? `✓ Povezano: ${data.port || "MAVLink"} · ${data.heartbeat?.mode || "—"}`
          : `✗ ${data.error || "Ni povezave s krmilnikom"}`;
        fcStatus.className = `settings-status ${connected ? "ok" : "err"}`;
      }
      const ac = data.autoconnect || {};
      const acDev = document.getElementById("settings-ac-device");
      const acAtt = document.getElementById("settings-ac-attempts");
      const acErr = document.getElementById("settings-ac-error");
      if (acDev) {
        acDev.textContent = ac.last_device
          ? `${ac.last_device}${ac.enabled === false ? " (izklopljeno)" : ""}`
          : (ac.enabled ? "—" : "izklopljeno");
      }
      if (acAtt) acAtt.textContent = String(ac.attempts ?? "—");
      if (acErr) acErr.textContent = ac.last_error || "—";
      for (const p of fcParams) p.setConnected(connected);
      return connected;
    } catch {
      if (fcStatus) {
        fcStatus.textContent = "✗ Stanja povezave ni mogoče prebrati";
        fcStatus.className = "settings-status err";
      }
      for (const p of fcParams) p.setConnected(false);
      return false;
    }
  }

  testSaveButton?.addEventListener("click", async () => {
    testSaveButton.disabled = true;
    message("settings-tf-msg", "Shranjujem…");
    const gps1 = input("settings-tf1-gps");
    const gps2 = input("settings-tf2-gps");
    try {
      const { data } = await jsonRequest("/api/testflight/params/save/", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
        body: JSON.stringify({
          hover: {
            altitude_m: numberValue("settings-tf1-alt", 3),
            hover_s: numberValue("settings-tf1-hover", 5),
            countdown_s: numberValue("settings-tf1-countdown", 5),
            min_satellites: numberValue("settings-tf1-sats", 10),
            max_hdop: numberValue("settings-tf1-hdop", 1.5),
            require_gps: gps1?.checked ?? true,
          },
          hop: {
            altitude_m: numberValue("settings-tf2-alt", 2),
            leg_m: numberValue("settings-tf2-leg", 2),
            countdown_s: numberValue("settings-tf2-countdown", 5),
            min_satellites: numberValue("settings-tf2-sats", 12),
            max_hdop: numberValue("settings-tf2-hdop", 1.2),
            require_gps: gps2?.checked ?? true,
          },
        }),
      });
      message(
        "settings-tf-msg",
        data.ok ? "✓ Nastavitve so shranjene." : `✗ ${data.error || "Shranjevanje ni uspelo"}`,
        data.ok ? "ok" : "err",
      );
    } catch (error) {
      message("settings-tf-msg", `✗ ${(error as Error).message}`, "err");
    } finally {
      testSaveButton.disabled = false;
    }
  });

  async function refreshStorage(): Promise<void> {
    const { data } = await jsonRequest("/api/logs/");
    const storage = document.getElementById("settings-storage");
    if (storage) {
      storage.textContent = data.storage?.available
        ? `na voljo · ${data.log_dir || ""}`
        : data.storage?.error || "ni na voljo";
      storage.className = data.storage?.available ? "ok" : "err";
    }
  }

  async function loadUsbDevices(): Promise<void> {
    if (!usbSelect) return;
    usbSelect.disabled = true;
    try {
      const { data } = await jsonRequest("/api/storage/devices/");
      const devices = (data.devices || []) as UsbDevice[];
      usbSelect.innerHTML = "";
      if (devices.length === 0) {
        usbSelect.add(new Option("Ni zaznanih USB particij", ""));
        message("settings-usb-msg", data.error || "Priklopi USB ključek.", "err");
        return;
      }
      for (const device of devices) {
        const name = device.label || device.model || device.path;
        const filesystem = device.filesystem || "brez datotečnega sistema";
        const selected = device.selected ? " · IZBRAN" : "";
        const option = new Option(
          `${name} · ${device.path} · ${filesystem} · ${formatSize(device.size_b)}${selected}`,
          device.path,
          device.selected,
          device.selected,
        );
        option.disabled = !device.usable;
        usbSelect.add(option);
      }
      message("settings-usb-msg", "");
    } catch (error) {
      usbSelect.innerHTML = "";
      usbSelect.add(new Option("Napaka pri branju naprav", ""));
      message("settings-usb-msg", `✗ ${(error as Error).message}`, "err");
    } finally {
      usbSelect.disabled = false;
    }
  }

  usbRefresh?.addEventListener("click", () => {
    void loadUsbDevices();
    void refreshStorage();
  });

  usbUse?.addEventListener("click", async () => {
    const device = usbSelect?.value || "";
    if (!device) {
      message("settings-usb-msg", "✗ Izberi uporabno USB particijo.", "err");
      return;
    }
    if (!window.confirm(
      `Uporabim ${device} za telemetrijo in slike?\n` +
      "Naprava bo odmountana z morebitne trenutne lokacije in trajno " +
      "mountana na /mnt/uav-data.")) return;
    usbUse.disabled = true;
    message("settings-usb-msg", "Nastavljam in mountam USB…");
    try {
      const { response, data } = await jsonRequest("/api/storage/select/", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
        body: JSON.stringify({ device, confirm: true }),
      });
      if (response.ok && data.ok) {
        message(
          "settings-usb-msg",
          `✓ Izbrano trajno: ${device} → ${data.mount_point}`,
          "ok",
        );
        await Promise.all([loadUsbDevices(), refreshStorage()]);
      } else {
        message(
          "settings-usb-msg",
          `✗ ${data.error || "Izbira USB-ja ni uspela"}`,
          "err",
        );
      }
    } catch (error) {
      message("settings-usb-msg", `✗ ${(error as Error).message}`, "err");
    } finally {
      usbUse.disabled = false;
    }
  });

  void refreshConnection().then((connected) => {
    if (connected) {
      for (const p of fcParams) void p.read();
    }
  });
  void loadUsbDevices();
  void refreshStorage();
  window.setInterval(() => { void refreshConnection(); }, 4000);
}
