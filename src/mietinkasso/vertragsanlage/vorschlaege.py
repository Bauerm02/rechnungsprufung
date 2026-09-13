"""Übersetzt rohe PDF-Regex-Treffer (`pdf_extraktion.py`) in Formular-
Vorschlagswerte für das editierbare Mietvertragsprofil-Review (Auftrag
HV-20260913-VERTRAGSANLAGE). Jeder Vorschlag bleibt EDITIERBAR und wird
NIE automatisch übernommen - siehe `backoffice/vertragsanlage_form.py`.

Nutzungsart wird NUR gesetzt, wenn GENAU EINER der drei Hinweismuster
(Wohnung/Büro/Geschäftslokal) trifft - bei Mehrdeutigkeit oder fehlendem
Treffer bleibt das Feld leer/UNGEKLAERT (keine Ableitung, keine
Rateentscheidung, siehe Auftrag: "keine Ableitung ... Büro=MRG-frei").

Ein Feld mit MEHREREN unterschiedlichen Fundstellen (`mehrdeutige_treffer`)
erhält KEINEN Formularvorschlag - stattdessen einen `Mehrdeutigkeit`-
Hinweis mit ALLEN Fundstellen, der im Formular sichtbar gemacht wird."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from mietinkasso.vertragsanlage.pdf_extraktion import ExtraktionsErgebnis, FeldTreffer


@dataclass(frozen=True)
class Vorschlag:
    formularwert: str
    seite: int
    auszug: str


@dataclass(frozen=True)
class Mehrdeutigkeit:
    funde: tuple[FeldTreffer, ...]


def _mietbeginn_iso(ddmmyyyy: str) -> str | None:
    try:
        tag, monat, jahr = ddmmyyyy.split(".")
        return date(int(jahr), int(monat), int(tag)).isoformat()
    except (ValueError, TypeError):
        return None


#: Direkte 1:1-Übernahmen von Extraktionsfeld -> Formularfeld (keine
#: weitere Umformung nötig).
_DIREKTE_FELDER = {
    "kaution_cent": "vertragliche_kaution_cent",
    "hauptmietzins_cent": "hauptmietzins_cent",
    "betriebskosten_cent": "betriebskosten_cent",
    "heizkosten_cent": "heizkosten_cent",
    "kueche_cent": "kueche_cent",
    "parkplatz_cent": "parkplatz_cent",
    "index_reihe": "index_reihe",
    "index_schwelle_prozent": "index_schwelle_prozent",
    "mieter_name": "mieter_name_hinweis",
    "vermieter_name": "vermieter_name_hinweis",
}


def vorschlaege_aus_extraktion(ergebnis: ExtraktionsErgebnis) -> dict[str, Vorschlag]:
    t = ergebnis.treffer
    vorschlaege: dict[str, Vorschlag] = {}

    for quellfeld, formularfeld in _DIREKTE_FELDER.items():
        if quellfeld in t:
            tr = t[quellfeld]
            vorschlaege[formularfeld] = Vorschlag(tr.wert.strip(), tr.seite, tr.auszug)

    if "mietbeginn" in t:
        tr = t["mietbeginn"]
        iso = _mietbeginn_iso(tr.wert)
        if iso is not None:
            vorschlaege["urspruenglicher_mietbeginn"] = Vorschlag(iso, tr.seite, tr.auszug)

    if "mietende" in t:
        tr = t["mietende"]
        iso = _mietbeginn_iso(tr.wert)
        if iso is not None:
            vorschlaege["gueltig_bis"] = Vorschlag(iso, tr.seite, tr.auszug)

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


#: Zuordnung Extraktionsfeld -> menschenlesbares Label für die
#: Mehrdeutigkeits-Anzeige im Formular.
_FELD_LABELS = {
    "kaution_cent": "Kaution",
    "hauptmietzins_cent": "Hauptmietzins",
    "betriebskosten_cent": "Betriebskosten",
    "heizkosten_cent": "Heizkosten",
    "kueche_cent": "Küche",
    "parkplatz_cent": "Parkplatz",
    "mahngebuehr_cent": "Mahngebühr",
    "index_reihe": "Indexreihe",
    "index_schwelle_prozent": "Index-Schwelle",
    "mietbeginn": "Mietbeginn",
    "mietende": "Mietende",
    "mieter_name": "Mieter (Name laut Dokument)",
    "vermieter_name": "Vermieter (Name laut Dokument)",
}


def mehrdeutigkeiten_aus_extraktion(ergebnis: ExtraktionsErgebnis) -> dict[str, Mehrdeutigkeit]:
    """Liefert für jedes mehrdeutig erkannte Feld ein anzeigefertiges
    Label + alle Fundstellen - für die "Mehrdeutigkeit"-Warnung im
    Review-Formular (siehe Moduldoc)."""

    return {
        _FELD_LABELS.get(feld, feld): Mehrdeutigkeit(tuple(funde))
        for feld, funde in ergebnis.mehrdeutige_treffer.items()
    }
