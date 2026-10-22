"""Tests für den druckfähigen JLB-PDF-Kontoauszug (`op/kontoauszug_pdf.py`,
Auftrag HV-20261020-KONTOBLATT-KOSTEN) - reine Rendering-Funktion über
einem bereits aggregierten `Kontoblatt` (keine DB, keine echten Daten)."""

from __future__ import annotations

import io
from datetime import date, datetime, timezone

import pytest
from pypdf import PdfReader

from mietinkasso.infrastructure.db.tables import (
    ManuellerMahnversandTable,
    MahnkostenBuchungTable,
    MahnLaufTable,
    OPPositionTable,
    RuecklastschriftkostenTable,
)
from mietinkasso.mahnwesen.brief_pdf import Absender
from mietinkasso.op.kontoauszug_pdf import erzeuge_kontoauszug_pdf
from mietinkasso.op.kontoblatt import (
    KATEGORIE_HAUPTFORDERUNG,
    KATEGORIE_MAHNSPESEN,
    KATEGORIE_RUECKLASTSCHRIFTKOSTEN,
    KATEGORIE_VERZUGSZINSEN,
    STATUS_BEZAHLT,
    STATUS_ERSETZT,
    STATUS_OFFEN,
    STATUS_TEILBEZAHLT,
    AusstehenderManuellerMahnversand,
    DokumentierterBankaufwand,
    Kontoblatt,
    KontoblattZeile,
    MahnkostenLedgerEintrag,
    ZinsZuordnungZeile,
)


def _buchung(**overrides) -> MahnkostenBuchungTable:
    werte = dict(
        vertrag_id="V-601-1", stufe=1, mahnlauf_schluessel="HASH", forderung_op_position_ids="[]",
        hauptforderung_cent=0, zinsbasis="VEREINBART_GEPRUEFT", zinssatz_prozent=0, zins_von=date(2026, 9, 1),
        zins_bis=date(2026, 9, 30), zinsen_cent=0, versandnachweis_referenz="mahnung:X", erstellt_von="codex-test",
    )
    werte.update(overrides)
    return MahnkostenBuchungTable(**werte)


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
        hauptforderung_cent=0, hauptforderung_offener_rest_cent=0, kosten_zeilen=[], kosten_cent=0,
        kosten_offener_rest_cent=0, kosten_historie=[], zahlungen_zeilen=[], zahlungen_saldo_cent=0,
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
    kontoblatt = _leeres_kontoblatt(dokumentierte_bankaufwaende=[DokumentierterBankaufwand(row=rls, rueckgabe_datum=date(2026, 4, 10))])
    pdf_bytes = _erzeugen(kontoblatt)
    text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf_bytes)).pages)
    assert "dokumentiert" in text and "nicht belastet" in text
    assert "7,00 EUR" in text
    assert "BELEG-RLS-7" in text


def test_dokumentierter_bankaufwand_zeigt_rueckgabedatum_nicht_erfassungsdatum():
    """Codex-Abnahme 20.10.2026, P2: Rückgabe (Bankereignistag) 10.04.,
    Kostenerfassung (`gebucht_am`) 18.04. - BEIDE Datumsangaben müssen im
    PDF erscheinen, und zwar unter der Überschrift "Rückgabe" exakt der
    10.04., NIE der 18.04."""

    rls = RuecklastschriftkostenTable(
        vertrag_id="V-601-1", ruecklastschrift_op_position_id=7, bankentgelt_cent=700,
        rueckgabeursache="Mangels Deckung", dem_zahlungspflichtigen_zurechenbar=False,
        zurechenbarkeit_begruendung="noch unklar", beleg_referenz="BELEG-RLS-7", kosten_op_position_id=None,
        erstellt_von="codex-test", gebucht_am=datetime(2026, 4, 18, 9, 0, tzinfo=timezone.utc),
    )
    kontoblatt = _leeres_kontoblatt(dokumentierte_bankaufwaende=[DokumentierterBankaufwand(row=rls, rueckgabe_datum=date(2026, 4, 10))])
    pdf_bytes = _erzeugen(kontoblatt)
    text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf_bytes)).pages)
    assert "Rückgabe 10.04.2026" in text
    assert "Erfasst am 18.04.2026" in text
    assert "Rückgabe 18.04.2026" not in text


