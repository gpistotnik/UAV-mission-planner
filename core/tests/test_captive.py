"""Enotski testi za captive-portal middleware."""
from django.http import HttpResponse
from django.test import RequestFactory, override_settings

from core.captive import CaptivePortalMiddleware, is_captive_probe


def test_probe_host_detected() -> None:
    assert is_captive_probe(host="captive.apple.com", path="/")
    assert is_captive_probe(host="connectivitycheck.gstatic.com", path="/generate_204")


def test_normal_host_not_probe() -> None:
    assert not is_captive_probe(host="dron.local", path="/nadzor/")
    assert not is_captive_probe(host="10.0.0.1", path="/nadzor/")


def test_probe_path_detected() -> None:
    assert is_captive_probe(host="10.0.0.1", path="/hotspot-detect.html")
    assert is_captive_probe(host="dron.local", path="/generate_204")


@override_settings(UAV_CAPTIVE_REDIRECT_URL="http://dron.local/nadzor/")
def test_middleware_redirects_probe() -> None:
    def ok(_request):
        return HttpResponse("ok")

    mw = CaptivePortalMiddleware(ok)
    req = RequestFactory().get("/hotspot-detect.html", HTTP_HOST="captive.apple.com")
    resp = mw(req)
    assert resp.status_code == 302
    assert resp["Location"] == "http://dron.local/nadzor/"


@override_settings(UAV_CAPTIVE_REDIRECT_URL="http://dron.local/nadzor/")
def test_middleware_passes_normal() -> None:
    def ok(_request):
        return HttpResponse("ok")

    mw = CaptivePortalMiddleware(ok)
    req = RequestFactory().get("/nadzor/", HTTP_HOST="dron.local")
    resp = mw(req)
    assert resp.status_code == 200
    assert resp.content == b"ok"
