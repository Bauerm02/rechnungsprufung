"""Tests für den PDF/A-Mahnbrief-Generator (mahnwesen/brief_pdf.py) -
reine Funktion, keine DB. Prüft PDF/A-2B-Konformitätserzwingung (fpdf2
`enforce_compliance`), EinfachBrief-Fensterposition, Adressvalidierung,
variable Zeilenhöhen und dass exakt dieselben Beträge wie in Vorschau/
E-Mail im Text erscheinen."""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

import pytest
from pypdf import PdfReader

from mietinkasso.mahnwesen.brief_pdf import (
    Absender, AdressfehlerError, Forderungszeile, Zinssegment, _MahnbriefPDF, _zeile,
    erzeuge_mahnbrief_pdf, pruefe_empfaengeradresse,
)


def _absender(**overrides) -> Absender:
    werte = dict(
        name="JLB Projects GmbH", adresse="Marc-Aurel-Straße 4/16, 1010 Wien",
        fn="FN 631126b, Handelsgericht Wien", uid="ATU81269707", telefon="+43 1 435 10 11",
        website="jlb-immo.at", email="hausverwaltung@jlb-immo.at",
        farbe_anthrazit="#1F2125", farbe_gold="#C9A86A",
    )
    werte.update(overrides)
    return Absender(**werte)


def _erzeugen(**overrides) -> bytes:
    werte = dict(
        absender=_absender(), empfaenger_name="Max Mustermieter", empfaenger_adresse="Musterstraße 1\n1010 Wien",
        objekt_bezeichnung="Am Corso", einheit_bezeichnung="Top 3", stufe=2, heute=date(2026, 9, 28),
        zahlungsfrist_bis=date(2026, 10, 12),
        forderungszeilen=[Forderungszeile("HMZ August", date(2026, 8, 5), 83_000)],
        bereits_offene_mahnkosten_cent=2073,
        neue_zinsen_delta_cent=54, zins_segmente=[], zinssatz_einheitlich_text="4,0 % p.a.",
        neue_gebuehr_cent=0, gebuehr_rechtsgrundlage=None,
        gesamtbetrag_cent=83_000 + 2073 + 54,
    )
    werte.update(overrides)
    return erzeuge_mahnbrief_pdf(**werte)


def test_erzeugtes_pdf_ist_lesbar_und_zeigt_denselben_gesamtbetrag_wie_die_vorschau():
    pdf_bytes = _erzeugen()
    assert pdf_bytes[:5] == b"%PDF-"
    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages) == 1
    text = reader.pages[0].extract_text()
    assert "Max Mustermieter" in text
    assert "Musterstraße 1" in text
    assert "Zweite Mahnung" in text
    assert "830,00" in text  # itemisierte Forderungszeile
    assert "20,73" in text  # bereits offene Mahnkosten
    assert "0,54" in text  # neue Zinsen
    assert "851,27" in text  # Gesamtbetrag - identisch zur Vorschau/E-Mail


def test_erste_stufe_heisst_zahlungserinnerung_nicht_mahnung():
    pdf_bytes = _erzeugen(
        stufe=1, forderungszeilen=[Forderungszeile("HMZ August", date(2026, 8, 5), 83_000)],
        bereits_offene_mahnkosten_cent=0, neue_zinsen_delta_cent=0, gesamtbetrag_cent=83_000,
    )
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = reader.pages[0].extract_text()
    assert "Zahlungserinnerung" in text
    assert "Zweite Mahnung" not in text


def test_ohne_konfiguriertes_logo_wird_kein_signet_gezeichnet():
    """Kein erfundenes JLB-Logo - ohne konfigurierten, tatsächlich
    vorhandenen Dateipfad bleibt der Brief ein reiner Textbrief."""

    pdf_bytes = _erzeugen(absender=_absender(logo_pfad="/pfad/existiert/nicht.png"))
    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages[0].images) == 0


def test_absenderdaten_stehen_in_fusszeile_zweizeilig_mit_seitenzahl():
    pdf_bytes = _erzeugen()
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = reader.pages[0].extract_text()
    assert "ATU81269707" in text
    assert "FN 631126b" in text
    assert "hausverwaltung@jlb-immo.at" in text
    assert "Seite 1/1" in text


