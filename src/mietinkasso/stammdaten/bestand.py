"""Reine Bestandsregeln (Auftrag HV-20261030-AKTIVER-BESTAND) - kein DB-Zugriff.

Filtern nur ANZEIGE und Listen: `verwaltet_bis` verkürzt keinen Vertrag, löscht
nichts und wirkt nicht auf Salden, Rückstände, Buchungen oder Vorschreibungen.
`getattr` hält ältere Fixtures ohne die neuen Felder kompatibel; `None` bleibt
Bestand. Annahme: `StammdatenRepository` liefert Objekte, die `verwaltet_bis`
durchreichen (Einheit-Zeile); das Repository lag der Umsetzung nicht vor.
"""

from __future__ import annotations

from datetime import date

BESTAND_FILTER = ("aktiv", "beendet", "alle")
BESTAND_STANDARD = "aktiv"


def normalisiere_bestand_filter(wert: str | None) -> str:
    """Unbekannte/leere Werte fallen sicher auf den aktiven Bestand zurück."""

    text = (wert or "").strip().casefold()
    return text if text in BESTAND_FILTER else BESTAND_STANDARD


def einheit_in_verwaltung_am(einheit, stichtag: date) -> bool:
    """`verwaltet_bis` gilt inklusive; ohne Datum bleibt die Einheit im Bestand."""

    verwaltet_bis = getattr(einheit, "verwaltet_bis", None)
    return verwaltet_bis is None or stichtag <= verwaltet_bis


def vertrag_status_am(vertrag, stichtag: date) -> str:
    """Laufzeit ausschließlich aus dem Vertrag: `gueltig_bis` gilt inklusive."""

    if vertrag.gueltig_bis is not None and vertrag.gueltig_bis < stichtag:
        return "BEENDET"
    if vertrag.gueltig_von > stichtag:
        return "KUENFTIG"
    return "AKTIV"


def bestand_status(vertrag, einheit, stichtag: date) -> str:
    """Ausgeschiedene Einheit hat Vorrang, ohne die Vertragslaufzeit zu ändern."""

    if not einheit_in_verwaltung_am(einheit, stichtag):
        return "VERWALTUNG_BEENDET"
    return vertrag_status_am(vertrag, stichtag)


def status_passt_zu_bestandfilter(status: str, bestand_filter: str) -> bool:
    if bestand_filter == "alle":
        return True
    if bestand_filter == "beendet":
        return status in ("BEENDET", "VERWALTUNG_BEENDET")
    return status in ("AKTIV", "KUENFTIG")


def einheit_passt_zu_bestandfilter(einheit, bestand_filter: str, stichtag: date) -> bool:
    if bestand_filter == "alle":
        return True
    in_verwaltung = einheit_in_verwaltung_am(einheit, stichtag)
    return not in_verwaltung if bestand_filter == "beendet" else in_verwaltung
