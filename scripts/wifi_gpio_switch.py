#!/usr/bin/env python3
"""Samodejni preklop Wi-Fi med klient in AP nacinom glede na doseg znanih omrezij.

Ideja: na terenu ni brskalnika, ki bi klikal "preklopi na AP". Namesto tega
dron **sam skenira** okolico in jo primerja s shranjenimi (znanimi) Wi-Fi
profili v NetworkManagerju (``nmcli con show``, tip ``802-11-wireless``,
brez AP profilov):

* vsaj eno znano omrezje je vidno v skenu  --- dron se poveze nanj (klient);
  ce jih je vec, poskusa po vrsti od najmocnejsega signala naprej. Ce
  nobena povezava ne uspe, ostane/se vrne AP (fail-open), da Pi ne ostane
  nedosegljiv; klient se nato ponovno poskusa.
* nobeno znano omrezje ni v dosegu         --- dron sam oddaja svoj SSID
  (AP nacin, profil ``dron-ap`` prek ``nmcli con up``).

Z ``--ssid`` (lahko veckrat) se nabor znanih omrezij po zelji omeji na
dolocene SSID-je; brez njega se uporabijo vsi shranjeni profili.

Da marginalen signal ne povzroca utripanja, je na prehodu "znana omrezja so
izginila" histereza: AP se vklopi sele po :data:`MISS_LIMIT` zaporednih
skenih brez znanega SSID-ja. Prvo *pojavitev* pa upostevamo takoj.

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

    # na dronu, kot root (nmcli) --- vsa shranjena Wi-Fi omrezja
    sudo python3 scripts/wifi_gpio_switch.py

    # samo dolocena omrezja / drug vmesnik
    python3 scripts/wifi_gpio_switch.py --ssid Area51 --ssid DomaciWiFi \\
        --iface wlan0
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
#: Toliko zaporednih skenov brez znanega SSID-ja, preden preklopimo na AP
#: (histereza proti utripanju ob sibkem signalu).
MISS_LIMIT = 2
#: Najvec toliko znanih omrezij poskusimo v enem ciklu (AP je med poskusi
#: ugasnjen, zato vrsta ne sme biti predolga).
MAX_CLIENT_CANDIDATES = 3


def log(msg: str) -> None:
    print(f"[wifi-switch] {msg}", flush=True)


def _nmcli(*args: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["nmcli", *args],
        capture_output=True, text=True, timeout=timeout, check=False,
    )


def _unescape(value: str) -> str:
    # -t / -g escapata ':' kot '\:'; za primerjavo imen to zadostuje.
    return value.strip().replace("\\:", ":")


def scan_ssids(iface: str) -> Optional[dict[str, int]]:
    """SSID-ji v dosegu na danem vmesniku: ``{SSID: signal 0-100}``.

    Napako locimo od "prazen rezultat": ob napaki vrnemo ``None`` (klicatelj
    preskoci cikel in ne spremeni nacina), prazen slovar pa pomeni "nic v
    dosegu" (legitimno -> AP po histerezi).
    """
    try:
        r = _nmcli("-t", "-f", "SSID,SIGNAL", "device", "wifi", "list",
                   "ifname", iface, "--rescan", "auto", timeout=25)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    out: dict[str, int] = {}
    for line in r.stdout.splitlines():
        # SSID lahko vsebuje (escapano) dvopicje, SIGNAL je zadnje polje.
        raw_ssid, _, raw_signal = line.rpartition(":")
        ssid = _unescape(raw_ssid)
        if not ssid:  # skrita omrezja (prazen SSID) preskocimo
            continue
        try:
            signal = int(raw_signal)
        except ValueError:
            signal = 0
        # Isti SSID z vec dostopnih tock: obdrzimo najmocnejso.
        out[ssid] = max(signal, out.get(ssid, -1))
    return out


def known_wifi_profiles(ap_con_name: str) -> dict[str, str]:
    """Shranjeni klient Wi-Fi profili: ``{SSID: ime nmcli profila}``.

    AP profile (``ap_con_name`` in vse z ``802-11-wireless.mode`` = ``ap``)
    izpustimo. Ob napaki vrne prazen slovar (-> ni znanih -> AP).
    """
    try:
        r = _nmcli("-t", "-f", "NAME,TYPE", "con", "show", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return {}
    if r.returncode != 0:
        return {}
    out: dict[str, str] = {}
    for line in r.stdout.splitlines():
        raw_name, _, con_type = line.rpartition(":")
        name = _unescape(raw_name)
        if con_type != "802-11-wireless" or not name or name == ap_con_name:
            continue
        try:
            d = _nmcli("-g", "802-11-wireless.ssid,802-11-wireless.mode",
                       "con", "show", name, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        if d.returncode != 0:
            continue
        fields = d.stdout.splitlines()
        ssid = _unescape(fields[0]) if fields else ""
        mode = fields[1].strip() if len(fields) > 1 else ""
        if not ssid or mode == "ap":
            continue
        out.setdefault(ssid, name)
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
    *, profiles: list[str], ap_con_name: str, iface: str,
    wait_s: float = CLIENT_CONNECT_WAIT_S,
) -> str:
    """Poskusi znane profile po vrsti (klient); ob neuspehu vrne AP (fail-open).

    Vrne dejanski nacin: ``CLIENT`` ali ``AP``.
    """
    log(f"Ugasam AP '{ap_con_name}', poskusam klient na {iface}: "
        f"{', '.join(profiles) or '(ni kandidatov)'} ...")
    try:
        _nmcli("con", "down", ap_con_name)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"Opozorilo: con down AP: {exc}")

    # Vsak profil aktiviramo eksplicitno po imenu (zanesljiveje kot
    # "device connect", ki prepusti izbiro NM-ju). "con up --wait" blokira,
    # dokler povezava ni aktivna ali ne pade.
    for name in profiles:
        try:
            up = _nmcli("--wait", str(int(wait_s)), "con", "up", name,
                        "ifname", iface, timeout=wait_s + 10)
            if up.returncode != 0:
                log(f"con up '{name}': "
                    f"{(up.stderr or '').strip() or up.stdout}")
        except (OSError, subprocess.SubprocessError) as exc:
            log(f"NAPAKA con up '{name}': {exc}")

        # Kratek odlog: NM po uspesnem "con up" lahko stanje objavi z zamikom.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if is_client_active(iface, ap_con_name):
                names = [n for n in active_connections_on_iface(iface)
                         if n != ap_con_name]
                log(f"OK: CLIENT aktiven ({', '.join(names) or iface}).")
                return CLIENT
            time.sleep(1.0)

    log("Klient ni povezan — varnostni AP (Pi mora ostati dosegljiv).")
    if ensure_ap(ap_con_name=ap_con_name, iface=iface):
        return AP
    log("NAPAKA: tudi varnostni AP ni uspel.")
    return AP


def apply_mode(mode: str, *, profiles: list[str], ap_con_name: str,
               iface: str) -> str:
    """Preklopi omrezje. Vrne dejanski nacin (``AP`` / ``CLIENT``).

    CLIENT nikoli ne pusti radia praznega: ce vsi ``profiles`` odpovejo,
    ostane (ali se vrne) AP.
    """
    if mode == AP:
        return AP if ensure_ap(ap_con_name=ap_con_name, iface=iface) else AP
    return try_client_then_ap(profiles=profiles, ap_con_name=ap_con_name,
                              iface=iface)


class WifiAutoDaemon:
    """Skenira okolico in usklaja wlan0: znan SSID viden -> klient, sicer AP."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.runtime_dir = Path(args.runtime_dir)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.stop = threading.Event()
        self.lock = threading.Lock()
        # Neobvezna omejitev nabora znanih omrezij (--ssid, lahko veckrat).
        self.ssid_filter: Optional[set[str]] = set(args.ssid) if args.ssid else None
        self.last_client_attempt_at: Optional[float] = None
        self.client_retry_s = float(
            getattr(args, "client_retry", DEFAULT_CLIENT_RETRY_S))

        # current_mode = zadnje DEJANSKO stanje wlan0; desired = kaj narekuje
        # zadnji sken; absent_count = zaporedni skeni brez znanega SSID-ja;
        # candidates = vidni znani profili, urejeni po moci signala.
        self.current_mode: str = AP
        self.desired: str = AP
        self.absent_count: int = 0
        self.visible_known: list[str] = []
        self.candidates: list[str] = []

        which = (", ".join(sorted(self.ssid_filter)) if self.ssid_filter
                 else "vsa shranjena Wi-Fi omrezja")
        log(f"Zagon. Znano omrezje ({which}) vidno -> CLIENT, sicer AP. "
            f"Stanje v {self.runtime_dir}.")
        self._tick_locked()  # takojsna prva uskladitev

    def _act(self, mode: str) -> None:
        if mode == CLIENT:
            self.last_client_attempt_at = time.time()
        actual = apply_mode(
            mode, profiles=self.candidates[:MAX_CLIENT_CANDIDATES],
            ap_con_name=self.args.ap_con_name, iface=self.args.iface,
        )
        # DEJANSKI nacin (CLIENT zahteva lahko pade na varnostni AP).
        self.current_mode = actual
        if mode == CLIENT and actual == AP:
            log(f"Znana omrezja vidna ({', '.join(self.visible_known)}), "
                f"a klient ni uspel — ostajam AP.")

    def _write_status(self) -> None:
        st = {
            "mode": self.current_mode,
            "pending": None,  # ni odlozenih preklopov
            "desired": self.desired,
            "target_ssid": self.visible_known[0] if self.visible_known else None,
            "visible_known_ssids": self.visible_known,
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

        known = known_wifi_profiles(self.args.ap_con_name)
        if self.ssid_filter is not None:
            known = {s: n for s, n in known.items() if s in self.ssid_filter}
        visible = sorted((s for s in known if s in ssids),
                         key=lambda s: ssids[s], reverse=True)
        self.visible_known = visible
        self.candidates = [known[s] for s in visible]

        if visible:
            self.absent_count = 0
            desired = CLIENT
        else:
            self.absent_count += 1
            # Histereza: eno zgresen sken ne sme takoj podreti klienta.
            desired = AP if self.absent_count >= MISS_LIMIT else self.desired

        if desired != self.desired:
            if desired == CLIENT:
                log(f"Znana omrezja v dosegu ({', '.join(visible)}) "
                    f"-> zelim {desired}.")
            else:
                log(f"Nobeno znano omrezje ni v dosegu -> zelim {desired}.")
            self.desired = desired
            # Ob spremembi zelje takoj poskusi (reset gatinga poskusov).
            self.last_client_attempt_at = None

        self._enforce(desired)
        self._write_status()

    def tick(self) -> None:
        with self.lock:
            self._tick_locked()

    def run(self) -> int:
        log(f"Tecem. Skeniram za znana omrezja vsakih "
            f"{POLL_INTERVAL_S:.0f} s, stanje v {self.runtime_dir}.")
        while not self.stop.is_set():
            self.tick()
            self.stop.wait(POLL_INTERVAL_S)
        log("Ustavljeno.")
        return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Samodejni preklop Wi-Fi (klient/AP) glede na doseg "
                    "znanih omrezij.")
    ap.add_argument("--ssid", action="append", default=None,
                     help="omeji znana omrezja na ta SSID (lahko veckrat); "
                          "privzeto vsi shranjeni Wi-Fi profili")
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