def test_separater_headline_font_wird_nur_fuer_betreff_verwendet_wenn_konfiguriert():
    """Codex-Rückmeldung 14.09.2026: Forum als Headline-Font, EB Garamond
    als Fließtext - optional, ohne konfigurierten Pfad unverändertes
    Verhalten (Betreff im normalen Fett-Font)."""

    pdf_bytes = _erzeugen(absender=_absender(
        font_headline_pfad="/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ))
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = reader.pages[0].extract_text()
    assert "Zweite Mahnung" in text
    assert "Gesamtbetrag" in text  # Fließtext bleibt unverändert lesbar


def test_ohne_konfigurierten_headline_font_bleibt_verhalten_unveraendert():
    pdf_bytes = _erzeugen(absender=_absender(font_headline_pfad="/pfad/existiert/nicht.ttf"))
    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert "Zweite Mahnung" in reader.pages[0].extract_text()


def test_fehlende_eingebettete_schrift_wird_von_pdfa_erzwingung_abgelehnt():
    """Rückprüfung Codex 14.09.2026: PDF/A-Konformität wird über fpdf2s
    `enforce_compliance="PDF/A-2B"` erzwungen, nicht selbst nachgebaut -
    ein Verstoß (z. B. eine nicht eingebettete Basis-Schriftart) muss
    bereits bei der Erzeugung laut fehlschlagen."""

    from fpdf import FPDF
    from fpdf.errors import PDFAComplianceError

    pdf = FPDF(format="A4", unit="mm", enforce_compliance="PDF/A-2B")
    pdf.add_page()
    with pytest.raises(PDFAComplianceError):
        pdf.set_font("Helvetica", "", 11)


# -- EinfachBrief-Fensterspezifikation (Codex 14.09.2026: 20/62 mm, ------
# -- 90x45 mm, 5 mm Schutzzone, Empfänger 11pt linksbündig, max 6 Zeilen)


def test_fensterposition_entspricht_der_einfachbrief_spezifikation():
    absender = _absender()
    assert absender.fenster_links_mm == 20.0
    assert absender.fenster_oben_mm == 62.0
    assert absender.fenster_breite_mm == 90.0
    assert absender.fenster_hoehe_mm == 45.0


def test_pruefe_empfaengeradresse_lehnt_fehlende_adresse_ab():
    with pytest.raises(AdressfehlerError):
        pruefe_empfaengeradresse(None)
    with pytest.raises(AdressfehlerError):
        pruefe_empfaengeradresse("   ")


def test_pruefe_empfaengeradresse_lehnt_zu_lange_adresse_ab():
    with pytest.raises(AdressfehlerError):
        pruefe_empfaengeradresse("Z1\nZ2\nZ3\nZ4\nZ5\nZ6\nZ7")


def test_erzeuge_mahnbrief_pdf_lehnt_fehlende_adresse_ab_statt_platzhalter_zu_drucken():
    """Ein druckfertiger Brief darf NIE "Postadresse fehlt" als
    Empfänger zeigen - kontrolliertes Scheitern statt Überlauf/
    Platzhalter."""

    with pytest.raises(AdressfehlerError):
        _erzeugen(empfaenger_adresse="")


def test_erzeuge_mahnbrief_pdf_lehnt_zu_lange_empfaenger_plus_adresse_ab():
    with pytest.raises(AdressfehlerError):
        _erzeugen(empfaenger_name="Ein Sehr Langer Name GmbH & Co KG", empfaenger_adresse="Z1\nZ2\nZ3\nZ4\nZ5\nZ6")


def test_empfaengeradresse_wird_linksbuendig_nicht_im_blocksatz_gesetzt():
    """Rückprüfung Codex 14.09.2026: `multi_cell`s Standard-Blocksatz
    hatte kurze Firmennamen sichtbar gesperrt/gestreckt - der Empfänger
    wird jetzt zeilenweise über `cell(align="L")` gesetzt."""

    import inspect
    quelltext = inspect.getsource(erzeuge_mahnbrief_pdf)
    fenster_abschnitt = quelltext.split("Empfänger AUSSCHLIESSLICH")[1].split("Fließtext beginnt")[0]
    assert "multi_cell(" not in fenster_abschnitt


# -- Variable Zeilenhöhe für lange Bezeichnungen (keine Überschneidung ---
# -- mit der Betragsspalte) -----------------------------------------------


def test_lange_gebuehrenbezeichnung_laeuft_nicht_in_betragsspalte():
    lange_bezeichnung = (
        "§458 UGB - eine sehr lange, ausführliche Rechtsgrundlage-Beschriftung, die eigentlich in die "
        "Betragsspalte hineinlaufen würde, wenn sie nicht umgebrochen wird"
    )
    pdf_bytes = _erzeugen(neue_gebuehr_cent=4000, gebuehr_rechtsgrundlage=lange_bezeichnung, gesamtbetrag_cent=87_127)
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = reader.pages[0].extract_text()
    assert "40,00" in text
    assert "Rechtsgrundlage-Beschriftung" in text


def test_zinssegmente_werden_mit_zeitraum_und_satz_aufgelistet():
    """Für eine nachvollziehbare Mahnung müssen die tatsächlichen
    Zinszeiträume/-sätze im PDF stehen, nicht nur ein pauschaler
    Verweis auf "mehrere Sätze"."""

    segmente = [
        Zinssegment(date(2026, 6, 1), date(2026, 6, 30), Decimal("1.530"), 40),
        Zinssegment(date(2026, 7, 1), date(2026, 9, 28), Decimal("2.000"), 14),
    ]
    pdf_bytes = _erzeugen(
        neue_zinsen_delta_cent=54, zins_segmente=segmente, zinssatz_einheitlich_text=None,
        gesamtbetrag_cent=83_000 + 2073 + 54,
    )
    text = PdfReader(io.BytesIO(pdf_bytes)).pages[0].extract_text()
    # `Zinssegment.bis` ist EXKLUSIV - angezeigt wird der tatsächlich
    # letzte verzinste Tag (bis minus 1 Tag), ausdrücklich "einschließlich".
    assert "01.06.2026" in text and "29.06.2026" in text and "(einschließlich)" in text
    assert "1.530" in text
    assert "01.07.2026" in text and "27.09.2026" in text
    assert "2.000" in text


def test_mehrere_forderungszeilen_werden_einzeln_und_als_summe_gezeigt():
    zeilen = [
        Forderungszeile("Hauptmietzins August", date(2026, 8, 5), 50_000),
        Forderungszeile("Betriebskosten August", date(2026, 8, 5), 33_000),
    ]
    pdf_bytes = _erzeugen(
        forderungszeilen=zeilen, bereits_offene_mahnkosten_cent=0, neue_zinsen_delta_cent=0,
        gesamtbetrag_cent=83_000,
    )
    text = PdfReader(io.BytesIO(pdf_bytes)).pages[0].extract_text()
    assert "Hauptmietzins August" in text
    assert "500,00" in text
    assert "Betriebskosten August" in text
    assert "330,00" in text
    assert "Hauptforderung gesamt" in text
    assert "830,00" in text


# -- Mehrseitige Briefe: wiederholter Kopf + zweizeilige Fußzeile + -------
# -- Seitenzahl, kein Seitenumbruch mitten in einer Kosten-/Forderungszeile


def test_viele_forderungszeilen_erzeugen_wiederholten_kompakten_kopf_auf_folgeseiten():
    zeilen = [Forderungszeile(f"Hauptmietzins Monat {i}", date(2026, 1, i % 28 + 1), 5000 + i) for i in range(1, 30)]
    pdf_bytes = _erzeugen(
        forderungszeilen=zeilen, bereits_offene_mahnkosten_cent=0, neue_zinsen_delta_cent=0,
        gesamtbetrag_cent=sum(z.betrag_cent for z in zeilen),
    )
    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages) >= 2
    seite2_text = reader.pages[1].extract_text()
    assert "JLB Projects GmbH" in seite2_text  # wiederholter knapper Kopf
    assert "Zweite Mahnung" in seite2_text
    assert "Seite 2/" in seite2_text


def test_seitenumbruch_reisst_keine_betragsspalte_von_ihrer_bezeichnung_ab():
    """Rückprüfung Codex 14.09.2026, echter Bug: `_zeile` verwendete für
    Bezeichnung und Betrag denselben, vor dem Zeilenumbruch erfassten
    `y0` - brach `multi_cell` selbst mitten in der Zeile um, landete der
    Betrag verwaist auf der FOLGESEITE ohne seine Bezeichnung. `_zeile`
    bricht jetzt selbst VOR der Zeile um (`will_page_break`), damit
    Bezeichnung und Betrag immer auf derselben Seite beginnen."""

    zeilen = [Forderungszeile(f"Hauptmietzins Monat {i}, fällig seit 05.08.2026", date(2026, 8, 5), 5000 + i) for i in range(1, 30)]
    pdf_bytes = _erzeugen(
        forderungszeilen=zeilen, bereits_offene_mahnkosten_cent=0, neue_zinsen_delta_cent=0,
        gesamtbetrag_cent=sum(z.betrag_cent for z in zeilen),
    )
    reader = PdfReader(io.BytesIO(pdf_bytes))
    for seite in reader.pages:
        for zeile in seite.extract_text().splitlines():
            zeile = zeile.strip()
            if zeile.endswith("EUR") and "gesamt" not in zeile.lower() and "Gesamtbetrag" not in zeile:
                assert "Hauptmietzins" in zeile, f"verwaiste Betragszeile ohne Bezeichnung: {zeile!r}"


def test_will_page_break_verhindert_verwaisten_betrag_direkt_am_seitenumbruch():
    """Gezielter Grenzfalltest exakt am Seitenumbruch (unabhängig vom
    restlichen Briefaufbau)."""

    pdf = _MahnbriefPDF(format="A4", unit="mm", enforce_compliance="PDF/A-2B")
    pdf.farbe_anthrazit, pdf.farbe_gold = (0, 0, 0), (0, 0, 0)
    pdf.kopf_text, pdf.fuss_zeile1, pdf.fuss_zeile2 = "K", "F1", "F2"
    pdf.set_margins(20, 20, 20)
    pdf.set_auto_page_break(True, margin=24)
    pdf.alias_nb_pages()
    pdf.add_font("Brief", "", "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf")
    pdf.add_font("Brief", "B", "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf")
    pdf.add_page()
    pdf.set_y(270)  # knapp unter dem Umbruch-Trigger (297-24=273)
    _zeile(pdf, "Hauptmietzins Monat X, fällig seit 05.08.2026", 5000)
    assert pdf.page_no() == 2  # Zeile als Ganzes auf Seite 2 umgebrochen

    out = bytes(pdf.output())
    text = PdfReader(io.BytesIO(out)).pages[1].extract_text()
    assert "Hauptmietzins Monat X" in text
    assert "50,00" in text


def test_will_page_break_prueft_echte_mehrzeilige_hoehe_nicht_nur_eine_zeile():
    """Rückprüfung 14.09.2026, Befund A: `_zeile` prüfte den
    Seitenumbruch bisher gegen eine feste Einzelzeilenhöhe (5.5mm), nicht
    gegen die tatsächliche - hier ZWEIZEILIGE - Höhe des umgebrochenen
    Labels. Bei y=265 passt noch eine einzelne Zeile (5.5mm) vor dem
    Umbruch-Trigger (273), aber nicht das komplette zweizeilige Label
    (11mm) - `multi_cell` brach dadurch mitten in der Zeile selbst um,
    der Betrag landete mit dem alten `y0` verwaist auf Seite 2."""

    pdf = _MahnbriefPDF(format="A4", unit="mm", enforce_compliance="PDF/A-2B")
    pdf.farbe_anthrazit, pdf.farbe_gold = (0, 0, 0), (0, 0, 0)
    pdf.kopf_text, pdf.fuss_zeile1, pdf.fuss_zeile2 = "K", "F1", "F2"
    pdf.set_margins(20, 20, 20)
    pdf.set_auto_page_break(True, margin=24)
    pdf.alias_nb_pages()
    pdf.add_font("Brief", "", "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf")
    pdf.add_font("Brief", "B", "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf")
    pdf.add_page()
    pdf.set_y(265)  # eine Zeile (5.5mm) passt noch vor 273, zwei (11mm) nicht mehr
    label = (
        "SYNTHETIC Forderungsbeleg 11 – anteilige Miete einschließlich Küche, "
        "Parkplatz und gesondert vereinbarter Betriebskosten, fällig seit 05.08.2026"
    )
    _zeile(pdf, label, 10_011)
    assert pdf.page_no() == 2  # gesamte (zweizeilige) Zeile als Ganzes auf Seite 2

    out = bytes(pdf.output())
    seite1_text = PdfReader(io.BytesIO(out)).pages[0].extract_text()
    seite2_text = PdfReader(io.BytesIO(out)).pages[1].extract_text()
    assert "100,11" not in seite1_text  # kein verwaister Betrag auf der Vorseite
    assert "SYNTHETIC Forderungsbeleg 11" in seite2_text
    assert "100,11" in seite2_text


def test_mehrzeilige_forderungszeilen_reissen_betrag_nicht_von_ihrem_label_ab():
    """Exakte Rückprüfung 14.09.2026 (Befund A): 18 Forderungszeilen mit
    je zweizeiligem Label (EB Garamond beim Prüfenden, hier mit der
    Fallback-Schrift reproduziert - die Zeilenumbruch-Logik ist
    fontunabhängig) und Beträgen 10000+N Cent. Vorher landete der Betrag
    zur 11. Zeile (100,11 EUR) verwaist am Seitenende von Seite 2 vor der
    Fußzeile, ohne sein Label."""

    zeilen = [
        Forderungszeile(
            f"SYNTHETIC Forderungsbeleg {n} – anteilige Miete einschließlich Küche, "
            "Parkplatz und gesondert vereinbarter Betriebskosten",
            date(2026, 8, 5), 10_000 + n,
        )
        for n in range(1, 19)
    ]
    gesamt = sum(f.betrag_cent for f in zeilen)
    pdf_bytes = _erzeugen(
        forderungszeilen=zeilen, bereits_offene_mahnkosten_cent=0, neue_zinsen_delta_cent=0,
        gesamtbetrag_cent=gesamt,
    )
    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages) >= 2

    for n in range(1, 19):
        betrag_text = f"{(10_000 + n) / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        seiten_texte = [seite.extract_text() for seite in reader.pages]
        seiten_mit_betrag = [t for t in seiten_texte if betrag_text in t]
        assert seiten_mit_betrag, f"Betrag {betrag_text} fehlt komplett im PDF"
        for text in seiten_mit_betrag:
            assert f"Forderungsbeleg {n} " in text, (
                f"Betrag {betrag_text} steht ohne sein Label \"Forderungsbeleg {n}\" auf einer Seite"
            )