def test_dokumentierter_bankaufwand_ohne_auffindbare_op_zeigt_datum_unbekannt():
    """Fehlt der Bezug zur Rücklastschrift-OP-Zeile (z. B. Dateninkonsistenz),
    wird NIE ein Datum erfunden - "Datum unbekannt" statt eines falschen
    Ersatzwerts wie `gebucht_am`."""

    rls = RuecklastschriftkostenTable(
        vertrag_id="V-601-1", ruecklastschrift_op_position_id=999, bankentgelt_cent=700,
        rueckgabeursache="Mangels Deckung", dem_zahlungspflichtigen_zurechenbar=False,
        zurechenbarkeit_begruendung="noch unklar", beleg_referenz="BELEG-OHNE-BEZUG", kosten_op_position_id=None,
        erstellt_von="codex-test",
    )
    kontoblatt = _leeres_kontoblatt(dokumentierte_bankaufwaende=[DokumentierterBankaufwand(row=rls, rueckgabe_datum=None)])
    pdf_bytes = _erzeugen(kontoblatt)
    text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf_bytes)).pages)
    assert "Datum unbekannt" in text


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


def test_zahlung_und_retourkapital_erscheinen_im_gemeinsamen_journal():
    """Auftrag HV-20261022-EIN-KONTOBLATT: Zahlung und Retourkapital
    (Rücklastschrift) erscheinen als eigene Buchungsarten im EINEN
    chronologischen Journal - keine separate Kategorie-Überschrift mehr."""

    zahlung = KontoblattZeile(position=_op(1, "ZAHLUNG", 50_000, referenz="Banküberweisung"), kategorie=KATEGORIE_HAUPTFORDERUNG, rest_cent=None)
    ruecklastschrift = KontoblattZeile(position=_op(2, "RUECKLASTSCHRIFT", 50_000, referenz="Rückbuchung"), kategorie=KATEGORIE_HAUPTFORDERUNG, rest_cent=None)
    kontoblatt = _leeres_kontoblatt(zahlungen_zeilen=[zahlung, ruecklastschrift], zahlungen_saldo_cent=0)
    pdf_bytes = _erzeugen(kontoblatt)
    text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf_bytes)).pages)
    assert "Kontoblatt" in text and "chronologisches Journal" in text
    assert "Zahlung" in text
    assert "Retourkapital" in text and "Rücklastschrift" in text
    assert "Banküberweisung" in text
    assert "Rückbuchung" in text


def test_kostenzeile_zeigt_status_offen_teilbezahlt_bezahlt():
    """Codex-Abnahme 20.10.2026, P3: `rest=None` darf nicht mehr pauschal
    als "bezahlt" missverstanden werden - das PDF muss offen/teilbezahlt/
    bezahlt ausdrücklich je Zeile ausweisen."""

    offen = KontoblattZeile(position=_op(1, "SOLL", 2000, referenz="Mahnspesen offen"), kategorie=KATEGORIE_MAHNSPESEN, rest_cent=2000, status=STATUS_OFFEN)
    teil = KontoblattZeile(position=_op(2, "SOLL", 2000, referenz="Mahnspesen teilbezahlt"), kategorie=KATEGORIE_MAHNSPESEN, rest_cent=800, status=STATUS_TEILBEZAHLT)
    bezahlt = KontoblattZeile(position=_op(3, "SOLL", 2000, referenz="Mahnspesen bezahlt"), kategorie=KATEGORIE_MAHNSPESEN, rest_cent=None, status=STATUS_BEZAHLT)
    kontoblatt = _leeres_kontoblatt(
        kosten_zeilen=[offen, teil, bezahlt], kosten_cent=6000, kosten_offener_rest_cent=2800,
        saldo_cent=6000, faelliger_unstrittiger_rest_cent=6000,
    )
    pdf_bytes = _erzeugen(kontoblatt)
    # Zeilenumbrüche normalisiert (`multi_cell` kann eine lange Bezeichnung
    # mitten im "Status: <Wort>"-Text umbrechen - kein Verlust, nur Umbruch,
    # siehe Mehrseiten-Regressionstest oben für die "nicht abgeschnitten"-
    # Garantie selbst).
    # Auftrag HV-20261022-EIN-KONTOBLATT: die neue zusammengefasste
    # Summenzeile ("Hauptforderung gebucht: ... · Kosten gebucht: ...")
    # wird von `multi_cell` per Wortabstand gesetzt - `pypdf.extract_text`
    # fügt dabei vereinzelt zusätzliche Leerzeichen zwischen Wörtern ein;
    # zusätzlich zu Zeilenumbrüchen werden daher auch mehrfache
    # Leerzeichen auf eines normalisiert (kein Inhaltsverlust).
    text = " ".join(" ".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf_bytes)).pages).replace("\n", " ").split())
    assert "Status: offen" in text
    assert "Status: teilbezahlt" in text
    assert "Status: bezahlt" in text
    assert "Kosten gebucht" in text
    assert "60,00 EUR" in text  # gebuchte Sollsumme
    assert "28,00 EUR" in text  # tatsächlich noch offener Rest


