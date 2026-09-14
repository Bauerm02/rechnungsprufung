"""PDF/A-Mahnbrief-Generator (EinfachBrief-Fensterkuvert-Layout).

Reine Funktion ohne DB-/Netzwerkzugriff: nimmt exakt die bereits an
anderer Stelle berechneten Werte (dieselbe `MahnkostenVorschau` wie der
E-Mail-Text und die Buchung) entgegen und rendert daraus ein Blatt.
PDF/A-Konformität wird über `fpdf2`s `enforce_compliance="PDF/A-2B"`
erzwungen (nicht selbst nachgebaut) - ein Verstoß (z. B. eine nicht
eingebettete Schriftart) lässt `output()` bereits hier laut fehlschlagen,
statt ein defektes PDF auszuliefern. Amtliche/externe Konformitätsprüfung
bleibt Sache von veraPDF am tatsächlichen Ergebnis-PDF (Codex).

Schrift/Logo sind bewusst konfigurierbare Dateipfade (siehe
`infrastructure/config.py`): ohne sie fällt die Schrift auf eine immer
vorhandene, offen lizenzierte TTF zurück und es wird KEIN Logo gezeichnet
(kein erfundenes Signet) - Codex legt die freigegebenen JLB-Assets beim
Deployment privat ab."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from fpdf import FPDF

_FALLBACK_FONT_REGULAR = "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf"
_FALLBACK_FONT_BOLD = "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf"


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
    fenster_links_mm: float = 20.0
    fenster_oben_mm: float = 45.0


@dataclass(frozen=True)
class Kostenzeile:
    text: str


def _hex_to_rgb(hexfarbe: str) -> tuple[int, int, int]:
    h = hexfarbe.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _eur(cent: int) -> str:
    return f"{cent / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " EUR"


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
    hauptforderung_cent: int,
    bereits_offene_mahnkosten_cent: int,
    neue_zinsen_delta_cent: int,
    neue_gebuehr_cent: int,
    gebuehr_rechtsgrundlage: str | None,
    zins_hinweis: str | None,
    gesamtbetrag_cent: int,
) -> bytes:
    anthrazit = _hex_to_rgb(absender.farbe_anthrazit)
    gold = _hex_to_rgb(absender.farbe_gold)

    pdf = FPDF(format="A4", unit="mm", enforce_compliance="PDF/A-2B")
    pdf.set_margins(20, 20, 20)
    pdf.set_auto_page_break(True, margin=20)
    betreff = "Zahlungserinnerung" if stufe == 1 else "Zweite Mahnung"
    pdf.set_title(f"{betreff} {objekt_bezeichnung} {einheit_bezeichnung}")
    pdf.set_author(absender.name)
    pdf.add_page()

    regular = absender.font_regular_pfad if absender.font_regular_pfad and Path(absender.font_regular_pfad).is_file() else _FALLBACK_FONT_REGULAR
    bold = absender.font_bold_pfad if absender.font_bold_pfad and Path(absender.font_bold_pfad).is_file() else _FALLBACK_FONT_BOLD
    pdf.add_font("Brief", "", regular)
    pdf.add_font("Brief", "B", bold)

    if absender.logo_pfad and Path(absender.logo_pfad).is_file():
        pdf.image(absender.logo_pfad, x=20, y=15, w=40)

    # Absenderzeile über dem Fenster (DIN 5008) + Fensteradresse.
    pdf.set_xy(20, absender.fenster_oben_mm - 8)
    pdf.set_font("Brief", "", 7)
    pdf.set_text_color(*anthrazit)
    pdf.cell(0, 4, f"{absender.name} · {absender.adresse}", new_x="LMARGIN", new_y="NEXT")

    pdf.set_xy(absender.fenster_links_mm, absender.fenster_oben_mm)
    pdf.set_font("Brief", "", 11)
    pdf.multi_cell(80, 5, f"{empfaenger_name}\n{empfaenger_adresse}")

    pdf.set_y(absender.fenster_oben_mm + 30)
    pdf.set_font("Brief", "", 10)
    pdf.cell(0, 5, f"Wien, {heute.strftime('%d.%m.%Y')}", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    pdf.set_font("Brief", "B", 13)
    pdf.cell(0, 8, betreff, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Brief", "", 10)
    pdf.cell(0, 5, f"{objekt_bezeichnung}, {einheit_bezeichnung}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    pdf.set_font("Brief", "", 10)
    pdf.multi_cell(0, 5.5, f"Sehr geehrte Damen und Herren,\n\nfür das oben genannte Mietverhältnis ist folgender Betrag offen:")
    pdf.ln(2)

    zeilen: list[tuple[str, int]] = [("Hauptforderung", hauptforderung_cent)]
    if bereits_offene_mahnkosten_cent:
        zeilen.append(("Noch offene Kosten aus früheren Mahnläufen", bereits_offene_mahnkosten_cent))
    if neue_zinsen_delta_cent:
        zeilen.append((zins_hinweis or "Verzugszinsen", neue_zinsen_delta_cent))
    if neue_gebuehr_cent:
        zeilen.append((gebuehr_rechtsgrundlage or "Mahnspesen/Versandkosten", neue_gebuehr_cent))

    for label, cent in zeilen:
        pdf.set_font("Brief", "", 10)
        pdf.cell(140, 6, label)
        pdf.cell(0, 6, _eur(cent), align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.set_draw_color(*gold)
    pdf.line(pdf.get_x(), pdf.get_y() + 1, 190, pdf.get_y() + 1)
    pdf.ln(3)
    pdf.set_font("Brief", "B", 11)
    pdf.cell(140, 7, "Gesamtbetrag")
    pdf.cell(0, 7, _eur(gesamtbetrag_cent), align="R", new_x="LMARGIN", new_y="NEXT")
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

    pdf.set_y(-25)
    pdf.set_draw_color(*gold)
    pdf.line(20, pdf.get_y(), 190, pdf.get_y())
    pdf.set_font("Brief", "", 7)
    pdf.set_text_color(*anthrazit)
    pdf.cell(
        0, 4,
        f"{absender.name}, {absender.adresse} · {absender.fn} · UID {absender.uid} · "
        f"{absender.telefon} · {absender.website} · {absender.email}",
        align="C",
    )

    return bytes(pdf.output())
