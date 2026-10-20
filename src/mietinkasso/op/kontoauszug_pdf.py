"""Druckfähiger JLB-PDF-Kontoauszug (Auftrag HV-20261020-KONTOBLATT-
KOSTEN) - reine Rendering-Funktion über dem bereits aggregierten,
read-only `op/kontoblatt.py::Kontoblatt`. Erzeugt KEINE eigenen Daten/
Berechnungen (identisch zur Anzeige in `backoffice/routes/konten.py::
kontoauszug`), bucht nichts, sendet nichts.

Bewusst EIN EIGENES, separates PDF-Layout statt `mahnwesen/brief_pdf.py`
mitzuverwenden ("Mahnbrief ist nicht Kontoblatt", Auftrag Markus
20.10.2026): kein Fensterkuvert, keine Empfänger-Freihaltezone - ein
normaler, ganzseitiger Bericht zum Ausdrucken/Weitergeben (z. B. an
einen Rechtsanwalt). Wiederverwendet aber dieselbe JLB-CI-Infrastruktur
wie der Mahnbrief (`brief_pdf.Absender`-Datenklasse, dieselben
Settings-Felder für Logo/Fonts/Farben, dieselbe PDF/A-Erzwingung,
derselbe Fallback-Font) - KEIN improvisiertes zweites Logo/keine zweite
Farbpalette."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from fpdf import FPDF
from fpdf.enums import MethodReturnValue

from mietinkasso.mahnwesen.brief_pdf import _FALLBACK_FONT_BOLD, _FALLBACK_FONT_REGULAR, Absender, _hex_to_rgb
from mietinkasso.op.kontoblatt import KATEGORIE_LABEL, Kontoblatt

_SEITE_BREITE = 210.0
_RAND = 20.0
_INHALT_RECHTS = _SEITE_BREITE - _RAND
_BETRAG_SPALTE_BREITE = 32.0
_REST_SPALTE_BREITE = 28.0
_LOGO_BREITE = 32.0


def _eur(cent: int) -> str:
    vorzeichen = "-" if cent < 0 else ""
    betrag = abs(cent)
    return f"{vorzeichen}{betrag / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " EUR"


class _KontoauszugPDF(FPDF):
    """Kopf (Logo rechtsbündig + Titelzeile, auf JEDER Seite) und Fußzeile
    (Absenderdaten zweizeilig + Seitenzahl, wie `brief_pdf._MahnbriefPDF`)
    - kein Fensterkuvert, also kein Sperrbereich zu beachten."""

    absender: Absender
    titel_zeile = ""
    farbe_anthrazit: tuple[int, int, int] = (0, 0, 0)
    farbe_gold: tuple[int, int, int] = (0, 0, 0)
    fett_familie = "Konto"

    def header(self):
        if self.absender.logo_pfad and Path(self.absender.logo_pfad).is_file():
            self.image(self.absender.logo_pfad, x=_INHALT_RECHTS - _LOGO_BREITE, y=10, w=_LOGO_BREITE)
        self.set_font(self.fett_familie, "B", 13)
        self.set_text_color(*self.farbe_anthrazit)
        self.set_xy(_RAND, 12)
        self.cell(_INHALT_RECHTS - _RAND - _LOGO_BREITE - 4, 7, self.titel_zeile, new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(*self.farbe_gold)
        self.line(_RAND, 24, _INHALT_RECHTS, 24)
        self.set_y(30)

    def footer(self):
        self.set_y(-20)
        self.set_draw_color(*self.farbe_gold)
        self.line(_RAND, self.get_y(), _INHALT_RECHTS, self.get_y())
        self.ln(1.5)
        self.set_font(self.fett_familie, "", 7.5)
        self.set_text_color(*self.farbe_anthrazit)
        self.cell(0, 4, f"{self.absender.name}, {self.absender.adresse} · {self.absender.fn}", new_x="LMARGIN", new_y="NEXT")
        self.cell(0, 4, f"UID {self.absender.uid} · {self.absender.telefon} · {self.absender.website} · "
                        f"{self.absender.email} · Seite {self.page_no()}/{{nb}}")


def _zeile(pdf: FPDF, *, datum_text: str, bezeichnung: str, betrag_cent: int, rest_text: str = "", fett: bool = False) -> None:
    """Dieselbe dry-run-Höhenprüfung + `will_page_break` wie `brief_pdf.
    _zeile` - eine lange Bezeichnung/Beleg-Referenz bricht VOR dem
    Zeichnen kontrolliert um, nie mitten im Seitenumbruch abgeschnitten."""

    datum_breite = 24.0
    label_breite = (_INHALT_RECHTS - _RAND - datum_breite) - _BETRAG_SPALTE_BREITE - _REST_SPALTE_BREITE
    pdf.set_font("Konto", "B" if fett else "", 9.5)
    hoehe = pdf.multi_cell(label_breite, 5, bezeichnung, align="L", dry_run=True, output=MethodReturnValue.HEIGHT)
    hoehe = max(hoehe, 5)
    if pdf.will_page_break(hoehe):
        pdf.add_page()
    x0, y0 = _RAND, pdf.get_y()
    pdf.set_xy(x0, y0)
    pdf.cell(datum_breite, 5, datum_text, align="L")
    pdf.set_xy(x0 + datum_breite, y0)
    pdf.multi_cell(label_breite, 5, bezeichnung, align="L", new_x="LMARGIN", new_y="NEXT")
    y1 = pdf.get_y()
    pdf.set_xy(x0 + datum_breite + label_breite, y0)
    pdf.cell(_BETRAG_SPALTE_BREITE, 5, _eur(betrag_cent), align="R")
    pdf.set_xy(x0 + datum_breite + label_breite + _BETRAG_SPALTE_BREITE, y0)
    pdf.cell(_REST_SPALTE_BREITE, 5, rest_text, align="R")
    pdf.set_y(max(y1, y0 + 5))


def _abschnitt_titel(pdf: FPDF, text: str) -> None:
    if pdf.will_page_break(10):
        pdf.add_page()
    pdf.ln(3)
    pdf.set_font("Konto", "B", 11)
    pdf.set_text_color(*pdf.farbe_anthrazit)
    pdf.cell(0, 7, text, new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(*pdf.farbe_gold)
    pdf.line(_RAND, pdf.get_y(), _INHALT_RECHTS, pdf.get_y())
    pdf.ln(2)


def erzeuge_kontoauszug_pdf(
    *, absender: Absender, objekt_bezeichnung: str, einheit_bezeichnung: str, debitor_name: str,
    vertrag_id: str, konto_id: str, heute: date, kontoblatt: Kontoblatt,
) -> bytes:
    anthrazit = _hex_to_rgb(absender.farbe_anthrazit)
    gold = _hex_to_rgb(absender.farbe_gold)

    pdf = _KontoauszugPDF(format="A4", unit="mm", enforce_compliance="PDF/A-2B")
    pdf.absender = absender
    pdf.farbe_anthrazit, pdf.farbe_gold = anthrazit, gold
    pdf.titel_zeile = f"{absender.name} · Kontoauszug · {objekt_bezeichnung}, {einheit_bezeichnung}"
    pdf.set_margins(_RAND, 30, _RAND)
    pdf.set_auto_page_break(True, margin=24)
    pdf.alias_nb_pages()
    pdf.set_title(f"Kontoauszug {vertrag_id}")
    pdf.set_author(absender.name)

    regular = absender.font_regular_pfad if absender.font_regular_pfad and Path(absender.font_regular_pfad).is_file() else _FALLBACK_FONT_REGULAR
    bold = absender.font_bold_pfad if absender.font_bold_pfad and Path(absender.font_bold_pfad).is_file() else _FALLBACK_FONT_BOLD
    pdf.add_font("Konto", "", regular)
    pdf.add_font("Konto", "B", bold)

    pdf.add_page()
    pdf.set_font("Konto", "", 10)
    pdf.set_text_color(0, 0, 0)
    pdf.multi_cell(
        0, 5.5,
        f"Vertrag {vertrag_id} · Konto {konto_id}\n"
        f"Mieter: {debitor_name}\n"
        f"Objekt/Einheit: {objekt_bezeichnung}, {einheit_bezeichnung}\n"
        f"Erstellt am: {heute.strftime('%d.%m.%Y')}",
    )
    pdf.ln(2)

    _abschnitt_titel(pdf, "Hauptforderung")
    if kontoblatt.hauptforderung_zeilen:
        for z in kontoblatt.hauptforderung_zeilen:
            p = z.position
            rest_text = _eur(z.rest_cent) if z.rest_cent is not None else ""
            bezeichnung = f"{p.typ} · {p.beleg_referenz or p.aenderungsgrund or ''}"
            _zeile(pdf, datum_text=p.belegdatum.strftime("%d.%m.%Y"), bezeichnung=bezeichnung, betrag_cent=p.betrag_cent, rest_text=rest_text)
    else:
        pdf.cell(0, 5, "Keine Hauptforderungszeilen.", new_x="LMARGIN", new_y="NEXT")
    _zeile(pdf, datum_text="", bezeichnung="Hauptforderung gesamt", betrag_cent=kontoblatt.hauptforderung_cent, fett=True)

    _abschnitt_titel(pdf, "Tatsächlich gebuchte Kosten (Mahngebühr/-pauschale, Verzugszinsen, Rücklastschriftkosten)")
    if kontoblatt.kosten_zeilen:
        for z in kontoblatt.kosten_zeilen:
            p = z.position
            rest_text = _eur(z.rest_cent) if z.rest_cent is not None else ""
            label = KATEGORIE_LABEL.get(z.kategorie, z.kategorie)
            bezeichnung = f"{label} · {p.beleg_referenz or ''}"
            _zeile(pdf, datum_text=p.belegdatum.strftime("%d.%m.%Y"), bezeichnung=bezeichnung, betrag_cent=p.betrag_cent, rest_text=rest_text)
        _zeile(pdf, datum_text="", bezeichnung="Kosten gesamt (bereits im Saldo enthalten)", betrag_cent=kontoblatt.kosten_cent, fett=True)
    else:
        pdf.cell(0, 5, "Keine gebuchten Kosten.", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(5)

    _abschnitt_titel(pdf, "Zahlungen / Rücklastschriften (Stornostand)")
    if kontoblatt.zahlungen_zeilen:
        for z in kontoblatt.zahlungen_zeilen:
            p = z.position
            bezeichnung = f"{p.typ} · {p.beleg_referenz or p.aenderungsgrund or ''}"
            _zeile(pdf, datum_text=p.belegdatum.strftime("%d.%m.%Y"), bezeichnung=bezeichnung, betrag_cent=p.betrag_cent)
        _zeile(pdf, datum_text="", bezeichnung="Zahlungs-/Stornostand gesamt", betrag_cent=kontoblatt.zahlungen_saldo_cent, fett=True)
    else:
        pdf.cell(0, 5, "Keine Zahlungen/Rücklastschriften.", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(5)

    pdf.ln(2)
    pdf.set_draw_color(*gold)
    pdf.line(_RAND, pdf.get_y(), _INHALT_RECHTS, pdf.get_y())
    pdf.ln(3)
    _zeile(pdf, datum_text="", bezeichnung="Gesamtsaldo", betrag_cent=kontoblatt.saldo_cent, fett=True)
    _zeile(pdf, datum_text="", bezeichnung="davon mit bekannter, bereits verstrichener Fälligkeit", betrag_cent=kontoblatt.faelliger_unstrittiger_rest_cent)

    if kontoblatt.dokumentierte_bankaufwaende:
        _abschnitt_titel(pdf, "Dokumentierte, (noch) nicht belastete Bankaufwände")
        pdf.set_font("Konto", "", 9)
        pdf.multi_cell(0, 5, "Diese Beträge sind erfasst, aber NICHT als Forderung gebucht - sie sind NICHT im Gesamtsaldo enthalten.")
        pdf.ln(1)
        for b in kontoblatt.dokumentierte_bankaufwaende:
            r = b.row
            datum_text = r.gebucht_am.strftime("%d.%m.%Y") if isinstance(r.gebucht_am, datetime) else ""
            bezeichnung = (
                f"Rückgabe {datum_text} · Ursache: {r.rueckgabeursache} · Beleg: {r.beleg_referenz} · "
                f"Rücklastschrift-OP #{r.ruecklastschrift_op_position_id}"
                + (f" · Bank-Transaktion #{r.bank_transaktion_id}" if r.bank_transaktion_id is not None else "")
                + f" · Status: {b.status_text}"
            )
            _zeile(pdf, datum_text=datum_text, bezeichnung=bezeichnung, betrag_cent=r.bankentgelt_cent)
        summe_bankaufwaende = sum(b.row.bankentgelt_cent for b in kontoblatt.dokumentierte_bankaufwaende)
        _zeile(pdf, datum_text="", bezeichnung="Summe nicht belasteter Bankaufwände (NICHT im Gesamtsaldo)", betrag_cent=summe_bankaufwaende, fett=True)

    if kontoblatt.ausstehende_mahnlaeufe:
        _abschnitt_titel(pdf, "Ausstehende Mahnläufe (Kosten noch nicht gebucht)")
        pdf.set_font("Konto", "", 9)
        pdf.multi_cell(0, 5, "Für diese Mahnläufe ist die Kostenverarbeitung noch NICHT abgeschlossen - das bedeutet 'nicht gebucht', nicht 'nichts offen'.")
        pdf.ln(1)
        for m in kontoblatt.ausstehende_mahnlaeufe:
            geplant_text = m.geplant_am.strftime("%d.%m.%Y") if isinstance(m.geplant_am, datetime) else ""
            bezeichnung = f"Stufe {m.stufe} · Kanal {m.kanal} · Status {m.status} · geplant am {geplant_text}"
            pdf.set_font("Konto", "", 9.5)
            pdf.multi_cell(0, 5, bezeichnung, new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())