def test_kosten_historie_zeigt_stornierte_und_ersetzte_zeilen_getrennt_von_aktueller_summe():
    """Codex-Abnahme 20.10.2026, P3: stornierte/ersetzte Kostenzeilen
    bleiben in einer eigenen Historie sichtbar - NICHT Teil der
    aktuellen Kostensumme."""

    ersetzt = KontoblattZeile(position=_op(10, "SOLL", 2000, referenz="Alte Mahnspesenberechnung"), kategorie=KATEGORIE_MAHNSPESEN, rest_cent=None, status=STATUS_ERSETZT)
    aktive_kosten = KontoblattZeile(position=_op(11, "SOLL", 1000, referenz="Korrigierte Mahnspesen"), kategorie=KATEGORIE_MAHNSPESEN, rest_cent=1000, status=STATUS_OFFEN)
    kontoblatt = _leeres_kontoblatt(
        kosten_zeilen=[aktive_kosten], kosten_cent=1000, kosten_offener_rest_cent=1000, kosten_historie=[ersetzt],
        saldo_cent=1000, faelliger_unstrittiger_rest_cent=1000,
    )
    pdf_bytes = _erzeugen(kontoblatt)
    # Siehe Kommentar im vorigen Test: mehrfache Leerzeichen aus der neuen
    # Summenzeile werden zusätzlich zu Zeilenumbrüchen normalisiert.
    text = " ".join("".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf_bytes)).pages).replace("\n", " ").split())
    # Auftrag HV-20261022-EIN-KONTOBLATT: keine eigene "Kosten-Historie"-
    # Überschrift mehr - die stornierte/ersetzte Zeile erscheint
    # chronologisch im EINEN Journal, ausdrücklich als saldo-neutral
    # markiert.
    assert "wirkt sich NICHT auf den laufenden Saldo aus" in text
    assert "Status: ersetzt" in text
    assert "Alte Mahnspesenberechnung" in text
    assert "Kosten gebucht" in text
    assert "10,00 EUR" in text  # korrigierter, aktueller Betrag (Kosten gesamt)
    assert "20,00 EUR" in text  # historischer (nicht mehr gültiger) Betrag taucht trotzdem auf


def test_pdf_zeigt_versandnachweis_und_zugrunde_liegende_forderungen():
    """Zusatz Zinszuordnung: Mahnbeleg/Versandnachweis und die zugrunde
    liegenden Forderungs-OP-IDs (`forderung_op_position_ids`) sind
    bereits bestehende `MahnkostenBuchungTable`-Ledgerdetails - müssen im
    PDF bei der jeweiligen Kostenzeile sichtbar sein."""

    buchung = _buchung(gebuehr_op_position_id=1, versandnachweis_referenz="mahnung:V-601-1:stufe1:2026-09-15", forderung_op_position_ids="[100, 101]")
    ledger = MahnkostenLedgerEintrag(buchung=buchung, versandnachweis_referenz=buchung.versandnachweis_referenz, forderung_op_position_ids=[100, 101])
    gebuehr = KontoblattZeile(position=_op(1, "SOLL", 2000, referenz="Mahnspesen"), kategorie=KATEGORIE_MAHNSPESEN, rest_cent=2000, status=STATUS_OFFEN, ledger=ledger)
    kontoblatt = _leeres_kontoblatt(kosten_zeilen=[gebuehr], kosten_cent=2000, kosten_offener_rest_cent=2000, saldo_cent=2000, faelliger_unstrittiger_rest_cent=2000)

    pdf_bytes = _erzeugen(kontoblatt)
    text = " ".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf_bytes)).pages).replace("\n", " ")
    assert "mahnung:V-601-1:stufe1:2026-09-15" in text
    assert "#100" in text and "#101" in text


