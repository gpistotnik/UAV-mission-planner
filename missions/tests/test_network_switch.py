"""Testi preklopa Wi-Fi omrežja prek fizičnega stikala.

:class:`SwitchPlanner` je čista logika stanja --- vsi testi tu delajo brez
GPIO, brez ``nmcli`` in brez datotečnega sistema (razen tam, kjer preizkušamo
prav datotečno koordinacijo).
"""
from __future__ import annotations

import time

import pytest

from missions.services.network_switch import (
    AP, CLIENT, GUI_ACTIVE_WINDOW_S, SwitchPlanner, consume_force,
    decide_wifi_enforcement, heartbeat_age_s, is_gui_active, other_mode,
    read_state, request_force, touch_heartbeat, write_state,
)


def planner(mode: str = CLIENT) -> SwitchPlanner:
    return SwitchPlanner(current_mode=mode)


# ---------------------------------------------------------------------------
# Brez GUI: takojšen preklop
# ---------------------------------------------------------------------------
def test_switch_without_gui_is_immediate() -> None:
    p = planner(CLIENT)
    res = p.request(AP, gui_active=False, now=0)
    assert res == {"action": "switch", "target": AP}


def test_request_for_current_mode_is_noop() -> None:
    p = planner(CLIENT)
    assert p.request(CLIENT, gui_active=False, now=0) == {"action": "noop"}
    assert p.request(CLIENT, gui_active=True, now=0) == {"action": "noop"}


def test_mark_applied_updates_current_mode() -> None:
    p = planner(CLIENT)
    p.request(AP, gui_active=False, now=0)
    p.mark_applied(AP)
    assert p.current_mode == AP
    assert p.status(now=1)["pending"] is None


# ---------------------------------------------------------------------------
# Z GUI: odlog
# ---------------------------------------------------------------------------
def test_switch_with_gui_is_deferred() -> None:
    p = planner(CLIENT)
    res = p.request(AP, gui_active=True, now=100, grace_s=30)
    assert res == {"action": "pending", "target": AP, "deadline": 130}
    st = p.status(now=110)
    assert st["pending"]["target"] == AP
    assert st["pending"]["remaining_s"] == pytest.approx(20.0)


def test_tick_before_deadline_waits() -> None:
    p = planner(CLIENT)
    p.request(AP, gui_active=True, now=0, grace_s=30)
    assert p.tick(now=10) == {"action": "wait", "remaining_s": 20.0}


def test_tick_after_deadline_switches() -> None:
    p = planner(CLIENT)
    p.request(AP, gui_active=True, now=0, grace_s=30)
    assert p.tick(now=30) == {"action": "switch", "target": AP}
    assert p.tick(now=31) == {"action": "none"}, "po izvedbi ni vec pending"


def test_tick_without_pending_is_noop() -> None:
    assert planner(CLIENT).tick(now=5) == {"action": "none"}


# ---------------------------------------------------------------------------
# Preklic: stikalo se vrne med odlogom
# ---------------------------------------------------------------------------
def test_switch_reverted_during_grace_cancels() -> None:
    p = planner(CLIENT)
    p.request(AP, gui_active=True, now=0, grace_s=30)
    res = p.request(CLIENT, gui_active=True, now=5)
    assert res == {"action": "cancelled"}
    assert p.status(now=6)["pending"] is None
    # Ura naprej --- nic se ne sme zgoditi, preklopa vec ni.
    assert p.tick(now=100) == {"action": "none"}


def test_cancel_works_even_without_gui() -> None:
    """Ce se stikalo vrne, prekliceno --- ne glede na to, ali je nekdo gleda."""
    p = planner(CLIENT)
    p.request(AP, gui_active=True, now=0, grace_s=30)
    assert p.request(CLIENT, gui_active=False, now=5) == {"action": "cancelled"}


# ---------------------------------------------------------------------------
# Ponovna sprememba cilja med odlogom (vrtenje stikala): rok se ne podaljsa
# ---------------------------------------------------------------------------
def test_retarget_during_grace_keeps_original_deadline() -> None:
    p = planner(CLIENT)
    p.request(AP, gui_active=True, now=0, grace_s=30)
    # Ni se preklopilo nazaj v CLIENT --- ostane na AP, samo se enkrat zahtevan.
    res = p.request(AP, gui_active=True, now=10)
    assert res == {"action": "none"}
    assert p.status(now=11)["pending"]["deadline"] == 30


def test_retarget_to_third_state_would_not_extend_deadline() -> None:
    """Ce bi med odlogom v AP nekdo znova zahteval AP (drugo stikalo?),
    se rok kljub temu ne podaljsa nazaj na +30s od tega trenutka."""
    p = planner(CLIENT)
    p.request(AP, gui_active=True, now=0, grace_s=30)
    p.request(CLIENT, gui_active=True, now=5)      # to prekine (cancelled)
    # Zdaj ni vec pending; nova zahteva za AP ustvari NOV odlog od 5.
    res = p.request(AP, gui_active=True, now=5, grace_s=30)
    assert res == {"action": "pending", "target": AP, "deadline": 35}


def test_force_before_deadline_switches_immediately() -> None:
    p = planner(CLIENT)
    p.request(AP, gui_active=True, now=0, grace_s=30)
    assert p.force(now=5) == {"action": "switch", "target": AP}
    assert p.status(now=6)["pending"] is None


def test_force_without_pending_is_noop() -> None:
    assert planner(CLIENT).force(now=5) == {"action": "none"}


