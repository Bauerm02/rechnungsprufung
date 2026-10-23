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

from datetime import date

from mietinkasso.op.kontoblatt import Kontoblatt, erstelle_einfache_tabelle, erweitere_um_vorschau


def haenge_kontoauszug_pdf_an(brief_pdf_bytes: bytes, kontoauszug_pdf_bytes: bytes) -> bytes:
    """Fügt ALLE Seiten von `kontoauszug_pdf_bytes` HINTER die Seiten von
    `brief_pdf_bytes` an - reine PDF-Seitenzusammenführung (kein erneutes
    Rendern, kein Eingriff in den Brief-Inhalt selbst).

    **Korrektur nach unabhängiger Rückprüfung Codex 22.10.2026, echter
    Bug:** die vorherige Fassung fing JEDE Ausnahme ab und lieferte
    stillschweigend `brief_pdf_bytes` OHNE den Kontoauszug zurück - der
    Kontoauszug ist aber eine Pflichtbeilage, kein optionales Extra.
    Ein defekter/nicht lesbarer Kontoauszug darf daher NIE zu einem
    scheinbar erfolgreichen, aber unvollständigen Versandpaket führen.
    Diese Funktion ist bewusst fail-closed: jeder Fehler wird als
    `ValueError` VOR jedem Providerkontakt (Archivierung/SFTP/E-Mail-
    Versand) weitergereicht, sodass der GESAMTE Versand blockiert - nicht
    nur stillschweigend ohne Pflichtbeilage fortgesetzt - wird. Die
    Aufrufer (`mailversand_service.py`) behandeln einen `ValueError` hier
    exakt wie jeden anderen vor Netzkontakt geworfenen Fehler: sicher
    nichts wurde gesendet, nichts wurde gebucht, BLOCKIERT."""

    try:
        writer = PdfWriter()
        for seite in PdfReader(io.BytesIO(brief_pdf_bytes)).pages:
            writer.add_page(seite)
        for seite in PdfReader(io.BytesIO(kontoauszug_pdf_bytes)).pages:
            writer.add_page(seite)
        ausgabe = io.BytesIO()
        writer.write(ausgabe)
        return ausgabe.getvalue()
    except Exception as exc:
        raise ValueError(
            "Kontoauszug-PDF (Pflichtbeilage) konnte nicht an das Mahnschreiben angehängt werden - "
            f"Versand wird VOR jedem Providerkontakt blockiert, kein Versand ohne Pflichtbeilage: {exc}"
        ) from exc


_BUCHUNGSART_SPALTE = 46


