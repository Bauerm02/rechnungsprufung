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
from mietinkasso.op.kontoblatt import (
    KATEGORIE_MAHNPAUSCHALE,
    KATEGORIE_MAHNSPESEN,
    KATEGORIE_VERZUGSZINSEN,
    STATUS_LABEL,
    Kontoblatt,
    erstelle_journal,
)


def _status_text(zeile) -> str:
    """`None` (z. B. GUTSCHRIFT/ZAHLUNG/KORREKTUR ohne eigenen
    Forderungscharakter) wird NIE als "bezahlt"/"offen" behauptet -
    Codex-Abnahme 20.10.2026, P3."""

    if zeile.status is None:
        return ""
    return f" · Status: {STATUS_LABEL.get(zeile.status, zeile.status)}"


def _ledger_text(zeile) -> str:
    """Zusatz Zinszuordnung (Folgeschritt zu Commit 239e33c): dieselben
    `MahnkostenBuchungTable`-Ledgerdetails wie in der HTML-Ansicht
    (`backoffice/routes/konten.py::_kontoblatt_ledger_html`), hier als
    einzeiliger Anhängsel-Text für `_zeile`'s `bezeichnung`. Fehlendes
    `ledger` wird ausdrücklich als "fehlend" gezeigt, nie ein erfundener
    Beleg."""

    if zeile.kategorie not in (KATEGORIE_VERZUGSZINSEN, KATEGORIE_MAHNSPESEN, KATEGORIE_MAHNPAUSCHALE):
        return ""
    ledger = zeile.ledger
    if ledger is None:
        return " · Mahnbeleg/Ledger: fehlend"
    forderungen_text = ", ".join(f"#{i}" for i in ledger.forderung_op_position_ids) or "—"
    text = f" · Mahnbeleg: {ledger.versandnachweis_referenz} · zugrunde liegende Forderungen: {forderungen_text}"
    if ledger.zins_nachweis_unvollstaendig:
        text += " · Zinsnachweis unvollständig/beschädigt - Originaldaten prüfen."
    return text


def _segment_text(s: dict) -> str:
    """EIN Zinssegment als Textzeile für das PDF - `rest_cent` (die
    tatsächlich verzinste Außenstandsbasis, kann sich zwischen
    Teilzahlungen ändern) fehlte bisher komplett (unabhängige Prüfung
    Commit 8ae0406, (1)); ohne sie ist nicht nachvollziehbar, WELCHER
    Außenstand mit `satz_prozent` verzinst wurde."""

    von, bis = s.get("von", "?"), s.get("bis", "?")
    satz = s.get("satz_prozent")
    satz_text = f"{satz} %" if satz else "Basis ungeklärt"
    quelle = s.get("quelle") or "?"
    rest_cent = s.get("rest_cent")
    basis_text = _eur(rest_cent) if isinstance(rest_cent, int) else "unbekannt"
    zinsen_cent = s.get("zinsen_cent")
    zinsen_text = _eur(zinsen_cent) if isinstance(zinsen_cent, int) else "unbekannt"
    return f"{von}–{bis}: {satz_text} ({quelle}) auf Basis {basis_text} -> {zinsen_text} kumulativ"


def _zins_zuordnung_zeilen_text(zeile) -> list[str]:
    """Je zugrunde liegender Forderung EINE eigene Textzeile - trennt
    scharf das vollständige, kumulative Segment-Bild (rein deskriptiv)
    vom tatsächlich an diesem Tag neu gebuchten Delta (NIE das eine aus
    dem anderen ableiten, siehe `kontoblatt.py`-Moduldoc). Nur für
    Verzugszinsen-Zeilen relevant - sonst leer."""

    if zeile.kategorie != KATEGORIE_VERZUGSZINSEN or zeile.ledger is None:
        return []
    texte = []
    for zuordnung in zeile.ledger.zins_zuordnung:
        segmente_text = "; ".join(_segment_text(s) for s in zuordnung.segmente) or "keine Segmente erfasst"
        delta_text = (
            _eur(zuordnung.delta_cent) if zuordnung.delta_cent is not None
            # Unabhängige Prüfung Commit 8ae0406, (4): "nicht gebucht"
            # behauptete fälschlich, dass für DIESE Forderung gar keine
            # Zinsen gebucht wurden - die Zinsbuchung insgesamt kann sehr
            # wohl erfolgt sein, nur OHNE nachvollziehbare Zuordnung zu
            # GENAU dieser Forderung.
            else "Zuordnung/Betrag nicht belegt"
        )
        texte.append(
            f"Forderung #{zuordnung.op_position_id} - kumulatives Zinsbild: {segmente_text} "
            f"/ tatsächlich an diesem Tag neu gebucht: {delta_text}"
        )
    return texte

