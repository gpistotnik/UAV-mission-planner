"""Skupni vmesnik za hover- in hop-test (en aktiven naravnost)."""
from __future__ import annotations

import threading
from typing import Any

from .hop_test import HopTestParams, get_hop_runner
from .test_flight import TestFlightParams, get_runner

_dispatch_lock = threading.Lock()


def start_test(profile: str, params: Any) -> dict[str, Any]:
    """Zazene hover ali hop. Če že teče katerikoli test, zavrne."""
    profile = (profile or "hover").strip().lower()
    with _dispatch_lock:
        hover = get_runner()
        hop = get_hop_runner()
        if hover.active or hop.active:
            active = "hop" if hop.active else "hover"
            return {
                "ok": False,
                "error": f"Test ({active}) že teče.",
                "status": status_test(),
            }
        if profile == "hop":
            if not isinstance(params, HopTestParams):
                params = HopTestParams()
            return hop.start(params)
        if not isinstance(params, TestFlightParams):
            params = TestFlightParams()
        result = hover.start(params)
        if result.get("ok") and isinstance(result.get("status"), dict):
            result["status"]["profile"] = "hover"
        return result


def abort_test(reason: str = "prekinil uporabnik",
               *, force: bool = False) -> dict[str, Any]:
    with _dispatch_lock:
        hop = get_hop_runner()
        hover = get_runner()
        if force:
            # Oba runnerja: katerikoli je "stuck" naj se odklene.
            hop_st = hop.status()
            hover_st = hover.status()
            hop_busy = hop_st.get("force_cancellable")
            hover_busy = hover_st.get("force_cancellable")
            if hop_busy:
                return hop.force_abort(reason)
            if hover_busy:
                return hover.force_abort(reason)
            # Noben ni aktiven — še vedno poskusi hover (LAND če armed).
            return hover.force_abort(reason)

        hop_phase = hop.status().get("phase")
        # Tudi če nit ni več živa, a UI še kaže PREFLIGHT/COUNTDOWN/...
        if hop.active or hop_phase not in ("IDLE", "DONE", "ABORTED", "FAILED"):
            return hop.abort(reason)
        if hover.active:
            return hover.abort(reason)
        return hover.abort(reason)


def status_test() -> dict[str, Any]:
    """Vrni stanje aktivnega testa; sicer zadnje neterminalno / hover IDLE."""
    hop = get_hop_runner()
    hover = get_runner()
    if hop.active:
        return hop.status()
    if hover.active:
        st = hover.status()
        st["profile"] = "hover"
        return st
    hop_st = hop.status()
    hover_st = hover.status()
    hover_st["profile"] = "hover"
    if hop_st.get("phase") != "IDLE":
        return hop_st
    return hover_st
