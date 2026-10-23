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

from mietinkasso.infrastructure.db.tables import OPPositionTable
from mietinkasso.mahnwesen.brief_pdf import (
    Absender, AdressfehlerError, Forderungszeile, Zahlungsziel, Zinssegment, _MahnbriefPDF, _zeile,
    _INHALT_RECHTS, _LOGO_BREITE, _LOGO_X, _LOGO_Y, _RAND, _SEITE_BREITE, _SPERRBEREICH_BREITE,
    _SPERRBEREICH_HOEHE, erzeuge_mahnbrief_pdf, pruefe_empfaengeradresse,
)
from mietinkasso.op.kontoblatt import erstelle_kontoblatt


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
        objekt_bezeichnung="Am Corso", einheit_bezeichnung="Top 3", stufe=2, rechtsordnung="OESTERREICH_MRG_VOLL",
        heute=date(2026, 9, 28), zahlungsfrist_bis=date(2026, 10, 12),
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


def _normalisiert(text: str) -> str:
    """`multi_cell`-Blocksatz fügt variable doppelte Leerzeichen/Zeilen-
    umbrüche mitten im Satz ein - für robuste Substring-Prüfungen wird
    jede Lauflänge Whitespace auf genau ein Leerzeichen reduziert."""

    return " ".join(text.split())


def test_ohne_zahlungsziel_bleibt_bisheriger_allgemeiner_hinweistext():
    pdf_bytes = _erzeugen()
    text = _normalisiert(PdfReader(io.BytesIO(pdf_bytes)).pages[0].extract_text())
    assert "auf das Ihnen für dieses Mietverhältnis bekannt gegebene Konto" in text
    assert "IBAN" not in text


def test_konfiguriertes_zahlungsziel_nennt_iban_kontoinhaber_und_verwendungszweck():
    """Abnahme-Folgefix (Codex 20.10.2026): "keine geprüfte Ziel-IBAN/
    Kontoinhaber/Referenz" - ist ein Zahlungsziel übergeben, wird es
    konkret im Brief genannt statt des vagen Hinweises."""

    pdf_bytes = _erzeugen(
        zahlungsziel=Zahlungsziel(
            iban="AT611904300234573201", kontoinhaber="JLB Projects GmbH",
            verwendungszweck="Mahnung Stufe 2, Vertrag V-601-1",
        ),
    )
    text = " ".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf_bytes)).pages)
    assert "AT611904300234573201" in text
    assert "JLB Projects GmbH" in text
    assert "V-601-1" in text
    assert "auf das Ihnen für dieses Mietverhältnis bekannt gegebene Konto" not in text


def test_zahlungsziel_ohne_vollstaendige_angaben_faellt_auf_allgemeinen_text_zurueck():
    """Eine nur teilweise konfigurierte Kontoverbindung (z. B. IBAN ohne
    Kontoinhaber) wird NIE unvollständig gedruckt - dann gilt weiterhin
    der allgemeine Hinweistext (kein erfundener Kontoinhaber)."""

    pdf_bytes = _erzeugen(zahlungsziel=Zahlungsziel(iban="AT611904300234573201", kontoinhaber=None, verwendungszweck="x"))
    text = _normalisiert(PdfReader(io.BytesIO(pdf_bytes)).pages[0].extract_text())
    assert "auf das Ihnen für dieses Mietverhältnis bekannt gegebene Konto" in text
    assert "AT611904300234573201" not in text


def test_ursprungsbetrag_und_angerechnete_zahlung_werden_pro_position_ausgewiesen():
    """Abnahme-Folgefix (Codex 20.10.2026): "keine vollständige Periode/
    Position/Ursprungsbetrag/angerechnete Zahlungen/Rest-Tabelle"."""

    pdf_bytes = _erzeugen(
        forderungszeilen=[
            Forderungszeile("HMZ August", date(2026, 8, 5), 60_000, urspruenglicher_betrag_cent=83_000),
        ],
        bereits_offene_mahnkosten_cent=0, neue_zinsen_delta_cent=0, gesamtbetrag_cent=60_000,
    )
    text = PdfReader(io.BytesIO(pdf_bytes)).pages[0].extract_text()
    assert "Ursprünglich" in text
    assert "830,00" in text  # Ursprungsbetrag
    assert "230,00" in text  # bereits angerechnet (830,00 - 600,00)
    assert "600,00" in text  # Rest


