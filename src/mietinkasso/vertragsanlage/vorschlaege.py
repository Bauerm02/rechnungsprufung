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
    quellart: str = "TEXT"
    weitere_belege: tuple[FeldTreffer, ...] = ()


@dataclass(frozen=True)
class Mehrdeutigkeit:
    funde: tuple[FeldTreffer, ...]
    #: Parteien/Einheiten: mehrere Angaben können zugleich richtig sein. Sie
    #: bleiben als Liste erhalten, keine wird ausgewählt.
    ist_liste: bool = False


@dataclass(frozen=True)
class Quellenstand:
    """Vergleich des Quellhashes mit den bisherigen Profilversionen."""

    status: str  # ERSTE_QUELLE | WIEDERHOLUNG | NEUE_VERSION
    hinweis: str


def quellenstand(quelle_referenz: str, bisherige_referenzen: list[str | None]) -> Quellenstand:
    """Unveränderte Datei und neue Dokumentversion werden allein über den
    Inhaltshash unterschieden, nie über den Dateinamen. Reine Anzeige."""

    frueher = [r for r in bisherige_referenzen if r and r.startswith("pdf-sha256:")]
    kurz = quelle_referenz.removeprefix("pdf-sha256:")[:12]
    if not frueher:
        return Quellenstand("ERSTE_QUELLE", f"Erstes hinterlegtes PDF zu diesem Vertrag (Prüfsumme {kurz}…).")
    if quelle_referenz == frueher[-1]:
        return Quellenstand("WIEDERHOLUNG", f"Unveränderte Wiederholung der zuletzt hinterlegten Datei (Prüfsumme {kurz}…).")
    if quelle_referenz in frueher:
        return Quellenstand(
            "WIEDERHOLUNG", f"Identisch mit einer ÄLTEREN hinterlegten Datei, nicht mit der zuletzt verwendeten (Prüfsumme {kurz}…).",
        )
    return Quellenstand(
        "NEUE_VERSION",
        f"Neue Dokumentversion (Prüfsumme {kurz}…), weicht von der zuletzt hinterlegten Datei ab "
        f"(Prüfsumme {frueher[-1].removeprefix('pdf-sha256:')[:12]}…). Geänderte Angaben bitte prüfen.",
    )


def _aus_treffer(formularwert: str, tr: FeldTreffer) -> Vorschlag:
    return Vorschlag(formularwert, tr.seite, tr.auszug, tr.quellart, tr.weitere_belege)


def _genau_einer(ergebnis: ExtraktionsErgebnis, optionen: dict[str, str]) -> tuple[str, FeldTreffer] | None:
    """Tri-State: nur wenn GENAU EIN Hinweis trifft und keiner der Hinweise in
    sich widersprüchlich ist - sonst bleibt das Feld ungeklärt."""

    if any(feld in ergebnis.mehrdeutige_treffer for feld in optionen.values()):
        return None
    gefunden = [(wert, ergebnis.treffer[feld]) for wert, feld in optionen.items() if feld in ergebnis.treffer]
    return gefunden[0] if len(gefunden) == 1 else None


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
#: Abnahme-Folgefix (Codex, zweiter Praxistest): die Extraktionsmuster in
#: `pdf_extraktion.py` erkennen bei "Jänner"/"März" ausdrücklich auch die
#: ASCII-Transliteration ohne Umlaut ("Jaenner"/"Maerz", via
#: `J(?:ä|ae)nner`/`M(?:ä|ae)rz`) - diese Tabelle muss deshalb BEIDE
#: Schreibweisen kennen, sonst liefert `.get()` `None` für einen vom
#: Regex bereits korrekt erkannten Treffer (genau dieser stille
#: Fehltreffer verschluckte zuvor einen belegten Anpassungsmonat).
_MONATSNAMEN = {
    "jänner": 1, "jaenner": 1, "januar": 1, "februar": 2, "märz": 3, "maerz": 3,
    "april": 4, "mai": 5, "juni": 6,
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
    # Abnahme-Folgefix (Codex, "vertraglicher Fälligkeitstag"): belegter
    # Zahlungstermin ("bis zum X. des Monats") - bleibt editierbarer
    # Vorschlag, ersetzt NIE stillschweigend den technischen Default.
    "faelligkeit_tag": "faelligkeit_tag",
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
    "mieter_adresse": "mieter_adresse_hinweis",
    # Auftrag HV-20261020-VERTRAGSDATENBASIS: Mitmieter/Vertreter sind KEIN
    # Mietvertragsprofil-Feld - reiner Anzeigehinweis für die manuelle
    # Erfassung über "Partei hinzufügen" (siehe `backoffice/
    # vertragsanlage_form.py::_eckdaten_block`), NIE automatisch angelegt.
    "mitmieter_name": "mitmieter_name_hinweis",
    "mitmieter_telefon": "mitmieter_telefon_hinweis",
    "mitmieter_email": "mitmieter_email_hinweis",
    "einheit_bezeichnung": "einheit_hinweis",
}

