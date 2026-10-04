"""Zaznava vzleta in pristanka iz telemetrije.

Zapisovalnik telemetrije se je prvotno prozil ob armanju in ustavljal ob
dis-armanju. To je preveč sirok interval: med armanjem in vzletom lahko mine
minuta preverjanja, po pristanku pa se nekaj casa pusti motorje teci. Poleg
tega vsak armanje-brez-leta (npr. test motorjev) ustvari prazno sejo.

Ta modul zazna **dejanski zacetek in konec leta**. Primarni vir je MAVLink
sporocilo ``EXTENDED_SYS_STATE``, ki ima polje ``landed_state`` s stirimi
vrednostmi (na tleh / v zraku / vzletanje / pristajanje) --- to je stanje, ki
ga oceni sam avtopilot iz vec senzorjev hkrati.

Ce krmilnik tega sporocila ne posilja (starejsi firmware ali izklopljen
stream), modul pade nazaj na visino nad vzletiscem, z locenima pragoma za
vzlet in pristanek ter zahtevo po mirovanju. Dva razlicna pragova sta nujna:
z enim samim bi dron, ki v lebdenju nihá okoli praga, sprozil neskoncno
zaporedje "vzletel-pristal-vzletel".

Modul je cist --- nima niti niti ur niti MAVLink odvisnosti, cas dobi kot
argument. Zato je mogoce v testih odigrati cel let v nekaj vrsticah.
"""
from __future__ import annotations

from typing import Optional

# MAV_LANDED_STATE
LANDED_UNDEFINED = 0
LANDED_ON_GROUND = 1
LANDED_IN_AIR = 2
LANDED_TAKEOFF = 3
LANDED_LANDING = 4

#: Vrednosti ``landed_state``, ki pomenijo "ni na tleh". Pristajanje (4) sem
#: sodi namenoma: let se konca sele, ko krmilnik poroca ON_GROUND.
_AIRBORNE_STATES = frozenset({LANDED_IN_AIR, LANDED_TAKEOFF, LANDED_LANDING})

TAKEOFF = "takeoff"
LANDING = "landing"


class AirborneDetector:
    """Stroj stanj z dvema stanjema: na tleh / v zraku.

    :meth:`update` vrne ``"takeoff"`` ali ``"landing"`` ob prehodu, sicer
    ``None``. Klicatelj na podlagi tega odpre ali zapre zapis.
    """

    def __init__(
        self,
        takeoff_alt_m: float = 0.8,
        land_alt_m: float = 0.4,
        land_settle_s: float = 3.0,
    ) -> None:
        if land_alt_m >= takeoff_alt_m:
            raise ValueError(
                "land_alt_m mora biti manjsi od takeoff_alt_m (histereza).")
        self.takeoff_alt_m = takeoff_alt_m
        self.land_alt_m = land_alt_m
        self.land_settle_s = land_settle_s

        self._airborne = False
        self._low_since: Optional[float] = None
        self._source = "brez podatkov"

    @property
    def airborne(self) -> bool:
        return self._airborne

    @property
    def source(self) -> str:
        """Kateri vir je nazadnje dolocil stanje --- za diagnostiko."""
        return self._source

    def reset(self) -> None:
        """Pozabi stanje (npr. ob novi povezavi s krmilnikom)."""
        self._airborne = False
        self._low_since = None

    def update(
        self,
        *,
        armed: bool,
        rel_alt_m: Optional[float],
        landed_state: Optional[int],
        now: float,
    ) -> Optional[str]:
        """Sprejme trenutno stanje in vrne morebiten prehod."""
        # Dis-arm je nedvoumen konec leta, ne glede na vse ostalo. Ce je dron
        # dis-armiran, ne leti --- tudi ce zadnja znana visina trdi drugace
        # (npr. zastarel podatek po izgubi povezave).
        if not armed:
            self._low_since = None
            self._source = "dis-armiran"
            return self._transition(False)

        if landed_state is not None and landed_state != LANDED_UNDEFINED:
            self._source = "EXTENDED_SYS_STATE"
            self._low_since = None
            return self._transition(landed_state in _AIRBORNE_STATES)

        # --- rezervna pot: visina nad vzletiscem ---
        self._source = "visina"
        if rel_alt_m is None:
            return None

        if not self._airborne:
            if rel_alt_m >= self.takeoff_alt_m:
                return self._transition(True)
            return None

        # V zraku: pristanek zahteva nizko visino, ki *vztraja*.
        if rel_alt_m > self.land_alt_m:
            self._low_since = None
            return None
        if self._low_since is None:
            self._low_since = now
            return None
        if now - self._low_since >= self.land_settle_s:
            self._low_since = None
            return self._transition(False)
        return None

    def _transition(self, airborne: bool) -> Optional[str]:
        if airborne == self._airborne:
            return None
        self._airborne = airborne
        return TAKEOFF if airborne else LANDING