def test_identischer_ursprungsbetrag_zeigt_keine_zusaetzliche_zeile():
    """Keine unpassende Zusatzzeile, wenn keine Zahlung angerechnet wurde
    (Ursprungsbetrag == Rest)."""

    pdf_bytes = _erzeugen(
        forderungszeilen=[
            Forderungszeile("HMZ August", date(2026, 8, 5), 83_000, urspruenglicher_betrag_cent=83_000),
        ],
        bereits_offene_mahnkosten_cent=0, neue_zinsen_delta_cent=0, gesamtbetrag_cent=83_000,
    )
    text = PdfReader(io.BytesIO(pdf_bytes)).pages[0].extract_text()
    assert "Ursprünglich" not in text


def test_erste_stufe_heisst_zahlungserinnerung_nicht_mahnung():
    pdf_bytes = _erzeugen(
        stufe=1, forderungszeilen=[Forderungszeile("HMZ August", date(2026, 8, 5), 83_000)],
        bereits_offene_mahnkosten_cent=0, neue_zinsen_delta_cent=0, gesamtbetrag_cent=83_000,
    )
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = reader.pages[0].extract_text()
    assert "Zahlungserinnerung" in text
    assert "Zweite Mahnung" not in text


def test_vierte_stufe_zeigt_letzte_zahlungsaufforderung_mit_gesetzlichem_kern():
    """Auftrag HV-20261020-VIER-MAHNSTUFEN, gekürzt durch Auftrag
    HV-20261023-MAHNUNG-EINFACH (23.10.2026): die Stufe-4-Mahnung muss
    weiterhin den kurzen, konditionalen rechtlichen Kern (RA-Übergabe,
    mögliche Aufkündigung/Auflösung/Räumung NUR bei Vorliegen der
    gesetzlichen Voraussetzungen, 'KEINE Kündigung') im gerenderten PDF
    zeigen - OHNE Paragraphen-Nennung (die konkrete §-Herleitung bleibt
    seit HV-20261023-MAHNUNG-EINFACH ausschließlich intern, siehe
    test_mahnwesen_mahntexte.py)."""

    pdf_bytes = _erzeugen(
        stufe=4, rechtsordnung="OESTERREICH_MRG_VOLL",
        forderungszeilen=[Forderungszeile("HMZ August", date(2026, 8, 5), 83_000)],
        bereits_offene_mahnkosten_cent=0, neue_zinsen_delta_cent=0, gesamtbetrag_cent=83_000,
    )
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = " ".join(p.extract_text() for p in reader.pages).replace("\n", " ")
    assert "Letzte Zahlungsaufforderung" in text
    assert "Rechtsanwalt" in text
    assert "Räumung" in text
    assert "gesetzlichen Voraussetzungen" in text
    assert "KEINE Kündigung" in text
    assert "MRG" not in text
    assert "ABGB" not in text


def test_vierte_stufe_auslaendische_rechtsordnung_zeigt_keine_oesterreichische_norm():
    pdf_bytes = _erzeugen(
        stufe=4, rechtsordnung="DEUTSCHLAND",
        forderungszeilen=[Forderungszeile("HMZ August", date(2026, 8, 5), 83_000)],
        bereits_offene_mahnkosten_cent=0, neue_zinsen_delta_cent=0, gesamtbetrag_cent=83_000,
    )
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = " ".join(p.extract_text() for p in reader.pages).replace("\n", " ")
    assert "MRG" not in text
    assert "ABGB" not in text


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


# -- EinfachBrief-Freihaltebereiche (Codex-Rückmeldung 19.10.2026, --------
# -- visuelle Prüfung des Original-Anhangs "EinfachBrief-Freihaltebereiche.pdf")


_SEITE_HOEHE = 297.0  # A4


def test_sperrbereich_oben_links_ist_52x60mm_plus_5mm_schutzabstand():
    assert _SPERRBEREICH_BREITE == 52.0 + 5.0
    assert _SPERRBEREICH_HOEHE == 60.0 + 5.0


