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


def test_journal_als_text_zeigt_belegreferenz_und_forderungsbezug_nicht_nur_buchungsart():
    """Fix4: die bisherige Fassung zeigte je Zeile NUR die generische
    Buchungsart ("Miete/Vorschreibung" + Datum) - der tatsächliche
    Belegbezug (hier "SYNRENT") ging unter. Das ist jetzt lesbar
    enthalten, keine stille Kürzung."""

    hauptforderung = _op(1, "SOLL", 100_000, belegdatum=date(2026, 1, 1), beleg_referenz="SYNRENT-2026-01")
    kontoblatt = _kontoblatt(hauptforderung)

    text = journal_als_text(kontoblatt)

    assert "SYNRENT-2026-01" in text
    assert "OP #1" in text  # forderungsbezug einer reinen Hauptforderungszeile ohne Ledger


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