def test_pdf_fehlendes_ledger_zeigt_ehrlich_fehlend():
    """Fehlt ausnahmsweise die `MahnkostenBuchungTable`-Zuordnung zu einer
    Verzugszinsen-/Mahnspesen-/Mahnpauschale-Zeile, muss das PDF das
    AUSDRÜCKLICH als "fehlend" zeigen - nie ein leeres Feld oder einen
    erfundenen Beleg."""

    gebuehr_ohne_ledger = KontoblattZeile(position=_op(1, "SOLL", 2000, referenz="Mahnspesen"), kategorie=KATEGORIE_MAHNSPESEN, rest_cent=2000, status=STATUS_OFFEN, ledger=None)
    kontoblatt = _leeres_kontoblatt(kosten_zeilen=[gebuehr_ohne_ledger], kosten_cent=2000, kosten_offener_rest_cent=2000, saldo_cent=2000, faelliger_unstrittiger_rest_cent=2000)

    pdf_bytes = _erzeugen(kontoblatt)
    text = " ".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf_bytes)).pages).replace("\n", " ")
    assert "Mahnbeleg/Ledger: fehlend" in text


def test_pdf_zinszeile_trennt_kumulatives_segment_von_tatsaechlichem_delta():
    """Kernregel der Zinszuordnung im PDF: das vollständige, kumulative
    Segment-Bild und das tatsächlich an diesem Tag neu gebuchte Delta
    müssen BEIDE sichtbar und UNTERSCHEIDBAR sein - das Delta darf NIE
    mit der kumulativen Summe verwechselt werden."""

    buchung = _buchung(zinsen_op_position_id=5, versandnachweis_referenz="mahnung:V-601-1:stufe2:2026-09-20")
    zuordnung = ZinsZuordnungZeile(
        op_position_id=100,
        segmente=[{"von": "2026-07-01", "bis": "2026-09-15", "satz_prozent": "4.000", "quelle": "VEREINBART_GEPRUEFT", "rest_cent": 50_000, "zinsen_cent": 417}],
        delta_cent=83,
    )
    ledger = MahnkostenLedgerEintrag(buchung=buchung, versandnachweis_referenz=buchung.versandnachweis_referenz, forderung_op_position_ids=[100], zins_zuordnung=[zuordnung])
    zinsen = KontoblattZeile(position=_op(5, "SOLL", 83, referenz="Verzugszinsen"), kategorie=KATEGORIE_VERZUGSZINSEN, rest_cent=83, status=STATUS_OFFEN, ledger=ledger)
    kontoblatt = _leeres_kontoblatt(kosten_zeilen=[zinsen], kosten_cent=83, kosten_offener_rest_cent=83, saldo_cent=83, faelliger_unstrittiger_rest_cent=83)

    pdf_bytes = _erzeugen(kontoblatt)
    text = " ".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf_bytes)).pages).replace("\n", " ")
    assert "Forderung #100" in text
    assert "auf Basis 500,00 EUR" in text  # (1): welcher Außenstand verzinst wurde
    assert "4,17 EUR kumulativ" in text  # das vollständige kumulative Segment-Bild
    assert "tatsächlich an diesem Tag neu gebucht: 0,83 EUR" in text  # NICHT 4,17 EUR


def test_pdf_zinszeile_ohne_gebuchtes_delta_zeigt_ehrlich_zuordnung_nicht_belegt():
    """Eine zugrunde liegende Forderung ohne an diesem Tag gebuchtes
    Delta darf NIE als 0,00 EUR erscheinen - UND NIE pauschal "nicht
    gebucht" heißen (unabhängige Prüfung Commit 8ae0406, (4)): die
    Zinsbuchung INSGESAMT kann sehr wohl erfolgt sein, nur ohne
    nachvollziehbare Zuordnung zu GENAU dieser Forderung - deshalb
    "Zuordnung/Betrag nicht belegt"."""

    buchung = _buchung(zinsen_op_position_id=5)
    zuordnung = ZinsZuordnungZeile(
        op_position_id=200,
        segmente=[{"von": "2026-07-01", "bis": "2026-09-15", "satz_prozent": "4.000", "quelle": "VEREINBART_GEPRUEFT", "rest_cent": 30_000, "zinsen_cent": 200}],
        delta_cent=None,
    )
    ledger = MahnkostenLedgerEintrag(buchung=buchung, versandnachweis_referenz=buchung.versandnachweis_referenz, forderung_op_position_ids=[200], zins_zuordnung=[zuordnung])
    zinsen = KontoblattZeile(position=_op(5, "SOLL", 0, referenz="Verzugszinsen"), kategorie=KATEGORIE_VERZUGSZINSEN, rest_cent=None, status=None, ledger=ledger)
    kontoblatt = _leeres_kontoblatt(kosten_zeilen=[zinsen], kosten_cent=0, kosten_offener_rest_cent=0, saldo_cent=0, faelliger_unstrittiger_rest_cent=0)

    pdf_bytes = _erzeugen(kontoblatt)
    text = " ".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf_bytes)).pages).replace("\n", " ")
    assert "Forderung #200" in text
    assert "tatsächlich an diesem Tag neu gebucht: Zuordnung/Betrag nicht belegt" in text
    assert "auf Basis 300,00 EUR" in text  # die tatsächlich verzinste Außenstandsbasis des Segments