def test_logo_steht_rechtsbuendig_ausserhalb_des_oben_links_sperrbereichs():
    """Logo x=20/y=12/w=32 lag vollständig im 52x60-mm-Sperrbereich -
    jetzt rechtsbündig (x >= Sperrbereichsbreite genügt unabhängig von y,
    da ein Rechteck nur verlassen wird, wenn mindestens eine Koordinate
    außerhalb liegt)."""

    assert _LOGO_X >= _SPERRBEREICH_BREITE
    assert _LOGO_X + _LOGO_BREITE == _INHALT_RECHTS  # rechtsbündig zum Inhaltsrand
    assert _LOGO_Y >= 5.0  # äußerer 5-mm-Rand von der Papierkante oben


def test_absenderzeile_steht_als_oberste_zeile_innerhalb_des_empfaengerfensters():
    """Konkreter, offiziell belegter Layoutnachweis (https://www.einfach-
    brief.at/fe/vorlagen + Original-DOCX-Einschreibenvorlage, Codex-
    Rückmeldung 19.10.2026): die Post verlangt die kurze Absenderzeile
    NICHT mehr außerhalb, sondern als oberste Zeile INNERHALB des
    Empfängerfensters (x=`fenster_links_mm`/y=`fenster_oben_mm`), 8pt,
    linksbündig, mit einer Leerzeile Abstand zur darunterstehenden
    Empfängeradresse - unabhängig davon, ob ein Logo konfiguriert ist
    (das Logo bleibt unverändert rechtsbündig oben, siehe
    `test_logo_steht_rechtsbuendig_ausserhalb_des_oben_links_sperrbereichs`).
    Es gibt dabei bewusst nur EINE Absenderzeile, keine zweite beim Logo."""

    absender = _absender(logo_pfad=None)
    pdf_bytes = _erzeugen(absender=absender)
    assert pdf_bytes[:5] == b"%PDF-"
    text = PdfReader(io.BytesIO(pdf_bytes)).pages[0].extract_text()
    absenderzeile = f"{absender.name} · {absender.adresse}"
    assert absenderzeile in text
    assert "Max Mustermieter" in text
    # Textflussreihenfolge: die Absenderzeile (oberste Fensterzeile) steht
    # VOR der Empfängeradresse darunter.
    assert text.index(absenderzeile) < text.index("Max Mustermieter")

    # Geometrische Prüfung über den tatsächlich steuernden Code: die
    # Absenderzeile wird bei (`fenster_links_mm`/`fenster_oben_mm`)
    # linksbündig gesetzt, nicht mehr rechtsbündig bei `_SPERRBEREICH_BREITE`.
    import inspect
    quelltext = inspect.getsource(erzeuge_mahnbrief_pdf)
    absender_abschnitt = quelltext.split("Kurze Absenderzeile")[1].split("Empfänger AUSSCHLIESSLICH")[0]
    assert "pdf.set_xy(absender.fenster_links_mm, absender.fenster_oben_mm)" in absender_abschnitt
    assert 'align="L"' in absender_abschnitt
    assert "_SPERRBEREICH_BREITE" not in absender_abschnitt


def test_absenderzeile_hat_mit_logo_dieselbe_fensterposition():
    """Dieselbe Geometrie gilt unverändert auch MIT konfiguriertem Logo -
    das Logo bleibt unabhängig rechtsbündig oben; es wird (anders als vor
    dieser Korrektur) nur noch EIN `pdf.cell(...)`-Aufruf für die
    Absenderzeile ausgeführt, nicht mehr ein zweiter, konkurrierender
    beim Logo."""

    absender = _absender(logo_pfad="/pfad/existiert/nicht.png")  # Pfad existiert nicht -> kein Logo gezeichnet
    pdf_bytes = _erzeugen(absender=absender)
    text = PdfReader(io.BytesIO(pdf_bytes)).pages[0].extract_text()
    assert f"{absender.name} · {absender.adresse}" in text

    import inspect
    quelltext = inspect.getsource(erzeuge_mahnbrief_pdf)
    logo_bis_fliesstext = quelltext.split("if absender.logo_pfad")[1].split("Fließtext beginnt")[0]
    assert logo_bis_fliesstext.count(f'pdf.cell(absender.fenster_breite_mm, 4, f"{{absender.name}}') == 1