_SEITE_BREITE = 210.0
_RAND = 20.0
_INHALT_RECHTS = _SEITE_BREITE - _RAND
_BETRAG_SPALTE_BREITE = 32.0
_REST_SPALTE_BREITE = 28.0
_LOGO_BREITE = 32.0
_LOGO_Y = 10.0
#: Codex-Abnahme 20.10.2026, P4, korrigiert nach unabhängiger Prüfung
#: gegen eine echte geschützte Kopie: das tatsächliche Produktionslogo
#: ist QUADRATISCH (32-mm-Breite => 32-mm-Höhe) - der ursprüngliche
#: Wert von 22.0 reichte dafür NICHT (Trennlinie/erster Betrag liefen
#: auf Folgeseiten durch das Logo). Jetzt 32 mm + 2 mm Marge als
#: Mindesthöhe/Fallback; `header()` verwendet zusätzlich bevorzugt die
#: TATSÄCHLICH gerenderte Höhe aus der `ImageInfo` von `image()`, falls
#: ein künftiges Logo ein anderes Seitenverhältnis hat. Der Kopf ist
#: zusätzlich dynamisch so hoch wie der TATSÄCHLICH umgebrochene
#: Titeltext, je nachdem, was größer ist.
_LOGO_HOEHE_RESERVIERT = 34.0


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
        # Codex-Abnahme 20.10.2026, P4: `cell()` bricht NICHT um - eine
        # lange echte Firmen-/Objekt-/Einheitsbezeichnung lief bisher
        # rechts aus der Zelle heraus (potenziell über das Logo). Jetzt
        # per `multi_cell` mehrzeilig, mit vorab per Dry-Run ermittelter
        # Höhe - die Trennlinie/der Fließtextbeginn liegen IMMER unter
        # dem jeweils höheren von Logo und (ggf. mehrzeiligem) Titel, auf
        # JEDER Seite gleich (kein Logo-/Farbimprovisation, dieselbe
        # Breite/Position wie bisher).
        #
        # Korrektur nach unabhängiger Prüfung gegen eine echte
        # geschützte Kopie: das Produktionslogo ist QUADRATISCH (32 mm
        # Breite => 32 mm Höhe) - der frühere Fallback-Wert (22 mm) war
        # dafür zu niedrig, wodurch auf Folgeseiten die Trennlinie/der
        # erste Betrag durch das Logo liefen. `image()` liefert in
        # `rendered_height` die TATSÄCHLICHE gerenderte Höhe (bereits in
        # mm, da `unit="mm"`) - diese wird bevorzugt verwendet (robust
        # auch gegen ein künftiges Logo mit anderem Seitenverhältnis),
        # mit `_LOGO_HOEHE_RESERVIERT` (32 mm + 2 mm Marge) als
        # garantierte Mindesthöhe.
        logo_hoehe_tatsaechlich: float | None = None
        if self.absender.logo_pfad and Path(self.absender.logo_pfad).is_file():
            bild_info = self.image(self.absender.logo_pfad, x=_INHALT_RECHTS - _LOGO_BREITE, y=_LOGO_Y, w=_LOGO_BREITE)
            logo_hoehe_tatsaechlich = getattr(bild_info, "rendered_height", None)
        if logo_hoehe_tatsaechlich:
            logo_reserviert = max(logo_hoehe_tatsaechlich + 2.0, _LOGO_HOEHE_RESERVIERT)
        else:
            logo_reserviert = _LOGO_HOEHE_RESERVIERT
        self.set_font(self.fett_familie, "B", 13)
        self.set_text_color(*self.farbe_anthrazit)
        titel_breite = _INHALT_RECHTS - _RAND - _LOGO_BREITE - 4
        titel_hoehe = self.multi_cell(titel_breite, 6, self.titel_zeile, align="L", dry_run=True, output=MethodReturnValue.HEIGHT)
        self.set_xy(_RAND, _LOGO_Y)
        self.multi_cell(titel_breite, 6, self.titel_zeile, align="L")
        linie_y = max(_LOGO_Y + logo_reserviert, _LOGO_Y + titel_hoehe + 2)
        self.set_draw_color(*self.farbe_gold)
        self.line(_RAND, linie_y, _INHALT_RECHTS, linie_y)
        self.set_y(linie_y + 6)

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


