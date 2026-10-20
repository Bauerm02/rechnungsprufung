"""Tests für den druckfähigen JLB-PDF-Kontoauszug (`op/kontoauszug_pdf.py`,
Auftrag HV-20261020-KONTOBLATT-KOSTEN) - reine Rendering-Funktion über
einem bereits aggregierten `Kontoblatt` (keine DB, keine echten Daten)."""

from __future__ import annotations

import io
from datetime import date

import pytest
from pypdf import PdfReader

from mietinkasso.infrastructure.db.tables import MahnLaufTable, OPPositionTable, RuecklastschriftkostenTable
from mietinkasso.mahnwesen.brief_pdf import Absender
from mietinkasso.op.kontoauszug_pdf import erzeuge_kontoauszug_pdf
from mietinkasso.op.kontoblatt import (
    KATEGORIE_HAUPTFORDERUNG,
    KATEGORIE_RUECKLASTSCHRIFTKOSTEN,
    KATEGORIE_VERZUGSZINSEN,
    DokumentierterBankaufwand,
    Kontoblatt,
    KontoblattZeile,
)


def _absender(**overrides) -> Absender:
    werte = dict(
        name="JLB Projects GmbH", adresse="Marc-Aurel-Straße 4/16, 1010 Wien",
        fn="FN 631126b, Handelsgericht Wien", uid="ATU81269707", telefon="+43 1 435 10 11",
        website="jlb-immo.at", email="hausverwaltung@jlb-immo.at",
        farbe_anthrazit="#1F2125", farbe_gold="#C9A86A",
    )
    werte.update(overrides)
    return Absender(**werte)


def _op(id: int, typ: str, betrag_cent: int, *, belegdatum: date = date(2026, 1, 1), referenz: str = "Beleg") -> OPPositionTable:
    return OPPositionTable(
        id=id, konto_id="K-1", typ=typ, betrag_cent=betrag_cent, belegdatum=belegdatum, buchungsdatum=belegdatum,
        faelligkeit=None, faelligkeit_bekannt=False, status="AKTIV", beleg_referenz=referenz,
    )


def _leeres_kontoblatt(**overrides) -> Kontoblatt:
    werte = dict(
        konto_id="K-1", saldo_cent=0, faelliger_unstrittiger_rest_cent=0, hauptforderung_zeilen=[],
        hauptforderung_cent=0, kosten_zeilen=[], kosten_cent=0, zahlungen_zeilen=[], zahlungen_saldo_cent=0,
        dokumentierte_bankaufwaende=[], ausstehende_mahnlaeufe=[],
    )
    werte.update(overrides)
    return Kontoblatt(**werte)


def _erzeugen(kontoblatt: Kontoblatt, **overrides) -> bytes:
    werte = dict(
        absender=_absender(), objekt_bezeichnung="Am Corso", einheit_bezeichnung="Top 3",
        debitor_name="Max Mustermieter", vertrag_id="V-601-1", konto_id="K-1", heute=date(2026, 10, 20),
        kontoblatt=kontoblatt,
    )
    werte.update(overrides)
    return erzeuge_kontoauszug_pdf(**werte)


def test_erzeugtes_pdf_ist_lesbar_und_zeigt_gesamtsaldo():
    kontoblatt = _leeres_kontoblatt(
        hauptforderung_zeilen=[KontoblattZeile(position=_op(1, "EROEFFNUNG", 100_000), kategorie=KATEGORIE_HAUPTFORDERUNG, rest_cent=None)],
        hauptforderung_cent=100_000, saldo_cent=100_000, faelliger_unstrittiger_rest_cent=100_000,
    )
    pdf_bytes = _erzeugen(kontoblatt)
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = reader.pages[0].extract_text()
    assert "1.000,00 EUR" in text
    assert "Gesamtsaldo" in text
    assert "V-601-1" in text
    assert "Max Mustermieter" in text


def test_pdf_zeigt_jlb_ci_absenderdaten_in_fusszeile():
    pdf_bytes = _erzeugen(_leeres_kontoblatt())
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = reader.pages[0].extract_text()
    assert "JLB Projects GmbH" in text
    assert "ATU81269707" in text
    assert "hausverwaltung@jlb-immo.at" in text


def test_pdf_lehnt_fehlende_eingebettete_schrift_nicht_ab_bei_fallback_font():
    """Ohne konfigurierte Absender-Fontpfade fällt die Erzeugung auf die
    immer vorhandene Fallback-TTF zurück (wie `brief_pdf.py`) - PDF/A-
    Erzwingung scheitert dabei NICHT."""

    pdf_bytes = _erzeugen(_leeres_kontoblatt())
    assert pdf_bytes[:5] == b"%PDF-"


def test_getrennte_hauptforderung_und_kosten_werden_beide_gezeigt():
    hauptforderung = KontoblattZeile(position=_op(1, "SOLL", 50_000, referenz="HMZ Jänner"), kategorie=KATEGORIE_HAUPTFORDERUNG, rest_cent=None)
    zinsen = KontoblattZeile(position=_op(2, "SOLL", 489, referenz="Verzugszinsen"), kategorie=KATEGORIE_VERZUGSZINSEN, rest_cent=None)
    kontoblatt = _leeres_kontoblatt(
        hauptforderung_zeilen=[hauptforderung], hauptforderung_cent=50_000,
        kosten_zeilen=[zinsen], kosten_cent=489, saldo_cent=50_489, faelliger_unstrittiger_rest_cent=50_489,
    )
    pdf_bytes = _erzeugen(kontoblatt)
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = reader.pages[0].extract_text()
    assert "Hauptforderung" in text
    assert "500,00 EUR" in text
    assert "Verzugszinsen" in text
    assert "4,89 EUR" in text