def test_folgeseitenkopf_steht_ebenfalls_ausserhalb_des_sperrbereichs():
    """Der laufende Kopf ab Seite 2 (`_MahnbriefPDF.header`) darf
    denselben oben-links-Sperrbereich ebenfalls nicht mehr verletzen -
    erzwingt mehrseitige Briefe (viele Forderungszeilen) und prüft, dass
    das PDF weiterhin fehlerfrei und mehrseitig erzeugt wird."""

    viele_zeilen = [Forderungszeile(f"HMZ Monat {i}", date(2026, 1, 1), 10_000) for i in range(1, 40)]
    pdf_bytes = _erzeugen(
        forderungszeilen=viele_zeilen, gesamtbetrag_cent=10_000 * len(viele_zeilen) + 2073 + 54,
    )
    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages) > 1
    # Der Kopf selbst startet laut Code jetzt bei `_SPERRBEREICH_BREITE`
    # (siehe `_MahnbriefPDF.header`), nicht mehr bei `_RAND` - geometrisch
    # über die Konstante geprüft, die den tatsächlichen Aufruf steuert.
    assert _SPERRBEREICH_BREITE >= 52.0 + 5.0
    assert _SPERRBEREICH_BREITE < _INHALT_RECHTS  # bleibt trotzdem genug Platz für den Kopftext


def _textpositionen_mm(page) -> list[tuple[str, float, float]]:
    """Liest die TATSÄCHLICH gerenderten Textpositionen einer `pypdf`-Seite
    aus (`extract_text(visitor_text=...)` liefert die bereits mit `cm`
    kombinierte Textmatrix `tm`, deren `e`/`f` die absolute Position in
    PDF-Punkten von links/unten sind) und rechnet sie in Millimeter von
    links/OBEN um (fpdf2s Koordinatensystem) - damit lässt sich ein
    reales, gerendertes PDF geometrisch gegen die mm-Konstanten dieses
    Moduls prüfen, nicht nur der Quellcode der Zeichenfunktion selbst."""

    pt_je_mm = 72.0 / 25.4
    hoehe_pt = float(page.mediabox.height)
    treffer: list[tuple[str, float, float]] = []

    def visitor(text, cm, tm, font_dict, font_size):
        if text.strip():
            x_mm = tm[4] / pt_je_mm
            y_von_oben_mm = (hoehe_pt - tm[5]) / pt_je_mm
            treffer.append((text, x_mm, y_von_oben_mm))

    page.extract_text(visitor_text=visitor)
    return treffer


def test_koerperzeilen_auf_seite_2_bleiben_ausserhalb_des_oben_links_sperrbereichs():
    """Codex-Abnahme 19.10.2026, dritte Korrektur: der bisherige Test oben
    prüfte NUR, dass der Kopf selbst (`_MahnbriefPDF.header`) außerhalb des
    Sperrbereichs liegt - `header()` setzte den Fließtext danach aber über
    `set_y(20)` fort, was links bei `_RAND` (20 mm) mitten im 52x60-mm-
    Sperrbereich (x<57mm, y<65mm) begann. Dieser Test rendert ein ECHTES
    mehrseitiges PDF und prüft geometrisch (über die tatsächlich gezeichnete
    Textposition, nicht nur den Quellcode), dass auf Seite 2 KEIN Text mit
    x < `_SPERRBEREICH_BREITE` oberhalb von `_SPERRBEREICH_HOEHE` steht -
    nur der rechtsbündige Kopf selbst (x >= `_SPERRBEREICH_BREITE`) darf in
    diesem Höhenbereich erscheinen."""

    viele_zeilen = [Forderungszeile(f"HMZ Monat {i}", date(2026, 1, 1), 10_000) for i in range(1, 40)]
    pdf_bytes = _erzeugen(
        forderungszeilen=viele_zeilen, gesamtbetrag_cent=10_000 * len(viele_zeilen) + 2073 + 54,
    )
    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages) > 1

    seite2 = reader.pages[1]
    treffer = _textpositionen_mm(seite2)
    assert treffer  # die Seite ist nicht leer - der Test prüft etwas Echtes

    verletzungen = [
        (text, x, y) for text, x, y in treffer
        if x < _SPERRBEREICH_BREITE and y < _SPERRBEREICH_HOEHE
    ]
    assert verletzungen == [], (
        f"Text im Sperrbereich auf Seite 2 gefunden (x<{_SPERRBEREICH_BREITE}mm, "
        f"y<{_SPERRBEREICH_HOEHE}mm): {verletzungen}"
    )

    # Der Kopf selbst (rechtsbündig ab `_SPERRBEREICH_BREITE`) steht
    # weiterhin oben auf Seite 2 - keine stillschweigende Verschiebung
    # des gesamten Kopfes nach unten, nur der Fließtext darunter rutscht.
    kopf_treffer = [(text, x, y) for text, x, y in treffer if y < _SPERRBEREICH_HOEHE]
    assert kopf_treffer, "Der Folgeseitenkopf selbst muss weiterhin oben auf Seite 2 stehen."
    assert all(x >= _SPERRBEREICH_BREITE for _, x, _ in kopf_treffer)


