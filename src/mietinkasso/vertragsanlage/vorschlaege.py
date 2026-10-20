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
from decimal import Decimal, InvalidOperation

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


#: Abnahme-Folgefix (Codex 20.10.2026): "ausdrücklich genannter
#: Indexbasiswert wird nicht extrahiert" - deutsche Monatsnamen (inkl.
#: österreichischer Schreibweise "Jänner") für die Übersetzung eines im
#: Vertrag genannten Basismonats in das vom `<input type="month">`
#: erwartete `YYYY-MM`-Format.
_MONATSNAMEN = {
    "jänner": 1, "januar": 1, "februar": 2, "märz": 3, "april": 4, "mai": 5, "juni": 6,
    "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11, "dezember": 12,
}


def _basismonat_iso(roh: str) -> str | None:
    """`roh` ist entweder "<Monatsname> <Jahr>" oder "MM/YYYY"/"MM.YYYY"
    (siehe `pdf_extraktion.py::_MUSTER['index_urspruenglicher_basismonat']`).
    Liefert `None` statt zu raten, wenn das Format nicht eindeutig ist."""

    roh = roh.strip()
    teile = roh.replace(".", "/").split("/") if ("/" in roh or "." in roh) else roh.split()
    if len(teile) != 2:
        return None
    monat_teil, jahr_teil = teile
    try:
        jahr = int(jahr_teil)
    except ValueError:
        return None
    if monat_teil.isdigit():
        monat = int(monat_teil)
    else:
        monat = _MONATSNAMEN.get(monat_teil.lower())
    if monat is None or not (1 <= monat <= 12):
        return None
    return f"{jahr:04d}-{monat:02d}"