_BEFRISTUNG = {"BEFRISTET": "befristet_hinweis", "UNBEFRISTET": "unbefristet_hinweis"}
_BK_HK = {"AKONTO_VORAUSZAHLUNG": "bk_hk_akonto_hinweis", "PAUSCHALE_INKLUSIVMIETE": "bk_hk_pauschale_hinweis"}
_NUTZUNGSART = {"WOHNUNG": "nutzungsart_wohnung", "BUERO": "nutzungsart_buero", "GESCHAEFTSLOKAL": "nutzungsart_geschaeft"}
_SCHWELLE = {"0": "index_schwelle_exklusiv_hinweis", "1": "index_schwelle_inklusiv_hinweis"}
_MRG = {"VOLL": "mrg_vollanwendung_hinweis", "TEIL": "mrg_teilanwendung_hinweis", "AUSNAHME": "mrg_ausnahme_hinweis"}
_MIETZINSBILDUNG = {
    "FREI": "mietzinsbildung_frei_hinweis", "RICHTWERT": "mietzinsbildung_richtwert_hinweis",
    "KATEGORIE": "mietzinsbildung_kategorie_hinweis",
}


def vorschlaege_aus_extraktion(ergebnis: ExtraktionsErgebnis) -> dict[str, Vorschlag]:
    t = ergebnis.treffer
    vorschlaege: dict[str, Vorschlag] = {}

    for quellfeld, formularfeld in _DIREKTE_FELDER.items():
        if quellfeld in t:
            tr = t[quellfeld]
            vorschlaege[formularfeld] = _aus_treffer(tr.wert.strip(), tr)

    if "mietbeginn" in t:
        tr = t["mietbeginn"]
        iso = _mietbeginn_iso(tr.wert)
        if iso is not None:
            vorschlaege["urspruenglicher_mietbeginn"] = _aus_treffer(iso, tr)

    if "mietende" in t:
        tr = t["mietende"]
        iso = _mietbeginn_iso(tr.wert)
        if iso is not None:
            vorschlaege["gueltig_bis"] = _aus_treffer(iso, tr)

    if "unterzeichnungsdatum" in t:
        tr = t["unterzeichnungsdatum"]
        iso = _mietbeginn_iso(tr.wert)
        if iso is not None:
            vorschlaege["unterzeichnungsdatum"] = _aus_treffer(iso, tr)

    if "uebergabe_am" in t:
        tr = t["uebergabe_am"]
        iso = _mietbeginn_iso(tr.wert)
        if iso is not None:
            vorschlaege["uebergabe_am"] = _aus_treffer(iso, tr)

    if "index_urspruenglicher_basismonat" in t:
        tr = t["index_urspruenglicher_basismonat"]
        iso_monat = _basismonat_iso(tr.wert)
        if iso_monat is not None:
            vorschlaege["index_urspruenglicher_basismonat"] = _aus_treffer(iso_monat, tr)

    # Tri-State wie `nutzungsart` oben: NUR bei GENAU EINEM der beiden
    # Hinweise ein Vorschlag - bei beiden/keinem bleibt das Feld
    # UNGEKLAERT (Auftrag Markus: "Leeres Ende darf NICHT automatisch
    # unbefristet sein" - dasselbe Vorsichtsprinzip gilt hier für die
    # Erkennung selbst, nicht nur für das leere `gueltig_bis`).
    for formularfeld, optionen in (
        ("befristungsstatus", _BEFRISTUNG), ("bk_hk_abrechnungsart", _BK_HK), ("nutzungsart", _NUTZUNGSART),
    ):
        eindeutig = _genau_einer(ergebnis, optionen)
        if eindeutig is not None:
            vorschlaege[formularfeld] = _aus_treffer(*eindeutig)
    # Mehrdeutig (mehrere Muster gleichzeitig) oder kein Treffer: bewusst
    # KEIN Vorschlag - das Formular bleibt bei UNGEKLAERT.

    # "Keine Mahngebühr" und ein Betrag widersprechen sich: kein Vorschlag,
    # sondern eine sichtbare Mehrdeutigkeit (`mehrdeutigkeiten_aus_extraktion`).
    betrag_genannt = "mahngebuehr_cent" in t or "mahngebuehr_cent" in ergebnis.mehrdeutige_treffer
    if "mahngebuehr_keine" in t and betrag_genannt:
        pass
    elif "mahngebuehr_keine" in t:
        tr = t["mahngebuehr_keine"]
        vorschlaege["mahngebuehr_cent"] = _aus_treffer("0", tr)
    elif "mahngebuehr_cent" in t:
        tr = t["mahngebuehr_cent"]
        vorschlaege["mahngebuehr_cent"] = _aus_treffer(tr.wert, tr)
    # sonst: kein Vorschlag - Feld bleibt leer -> None (unbekannt, KEIN erfundenes 0)

    # Abnahme-Folgefix (Codex, zweiter Praxistest): Brutto/Netto/USt je
    # Mietkomponente werden jetzt HIER, gemeinsam je Komponente,
    # aufgelöst - NICHT mehr über unabhängige 1:1-Einträge in
    # `_DIREKTE_FELDER`. Grund: liegt ein Netto-Treffer vor (aus
    # `pdf_extraktion.py::_netto_muster`, erkennt "netto" VOR oder NACH
    # dem Betrag), gewinnt der Brutto-Treffer für DIESELBE Komponente
    # sonst fälschlich (`komponenten_werte_aus_form`: "ein angegebenes
    # Brutto bleibt immer maßgeblich") - der Netto-Betrag würde dann
    # STILLSCHWEIGEND verworfen. Ein Brutto-Vorschlag wird deshalb NUR
    # übernommen, wenn für dieselbe Komponente KEIN Netto-Treffer vorliegt.
    for brutto_feld, netto_feld, ust_feld, formular_praefix in (
        ("hauptmietzins_cent", "hauptmietzins_netto_cent", "hauptmietzins_ust_prozent", "komponente_hmz"),
        ("betriebskosten_cent", "betriebskosten_netto_cent", "betriebskosten_ust_prozent", "komponente_bk"),
        ("heizkosten_cent", "heizkosten_netto_cent", "heizkosten_ust_prozent", "komponente_hk"),
        ("kueche_cent", "kueche_netto_cent", "kueche_ust_prozent", "komponente_kueche"),
        ("parkplatz_cent", "parkplatz_netto_cent", "parkplatz_ust_prozent", "komponente_parkplatz"),
    ):
        if netto_feld in t:
            tr = t[netto_feld]
            vorschlaege[f"{formular_praefix}_netto"] = _aus_treffer(tr.wert.strip(), tr)
            if ust_feld in t:
                tr_ust = t[ust_feld]
                # Abnahme-Folgefix: die Auswahlfelder im Review bieten nur
                # die festen Prozentsätze 0/10/13/20 an (`_UST_SAETZE_
                # PROZENT`) - ein erkannter Wert wird auf eine glatte
                # Ganzzahl normalisiert ("20,0" -> "20"), damit die
                # entsprechende Option tatsächlich vorausgewählt erscheint.
                vorschlaege[f"{formular_praefix}_ust"] = _aus_treffer(_prozent_normalisiert(tr_ust.wert), tr_ust)
        elif brutto_feld in t and netto_feld not in ergebnis.mehrdeutige_treffer:
            tr = t[brutto_feld]
            vorschlaege[formular_praefix] = _aus_treffer(tr.wert.strip(), tr)

    # Ein belegter Verzugszins-SATZ ist selbst der Beleg für
    # "vertraglich vereinbart" - ohne eigenes Fundmuster für diese
    # Einordnung würde das Auswahlfeld sonst bei UNGEKLAERT bleiben,
    # obwohl der Satz direkt daneben bereits als Vorschlag steht.
    if "verzugszins_satz_prozent" in t:
        tr = t["verzugszins_satz_prozent"]
        vorschlaege["verzugszins_art"] = _aus_treffer("VERTRAGLICH_VEREINBART", tr)

    if "index_anpassungsmonat_name" in t:
        tr = t["index_anpassungsmonat_name"]
        monat = _MONATSNAMEN.get(tr.wert.strip().lower())
        if monat is not None:
            vorschlaege["index_anpassungsmonat"] = _aus_treffer(str(monat), tr)

    # Tri-State wie `befristungsstatus`/`nutzungsart` oben: NUR bei GENAU
    # EINEM der beiden Hinweise ein Vorschlag - bei beiden/keinem bleibt
    # die Einordnung ungeklärt statt geraten.
    for formularfeld, optionen in (
        ("index_schwelle_inklusive", _SCHWELLE), ("rechtsachse_mrg_anwendungsbereich", _MRG),
        ("rechtsachse_mietzinsbildung", _MIETZINSBILDUNG),
    ):
        eindeutig = _genau_einer(ergebnis, optionen)
        if eindeutig is not None:
            vorschlaege[formularfeld] = _aus_treffer(*eindeutig)

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
    "einheit_bezeichnung": "Einheit (laut Dokument)",
    "hauptmietzins_netto_cent": "Hauptmietzins netto",
    "hauptmietzins_ust_prozent": "Hauptmietzins USt",
    "betriebskosten_netto_cent": "Betriebskosten netto",
    "betriebskosten_ust_prozent": "Betriebskosten USt",
    "heizkosten_netto_cent": "Heizkosten netto",
    "heizkosten_ust_prozent": "Heizkosten USt",
    "kueche_netto_cent": "Küche netto",
    "kueche_ust_prozent": "Küche USt",
    "parkplatz_netto_cent": "Parkplatz netto",
    "parkplatz_ust_prozent": "Parkplatz USt",
    "mieter_adresse": "Mieter (Anschrift laut Dokument)",
    "faelligkeit_tag": "Fälligkeitstag",
    "verzugszins_satz_prozent": "Verzugszinssatz",
}

