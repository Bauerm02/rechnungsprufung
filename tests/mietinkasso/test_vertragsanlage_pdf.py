"""Tests für die lokale PDF-Textextraktion und Ablage (Auftrag
HV-20260913-VERTRAGSANLAGE). Alle PDFs sind synthetisch von Hand gebaut
(`_pdf_test_helpers.py`), keine echten Vertragsdokumente."""

from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path

import pytest

from mietinkasso.vertragsanlage.ablage import UploadAbgelehntError, lesen, speichern
from mietinkasso.vertragsanlage.pdf_extraktion import PdfNichtLesbarError, _betrag_in_cent, extrahiere
from mietinkasso.vertragsanlage.vorschlaege import mehrdeutigkeiten_aus_extraktion, quellenstand, vorschlaege_aus_extraktion
from tests.mietinkasso._pdf_test_helpers import build_form_pdf, build_scan_pdf, build_text_pdf


def test_textlage_wird_erkannt_und_felder_extrahiert():
    pdf = build_text_pdf([
        "Wohnungsmietvertrag",
        "Mietbeginn: 01.06.2015",
        "Kaution: 1.500,00 EUR",
        "VPI 2020 Basiswert",
        "Erhöhung übersteigt um 5,0 % Prozent",
    ])
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert ergebnis.hat_textlage
    assert ergebnis.seiten_anzahl == 1
    assert ergebnis.treffer["mietbeginn"].wert == "01.06.2015"
    assert ergebnis.treffer["mietbeginn"].seite == 1
    assert "Mietbeginn" in ergebnis.treffer["mietbeginn"].auszug
    assert ergebnis.treffer["kaution_cent"].wert == "1.500,00"
    assert ergebnis.treffer["nutzungsart_wohnung"].wert == "Wohnungsmietvertrag"
    assert not ergebnis.warnungen


def test_gescanntes_dokument_ohne_textlage_liefert_keine_treffer_und_warnung():
    pdf = build_scan_pdf()
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert not ergebnis.hat_textlage
    assert ergebnis.treffer == {}
    assert any("Textlayer" in w for w in ergebnis.warnungen)


def test_seitenlimit_wird_angewendet_und_gemeldet():
    pdf = build_text_pdf(["Wohnungsmietvertrag"])
    ergebnis = extrahiere(pdf, max_seiten=0)
    # 0 Seiten ausgewertet -> keine Textlage feststellbar (bewusst
    # konservativ: lieber "kein Fund" als eine vorgetäuschte Auswertung).
    assert ergebnis.ausgewertete_seiten == 0
    assert not ergebnis.hat_textlage


def test_kaputte_datei_wird_als_nicht_lesbar_erkannt():
    with pytest.raises(PdfNichtLesbarError):
        extrahiere(b"%PDF-1.4\noffensichtlich kein valides PDF danach", max_seiten=5)


