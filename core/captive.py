"""Captive-portal preusmeritve znotraj Djanga (port 80).

Ker ``mission-planner`` posluša na :80, ločen ``drone-captive-portal``
servis ne more teči hkrati. DNS hijack (dnsmasq-shared) še vedno usmeri
probe domene na IP drona; ta middleware na tiste Host/path zahteve
odgovori s 302 na ``UAV_CAPTIVE_REDIRECT_URL`` (privzeto
``http://dron.local/nadzor/``).

Brez klica ``request.get_host()``, da Domains niso v ``ALLOWED_HOSTS``
ne sprožijo ``DisallowedHost`` pred preusmeritvijo.
"""
from __future__ import annotations

from django.conf import settings
from django.http import HttpResponseRedirect

# Domene iz scripts/networkmanager/captive-dns.conf (brez vodečega www. kjer gre).
PROBE_HOSTS = frozenset({
    "captive.apple.com",
    "apple.com",
    "icloud.com",
    "connectivitycheck.gstatic.com",
    "connectivitycheck.android.com",
    "clients3.google.com",
    "clients4.google.com",
    "play.googleapis.com",
    "www.msftconnecttest.com",
    "msftconnecttest.com",
    "www.msftncsi.com",
    "msftncsi.com",
    "dns.msftncsi.com",
    "ipv6.msftconnecttest.com",
    "nmcheck.gnome.org",
    "network-test.debian.org",
    "connectivity-check.ubuntu.com",
    "conncheck.opensuse.org",
    "detectportal.firefox.com",
})

PROBE_PATHS = frozenset({
    "/hotspot-detect.html",
    "/library/test/success.html",
    "/generate_204",
    "/gen_204",
    "/connecttest.txt",
    "/ncsi.txt",
    "/success.txt",
    "/canonical.html",
    "/redirect",
})


def _host_only(meta_host: str) -> str:
    return (meta_host or "").split(":")[0].strip().lower()


def is_captive_probe(*, host: str, path: str) -> bool:
    if host in PROBE_HOSTS:
        return True
    if path in PROBE_PATHS:
        return True
    # Android / iOS včasih dodajo query; primerjaj prefix znanih poti
    for probe in PROBE_PATHS:
        if path.startswith(probe):
            return True
    return False


class CaptivePortalMiddleware:
    """302 na nadzor za captive-portal probe zahteve."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        host = _host_only(request.META.get("HTTP_HOST", ""))
        path = request.path or "/"
        if is_captive_probe(host=host, path=path):
            target = getattr(
                settings, "UAV_CAPTIVE_REDIRECT_URL",
                "http://dron.local/nadzor/",
            )
            return HttpResponseRedirect(target)
        return self.get_response(request)