#: Mehrere Angaben sind hier nicht zwingend ein Widerspruch, sondern eine Liste
#: - auch dann wird keine ausgewählt.
_LISTENFELDER = frozenset({
    "mieter_name", "mieter_email", "mieter_telefon", "mieter_adresse", "vermieter_name",
    "mitmieter_name", "mitmieter_telefon", "mitmieter_email", "einheit_bezeichnung",
})


def mehrdeutigkeiten_aus_extraktion(ergebnis: ExtraktionsErgebnis) -> dict[str, Mehrdeutigkeit]:
    """Liefert für jedes mehrdeutig erkannte Feld ein anzeigefertiges
    Label + alle Fundstellen - für die "Mehrdeutigkeit"-Warnung im
    Review-Formular (siehe Moduldoc)."""

    ergebnis_je_label = {
        _FELD_LABELS.get(feld, feld): Mehrdeutigkeit(tuple(funde), ist_liste=feld in _LISTENFELDER)
        for feld, funde in ergebnis.mehrdeutige_treffer.items()
    }
    t = ergebnis.treffer
    if "mahngebuehr_keine" in t:
        betraege = (t["mahngebuehr_cent"],) if "mahngebuehr_cent" in t else ()
        bisher = ergebnis_je_label.get("Mahngebühr")
        betraege += bisher.funde if bisher else ()
        if betraege:
            ergebnis_je_label["Mahngebühr"] = Mehrdeutigkeit((t["mahngebuehr_keine"], *betraege))
    for label, optionen in (
        ("Befristung", _BEFRISTUNG), ("BK/HK-Abrechnungsart", _BK_HK), ("Nutzungsart", _NUTZUNGSART),
        ("MRG-Anwendungsbereich", _MRG), ("Mietzinsbildung", _MIETZINSBILDUNG), ("Index-Schwelle (Einordnung)", _SCHWELLE),
    ):
        gefunden = [t[feld] for feld in optionen.values() if feld in t]
        if len(gefunden) > 1:
            ergebnis_je_label[label] = Mehrdeutigkeit(tuple(gefunden))
    return ergebnis_je_label