def test_pdf_kopf_bricht_lange_firmen_objekt_einheit_bezeichnung_um_ohne_ueberlappung():
    """Codex-Abnahme 20.10.2026, P4: eine lange, reale Firmen-/Objekt-/
    Einheitsbezeichnung lief bislang über die feste Kopf-Zelle hinaus
    (potenziell über das Logo). Geometrisch geprüft: die letzte
    Titelzeile und der nachfolgende Fließtext überlappen NICHT, und der
    Fließtext ist vollständig lesbar - kein neues Logo/keine neue Farbe."""

    absender = _absender(
        name="JLB Projects Gesellschaft mit beschränkter Haftung und sehr langem Zusatz",
    )
    kontoblatt = _leeres_kontoblatt(
        hauptforderung_zeilen=[KontoblattZeile(position=_op(1, "SOLL", 100_000, referenz="HMZ"), kategorie=KATEGORIE_HAUPTFORDERUNG, rest_cent=None)],
        hauptforderung_cent=100_000, saldo_cent=100_000, faelliger_unstrittiger_rest_cent=100_000,
    )
    pdf_bytes = _erzeugen(
        kontoblatt, absender=absender,
        objekt_bezeichnung="Ein außergewöhnlich lang ausgeschriebenes Objekt mit vielen zusätzlichen Worten",
        einheit_bezeichnung="Eine ebenso außergewöhnlich lang ausgeschriebene Einheitsbezeichnung mit Zusatz",
    )
    reader = PdfReader(io.BytesIO(pdf_bytes))
    page = reader.pages[0]

    pt_je_mm = 72.0 / 25.4
    hoehe_pt = float(page.mediabox.height)
    treffer: list[tuple[float, str]] = []

    def visitor(text, cm, tm, font_dict, font_size):
        if text.strip():
            treffer.append(((hoehe_pt - tm[5]) / pt_je_mm, text))

    page.extract_text(visitor_text=visitor)
    assert treffer, "Die Seite ist nicht leer - der Test prüft etwas Echtes."

    # "Vertrag ..." ist die erste Zeile des Fließtexts NACH dem Kopf.
    fliesstext_treffer = [(y, t) for y, t in treffer if t.strip().startswith("Vertrag")]
    assert fliesstext_treffer, "Fließtext nach dem Kopf wurde nicht gefunden."
    fliesstext_y = fliesstext_treffer[0][0]

    kopf_treffer = [(y, t) for y, t in treffer if y < fliesstext_y]
    assert len(kopf_treffer) > 1  # der Titel ist TATSÄCHLICH mehrzeilig umgebrochen
    letzte_kopfzeile_y = max(y for y, _ in kopf_treffer)
    # Der Fließtext beginnt NICHT überlappend mit der letzten Kopfzeile -
    # mindestens eine Zeilenhöhe (hier konservativ 5mm) Abstand.
    assert fliesstext_y - letzte_kopfzeile_y >= 5.0

    gesamter_text = "".join(p.extract_text() for p in reader.pages)
    assert "Ein außergewöhnlich lang ausgeschriebenes Objekt mit vielen zusätzlichen Worten" in gesamter_text.replace("\n", " ")
    assert "1.000,00 EUR" in gesamter_text  # Betrag bleibt trotz langer Kopfzeile lesbar/vollständig