def test_dokumentierter_bankaufwand_zeigt_status_nicht_belastet():
    rls = RuecklastschriftkostenTable(
        vertrag_id="V-601-1", ruecklastschrift_op_position_id=7, bankentgelt_cent=700,
        rueckgabeursache="Mangels Deckung", dem_zahlungspflichtigen_zurechenbar=False,
        zurechenbarkeit_begruendung="noch unklar", beleg_referenz="BELEG-RLS-7", kosten_op_position_id=None,
        erstellt_von="codex-test",
    )
    kontoblatt = _leeres_kontoblatt(dokumentierte_bankaufwaende=[DokumentierterBankaufwand(row=rls)])
    pdf_bytes = _erzeugen(kontoblatt)
    text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf_bytes)).pages)
    assert "dokumentiert" in text and "nicht belastet" in text
    assert "7,00 EUR" in text
    assert "BELEG-RLS-7" in text


def test_ausstehender_mahnlauf_zeigt_nicht_gebucht_hinweis():
    mahnlauf = MahnLaufTable(
        id=1, outbox_key="O-1", vertrag_id="V-601-1", gesellschaft_id="7DI", stufe=2,
        mitglieder_mahnfall_ids="[]", kanal="BRIEF", status="UNSICHER", mahnkosten_verarbeitet_am=None,
    )
    kontoblatt = _leeres_kontoblatt(ausstehende_mahnlaeufe=[mahnlauf])
    pdf_bytes = _erzeugen(kontoblatt)
    text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf_bytes)).pages)
    assert "nicht gebucht" in text
    assert "Stufe 2" in text
    assert "UNSICHER" in text


def test_viele_zeilen_erzeugen_mehrseitiges_pdf_ohne_abgeschnittenen_inhalt():
    """Codex-Abnahme-relevanter Regressionstest: eine lange Liste
    gebuchter Positionen MUSS auf mehrere Seiten umbrechen, OHNE dass der
    letzte Eintrag verloren geht oder mitten im Text abgeschnitten wird -
    über die tatsächlich extrahierte letzte Zeile geprüft, nicht nur über
    die Seitenzahl."""

    viele_zeilen = [
        KontoblattZeile(
            position=_op(
                i, "SOLL", 1000 * i,
                referenz=f"Sehr lange Forderungsbezeichnung Nummer {i:03d} mit genug Text, um sicher umzubrechen und mehrere Zeilen zu belegen.",
            ),
            kategorie=KATEGORIE_HAUPTFORDERUNG, rest_cent=None,
        )
        for i in range(1, 50)
    ]
    letzte_markierung = "EINDEUTIGE-LETZTE-ZEILE-MARKIERUNG-9999"
    viele_zeilen.append(
        KontoblattZeile(position=_op(999, "SOLL", 12_345, referenz=letzte_markierung), kategorie=KATEGORIE_HAUPTFORDERUNG, rest_cent=None)
    )
    gesamtsumme = sum(z.position.betrag_cent for z in viele_zeilen)
    kontoblatt = _leeres_kontoblatt(
        hauptforderung_zeilen=viele_zeilen, hauptforderung_cent=gesamtsumme,
        saldo_cent=gesamtsumme, faelliger_unstrittiger_rest_cent=gesamtsumme,
    )
    pdf_bytes = _erzeugen(kontoblatt)
    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages) > 1

    gesamter_text = "".join(p.extract_text() for p in reader.pages)
    assert letzte_markierung in gesamter_text  # die letzte Zeile ging NICHT verloren
    assert "123,45 EUR" in gesamter_text  # ihr Betrag ist vollständig lesbar, nicht abgeschnitten
    # Jede der 50 Forderungsnummern erscheint im extrahierten Text -
    # keine Zeile fiel beim Seitenumbruch "zwischen zwei Seiten" weg.
    for i in range(1, 50):
        assert f"{i:03d}" in gesamter_text


def test_zahlungen_und_stornostand_werden_separat_gezeigt():
    zahlung = KontoblattZeile(position=_op(1, "ZAHLUNG", 50_000, referenz="Banküberweisung"), kategorie=KATEGORIE_HAUPTFORDERUNG, rest_cent=None)
    ruecklastschrift = KontoblattZeile(position=_op(2, "RUECKLASTSCHRIFT", 50_000, referenz="Rückbuchung"), kategorie=KATEGORIE_HAUPTFORDERUNG, rest_cent=None)
    kontoblatt = _leeres_kontoblatt(zahlungen_zeilen=[zahlung, ruecklastschrift], zahlungen_saldo_cent=0)
    pdf_bytes = _erzeugen(kontoblatt)
    text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf_bytes)).pages)
    assert "Zahlungen" in text and "Stornostand" in text
    assert "Banküberweisung" in text
    assert "Rückbuchung" in text
