"""Tests für den PDF/A-Mahnbrief-Generator (mahnwesen/brief_pdf.py) -
reine Funktion, keine DB. Prüft PDF/A-2B-Konformitätserzwingung (fpdf2
`enforce_compliance`), Fenster-Adressblock und dass exakt dieselben
Beträge wie in Vorschau/E-Mail im Text erscheinen."""

from __future__ import annotations

import io
from datetime import date

import pytest
from pypdf import PdfReader

from mietinkasso.mahnwesen.brief_pdf import Absender, erzeuge_mahnbrief_pdf


def _absender(**overrides) -> Absender:
    werte = dict(
        name="JLB Projects GmbH", adresse="Marc-Aurel-Straße 4/16, 1010 Wien",
        fn="FN 631126b, Handelsgericht Wien", uid="ATU81269707", telefon="+43 1 435 10 11",
        website="jlb-immo.at", email="hausverwaltung@jlb-immo.at",
        farbe_anthrazit="#1F2125", farbe_gold="#C9A86A",
    )
    werte.update(overrides)
    return Absender(**werte)


def _erzeugen(**overrides) -> bytes:
    werte = dict(
        absender=_absender(), empfaenger_name="Max Mustermieter", empfaenger_adresse="Musterstraße 1\n1010 Wien",
        objekt_bezeichnung="Am Corso", einheit_bezeichnung="Top 3", stufe=2, heute=date(2026, 9, 28),
        zahlungsfrist_bis=date(2026, 10, 12), hauptforderung_cent=0, bereits_offene_mahnkosten_cent=2073,
        neue_zinsen_delta_cent=54, neue_gebuehr_cent=0, gebuehr_rechtsgrundlage=None,
        zins_hinweis="Verzugszinsen (4,0 % p.a.)", gesamtbetrag_cent=2127,
    )
    werte.update(overrides)
    return erzeuge_mahnbrief_pdf(**werte)


def test_erzeugtes_pdf_ist_lesbar_und_zeigt_denselben_gesamtbetrag_wie_die_vorschau():
    pdf_bytes = _erzeugen()
    assert pdf_bytes[:5] == b"%PDF-"
    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages) == 1
    text = reader.pages[0].extract_text()
    assert "Max Mustermieter" in text
    assert "Musterstraße 1" in text
    assert "Zweite Mahnung" in text
    assert "20,73" in text  # bereits offene Mahnkosten
    assert "0,54" in text  # neue Zinsen
    assert "21,27" in text  # Gesamtbetrag - identisch zur Vorschau/E-Mail


def test_erste_stufe_heisst_zahlungserinnerung_nicht_mahnung():
    pdf_bytes = _erzeugen(stufe=1, hauptforderung_cent=83_000, bereits_offene_mahnkosten_cent=0, gesamtbetrag_cent=83_000)
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = reader.pages[0].extract_text()
    assert "Zahlungserinnerung" in text
    assert "Zweite Mahnung" not in text


def test_ohne_konfiguriertes_logo_wird_kein_signet_gezeichnet():
    """Kein erfundenes JLB-Logo - ohne konfigurierten, tatsächlich
    vorhandenen Dateipfad bleibt der Brief ein reiner Textbrief."""

    pdf_bytes = _erzeugen(absender=_absender(logo_pfad="/pfad/existiert/nicht.png"))
    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages[0].images) == 0


def test_absenderdaten_stehen_in_fusszeile():
    pdf_bytes = _erzeugen()
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = reader.pages[0].extract_text()
    assert "ATU81269707" in text
    assert "FN 631126b" in text
    assert "hausverwaltung@jlb-immo.at" in text


def test_fehlende_eingebettete_schrift_wird_von_pdfa_erzwingung_abgelehnt():
    """Rückprüfung Codex 14.09.2026: PDF/A-Konformität wird über fpdf2s
    `enforce_compliance="PDF/A-2B"` erzwungen, nicht selbst nachgebaut -
    ein Verstoß (z. B. eine nicht eingebettete Basis-Schriftart) muss
    bereits bei der Erzeugung laut fehlschlagen."""

    from fpdf import FPDF
    from fpdf.errors import PDFAComplianceError

    pdf = FPDF(format="A4", unit="mm", enforce_compliance="PDF/A-2B")
    pdf.add_page()
    with pytest.raises(PDFAComplianceError):
        pdf.set_font("Helvetica", "", 11)
