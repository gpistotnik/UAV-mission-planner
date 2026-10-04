"""Pogledi jedra: nadzorna plosca (tudi glavna stran)."""
from django.conf import settings
from django.middleware.csrf import get_token
from django.shortcuts import render


def dashboard(request):
    """Nadzorna plošča s HUD, karto, kontrolo letalnika in video streamom.

    To je tudi glavna stran (``/`` in ``/nadzor/``).
    """
    from missions.models import Mission

    camera_url = getattr(settings, "DRONE_CAMERA_URL", "auto")

    # Seznam misij za drop-down. Pošljemo samo (id, ime, št. gradnikov).
    missions = [
        {"id": m.pk, "name": m.name, "elements_count": m.elements.count()}
        for m in Mission.objects.all().order_by("-id")[:200]
    ]

    # Ali sme ta obiskovalec upravljati letalnik. Vmesnik gumbe vseeno
    # prikaze (da je jasno, kaj sistem zmore), a doda opozorilo s prijavo.
    require_auth = getattr(settings, "UAV_REQUIRE_AUTH", True)
    can_control = (not require_auth) or request.user.is_authenticated

    context = {
        "csrf_token": get_token(request),
        "camera_url": camera_url,
        "missions": missions,
        "can_control": can_control,
        "require_auth": require_auth,
        "serial_device": getattr(settings, "UAV_SERIAL_DEVICE", "auto"),
        "serial_baud": int(getattr(settings, "UAV_SERIAL_BAUD", 115200)),
    }
    return render(request, "core/dashboard.html", context)
