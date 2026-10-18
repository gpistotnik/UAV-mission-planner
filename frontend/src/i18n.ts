/**
 * Dvojezični sloj za frontend (slovenščina primarno, angleščina opcijsko).
 *
 * Uvozi isti katalog kot strežnik (`locale/catalog.json`), tako da sta
 * predloge in TypeScript vedno usklajena. Jezik se bere iz piškotka
 * `uav_lang` (privzeto `sl`), istega, ki ga nastavi preklopnik v glavi.
 */
import catalog from "../../locale/catalog.json";

export const LANGS = ["sl", "en"] as const;
export type Lang = (typeof LANGS)[number];

type Entry = { sl: string; en?: string };
const CAT = catalog as Record<string, Entry>;

export function getLang(): Lang {
  const m = document.cookie.match(/(?:^|;\s*)uav_lang=([^;]+)/);
  const v = m ? decodeURIComponent(m[1]) : "sl";
  return (LANGS as readonly string[]).includes(v) ? (v as Lang) : "sl";
}

const current: Lang = getLang();

/**
 * Prevod ključa v trenutni jezik. Neznan ključ vrne kar ključ (viden v
 * razvoju). Parametri `{ime}` se nadomestijo: `t("x.y", { n: 3 })`.
 */
export function t(key: string, params?: Record<string, string | number>): string {
  const e = CAT[key];
  let s = e ? e[current] ?? e.sl ?? key : key;
  if (params) {
    for (const k of Object.keys(params)) {
      s = s.replace(new RegExp(`\\{${k}\\}`, "g"), String(params[k]));
    }
  }
  return s;
}

/** Nastavi jezik (piškotek) — klicatelj naj nato osveži stran. */
export function setLang(lang: Lang): void {
  document.cookie = `uav_lang=${lang}; path=/; max-age=31536000; samesite=lax`;
}