def test_linker_rand_und_aeusserer_rand_erfuellen_bereits_bestehende_freihaltebereiche():
    """Separat genannter Freihaltebereich "links 18 mm bis 160 mm Höhe"
    sowie ein äußerer 5-mm-Rand ringsum: beide waren schon durch den
    bestehenden Inhaltsrand (`_RAND`) erfüllt, ohne Codeänderung - hier
    nur geometrisch festgeschrieben, damit eine künftige Änderung von
    `_RAND` das nicht stillschweigend wieder verletzt."""

    assert _RAND >= 18.0
    assert _RAND >= 5.0
    assert _SEITE_BREITE - _INHALT_RECHTS >= 5.0  # rechter äußerer Rand


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


# -- Auftrag HV-20261023-MAHNUNG-EINFACH, Abnahmegrenze 23.10.2026: -------
# "nur das Kontoblatt als zweite PDF-Seite anzuhängen und vorne die alte
# Forderungsliste zu lassen erfüllt den Nutzerauftrag nicht - der
# eigentliche Mahnbrief braucht die chronologische Tabelle bereits auf
# seiner ersten Briefseite." `kontoblatt=None` (Standard, siehe alle
# bisherigen Tests oben) lässt die alte Forderungsliste unverändert -
# erst `kontoblatt=<echtes Kontoblatt>` schaltet auf die neue, kurze
# Empfänger-Tabelle um.


def _op(id: int, typ: str, betrag_cent: int, *, belegdatum: date, leistungsperiode: str | None = None, faelligkeit=None) -> OPPositionTable:
    return OPPositionTable(
        id=id, konto_id="K-1", typ=typ, betrag_cent=betrag_cent, belegdatum=belegdatum, buchungsdatum=belegdatum,
        faelligkeit=faelligkeit, faelligkeit_bekannt=faelligkeit is not None, status="AKTIV", quelle_system=None,
        storniert_durch_id=None, beleg_referenz=None, leistungsperiode=leistungsperiode,
    )


def _kontoblatt(*positionen: OPPositionTable):
    saldo = sum((p.betrag_cent if p.typ in ("SOLL", "EROEFFNUNG", "RUECKLASTSCHRIFT") else -p.betrag_cent) for p in positionen)
    return erstelle_kontoblatt(
        konto_id="K-1", saldo_cent=saldo, faelliger_unstrittiger_rest_cent=saldo,
        positionen_aktiv=list(positionen), positionen_alle=list(positionen),
        offene_forderungen_rest_je_id={}, mahnkosten_buchungen=[], ruecklastschriftkosten_zeilen=[], mahnlaeufe=[],
    )


def test_kontoblatt_ersetzt_forderungsliste_mit_kurzer_tabelle_bereits_auf_erster_briefseite():
    """Wörtliche Abnahmevorgabe: ein synthetischer Brief mit NUR einer
    Miete muss bereits auf seiner ERSTEN Briefseite Ausgangssaldo und
    Tabelle zeigen - nicht nur als angehängte zweite PDF-Seite."""

    miete = _op(1, "SOLL", 100_000, belegdatum=date(2026, 1, 1), leistungsperiode="2026-01", faelligkeit=date(2026, 1, 5))
    kontoblatt = _kontoblatt(miete)

    pdf_bytes = _erzeugen(
        forderungszeilen=[], bereits_offene_mahnkosten_cent=0, neue_zinsen_delta_cent=0, neue_gebuehr_cent=0,
        gesamtbetrag_cent=100_000, heute=date(2026, 1, 20), kontoblatt=kontoblatt,
    )
    reader = PdfReader(io.BytesIO(pdf_bytes))
    erste_seite = reader.pages[0].extract_text()
    text = _normalisiert(erste_seite)

    assert "Ihre Kontoaufstellung" in text
    assert "Ausgangssaldo" in text
    assert "Mietvorschreibung Jänner 2026, fällig 05.01.2026" in text
    assert "Gesamt offen" in text and "1.000,00 EUR" in text
    # Die alte, nach Kategorien aufgeschlüsselte Forderungsliste ist
    # vollständig ersetzt, nicht nur ergänzt.
    assert "Hauptforderung gesamt" not in text
    # Keine internen Referenzen/IDs im Empfängertext.
    assert "OP #" not in text and "K-1" not in text


