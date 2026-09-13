"""Tests für die lokale PDF-Textextraktion und Ablage (Auftrag
HV-20260913-VERTRAGSANLAGE). Alle PDFs sind synthetisch von Hand gebaut
(`_pdf_test_helpers.py`), keine echten Vertragsdokumente."""

from __future__ import annotations

from pathlib import Path

import pytest

from mietinkasso.vertragsanlage.ablage import UploadAbgelehntError, lesen, speichern
from mietinkasso.vertragsanlage.pdf_extraktion import PdfNichtLesbarError, extrahiere
from mietinkasso.vertragsanlage.vorschlaege import vorschlaege_aus_extraktion
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