def _journal_tabellenkopf(pdf: FPDF) -> None:
    """Spaltenköpfe des chronologischen Journals (Fix7, unabhängige
    Rückprüfung Codex 22.10.2026, Sichtprüfung des kanonischen PDFs:
    "zwei Beträge nebeneinander ohne Kennzeichnung, welcher
    Buchungsbetrag und welcher laufender Saldo ist") - dieselben
    Spaltenbreiten/-positionen wie `_zeile`, damit Kopf und Inhalt
    exakt übereinanderstehen. Wird einmal vor dem Journal UND erneut
    nach jedem Seitenumbruch INNERHALB des Journals gezeichnet (siehe
    Aufrufer) - KEIN neues separates Blatt, nur eine wiederholte
    Kopfzeile derselben Tabelle; Logo/mehrseitiges Layout bleiben
    unverändert (`header()` oben wird dadurch nicht berührt)."""

    datum_breite = 24.0
    label_breite = (_INHALT_RECHTS - _RAND - datum_breite) - _BETRAG_SPALTE_BREITE - _REST_SPALTE_BREITE
    pdf.set_font("Konto", "B", 7.5)
    pdf.set_text_color(*pdf.farbe_anthrazit)
    x0, y0 = _RAND, pdf.get_y()
    pdf.set_xy(x0, y0)
    pdf.cell(datum_breite, 5, "Datum", align="L")
    pdf.set_xy(x0 + datum_breite, y0)
    pdf.cell(label_breite, 5, "Buchung", align="L")
    pdf.set_xy(x0 + datum_breite + label_breite, y0)
    pdf.cell(_BETRAG_SPALTE_BREITE, 5, "Betrag (+Soll,-Haben)", align="R")
    pdf.set_xy(x0 + datum_breite + label_breite + _BETRAG_SPALTE_BREITE, y0)
    pdf.cell(_REST_SPALTE_BREITE, 5, "Saldo", align="R")
    pdf.set_y(y0 + 5)
    pdf.set_draw_color(*pdf.farbe_gold)
    pdf.line(_RAND, pdf.get_y(), _INHALT_RECHTS, pdf.get_y())
    pdf.ln(1.5)
    pdf.set_font("Konto", "", 9.5)


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

    # Auftrag HV-20261022-EIN-KONTOBLATT: EIN gemeinsames, chronologisches
    # Journal (Miete/Zahlung/Gutschrift/Retourkapital/Mahngebühren/
    # Bankkosten/Zinsen in EINER Tabelle mit laufendem Saldo) statt
    # getrennter Kategorie-Abschnitte - identisch zur HTML-Ansicht
    # (`backoffice/kontoblatt_render.py::render_kontoblatt_html`) und zur
    # Mieterakte. Kurze Summen zuerst, dann das vollständige Journal -
    # darf über mehrere Druckseiten laufen (automatischer Seitenumbruch
    # über `_zeile`/`will_page_break`), es gibt aber bewusst KEIN
    # separates zweites Kosten-/Zinsblatt.
    pdf.set_font("Konto", "", 9.5)
    pdf.multi_cell(
        0, 5,
        f"Hauptforderung gebucht: {_eur(kontoblatt.hauptforderung_cent)} (offen: {_eur(kontoblatt.hauptforderung_offener_rest_cent)}) · "
        f"Kosten gebucht: {_eur(kontoblatt.kosten_cent)} (offen: {_eur(kontoblatt.kosten_offener_rest_cent)}) · "
        f"Zahlungs-/Stornostand: {_eur(kontoblatt.zahlungen_saldo_cent)}",
    )
    pdf.ln(1)

    _abschnitt_titel(pdf, "Kontoblatt — chronologisches Journal")
    journal = erstelle_journal(kontoblatt)
    if journal:
        # Fix7 (unabhängige Rückprüfung Codex 22.10.2026): explizite
        # Spaltenköpfe, VOR dem Journal UND nach jedem Seitenumbruch
        # INNERHALB des Journals wiederholt (siehe `_journal_tabellenkopf`-
        # Docstring) - vorher standen zwei Beträge (Buchungsbetrag/
        # laufender Saldo) nebeneinander ohne erkennbare Kennzeichnung.
        _journal_tabellenkopf(pdf)
        for jz in journal:
            seite_vor_zeile = pdf.page_no()
            status_text = _status_text(jz.zeile)
            if not jz.beeinflusst_saldo:
                status_text += " · wirkt sich NICHT auf den laufenden Saldo aus"
            bezeichnung = (
                f"{jz.buchungsart} · {jz.beleg_referenz}{status_text}{_ledger_text(jz.zeile)} · "
                f"Forderungsbezug: {jz.forderungsbezug}"
            )
            effekt_cent = jz.soll_cent - jz.haben_cent
            _zeile(
                pdf, datum_text=jz.datum.strftime("%d.%m.%Y"), bezeichnung=bezeichnung, betrag_cent=effekt_cent,
                rest_text=_eur(jz.laufender_saldo_cent),
            )
            if pdf.page_no() != seite_vor_zeile:
                _journal_tabellenkopf(pdf)
            pdf.set_font("Konto", "", 8.5)
            for zuordnung_text in _zins_zuordnung_zeilen_text(jz.zeile):
                seite_vor_detail = pdf.page_no()
                # Unabhängige Prüfung Commit 8ae0406, optionaler Hinweis:
                # "↳" (U+21B3) fehlt im Zeichensatz der Fallback-Schrift
                # (LiberationSerif) - ein einfacher, in jeder Schrift
                # vorhandener Einzug ("- ") statt eines möglichen
                # Platzhalter-/Fehlzeichens.
                pdf.multi_cell(0, 4.5, "    - " + zuordnung_text, new_x="LMARGIN", new_y="NEXT")
                if pdf.page_no() != seite_vor_detail:
                    _journal_tabellenkopf(pdf)
            pdf.set_font("Konto", "", 9.5)
    else:
        pdf.cell(0, 5, "Keine Buchungen.", new_x="LMARGIN", new_y="NEXT")
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
            # Codex-Abnahme 20.10.2026, P2: "Rückgabe" ist der TATSÄCHLICHE
            # Bankereignistag (`b.rueckgabe_datum`, aus der gebundenen
            # Rücklastschrift-OP-Zeile), NICHT `r.gebucht_am` (nur das
            # spätere Erfassungsdatum dieser Kosten-Dokumentationszeile) -
            # beide werden getrennt gezeigt, nie vermischt.
            rueckgabe_text = b.rueckgabe_datum.strftime("%d.%m.%Y") if b.rueckgabe_datum else "Datum unbekannt"
            erfasst_text = r.gebucht_am.strftime("%d.%m.%Y") if isinstance(r.gebucht_am, datetime) else ""
            bezeichnung = (
                f"Rückgabe {rueckgabe_text} · Erfasst am {erfasst_text} · Ursache: {r.rueckgabeursache} · "
                f"Beleg: {r.beleg_referenz} · Rücklastschrift-OP #{r.ruecklastschrift_op_position_id}"
                + (f" · Bank-Transaktion #{r.bank_transaktion_id}" if r.bank_transaktion_id is not None else "")
                + f" · Status: {b.status_text}"
            )
            _zeile(pdf, datum_text=rueckgabe_text, bezeichnung=bezeichnung, betrag_cent=r.bankentgelt_cent)
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

    # Fix6 (unabhängige Rückprüfung Codex 22.10.2026): ein Manualimport
    # OHNE Kosten-Freigabe (`pauschale_abgegolten=False`) erschien bisher
    # NIRGENDS auf diesem Kontoauszug - `erzeuge_kontoauszug_pdf` kannte
    # `ManuellerMahnversandTable` überhaupt nicht. Derselbe Belegstand wie
    # im HTML-Kontoblatt, KEINE erfundene 0,00-EUR-Summe.
    #
    # Zweite Rückprüfung Codex 22.10.2026, echter Anzeige-Mangel: Referenz/
    # Datum allein zeigten noch KEINEN Ansatz - jetzt zusätzlich der
    # unverbindliche, aktuell geltende Mahnpauschale-Prüfwert
    # (`a.vorschlag_cent`, identisch zur Freigabe-Maske) - "ungeprüfte
    # Ansätze sichtbar", NIE gebucht/versandt.
    if kontoblatt.ausstehende_manuelle_mahnversand:
        _abschnitt_titel(pdf, "Manuell dokumentierte Mahnschreiben (Kosten noch nicht gebucht)")
        pdf.set_font("Konto", "", 9)
        pdf.multi_cell(
            0, 5,
            "Für diese bereits erfassten, tatsächlich versandten Schreiben ist noch KEINE Kostenbuchung "
            "erfolgt (Freigabe/Bestätigung steht aus) - das bedeutet 'in Prüfung, noch nicht gebucht', "
            "NICHT 'keine Kosten'. Der gezeigte Ansatz ist der unverbindliche, aktuell geltende "
            "Mahnpauschale-Prüfwert - NICHT im Saldo enthalten, wird erst nach Freigabe/Bestätigung gebucht.",
        )
        pdf.ln(1)
        for a in kontoblatt.ausstehende_manuelle_mahnversand:
            z = a.row
            ansatz_text = (
                f"{_eur(a.vorschlag_cent)} (unverbindlich, in Prüfung)" if a.vorschlag_cent is not None
                else "kein Profilwert hinterlegt"
            )
            bezeichnung = (
                f"Stufe {z.stufe} · Kanal {z.kanal} · Versanddatum {z.versanddatum.strftime('%d.%m.%Y')} · "
                f"Beleg {z.beleg_referenz} · Empfänger {z.empfaenger_name} · Ansatz: {ansatz_text}"
            )
            pdf.set_font("Konto", "", 9.5)
            pdf.multi_cell(0, 5, bezeichnung, new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())