def test_kontoblatt_none_belaesst_alte_forderungsliste_unveraendert():
    """Rückwärtskompatibilität: bestehende Aufrufer OHNE `kontoblatt`
    (Standard `None`) sind von der neuen Tabelle nicht betroffen - die
    alte Forderungsliste bleibt unverändert (siehe alle Tests oben)."""

    pdf_bytes = _erzeugen()
    text = _normalisiert(PdfReader(io.BytesIO(pdf_bytes)).pages[0].extract_text())
    assert "Hauptforderung gesamt" in text
    assert "Ihre Kontoaufstellung" not in text


def test_kontoblatt_mit_vorschau_merge_zeigt_woertliches_zahlenbeispiel_1029():
    """Wörtliches Zahlenbeispiel aus dem Auftrag (synthetisch):
    Ausgangssaldo 0; Miete 1000; Zahlung -1000; Rückbuchung +1000;
    geprüfte Bankspesen +7; zulässige, noch NICHT gebuchte Mahngebühr
    +20; noch NICHT gebuchte Verzugszinsen +2 -> Gesamt offen 1029,00 EUR
    - angehängt über `erweitere_um_vorschau` (`zins_von`/`zins_bis`/
    `zinssatz_prozent`/`neue_gebuehr_cent`), OHNE jede DB-Buchung (reine
    Darstellung)."""

    miete = _op(1, "SOLL", 100_000, belegdatum=date(2026, 1, 1), leistungsperiode="2026-01")
    zahlung = _op(2, "ZAHLUNG", 100_000, belegdatum=date(2026, 1, 5))
    ruecklastschrift = _op(3, "RUECKLASTSCHRIFT", 100_000, belegdatum=date(2026, 1, 10))
    bankspesen = _op(4, "SOLL", 700, belegdatum=date(2026, 1, 11))
    kontoblatt = _kontoblatt(miete, zahlung, ruecklastschrift, bankspesen)

    pdf_bytes = _erzeugen(
        forderungszeilen=[], bereits_offene_mahnkosten_cent=0, neue_zinsen_delta_cent=200, neue_gebuehr_cent=2_000,
        gesamtbetrag_cent=102_900, heute=date(2026, 1, 20), kontoblatt=kontoblatt,
        zins_von=date(2026, 1, 6), zins_bis=date(2026, 1, 20), zinssatz_prozent=Decimal("4.0"),
    )
    text = _normalisiert(PdfReader(io.BytesIO(pdf_bytes)).pages[0].extract_text())
    assert "Gesamt offen" in text and "1.029,00 EUR" in text
    # Zinsen und Gebühr erscheinen GENAU EINMAL (keine konkurrierende
    # zweite Summe/kein doppelter Ansatz).
    assert text.count("Gesamt offen") == 1


# -- Gegenlesen von Commit d52948f (Codex, 23.10.2026): P1, echter --------
# Fachfehler - der Brief durfte den VOLLEN Kontostand nicht unbesehen als
# "jetzt zu zahlen" einfordern, wenn das Konto weitere, noch nicht
# mahnbare Positionen trägt.


