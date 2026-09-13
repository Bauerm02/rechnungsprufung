"""Reine, abhängigkeitsfreie Kalenderarithmetik für die Indexautomatik
(Auftrag 13.09., HV-20260913-INDEXAUTOMATIK).

Bewusst KEINE neue Abhängigkeit (`python-dateutil`/`relativedelta`) - das
Repository kennt bislang ausschließlich naive `date`/UTC-`datetime`
(siehe Explorationsbericht zu diesem Auftrag) und `mieweg_vorschau/
service.py::_naechster_gueltiger_april` macht Kalenderarithmetik bereits
von Hand. `zoneinfo` ist Standardbibliothek (Python 3.9+), keine neue
Abhängigkeit."""

from __future__ import annotations

import calendar
from datetime import date, datetime
from zoneinfo import ZoneInfo

WIEN = ZoneInfo("Europe/Vienna")


def heute_wien(jetzt: datetime | None = None) -> date:
    """Aktuelles Kalenderdatum in Europe/Vienna - `jetzt` ist für Tests
    injizierbar (muss TZ-aware sein, sonst wird UTC angenommen, siehe
    unten)."""

    aktuelle_zeit = jetzt or datetime.now(WIEN)
    if aktuelle_zeit.tzinfo is None:
        raise ValueError("`jetzt` muss TZ-aware sein (z. B. UTC oder Europe/Vienna) - kein implizites Naive-Datum.")
    return aktuelle_zeit.astimezone(WIEN).date()


def kalendermonate_subtrahieren(datum: date, monate: int) -> date:
    """Zieht `monate` volle KALENDERmonate von `datum` ab (nicht 30/90
    Tage) - Tag wird auf den letzten Tag des Zielmonats gekappt, falls
    dieser kürzer ist (z. B. 31.05. minus 3 Monate -> 28.02. bzw. 29.02.
    in einem Schaltjahr, nie ein ungültiges 31.02.)."""

    monat_index = datum.month - 1 - monate
    jahr = datum.year + monat_index // 12
    monat = monat_index % 12 + 1
    letzter_tag_im_zielmonat = calendar.monthrange(jahr, monat)[1]
    tag = min(datum.day, letzter_tag_im_zielmonat)
    return date(jahr, monat, tag)


def naechster_zinstermin_ab(stichtag: date, *, faelligkeit_tag: int) -> date:
    """Der nächste monatliche Zinstermin (Fälligkeitstag laut
    `VertragTable.faelligkeit_tag`) AUF ODER NACH `stichtag` - trägt
    §16 Abs9 MRG (Wirksamkeit ab dem nächsten Zinstermin nach
    fristgerechtem Zugang). Ein `faelligkeit_tag`, der im Zielmonat
    nicht existiert (z. B. 31 im Februar), fällt auf den letzten Tag
    dieses Monats zurück statt einen ungültigen Tag zu erzeugen."""

    if faelligkeit_tag < 1:
        raise ValueError(f"faelligkeit_tag muss >= 1 sein, war {faelligkeit_tag}.")

    def _termin_im_monat(jahr: int, monat: int) -> date:
        letzter_tag = calendar.monthrange(jahr, monat)[1]
        return date(jahr, monat, min(faelligkeit_tag, letzter_tag))

    kandidat = _termin_im_monat(stichtag.year, stichtag.month)
    if kandidat >= stichtag:
        return kandidat
    naechster_monat_index = stichtag.month  # 0-basiert nach +1 unten
    jahr = stichtag.year + naechster_monat_index // 12
    monat = naechster_monat_index % 12 + 1
    return _termin_im_monat(jahr, monat)