def test_other_mode_is_pure_toggle() -> None:
    assert other_mode(CLIENT) == AP
    assert other_mode(AP) == CLIENT


# ---------------------------------------------------------------------------
# Datotečna koordinacija med demonom in Django pogledi
# ---------------------------------------------------------------------------
def test_write_and_read_state_roundtrip(tmp_path) -> None:
    write_state(tmp_path, {"mode": AP, "pending": None}, reason="gpio19 high")
    data = read_state(tmp_path)
    assert data["mode"] == AP
    assert data["reason"] == "gpio19 high"
    assert "written_at" in data


def test_read_state_without_daemon_running_is_none(tmp_path) -> None:
    assert read_state(tmp_path) is None


def test_read_state_survives_corrupt_file(tmp_path) -> None:
    (tmp_path / "network_state.json").write_text("{ni json", encoding="utf-8")
    assert read_state(tmp_path) is None


def test_heartbeat_age_grows_over_time(tmp_path) -> None:
    touch_heartbeat(tmp_path)
    now = time.time()
    assert heartbeat_age_s(tmp_path, now) == pytest.approx(0.0, abs=0.5)
    assert heartbeat_age_s(tmp_path, now + 100) == pytest.approx(100.0, abs=0.5)


def test_heartbeat_age_none_when_never_touched(tmp_path) -> None:
    assert heartbeat_age_s(tmp_path, time.time()) is None


def test_gui_active_within_window(tmp_path) -> None:
    touch_heartbeat(tmp_path)
    now = time.time()
    assert is_gui_active(tmp_path, now) is True
    assert is_gui_active(tmp_path, now + GUI_ACTIVE_WINDOW_S + 1) is False


def test_gui_inactive_without_any_heartbeat(tmp_path) -> None:
    assert is_gui_active(tmp_path, time.time()) is False


def test_force_flag_roundtrip(tmp_path) -> None:
    assert consume_force(tmp_path) is False
    request_force(tmp_path)
    assert consume_force(tmp_path) is True
    assert consume_force(tmp_path) is False, "zahteva se pocisti po prvem branju"


def test_runtime_dir_is_created_on_first_write(tmp_path) -> None:
    d = tmp_path / "does" / "not" / "exist"
    write_state(d, {"mode": CLIENT, "pending": None})
    assert (d / "network_state.json").is_file()


# ---------------------------------------------------------------------------
# Scenarij od konca do konca (brez GPIO/nmcli, samo planer + datoteke)
# ---------------------------------------------------------------------------
def test_end_to_end_scenario_with_active_gui(tmp_path) -> None:
    """Pilot gleda nadzorno plosco; stikalo preklopi iz AP v klient;
    odlog tece; pilot klikne 'Preklopi zdaj'."""
    p = planner(AP)
    touch_heartbeat(tmp_path)               # brskalnik je pravkar vprasal
    now = time.time()

    res = p.request(CLIENT, gui_active=is_gui_active(tmp_path, now), now=now)
    assert res["action"] == "pending"
    write_state(tmp_path, p.status(now), reason="gpio19 low")

    # Uporabnik klikne "Preklopi zdaj" (Django zapise force zastavico).
    request_force(tmp_path)

    # Demon v svoji zanki: zazna force, izvede preklop.
    assert consume_force(tmp_path) is True
    res2 = p.force(now=now + 2)
    assert res2 == {"action": "switch", "target": CLIENT}
    p.mark_applied(CLIENT)
    write_state(tmp_path, p.status(now + 2), reason="force")

    assert read_state(tmp_path)["mode"] == CLIENT
    assert read_state(tmp_path)["pending"] is None


# ---------------------------------------------------------------------------
# Varnost: nikoli temen radio (brez AP in brez klienta)
# ---------------------------------------------------------------------------
def test_enforce_ap_when_pin_wants_ap() -> None:
    assert decide_wifi_enforcement(
        AP, ap_active=False, client_active=False,
    ) == {"action": "ensure_ap", "reason": "pin_wants_ap"}
    assert decide_wifi_enforcement(
        AP, ap_active=True, client_active=False,
    )["action"] == "noop"


def test_enforce_client_ok_drops_stray_ap() -> None:
    assert decide_wifi_enforcement(
        CLIENT, ap_active=True, client_active=True,
    ) == {"action": "drop_ap", "reason": "client_ok"}


def test_enforce_client_missing_tries_then_keeps_ap() -> None:
    # Prvi poskus (se ni bilo attempta)
    assert decide_wifi_enforcement(
        CLIENT, ap_active=False, client_active=False, now=100.0,
        last_client_attempt_at=None,
    ) == {"action": "try_client_then_ap", "reason": "client_missing"}

    # Kmalu po poskusu, AP ze gor — ne flappaj
    assert decide_wifi_enforcement(
        CLIENT, ap_active=True, client_active=False, now=130.0,
        last_client_attempt_at=100.0, client_retry_s=60.0,
    )["action"] == "noop"

    # AP padel med cakanjem — takoj nazaj
    assert decide_wifi_enforcement(
        CLIENT, ap_active=False, client_active=False, now=130.0,
        last_client_attempt_at=100.0, client_retry_s=60.0,
    ) == {
        "action": "ensure_ap",
        "reason": "client_missing_keep_reachable",
    }

    # Po retry intervalu — poskusi klienta znova
    assert decide_wifi_enforcement(
        CLIENT, ap_active=True, client_active=False, now=170.0,
        last_client_attempt_at=100.0, client_retry_s=60.0,
    ) == {"action": "try_client_then_ap", "reason": "client_missing"}
