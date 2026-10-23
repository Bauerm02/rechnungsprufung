"""Gemeinsamer PDF-Tabellenrenderer für die kurze, menschlich
verständliche Kontoaufstellung (Datum | Buchung | Betrag | Offen).

Nachtrag Markus 23.10.2026 zum Auftrag HV-20261023-MAHNUNG-EINFACH:
"nur das Kontoblatt als zweite PDF-Seite anzuhängen und vorne die alte
Forderungsliste zu lassen erfüllt den Nutzerauftrag nicht - der
eigentliche Mahnbrief braucht die chronologische Tabelle bereits auf
seiner ersten Briefseite." Dieses Modul ist deshalb der EINE
Renderer für BEIDE Empfänger-PDFs:

- `mahnwesen/brief_pdf.py::erzeuge_mahnbrief_pdf` (der Mahnbrief SELBST,
  nicht nur ein angehängter Anhang)
- `op/kontoauszug_pdf.py::erzeuge_kontoauszug_pdf(..., empfaenger_ansicht
  =True)` (der eigenständige Kontoauszug-Download/-Anhang)

- KEINE zweite, unabhängig gepflegte Rendering-/Rechenlogik. Arbeitet
  ausschließlich auf bereits fertigen `EinfacheZeile`-Listen (`op/
  kontoblatt.py::erstelle_einfache_tabelle`/`erweitere_um_vorschau`) -
  erzeugt selbst KEINE Daten/Berechnung, liefert nur den tatsächlich
  gezeigten Schlusssaldo zurück (identisch zur letzten `EinfacheZeile.
  offen_cent`).

Bewusst in einem EIGENEN Modul ohne Import von `brief_pdf.py` ODER
`kontoauszug_pdf.py`, obwohl deren jeweilige `_zeile`/`_journal_
tabellenkopf`-Helfer strukturell sehr ähnlich sind: `kontoauszug_pdf.py`
importiert bereits von `brief_pdf.py` (`Absender`/Fallback-Fonts) - ein
Import in die umgekehrte Richtung (aus `brief_pdf.py` heraus) würde einen
Zirkelimport erzeugen. `familie` (Fontfamilienname, bereits beim
Aufrufer via `pdf.add_font(...)` registriert) UND `anthrazit`/`gold`
sind deshalb reine Parameter statt fest codierter Modulnamen - macht den
Renderer unabhängig davon, welche konkrete `FPDF`-Unterklasse
(`_MahnbriefPDF` mit Fensterkuvert-Layout vs. `_KontoauszugPDF` als
ganzseitiger Bericht) ihn aufruft."""

from __future__ import annotations

from fpdf import FPDF
from fpdf.enums import MethodReturnValue

from mietinkasso.op.kontoblatt import EinfacheZeile

_SEITE_BREITE = 210.0
_RAND = 20.0
_INHALT_RECHTS = _SEITE_BREITE - _RAND
_DATUM_SPALTE_BREITE = 24.0
_BETRAG_SPALTE_BREITE = 32.0
_OFFEN_SPALTE_BREITE = 28.0
_LABEL_SPALTE_BREITE = (_INHALT_RECHTS - _RAND - _DATUM_SPALTE_BREITE) - _BETRAG_SPALTE_BREITE - _OFFEN_SPALTE_BREITE

#: Kundenverständliche Überschrift (Nachtrag Markus 23.10.2026:
#: "Überschrift kundenverständlich, etwa 'Ihre Kontoaufstellung', nicht
#: technisches 'chronologisches Journal'") - gilt NUR für diese
#: Empfänger-Tabelle, NICHT für die interne Vollansicht in
#: `kontoauszug_pdf.py` (die bleibt technisch beschriftet, siehe dort).
UEBERSCHRIFT = "Ihre Kontoaufstellung"
_ERKLAERUNG = "+ erhöht den offenen Betrag, - vermindert ihn."


def _eur(cent: int) -> str:
    vorzeichen = "-" if cent < 0 else ""
    betrag = abs(cent)
    return f"{vorzeichen}{betrag / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " EUR"


def _tabellenkopf(pdf: FPDF, *, familie: str, anthrazit: tuple[int, int, int], gold: tuple[int, int, int]) -> None:
    pdf.set_font(familie, "B", 7.5)
    pdf.set_text_color(*anthrazit)
    x0, y0 = _RAND, pdf.get_y()
    pdf.set_xy(x0, y0)
    pdf.cell(_DATUM_SPALTE_BREITE, 5, "Datum", align="L")
    pdf.set_xy(x0 + _DATUM_SPALTE_BREITE, y0)
    pdf.cell(_LABEL_SPALTE_BREITE, 5, "Buchung", align="L")
    pdf.set_xy(x0 + _DATUM_SPALTE_BREITE + _LABEL_SPALTE_BREITE, y0)
    pdf.cell(_BETRAG_SPALTE_BREITE, 5, "Betrag", align="R")
    pdf.set_xy(x0 + _DATUM_SPALTE_BREITE + _LABEL_SPALTE_BREITE + _BETRAG_SPALTE_BREITE, y0)
    pdf.cell(_OFFEN_SPALTE_BREITE, 5, "Offen", align="R")
    pdf.set_y(y0 + 5)
    pdf.set_draw_color(*gold)
    pdf.line(_RAND, pdf.get_y(), _INHALT_RECHTS, pdf.get_y())
    pdf.ln(1.5)
    pdf.set_font(familie, "", 9.5)