#: Direkte 1:1-Übernahmen von Extraktionsfeld -> Formularfeld (keine
#: weitere Umformung nötig).
#:
#: **"nettomiete_cent" ist ABSICHTLICH NICHT hier enthalten** (Korrektur
#: nach unabhängiger Abnahme, Codex 20.10.2026): `hauptmietzins_cent` ist
#: im System per Definition BRUTTO (siehe `pdf_extraktion.py`-Kommentar)
#: - eine als "Nettomiete" bezeichnete Fundstelle wird deshalb NIE in
#: dieses Brutto-Feld gemappt, sondern unten als eigener, klar als NETTO
#: beschrifteter Hinweis (`nettomiete_hinweis_cent`) geführt, den ein
#: Mensch bewusst gegen das Brutto-Feld abgleichen muss.
_DIREKTE_FELDER = {
    "kaution_cent": "vertragliche_kaution_cent",
    # Abnahme-Folgefix (Codex, "Netto/USt/Brutto je Mietkomponente"): die
    # PDF-erkannten Brutto-Beträge je Mietbestandteil mappen jetzt DIREKT
    # auf die tatsächlich im Review gerenderten `komponente_*`-Felder
    # (`vertragsanlage_form.py::_komponente_felder_html`) statt auf
    # tote, nirgends gerenderte Formularnamen.
    "hauptmietzins_cent": "komponente_hmz",
    "betriebskosten_cent": "komponente_bk",
    "heizkosten_cent": "komponente_hk",
    "kueche_cent": "komponente_kueche",
    "parkplatz_cent": "komponente_parkplatz",
    "hauptmietzins_netto_cent": "komponente_hmz_netto",
    "betriebskosten_netto_cent": "komponente_bk_netto",
    "heizkosten_netto_cent": "komponente_hk_netto",
    "kueche_netto_cent": "komponente_kueche_netto",
    "parkplatz_netto_cent": "komponente_parkplatz_netto",
    "index_reihe": "index_reihe",
    "index_schwelle_prozent": "index_schwelle_prozent",
    "index_urspruenglicher_basiswert": "index_urspruenglicher_basiswert",
    "index_mindestintervall_monate": "index_mindestintervall_monate",
    "verzugszins_satz_prozent": "verzugszins_satz_prozent",
    "mieter_name": "mieter_name_hinweis",
    "vermieter_name": "vermieter_name_hinweis",
    "nettomiete_cent": "nettomiete_hinweis_cent",
    # Abnahme-Folgefix (Codex 20.10.2026): "explizite Mieter-E-Mail/
    # -Telefon werden gar nicht extrahiert" - ANDERS als beim bloßen
    # Namensabgleich (`mieter_name_hinweis`, für einen BEREITS gewählten
    # bestehenden Debitor) werden diese beiden Hinweise, wenn gerade ein
    # NEUER Mieter angelegt wird, direkt zum editierbaren Formularvorschlag
    # für dessen Kontaktfelder (siehe `routes/vertragsanlage.py::
    # vertragsanlage_pdf_hochladen`) - niemals für Vermieterkontakte
    # (die werden bewusst gar nicht erst extrahiert, siehe `pdf_extraktion.
    # py`).
    "mieter_email": "mieter_email_hinweis",
    "mieter_telefon": "mieter_telefon_hinweis",
    # Auftrag HV-20261020-VERTRAGSDATENBASIS: Mitmieter/Vertreter sind KEIN
    # Mietvertragsprofil-Feld - reiner Anzeigehinweis für die manuelle
    # Erfassung über "Partei hinzufügen" (siehe `backoffice/
    # vertragsanlage_form.py::_eckdaten_block`), NIE automatisch angelegt.
    "mitmieter_name": "mitmieter_name_hinweis",
    "mitmieter_telefon": "mitmieter_telefon_hinweis",
    "mitmieter_email": "mitmieter_email_hinweis",
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

    if "unterzeichnungsdatum" in t:
        tr = t["unterzeichnungsdatum"]
        iso = _mietbeginn_iso(tr.wert)
        if iso is not None:
            vorschlaege["unterzeichnungsdatum"] = Vorschlag(iso, tr.seite, tr.auszug)

    if "uebergabe_am" in t:
        tr = t["uebergabe_am"]
        iso = _mietbeginn_iso(tr.wert)
        if iso is not None:
            vorschlaege["uebergabe_am"] = Vorschlag(iso, tr.seite, tr.auszug)

    if "index_urspruenglicher_basismonat" in t:
        tr = t["index_urspruenglicher_basismonat"]
        iso_monat = _basismonat_iso(tr.wert)
        if iso_monat is not None:
            vorschlaege["index_urspruenglicher_basismonat"] = Vorschlag(iso_monat, tr.seite, tr.auszug)

    # Tri-State wie `nutzungsart` oben: NUR bei GENAU EINEM der beiden
    # Hinweise ein Vorschlag - bei beiden/keinem bleibt das Feld
    # UNGEKLAERT (Auftrag Markus: "Leeres Ende darf NICHT automatisch
    # unbefristet sein" - dasselbe Vorsichtsprinzip gilt hier für die
    # Erkennung selbst, nicht nur für das leere `gueltig_bis`).
    befristungsstatus_treffer = {
        "BEFRISTET": t.get("befristet_hinweis"),
        "UNBEFRISTET": t.get("unbefristet_hinweis"),
    }
    gefundene_befristung = {k: v for k, v in befristungsstatus_treffer.items() if v is not None}
    if len(gefundene_befristung) == 1:
        (status, tr), = gefundene_befristung.items()
        vorschlaege["befristungsstatus"] = Vorschlag(status, tr.seite, tr.auszug)

    bk_hk_treffer = {
        "AKONTO_VORAUSZAHLUNG": t.get("bk_hk_akonto_hinweis"),
        "PAUSCHALE_INKLUSIVMIETE": t.get("bk_hk_pauschale_hinweis"),
    }
    gefundene_bk_hk = {k: v for k, v in bk_hk_treffer.items() if v is not None}
    if len(gefundene_bk_hk) == 1:
        (abrechnungsart, tr), = gefundene_bk_hk.items()
        vorschlaege["bk_hk_abrechnungsart"] = Vorschlag(abrechnungsart, tr.seite, tr.auszug)

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

    # Abnahme-Folgefix: USt-Satz je Mietkomponente - die Auswahlfelder im
    # Review bieten nur die festen Prozentsätze 0/10/13/20 an (siehe
    # `_UST_SAETZE_PROZENT`); ein erkannter Wert wird dafür auf eine
    # glatte Ganzzahl normalisiert ("20,0" -> "20"), damit die
    # entsprechende Option tatsächlich vorausgewählt erscheint.
    for quellfeld, formularfeld in (
        ("hauptmietzins_ust_prozent", "komponente_hmz_ust"),
        ("betriebskosten_ust_prozent", "komponente_bk_ust"),
        ("heizkosten_ust_prozent", "komponente_hk_ust"),
        ("kueche_ust_prozent", "komponente_kueche_ust"),
        ("parkplatz_ust_prozent", "komponente_parkplatz_ust"),
    ):
        if quellfeld in t:
            tr = t[quellfeld]
            vorschlaege[formularfeld] = Vorschlag(_prozent_normalisiert(tr.wert), tr.seite, tr.auszug)

    # Ein belegter Verzugszins-SATZ ist selbst der Beleg für
    # "vertraglich vereinbart" - ohne eigenes Fundmuster für diese
    # Einordnung würde das Auswahlfeld sonst bei UNGEKLAERT bleiben,
    # obwohl der Satz direkt daneben bereits als Vorschlag steht.
    if "verzugszins_satz_prozent" in t:
        tr = t["verzugszins_satz_prozent"]
        vorschlaege["verzugszins_art"] = Vorschlag("VERTRAGLICH_VEREINBART", tr.seite, tr.auszug)

    if "index_anpassungsmonat_name" in t:
        tr = t["index_anpassungsmonat_name"]
        monat = _MONATSNAMEN.get(tr.wert.strip().lower())
        if monat is not None:
            vorschlaege["index_anpassungsmonat"] = Vorschlag(str(monat), tr.seite, tr.auszug)

    # Tri-State wie `befristungsstatus`/`nutzungsart` oben: NUR bei GENAU
    # EINEM der beiden Hinweise ein Vorschlag - bei beiden/keinem bleibt
    # die Einordnung ungeklärt statt geraten.
    schwelle_treffer = {
        "0": t.get("index_schwelle_exklusiv_hinweis"),
        "1": t.get("index_schwelle_inklusiv_hinweis"),
    }
    gefundene_schwelle = {k: v for k, v in schwelle_treffer.items() if v is not None}
    if len(gefundene_schwelle) == 1:
        (inklusive, tr), = gefundene_schwelle.items()
        vorschlaege["index_schwelle_inklusive"] = Vorschlag(inklusive, tr.seite, tr.auszug)

    mrg_treffer = {
        "VOLL": t.get("mrg_vollanwendung_hinweis"),
        "TEIL": t.get("mrg_teilanwendung_hinweis"),
        "AUSNAHME": t.get("mrg_ausnahme_hinweis"),
    }
    gefundene_mrg = {k: v for k, v in mrg_treffer.items() if v is not None}
    if len(gefundene_mrg) == 1:
        (anwendungsbereich, tr), = gefundene_mrg.items()
        vorschlaege["rechtsachse_mrg_anwendungsbereich"] = Vorschlag(anwendungsbereich, tr.seite, tr.auszug)

    mietzinsbildung_treffer = {
        "FREI": t.get("mietzinsbildung_frei_hinweis"),
        "RICHTWERT": t.get("mietzinsbildung_richtwert_hinweis"),
        "KATEGORIE": t.get("mietzinsbildung_kategorie_hinweis"),
    }
    gefundene_mietzinsbildung = {k: v for k, v in mietzinsbildung_treffer.items() if v is not None}
    if len(gefundene_mietzinsbildung) == 1:
        (mietzinsbildung, tr), = gefundene_mietzinsbildung.items()
        vorschlaege["rechtsachse_mietzinsbildung"] = Vorschlag(mietzinsbildung, tr.seite, tr.auszug)

    return vorschlaege


def _prozent_normalisiert(roh: str) -> str:
    try:
        wert = Decimal(roh.strip().replace(",", "."))
    except InvalidOperation:
        return roh.strip()
    return str(int(wert)) if wert == wert.to_integral_value() else str(wert)


#: Zuordnung Extraktionsfeld -> menschenlesbares Label für die
#: Mehrdeutigkeits-Anzeige im Formular.
_FELD_LABELS = {
    "kaution_cent": "Kaution",
    "hauptmietzins_cent": "Hauptmietzins",
    "nettomiete_cent": "Nettomiete (NICHT automatisch Brutto)",
    "betriebskosten_cent": "Betriebskosten",
    "heizkosten_cent": "Heizkosten",
    "kueche_cent": "Küche",
    "parkplatz_cent": "Parkplatz",
    "mahngebuehr_cent": "Mahngebühr",
    "index_reihe": "Indexreihe",
    "index_schwelle_prozent": "Index-Schwelle",
    "index_urspruenglicher_basismonat": "Index-Basismonat",
    "index_urspruenglicher_basiswert": "Index-Basiswert",
    "mietbeginn": "Mietbeginn",
    "mietende": "Mietende",
    "unterzeichnungsdatum": "Unterzeichnungsdatum",
    "uebergabe_am": "Übergabedatum",
    "mieter_name": "Mieter (Name laut Dokument)",
    "mieter_email": "Mieter (E-Mail laut Dokument)",
    "mieter_telefon": "Mieter (Telefon laut Dokument)",
    "vermieter_name": "Vermieter (Name laut Dokument)",
    "mitmieter_name": "Mitmieter (Name laut Dokument)",
    "mitmieter_telefon": "Mitmieter (Telefon laut Dokument)",
    "mitmieter_email": "Mitmieter (E-Mail laut Dokument)",
}


def mehrdeutigkeiten_aus_extraktion(ergebnis: ExtraktionsErgebnis) -> dict[str, Mehrdeutigkeit]:
    """Liefert für jedes mehrdeutig erkannte Feld ein anzeigefertiges
    Label + alle Fundstellen - für die "Mehrdeutigkeit"-Warnung im
    Review-Formular (siehe Moduldoc)."""

    return {
        _FELD_LABELS.get(feld, feld): Mehrdeutigkeit(tuple(funde))
        for feld, funde in ergebnis.mehrdeutige_treffer.items()
    }
