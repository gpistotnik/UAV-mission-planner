#!/usr/bin/env python3
"""Samodejni preklop Wi-Fi med klient in AP nacinom glede na doseg SSID-ja.

Ideja: na terenu ni brskalnika, ki bi klikal "preklopi na AP". Namesto tega
dron **sam skenira** okolico:

* ciljni SSID (privzeto ``Area51``) je viden v skenu   --- dron se poveze
  nanj (klient). Ce povezava v ~45 s ne uspe, ostane/se vrne AP (fail-open),
  da Pi ne ostane nedosegljiv; klient se nato ponovno poskusa.
* ciljnega SSID-ja ni v dosegu                          --- dron sam oddaja
  svoj SSID (AP nacin, profil ``dron-ap`` prek ``nmcli con up``).

Da marginalen signal ne povzroca utripanja, je na prehodu "SSID je izginil"
histereza: AP se vklopi sele po :data:`MISS_LIMIT` zaporednih skenih brez
ciljnega SSID-ja. Prvo *pojavitev* SSID-ja pa upostevamo takoj.

Ta skript je *edini* del sistema, ki dejansko klice ``nmcli``. Varnostno
pravilo (da wlan0 nikoli ne ostane brez AP in brez klienta) je prepusceno
:func:`decide_wifi_enforcement` iz :mod:`missions.services.network_switch`
(cista logika, brez nmcli/Django odvisnosti, pokrita s testi) --- ta skript
izracuna le zeleni nacin iz skena in izvede ukaz, ki ga funkcija vrne.

Stanje za vmesnik demon zapise v ``UAV_RUNTIME_DIR`` (privzeto ``<repo>/run``)
prek :func:`write_state`; Django ga samo bere (``/api/network/status/``).

Brez GPIO
---------
Odlocitev ne potrebuje vec fizicnega stikala ali ``gpiozero`` --- edini vhod
je rezultat skeniranja Wi-Fi. Skript zato tece tudi brez posebne strojne
opreme (potrebuje le ``nmcli``).

Uporaba
-------
::

    # na dronu, kot root (nmcli)
    sudo python3 scripts/wifi_gpio_switch.py --ssid Area51

    # drug SSID / vmesnik
    python3 scripts/wifi_gpio_switch.py --ssid DomaciWiFi --iface wlan0
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
    decide_wifi_enforcement,
    write_state,
)

#: Kako pogosto skeniramo okolico in uskladimo stanje.
POLL_INTERVAL_S = 15.0
#: Koliko sekund cakamo na klient povezavo, preden gre varnostni AP.
CLIENT_CONNECT_WAIT_S = 45.0
#: Toliko zaporednih skenov brez ciljnega SSID-ja, preden preklopimo na AP
#: (histereza proti utripanju ob sibkem signalu).
MISS_LIMIT = 2


def log(msg: str) -> None:
    print(f"[wifi-switch] {msg}", flush=True)


def _nmcli(*args: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["nmcli", *args],
        capture_output=True, text=True, timeout=timeout, check=False,
    )


def scan_ssids(iface: str) -> Optional[set[str]]:
    """SSID-ji v dosegu na danem vmesniku. ``None`` ob napaki skena.

    Napako locimo od "prazen rezultat": ob napaki vrnemo ``None`` (klicatelj
    preskoci cikel in ne spremeni nacina), prazna mnozica pa pomeni "nic v
    dosegu" (legitimno -> AP po histerezi).
    """
    try:
        r = _nmcli("-t", "-f", "SSID", "device", "wifi", "list",
                   "ifname", iface, "--rescan", "auto", timeout=25)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    out: set[str] = set()
    for line in r.stdout.splitlines():
        # -t escapa ':' kot '\:'; za primerjavo imena to zadostuje.
        s = line.strip().replace("\\:", ":")
        if s:  # skrita omrezja (prazen SSID) preskocimo
            out.add(s)
    return out


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
    *, ssid: str, ap_con_name: str, iface: str,
    wait_s: float = CLIENT_CONNECT_WAIT_S,
) -> str:
    """Poskusi se povezati na ``ssid`` (klient); ob neuspehu vrne AP (fail-open).

    Vrne dejanski nacin: ``CLIENT`` ali ``AP``.
    """
    log(f"Ugasam AP '{ap_con_name}', poskusam klient '{ssid}' na {iface} ...")
    try:
        _nmcli("con", "down", ap_con_name)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"Opozorilo: con down AP: {exc}")

    # Ciljno omrezje aktiviramo eksplicitno po imenu profila (zanesljiveje kot
    # "device connect", ki prepusti izbiro NM-ju).
    try:
        up = _nmcli("con", "up", ssid, "ifname", iface,
                    timeout=max(30.0, wait_s))
        if up.returncode != 0:
            log(f"con up '{ssid}': {(up.stderr or '').strip() or up.stdout}")
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"NAPAKA con up '{ssid}': {exc}")

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


def apply_mode(mode: str, *, ssid: str, ap_con_name: str, iface: str) -> str:
    """Preklopi omrezje. Vrne dejanski nacin (``AP`` / ``CLIENT``).

    CLIENT nikoli ne pusti radia praznega: ce ``ssid`` odpove, ostane
    (ali se vrne) AP.
    """
    if mode == AP:
        return AP if ensure_ap(ap_con_name=ap_con_name, iface=iface) else AP
    return try_client_then_ap(ssid=ssid, ap_con_name=ap_con_name, iface=iface)


class WifiAutoDaemon:
    """Skenira okolico in usklaja wlan0: ciljni SSID viden -> klient, sicer AP."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.runtime_dir = Path(args.runtime_dir)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.target_ssid = args.ssid
        self.last_client_attempt_at: Optional[float] = None
        self.client_retry_s = float(
            getattr(args, "client_retry", DEFAULT_CLIENT_RETRY_S))

        # current_mode = zadnje DEJANSKO stanje wlan0; desired = kaj narekuje
        # zadnji sken; absent_count = zaporedni skeni brez ciljnega SSID-ja.
        self.current_mode: str = AP
        self.desired: str = AP
        self.absent_count: int = 0

        log(f"Zagon. Ciljni SSID '{self.target_ssid}' viden -> CLIENT, "
            f"sicer AP. Stanje v {self.runtime_dir}.")
        self._tick_locked()  # takojsna prva uskladitev

    def _act(self, mode: str) -> None:
        if mode == CLIENT:
            self.last_client_attempt_at = time.time()
        actual = apply_mode(
            mode, ssid=self.target_ssid,
            ap_con_name=self.args.ap_con_name, iface=self.args.iface,
        )
        # DEJANSKI nacin (CLIENT zahteva lahko pade na varnostni AP).
        self.current_mode = actual
        if mode == CLIENT and actual == AP:
            log(f"'{self.target_ssid}' viden, a klient ni uspel — ostajam AP.")

    def _write_status(self) -> None:
        st = {
            "mode": self.current_mode,
            "pending": None,  # ni odlozenih preklopov
            "desired": self.desired,
            "target_ssid": self.target_ssid,
            "ap_active": is_ap_active(self.args.ap_con_name),
            "client_active": is_client_active(
                self.args.iface, self.args.ap_con_name),
        }
        write_state(self.runtime_dir, st)

    def _enforce(self, desired: str) -> None:
        """Uskladi nmcli z zelenim nacinom (nikoli temen radio)."""
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
                self.current_mode = AP
        elif action == "drop_ap":
            if drop_ap(ap_con_name=self.args.ap_con_name):
                self.current_mode = CLIENT
        elif action == "try_client_then_ap":
            self._act(CLIENT)

    def _tick_locked(self) -> None:
        ssids = scan_ssids(self.args.iface)
        if ssids is None:
            log("Sken ni uspel — preskakam cikel (ne spreminjam nacina).")
            return

        if self.target_ssid in ssids:
            self.absent_count = 0
            desired = CLIENT
        else:
            self.absent_count += 1
            # Histereza: eno zgresen sken ne sme takoj podreti klienta.
            desired = AP if self.absent_count >= MISS_LIMIT else self.desired

        if desired != self.desired:
            seen = desired == CLIENT
            log(f"'{self.target_ssid}' {'je v dosegu' if seen else 'ni v dosegu'}"
                f" -> zelim {desired}.")
            self.desired = desired
            # Ob spremembi zelje takoj poskusi (reset gatinga poskusov).
            self.last_client_attempt_at = None

        self._enforce(desired)
        self._write_status()

    def tick(self) -> None:
        with self.lock:
            self._tick_locked()

    def run(self) -> int:
        log(f"Tecem. Skeniram za '{self.target_ssid}' vsakih "
            f"{POLL_INTERVAL_S:.0f} s, stanje v {self.runtime_dir}.")
        while not self.stop.is_set():
            self.tick()
            self.stop.wait(POLL_INTERVAL_S)
        log("Ustavljeno.")
        return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Samodejni preklop Wi-Fi (klient/AP) glede na doseg SSID-ja.")
    ap.add_argument("--ssid", default="Area51",
                     help="ciljni SSID; viden -> klient, sicer AP (privzeto Area51)")
    ap.add_argument("--ap-con-name", default="dron-ap",
                     help="ime nmcli profila za AP (privzeto dron-ap)")
    ap.add_argument("--iface", default="wlan0",
                     help="brezzicni vmesnik (privzeto wlan0)")
    ap.add_argument("--runtime-dir",
                     default=str(Path(__file__).resolve().parent.parent / "run"),
                     help="mapa za deljeno stanje z Django streznikom "
                          "(mora ustrezati UAV_RUNTIME_DIR)")
    ap.add_argument("--client-retry", type=float,
                     default=DEFAULT_CLIENT_RETRY_S,
                     help="sekund med ponovnimi poskusi klienta, dokler "
                          "drzimo varnostni AP (privzeto 60)")
    # Zastareli argumenti nekdanjega GPIO stikala --- sprejeti, a ignorirani,
    # da obstojeci systemd unit (--pin / --grace / --bounce-time) ne pade.
    ap.add_argument("--pin", type=int, default=19, help=argparse.SUPPRESS)
    ap.add_argument("--grace", type=float, default=0.0, help=argparse.SUPPRESS)
    ap.add_argument("--bounce-time", type=float, default=0.2,
                     help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    try:
        daemon = WifiAutoDaemon(args)
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