def _sichere_seite_vor_zeile(
    pdf: FPDF, *, familie: str, bezeichnung: str, fett: bool, anthrazit: tuple[int, int, int], gold: tuple[int, int, int],
) -> None:
    """Berechnet die tatsächliche (ggf. mehrzeilige) Höhe der GLEICH
    folgenden Zeile per Dry-Run und bricht VOR dem Zeichnen kontrolliert
    um, inklusive sofort wiederholtem Tabellenkopf.

    **Korrektur nach unabhängiger Rückprüfung (Codex, 23.10.2026,
    Gegenlesen von Commit d52948f, echter Layoutfehler):** die vorherige
    Fassung prüfte `will_page_break`/zeichnete die neue Seite ERST
    INNERHALB von `_zeile` selbst, NACHDEM `_zeile` bereits begonnen
    hatte, die Zeile zu zeichnen - der Aufrufer prüfte danach nur noch
    `page_no() != vorher`, um GEGEBENENFALLS den Tabellenkopf
    nachzuziehen. Das brachte auf der neuen Seite die bereits gezeichnete
    (umgebrochene) Zeile OBEN und den Tabellenkopf ERST DARUNTER - exakt
    umgekehrte Reihenfolge. Jetzt entscheidet der Aufrufer VOR dem
    Zeichnen der Zeile, zeichnet den Seitenumbruch UND den Tabellenkopf
    bei Bedarf selbst, und erst DANACH wird `_zeile` (die selbst KEINEN
    Seitenumbruch mehr prüft) aufgerufen - Kopf steht garantiert vor der
    ersten Zeile jeder Seite."""

    pdf.set_font(familie, "B" if fett else "", 9.5)
    hoehe = max(pdf.multi_cell(_LABEL_SPALTE_BREITE, 5, bezeichnung, align="L", dry_run=True, output=MethodReturnValue.HEIGHT), 5)
    if pdf.will_page_break(hoehe):
        pdf.add_page()
        _tabellenkopf(pdf, familie=familie, anthrazit=anthrazit, gold=gold)


def _zeile(
    pdf: FPDF, *, familie: str, datum_text: str, bezeichnung: str, betrag_cent: int, offen_text: str, fett: bool = False,
) -> None:
    """Zeichnet GENAU eine Zeile an der aktuellen Position - prüft
    SELBST keinen Seitenumbruch mehr (siehe `_sichere_seite_vor_zeile`,
    die das VOR diesem Aufruf übernimmt)."""

    pdf.set_font(familie, "B" if fett else "", 9.5)
    x0, y0 = _RAND, pdf.get_y()
    pdf.set_xy(x0, y0)
    pdf.cell(_DATUM_SPALTE_BREITE, 5, datum_text, align="L")
    pdf.set_xy(x0 + _DATUM_SPALTE_BREITE, y0)
    pdf.multi_cell(_LABEL_SPALTE_BREITE, 5, bezeichnung, align="L", new_x="LMARGIN", new_y="NEXT")
    y1 = pdf.get_y()
    pdf.set_xy(x0 + _DATUM_SPALTE_BREITE + _LABEL_SPALTE_BREITE, y0)
    pdf.cell(_BETRAG_SPALTE_BREITE, 5, _eur(betrag_cent), align="R")
    pdf.set_xy(x0 + _DATUM_SPALTE_BREITE + _LABEL_SPALTE_BREITE + _BETRAG_SPALTE_BREITE, y0)
    pdf.cell(_OFFEN_SPALTE_BREITE, 5, offen_text, align="R")
    pdf.set_y(max(y1, y0 + 5))