def test_pdf_kopf_reserviert_genug_hoehe_fuer_quadratisches_produktionslogo(tmp_path):
    """Letzter visueller P4-Befund aus einer echten geschützten Kopie: das
    TATSÄCHLICHE Produktionslogo ist QUADRATISCH (32-mm-Breite => 32-mm-
    Höhe), nicht ein flaches Wortbild wie zuvor angenommen. Der frühere
    Fallback-Wert (22 mm) reichte dafür NICHT - auf Folgeseiten liefen
    Trennlinie und der erste rechte Betrag/Rest durch das Logo. Geprüft
    mit einem ECHTEN quadratischen 32×32-mm-Logo (nicht nur einer
    falschen 22-mm-Annahme): KEIN Textinhalt darf innerhalb des
    tatsächlichen Logo-Rechtecks (x- UND y-Bereich) auf einer Folgeseite
    liegen."""

    from PIL import Image

    logo_pfad = tmp_path / "quadratlogo.png"
    Image.new("RGB", (300, 300), (10, 10, 10)).save(logo_pfad)
    absender = _absender(logo_pfad=str(logo_pfad))

    # Genug Zeilen, um sicher auf Seite 2 umzubrechen (Logo erscheint auf
    # JEDER Seite, der Bug betrifft also jede Folgeseite gleichermaßen).
    # `rest_cent` bewusst gesetzt (nicht `None`): die dadurch gefüllte
    # "Offener Rest"-Spalte liegt vollständig innerhalb der x-Spanne des
    # Logos (siehe `logo_x_links` unten) - genau DAS war der konkret
    # gemeldete Befund ("der erste Zahlungsbetrag rechts unter dem
    # Logo").
    viele_zeilen = [
        KontoblattZeile(position=_op(i, "SOLL", 1000 * i, referenz=f"Position {i:03d}"), kategorie=KATEGORIE_HAUPTFORDERUNG, rest_cent=1000 * i)
        for i in range(1, 60)
    ]
    gesamtsumme = sum(z.position.betrag_cent for z in viele_zeilen)
    kontoblatt = _leeres_kontoblatt(
        hauptforderung_zeilen=viele_zeilen, hauptforderung_cent=gesamtsumme,
        saldo_cent=gesamtsumme, faelliger_unstrittiger_rest_cent=gesamtsumme,
    )
    pdf_bytes = _erzeugen(kontoblatt, absender=absender)
    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages) > 1, "Der Test braucht tatsächlich eine Folgeseite."

    # Geometrie des quadratischen Logos (dieselben Konstanten wie im
    # Produktionscode, hier bewusst als Erwartungswerte dupliziert, damit
    # der Test eine ECHTE Regression gegen die tatsächliche Logo-Fläche
    # prüft, nicht gegen die (zuvor falsche) 22-mm-Annahme selbst).
    logo_y_oben, logo_breite, logo_hoehe = 10.0, 32.0, 32.0
    logo_x_links = 210.0 - 20.0 - logo_breite  # _SEITE_BREITE - _RAND - _LOGO_BREITE
    logo_y_unten = logo_y_oben + logo_hoehe

    pt_je_mm = 72.0 / 25.4
    for seite in reader.pages[1:]:
        hoehe_pt = float(seite.mediabox.height)
        treffer: list[tuple[float, float, str]] = []

        def visitor(text, cm, tm, font_dict, font_size, _treffer=treffer, _hoehe_pt=hoehe_pt):
            if text.strip():
                _treffer.append((tm[4] / pt_je_mm, (_hoehe_pt - tm[5]) / pt_je_mm, text))

        seite.extract_text(visitor_text=visitor)
        assert treffer, "Die Seite ist nicht leer - der Test prüft etwas Echtes."
        im_logo_bereich = [
            (x, y, t) for x, y, t in treffer
            if x + 1.0 >= logo_x_links and y < logo_y_unten  # kleine Toleranz für Textbreite/Rundung
        ]
        assert im_logo_bereich == [], (
            f"Textinhalt überlappt das tatsächliche 32×32-mm-Logo-Rechteck: {im_logo_bereich}"
        )


def test_journal_hat_spaltenkoepfe_datum_buchung_betrag_saldo():
    """Fix7 (unabhängige Rückprüfung Codex 22.10.2026, Sichtprüfung des
    kanonischen PDFs): zwei Beträge standen nebeneinander ohne
    Kennzeichnung, welcher der Buchungsbetrag und welcher der laufende
    Saldo ist - jetzt mit expliziten Spaltenköpfen."""

    kontoblatt = _leeres_kontoblatt(
        hauptforderung_zeilen=[KontoblattZeile(position=_op(1, "EROEFFNUNG", 100_000), kategorie=KATEGORIE_HAUPTFORDERUNG, rest_cent=None)],
        hauptforderung_cent=100_000, saldo_cent=100_000, faelliger_unstrittiger_rest_cent=100_000,
    )
    pdf_bytes = _erzeugen(kontoblatt)
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = reader.pages[0].extract_text()
    assert "Datum" in text
    assert "Buchung" in text
    assert "Betrag" in text
    assert "Saldo" in text


