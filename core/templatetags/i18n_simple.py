"""Predloga tag ``{% t "ključ" %}`` za dvojezični vmesnik (glej ``core.i18n``).

Jezik se prebere iz konteksta (``LANG``, ki ga nastavi context processor
``core.i18n.language_context``). Izhod se v predlogi samodejno pobegne (HTML
varno); za nize z oznakami uporabi ``mark_safe`` na klicni strani.
"""
from __future__ import annotations

from django import template

from core.i18n import DEFAULT_LANG, tr

register = template.Library()


@register.simple_tag(takes_context=True)
def t(context, key: str) -> str:
    lang = context.get("LANG", DEFAULT_LANG)
    return tr(key, lang)
