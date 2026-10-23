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

Freihaltebereiche (visuelle Prüfung des Original-Anhangs
"EinfachBrief-Freihaltebereiche.pdf", Codex-Rückmeldung 19.10.2026): auf
der Vorderseite oben links ein 52x60-mm-Rechteck, umlaufend 5 mm
Schutzabstand (`_SPERRBEREICH_BREITE`/`_SPERRBEREICH_HOEHE` unten) - dort
darf NICHTS gedruckt werden. Logo UND der laufende Folgeseitenkopf stehen
deshalb rechtsbündig (nicht bei x=20), niemals im Sperrbereich, auch wenn
kein Logo konfiguriert ist. Der bestehende linke Rand (`_RAND` = 20 mm)
und der bestehende äußere Seitenrand erfüllen den separat genannten
Freihaltebereich "links 18 mm bis 160 mm Höhe" sowie den äußeren
5-mm-Rand bereits unverändert (siehe geometrische Tests in
`test_brief_pdf.py`).

Absenderzeile (zusätzlicher, konkreter Layoutnachweis, offizielle
Vorlage https://www.einfach-brief.at/fe/vorlagen + Original-DOCX-
Einschreibenvorlage, Codex-Rückmeldung 19.10.2026): die Post verlangt für
Rückläufer eine kurze, einzeilige 8pt-Retouradresse INNERHALB des
Empfängerfensters, in dessen oberster Zeile, mit einer Leerzeile Abstand
zur Empfängeradresse darunter - das überlappt zahlenmäßig mit dem oben
genannten Sperrbereich (das Fenster beginnt bei x=20/y=62, der
Sperrbereich bei x<57/y<65), ist aber eine eigene, später und
spezifischer belegte Vorgabe GENAU für diese eine Zeile im Fenster und
ersetzt dort NICHT den allgemeinen Sperrbereich für Logo/Folgeseitenkopf
(die bleiben wie oben beschrieben rechtsbündig außerhalb). Es gibt
dadurch bewusst NUR EINE Absenderzeile (im Fenster), keine zweite,
konkurrierende Retouradresse beim Logo.

Schrift/Logo sind konfigurierbare Dateipfade (siehe
`infrastructure/config.py`): ohne sie fällt die Schrift auf eine immer
vorhandene, offen lizenzierte TTF zurück und es wird KEIN Logo gezeichnet."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from fpdf import FPDF
from fpdf.enums import MethodReturnValue

from mietinkasso.mahnwesen.kosten import zins_bis_einschliesslich
from mietinkasso.mahnwesen.mahntexte import text_fuer_stufe
from mietinkasso.op.einfache_tabelle_pdf import zeichne_einfache_tabelle
from mietinkasso.op.kontoblatt import Kontoblatt, erstelle_einfache_tabelle, erweitere_um_vorschau

_FALLBACK_FONT_REGULAR = "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf"
_FALLBACK_FONT_BOLD = "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf"

_SEITE_BREITE = 210.0
_RAND = 20.0
_INHALT_RECHTS = _SEITE_BREITE - _RAND  # 190
_BETRAG_SPALTE_BREITE = 35.0

# EinfachBrief-Freihaltebereich oben links: 52 mm Breite x 60 mm Höhe,
# umlaufend 5 mm Schutzabstand -> nichts darf links von
# `_SPERRBEREICH_BREITE` beginnen, solange y < `_SPERRBEREICH_HOEHE` ist.
_SPERRBEREICH_BREITE = 57.0  # 52 + 5
_SPERRBEREICH_HOEHE = 65.0  # 60 + 5
_LOGO_BREITE = 32.0
_LOGO_X = _INHALT_RECHTS - _LOGO_BREITE  # rechtsbündig, sicher außerhalb des Sperrbereichs
_LOGO_Y = 12.0
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
    # Abnahme-Folgefix (Codex 20.10.2026): "keine vollständige Periode/
    # Position/Ursprungsbetrag/angerechnete Zahlungen/Rest-Tabelle" -
    # optional, damit bestehende Aufrufer ohne diese Angabe unverändert
    # funktionieren (reine Restbetrag-Zeile wie bisher). Ist
    # `urspruenglicher_betrag_cent` gesetzt und weicht von `betrag_cent`
    # (= Rest) ab, wird darunter eine zweite, eingezogene Zeile mit
    # Ursprungsbetrag und bereits angerechneter Zahlung ausgewiesen.
    urspruenglicher_betrag_cent: int | None = None


