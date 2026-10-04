#!/usr/bin/env python3
"""Fizicno stikalo za preklop Wi-Fi med klient in AP nacinom (GPIO19).

Ideja: na terenu ni brskalnika, ki bi klikal "preklopi na AP". Namesto tega
je na ohisje speljano stikalo med GPIO19 in GND:

* GPIO19 povezan na GND (nizko)   --- dron naj se poveze na ZNANO Wi-Fi
  omrezje (klient). Ce povezava v ~45 s ne uspe, ostane/se vrne AP
  (fail-open), da Pi ne ostane nedosegljiv; klient se nato ponovno
  poskusa vsakih ~60 s.
* GPIO19 odklopljen (visoko, notranji pull-up) --- dron naj sam oddaja svoj
  SSID (AP nacin, profil ``dron-ap`` prek ``nmcli con up``).

Ta skript je *edini* del sistema, ki dejansko klice ``nmcli``. Odlocitev,
KDAJ preklopiti, je prepuscena :mod:`missions.services.network_switch`
(cista logika, brez GPIO/Django/nmcli odvisnosti, pokrita s 24 testi) --- ta
skript jo samo poklice in izvede njen ukaz. Enak vzorec kot pri
``camera_trigger.py``: cista logika + tanek izvedbeni sloj.

Zakaj ne preklopi takoj
------------------------
Ce nekdo ravno gleda spletni vmesnik (nadzorna plosca, planer, testni polet)
prek trenutnega omrezja, bi takojsen preklop wlan0 prekinil njegovo sejo
sredi klika. Zato ``SwitchPlanner`` (deljen z Django streznikom prek
``missions/views.py`` --- ``/api/network/status/`` in ``/api/network/force/``)
odlozi preklop za ``UAV_NETWORK_GRACE_S`` sekund, ce je GUI aktiven (utrip v
``gui_heartbeat`` mlajsi od nekaj sekund), uporabnik v vmesniku pa lahko med
tem klikne "Preklopi zdaj" (zapise se v datoteko ``network_force``, ki jo ta
demon prebere v naslednjem obhodu).

Usklajevanje med tem demonom in Django streznikom gre izkljucno prek
datotek v ``UAV_RUNTIME_DIR`` (privzeto ``<repo>/run``) --- ni zivega RPC,
ker demon mora delovati pravilno tudi, ce je Django streznik ravno v
ponovnem zagonu.

Brez gpiozero
-------------
Ce ``gpiozero`` ni na voljo (razvojni racunalnik, ne RPi), se skript ne
zazene --- v nasprotju s kamero preklop omrezja ni nekaj, kar ima smiselen
"dry-run" brez GPIO, saj je edini vhodni signal prav stanje pina. Za razvoj
in teste logike glej ``missions/tests/test_network_switch.py``, ki testira
``SwitchPlanner`` neposredno, brez GPIO.

Uporaba
-------
::

    # na dronu, kot root (nmcli in GPIO)
    sudo python3 scripts/wifi_gpio_switch.py

    # z drugimi privzetimi vrednostmi
    python3 scripts/wifi_gpio_switch.py --pin 19 --ap-con-name dron-ap \\
        --iface wlan0 --grace 30
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

# Omogoci uvoz "missions.services.network_switch" brez django.setup() ---
# modul je namenoma brez Django/pymavlink odvisnosti (enak vzorec kot
# analysis/analyze_flight.py, ki na enak nacin uvozi "analysis.metrics").
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from missions.services.network_switch import (  # noqa: E402
    AP,
    CLIENT,
    DEFAULT_CLIENT_RETRY_S,
    SwitchPlanner,
    consume_force,
    decide_wifi_enforcement,
    is_gui_active,
    other_mode,
    write_state,
)

POLL_INTERVAL_S = 1.0
#: Koliko sekund cakamo na klient povezavo, preden gre varnostni AP.
CLIENT_CONNECT_WAIT_S = 45.0


def log(msg: str) -> None:
    print(f"[wifi-gpio-switch] {msg}", flush=True)


def read_pin_mode(pin: Any) -> str:
    """GPIO19 na GND (is_pressed=True pri pull_up) => CLIENT, sicer AP."""
    return CLIENT if pin.is_pressed else AP


def _nmcli(*args: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["nmcli", *args],
        capture_output=True, text=True, timeout=timeout, check=False,
    )


def is_ap_active(ap_con_name: str) -> bool:
    """Ali je nmcli profil AP trenutno aktiviran?"""
    try:
        result = _nmcli("-t", "-f", "NAME", "con", "show", "--active", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    return any(line.strip() == ap_con_name for line in result.stdout.splitlines())


def active_connections_on_iface(iface: str) -> list[str]:
    """Imena aktivnih povezav na danem vmesniku (klient ali AP)."""
    try:
        result = _nmcli("-t", "-f", "NAME,DEVICE", "con", "show", "--active",
                        timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    names: list[str] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        # NAME:DEVICE --- ime lahko vsebuje dvopicje, device je zadnje polje
        name, _, device = line.rpartition(":")
        if device == iface and name:
            names.append(name)
    return names


def is_client_active(iface: str, ap_con_name: str) -> bool:
    """Ali je na vmesniku aktivna klient Wi-Fi povezava (ne AP)?"""
    return any(
        name != ap_con_name
        for name in active_connections_on_iface(iface)
    )


def _nmcli_ok(result: subprocess.CompletedProcess[str], *, mode: str,
              ap_con_name: str) -> bool:
    if result.returncode == 0:
        return True
    stderr = (result.stderr or "").strip()
    # "con down" na ze neaktivnem profilu in "con up" na ze aktivnem
    # nista napaka --- nmcli samo pove, da ni bilo kaj (s)preklopiti.
    low = stderr.lower()
    if "not an active connection" in low or "already active" in low:
        log(f"'{ap_con_name}' ze v zeljenem stanju ({mode}).")
        return True
    log(f"NAPAKA: nmcli vrnil kodo {result.returncode}: {stderr}")
    return False


def ensure_ap(*, ap_con_name: str, iface: str) -> bool:
    """Aktivira hotspot. Vrne True ob uspehu."""
    try:
        for name in active_connections_on_iface(iface):
            if name == ap_con_name:
                continue
            log(f"Ugasam klient povezavo '{name}' na {iface} ...")
            down = _nmcli("con", "down", name)
            if down.returncode != 0:
                log(f"Opozorilo: con down '{name}': "
                    f"{(down.stderr or '').strip()}")
        if is_ap_active(ap_con_name):
            log(f"AP '{ap_con_name}' ze aktiven.")
            return True
        log(f"Aktiviram AP '{ap_con_name}' na {iface} ...")
        result = _nmcli("con", "up", ap_con_name)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"NAPAKA pri klicu nmcli: {exc}")
        return False
    if not _nmcli_ok(result, mode=AP, ap_con_name=ap_con_name):
        return False
    log("OK: AP aktiven.")
    return True


def drop_ap(*, ap_con_name: str) -> bool:
    """Ugasne samo AP profil (klient ostane)."""
    try:
        log(f"Ugasam AP '{ap_con_name}' ...")
        result = _nmcli("con", "down", ap_con_name)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"NAPAKA pri klicu nmcli: {exc}")
        return False
    return _nmcli_ok(result, mode=CLIENT, ap_con_name=ap_con_name)


def try_client_then_ap(
    *, ap_con_name: str, iface: str, wait_s: float = CLIENT_CONNECT_WAIT_S,
) -> str:
    """Poskusi klient Wi-Fi; ob neuspehu vrne AP (fail-open).

    Vrne dejanski nacin: ``CLIENT`` ali ``AP``.
    """
    log(f"Ugasam AP '{ap_con_name}', poskusam klient na {iface} ...")
    try:
        _nmcli("con", "down", ap_con_name)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"Opozorilo: con down AP: {exc}")

    try:
        _nmcli("device", "wifi", "rescan", "ifname", iface, timeout=20)
    except (OSError, subprocess.SubprocessError):
        pass

    # NM naj poveze najboljsi znan profil na tem vmesniku.
    try:
        up = _nmcli("device", "connect", iface, timeout=max(30.0, wait_s))
        if up.returncode != 0:
            log(f"device connect: {(up.stderr or '').strip() or up.stdout}")
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"NAPAKA device connect: {exc}")

    deadline = time.time() + wait_s
    while time.time() < deadline:
        if is_client_active(iface, ap_con_name):
            names = [n for n in active_connections_on_iface(iface)
                     if n != ap_con_name]
            log(f"OK: CLIENT aktiven ({', '.join(names) or iface}).")
            return CLIENT
        time.sleep(1.0)

    log("Klient ni povezan v casu — varnostni AP (Pi mora ostati dosegljiv).")
    if ensure_ap(ap_con_name=ap_con_name, iface=iface):
        return AP
    log("NAPAKA: tudi varnostni AP ni uspel.")
    return AP


def apply_mode(mode: str, *, ap_con_name: str, iface: str) -> str:
    """Preklopi omrezje. Vrne dejanski nacin (``AP`` / ``CLIENT``).

    CLIENT nikoli ne pusti radia praznega: ce domace omrezje odpove,
    ostane (ali se vrne) AP.
    """
    if mode == AP:
        return AP if ensure_ap(ap_con_name=ap_con_name, iface=iface) else AP
    return try_client_then_ap(ap_con_name=ap_con_name, iface=iface)


class GpioSwitchDaemon:
    """Poveze GPIO stikalo, SwitchPlanner in nmcli v en demon proces."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.runtime_dir = Path(args.runtime_dir)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.last_client_attempt_at: Optional[float] = None
        self.client_retry_s = float(
            getattr(args, "client_retry", DEFAULT_CLIENT_RETRY_S))

        # gpiozero uvozimo sele tu, da napaka o manjkajoci knjiznici pride
        # z jasnim navodilom, ne kot golo ImportError na vrhu datoteke.
        try:
            from gpiozero import Button
        except ImportError:
            log("NAPAKA: 'gpiozero' ni namescen. Namesti z:")
            log("    pip install gpiozero  (na RPi je potreben tudi lgpio: "
                "sudo apt install -y python3-lgpio)")
            raise

        self.pin = Button(
            args.pin, pull_up=True, bounce_time=args.bounce_time,
        )
        initial_mode = read_pin_mode(self.pin)
        # Namerno zacetno "nasprotno" stanje: sicer bi ob bootu planner
        # ze mislil, da smo v AP, ceprav NM se drzi klienta, in ne bi
        # ponovil preklopa. Prvi _act() mora vedno zares zagnati nmcli.
        self.planner = SwitchPlanner(current_mode=other_mode(initial_mode))
        log(f"Zagon. GPIO{args.pin} pravi zacetni nacin: {initial_mode}.")

        # Ob zagonu poskrbimo, da dejansko stanje wlan0 ustreza pinu (npr.
        # po ponovnem zagonu RPi, ko je AP profil se v prejsnjem stanju).
        self._act(initial_mode)
        self._write_status()

        self.pin.when_pressed = self._on_pin_change
        self.pin.when_released = self._on_pin_change

    # --- GPIO callback (klice gpiozero v svoji niti) ---
    def _on_pin_change(self) -> None:
        target = read_pin_mode(self.pin)
        with self.lock:
            # time.time() in ne monotonic(): is_gui_active() primerja z
            # mtime datoteke gui_heartbeat (stenska ura), oba morata biti
            # ista casovna osnova.
            now = time.time()
            gui_active = is_gui_active(self.runtime_dir, now)
            result = self.planner.request(
                target, gui_active=gui_active, now=now,
                grace_s=self.args.grace,
            )
            log(f"Stikalo -> {target}: {result}")
            if result["action"] == "switch":
                self._act(target)
            self._write_status()

    def _act(self, mode: str) -> None:
        if mode == CLIENT:
            self.last_client_attempt_at = time.time()
        actual = apply_mode(
            mode, ap_con_name=self.args.ap_con_name, iface=self.args.iface,
        )
        # mark_applied z DEJANSKIM nacinom (CLIENT zahteva lahko pade na AP).
        self.planner.mark_applied(actual)
        if mode == CLIENT and actual == AP:
            log("Stikalo zeli CLIENT, dejansko je AP (fallback).")

    def _write_status(self) -> None:
        # Ista ura kot v _on_pin_change / is_gui_active (stenska ura, ne
        # monotonic): deadline in remaining_s morata biti na isti osi, sicer
        # UI pokaze milijarde sekund in tick nikoli ne izvede preklopa.
        st = self.planner.status(time.time())
        st["desired"] = read_pin_mode(self.pin)
        st["ap_active"] = is_ap_active(self.args.ap_con_name)
        st["client_active"] = is_client_active(
            self.args.iface, self.args.ap_con_name)
        write_state(self.runtime_dir, st)

    def _enforce_pin(self) -> None:
        """Uskladi nmcli z GPIO + varnostno pravilo (nikoli temen radio)."""
        if self.planner.pending is not None:
            return
        desired = read_pin_mode(self.pin)
        ap_up = is_ap_active(self.args.ap_con_name)
        client_up = is_client_active(self.args.iface, self.args.ap_con_name)
        now = time.time()
        decision = decide_wifi_enforcement(
            desired,
            ap_active=ap_up,
            client_active=client_up,
            now=now,
            last_client_attempt_at=self.last_client_attempt_at,
            client_retry_s=self.client_retry_s,
        )
        action = decision.get("action")
        if action == "noop":
            return
        reason = decision.get("reason", "")
        log(f"Enforce ({desired}): {action} ({reason})")
        if action == "ensure_ap":
            if ensure_ap(ap_con_name=self.args.ap_con_name,
                         iface=self.args.iface):
                self.planner.mark_applied(AP)
            self._write_status()
        elif action == "drop_ap":
            if drop_ap(ap_con_name=self.args.ap_con_name):
                self.planner.mark_applied(CLIENT)
            self._write_status()
        elif action == "try_client_then_ap":
            self._act(CLIENT)
            self._write_status()

    def tick(self) -> None:
        """Periodicno: izvedi zapadle odlozene preklope in 'Preklopi zdaj'."""
        with self.lock:
            now = time.time()
            if consume_force(self.runtime_dir):
                result = self.planner.force(now)
                log(f"'Preklopi zdaj' iz vmesnika: {result}")
                if result["action"] == "switch":
                    self._act(result["target"])
                    self._write_status()
                    return

            result = self.planner.tick(now)
            if result["action"] == "switch":
                log(f"Potekel odlog, izvajam preklop: {result}")
                self._act(result["target"])
                self._write_status()
                return

            self._enforce_pin()

    def run(self) -> int:
        log(f"Tecem. Nadzor pina GPIO{self.args.pin}, "
            f"stanje v {self.runtime_dir}.")
        while not self.stop.is_set():
            self.tick()
            self.stop.wait(POLL_INTERVAL_S)
        log("Ustavljeno.")
        return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Preklop Wi-Fi (klient/AP) prek fizicnega stikala na GPIO.")
    ap.add_argument("--pin", type=int, default=19,
                     help="GPIO pin (BCM stevilcenje), privzeto 19")
    ap.add_argument("--ap-con-name", default="dron-ap",
                     help="ime nmcli profila za AP (privzeto dron-ap)")
    ap.add_argument("--iface", default="wlan0",
                     help="brezzicni vmesnik (privzeto wlan0)")
    ap.add_argument("--runtime-dir",
                     default=str(Path(__file__).resolve().parent.parent / "run"),
                     help="mapa za deljeno stanje z Django streznikom "
                          "(mora ustrezati UAV_RUNTIME_DIR)")
    ap.add_argument("--grace", type=float, default=30.0,
                     help="sekund odloga preklopa, ce je GUI aktiven "
                          "(mora ustrezati UAV_NETWORK_GRACE_S)")
    ap.add_argument("--bounce-time", type=float, default=0.2,
                     help="debounce za mehansko stikalo v sekundah")
    ap.add_argument("--client-retry", type=float,
                     default=DEFAULT_CLIENT_RETRY_S,
                     help="sekund med ponovnimi poskusi klienta, dokler "
                          "drzimo varnostni AP (privzeto 60)")
    args = ap.parse_args(argv)

    try:
        daemon = GpioSwitchDaemon(args)
    except ImportError:
        return 1
    except Exception as exc:  # noqa: BLE001 - hocemo jasno sporocilo, ne traceback
        log(f"NAPAKA pri zagonu: {exc}")
        return 1

    import signal as signal_module

    def _sig(_signum: int, _frame: Any) -> None:
        print("\n[signal] zakljucujem ...", flush=True)
        daemon.stop.set()

    signal_module.signal(signal_module.SIGINT, _sig)
    signal_module.signal(signal_module.SIGTERM, _sig)
    return daemon.run()


if __name__ == "__main__":
    sys.exit(main())