def test_davon_jetzt_zu_zahlen_zeigt_nur_den_geprueften_faelligen_teilbetrag():
    """Repro aus dem Abnahmehinweis: 100 EUR fällige Miete + 900 EUR
    erst später fällige Miete, Kontosaldo 1.000 EUR. Der volle Saldo darf
    sichtbar sein, verbindlich einzufordern (und über `gesamtbetrag_cent`
    aus dem bestehenden, geprüften Mahnkosten-Snapshot vorgegeben) sind
    aber NUR die 100 EUR - niemals ein unbekannter/noch nicht fälliger
    Posten."""

    faellige_miete = _op(1, "SOLL", 10_000, belegdatum=date(2026, 1, 1), leistungsperiode="2026-01", faelligkeit=date(2026, 1, 5))
    spaetere_miete = _op(2, "SOLL", 90_000, belegdatum=date(2026, 2, 1), leistungsperiode="2026-02", faelligkeit=date(2026, 2, 5))
    kontoblatt = _kontoblatt(faellige_miete, spaetere_miete)

    pdf_bytes = _erzeugen(
        forderungszeilen=[], bereits_offene_mahnkosten_cent=0, neue_zinsen_delta_cent=0, neue_gebuehr_cent=0,
        gesamtbetrag_cent=10_000, heute=date(2026, 1, 20), kontoblatt=kontoblatt,
    )
    text = _normalisiert(PdfReader(io.BytesIO(pdf_bytes)).pages[0].extract_text())
    assert "Gesamt offen" in text and "1.000,00 EUR" in text  # voller Kontostand bleibt sichtbar
    assert "Davon jetzt zu zahlen" in text and "100,00 EUR" in text  # nur der geprüfte fällige Teil
    # Die Pflichtaufforderung im Fließtext bezieht sich auf "den ...
    # fälligen Rückstand" (siehe `mahntexte.py`) - nicht auf "den
    # Gesamtbetrag"/den vollen Kontostand.
    assert "fälligen Rückstand" in text


def test_davon_jetzt_zu_zahlen_entfaellt_wenn_voller_saldo_bereits_faellig_ist():
    """Stimmen voller Kontostand und geprüfter fälliger Betrag überein
    (keine weitere, noch nicht mahnbare Position), entfällt die
    Zusatzzeile - keine redundante Wiederholung derselben Zahl."""

    nur_faellige_miete = _op(1, "SOLL", 10_000, belegdatum=date(2026, 1, 1), leistungsperiode="2026-01", faelligkeit=date(2026, 1, 5))
    kontoblatt = _kontoblatt(nur_faellige_miete)

    pdf_bytes = _erzeugen(
        forderungszeilen=[], bereits_offene_mahnkosten_cent=0, neue_zinsen_delta_cent=0, neue_gebuehr_cent=0,
        gesamtbetrag_cent=10_000, heute=date(2026, 1, 20), kontoblatt=kontoblatt,
    )
    text = PdfReader(io.BytesIO(pdf_bytes)).pages[0].extract_text()
    assert "Davon jetzt zu zahlen" not in text


# -- Gegenlesen von Commit d52948f: Mehrseitenkopf-Reihenfolge -----------


def test_mehrseitenkopf_steht_vor_der_umbrechenden_zeile_nicht_danach():
    """Echter Layoutfehler: die vorherige Fassung zeichnete bei einer
    umbrechenden Zeile zuerst die Zeile auf der neuen Seite und ERST
    DANACH den wiederholten Tabellenkopf darunter - exakt umgekehrte
    Reihenfolge. Viele Positionen erzwingen hier mehrere Seiten; auf
    JEDER Folgeseite muss "Datum"/"Buchung" vor der ersten Buchungszeile
    dieser Seite im extrahierten Text erscheinen."""

    positionen = [
        _op(i, "SOLL", 1_000 + i, belegdatum=date(2026, 1, 1), leistungsperiode=f"2025-{(i % 12) + 1:02d}")
        for i in range(1, 60)
    ]
    letzte_markierung_periode = "2030-12"
    positionen.append(_op(999, "SOLL", 12_345, belegdatum=date(2026, 1, 2), leistungsperiode=letzte_markierung_periode))
    kontoblatt = _kontoblatt(*positionen)

    pdf_bytes = _erzeugen(
        forderungszeilen=[], bereits_offene_mahnkosten_cent=0, neue_zinsen_delta_cent=0, neue_gebuehr_cent=0,
        gesamtbetrag_cent=kontoblatt.saldo_cent, heute=date(2026, 1, 20), kontoblatt=kontoblatt,
    )
    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages) > 2, "Testaufbau muss tatsächlich mehrere Folgeseiten erzwingen."
    for seite in reader.pages[1:]:
        seiten_text = seite.extract_text()
        if "Mietvorschreibung" not in seiten_text:
            continue
        kopf_pos = seiten_text.find("Datum")
        erste_zeile_pos = seiten_text.find("Mietvorschreibung")
        assert kopf_pos != -1, "Jede Folgeseite mit Buchungszeilen muss ihren eigenen Tabellenkopf zeigen."
        assert kopf_pos < erste_zeile_pos, "Tabellenkopf muss VOR der ersten Buchungszeile dieser Seite stehen."