@dataclass(frozen=True)
class Zahlungsziel:
    """Ziel-Kontoverbindung für die Zahlungsaufforderung im Mahnschreiben
    (Abnahme-Folgefix Codex 20.10.2026). `iban`/`kontoinhaber` kommen aus
    `infrastructure/config.py` (optional, kein erfundener Platzhalter) -
    fehlen sie, bleibt der bisherige allgemeine Hinweistext bestehen.
    `verwendungszweck` wird deterministisch aus Vertrag/Stufe gebildet,
    NIE frei erfunden."""

    iban: str | None
    kontoinhaber: str | None
    verwendungszweck: str


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
        # Rechtsbündig ab `_SPERRBEREICH_BREITE` statt ab `_RAND` (20 mm):
        # derselbe oben-links-Sperrbereich wie auf der Vorderseite gilt
        # vorsorglich für jede Seite (Codex-Rückmeldung 19.10.2026).
        self.set_font(self.fett_familie, "", 8)
        self.set_text_color(*self.farbe_anthrazit)
        self.set_xy(_SPERRBEREICH_BREITE, 10)
        self.cell(_INHALT_RECHTS - _SPERRBEREICH_BREITE, 5, self.kopf_text, new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(*self.farbe_gold)
        self.line(_SPERRBEREICH_BREITE, 16, _INHALT_RECHTS, 16)
        # Codex-Abnahme 19.10.2026, dritte Korrektur: NICHT `set_y(20)` -
        # der Fließtext beginnt danach links bei `_RAND` (20 mm), also
        # mitten im oben-links-Sperrbereich (x<57mm, y<65mm). Das
        # Originalblatt benennt diese Zone zwar nur für die Vorderseite,
        # bei diesem Simplexprodukt ist aber JEDE gedruckte Seite eine
        # eigene Blattvorderseite - konservativ auf jeder Seite
        # freigehalten, nicht nur auf Seite 1.
        self.set_y(_SPERRBEREICH_HOEHE + 5)

    def footer(self):
        self.set_y(-20)
        self.set_draw_color(*self.farbe_gold)
        self.line(_RAND, self.get_y(), _INHALT_RECHTS, self.get_y())
        self.ln(1.5)
        self.set_font(self.fett_familie, "", 7.5)
        self.set_text_color(*self.farbe_anthrazit)
        self.cell(0, 4, self.fuss_zeile1, new_x="LMARGIN", new_y="NEXT")
        self.cell(0, 4, f"{self.fuss_zeile2} · Seite {self.page_no()}/{{nb}}")


def _notizzeile(pdf: FPDF, text: str, *, einzug: float = 6.0) -> None:
    """Eine reine Hinweiszeile OHNE eigene Betragsspalte (z. B. die
    Ursprungsbetrag-/angerechnet-Aufschlüsselung unter einer Forderungs-
    zeile) - anders als `_zeile` wird hier KEIN (ggf. irreführend
    doppelter) Betrag rechtsbündig mitgedruckt."""

    pdf.set_font("Brief", "", 8.5)
    pdf.set_x(_RAND + einzug)
    pdf.multi_cell(_INHALT_RECHTS - _RAND - einzug, 4.5, text, align="L", new_x="LMARGIN", new_y="NEXT")


def _zeile(pdf: FPDF, label: str, betrag_cent: int, *, einzug: float = 0.0, fett: bool = False) -> None:
    """Eine Kosten-/Forderungszeile mit variabler Zeilenhöhe für den
    Bezeichnungstext (Umbruch statt Überlauf in die Betragsspalte) und
    dem Betrag als eigener, rechtsbündiger Spalte."""

    label_breite = (_INHALT_RECHTS - _RAND - einzug) - _BETRAG_SPALTE_BREITE
    pdf.set_font("Brief", "B" if fett else "", 10)
    # Manuell VOR der Zeile umbrechen, statt `y0` unter der Hand
    # veralten zu lassen: bräche `multi_cell` selbst mitten in einer
    # MEHRZEILIGEN Bezeichnung um, würde der Betrag mit dem alten `y0`
    # sonst als verwaiste Zahl auf der Folgeseite landen. Die tatsächliche
    # (ggf. mehrzeilige) Höhe wird deshalb per Dry-Run ermittelt statt nur
    # eine einzelne Zeile anzunehmen.
    hoehe = pdf.multi_cell(label_breite, 5.5, label, align="L", dry_run=True, output=MethodReturnValue.HEIGHT)
    if pdf.will_page_break(hoehe):
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
    rechtsordnung: str,
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
    zahlungsziel: Zahlungsziel | None = None,
    kontoblatt: Kontoblatt | None = None,
    zins_von: date | None = None,
    zins_bis: date | None = None,
    zinssatz_prozent=None,
    mahnpauschale_cent: int | None = None,
    vorschau_zins_segmente=(),
    neue_zinsen_delta_je_op_position: dict | None = None,
) -> bytes:
    """`kontoblatt` (Nachtrag Markus 23.10.2026 zum Auftrag HV-20261023-
    MAHNUNG-EINFACH, Abnahmegrenze nach Sichtprüfung der ersten Fassung:
    "nur das Kontoblatt als zweite PDF-Seite anzuhängen und vorne die
    alte Forderungsliste zu lassen erfüllt den Nutzerauftrag nicht - der
    eigentliche Mahnbrief braucht die chronologische Tabelle bereits auf
    seiner ersten Briefseite"): ist es gesetzt, ersetzt die kurze,
    chronologische Empfänger-Tabelle (`erstelle_einfache_tabelle`/
    `erweitere_um_vorschau`, derselbe Renderer wie der separate
    Kontoauszug-Anhang - `op/einfache_tabelle_pdf.zeichne_einfache_
    tabelle`, KEINE zweite Rechenlogik) die bisherige, nach Kategorien
    aufgeschlüsselte Forderungsliste (`forderungszeilen`/
    `bereits_offene_mahnkosten_cent`/`zins_segmente`/`neue_gebuehr_cent`
    unten, dann ignoriert). `kontoblatt=None` (Standard) belässt die
    bisherige Forderungslisten-Darstellung unverändert - bestehende
    Aufrufer/Tests ohne `kontoblatt` sind davon nicht betroffen.
    `zins_von`/`zins_bis`/`zinssatz_prozent`/`mahnpauschale_cent` sind
    nur zusammen mit `kontoblatt` wirksam (dieselben, dem Briefkörper
    bereits bekannten noch NICHT gebuchten Posten wie beim Kontoauszug-
    Anhang, `neue_zinsen_delta_cent`/`neue_gebuehr_cent` werden dafür
    wiederverwendet - keine doppelten Parameter).

    `vorschau_zins_segmente`/`neue_zinsen_delta_je_op_position`
    (Ergänzung Markus 23.10.2026, Abnahme-Hinweis zu Commit 3c6c175):
    dieselben bereits vorhandenen strukturierten `MahnkostenVorschau`-
    Felder wie beim Kontoauszug-Anhang, nur für die NEUE `kontoblatt`-
    Tabelle wirksam (`erweitere_um_vorschau`) - bewusst ANDERS benannt
    als das bestehende `zins_segmente` oben, das ausschließlich zur
    alten, bei aktivem `kontoblatt` ohnehin ignorierten Forderungsliste
    gehört und ein anderer Typ (`Zinssegment`, nicht `kosten.py::
    ZinsSegment`) ist."""
    empfaenger_zeilen = pruefe_empfaengeradresse(empfaenger_adresse)
    empfaenger_alle_zeilen = [empfaenger_name.strip(), *empfaenger_zeilen] if empfaenger_name.strip() else empfaenger_zeilen
    if len(empfaenger_alle_zeilen) > _MAX_EMPFAENGER_ZEILEN:
        raise AdressfehlerError(
            f"Empfänger+Adresse ergeben {len(empfaenger_alle_zeilen)} Zeilen, das Fenster erlaubt höchstens {_MAX_EMPFAENGER_ZEILEN}."
        )

    anthrazit = _hex_to_rgb(absender.farbe_anthrazit)
    gold = _hex_to_rgb(absender.farbe_gold)
    # Auftrag HV-20261020-VIER-MAHNSTUFEN: Betreff/Einleitung/Kerntext/
    # Rechtsabsatz kommen jetzt AUSSCHLIESSLICH aus `mahntexte.
    # text_fuer_stufe` - demselben Textbaustein, den auch die E-Mail
    # (`indexautomatik/mailversand_service.py`) verwendet (Moduldoc:
    # "Kein Kundentext an zwei Stellen unabhängig formuliert").
    mahntext = text_fuer_stufe(stufe=stufe, rechtsordnung=rechtsordnung, zahlungsfrist_bis=zahlungsfrist_bis)
    betreff = mahntext.betreff

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

    # Bisher wurde nur die ANZAHL der Empfängerzeilen geprüft - eine
    # einzelne, für sich genommen zu lange Zeile lief mangels
    # Breitenprüfung mit `cell(fenster_breite_mm, ...)` rechts aus dem
    # Fenster. Jetzt mit der tatsächlichen Fontbreite (11pt) je Zeile per
    # Dry-Run kontrolliert umbrechen; ergibt das in Summe mehr als die
    # zulässigen Zeilen, wird kontrolliert abgelehnt statt still zu
    # überlaufen.
    pdf.set_font("Brief", "", 11)
    empfaenger_render_zeilen: list[str] = []
    for zeile in empfaenger_alle_zeilen:
        empfaenger_render_zeilen.extend(
            pdf.multi_cell(absender.fenster_breite_mm, 5, zeile, align="L", dry_run=True, output=MethodReturnValue.LINES)
        )
    if len(empfaenger_render_zeilen) > _MAX_EMPFAENGER_ZEILEN:
        raise AdressfehlerError(
            f"Empfänger+Adresse ergeben nach Zeilenumbruch im {absender.fenster_breite_mm:.0f}-mm-Fenster "
            f"{len(empfaenger_render_zeilen)} Zeilen, das Fenster erlaubt höchstens {_MAX_EMPFAENGER_ZEILEN}."
        )

    fenster_unten = absender.fenster_oben_mm + absender.fenster_hoehe_mm
    # Logo bleibt rechtsbündig (`_LOGO_X` >= `_SPERRBEREICH_BREITE`), NIE
    # im oben-links-Sperrbereich.
    if absender.logo_pfad and Path(absender.logo_pfad).is_file():
        pdf.image(absender.logo_pfad, x=_LOGO_X, y=_LOGO_Y, w=_LOGO_BREITE)

    # Kurze Absenderzeile (Retouradresse) als EINZIGE Absenderangabe
    # (keine zweite, konkurrierende Zeile beim Logo): laut Post-Vorgabe
    # für Fensterkuvert-Rückläufer einzeilig 8pt INNERHALB des
    # Empfängerfensters, in dessen oberster Zeile, linksbündig bei
    # (`fenster_links_mm`/`fenster_oben_mm`) - NICHT mehr außerhalb des
    # Fensters (offizielle Vorlage https://www.einfach-brief.at/fe/vorlagen,
    # Original-DOCX-Einschreibenvorlage, Codex-Rückmeldung 19.10.2026).
    pdf.set_xy(absender.fenster_links_mm, absender.fenster_oben_mm)
    pdf.set_font("Brief", "", 8)
    pdf.set_text_color(*anthrazit)
    pdf.cell(absender.fenster_breite_mm, 4, f"{absender.name} · {absender.adresse}", align="L")

    # Empfänger AUSSCHLIESSLICH innerhalb des Fensters, linksbündig
    # (kein Blocksatz - je Zeile eigene `cell()`, kein `multi_cell`
    # mit eingebetteten Zeilenumbrüchen), eine Leerzeile unter der
    # Absenderzeile (Post-Vorgabe für die Rückläufer-Erkennung).
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Brief", "", 11)
    y = absender.fenster_oben_mm + 4 + 4  # Absenderzeile (4mm) + eine Leerzeile (4mm)
    for zeile in empfaenger_render_zeilen:
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
    pdf.multi_cell(
        0, 5.5,
        f"Sehr geehrte Damen und Herren,\n\n{mahntext.einleitung} {mahntext.kerntext}\n\n"
        "Für das oben genannte Mietverhältnis ist folgende Forderung offen:",
    )
    pdf.ln(2)

    if kontoblatt is not None:
        # Auftrag HV-20261023-MAHNUNG-EINFACH, Abnahmegrenze 23.10.2026:
        # die kurze, chronologische Empfänger-Tabelle direkt auf dieser
        # ersten Briefseite - derselbe Renderer wie der separate
        # Kontoauszug-Anhang, KEINE zweite Rechenlogik/kein abweichender
        # Schlusssaldo.
        einfache_zeilen = erweitere_um_vorschau(
            erstelle_einfache_tabelle(kontoblatt), heute=heute, neue_zinsen_delta_cent=neue_zinsen_delta_cent,
            zins_von=zins_von, zins_bis=zins_bis, zinssatz_prozent=zinssatz_prozent,
            gebuehr_cent=(neue_gebuehr_cent or None), mahnpauschale_cent=mahnpauschale_cent,
            zins_segmente=vorschau_zins_segmente, neue_zinsen_delta_je_op_position=neue_zinsen_delta_je_op_position,
        )
        # Korrektur nach unabhängiger Rückprüfung (Codex, 23.10.2026,
        # Gegenlesen von Commit d52948f, echter Fachfehler): die Tabelle
        # zeigt bewusst die VOLLSTÄNDIGE Kontohistorie (identisch zum
        # angehängten kanonischen Kontoauszug) - ihr "Gesamt offen" kann
        # dadurch Positionen enthalten, die (noch) NICHT Teil DIESES
        # geprüften Mahnlaufs/-snapshots sind (z. B. eine später fällige
        # Folgeperiode). `gesamtbetrag_cent` - der tatsächlich geprüfte,
        # aus dem bestehenden Mahnkosten-Snapshot stammende, JETZT
        # einforderbare Betrag - wird deshalb als `faelliger_betrag_cent`
        # an den Renderer gereicht: weicht er vom vollen Saldo ab, zeigt
        # die Tabelle zusätzlich klar "Davon jetzt zu zahlen: X EUR" -
        # NIEMALS wird ein unbekannter/noch nicht fälliger Posten
        # eingefordert.
        zeichne_einfache_tabelle(
            pdf, einfache_zeilen, familie="Brief", anthrazit=anthrazit, gold=gold,
            faelliger_betrag_cent=gesamtbetrag_cent,
        )
    else:
        hauptforderung_cent = sum(f.betrag_cent for f in forderungszeilen)
        for f in forderungszeilen:
            faelligkeit_text = f"fällig seit {f.faelligkeit.strftime('%d.%m.%Y')}" if f.faelligkeit else "Fälligkeit ungeklärt"
            _zeile(pdf, f"{f.bezeichnung}, {faelligkeit_text}", f.betrag_cent)
            if f.urspruenglicher_betrag_cent is not None and f.urspruenglicher_betrag_cent != f.betrag_cent:
                angerechnet_cent = f.urspruenglicher_betrag_cent - f.betrag_cent
                _notizzeile(pdf, f"Ursprünglich {_eur(f.urspruenglicher_betrag_cent)}, davon bereits angerechnet {_eur(angerechnet_cent)}, Rest {_eur(f.betrag_cent)}.")
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
                # `segment.bis` ist EXKLUSIV (siehe `kosten.py::ZinsSegment`) -
                # angezeigt wird der tatsächlich letzte verzinste Tag.
                bis_einschliesslich = zins_bis_einschliesslich(segment.bis)
                _zeile(
                    pdf, f"{segment.von.strftime('%d.%m.%Y')}–{bis_einschliesslich.strftime('%d.%m.%Y')} (einschließlich): {satz}",
                    segment.zinsen_cent, einzug=6,
                )

        if neue_gebuehr_cent:
            _zeile(pdf, gebuehr_rechtsgrundlage or "Mahnspesen/Versandkosten", neue_gebuehr_cent)

        pdf.set_draw_color(*gold)
        pdf.line(_RAND, pdf.get_y() + 1, _INHALT_RECHTS, pdf.get_y() + 1)
        pdf.ln(3)
        _zeile(pdf, "Gesamtbetrag", gesamtbetrag_cent, fett=True)
    pdf.ln(4)

    # Korrektur nach unabhängiger Abnahme (Codex, 20.10.2026): KEIN
    # konkretes Kalenderdatum mehr nennen - der tatsächliche Zugang ist
    # zum Versandzeitpunkt noch nicht bekannt, ein daraus abgeleitetes
    # "bis {datum}" wäre ein vorgetäuschtes Zugangsdatum (siehe
    # `mahntexte.py::text_fuer_stufe`-Docstring, dieselbe Korrektur).
    # Abnahme-Folgefix (Codex 20.10.2026): "keine geprüfte Ziel-IBAN/
    # Kontoinhaber/Referenz" - ist eine eindeutige Bankkontobindung der
    # Vertrags-Gesellschaft auflösbar (siehe `indexautomatik/
    # mailversand_service.py::zahlungsziel_fuer_mahnung`), wird sie HIER
    # KONKRET genannt statt des bisherigen vagen Verweises; ohne
    # auflösbare Bindung bleibt der bisherige Text unverändert (kein
    # erfundener Platzhalter).
    # Nachtrag Markus 23.10.2026 (Gegenlesen von d52948f): die 14-Tage-
    # Frist steht bereits EINMAL in `mahntext.kerntext` (siehe oben) -
    # dieser Abschnitt nennt deshalb nur noch schlicht das Zahlungskonto,
    # OHNE die Frist/Zahlungsaufforderung ein zweites Mal zu wiederholen.
    pdf.set_font("Brief", "", 10)
    if zahlungsziel is not None and zahlungsziel.iban and zahlungsziel.kontoinhaber:
        pdf.set_font("Brief", "B", 10)
        pdf.cell(0, 5, "Zahlungskonto:", new_x="LMARGIN", new_y="NEXT")
        pdf.cell(0, 5, f"IBAN: {zahlungsziel.iban}", new_x="LMARGIN", new_y="NEXT")
        pdf.cell(0, 5, f"Kontoinhaber: {zahlungsziel.kontoinhaber}", new_x="LMARGIN", new_y="NEXT")
        pdf.cell(0, 5, f"Verwendungszweck: {zahlungsziel.verwendungszweck}", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)
        pdf.set_font("Brief", "", 10)
        pdf.multi_cell(
            0, 5.5,
            "Bei einer inzwischen erfolgten Zahlung senden Sie uns bitte den Zahlungsbeleg. Bei Fragen "
            "zur Forderung wenden Sie sich bitte an untenstehende Kontaktdaten.",
        )
    else:
        pdf.multi_cell(
            0, 5.5,
            "Die Zahlung erfolgt auf das Ihnen für dieses Mietverhältnis bekannt gegebene Konto. Bei "
            "einer inzwischen erfolgten Zahlung senden Sie uns bitte den Zahlungsbeleg. Bei Fragen zur "
            "Forderung wenden Sie sich bitte an untenstehende Kontaktdaten.",
        )
    if mahntext.rechtlicher_kern:
        pdf.ln(3)
        pdf.multi_cell(0, 5.5, mahntext.rechtlicher_kern)
    pdf.ln(6)
    # Auftrag HV-20261023-MAHNUNG-EINFACH (23.10.2026, verbindlicher
    # Darstellungsauftrag): der Briefabschluss ist jetzt FEST auf genau
    # diese drei Zeilen begrenzt ("Mit freundlichen Grüßen / JLB / Die
    # Hausverwaltung") - NICHT mehr `absender.name` (die volle
    # Firmenbezeichnung, z. B. "JLB Projects GmbH"), die weiterhin
    # vollständig im Fußzeilen-/Kontaktfooter steht (siehe `footer()`
    # oben). "JLB" ist dieselbe bestehende Marke wie `absender.name`,
    # keine Umfirmierung - KEINE persönliche Unterschrift.
    pdf.cell(0, 5, "Mit freundlichen Grüßen", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, "JLB", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, "Die Hausverwaltung", new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())