def _eur_text(cent: int) -> str:
    """Wie `mailversand_service.py::_eur_text` - bewusst dupliziert
    (gleiche Konvention wie `_effect_cent`/`_eur` an mehreren Stellen
    dieses Moduls), kein modulübergreifender Import eines privaten
    Helfers."""

    vorzeichen = "-" if cent < 0 else ""
    return vorzeichen + f"{abs(cent) / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def journal_als_text(
    kontoblatt: Kontoblatt, *, heute: date,
    neue_zinsen_delta_cent: int = 0, zins_von: date | None = None, zins_bis: date | None = None,
    zinssatz_prozent=None, gebuehr_cent: int | None = None, mahnpauschale_cent: int | None = None,
    faelliger_betrag_cent: int | None = None,
    zins_segmente=(), neue_zinsen_delta_je_op_position: dict | None = None,
) -> str:
    """Kurze, menschlich verständliche Kontoauszug-Tabelle für den
    Kanal EMAIL (Auftrag HV-20261023-MAHNUNG-EINFACH, kein PDF-Anhang
    möglich, siehe Moduldoc) - Datum | Buchung | Betrag | Offen,
    chronologisch (ältestes Ereignis oben), abgeleitet AUSSCHLIESSLICH
    aus `erstelle_einfache_tabelle` (keine zweite Berechnung/Engine).
    KEINE OP-IDs/Beleg-/Importschlüssel/Ledger-Interna - diese bleiben
    ausschließlich im internen Backoffice-Kontoblatt sichtbar.

    **Korrektur zu Fix4 (22.10.2026):** jene Fassung zeigte zusätzlich
    `beleg_referenz`/`forderungsbezug` roh an - nach dem ausdrücklichen
    neuen Auftrag HV-20261023-MAHNUNG-EINFACH darf ein Empfängertext
    GENAU das NICHT mehr zeigen (keine Datenbank-/Beleg-Referenzketten).
    Die menschliche Kurzbeschreibung (`erstelle_einfache_tabelle`)
    ersetzt das - fachlich nachvollziehbar (Periode/Zeitraum/Satz/
    Fälligkeit), ohne jede interne Kennung.

    **Nachtrag Markus 23.10.2026 (Abnahme-Hinweis):** die optionalen
    `neue_zinsen_delta_cent`/`gebuehr_cent`/`mahnpauschale_cent`-Parameter
    hängen GENAU dieselben, dem E-Mail-Text bereits bekannten neuen
    Posten (`MahnkostenVorschau`, noch nicht gebucht) an, damit dieser
    Kontoauszug-Block und der übrige E-Mail-Text NIE unterschiedliche
    Gesamtbeträge zeigen (siehe `erweitere_um_vorschau`).

    **Korrektur nach unabhängiger Rückprüfung (Codex, 23.10.2026,
    Gegenlesen von Commit d52948f, echter Fachfehler):** die Tabelle
    zeigt die VOLLSTÄNDIGE Kontohistorie, die auch NICHT mahnbare/noch
    nicht fällige Positionen enthalten kann (z. B. eine spätere
    Folgeperiode) - ihr "Gesamt offen" ist deshalb NICHT automatisch der
    tatsächlich jetzt einforderbare Betrag. `faelliger_betrag_cent` (der
    bereits geprüfte, aus dem bestehenden Mahnkosten-Snapshot stammende
    Teilbetrag, identisch zum Brief) wird - weicht er vom vollen
    Schlusssaldo ab - als zusätzliche, klar benannte Zeile "Davon jetzt
    zu zahlen" angehängt; stimmen beide überein, entfällt die
    Zusatzzeile. `None` (Standard) zeigt sie nie.

    `zins_segmente`/`neue_zinsen_delta_je_op_position` (Ergänzung Markus
    23.10.2026, Abnahme-Hinweis zu Commit 3c6c175): dieselben bereits
    vorhandenen strukturierten `MahnkostenVorschau`-Felder wie Brief/
    Mailtext - durchgereicht an `erweitere_um_vorschau`, damit dieser
    Anhang denselben kumulativ/bereits-gebucht/neu-Fall erklärt wie Brief
    und Mailtext (keine zweite Berechnung)."""

    zeilen_daten = erstelle_einfache_tabelle(kontoblatt)
    zeilen_daten = erweitere_um_vorschau(
        zeilen_daten, heute=heute, neue_zinsen_delta_cent=neue_zinsen_delta_cent,
        zins_von=zins_von, zins_bis=zins_bis, zinssatz_prozent=zinssatz_prozent,
        gebuehr_cent=gebuehr_cent, mahnpauschale_cent=mahnpauschale_cent,
        zins_segmente=zins_segmente, neue_zinsen_delta_je_op_position=neue_zinsen_delta_je_op_position,
    )
    if not zeilen_daten:
        return "Keine Buchungen vorhanden."
    zeilen = [
        "+ erhöht den offenen Betrag, - vermindert ihn.", "",
        "Datum       Buchung" + " " * (_BUCHUNGSART_SPALTE - len("Buchung")) + "   Betrag        Offen",
    ]
    for z in zeilen_daten:
        buchung_text = z.buchung.ljust(_BUCHUNGSART_SPALTE)
        zeilen.append(
            f"{z.datum.strftime('%d.%m.%Y')}  {buchung_text}   {_eur_text(z.betrag_cent):>10} EUR  "
            f"{_eur_text(z.offen_cent):>10} EUR"
        )
    letzter_saldo = zeilen_daten[-1].offen_cent
    if letzter_saldo >= 0:
        zeilen.append(f"\nGesamt offen: {_eur_text(letzter_saldo)} EUR")
    else:
        zeilen.append(f"\nGesamt Guthaben: {_eur_text(-letzter_saldo)} EUR")
    if faelliger_betrag_cent is not None and faelliger_betrag_cent != letzter_saldo:
        zeilen.append(f"Davon jetzt zu zahlen: {_eur_text(faelliger_betrag_cent)} EUR")
    return "\n".join(zeilen)
