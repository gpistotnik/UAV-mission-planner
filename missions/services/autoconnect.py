"""Samodejna povezava s Pixhawkom ob zagonu streznika.

Na dronu nihce ne odpre brskalnika, da bi kliknil "Povezi". Django tece kot
systemd servis in se mora s krmilnikom povezati sam --- sicer telemetrija ni
zapisana, dokler se kdo ne prijavi v vmesnik, kar je ravno narobe: najbolj
zanimive so prve sekunde po vklopu.

Nit poskusa povezavo z eksponentnim zamikom in ne odneha: dron je lahko
priklopljen sele po zagonu Pi-ja, ali pa se Pixhawk zaganja pocasneje.

Izbira naprave (``UAV_SERIAL_DEVICE``):

``auto``
    Poskusi kandidate po vrsti, dokler heartbeat ne uspe. Trenutna faza
    (USB CDC): najprej ``ttyACM*`` / ``ttyUSB*`` / ``COM*``, nato UART
    (``/dev/serial0``, ``/dev/ttyAMA0``) za naslednjo TELEM2 fazo.
    Ce ena naprava obstaja, a ne odgovarja, gre na naslednjo.
karkoli drugega
    Uporabi natanko to (npr. ``/dev/ttyACM0``, ``COM3``,
    ``udp:127.0.0.1:14551`` za SITL ali mavlink-router).
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import Any, Optional

log = logging.getLogger(__name__)

#: UART poti (naslednja faza: GPIO ↔ TELEM2). Pri ``auto`` gredo *za* USB.
_UART_PATHS = ("/dev/serial0", "/dev/ttyAMA0")

#: Ukazi, pri katerih se povezovanje **ne** sme zgoditi.
_SKIP_COMMANDS = frozenset({
    "migrate", "makemigrations", "collectstatic", "test", "shell", "dbshell",
    "createsuperuser", "loaddata", "dumpdata", "check", "showmigrations",
    "sqlmigrate", "flush",
})

#: Zakaj start_autoconnect ni zagnal niti (za /api/telemetry/).
_skip_reason: Optional[str] = None


def list_candidate_devices(preferred: str = "auto") -> list[str]:
    """Vrne urejen seznam kandidatov za povezavo (brez podvajanja)."""
    preferred = (preferred or "auto").strip()
    if preferred and preferred.lower() != "auto":
        return [preferred]

    seen: set[str] = set()
    seen_real: set[str] = set()
    out: list[str] = []

    def _add(dev: Optional[str]) -> None:
        if not dev or dev in seen:
            return
        # Na Pi je /dev/serial0 pogosto symlink na ttyAMA0 --- ne poskusi dvakrat.
        try:
            real = os.path.realpath(dev) if not (
                ":" in dev and not dev.startswith("/")
            ) else dev
        except OSError:
            real = dev
        if real in seen_real:
            return
        seen.add(dev)
        seen_real.add(real)
        out.append(dev)

    # 1) USB / COM najprej (trenutna bench faza iz docs/09).
    try:
        from .mavlink_bridge import list_serial_ports
        ports = list_serial_ports()
    except Exception:
        ports = []

    for p in ports:
        dev = p.get("device") or ""
        low = dev.lower()
        if "acm" in low or "usb" in low or low.startswith("com"):
            _add(dev)

    # 2) UART za TELEM2 fazo.
    for path in _UART_PATHS:
        if os.path.exists(path):
            _add(path)

    return out


def pick_device(preferred: str = "auto") -> Optional[str]:
    """Vrne prvo smiselno napravo ali ``None``, ce ni nobene."""
    cands = list_candidate_devices(preferred)
    return cands[0] if cands else None


def should_autoconnect(argv: Optional[list[str]] = None) -> bool:
    """Ali je trenutni proces tak, da se sme povezati na krmilnik."""
    argv = sys.argv if argv is None else argv
    if "pytest" in sys.modules or any("pytest" in a for a in argv):
        return False
    if len(argv) > 1 and argv[1] in _SKIP_COMMANDS:
        return False
    # runserver brez --noreload: starš + otrok; poveži samo delovni proces
    # (RUN_MAIN=true). Z --noreload je en sam proces in RUN_MAIN ni nastavljen.
    if len(argv) > 1 and argv[1] == "runserver":
        if "--noreload" in argv:
            return True
        if os.environ.get("RUN_MAIN") != "true":
            return False
    return True


def should_autoconnect_reason(argv: Optional[list[str]] = None) -> Optional[str]:
    """None = OK; sicer kratka razlaga, zakaj ne."""
    argv = sys.argv if argv is None else argv
    if "pytest" in sys.modules or any("pytest" in a for a in argv):
        return "pytest"
    if len(argv) > 1 and argv[1] in _SKIP_COMMANDS:
        return f"management command: {argv[1]}"
    if len(argv) > 1 and argv[1] == "runserver":
        if "--noreload" in argv:
            return None
        if os.environ.get("RUN_MAIN") != "true":
            return "runserver parent (RUN_MAIN!=true) — cakam na reloader child"
    return None


class AutoConnector:
    """Nit, ki vzdrzuje povezavo s krmilnikom."""

    def __init__(
        self,
        bridge: Any,
        device: str = "auto",
        baud: int = 115200,
        retry_s: float = 5.0,
        max_retry_s: float = 60.0,
    ) -> None:
        self.bridge = bridge
        self.device = device
        self.baud = baud
        self.retry_s = retry_s
        self.max_retry_s = max_retry_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.attempts = 0
        self.last_error: Optional[str] = None
        self.last_device: Optional[str] = None
        self._cand_index = 0

    def status(self) -> dict[str, Any]:
        """Diagnostika za /api/telemetry/ in nastavitve."""
        return {
            "enabled": True,
            "running": self._thread is not None and self._thread.is_alive(),
            "configured_device": self.device,
            "baud": self.baud,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "last_device": self.last_device,
            "candidates": list_candidate_devices(self.device),
            "skip_reason": None,
        }

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="pixhawk-autoconnect")
        self._thread.start()
        log.info(
            "Pixhawk autoconnect zagnan (device=%s baud=%s candidates=%s)",
            self.device, self.baud, list_candidate_devices(self.device),
        )

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        backoff = self.retry_s
        last_sig: Optional[tuple[str, ...]] = None
        # Kratek premor ob zagonu: USB ACM se na Pi pojavi sele po enumeraciji.
        if self._stop.wait(2.0):
            return

        while not self._stop.is_set():
            if self.bridge.is_connected():
                backoff = self.retry_s
                self._cand_index = 0
                if self._stop.wait(self.retry_s):
                    return
                continue

            if getattr(self.bridge, "is_connecting", lambda: False)():
                if self._stop.wait(1.0):
                    return
                continue

            candidates = list_candidate_devices(self.device)
            sig = tuple(candidates)
            # Nova naprava (npr. ttyACM0 po bootu Pixhawka) → takoj znova, brez
            # dolgega backoffa od zgodnjega missa.
            if sig and sig != last_sig:
                log.info("Autoconnect: novi kandidati %s", list(sig))
                last_sig = sig
                self._cand_index = 0
                backoff = self.retry_s

            if not candidates:
                self.attempts += 1
                self.last_device = None
                self.last_error = "ni vidne serijske naprave"
                log.warning("Autoconnect: ni kandidatov (poskus %s)", self.attempts)
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2, self.max_retry_s)
                continue

            if self._cand_index >= len(candidates):
                self._cand_index = 0

            device = candidates[self._cand_index]
            self.attempts += 1
            self.last_device = device
            log.info(
                "Autoconnect poskus %s: %s @ %s",
                self.attempts, device, self.baud,
            )
            snap = self.bridge.connect(device, self.baud)
            if snap.get("connected"):
                self.last_error = None
                backoff = self.retry_s
                self._cand_index = 0
                log.info("Autoconnect uspel: %s @ %s", device, self.baud)
                continue

            self.last_error = snap.get("error") or "povezava ni uspela"
            log.warning(
                "Autoconnect neuspeh na %s: %s", device, self.last_error,
            )
            # Ustavi reconnect na mrtvem kandidatu, sicer bridge zavzame
            # vrata za vedno in nikoli ne pridemo do naslednjega.
            try:
                self.bridge.disconnect()
            except Exception:
                pass
            self._cand_index = (self._cand_index + 1) % len(candidates)
            if self._cand_index == 0:
                if self._stop.wait(backoff):
                    return
                # Z USB napravo ne cakaj minuto — Pixhawk se lahko se zaganja.
                cap = 15.0 if any(
                    "acm" in d.lower() or "usb" in d.lower() or d.lower().startswith("com")
                    for d in candidates
                ) else self.max_retry_s
                backoff = min(backoff * 2, cap)
            else:
                if self._stop.wait(1.0):
                    return


_instance: Optional[AutoConnector] = None
_lock = threading.Lock()


def start_autoconnect() -> Optional[AutoConnector]:
    """Zazene samodejno povezovanje glede na Django nastavitve."""
    global _instance, _skip_reason
    from django.conf import settings

    if not getattr(settings, "UAV_AUTOCONNECT", False):
        _skip_reason = "UAV_AUTOCONNECT=False"
        log.info("Pixhawk autoconnect izklopljen (%s)", _skip_reason)
        return None

    reason = should_autoconnect_reason()
    if reason is not None:
        _skip_reason = reason
        log.info("Pixhawk autoconnect preskocen: %s", reason)
        return None

    _skip_reason = None
    with _lock:
        if _instance is not None:
            return _instance
        from .mavlink_bridge import get_bridge
        _instance = AutoConnector(
            get_bridge(),
            device=getattr(settings, "UAV_SERIAL_DEVICE", "auto"),
            baud=int(getattr(settings, "UAV_SERIAL_BAUD", 115200)),
        )
        _instance.start()
        return _instance


def get_autoconnector() -> Optional[AutoConnector]:
    return _instance


def autoconnect_status() -> dict[str, Any]:
    """Stanje samodejne povezave (tudi ce nit ni zagnana)."""
    from django.conf import settings

    inst = _instance
    if inst is not None:
        return inst.status()

    enabled = bool(getattr(settings, "UAV_AUTOCONNECT", False))
    reason = _skip_reason
    if not enabled:
        reason = "UAV_AUTOCONNECT=False"
    elif reason is None:
        # ready() se se ni poklical ali pa je bil pred nastavitvijo.
        reason = should_autoconnect_reason() or "nit ni zagnana"

    return {
        "enabled": enabled,
        "running": False,
        "configured_device": getattr(settings, "UAV_SERIAL_DEVICE", "auto"),
        "baud": int(getattr(settings, "UAV_SERIAL_BAUD", 115200)),
        "attempts": 0,
        "last_error": reason,
        "last_device": None,
        "candidates": list_candidate_devices(
            getattr(settings, "UAV_SERIAL_DEVICE", "auto")),
        "skip_reason": reason,
    }
