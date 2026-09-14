"""PDF/A-Mahnbrief-Generator (EinfachBrief-Fensterkuvert-Layout).

Reine Funktion ohne DB-/Netzwerkzugriff: nimmt exakt die bereits an
anderer Stelle berechneten Werte (dieselbe `MahnkostenVorschau` wie der
E-Mail-Text und die Buchung) entgegen und rendert daraus ein Blatt.
PDF/A-Konformität wird über `fpdf2`s `enforce_compliance="PDF/A-2B"`
erzwungen, nicht selbst nachgebaut. Amtliche Konformitätsprüfung
(veraPDF) erfolgt extern am erzeugten PDF.

Fensterposition: EinfachBrief-Spezifikation 20/62 mm, 90x45 mm, 5 mm
Schutzzone ringsum, Empfänger 11pt linksbündig, max. 6 Zeilen - eine zu
lange/fehlende Adresse wird über `AdressfehlerError` kontrolliert
abgelehnt statt als Platzhalter in einen druckfertigen Brief zu geraten.

Schrift/Logo sind konfigurierbare Dateipfade (siehe
`infrastructure/config.py`): ohne sie fällt die Schrift auf eine immer
vorhandene, offen lizenzierte TTF zurück und es wird KEIN Logo gezeichnet."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from fpdf import FPDF

_FALLBACK_FONT_REGULAR = "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf"
_FALLBACK_FONT_BOLD = "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf"

_SEITE_BREITE = 210.0
_RAND = 20.0
_INHALT_RECHTS = _SEITE_BREITE - _RAND  # 190
_BETRAG_SPALTE_BREITE = 35.0
_MAX_EMPFAENGER_ZEILEN = 6


class AdressfehlerError(ValueError):
    """Fehlende oder für das Fensterkuvert zu lange Empfängeradresse -
    ein druckfertiger Brief darf NIE einen Platzhalter dafür zeigen."""


@dataclass(frozen=True)
class Absender:
    name: str
    adresse: str
    fn: str
    uid: str
    telefon: str
    website: str
    email: str
    farbe_anthrazit: str
    farbe_gold: str
    logo_pfad: str | None = None
    font_regular_pfad: str | None = None
    font_bold_pfad: str | None = None
    # Optionaler, vom Fließtext getrennter Font NUR für den Betreff
    # (z. B. "Forum" als Headline, "EB Garamond" als Copy) - ohne
    # konfigurierten Pfad bleibt der Betreff auf dem normalen Fett-Font.
    font_headline_pfad: str | None = None
    fenster_links_mm: float = 20.0
    fenster_oben_mm: float = 62.0
    fenster_breite_mm: float = 90.0
    fenster_hoehe_mm: float = 45.0


@dataclass(frozen=True)
class Forderungszeile:
    bezeichnung: str
    faelligkeit: date | None
    betrag_cent: int


@dataclass(frozen=True)
class Zinssegment:
    von: date
    bis: date
    satz_prozent: object  # Decimal | None, nur zur Anzeige formatiert
    zinsen_cent: int


def _hex_to_rgb(hexfarbe: str) -> tuple[int, int, int]:
    h = hexfarbe.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _eur(cent: int) -> str:
    return f"{cent / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " EUR"


def pruefe_empfaengeradresse(adresse: str | None) -> list[str]:
    """Zerlegt/prüft die Empfängeradresse für das Fensterkuvert -
    wirft `AdressfehlerError`, statt eine fehlende/zu lange Adresse
    stillschweigend zu drucken oder abzuschneiden."""

    if not adresse or not adresse.strip():
        raise AdressfehlerError("Keine Postadresse hinterlegt - kein druckfertiger Brief möglich.")
    zeilen = [z.strip() for z in adresse.replace("\r\n", "\n").split("\n") if z.strip()]
    if not zeilen:
        raise AdressfehlerError("Keine Postadresse hinterlegt - kein druckfertiger Brief möglich.")
    if len(zeilen) > _MAX_EMPFAENGER_ZEILEN:
        raise AdressfehlerError(
            f"Postadresse hat {len(zeilen)} Zeilen, das EinfachBrief-Fenster erlaubt höchstens {_MAX_EMPFAENGER_ZEILEN}."
        )
    return zeilen


class _MahnbriefPDF(FPDF):
    """Zeichnet Kopf (nur ab Seite 2, knapp) und Fußzeile (jede Seite,
    zweizeilig + Seitenzahl) automatisch bei jedem `add_page()`."""

    kopf_text = ""
    fuss_zeile1 = ""
    fuss_zeile2 = ""
    fett_familie = "Brief"
    farbe_anthrazit: tuple[int, int, int] = (0, 0, 0)
    farbe_gold: tuple[int, int, int] = (0, 0, 0)

    def header(self):
        if self.page_no() == 1:
            return
        self.set_font(self.fett_familie, "", 8)
        self.set_text_color(*self.farbe_anthrazit)
        self.set_xy(_RAND, 10)
        self.cell(0, 5, self.kopf_text, new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(*self.farbe_gold)
        self.line(_RAND, 16, _INHALT_RECHTS, 16)
        self.set_y(20)

    def footer(self):
        self.set_y(-20)
        self.set_draw_color(*self.farbe_gold)
        self.line(_RAND, self.get_y(), _INHALT_RECHTS, self.get_y())
        self.ln(1.5)
        self.set_font(self.fett_familie, "", 7.5)
        self.set_text_color(*self.farbe_anthrazit)
        self.cell(0, 4, self.fuss_zeile1, new_x="LMARGIN", new_y="NEXT")
        self.cell(0, 4, f"{self.fuss_zeile2} · Seite {self.page_no()}/{{nb}}")


def _zeile(pdf: FPDF, label: str, betrag_cent: int, *, einzug: float = 0.0, fett: bool = False) -> None:
    """Eine Kosten-/Forderungszeile mit variabler Zeilenhöhe für den
    Bezeichnungstext (Umbruch statt Überlauf in die Betragsspalte) und
    dem Betrag als eigener, rechtsbündiger Spalte."""

    label_breite = (_INHALT_RECHTS - _RAND - einzug) - _BETRAG_SPALTE_BREITE
    pdf.set_font("Brief", "B" if fett else "", 10)
    # Manuell VOR der Zeile umbrechen, statt `y0` unter der Hand
    # veralten zu lassen: bräche `multi_cell` selbst mitten in der
    # Zeile um, würde der Betrag mit dem alten `y0` sonst erneut einen
    # (zweiten) Seitenumbruch auslösen und als verwaiste Zahl ohne
    # Bezeichnung auf einer eigenen Seite landen.
    if pdf.will_page_break(5.5):
        pdf.add_page()
    x0, y0 = _RAND + einzug, pdf.get_y()
    pdf.set_xy(x0, y0)
    pdf.multi_cell(label_breite, 5.5, label, align="L", new_x="LMARGIN", new_y="NEXT")
    y1 = pdf.get_y()
    pdf.set_xy(x0 + label_breite, y0)
    pdf.cell(_BETRAG_SPALTE_BREITE, 5.5, _eur(betrag_cent), align="R")
    pdf.set_y(y1)


def erzeuge_mahnbrief_pdf(
    *,
    absender: Absender,
    empfaenger_name: str,
    empfaenger_adresse: str,
    objekt_bezeichnung: str,
    einheit_bezeichnung: str,
    stufe: int,
    heute: date,
    zahlungsfrist_bis: date,
    forderungszeilen: list[Forderungszeile],
    bereits_offene_mahnkosten_cent: int,
    neue_zinsen_delta_cent: int,
    zins_segmente: list[Zinssegment],
    zinssatz_einheitlich_text: str | None,
    neue_gebuehr_cent: int,
    gebuehr_rechtsgrundlage: str | None,
    gesamtbetrag_cent: int,
) -> bytes:
    empfaenger_zeilen = pruefe_empfaengeradresse(empfaenger_adresse)
    empfaenger_alle_zeilen = [empfaenger_name.strip(), *empfaenger_zeilen] if empfaenger_name.strip() else empfaenger_zeilen
    if len(empfaenger_alle_zeilen) > _MAX_EMPFAENGER_ZEILEN:
        raise AdressfehlerError(
            f"Empfänger+Adresse ergeben {len(empfaenger_alle_zeilen)} Zeilen, das Fenster erlaubt höchstens {_MAX_EMPFAENGER_ZEILEN}."
        )

    anthrazit = _hex_to_rgb(absender.farbe_anthrazit)
    gold = _hex_to_rgb(absender.farbe_gold)
    betreff = "Zahlungserinnerung" if stufe == 1 else "Zweite Mahnung"

    pdf = _MahnbriefPDF(format="A4", unit="mm", enforce_compliance="PDF/A-2B")
    pdf.farbe_anthrazit, pdf.farbe_gold = anthrazit, gold
    pdf.kopf_text = f"{absender.name} · {betreff} · {objekt_bezeichnung}, {einheit_bezeichnung}"
    pdf.fuss_zeile1 = f"{absender.name}, {absender.adresse} · {absender.fn}"
    pdf.fuss_zeile2 = f"UID {absender.uid} · {absender.telefon} · {absender.website} · {absender.email}"
    pdf.set_margins(_RAND, 20, _RAND)
    pdf.set_auto_page_break(True, margin=24)
    pdf.alias_nb_pages()
    pdf.set_title(f"{betreff} {objekt_bezeichnung} {einheit_bezeichnung}")
    pdf.set_author(absender.name)

    regular = absender.font_regular_pfad if absender.font_regular_pfad and Path(absender.font_regular_pfad).is_file() else _FALLBACK_FONT_REGULAR
    bold = absender.font_bold_pfad if absender.font_bold_pfad and Path(absender.font_bold_pfad).is_file() else _FALLBACK_FONT_BOLD
    pdf.add_font("Brief", "", regular)
    pdf.add_font("Brief", "B", bold)
    headline_familie, headline_stil = "Brief", "B"
    if absender.font_headline_pfad and Path(absender.font_headline_pfad).is_file():
        pdf.add_font("Headline", "", absender.font_headline_pfad)
        headline_familie, headline_stil = "Headline", ""

    pdf.add_page()

    fenster_unten = absender.fenster_oben_mm + absender.fenster_hoehe_mm
    logo_unten = 12.0
    if absender.logo_pfad and Path(absender.logo_pfad).is_file():
        info = pdf.image(absender.logo_pfad, x=_RAND, y=12, w=32)
        logo_unten = 12.0 + info.rendered_height

    # Absenderzeile (Retouradresse) sauber vom Logo getrennt UND
    # sichtbar außerhalb der 5-mm-Schutzzone über dem Fenster (Fenster
    # beginnt bei `fenster_oben_mm`).
    absender_y = min(logo_unten + 4, absender.fenster_oben_mm - 5 - 4)
    pdf.set_xy(_RAND, absender_y)
    pdf.set_font("Brief", "", 8)
    pdf.set_text_color(*anthrazit)
    pdf.cell(0, 4, f"{absender.name} · {absender.adresse}", new_x="LMARGIN", new_y="NEXT")

    # Empfänger AUSSCHLIESSLICH innerhalb des Fensters, linksbündig
    # (kein Blocksatz - je Zeile eigene `cell()`, kein `multi_cell`
    # mit eingebetteten Zeilenumbrüchen).
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Brief", "", 11)
    y = absender.fenster_oben_mm
    for zeile in empfaenger_alle_zeilen:
        pdf.set_xy(absender.fenster_links_mm, y)
        pdf.cell(absender.fenster_breite_mm, 5, zeile, align="L")
        y += 5

    # Fließtext beginnt ERST nach Fenster + 5-mm-Schutzzone.
    pdf.set_y(fenster_unten + 5)
    pdf.set_font("Brief", "", 10)
    pdf.cell(0, 5, f"Wien, {heute.strftime('%d.%m.%Y')}", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    pdf.set_font(headline_familie, headline_stil, 13)
    pdf.cell(0, 8, betreff, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Brief", "", 10)
    pdf.cell(0, 5, f"{objekt_bezeichnung}, {einheit_bezeichnung}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    pdf.set_font("Brief", "", 10)
    pdf.multi_cell(0, 5.5, "Sehr geehrte Damen und Herren,\n\nfür das oben genannte Mietverhältnis sind folgende Forderungen offen:")
    pdf.ln(2)

    hauptforderung_cent = sum(f.betrag_cent for f in forderungszeilen)
    for f in forderungszeilen:
        faelligkeit_text = f"fällig seit {f.faelligkeit.strftime('%d.%m.%Y')}" if f.faelligkeit else "Fälligkeit ungeklärt"
        _zeile(pdf, f"{f.bezeichnung}, {faelligkeit_text}", f.betrag_cent)
    _zeile(pdf, "Hauptforderung gesamt", hauptforderung_cent, fett=True)

    if bereits_offene_mahnkosten_cent:
        _zeile(pdf, "Noch offene Kosten aus früheren Mahnläufen", bereits_offene_mahnkosten_cent)

    if neue_zinsen_delta_cent:
        satz_text = f" ({zinssatz_einheitlich_text})" if zinssatz_einheitlich_text else ""
        _zeile(pdf, f"Neu anzusetzende Verzugszinsen{satz_text}", neue_zinsen_delta_cent)
        for segment in zins_segmente:
            if segment.zinsen_cent <= 0:
                continue
            satz = f"{segment.satz_prozent} % p.a." if segment.satz_prozent is not None else "Satz ungeklärt"
            _zeile(
                pdf, f"{segment.von.strftime('%d.%m.%Y')}–{segment.bis.strftime('%d.%m.%Y')}: {satz}",
                segment.zinsen_cent, einzug=6,
            )

    if neue_gebuehr_cent:
        _zeile(pdf, gebuehr_rechtsgrundlage or "Mahnspesen/Versandkosten", neue_gebuehr_cent)

    pdf.set_draw_color(*gold)
    pdf.line(_RAND, pdf.get_y() + 1, _INHALT_RECHTS, pdf.get_y() + 1)
    pdf.ln(3)
    _zeile(pdf, "Gesamtbetrag", gesamtbetrag_cent, fett=True)
    pdf.ln(4)

    pdf.set_font("Brief", "", 10)
    pdf.multi_cell(
        0, 5.5,
        f"Bitte begleichen Sie den Gesamtbetrag bis {zahlungsfrist_bis.strftime('%d.%m.%Y')} auf das Ihnen "
        "für dieses Mietverhältnis bekannt gegebene Konto. Bei einer inzwischen erfolgten Zahlung senden Sie "
        "uns bitte den Zahlungsbeleg. Bei Fragen zur Forderung wenden Sie sich bitte an untenstehende Kontaktdaten.",
    )
    pdf.ln(6)
    pdf.cell(0, 5, "Mit freundlichen Grüßen", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, absender.name, new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())
