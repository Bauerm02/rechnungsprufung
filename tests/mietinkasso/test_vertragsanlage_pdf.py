"""Tests für die lokale PDF-Textextraktion und Ablage (Auftrag
HV-20260913-VERTRAGSANLAGE). Alle PDFs sind synthetisch von Hand gebaut
(`_pdf_test_helpers.py`), keine echten Vertragsdokumente."""

from __future__ import annotations

from pathlib import Path

import pytest

from mietinkasso.vertragsanlage.ablage import UploadAbgelehntError, lesen, speichern
from mietinkasso.vertragsanlage.pdf_extraktion import PdfNichtLesbarError, extrahiere
from mietinkasso.vertragsanlage.vorschlaege import mehrdeutigkeiten_aus_extraktion, vorschlaege_aus_extraktion
from tests.mietinkasso._pdf_test_helpers import build_scan_pdf, build_text_pdf


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


def test_mietbestandteile_werden_getrennt_extrahiert():
    pdf = build_text_pdf([
        "Hauptmietzins: 500,00 EUR", "Betriebskosten: 80,00 EUR",
        "Heizkosten: 40,00 EUR", "Küche: 30,00 EUR", "Parkplatz: 50,00 EUR",
    ])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert vorschlaege["hauptmietzins_cent"].formularwert == "500,00"
    assert vorschlaege["betriebskosten_cent"].formularwert == "80,00"
    assert vorschlaege["heizkosten_cent"].formularwert == "40,00"
    assert vorschlaege["kueche_cent"].formularwert == "30,00"
    assert vorschlaege["parkplatz_cent"].formularwert == "50,00"


def test_mietende_wird_extrahiert():
    pdf = build_text_pdf(["Mietende: 31.05.2030"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert vorschlaege["gueltig_bis"].formularwert == "2030-05-31"


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
