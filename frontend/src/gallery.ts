/**
 * Galerija: slike po misijah, skupen ZIP prenos, ostale po času.
 */

type GalImage = {
  session: string;
  file: string;
  rel: string;
  size_b: number;
  created_at: string;
  url: string;
  mission_id?: number | null;
  mission_name?: string | null;
};

type GalMission = {
  mission_id: number;
  mission_name: string;
  images: GalImage[];
  count: number;
  total_b: number;
};

type GalResponse = {
  missions: GalMission[];
  other: GalImage[];
  total_count: number;
  other_count: number;
  storage?: { available: boolean; error?: string | null };
};

function fmtTime(iso: string): string {
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString("sl-SI", {
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function fmtBytes(b: number): string {
  if (b >= 1e6) return `${(b / 1e6).toFixed(1)} MB`;
  if (b >= 1e3) return `${(b / 1e3).toFixed(0)} KB`;
  return `${b} B`;
}

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c] as string));
}

async function downloadZip(
  csrf: string,
  body: Record<string, unknown>,
): Promise<void> {
  const r = await fetch("/api/gallery/download/", {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRFToken": csrf,
    },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    let msg = `Napaka ${r.status}`;
    try {
      const j = await r.json();
      if (j.error) msg = j.error;
      if (j.auth_required && j.login_url) {
        msg += ` — <a href="${j.login_url}">prijava</a>`;
      }
    } catch { /* ignore */ }
    throw new Error(msg);
  }
  const blob = await r.blob();
  const dispo = r.headers.get("Content-Disposition") || "";
  const m = /filename="([^"]+)"/.exec(dispo);
  const name = m?.[1] || "galerija.zip";
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(a.href);
}

function selectedKeys(root: HTMLElement): string[] {
  return Array.from(
    root.querySelectorAll<HTMLInputElement>("input.gal-pick:checked"),
  ).map((el) => el.value).filter(Boolean);
}

function sessionLabel(session: string): string {
  const name = session.split("/").pop() || session;
  return name.replace(/^\d{8}-\d{6}_/, "");
}

function renderCard(img: GalImage): string {
  return `
    <article class="gal-card">
      <input type="checkbox" class="gal-pick" value="${escapeHtml(img.rel)}"
             aria-label="Izberi ${escapeHtml(img.file)}">
      <img src="${escapeHtml(img.url)}" alt="${escapeHtml(img.file)}"
           loading="lazy" data-full="${escapeHtml(img.url)}">
      <div class="gal-meta">${escapeHtml(sessionLabel(img.session))}<br>
        ${escapeHtml(fmtTime(img.created_at))} · ${escapeHtml(fmtBytes(img.size_b))}</div>
    </article>`;
}