def test_journal_spaltenkoepfe_wiederholen_sich_auf_folgeseite():
    """Fix7: 'bei Fortsetzung sinnvoll wiederholen' - ein mehrseitiges
    Journal zeigt die Spaltenköpfe auf JEDER Seite, nicht nur auf der
    ersten."""

    viele_zeilen = [
        KontoblattZeile(position=_op(i, "SOLL", 1000 * i, referenz=f"Position {i:03d}"), kategorie=KATEGORIE_HAUPTFORDERUNG, rest_cent=1000 * i)
        for i in range(1, 60)
    ]
    gesamtsumme = sum(z.position.betrag_cent for z in viele_zeilen)
    kontoblatt = _leeres_kontoblatt(
        hauptforderung_zeilen=viele_zeilen, hauptforderung_cent=gesamtsumme,
        saldo_cent=gesamtsumme, faelliger_unstrittiger_rest_cent=gesamtsumme,
    )
    pdf_bytes = _erzeugen(kontoblatt)
    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages) > 1, "Der Test braucht tatsächlich eine Folgeseite."
    for seite in reader.pages[1:]:
        text = seite.extract_text()
        assert "Datum" in text and "Buchung" in text and "Saldo" in text


def _manualimport_zeile(*, id: int, beleg_referenz: str = "SYNRENT-1") -> ManuellerMahnversandTable:
    from datetime import date as _date

    return ManuellerMahnversandTable(
        id=id, vertrag_id="V-601-1", stufe=1, forderung_op_position_ids_json="[1]",
        versanddatum=_date(2026, 3, 1), kanal="BRIEF", empfaenger_name="Max Mustermieter",
        empfaenger_adresse="Musterstraße 1\n1010 Wien", beleg_referenz=beleg_referenz,
        beleg_pdf_bytes=b"", beleg_sha256="abc", idempotenz_schluessel=f"KEY-{id}",
        erfasst_von="codex-test", mahnkosten_buchung_id=None, pauschale_abgegolten=False,
    )


def test_pdf_zeigt_ausstehenden_manualimport_fix6():
    """Fix6: ein Manualimport OHNE Kosten-Freigabe erschien bisher
    NIRGENDS auf dem PDF-Kontoauszug."""

    kontoblatt = _leeres_kontoblatt(
        ausstehende_manuelle_mahnversand=[
            AusstehenderManuellerMahnversand(row=_manualimport_zeile(id=1, beleg_referenz="SYNRENT-MAHNUNG-XY"), vorschlag_cent=None),
        ],
    )
    pdf_bytes = _erzeugen(kontoblatt)
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = "".join(p.extract_text() for p in reader.pages)
    assert "SYNRENT-MAHNUNG-XY" in text
    assert "noch nicht gebucht" in text


def test_pdf_zeigt_unverbindlichen_ansatz_fuer_ausstehenden_manualimport():
    """Zweite Rückprüfung Codex 22.10.2026, echter Anzeige-Mangel: das
    importierte, noch nicht freigegebene Schreiben stand zwar mit
    Referenz/Datum im Kontoblatt, aber OHNE den vorgesehenen Ansatz
    (z. B. 20,00 EUR) aus der aktuell geltenden Konfiguration - "ungeprüfte
    Ansätze sichtbar" verlangt ausdrücklich auch den Betrag selbst, als
    unverbindliche "in Prüfung"-Anzeige, NICHT im Saldo, nicht gebucht."""

    kontoblatt = _leeres_kontoblatt(
        ausstehende_manuelle_mahnversand=[
            AusstehenderManuellerMahnversand(
                row=_manualimport_zeile(id=2, beleg_referenz="SYN-PENDING-LETTER"), vorschlag_cent=2_000,
            ),
        ],
    )
    pdf_bytes = _erzeugen(kontoblatt)
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = "".join(p.extract_text() for p in reader.pages)
    assert "SYN-PENDING-LETTER" in text
    assert "20,00" in text
    assert "in Prüfung" in text
