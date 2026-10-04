"""Preklop Wi-Fi med klient in AP načinom prek fizičnega stikala.

Zamisel: žica med GPIO19 in GND je stikalo z dvema položajema.

======================  ==============================================
Stanje GPIO19           Zahtevan način
======================  ==============================================
ozemljen (LOW)          KLIENT --- poveži se na znano domače omrežje
odklopljen (HIGH)       AP --- oddajaj lasten hotspot ``dron-F450``
======================  ==============================================

Ta modul je namenoma **brez** kakršnekoli odvisnosti na strojno opremo,
``nmcli`` ali Django: vsebuje samo (1) čisto stanje odločanja
(:class:`SwitchPlanner`) in (2) tanek sloj za izmenjavo tega stanja med
dvema neodvisnima procesoma prek datotek. Zaradi tega je:

- v celoti enotno testljiv brez Raspberry Pi-ja in brez GPIO knjižnice,
- uporaben tako v samostojnem demonu (``scripts/wifi_gpio_switch.py``, ki
  bere GPIO in kliče ``nmcli``) kot v Django pogledih (ki datoteke samo
  berejo/pišejo, brez lastne logike odločanja) --- oba procesa delita
  isto definicijo stanja in ne more priti do razhajanja.

Zakaj odlog in ne takojšen preklop
-----------------------------------
Preklop omrežja skoraj vedno prekine WiFi povezavo, prek katere si nekdo
morda ravno gleda nadzorno ploščo --- preklop iz AP v klient odvzame
hotspot, na katerega je povezan telefon pilota; preklop iz klienta v AP
prekine domače omrežje, prek katerega je povezan laptop. Če na dronu
trenutno nihče ne gleda vmesnika, to ni pomembno in se preklopi takoj. Če pa
je nekdo aktiven, dobi ``UAV_NETWORK_GRACE_S`` (privzeto 30 s) časa, da se
odloči: pusti odštevanje teči, ali s klikom ``Preklopi zdaj`` prekinitev
sam sproži namesto da ga preseneti.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Načina
# ---------------------------------------------------------------------------
CLIENT = "CLIENT"
AP = "AP"

#: Privzeto trajanje odloga, če ga klicatelj ne poda izrecno.
DEFAULT_GRACE_S = 30.0

#: Koliko časa po zadnjem klicu ``touch_heartbeat`` šteje GUI za "aktiven".
#: Dlje od intervala pollinga v brskalniku (2 s), da ena zamujena zahteva
#: (npr. kratek zastoj Wi-Fi) ne prekine odloga po nepotrebnem.
GUI_ACTIVE_WINDOW_S = 8.0

#: Ob CLIENT zahtevi, ce domace omrezje ni dosegljivo: kako pogosto ponovno
#: poskusiti klienta, medtem ko drzimo varnostni AP (da Pi ne ostane temen).
DEFAULT_CLIENT_RETRY_S = 60.0


def other_mode(mode: str) -> str:
    return CLIENT if mode == AP else AP


def decide_wifi_enforcement(
    desired: str,
    *,
    ap_active: bool,
    client_active: bool,
    now: float = 0.0,
    last_client_attempt_at: Optional[float] = None,
    client_retry_s: float = DEFAULT_CLIENT_RETRY_S,
) -> dict[str, Any]:
    """Varnostna odlocitev: ``wlan0`` ne sme ostati brez AP in brez klienta.

    Stikalo lahko zahteva CLIENT, a Area51 ni v dosegu --- stari demon je
    samo ugasnil AP in pustil radio mrtev. Ta funkcija vrne ukaz:

    - ``noop`` --- stanje je OK
    - ``ensure_ap`` --- aktiviraj hotspot (pin hoce AP, ali fallback)
    - ``drop_ap`` --- klient dela, ugasni morebitni AP
    - ``try_client_then_ap`` --- poskusi znan Wi-Fi; ob neuspehu AP

    Demon (``wifi_gpio_switch.py``) izvede ukaz prek ``nmcli``.
    """
    if desired == AP:
        if ap_active:
            return {"action": "noop"}
        return {"action": "ensure_ap", "reason": "pin_wants_ap"}

    # desired == CLIENT
    if client_active:
        if ap_active:
            return {"action": "drop_ap", "reason": "client_ok"}
        return {"action": "noop"}

    due = (
        last_client_attempt_at is None
        or (now - last_client_attempt_at) >= client_retry_s
    )
    if due:
        return {"action": "try_client_then_ap", "reason": "client_missing"}
    if not ap_active:
        return {
            "action": "ensure_ap",
            "reason": "client_missing_keep_reachable",
        }
    return {"action": "noop"}


# ---------------------------------------------------------------------------
# Čisto stanje odločanja
# ---------------------------------------------------------------------------
@dataclass
class PendingSwitch:
    target: str
    requested_at: float
    deadline: float


@dataclass
class SwitchPlanner:
    """Odloča, ali se zahtevan preklop zgodi takoj, odloži ali prekliče.

    Instanca predstavlja **trenutno dejansko stanje omrežja** (kar je
    ``nmcli`` nazadnje dejansko naredil) in morebiten odložen preklop, ki še
    čaka na izvedbo. Sama ne kliče ``nmcli`` --- klicatelj izvede dejanje, ki
    ga metoda vrne, in nato pokliče :meth:`mark_applied`.
    """
    current_mode: str
    pending: Optional[PendingSwitch] = field(default=None)

    def request(
        self, target: str, *, gui_active: bool, now: float,
        grace_s: float = DEFAULT_GRACE_S,
    ) -> dict[str, Any]:
        """Obravnava spremembo želenega stanja stikala.

        Vrne ukaz za klicatelja:

        - ``{"action": "noop"}`` --- cilj je že trenutno stanje, nič se ne
          zgodi (tudi če je preklop že tekel v to smer, ostane nespremenjen).
        - ``{"action": "switch", "target": ...}`` --- preklopi takoj.
        - ``{"action": "pending", "target": ..., "deadline": ...}`` --- odlog
          se je pravkar začel.
        - ``{"action": "cancelled"}`` --- stikalo se je vrnilo v prejšnji
          položaj med odlogom; preklop odpade.
        - ``{"action": "retarget", "target": ...}`` --- odlog že teče, cilj
          pa se je spremenil (zaporedno preklapljanje stikala); rok se
          **ne** podaljša, da vrtenje stikala ne odloži preklopa v
          nedogled.
        """
        if self.pending is not None:
            if target == self.current_mode:
                self.pending = None
                return {"action": "cancelled"}
            if target != self.pending.target:
                self.pending = PendingSwitch(
                    target=target, requested_at=now,
                    deadline=self.pending.deadline)
                return {"action": "retarget", "target": target,
                        "deadline": self.pending.deadline}
            return {"action": "none"}  # isti zahtevek, ki že teče

        if target == self.current_mode:
            return {"action": "noop"}

        if not gui_active:
            return {"action": "switch", "target": target}

        deadline = now + grace_s
        self.pending = PendingSwitch(target=target, requested_at=now,
                                     deadline=deadline)
        return {"action": "pending", "target": target, "deadline": deadline}

    def force(self, now: float) -> dict[str, Any]:
        """Uporabnik je kliknil »Preklopi zdaj«."""
        if self.pending is None:
            return {"action": "none"}
        target = self.pending.target
        self.pending = None
        return {"action": "switch", "target": target}

    def tick(self, now: float) -> dict[str, Any]:
        """Redno preverjanje: je čas za izvedbo odloženega preklopa potekel?"""
        if self.pending is None:
            return {"action": "none"}
        if now >= self.pending.deadline:
            target = self.pending.target
            self.pending = None
            return {"action": "switch", "target": target}
        return {"action": "wait",
                "remaining_s": round(self.pending.deadline - now, 1)}

    def mark_applied(self, mode: str) -> None:
        """Klicatelj je uspešno izvedel preklop --- posodobi dejansko stanje."""
        self.current_mode = mode
        self.pending = None

    def status(self, now: float) -> dict[str, Any]:
        """Stanje za prikaz v vmesniku."""
        pending = None
        if self.pending is not None:
            pending = {
                "target": self.pending.target,
                "requested_at": self.pending.requested_at,
                "deadline": self.pending.deadline,
                "remaining_s": max(0.0, round(self.pending.deadline - now, 1)),
            }
        return {"mode": self.current_mode, "pending": pending}


# ---------------------------------------------------------------------------
# Koordinacija med demonom in Djangom prek datotek
# ---------------------------------------------------------------------------
STATE_FILE = "network_state.json"
HEARTBEAT_FILE = "gui_heartbeat"
FORCE_FILE = "network_force"


def _path(runtime_dir: Path | str, name: str) -> Path:
    return Path(runtime_dir) / name


def _ensure_runtime_dir(runtime_dir: Path | str) -> Path:
    """Ustvari runtime mapo, zapisljivo tako za root demona kot za Django.

    Demon teče kot root, Django kot ``dron``. Če mapa ostane ``root:root``
    0755, Django ne more ustvariti ``gui_heartbeat`` / ``network_force`` in
    API vrne 500 (PermissionError) --- UI pa potem kaže Django DEBUG HTML.
    """
    d = Path(runtime_dir)
    d.mkdir(parents=True, exist_ok=True)
    try:
        d.chmod(0o777)
    except OSError:
        pass
    return d


def write_state(runtime_dir: Path | str, status: dict[str, Any], *,
                reason: str = "") -> None:
    """Demon: zapiše trenutno stanje, da ga lahko Django prebere.

    Piše v začasno datoteko in jo preimenuje (``os.replace`` je atomičen na
    istem datotečnem sistemu) --- Django ne sme nikoli prebrati polovično
    zapisanega JSON-a, ker bere vzporedno iz drugega procesa.
    """
    d = _ensure_runtime_dir(runtime_dir)
    payload = {**status, "reason": reason, "written_at": time.time()}
    final = _path(d, STATE_FILE)
    tmp = _path(d, STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    try:
        tmp.chmod(0o666)
    except OSError:
        pass
    tmp.replace(final)
    try:
        final.chmod(0o666)
    except OSError:
        pass


def read_state(runtime_dir: Path | str) -> Optional[dict[str, Any]]:
    """Django: prebere zadnje stanje, ki ga je zapisal demon.

    Vrne ``None``, če demon (še) ne teče --- npr. stikalo ni priklopljeno,
    to ni napaka. Tudi če datoteka obstaja, a je ni mogoče prebrati
    (npr. root-only po starem demonu), vrnemo ``None``.
    """
    p = _path(runtime_dir, STATE_FILE)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def touch_heartbeat(runtime_dir: Path | str) -> None:
    """Django: zabeleži, da je nekdo pravkar gledal vmesnik.

    Namenoma poceni operacija (dotik prazne datoteke) --- kliče se ob vsakem
    branju stanja iz brskalnika, torej vsakih nekaj sekund.
    """
    d = _ensure_runtime_dir(runtime_dir)
    path = _path(d, HEARTBEAT_FILE)
    path.touch()
    try:
        path.chmod(0o666)
    except OSError:
        pass


def heartbeat_age_s(runtime_dir: Path | str, now: float) -> Optional[float]:
    """Demon: koliko sekund je minilo od zadnjega dotika. ``None`` = nikoli."""
    p = _path(runtime_dir, HEARTBEAT_FILE)
    try:
        return max(0.0, now - p.stat().st_mtime)
    except OSError:
        return None


def is_gui_active(runtime_dir: Path | str, now: float,
                  window_s: float = GUI_ACTIVE_WINDOW_S) -> bool:
    age = heartbeat_age_s(runtime_dir, now)
    return age is not None and age < window_s


def request_force(runtime_dir: Path | str) -> None:
    """Django: uporabnik je kliknil »Preklopi zdaj«."""
    d = _ensure_runtime_dir(runtime_dir)
    path = _path(d, FORCE_FILE)
    path.touch()
    try:
        path.chmod(0o666)
    except OSError:
        pass


def consume_force(runtime_dir: Path | str) -> bool:
    """Demon: je bila zahtevana takojšnja izvedba? Če da, počisti zahtevo."""
    p = _path(runtime_dir, FORCE_FILE)
    if not p.is_file():
        return False
    try:
        p.unlink()
    except OSError:
        pass
    return True