export function setupGallery(csrf: string, canControl: boolean): void {
  const statusEl = document.getElementById("gal-status");
  const missionsEl = document.getElementById("gal-missions");
  const otherEl = document.getElementById("gal-other");
  const otherSection = document.getElementById("gal-other-section");
  const otherCount = document.getElementById("gal-other-count");
  const btnRefresh = document.getElementById("gal-refresh") as HTMLButtonElement | null;
  const btnSel = document.getElementById("gal-download-selected") as HTMLButtonElement | null;
  const btnOther = document.getElementById("gal-download-other") as HTMLButtonElement | null;
  const otherAll = document.getElementById("gal-other-all") as HTMLInputElement | null;
  if (!missionsEl || !otherEl) return;

  if (!canControl) {
    if (btnSel) btnSel.disabled = true;
    if (btnOther) btnOther.disabled = true;
  }

  // Lightbox
  let lightbox = document.querySelector<HTMLDivElement>(".gal-lightbox");
  if (!lightbox) {
    lightbox = document.createElement("div");
    lightbox.className = "gal-lightbox";
    lightbox.innerHTML = "<img alt=\"\">";
    document.body.appendChild(lightbox);
    lightbox.addEventListener("click", () => lightbox!.classList.remove("open"));
  }
  const lbImg = lightbox.querySelector("img")!;

  const syncSelectionUi = () => {
    const n = selectedKeys(document.body).length;
    if (btnSel) btnSel.disabled = !canControl || n === 0;
  };

  const bindGrid = (root: HTMLElement) => {
    root.querySelectorAll<HTMLImageElement>("img[data-full]").forEach((img) => {
      img.addEventListener("click", () => {
        lbImg.src = img.dataset.full || img.src;
        lightbox!.classList.add("open");
      });
    });
    root.querySelectorAll<HTMLInputElement>("input.gal-pick").forEach((cb) => {
      cb.addEventListener("change", syncSelectionUi);
    });
  };

  async function load() {
    if (statusEl) {
      statusEl.textContent = "Nalagam…";
      statusEl.className = "gallery-status";
    }
    try {
      const r = await fetch("/api/gallery/", { credentials: "same-origin" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = (await r.json()) as GalResponse;

      if (data.storage && data.storage.available === false) {
        if (statusEl) {
          statusEl.textContent = data.storage.error
            || "Shramba ni na voljo.";
          statusEl.className = "gallery-status error";
        }
        missionsEl!.innerHTML = "";
        otherEl!.innerHTML = "";
        if (otherSection) otherSection.style.display = "none";
        if (btnOther) btnOther.disabled = true;
        return;
      }

      if (statusEl) {
        statusEl.textContent = data.total_count
          ? `${data.total_count} slik`
          : "Ni shranjenih slik.";
      }

      missionsEl!.innerHTML = data.missions.map((g) => `
        <section class="gallery-section" data-mission="${g.mission_id}">
          <div class="gallery-section-head">
            <h2>${escapeHtml(g.mission_name)}
              <span class="gal-count">(${g.count} · ${escapeHtml(fmtBytes(g.total_b))})</span>
            </h2>
            <div class="gal-section-actions">
              <label class="gal-check-all">
                <input type="checkbox" class="gal-mission-all"
                       data-mission="${g.mission_id}"> Izberi vse
              </label>
              <button type="button" class="gal-btn gal-dl-mission"
                      data-mission="${g.mission_id}"
                      ${canControl ? "" : "disabled"}>Prenesi misijo</button>
            </div>
          </div>
          <div class="gallery-grid">
            ${g.images.map(renderCard).join("")}
          </div>
        </section>
      `).join("") || `<p class="gallery-empty">Ni slik, povezanih z misijami.</p>`;

      if (data.other.length) {
        if (otherSection) otherSection.style.display = "";
        if (otherCount) otherCount.textContent = `(${data.other_count})`;
        otherEl!.innerHTML = data.other.map(renderCard).join("");
        if (btnOther) btnOther.disabled = !canControl;
      } else {
        if (otherSection) otherSection.style.display = "none";
        otherEl!.innerHTML = "";
        if (btnOther) btnOther.disabled = true;
      }

      bindGrid(missionsEl!);
      bindGrid(otherEl!);

      missionsEl!.querySelectorAll<HTMLInputElement>(".gal-mission-all").forEach((cb) => {
        cb.addEventListener("change", () => {
          const mid = cb.dataset.mission;
          const section = missionsEl!.querySelector(
            `section[data-mission="${mid}"]`,
          );
          section?.querySelectorAll<HTMLInputElement>("input.gal-pick")
            .forEach((pick) => { pick.checked = cb.checked; });
          syncSelectionUi();
        });
      });

      missionsEl!.querySelectorAll<HTMLButtonElement>(".gal-dl-mission").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const mid = Number(btn.dataset.mission);
          btn.disabled = true;
          try {
            await downloadZip(csrf, { mission_id: mid });
          } catch (e) {
            if (statusEl) {
              statusEl.innerHTML = (e as Error).message;
              statusEl.className = "gallery-status error";
            }
          } finally {
            btn.disabled = !canControl;
          }
        });
      });

      syncSelectionUi();
    } catch (e) {
      if (statusEl) {
        statusEl.textContent = "Napaka pri nalaganju: " + (e as Error).message;
        statusEl.className = "gallery-status error";
      }
    }
  }

  btnRefresh?.addEventListener("click", () => { void load(); });

  otherAll?.addEventListener("change", () => {
    otherEl.querySelectorAll<HTMLInputElement>("input.gal-pick")
      .forEach((pick) => { pick.checked = !!otherAll.checked; });
    syncSelectionUi();
  });

  btnSel?.addEventListener("click", async () => {
    const keys = selectedKeys(document.body);
    if (!keys.length) return;
    btnSel.disabled = true;
    try {
      await downloadZip(csrf, { keys });
    } catch (e) {
      if (statusEl) {
        statusEl.innerHTML = (e as Error).message;
        statusEl.className = "gallery-status error";
      }
    } finally {
      syncSelectionUi();
    }
  });

  btnOther?.addEventListener("click", async () => {
    btnOther.disabled = true;
    try {
      await downloadZip(csrf, { scope: "other" });
    } catch (e) {
      if (statusEl) {
        statusEl.innerHTML = (e as Error).message;
        statusEl.className = "gallery-status error";
      }
    } finally {
      btnOther.disabled = !canControl;
    }
  });

  void load();
}