def zeichne_einfache_tabelle(
    pdf: FPDF, zeilen: list[EinfacheZeile], *, familie: str, anthrazit: tuple[int, int, int], gold: tuple[int, int, int],
    ueberschrift: str = UEBERSCHRIFT, faelliger_betrag_cent: int | None = None,
) -> int:
    """Zeichnet die kurze Empfänger-Tabelle direkt auf die laufende
    Seite des übergebenen `pdf` - Überschrift, +/- Erklärung,
    Spaltenköpfe, Zeilen mit automatischem Seitenumbruch samt
    wiederholtem Spaltenkopf (siehe `_sichere_seite_vor_zeile` - Kopf
    wird VOR der jeweils umbrechenden Zeile gezeichnet, nicht danach),
    Trennlinie, fette Gesamt-/Guthaben-Zeile. `familie` ist der
    Fontfamilienname, den der Aufrufer bereits per `pdf.add_font(...)`
    registriert hat ("Brief" im Mahnbrief, "Konto" im eigenständigen
    Kontoauszug) - dieser Renderer registriert selbst KEINE Fonts.

    `faelliger_betrag_cent` (Nachtrag Markus 23.10.2026, Gegenlesen von
    d52948f, echter Fachfehler): die Tabelle zeigt bewusst die
    VOLLSTÄNDIGE Kontohistorie inkl. bereits gebuchter, aber ZUM
    HEUTIGEN Zeitpunkt noch NICHT fälliger Positionen (ein Konto kann
    mehrere Perioden mit unterschiedlichen Fälligkeiten tragen) - ihr
    "Gesamt offen" ist deshalb NICHT automatisch der tatsächlich jetzt
    einforderbare Betrag. Weicht `faelliger_betrag_cent` (der bereits
    geprüfte, aus dem bestehenden Mahnkosten-Snapshot stammende,
    tatsächlich mahnbare Betrag) vom vollen Schlusssaldo ab, wird direkt
    darunter eine zusätzliche, deutlich gekennzeichnete Zeile "Davon
    jetzt zu zahlen: X EUR" gezeichnet - NIE ein unbekannter/noch nicht
    fälliger Posten eingefordert. Stimmen beide Beträge überein, entfällt
    die Zusatzzeile (keine redundante Wiederholung derselben Zahl).
    `None` (Standard) zeigt sie nie - für Aufrufer ohne einen
    konkreten, geprüften Mahnlauf-Snapshot (z. B. der reine,
    mahnlaufunabhängige Kontoblatt-Download).

    Liefert den tatsächlich gezeigten VOLLEN Schlusssaldo zurück
    (identisch zur letzten `EinfacheZeile.offen_cent`, 0 bei einer leeren
    Liste) - NICHT `faelliger_betrag_cent`; `brief_pdf.erzeuge_mahnbrief_
    pdf` prüft diesen Rückgabewert separat gegen den vollen Kontostand,
    der fällige Teilbetrag bleibt davon unabhängig durch den Aufrufer
    (`gesamtbetrag_cent`) bestimmt."""

    if pdf.will_page_break(20):
        pdf.add_page()
    pdf.set_font(familie, "B", 11)
    pdf.set_text_color(*anthrazit)
    pdf.cell(0, 7, ueberschrift, new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(*gold)
    pdf.line(_RAND, pdf.get_y(), _INHALT_RECHTS, pdf.get_y())
    pdf.ln(2)
    pdf.set_font(familie, "", 9)
    pdf.set_text_color(0, 0, 0)
    pdf.multi_cell(0, 5, _ERKLAERUNG)
    pdf.ln(1)

    if not zeilen:
        pdf.set_font(familie, "", 9.5)
        pdf.cell(0, 5, "Keine Buchungen.", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(5)
        return 0

    _tabellenkopf(pdf, familie=familie, anthrazit=anthrazit, gold=gold)
    for z in zeilen:
        _sichere_seite_vor_zeile(pdf, familie=familie, bezeichnung=z.buchung, fett=False, anthrazit=anthrazit, gold=gold)
        _zeile(
            pdf, familie=familie, datum_text=z.datum.strftime("%d.%m.%Y"), bezeichnung=z.buchung,
            betrag_cent=z.betrag_cent, offen_text=_eur(z.offen_cent),
        )
    letzter_saldo = zeilen[-1].offen_cent

    pdf.ln(2)
    pdf.set_draw_color(*gold)
    pdf.line(_RAND, pdf.get_y(), _INHALT_RECHTS, pdf.get_y())
    pdf.ln(3)
    gesamt_text = "Gesamt offen" if letzter_saldo >= 0 else "Gesamt Guthaben"
    _sichere_seite_vor_zeile(pdf, familie=familie, bezeichnung=gesamt_text, fett=True, anthrazit=anthrazit, gold=gold)
    _zeile(pdf, familie=familie, datum_text="", bezeichnung=gesamt_text, betrag_cent=abs(letzter_saldo), offen_text="", fett=True)
    if faelliger_betrag_cent is not None and faelliger_betrag_cent != letzter_saldo:
        jetzt_text = "Davon jetzt zu zahlen"
        _sichere_seite_vor_zeile(pdf, familie=familie, bezeichnung=jetzt_text, fett=True, anthrazit=anthrazit, gold=gold)
        _zeile(pdf, familie=familie, datum_text="", bezeichnung=jetzt_text, betrag_cent=abs(faelliger_betrag_cent), offen_text="", fett=True)
    return letzter_saldo
