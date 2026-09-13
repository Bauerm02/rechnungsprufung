"""Übersetzt rohe PDF-Regex-Treffer (`pdf_extraktion.py`) in Formular-
Vorschlagswerte für das editierbare Mietvertragsprofil-Review (Auftrag
HV-20260913-VERTRAGSANLAGE). Jeder Vorschlag bleibt EDITIERBAR und wird
NIE automatisch übernommen - siehe `backoffice/vertragsanlage_form.py`.

Nutzungsart wird NUR gesetzt, wenn GENAU EINER der drei Hinweismuster
(Wohnung/Büro/Geschäftslokal) trifft - bei Mehrdeutigkeit oder fehlendem
Treffer bleibt das Feld leer/UNGEKLAERT (keine Ableitung, keine
Rateentscheidung, siehe Auftrag: "keine Ableitung ... Büro=MRG-frei")."""

from __future__ import annotations

from dataclasses import dataclass

from mietinkasso.vertragsanlage.pdf_extraktion import ExtraktionsErgebnis


@dataclass(frozen=True)
class Vorschlag:
    formularwert: str
    seite: int
    auszug: str


def _mietbeginn_iso(ddmmyyyy: str) -> str | None:
    try:
        tag, monat, jahr = ddmmyyyy.split(".")
        from datetime import date

        return date(int(jahr), int(monat), int(tag)).isoformat()
    except (ValueError, TypeError):
        return None


def vorschlaege_aus_extraktion(ergebnis: ExtraktionsErgebnis) -> dict[str, Vorschlag]:
    t = ergebnis.treffer
    vorschlaege: dict[str, Vorschlag] = {}

    if "mietbeginn" in t:
        tr = t["mietbeginn"]
        iso = _mietbeginn_iso(tr.wert)
        if iso is not None:
            vorschlaege["urspruenglicher_mietbeginn"] = Vorschlag(iso, tr.seite, tr.auszug)

    if "kaution_cent" in t:
        tr = t["kaution_cent"]
        vorschlaege["vertragliche_kaution_cent"] = Vorschlag(tr.wert, tr.seite, tr.auszug)

    if "index_reihe" in t:
        tr = t["index_reihe"]
        vorschlaege["index_reihe"] = Vorschlag(tr.wert, tr.seite, tr.auszug)

    if "index_schwelle_prozent" in t:
        tr = t["index_schwelle_prozent"]
        vorschlaege["index_schwelle_prozent"] = Vorschlag(tr.wert, tr.seite, tr.auszug)

    nutzungsart_treffer = {
        "WOHNUNG": t.get("nutzungsart_wohnung"),
        "BUERO": t.get("nutzungsart_buero"),
        "GESCHAEFTSLOKAL": t.get("nutzungsart_geschaeft"),
    }
    gefundene = {k: v for k, v in nutzungsart_treffer.items() if v is not None}
    if len(gefundene) == 1:
        (nutzungsart, tr), = gefundene.items()
        vorschlaege["nutzungsart"] = Vorschlag(nutzungsart, tr.seite, tr.auszug)
    # Mehrdeutig (mehrere Muster gleichzeitig) oder kein Treffer: bewusst
    # KEIN Vorschlag - das Formular bleibt bei UNGEKLAERT.

    if "mahngebuehr_keine" in t:
        tr = t["mahngebuehr_keine"]
        vorschlaege["mahngebuehr_cent"] = Vorschlag("0", tr.seite, tr.auszug)
    elif "mahngebuehr_cent" in t:
        tr = t["mahngebuehr_cent"]
        vorschlaege["mahngebuehr_cent"] = Vorschlag(tr.wert, tr.seite, tr.auszug)
    # sonst: kein Vorschlag - Feld bleibt leer -> None (unbekannt, KEIN erfundenes 0)

    return vorschlaege
