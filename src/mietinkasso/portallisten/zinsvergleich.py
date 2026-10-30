"""Deterministischer Vormonatsvergleich der Zinsliste (HV-20261030-ZINSLISTE).

Rein lesend und ohne Datenzugriff. Verglichen wird nur über `einheit_id`/`vertrag_id`.
Eine Änderung sagt nichts darüber, ob sie rechtlich erlaubt oder beschlossen ist.
Der VPI bleibt außen vor, weil Profile nur den aktuellen Stand zeigen."""

from __future__ import annotations

from dataclasses import dataclass

from mietinkasso.portallisten.zinsdetails import KATEGORIEN, ZinsDetailListe, ZinsDetailZeile

ART_NICHT_IM_VORMONAT = "NICHT_IM_VORMONAT"
ART_NICHT_IM_AKTUELLEN_MONAT = "NICHT_IM_AKTUELLEN_MONAT"
ART_NICHT_EINDEUTIG = "NICHT_EINDEUTIG"
ART_VERTRAG = "VERTRAG"
ART_MIETER = "MIETER"
ART_NUTZUNG = "NUTZUNG"
ART_BETRAG = "BETRAG"


@dataclass(frozen=True)
class ZinsAenderung:
    art: str
    objekt_id: str
    einheit_id: str
    vertrag_id: str | None
    top: str
    mieter: str
    bereich: str
    alt_text: str | None = None
    neu_text: str | None = None
    alt_cent: int | None = None  # None: keine Komponente dieser Kategorie, nie 0
    neu_cent: int | None = None
    delta_cent: int | None = None  # nur wenn beide Seiten bekannt sind


def _monatsindex(monat: str) -> int:
    jahr, monatszahl = monat.split("-")
    return int(jahr) * 12 + int(monatszahl)


def _name(zeile) -> str:
    return zeile.basis.debitor_name or zeile.anzeige_name


def _aenderung(art, zeile, bereich, **werte) -> ZinsAenderung:
    basis = zeile.basis
    return ZinsAenderung(
        art=art, objekt_id=basis.objekt_id, einheit_id=basis.einheit_id, vertrag_id=basis.vertrag_id,
        top=zeile.top, mieter=_name(zeile), bereich=bereich, **werte,
    )


def _vertrag_text(zeilen) -> str | None:
    texte = [f"{_name(z)} (Vertrag {z.basis.vertrag_id})" if z.basis.vertrag_id else _name(z) for z in zeilen]
    return ", ".join(texte) or None


def _brutto_je_kategorie(zeile) -> dict[str, int]:
    return {k.kategorie: k.betraege.brutto_cent for k in zeile.kategorien if k.betraege is not None}


def _vergleiche_paar(alt, neu) -> list[ZinsAenderung]:
    ergebnis = []
    if alt.basis.vertrag_id == neu.basis.vertrag_id and alt.basis.debitor_name != neu.basis.debitor_name:
        ergebnis.append(_aenderung(ART_MIETER, neu, "Mieter", alt_text=alt.basis.debitor_name, neu_text=neu.basis.debitor_name))
    if alt.basis.nutzungsstatus != neu.basis.nutzungsstatus:
        ergebnis.append(_aenderung(ART_NUTZUNG, neu, "Nutzungsstatus", alt_text=alt.basis.nutzungsstatus, neu_text=neu.basis.nutzungsstatus))
    alt_brutto, neu_brutto = _brutto_je_kategorie(alt), _brutto_je_kategorie(neu)
    for kategorie, bezeichnung in KATEGORIEN:
        alt_cent, neu_cent = alt_brutto.get(kategorie), neu_brutto.get(kategorie)
        if alt_cent == neu_cent:
            continue
        delta = None if alt_cent is None or neu_cent is None else neu_cent - alt_cent
        ergebnis.append(_aenderung(ART_BETRAG, neu, f"Brutto {bezeichnung}", alt_cent=alt_cent, neu_cent=neu_cent, delta_cent=delta))
    return ergebnis


def _sortiert(zeilen) -> list:
    return sorted(zeilen, key=lambda z: z.basis.vertrag_id or "")


def _vergleiche_einheit(alt, neu, suche_aktiv: bool) -> list[ZinsAenderung]:
    alt_je = {z.basis.vertrag_id: z for z in alt}
    neu_je = {z.basis.vertrag_id: z for z in neu}
    if len(alt_je) != len(alt) or len(neu_je) != len(neu):
        return [_aenderung(ART_NICHT_EINDEUTIG, (neu or alt)[0], "Zuordnung nicht eindeutig; nicht verglichen")]
    nur_alt = _sortiert(z for z in alt if z.basis.vertrag_id not in neu_je)
    nur_neu = _sortiert(z for z in neu if z.basis.vertrag_id not in alt_je)
    ergebnis = []
    if suche_aktiv or not alt or not neu:
        ergebnis.extend(_aenderung(ART_NICHT_IM_AKTUELLEN_MONAT, z, "Im gefilterten aktuellen Monat nicht enthalten") for z in nur_alt)
        ergebnis.extend(_aenderung(ART_NICHT_IM_VORMONAT, z, "Im gefilterten Vormonat nicht enthalten") for z in nur_neu)
    elif nur_alt or nur_neu:
        ergebnis.append(_aenderung(ART_VERTRAG, (nur_neu or nur_alt)[0], "Mieter-/Vertragswechsel", alt_text=_vertrag_text(nur_alt), neu_text=_vertrag_text(nur_neu)))
        if len(nur_alt) == 1 == len(nur_neu):
            ergebnis.extend(_vergleiche_paar(nur_alt[0], nur_neu[0]))
    for zeile in _sortiert(z for z in neu if z.basis.vertrag_id in alt_je):
        ergebnis.extend(_vergleiche_paar(alt_je[zeile.basis.vertrag_id], zeile))
    return ergebnis


def _gruppen(details: ZinsDetailListe) -> dict[tuple[str, str], list[ZinsDetailZeile]]:
    gruppen: dict[tuple[str, str], list[ZinsDetailZeile]] = {}
    for zeile in details.zeilen:
        gruppen.setdefault((zeile.basis.objekt_id, zeile.basis.einheit_id), []).append(zeile)
    return gruppen


def vergleiche_zinsdetails(vormonat: ZinsDetailListe, aktuell: ZinsDetailListe) -> tuple[ZinsAenderung, ...]:
    """Wirft `ValueError` bei abweichendem Scope oder Monat; ein Fehler ist nie eine Leermeldung."""

    alt_basis, neu_basis = vormonat.basis, aktuell.basis
    if (alt_basis.objekt_filter, alt_basis.suche) != (neu_basis.objekt_filter, neu_basis.suche):
        raise ValueError("Vergleich nur bei identischem Objekt- und Suchfilter.")
    if _monatsindex(neu_basis.monat) - _monatsindex(alt_basis.monat) != 1:
        raise ValueError("Vergleich nur mit dem direkten Vormonat.")
    alt_gruppen, neu_gruppen = _gruppen(vormonat), _gruppen(aktuell)
    ergebnis: list[ZinsAenderung] = []
    for schluessel in sorted(alt_gruppen.keys() | neu_gruppen.keys()):
        ergebnis.extend(_vergleiche_einheit(alt_gruppen.get(schluessel, []), neu_gruppen.get(schluessel, []), bool(neu_basis.suche)))
    return tuple(ergebnis)