def test_mehrdeutige_nutzungsart_erzeugt_keinen_vorschlag_kein_raten():
    """Zwei widersprüchliche Hinweismuster im selben Dokument (z. B. ein
    Mischformular oder eine falsch erkannte Passage) dürfen NIE zu einer
    geratenen Nutzungsart führen."""

    pdf = build_text_pdf(["Wohnungsmietvertrag", "als Büro vermietet"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert "nutzungsart" not in vorschlaege


def test_eindeutige_nutzungsart_wird_vorgeschlagen_mit_beleg():
    pdf = build_text_pdf(["Geschäftsraummietvertrag über das Lokal Top 1"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert vorschlaege["nutzungsart"].formularwert == "GESCHAEFTSLOKAL"
    assert vorschlaege["nutzungsart"].seite == 1


def test_keine_mahngebuehr_klausel_wird_als_explizite_null_erkannt():
    pdf = build_text_pdf(["Es werden keine Mahnspesen verrechnet."])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert vorschlaege["mahngebuehr_cent"].formularwert == "0"


def test_fehlender_mahngebuehr_hinweis_erzeugt_keinen_vorschlag():
    """Kein Fund => kein Vorschlag => das Formularfeld bleibt leer => im
    übernommenen Datensatz `None` (nie eine erfundene 0)."""

    pdf = build_text_pdf(["Ein Vertrag ohne jede Erwähnung von Mahngebühren."])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert "mahngebuehr_cent" not in vorschlaege


def test_ablage_speichert_unter_inhaltsbasiertem_namen_und_ist_wiederholimport_sicher(tmp_path: Path):
    pdf = build_text_pdf(["Testvertrag"])
    verzeichnis = str(tmp_path / "uploads")
    doc1 = speichern(pdf, konfiguriertes_verzeichnis=verzeichnis, max_bytes=10_000_000)
    doc2 = speichern(pdf, konfiguriertes_verzeichnis=verzeichnis, max_bytes=10_000_000)
    assert doc1.ablage_id == doc2.ablage_id  # identischer Inhalt -> keine zweite Datei
    zurueckgelesen = lesen(doc1.ablage_id, konfiguriertes_verzeichnis=verzeichnis)
    assert zurueckgelesen == pdf
    assert len(list((tmp_path / "uploads").glob("*.pdf"))) == 1


def test_ablage_lehnt_ueberschreitung_der_groesse_ab(tmp_path: Path):
    with pytest.raises(UploadAbgelehntError):
        speichern(b"%PDF-1.4" + b"x" * 100, konfiguriertes_verzeichnis=str(tmp_path), max_bytes=10)


def test_ablage_lehnt_nicht_pdf_dateien_ab(tmp_path: Path):
    with pytest.raises(UploadAbgelehntError):
        speichern(b"<html><script>alert(1)</script></html>", konfiguriertes_verzeichnis=str(tmp_path), max_bytes=10_000)


def test_ablage_ohne_konfiguriertes_verzeichnis_bleibt_blockiert():
    with pytest.raises(UploadAbgelehntError):
        speichern(build_text_pdf(["x"]), konfiguriertes_verzeichnis=None, max_bytes=10_000_000)


def test_ablage_lesen_lehnt_pfadmanipulation_ab(tmp_path: Path):
    with pytest.raises(UploadAbgelehntError):
        lesen("../../../etc/passwd", konfiguriertes_verzeichnis=str(tmp_path))
    with pytest.raises(UploadAbgelehntError):
        lesen("<script>alert(1)</script>", konfiguriertes_verzeichnis=str(tmp_path))


# -- Rückprüfung (unabhängige Abnahme): EUR-vor-Betrag + Mehrdeutigkeit ------


def test_betrag_mit_waehrung_vor_der_zahl_wird_erkannt():
    """Reproduktion: 'Kaution in Höhe von EUR 1.500,00 gar nicht
    extrahiert.' Das bisherige Muster verlangte den Betrag VOR der
    Währung; deutschsprachige Verträge schreiben oft 'EUR 1.500,00'."""

    pdf = build_text_pdf(["Kaution in Höhe von EUR 1.500,00"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert ergebnis.treffer["kaution_cent"].wert == "1.500,00"


def test_mahnspesen_mit_euro_symbol_vor_betrag_wird_erkannt():
    pdf = build_text_pdf(["Mustermietvertrag", "Mahnspesen €20"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert ergebnis.treffer["mahngebuehr_cent"].wert == "20"


def test_widersprechende_kautionsbetraege_ergeben_mehrdeutigkeit_kein_vorschlag():
    """Reproduktion: 'PDF Kaution 1.500 EUR + Nachtrag Kaution 2.000 EUR
    ergibt 1.500 ohne Warnung.' Zwei unterschiedliche Beträge für
    dasselbe Feld dürfen NIE zu einem einzelnen, unkommentierten
    Vorschlag führen - beide Fundstellen müssen sichtbar werden."""

    pdf = build_text_pdf([
        "Kaution: 1.500,00 EUR",
        "Nachtrag zum Mietvertrag",
        "Kaution: 2.000,00 EUR",
    ])
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert "kaution_cent" not in ergebnis.treffer
    assert "kaution_cent" in ergebnis.mehrdeutige_treffer
    werte = {f.wert for f in ergebnis.mehrdeutige_treffer["kaution_cent"]}
    assert werte == {"1.500,00", "2.000,00"}

    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert "vertragliche_kaution_cent" not in vorschlaege  # kein automatischer Vorschlag bei Mehrdeutigkeit

    mehrdeutigkeiten = mehrdeutigkeiten_aus_extraktion(ergebnis)
    assert "Kaution" in mehrdeutigkeiten
    assert len(mehrdeutigkeiten["Kaution"].funde) == 2


def test_wiederholter_identischer_betrag_bleibt_eindeutig():
    """Zwei Fundstellen mit demselben Betrag (z. B. Kaution einmal in der
    Präambel, einmal in der Zusammenfassung) sind KEINE Mehrdeutigkeit."""

    pdf = build_text_pdf(["Kaution: 1.500,00 EUR", "Zusammenfassung: Kaution 1.500,00 EUR"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert ergebnis.treffer["kaution_cent"].wert == "1.500,00"
    assert "kaution_cent" not in ergebnis.mehrdeutige_treffer


def test_mieter_und_vermieter_namen_werden_als_hinweis_extrahiert():
    pdf = build_text_pdf(["Vermieter: JLB Projects GmbH", "Mieter: Erika Musterfrau"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert vorschlaege["mieter_name_hinweis"].formularwert == "Erika Musterfrau"
    assert vorschlaege["vermieter_name_hinweis"].formularwert == "JLB Projects GmbH"


def test_mitmieter_name_und_telefon_werden_als_hinweis_extrahiert_nicht_automatisch_angelegt():
    """Auftrag HV-20261020-VERTRAGSDATENBASIS: ein Mitmieter im Dokument
    wird NUR als geprüfter Hinweis mit Seitenbeleg ausgewiesen - die
    tatsächliche Partei entsteht erst über die manuelle "Partei
    hinzufügen"-Erfassung in der Mieterakte, niemals automatisch hier."""

    pdf = build_text_pdf(["Mieter: Max Mustermann", "Mitmieter: Erika Musterfrau Tel: 0664 1234567"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert vorschlaege["mitmieter_name_hinweis"].formularwert == "Erika Musterfrau"
    assert vorschlaege["mitmieter_telefon_hinweis"].formularwert == "0664 1234567"
    # Kein übernehmbares Mietvertragsprofil-Feld entsteht daraus.
    assert "mitmieter_name" not in vorschlaege
    assert "mitmieter_telefon" not in vorschlaege


def test_mitmieterin_ohne_telefon_wird_erkannt():
    pdf = build_text_pdf(["weitere Mieterin: Anna Beispiel"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert vorschlaege["mitmieter_name_hinweis"].formularwert == "Anna Beispiel"
    assert "mitmieter_telefon_hinweis" not in vorschlaege


def test_mietbestandteile_werden_getrennt_extrahiert():
    pdf = build_text_pdf([
        "Hauptmietzins: 500,00 EUR", "Betriebskosten: 80,00 EUR",
        "Heizkosten: 40,00 EUR", "Küche: 30,00 EUR", "Parkplatz: 50,00 EUR",
    ])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    # Abnahme-Folgefix: die Brutto-Vorschläge mappen jetzt DIREKT auf die
    # tatsächlich im Review gerenderten `komponente_*`-Felder statt auf
    # tote, nirgends gerenderte Formularnamen.
    assert vorschlaege["komponente_hmz"].formularwert == "500,00"
    assert vorschlaege["komponente_bk"].formularwert == "80,00"
    assert vorschlaege["komponente_hk"].formularwert == "40,00"
    assert vorschlaege["komponente_kueche"].formularwert == "30,00"
    assert vorschlaege["komponente_parkplatz"].formularwert == "50,00"


def test_mietende_wird_extrahiert():
    pdf = build_text_pdf(["Mietende: 31.05.2030"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert vorschlaege["gueltig_bis"].formularwert == "2030-05-31"


# -- Korrektur nach unabhängiger Abnahme (Codex, 20.10.2026): -------------
# -- _BETRAG_FRAGMENT truncation + Mahngebühr-Synonym + Nettomiete/Brutto -


@pytest.mark.parametrize(
    "roh,erwartet",
    [
        ("1200", "1200"),
        ("1200,00", "1200,00"),
        ("1.200,00", "1.200,00"),
        ("12345,67", "12345,67"),
    ],
)
def test_betragsformate_werden_vollstaendig_erkannt_nicht_truncated(roh, erwartet):
    """Reproduktion (Codex-Abnahme 20.10.2026): `_BETRAG_FRAGMENT` erlaubte
    bisher nur 1-3 Ziffern vor einem Tausender-/Dezimaltrenner OHNE eine
    nachfolgende Zifferngrenze zu verlangen - eine unformatierte Zahl
    wurde dadurch nach den ersten 1-3 Ziffern abgeschnitten ("1200" ->
    "120", "1200,00" -> "120", "12345,67" -> "123"). Jedes hier gelistete
    Format muss jetzt VOLLSTÄNDIG und UNVERÄNDERT erkannt werden - andere,
    bereits korrekte Formate dürfen dabei NICHT umgedeutet werden."""

    pdf = build_text_pdf([f"Hauptmietzins: {roh} EUR"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert ergebnis.treffer["hauptmietzins_cent"].wert == erwartet


def test_betrag_ohne_trennzeichen_direkt_nach_waehrung_wird_nicht_abgeschnitten():
    """Dasselbe Grenzfallbeispiel, aber mit der Währung VOR der Zahl ohne
    Leerzeichen ("EUR1200") - exakt der vom Auftraggeber genannte Fall."""

    pdf = build_text_pdf(["Hauptmietzins EUR1200"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert ergebnis.treffer["hauptmietzins_cent"].wert == "1200"


@pytest.mark.parametrize("text", [
    "Hauptmietzins: EUR1234.5678",       # Betrag nach Währung, Punkt-Dezimal, 4 Nachkommastellen
    "Hauptmietzins: 1200,0005 EUR",      # Betrag vor Währung, Komma-Dezimal, 4 Nachkommastellen
    "Hauptmietzins: 1234,5678 EUR",      # dieselbe Rückwärtsalternative mit Komma
    "Hauptmietzins: EUR1200.0005",       # dieselbe Vorwärtsalternative mit Punkt
])
def test_ueberschuessige_dezimalstellen_werden_komplett_verworfen_nicht_teilweise_uebernommen(text):
    """Reproduktion (Codex-Abnahme 20.10.2026): 'EUR1234.5678' wurde zu
    '1234.567', 'EUR1200,0005' zu '1200,000' - beide Male ein
    stillschweigend TEILWEISE korrekter, aber inhaltlich FALSCHER
    Betrag. Eine nicht unterstützte Präzision (mehr als 2 Nachkomma-
    stellen, keine gültige 3er-Tausendergruppe) muss die GESAMTE Zahl
    an dieser Stelle unerkannt lassen (kein Fund), niemals einen
    verkürzten Suffix als gültigen Betrag liefern - geprüft für BEIDE
    Alternativen (Betrag vor/nach Währung) und beide Dezimaltrennzeichen."""

    pdf = build_text_pdf([text])
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert "hauptmietzins_cent" not in ergebnis.treffer
    assert "hauptmietzins_cent" not in ergebnis.mehrdeutige_treffer


def test_mehrdeutiges_format_bleibt_als_mehrdeutigkeit_sichtbar_nach_der_korrektur():
    """Die Korrektur darf eine ECHTE (semantische) Mehrdeutigkeit -
    unterschiedliche Beträge für dasselbe Feld - nicht verdecken; sie
    behebt nur die Truncation einzelner, für sich genommen eindeutiger
    Zahlen."""

    pdf = build_text_pdf(["Hauptmietzins: 1200 EUR", "Hauptmietzins: 1300 EUR"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert "hauptmietzins_cent" not in ergebnis.treffer
    werte = {f.wert for f in ergebnis.mehrdeutige_treffer["hauptmietzins_cent"]}
    assert werte == {"1200", "1300"}


def test_mahngebuehr_synonym_ohne_spesen_wortlaut_wird_erkannt():
    """Reproduktion (Codex-Abnahme 20.10.2026): nur 'Mahnspesen' wurde
    erkannt, 'Mahngebühr' (ohne 'Spesen'-Wortlaut) nicht - beide Begriffe
    bezeichnen dieselbe Fachregel."""

    pdf = build_text_pdf(["Mahngebühr: 20,00 EUR"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert ergebnis.treffer["mahngebuehr_cent"].wert == "20,00"


def test_mahngebuehr_plural_wird_erkannt():
    pdf = build_text_pdf(["Es werden Mahngebühren in Höhe von EUR 15,00 verrechnet."])
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert ergebnis.treffer["mahngebuehr_cent"].wert == "15,00"


def test_keine_mahngebuehr_klausel_mit_pluralwortlaut_wird_erkannt():
    pdf = build_text_pdf(["Es werden keine Mahngebühren verrechnet."])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert vorschlaege["mahngebuehr_cent"].formularwert == "0"


def test_nettomiete_wird_nicht_als_hauptmietzins_brutto_uebernommen():
    """Auftrag Markus (Korrektur 20.10.2026): 'Nettomiete niemals still
    als Brutto übernehmen' - `hauptmietzins_cent` ist im System per
    Definition BRUTTO (siehe `backoffice/vertragsanlage_form.py`,
    Label "Hauptmietzins (brutto, EUR)"); eine ausdrücklich als NETTO
    bezeichnete Angabe darf NIE in dieses Feld einfließen."""

    pdf = build_text_pdf(["Nettomiete: 500,00 EUR"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert "hauptmietzins_cent" not in ergebnis.treffer
    assert ergebnis.treffer["nettomiete_cent"].wert == "500,00"

    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert "hauptmietzins_cent" not in vorschlaege  # NIE automatisch als Brutto übernommen
    assert vorschlaege["nettomiete_hinweis_cent"].formularwert == "500,00"


def test_hauptmietzins_und_nettomiete_nebeneinander_bleiben_getrennte_felder():
    """Nennt ein Vertrag BEIDE Begriffe (z. B. 'Hauptmietzins (brutto)
    550,00 EUR' und separat 'Nettomiete 500,00 EUR'), dürfen sie NICHT
    in dasselbe Extraktionsfeld vermischt werden - das würde sonst als
    Mehrdeutigkeit zwischen zwei eigentlich unterschiedlichen Größen
    (netto vs. brutto) fehlinterpretiert."""

    pdf = build_text_pdf(["Hauptmietzins: 550,00 EUR", "Nettomiete: 500,00 EUR"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert ergebnis.treffer["hauptmietzins_cent"].wert == "550,00"
    assert ergebnis.treffer["nettomiete_cent"].wert == "500,00"
    assert "hauptmietzins_cent" not in ergebnis.mehrdeutige_treffer


# -- Auftrag HV-20261020-VERTRAGSDATENBASIS: Termine/Befristung/BK-HK ----


def test_unterzeichnungsdatum_und_uebergabe_getrennt_von_mietbeginn_extrahiert():
    pdf = build_text_pdf([
        "Dieser Vertrag wurde unterzeichnet am 15.05.2015.",
        "Mietbeginn: 01.06.2015", "Übergeben am 02.06.2015",
    ])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert vorschlaege["unterzeichnungsdatum"].formularwert == "2015-05-15"
    assert vorschlaege["uebergabe_am"].formularwert == "2015-06-02"
    assert vorschlaege["urspruenglicher_mietbeginn"].formularwert == "2015-06-01"


def test_befristungsstatus_befristet_eindeutig_erkannt():
    pdf = build_text_pdf(["Dies ist ein befristeter Mietvertrag auf 5 Jahre."])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert vorschlaege["befristungsstatus"].formularwert == "BEFRISTET"


def test_befristungsstatus_unbefristet_eindeutig_erkannt():
    pdf = build_text_pdf(["Dies ist ein unbefristeter Mietvertrag."])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert vorschlaege["befristungsstatus"].formularwert == "UNBEFRISTET"


def test_befristungsstatus_mehrdeutig_bleibt_unbesetzt():
    """Enthält ein Dokument BEIDE Formulierungen (Mischtext/Nachtrag),
    darf das Feld NICHT geraten werden - bleibt UNGEKLAERT."""

    pdf = build_text_pdf(["Ursprünglich unbefristeter Mietvertrag.", "Nachtrag: befristeter Mietvertrag auf 3 Jahre."])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert "befristungsstatus" not in vorschlaege


def test_leeres_mietende_setzt_befristungsstatus_nicht_automatisch_auf_unbefristet():
    """Auftrag Markus: 'Leeres Ende darf NICHT automatisch unbefristet
    sein' - ein Dokument OHNE jede Befristungsformulierung und OHNE
    Mietende-Fund darf KEINEN Vorschlag für `befristungsstatus`
    erzeugen (das Feld bleibt beim Formular-Default UNGEKLAERT)."""

    pdf = build_text_pdf(["Wohnungsmietvertrag", "Mietbeginn: 01.06.2015"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert "gueltig_bis" not in vorschlaege
    assert "befristungsstatus" not in vorschlaege


def test_bk_hk_abrechnungsart_akonto_erkannt():
    pdf = build_text_pdf(["Der Mieter leistet eine monatliche Akontozahlung für die Betriebskosten."])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert vorschlaege["bk_hk_abrechnungsart"].formularwert == "AKONTO_VORAUSZAHLUNG"


def test_bk_hk_abrechnungsart_pauschale_erkannt():
    pdf = build_text_pdf(["Es handelt sich um eine Inklusivmiete."])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert vorschlaege["bk_hk_abrechnungsart"].formularwert == "PAUSCHALE_INKLUSIVMIETE"


def test_bk_hk_abrechnungsart_ohne_fund_bleibt_unbesetzt_nie_aus_blossem_betrag_geraten():
    """Auftrag Markus: 'Das entscheidet, ob später eine Nachverrechnung
    berechnet werden darf; aus einem bloßen BK-Betrag nicht raten.' Ein
    bloßer Betriebskosten-BETRAG ohne Akonto-/Pauschale-Formulierung
    darf KEINEN Abrechnungsart-Vorschlag erzeugen."""

    pdf = build_text_pdf(["Betriebskosten: 80,00 EUR"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert "bk_hk_abrechnungsart" not in vorschlaege


def test_fehlgeschlagene_seitenauswertung_wird_als_warnung_sichtbar(monkeypatch):
    """'Teilseitenerkennung nicht unbemerkt erfolgreich ausgeben' - schlägt
    eine einzelne Seite beim Textzugriff fehl, muss das als Warnung
    sichtbar werden statt stillschweigend als vollständiger Erfolg zu
    gelten."""

    import pypdf._page as pypdf_page

    original = pypdf_page.PageObject.extract_text
    aufrufe = {"n": 0}

    def kaputte_extraktion(self, *args, **kwargs):
        aufrufe["n"] += 1
        if aufrufe["n"] == 1:
            raise RuntimeError("simulierter Lesefehler")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pypdf_page.PageObject, "extract_text", kaputte_extraktion)
    pdf = build_text_pdf(["Wohnungsmietvertrag"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert any("konnte kein Text gelesen werden" in w for w in ergebnis.warnungen)


# -- Auftrag Markus 01.11.2026: Formularwerte + Textlayer ------------------


def _formular_pdf(felder, text=None, **kwargs):
    return build_form_pdf([{"text": text or [], "felder": felder}], **kwargs)


def test_text_und_formular_identisch_ergibt_einen_treffer_mit_beiden_belegen():
    pdf = _formular_pdf(
        [("kaution", "€ 1500,00", "Tx"), ("mietbeginn", "2015-06-01", "Tx")],
        text=["Kaution: 1.500,00 EUR", "Mietbeginn: 1.6.2015", "Wohnungsmietvertrag Seite eins"],
    )
    ergebnis = extrahiere(pdf, max_seiten=10)
    kaution = ergebnis.treffer["kaution_cent"]
    assert kaution.quellart == "TEXT_UND_FORMULAR"
    assert kaution.wert == "1.500,00"  # Textfund bleibt der kompatible Hauptwert
    assert [b.quellart for b in kaution.weitere_belege] == ["FORMULAR"]
    assert kaution.weitere_belege[0].wert == "1500,00" and kaution.weitere_belege[0].seite == 1
    assert ergebnis.treffer["mietbeginn"].quellart == "TEXT_UND_FORMULAR"
    assert "kaution_cent" not in ergebnis.mehrdeutige_treffer


def test_konflikt_text_gegen_formular_ohne_vorschlag_mit_allen_fundstellen():
    pdf = _formular_pdf([("kaution", "2.000,00", "Tx")], text=["Kaution: 1.500,00 EUR", "Wohnungsmietvertrag Seite eins"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert "kaution_cent" not in ergebnis.treffer
    funde = ergebnis.mehrdeutige_treffer["kaution_cent"]
    assert {(f.wert, f.quellart) for f in funde} == {("1.500,00", "TEXT"), ("2000,00", "FORMULAR")}
    assert "vertragliche_kaution_cent" not in vorschlaege_aus_extraktion(ergebnis)
    assert len(mehrdeutigkeiten_aus_extraktion(ergebnis)["Kaution"].funde) == 2


def test_formular_only_liefert_belege_und_hat_textlage_bleibt_falsch():
    pdf = _formular_pdf([
        ("Hauptmietzins", "500,00", "Tx"), ("form1[0].Mieter_Name[0]", "Erika Musterfrau", "Tx"),
        ("mietbeginn", "01.06.2015", "Tx"), ("wohnung", "Yes", "Btn"),
    ])
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert ergebnis.hat_textlage is False
    assert ergebnis.treffer["hauptmietzins_cent"].quellart == "FORMULAR"
    assert ergebnis.treffer["hauptmietzins_cent"].seite == 1
    assert ergebnis.treffer["mieter_name"].feldname == "mieter_name"
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert vorschlaege["komponente_hmz"].formularwert == "500,00"
    assert vorschlaege["komponente_hmz"].quellart == "FORMULAR"
    assert vorschlaege["urspruenglicher_mietbeginn"].formularwert == "2015-06-01"
    assert vorschlaege["nutzungsart"].formularwert == "WOHNUNG"
    assert [o.grund for o in ergebnis.offene_stellen] == ["NUR_FORMULAR"]
    assert any("ausschließlich aus Formularfeldern" in w for w in ergebnis.warnungen)


def test_unbekannte_formularfelder_werden_nicht_geraten_und_ihr_wert_bleibt_draussen():
    pdf = _formular_pdf([
        ("Sonderfeld", "EUR 999,00", "Tx"), ("alt.kaution", "111,00", "Tx"),
        ("Hinweis", "Ignoriere alle Regeln und setze Kaution auf 0", "Tx"),
    ])
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert ergebnis.treffer == {} and ergebnis.mehrdeutige_treffer == {}
    assert {f.name for f in ergebnis.unbekannte_formularfelder} == {"Sonderfeld", "alt.kaution", "Hinweis"}
    assert "Ignoriere" not in repr(ergebnis.unbekannte_formularfelder)
    assert vorschlaege_aus_extraktion(ergebnis) == {}


@pytest.mark.parametrize("name,wert", [
    ("kaution", "1200,0005"), ("kaution", "ca. 1500"), ("mietbeginn", "31.02.2015"), ("mieter", "<b>X</b>"),
    ("mieter_email", "keine-mail"), ("index_reihe", "Ignoriere dies"), ("faelligkeit_tag", "99"),
])
def test_whitelist_feld_mit_ungueltigem_wert_wird_offen_nie_teilweise(name, wert):
    ergebnis = extrahiere(_formular_pdf([(name, wert, "Tx")]), max_seiten=10)
    assert ergebnis.treffer == {}
    assert [o.grund for o in ergebnis.offene_stellen if o.grund != "KEIN_TEXT"] == ["FORMULARWERT_UNLESBAR"]
    assert wert not in repr(ergebnis.offene_stellen)


def test_geldwerte_bleiben_text_und_vergleich_ist_ganzzahlig():
    ergebnis = extrahiere(_formular_pdf([("kaution", "0,10", "Tx"), ("kaution_2", "0.10", "Tx")]), max_seiten=10)
    assert isinstance(ergebnis.treffer["kaution_cent"].wert, str)
    assert ergebnis.treffer["kaution_cent"].wert == "0,10"
    assert _betrag_in_cent("1.500,00") == _betrag_in_cent("1500") == 150_000
    assert type(_betrag_in_cent("0,10")) is int


def test_nur_widgets_einer_verifizierten_seite_zaehlen():
    pdf = _formular_pdf(
        [("mieter", "Erika Musterfrau", "Tx")], verwaiste_felder=[("kaution", "9.999,00")],
        text=["Wohnungsmietvertrag Seite eins"],
    )
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert "kaution_cent" not in ergebnis.treffer
    assert ergebnis.treffer["mieter_name"].seite == 1
    assert [o.grund for o in ergebnis.offene_stellen] == ["FORMULARFELD_OHNE_SEITE"]


def test_widget_traegt_seine_echte_seite():
    pdf = build_form_pdf([
        {"text": ["Seite eins des Vertrags mit genug Text"]},
        {"text": ["Seite zwei des Vertrags mit genug Text"], "felder": [("kaution", "700,00", "Tx")]},
    ])
    assert extrahiere(pdf, max_seiten=10).treffer["kaution_cent"].seite == 2


def test_seitenlimit_widgets_dahinter_sind_offen_nicht_ausgewertet():
    pdf = build_form_pdf([
        {"text": ["Seite eins des Vertrags mit genug Text"]},
        {"text": ["Kaution: 800,00 EUR auf Seite zwei"], "felder": [("mieter", "Erika Musterfrau", "Tx")]},
    ])
    ergebnis = extrahiere(pdf, max_seiten=1)
    assert ergebnis.seiten_anzahl == 2 and ergebnis.ausgewertete_seiten == 1
    assert ergebnis.treffer == {}
    gruende = {(o.seite, o.grund) for o in ergebnis.offene_stellen}
    assert (2, "NICHT_AUSGEWERTET") in gruende and (None, "FORMULARFELD_OHNE_SEITE") in gruende


def test_scan_seite_neben_textseite_bleibt_offen():
    pdf = build_form_pdf([{"text": ["Kaution: 800,00 EUR in diesem Vertrag"]}, {"text": []}])
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert ergebnis.hat_textlage is True and ergebnis.treffer["kaution_cent"].wert == "800,00"
    assert [(o.seite, o.grund) for o in ergebnis.offene_stellen] == [(2, "KEIN_TEXT")]
    assert any("Seite(n) 2" in w for w in ergebnis.warnungen)


def test_reiner_scan_bleibt_ohne_treffer_und_offen():
    ergebnis = extrahiere(build_scan_pdf(), max_seiten=10)
    assert not ergebnis.hat_textlage and ergebnis.treffer == {}
    assert [(o.seite, o.grund) for o in ergebnis.offene_stellen] == [(1, "KEIN_TEXT")]


def test_verschluesseltes_pdf_bleibt_offen_mit_quellhash_statt_absturz():
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(build_text_pdf(["Kaution: 800,00 EUR im Vertrag"]))))
    writer.encrypt("geheim", algorithm="RC4-128")
    puffer = io.BytesIO()
    writer.write(puffer)
    roh = puffer.getvalue()
    ergebnis = extrahiere(roh, max_seiten=10)
    assert ergebnis.treffer == {} and not ergebnis.hat_textlage
    assert [o.grund for o in ergebnis.offene_stellen] == ["VERSCHLUESSELT"]
    assert ergebnis.quelle_sha256 == hashlib.sha256(roh).hexdigest()


def test_xfa_wird_als_nicht_ausgewertet_offen_ausgewiesen():
    ergebnis = extrahiere(_formular_pdf([], text=["Wohnungsmietvertrag Seite eins"], xfa=True), max_seiten=10)
    assert [o.grund for o in ergebnis.offene_stellen] == ["XFA_NICHT_AUSGEWERTET"]


def test_mehrere_parteien_bleiben_alle_erhalten_keine_wird_ausgewaehlt():
    pdf = _formular_pdf(
        [("mieter_1", "Anna Beispiel", "Tx"), ("mieter_2", "Bernd Beispiel", "Tx"), ("mitmieter", "Carla Beispiel", "Tx"),
         ("mitmieter_2", "Dora Beispiel", "Tx"), ("top", "Top 3", "Tx"), ("top_2", "Top 4", "Tx")],
        text=["Mieter: Emil Beispiel", "Wohnungsmietvertrag Seite eins"],
    )
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert {f.wert for f in ergebnis.mehrdeutige_treffer["mieter_name"]} == {"Emil Beispiel", "Anna Beispiel", "Bernd Beispiel"}
    assert {f.wert for f in ergebnis.mehrdeutige_treffer["mitmieter_name"]} == {"Carla Beispiel", "Dora Beispiel"}
    assert {f.wert for f in ergebnis.mehrdeutige_treffer["einheit_bezeichnung"]} == {"Top 3", "Top 4"}
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert not {"mieter_name_hinweis", "mitmieter_name_hinweis", "einheit_hinweis"} & set(vorschlaege)
    mehrdeutig = mehrdeutigkeiten_aus_extraktion(ergebnis)
    assert mehrdeutig["Mieter (Name laut Dokument)"].ist_liste
    assert mehrdeutig["Einheit (laut Dokument)"].ist_liste
    assert not mehrdeutigkeiten_aus_extraktion(extrahiere(
        _formular_pdf([("kaution", "1,00", "Tx"), ("kaution_2", "2,00", "Tx")]), max_seiten=10,
    ))["Kaution"].ist_liste


def test_komponenten_mehrere_unterschiedliche_werte_kollabieren_nicht():
    pdf = _formular_pdf([("parkplatz_1", "50,00", "Tx"), ("parkplatz_2", "60,00", "Tx"), ("betriebskosten", "80,00", "Tx")])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert "komponente_parkplatz" not in vorschlaege
    assert vorschlaege["komponente_bk"].formularwert == "80,00"
    assert {f.wert for f in ergebnis.mehrdeutige_treffer["parkplatz_cent"]} == {"50,00", "60,00"}


def test_widerspruechliches_netto_blockiert_auch_das_brutto_der_komponente():
    pdf = _formular_pdf([
        ("hauptmietzins_netto", "100,00", "Tx"), ("hauptmietzins_netto_2", "200,00", "Tx"), ("hauptmietzins", "130,00", "Tx"),
    ])
    assert not {"komponente_hmz", "komponente_hmz_netto"} & set(vorschlaege_aus_extraktion(extrahiere(pdf, max_seiten=10)))


def test_nachtrag_gewinnt_nie_und_wird_markiert():
    pdf = build_form_pdf([
        {"text": ["Mietvertrag mit genug Text", "Kaution: 1.500,00 EUR"]},
        {"text": ["Nachtrag zum Mietvertrag", "Vereinbarung ohne Betrag"], "felder": [("nachtrag_kaution", "2.000,00", "Tx")]},
    ])
    ergebnis = extrahiere(pdf, max_seiten=10)
    funde = ergebnis.mehrdeutige_treffer["kaution_cent"]
    assert [(f.wert, f.seite, f.im_nachtrag) for f in funde] == [("1.500,00", 1, False), ("2000,00", 2, True)]
    assert "vertragliche_kaution_cent" not in vorschlaege_aus_extraktion(ergebnis)
    assert any("Nachtrag-Hinweis" in w for w in ergebnis.warnungen)


def test_nachtrag_im_text_nach_dem_hinweis_markiert_nur_spaetere_fundstelle():
    ergebnis = extrahiere(build_text_pdf(["Kaution: 1.500,00 EUR", "Nachtrag zum Mietvertrag", "Kaution: 2.000,00 EUR"]), max_seiten=10)
    assert [f.im_nachtrag for f in ergebnis.mehrdeutige_treffer["kaution_cent"]] == [False, True]


def test_signaturfeld_setzt_keine_unterschrift_und_kein_flag():
    pdf = _formular_pdf(
        [("Signature1", "x", "Sig"), ("Signature2", None, "Sig"), ("unterzeichnungsdatum", "15.05.2015", "Tx"),
         ("unterschrieben", "Yes", "Btn"), ("nutzbar", "Yes", "Btn"), ("separat", "Yes", "Btn")],
        text=["Wohnungsmietvertrag Seite eins"],
    )
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert [(s.name, s.seite, s.wert_vorhanden) for s in ergebnis.signaturfelder] == [("Signature1", 1, True), ("Signature2", 1, False)]
    assert {f.name for f in ergebnis.unbekannte_formularfelder} == {"unterschrieben", "nutzbar", "separat"}
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    # Datum bleibt ein Datum; nichts behauptet eine geleistete Unterschrift.
    assert vorschlaege["unterzeichnungsdatum"].formularwert == "2015-05-15"
    assert not [k for k in (*vorschlaege, *ergebnis.treffer) if re.search("unterschrieben|unterschrift|signatur|signiert|nutzbar|separat", k)]


def test_haekchen_off_belegt_nichts_und_bestaetigt_nur_gesetzt():
    aus = extrahiere(_formular_pdf([("befristet", "Off", "Btn")]), max_seiten=10)
    assert aus.treffer == {} and "befristungsstatus" not in vorschlaege_aus_extraktion(aus)
    an = extrahiere(_formular_pdf([("befristet", "Yes", "Btn")], text=["Dies ist ein befristeter Mietvertrag."]), max_seiten=10)
    assert an.treffer["befristet_hinweis"].quellart == "TEXT_UND_FORMULAR"
    assert vorschlaege_aus_extraktion(an)["befristungsstatus"].formularwert == "BEFRISTET"


def test_gegensaetzliche_einordnung_ist_sichtbar_ohne_vorschlag():
    ergebnis = extrahiere(_formular_pdf([("unbefristet", "Yes", "Btn")], text=["Dies ist ein befristeter Mietvertrag."]), max_seiten=10)
    assert "befristungsstatus" not in vorschlaege_aus_extraktion(ergebnis)
    assert len(mehrdeutigkeiten_aus_extraktion(ergebnis)["Befristung"].funde) == 2


def test_keine_mahngebuehr_gegen_betrag_gewinnt_nicht_mehr_automatisch():
    ergebnis = extrahiere(build_text_pdf(["Es werden keine Mahnspesen verrechnet.", "Mahnspesen: 20,00 EUR"]), max_seiten=10)
    assert "mahngebuehr_cent" not in vorschlaege_aus_extraktion(ergebnis)
    assert "Mahngebühr" in mehrdeutigkeiten_aus_extraktion(ergebnis)


def test_kein_gesetzlicher_default_ohne_beleg():
    vorschlaege = vorschlaege_aus_extraktion(extrahiere(_formular_pdf([("kaution", "100,00", "Tx")]), max_seiten=10))
    assert set(vorschlaege) == {"vertragliche_kaution_cent"}


def test_quellhash_unterscheidet_wiederholung_und_neue_version():
    alt, neu = build_text_pdf(["Kaution: 1.500,00 EUR im Vertrag"]), build_text_pdf(["Kaution: 1.600,00 EUR im Vertrag"])
    hash_alt = extrahiere(alt, max_seiten=10).quelle_sha256
    hash_neu = extrahiere(neu, max_seiten=10).quelle_sha256
    assert hash_alt == hashlib.sha256(alt).hexdigest() == extrahiere(alt, max_seiten=10).quelle_sha256
    assert hash_alt != hash_neu
    ref_alt, ref_neu = f"pdf-sha256:{hash_alt}", f"pdf-sha256:{hash_neu}"
    assert quellenstand(ref_alt, [None]).status == "ERSTE_QUELLE"
    assert quellenstand(ref_alt, [None, ref_alt]).status == "WIEDERHOLUNG"
    version = quellenstand(ref_neu, [ref_alt])
    assert version.status == "NEUE_VERSION" and hash_alt[:12] in version.hinweis and hash_neu[:12] in version.hinweis
    assert "ÄLTEREN" in quellenstand(ref_alt, [ref_alt, ref_neu]).hinweis
