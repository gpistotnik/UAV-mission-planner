"""Lahek dvojezični sloj (slovenščina primarno, angleščina opcijsko).

Vsi prevodi so v enem samem katalogu (``locale/catalog.json``), ki ga berejo
vsi trije sloji: Django predloge (prek ``{% t %}`` iz ``i18n_simple``),
pogledi v Pythonu (prek :func:`tr`) in TypeScript frontend (prek
``frontend/src/i18n.ts``, ki isti JSON uvozi z esbuild).

Izbrani jezik se hrani v piškotku ``uav_lang`` (privzeto ``sl``). Ker ga
berejo tako strežnik kot brskalnik, je prikaz po preklopu usklajen brez
ločenih URL-jev ali Djangovega prevajalskega stroja (``USE_I18N`` ostaja
izklopljen).
"""
from __future__ import annotations

import json
from pathlib import Path

from django.conf import settings

LANG_COOKIE = "uav_lang"
DEFAULT_LANG = "sl"
LANGUAGES = ("sl", "en")

_CATALOG_PATH = Path(settings.BASE_DIR) / "locale" / "catalog.json"
_cache: dict[str, dict[str, str]] | None = None


def _load() -> dict[str, dict[str, str]]:
    try:
        with _CATALOG_PATH.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def catalog() -> dict[str, dict[str, str]]:
    """Katalog prevodov. V razvoju se bere ob vsaki uporabi (urejanje brez
    ponovnega zagona), v produkciji pa enkrat in se predpomni."""
    global _cache
    if getattr(settings, "DEBUG", False):
        return _load()
    if _cache is None:
        _cache = _load()
    return _cache


def get_language(request) -> str:
    """Trenutni jezik iz piškotka; pade na ``sl`` ob neznani vrednosti."""
    lang = request.COOKIES.get(LANG_COOKIE, DEFAULT_LANG)
    return lang if lang in LANGUAGES else DEFAULT_LANG


def tr(key: str, lang: str = DEFAULT_LANG) -> str:
    """Prevod ``key`` v ``lang``; fallback na slovenščino, nato na sam ključ."""
    entry = catalog().get(key)
    if not entry:
        return key
    return entry.get(lang) or entry.get(DEFAULT_LANG) or key


def language_context(request) -> dict:
    """Context processor: ``LANG`` in ``LANGUAGES`` v vsaki predlogi."""
    return {"LANG": get_language(request), "LANGUAGES": LANGUAGES}