# -- Empfänger-Fensterbreite: einzelne, für sich genommen zu lange -------
# -- Zeile darf NICHT still aus dem 90mm-Fenster laufen (Rückprüfung -----
# -- 14.09.2026, Befund B) -------------------------------------------------


def test_zu_lange_einzelne_empfaengerzeile_wird_kontrolliert_umgebrochen():
    """Eine einzelne, für sich genommen zu breite Zeile (Name ODER
    Adresse) wird jetzt anhand der tatsächlichen 11pt-Fontbreite im
    90mm-Fenster umgebrochen statt mit `cell(90, 5)` unbeachtet ihrer
    Breite rechts aus dem Fenster zu laufen. Bleibt die Summe der
    umgebrochenen Zeilen bei höchstens 6, wird der Brief trotzdem
    erzeugt."""

    pdf_bytes = _erzeugen(
        empfaenger_name="Max Mustermieter",
        empfaenger_adresse="Sehr lange Musterstraße mit sehr vielen Wörtern die nicht in neunzig Millimeter passen 12-14/5\n1010 Wien",
    )
    assert pdf_bytes[:5] == b"%PDF-"
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = reader.pages[0].extract_text()
    assert "Sehr lange Musterstraße" in text
    assert "1010 Wien" in text


def test_zu_lange_einzelne_empfaengerzeile_die_trotz_umbruch_nicht_passt_wird_abgelehnt():
    """Vorher wurde nur die ROHE Zeilenanzahl (hier 3: Name + 2
    Adresszeilen) gegen das Limit von 6 geprüft und bestanden - die
    einzelnen Zeilen liefen dabei unbeachtet ihrer tatsächlichen Breite
    aus dem 90mm-Fenster. Ergibt der Umbruch anhand der echten
    11pt-Fontbreite in Summe mehr als 6 Zeilen, wird jetzt kontrolliert
    mit `AdressfehlerError` (422) abgelehnt statt still zu überlaufen."""

    with pytest.raises(AdressfehlerError):
        _erzeugen(
            empfaenger_name=(
                "Max Mustermieter mit außergewöhnlich langem doppeltem Nachnamen-Bindestrich-Kombination "
                "und noch einem Adelstitel obendrauf"
            ),
            empfaenger_adresse=(
                "Sehr lange Musterstraße mit sehr vielen Wörtern die nicht in neunzig Millimeter "
                "Fensterbreite passen 12-14/5/22\n1010 Wien"
            ),
        )
