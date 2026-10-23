"""Gezielte Regressionstests zu den unabhängig rückgeprüften Befunden
Codex 22.10.2026 Fix4/Fix5 (`mahnwesen/kontoauszug_anhang.py`) -
synthetische Daten, keine echten Belege/Provideraufrufe."""

from __future__ import annotations

from datetime import date

import pytest

from mietinkasso.infrastructure.db.tables import OPPositionTable
from mietinkasso.mahnwesen.kontoauszug_anhang import haenge_kontoauszug_pdf_an, journal_als_text
from mietinkasso.op.kontoblatt import erstelle_kontoblatt


def _op(id: int, typ: str, betrag_cent: int, *, belegdatum: date = date(2026, 9, 1), beleg_referenz: str | None = None) -> OPPositionTable:
    return OPPositionTable(
        id=id, konto_id="K-1", typ=typ, betrag_cent=betrag_cent, belegdatum=belegdatum, buchungsdatum=belegdatum,
        faelligkeit=None, faelligkeit_bekannt=False, status="AKTIV", quelle_system=None,
        storniert_durch_id=None, beleg_referenz=beleg_referenz,
    )


def _kontoblatt(*positionen: OPPositionTable):
    saldo = sum((p.betrag_cent if p.typ == "SOLL" else -p.betrag_cent) for p in positionen)
    return erstelle_kontoblatt(
        konto_id="K-1", saldo_cent=saldo, faelliger_unstrittiger_rest_cent=saldo,
        positionen_aktiv=list(positionen), positionen_alle=list(positionen),
        offene_forderungen_rest_je_id={}, mahnkosten_buchungen=[], ruecklastschriftkosten_zeilen=[], mahnlaeufe=[],
    )


def test_journal_als_text_zeigt_menschliche_buchungsbeschreibung_ohne_interne_referenzen():
    """Nachfolger-Test zu Fix4 (22.10.2026): jene Fassung zeigte je Zeile
    zusätzlich die rohe `beleg_referenz`/ein `OP #<id>`-Forderungsbezug an.
    Auftrag HV-20261023-MAHNUNG-EINFACH (23.10.2026) fordert das GENAUE
    GEGENTEIL für einen Empfängertext: keine OP-IDs, keine Beleg-/
    Importschlüssel, keine DB-Referenzketten - stattdessen eine kurze,
    menschlich verständliche Buchungsbeschreibung
    (`erstelle_einfache_tabelle`/`_einfache_buchung_text`)."""

    hauptforderung = _op(
        1, "SOLL", 100_000, belegdatum=date(2026, 1, 1), beleg_referenz="SYNRENT-2026-01",
    )
    hauptforderung.leistungsperiode = "2026-01"
    kontoblatt = _kontoblatt(hauptforderung)

    text = journal_als_text(kontoblatt, heute=date(2026, 1, 15))

    assert "Mietvorschreibung Jänner 2026" in text
    assert "SYNRENT-2026-01" not in text
    assert "OP #1" not in text
    assert "Gesamt offen: 1.000,00 EUR" in text


def test_haenge_kontoauszug_pdf_an_fail_closed_statt_fehler_schlucken():
    """Fix5: ein kaputter Kontoauszug darf NIE zu einem scheinbar
    erfolgreichen Versandpaket OHNE die Pflichtbeilage führen - die
    vorherige Fassung fing jede Ausnahme ab und lieferte den
    unveränderten Brief zurück. Jetzt: ValueError VOR jedem
    Providerkontakt, sichtbar, kein stilles Weiterlaufen."""

    gueltiger_brief = _echtes_minimal_pdf()
    kaputter_kontoauszug = b"DAS-IST-KEIN-PDF"

    with pytest.raises(ValueError):
        haenge_kontoauszug_pdf_an(gueltiger_brief, kaputter_kontoauszug)


def test_haenge_kontoauszug_pdf_an_funktioniert_weiterhin_mit_gueltigen_pdfs():
    brief = _echtes_minimal_pdf()
    kontoauszug = _echtes_minimal_pdf()

    ergebnis = haenge_kontoauszug_pdf_an(brief, kontoauszug)

    from pypdf import PdfReader
    import io

    seiten = len(PdfReader(io.BytesIO(ergebnis)).pages)
    assert seiten == 2


def _echtes_minimal_pdf() -> bytes:
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    puffer = io.BytesIO()
    writer.write(puffer)
    return puffer.getvalue()


def test_journal_als_text_zeigt_davon_jetzt_zu_zahlen_bei_abweichendem_faelligem_teilbetrag():
    """Gegenlesen von d52948f, P1: derselbe "Davon jetzt zu zahlen"-
    Mechanismus wie im Brief-PDF, hier für die E-Mail-Tabelle."""

    faellig = _op(1, "SOLL", 10_000, belegdatum=date(2026, 1, 1))
    spaeter = _op(2, "SOLL", 90_000, belegdatum=date(2026, 2, 1))
    kontoblatt = _kontoblatt(faellig, spaeter)

    text = journal_als_text(kontoblatt, heute=date(2026, 1, 20), faelliger_betrag_cent=10_000)

    assert "Gesamt offen: 1.000,00 EUR" in text
    assert "Davon jetzt zu zahlen: 100,00 EUR" in text


def test_journal_als_text_ohne_abweichung_zeigt_keine_zusatzzeile():
    miete = _op(1, "SOLL", 10_000, belegdatum=date(2026, 1, 1))
    kontoblatt = _kontoblatt(miete)

    text = journal_als_text(kontoblatt, heute=date(2026, 1, 20), faelliger_betrag_cent=10_000)

    assert "Davon jetzt zu zahlen" not in text
