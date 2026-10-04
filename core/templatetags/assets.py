"""Predpomnilnik-varni URL-ji za static assete (?v=mtime)."""
from __future__ import annotations

from pathlib import Path

from django import template
from django.conf import settings
from django.templatetags.static import static

register = template.Library()


@register.simple_tag
def asset(path: str) -> str:
    """Kot ``{% static %}``, z ``?v=<mtime>`` da brskalnik ne drži starega JS/CSS."""
    url = static(path)
    candidates = [Path(settings.BASE_DIR) / "static" / path]
    root = getattr(settings, "STATIC_ROOT", None)
    if root:
        candidates.append(Path(root) / path)
    for fp in candidates:
        try:
            if fp.is_file():
                return f"{url}?v={int(fp.stat().st_mtime)}"
        except OSError:
            continue
    return url
