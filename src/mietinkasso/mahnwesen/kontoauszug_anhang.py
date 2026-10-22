"""Auftrag HV-20261022-EIN-KONTOBLATT, unabhängiger Nachbesserungshinweis
Markus (22.10.2026): "bisherige Mail-/Briefpipeline nutzt noch keinen
kanonischen Kontoauszug" - bis hierher existierte der PDF-Kontoauszug NUR
als separater Backoffice-Download, NICHT als Teil des tatsächlich an den
Mieter versendeten Schreibens. Dieses Modul verdrahtet GENAU DAS: der
EINE kanonische Kontoauszug (`op/kontoblatt.py::lade_kontoblatt` +
`op/kontoauszug_pdf.py::erzeuge_kontoauszug_pdf`, dieselbe Quelle wie
Kontoansicht/Mieterakte) wird UNVERÄNDERLICH Teil des tatsächlichen
Mahnschreibens:

- Kanal BRIEF: die Kontoauszug-Seiten werden an das Mahnbrief-PDF
  ANGEHÄNGT (`haenge_kontoauszug_pdf_an`), BEVOR es archiviert/übertragen
  wird - EIN physisches Dokument, kein zweiter separater Anhang, der
  verloren gehen könnte. Die archivierten Bytes (`MahnbriefArchivTable.
  pdf_bytes`) sind damit die unveränderliche, vollständige Kopie dessen,
  was tatsächlich verschickt wurde.
- Kanal EMAIL: dieser Codebase-Mailclient (`mailops_client.py::
  MailOpsAuftrag`) unterstützt KEINE Dateianhänge (reiner Text) - statt
  eine nicht existierende Anhangsfähigkeit vorzutäuschen, wird derselbe
  kanonische Journalinhalt als TEXT-Kurzfassung (`journal_als_text`) in
  den E-Mail-Text eingebettet.

Beide Wege lesen AUSSCHLIESSLICH über `lade_kontoblatt`/`erstelle_journal`
- es entsteht NIE eine zweite, unabhängig berechnete Darstellung des
Kontostands."""

from __future__ import annotations

import io

from pypdf import PdfReader, PdfWriter

from mietinkasso.op.kontoblatt import Kontoblatt, erstelle_journal


def haenge_kontoauszug_pdf_an(brief_pdf_bytes: bytes, kontoauszug_pdf_bytes: bytes) -> bytes:
    """Fügt ALLE Seiten von `kontoauszug_pdf_bytes` HINTER die Seiten von
    `brief_pdf_bytes` an - reine PDF-Seitenzusammenführung (kein erneutes
    Rendern, kein Eingriff in den Brief-Inhalt selbst). Gibt bei einem
    nicht lesbaren Kontoauszug-PDF den unveränderten `brief_pdf_bytes`
    zurück (der Brief selbst darf NIE an einem defekten Anhang scheitern) -
    das ist bewusst konservativ, da der Brief ohnehin bereits denselben
    Forderungsstand textuell enthält."""

    try:
        writer = PdfWriter()
        for seite in PdfReader(io.BytesIO(brief_pdf_bytes)).pages:
            writer.add_page(seite)
        for seite in PdfReader(io.BytesIO(kontoauszug_pdf_bytes)).pages:
            writer.add_page(seite)
        ausgabe = io.BytesIO()
        writer.write(ausgabe)
        return ausgabe.getvalue()
    except Exception:
        return brief_pdf_bytes


_BUCHUNGSART_SPALTE = 32


def _eur_text(cent: int) -> str:
    """Wie `mailversand_service.py::_eur_text` - bewusst dupliziert
    (gleiche Konvention wie `_effect_cent`/`_eur` an mehreren Stellen
    dieses Moduls), kein modulübergreifender Import eines privaten
    Helfers."""

    return f"{cent / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def journal_als_text(kontoblatt: Kontoblatt) -> str:
    """Plain-Text-Kurzfassung DESSELBEN kanonischen Journals
    (`erstelle_journal`) für den Kanal EMAIL (kein PDF-Anhang möglich,
    siehe Moduldoc) - identische Zeilen/Reihenfolge/Beträge wie die
    HTML-/PDF-Darstellung, nur als reiner Text. Storno-/Historie-Zeilen
    sind als solche markiert und verändern den gezeigten laufenden Saldo
    nicht."""

    journal = erstelle_journal(kontoblatt)
    if not journal:
        return "Keine Buchungen vorhanden."
    zeilen = ["Datum       Buchungsart" + " " * (_BUCHUNGSART_SPALTE - len("Buchungsart")) + "   Betrag        Saldo"]
    for jz in journal:
        betrag_cent = jz.soll_cent if jz.soll_cent else -jz.haben_cent
        markierung = " (storniert/ersetzt - nicht im Saldo)" if not jz.beeinflusst_saldo else ""
        buchungsart = jz.buchungsart[: _BUCHUNGSART_SPALTE].ljust(_BUCHUNGSART_SPALTE)
        zeilen.append(
            f"{jz.datum.isoformat()}  {buchungsart}   {_eur_text(betrag_cent):>10} EUR  "
            f"{_eur_text(jz.laufender_saldo_cent):>10} EUR{markierung}"
        )
    zeilen.append(f"\nSchlussaldo: {_eur_text(kontoblatt.saldo_cent)} EUR")
    return "\n".join(zeilen)
