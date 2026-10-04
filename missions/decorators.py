"""Zascita kontrolnih endpointov: branje je odprto, kontrola zahteva prijavo.
Vklop krmili ``UAV_REQUIRE_AUTH`` (privzeto vezan na ``DEBUG``).
"""
from __future__ import annotations

from functools import wraps
from typing import Any, Callable

from django.conf import settings
from django.http import HttpRequest, JsonResponse


def control_required(view: Callable[..., Any]) -> Callable[..., Any]:
    """Zahteva prijavljenega uporabnika, ce je ``UAV_REQUIRE_AUTH`` vklopljen.

    Namesto preusmeritve na prijavno stran vrne ``403`` z JSON telesom ---
    klici prihajajo iz ``fetch()``, ne iz brskalnikove navigacije.
    """
    @wraps(view)
    def _wrapped(request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
        if not getattr(settings, "UAV_REQUIRE_AUTH", True):
            return view(request, *args, **kwargs)
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            return view(request, *args, **kwargs)
        return JsonResponse(
            {
                "ok": False,
                "error": "Za upravljanje letalnika je potrebna prijava.",
                "login_url": f"{settings.LOGIN_URL}?next={request.path}",
                "auth_required": True,
            },
            status=403,
        )
    return _wrapped


def require_confirm(view: Callable[..., Any]) -> Callable[..., Any]:
    """Zahteva ``{"confirm": true}`` v telesu zahteve.

    Varovalka pred nesrecnim klikom in pred zahtevami, ki bi jih sprozila
    tuja stran: arm in start misije sta nepovratna dejanja na fizicni
    napravi.
    """
    @wraps(view)
    def _wrapped(request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
        import json
        try:
            data = json.loads((request.body or b"{}").decode("utf-8") or "{}")
        except json.JSONDecodeError:
            data = {}
        if not data.get("confirm"):
            return JsonResponse(
                {"ok": False, "error": "Manjka potrditev (confirm=true).",
                 "confirm_required": True},
                status=400,
            )
        request.confirmed_data = data  # type: ignore[attr-defined]
        return view(request, *args, **kwargs)
    return _wrapped
