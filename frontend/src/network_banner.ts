/**
 * Pasica za preklop Wi-Fi omrezja prek fizicnega stikala (GPIO19 -> GND).
 *
 * Polling /api/network/status/ zivi v base.html (ena mreza za vse strani).
 * Ta modul samo poslusca dogodek `uav-network-status` in prikaze odlozeni
 * preklop + gumb "Preklopi zdaj".
 */

interface NetworkPending {
  target: "CLIENT" | "AP";
  deadline: number;
  remaining_s: number;
}

interface NetworkStatus {
  available: boolean;
  mode?: "CLIENT" | "AP";
  pending?: NetworkPending | null;
  error?: string;
}

const MODE_LABEL: Record<string, string> = {
  CLIENT: "klient (domače omrežje)",
  AP: "dostopna točka (hotspot)",
};

function ensureBanner(): HTMLDivElement {
  let el = document.getElementById("net-switch-banner") as HTMLDivElement | null;
  if (el) return el;
  el = document.createElement("div");
  el.id = "net-switch-banner";
  el.className = "net-switch-banner";
  el.style.display = "none";
  el.innerHTML = `
    <span class="net-switch-text"></span>
    <button type="button" class="net-switch-btn">Preklopi zdaj</button>
  `;
  document.body.prepend(el);
  return el;
}

export function setupNetworkBanner(csrf: string): void {
  const banner = ensureBanner();
  const textEl = banner.querySelector(".net-switch-text") as HTMLSpanElement;
  const btn = banner.querySelector(".net-switch-btn") as HTMLButtonElement;

  let busy = false;

  function render(pending: NetworkPending | null | undefined): void {
    if (!pending) {
      banner.style.display = "none";
      return;
    }
    banner.style.display = "flex";
    const label = MODE_LABEL[pending.target] || pending.target;
    const s = Math.max(0, Math.ceil(pending.remaining_s));
    textEl.textContent =
      `⚠ Omrežje se bo prek fizičnega stikala preklopilo v način "${label}" ` +
      `čez ${s} s.`;
    btn.disabled = busy;
  }

  function onStatus(data: NetworkStatus): void {
    if (!data.available) {
      banner.style.display = "none";
      return;
    }
    render(data.pending);
  }

  window.addEventListener("uav-network-status", ((ev: CustomEvent<NetworkStatus>) => {
    onStatus(ev.detail || { available: false });
  }) as EventListener);

  // Fallback: ce base.html poller iz kaksnega razloga ni aktiven (testi).
  if (!(window as unknown as { __UAV_NET_POLL__?: boolean }).__UAV_NET_POLL__) {
    const POLL_MS = 2000;
    async function poll(): Promise<void> {
      try {
        const r = await fetch("/api/network/status/", { credentials: "same-origin" });
        if (!r.ok) return;
        onStatus(await r.json());
      } catch {
        /* tiho */
      }
    }
    void poll();
    setInterval(poll, POLL_MS);
  }

  btn.addEventListener("click", async () => {
    busy = true;
    btn.disabled = true;
    btn.textContent = "Preklapljam…";
    try {
      await fetch("/api/network/force/", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
        body: "{}",
      });
    } catch {
      /* poll bo osvezil */
    } finally {
      busy = false;
      btn.textContent = "Preklopi zdaj";
    }
  });
}
