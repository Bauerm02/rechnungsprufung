"""Tests für die lokale PDF-Textextraktion und Ablage (Auftrag
HV-20260913-VERTRAGSANLAGE). Alle PDFs sind synthetisch von Hand gebaut
(`_pdf_test_helpers.py`), keine echten Vertragsdokumente."""

from __future__ import annotations

from pathlib import Path

import pytest

from mietinkasso.vertragsanlage.ablage import UploadAbgelehntError, lesen, speichern
from mietinkasso.vertragsanlage.pdf_extraktion import PdfNichtLesbarError, extrahiere
from mietinkasso.vertragsanlage.vorschlaege import mehrdeutigkeiten_aus_extraktion, vorschlaege_aus_extraktion
from tests.mietinkasso._pdf_test_helpers import build_scan_pdf, build_text_pdf


def test_textlage_wird_erkannt_und_felder_extrahiert():
    pdf = build_text_pdf([
        "Wohnungsmietvertrag",
        "Mietbeginn: 01.06.2015",
        "Kaution: 1.500,00 EUR",
        "VPI 2020 Basiswert",
        "Erhöhung übersteigt um 5,0 % Prozent",
    ])
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert ergebnis.hat_textlage
    assert ergebnis.seiten_anzahl == 1
    assert ergebnis.treffer["mietbeginn"].wert == "01.06.2015"
    assert ergebnis.treffer["mietbeginn"].seite == 1
    assert "Mietbeginn" in ergebnis.treffer["mietbeginn"].auszug
    assert ergebnis.treffer["kaution_cent"].wert == "1.500,00"
    assert ergebnis.treffer["nutzungsart_wohnung"].wert == "Wohnungsmietvertrag"
    assert not ergebnis.warnungen


def test_gescanntes_dokument_ohne_textlage_liefert_keine_treffer_und_warnung():
    pdf = build_scan_pdf()
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert not ergebnis.hat_textlage
    assert ergebnis.treffer == {}
    assert any("Textlayer" in w for w in ergebnis.warnungen)


def test_seitenlimit_wird_angewendet_und_gemeldet():
    pdf = build_text_pdf(["Wohnungsmietvertrag"])
    ergebnis = extrahiere(pdf, max_seiten=0)
    # 0 Seiten ausgewertet -> keine Textlage feststellbar (bewusst
    # konservativ: lieber "kein Fund" als eine vorgetäuschte Auswertung).
    assert ergebnis.ausgewertete_seiten == 0
    assert not ergebnis.hat_textlage


def test_kaputte_datei_wird_als_nicht_lesbar_erkannt():
    with pytest.raises(PdfNichtLesbarError):
        extrahiere(b"%PDF-1.4\noffensichtlich kein valides PDF danach", max_seiten=5)


def test_mehrdeutige_nutzungsart_erzeugt_keinen_vorschlag_kein_raten():
    """Zwei widersprüchliche Hinweismuster im selben Dokument (z. B. ein
    Mischformular oder eine falsch erkannte Passage) dürfen NIE zu einer
    geratenen Nutzungsart führen."""

    pdf = build_text_pdf(["Wohnungsmietvertrag", "als Büro vermietet"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert "nutzungsart" not in vorschlaege


def test_eindeutige_nutzungsart_wird_vorgeschlagen_mit_beleg():
    pdf = build_text_pdf(["Geschäftsraummietvertrag über das Lokal Top 1"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert vorschlaege["nutzungsart"].formularwert == "GESCHAEFTSLOKAL"
    assert vorschlaege["nutzungsart"].seite == 1


def test_keine_mahngebuehr_klausel_wird_als_explizite_null_erkannt():
    pdf = build_text_pdf(["Es werden keine Mahnspesen verrechnet."])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert vorschlaege["mahngebuehr_cent"].formularwert == "0"


def test_fehlender_mahngebuehr_hinweis_erzeugt_keinen_vorschlag():
    """Kein Fund => kein Vorschlag => das Formularfeld bleibt leer => im
    übernommenen Datensatz `None` (nie eine erfundene 0)."""

    pdf = build_text_pdf(["Ein Vertrag ohne jede Erwähnung von Mahngebühren."])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert "mahngebuehr_cent" not in vorschlaege


def test_ablage_speichert_unter_inhaltsbasiertem_namen_und_ist_wiederholimport_sicher(tmp_path: Path):
    pdf = build_text_pdf(["Testvertrag"])
    verzeichnis = str(tmp_path / "uploads")
    doc1 = speichern(pdf, konfiguriertes_verzeichnis=verzeichnis, max_bytes=10_000_000)
    doc2 = speichern(pdf, konfiguriertes_verzeichnis=verzeichnis, max_bytes=10_000_000)
    assert doc1.ablage_id == doc2.ablage_id  # identischer Inhalt -> keine zweite Datei
    zurueckgelesen = lesen(doc1.ablage_id, konfiguriertes_verzeichnis=verzeichnis)
    assert zurueckgelesen == pdf
    assert len(list((tmp_path / "uploads").glob("*.pdf"))) == 1


def test_ablage_lehnt_ueberschreitung_der_groesse_ab(tmp_path: Path):
    with pytest.raises(UploadAbgelehntError):
        speichern(b"%PDF-1.4" + b"x" * 100, konfiguriertes_verzeichnis=str(tmp_path), max_bytes=10)


def test_ablage_lehnt_nicht_pdf_dateien_ab(tmp_path: Path):
    with pytest.raises(UploadAbgelehntError):
        speichern(b"<html><script>alert(1)</script></html>", konfiguriertes_verzeichnis=str(tmp_path), max_bytes=10_000)


def test_ablage_ohne_konfiguriertes_verzeichnis_bleibt_blockiert():
    with pytest.raises(UploadAbgelehntError):
        speichern(build_text_pdf(["x"]), konfiguriertes_verzeichnis=None, max_bytes=10_000_000)


def test_ablage_lesen_lehnt_pfadmanipulation_ab(tmp_path: Path):
    with pytest.raises(UploadAbgelehntError):
        lesen("../../../etc/passwd", konfiguriertes_verzeichnis=str(tmp_path))
    with pytest.raises(UploadAbgelehntError):
        lesen("<script>alert(1)</script>", konfiguriertes_verzeichnis=str(tmp_path))


# -- Rückprüfung (unabhängige Abnahme): EUR-vor-Betrag + Mehrdeutigkeit ------


def test_betrag_mit_waehrung_vor_der_zahl_wird_erkannt():
    """Reproduktion: 'Kaution in Höhe von EUR 1.500,00 gar nicht
    extrahiert.' Das bisherige Muster verlangte den Betrag VOR der
    Währung; deutschsprachige Verträge schreiben oft 'EUR 1.500,00'."""

    pdf = build_text_pdf(["Kaution in Höhe von EUR 1.500,00"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert ergebnis.treffer["kaution_cent"].wert == "1.500,00"


def test_mahnspesen_mit_euro_symbol_vor_betrag_wird_erkannt():
    pdf = build_text_pdf(["Mustermietvertrag", "Mahnspesen €20"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert ergebnis.treffer["mahngebuehr_cent"].wert == "20"


def test_widersprechende_kautionsbetraege_ergeben_mehrdeutigkeit_kein_vorschlag():
    """Reproduktion: 'PDF Kaution 1.500 EUR + Nachtrag Kaution 2.000 EUR
    ergibt 1.500 ohne Warnung.' Zwei unterschiedliche Beträge für
    dasselbe Feld dürfen NIE zu einem einzelnen, unkommentierten
    Vorschlag führen - beide Fundstellen müssen sichtbar werden."""

    pdf = build_text_pdf([
        "Kaution: 1.500,00 EUR",
        "Nachtrag zum Mietvertrag",
        "Kaution: 2.000,00 EUR",
    ])
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert "kaution_cent" not in ergebnis.treffer
    assert "kaution_cent" in ergebnis.mehrdeutige_treffer
    werte = {f.wert for f in ergebnis.mehrdeutige_treffer["kaution_cent"]}
    assert werte == {"1.500,00", "2.000,00"}

    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert "vertragliche_kaution_cent" not in vorschlaege  # kein automatischer Vorschlag bei Mehrdeutigkeit

    mehrdeutigkeiten = mehrdeutigkeiten_aus_extraktion(ergebnis)
    assert "Kaution" in mehrdeutigkeiten
    assert len(mehrdeutigkeiten["Kaution"].funde) == 2


def test_wiederholter_identischer_betrag_bleibt_eindeutig():
    """Zwei Fundstellen mit demselben Betrag (z. B. Kaution einmal in der
    Präambel, einmal in der Zusammenfassung) sind KEINE Mehrdeutigkeit."""

    pdf = build_text_pdf(["Kaution: 1.500,00 EUR", "Zusammenfassung: Kaution 1.500,00 EUR"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert ergebnis.treffer["kaution_cent"].wert == "1.500,00"
    assert "kaution_cent" not in ergebnis.mehrdeutige_treffer


def test_mieter_und_vermieter_namen_werden_als_hinweis_extrahiert():
    pdf = build_text_pdf(["Vermieter: JLB Projects GmbH", "Mieter: Erika Musterfrau"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert vorschlaege["mieter_name_hinweis"].formularwert == "Erika Musterfrau"
    assert vorschlaege["vermieter_name_hinweis"].formularwert == "JLB Projects GmbH"


def test_mitmieter_name_und_telefon_werden_als_hinweis_extrahiert_nicht_automatisch_angelegt():
    """Auftrag HV-20261020-VERTRAGSDATENBASIS: ein Mitmieter im Dokument
    wird NUR als geprüfter Hinweis mit Seitenbeleg ausgewiesen - die
    tatsächliche Partei entsteht erst über die manuelle "Partei
    hinzufügen"-Erfassung in der Mieterakte, niemals automatisch hier."""

    pdf = build_text_pdf(["Mieter: Max Mustermann", "Mitmieter: Erika Musterfrau Tel: 0664 1234567"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert vorschlaege["mitmieter_name_hinweis"].formularwert == "Erika Musterfrau"
    assert vorschlaege["mitmieter_telefon_hinweis"].formularwert == "0664 1234567"
    # Kein übernehmbares Mietvertragsprofil-Feld entsteht daraus.
    assert "mitmieter_name" not in vorschlaege
    assert "mitmieter_telefon" not in vorschlaege


def test_mitmieterin_ohne_telefon_wird_erkannt():
    pdf = build_text_pdf(["weitere Mieterin: Anna Beispiel"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert vorschlaege["mitmieter_name_hinweis"].formularwert == "Anna Beispiel"
    assert "mitmieter_telefon_hinweis" not in vorschlaege


def test_mietbestandteile_werden_getrennt_extrahiert():
    pdf = build_text_pdf([
        "Hauptmietzins: 500,00 EUR", "Betriebskosten: 80,00 EUR",
        "Heizkosten: 40,00 EUR", "Küche: 30,00 EUR", "Parkplatz: 50,00 EUR",
    ])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    # Abnahme-Folgefix: die Brutto-Vorschläge mappen jetzt DIREKT auf die
    # tatsächlich im Review gerenderten `komponente_*`-Felder statt auf
    # tote, nirgends gerenderte Formularnamen.
    assert vorschlaege["komponente_hmz"].formularwert == "500,00"
    assert vorschlaege["komponente_bk"].formularwert == "80,00"
    assert vorschlaege["komponente_hk"].formularwert == "40,00"
    assert vorschlaege["komponente_kueche"].formularwert == "30,00"
    assert vorschlaege["komponente_parkplatz"].formularwert == "50,00"


def test_mietende_wird_extrahiert():
    pdf = build_text_pdf(["Mietende: 31.05.2030"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert vorschlaege["gueltig_bis"].formularwert == "2030-05-31"


# -- Korrektur nach unabhängiger Abnahme (Codex, 20.10.2026): -------------
# -- _BETRAG_FRAGMENT truncation + Mahngebühr-Synonym + Nettomiete/Brutto -


@pytest.mark.parametrize(
    "roh,erwartet",
    [
        ("1200", "1200"),
        ("1200,00", "1200,00"),
        ("1.200,00", "1.200,00"),
        ("12345,67", "12345,67"),
    ],
)
def test_betragsformate_werden_vollstaendig_erkannt_nicht_truncated(roh, erwartet):
    """Reproduktion (Codex-Abnahme 20.10.2026): `_BETRAG_FRAGMENT` erlaubte
    bisher nur 1-3 Ziffern vor einem Tausender-/Dezimaltrenner OHNE eine
    nachfolgende Zifferngrenze zu verlangen - eine unformatierte Zahl
    wurde dadurch nach den ersten 1-3 Ziffern abgeschnitten ("1200" ->
    "120", "1200,00" -> "120", "12345,67" -> "123"). Jedes hier gelistete
    Format muss jetzt VOLLSTÄNDIG und UNVERÄNDERT erkannt werden - andere,
    bereits korrekte Formate dürfen dabei NICHT umgedeutet werden."""

    pdf = build_text_pdf([f"Hauptmietzins: {roh} EUR"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert ergebnis.treffer["hauptmietzins_cent"].wert == erwartet


def test_betrag_ohne_trennzeichen_direkt_nach_waehrung_wird_nicht_abgeschnitten():
    """Dasselbe Grenzfallbeispiel, aber mit der Währung VOR der Zahl ohne
    Leerzeichen ("EUR1200") - exakt der vom Auftraggeber genannte Fall."""

    pdf = build_text_pdf(["Hauptmietzins EUR1200"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert ergebnis.treffer["hauptmietzins_cent"].wert == "1200"


@pytest.mark.parametrize("text", [
    "Hauptmietzins: EUR1234.5678",       # Betrag nach Währung, Punkt-Dezimal, 4 Nachkommastellen
    "Hauptmietzins: 1200,0005 EUR",      # Betrag vor Währung, Komma-Dezimal, 4 Nachkommastellen
    "Hauptmietzins: 1234,5678 EUR",      # dieselbe Rückwärtsalternative mit Komma
    "Hauptmietzins: EUR1200.0005",       # dieselbe Vorwärtsalternative mit Punkt
])
def test_ueberschuessige_dezimalstellen_werden_komplett_verworfen_nicht_teilweise_uebernommen(text):
    """Reproduktion (Codex-Abnahme 20.10.2026): 'EUR1234.5678' wurde zu
    '1234.567', 'EUR1200,0005' zu '1200,000' - beide Male ein
    stillschweigend TEILWEISE korrekter, aber inhaltlich FALSCHER
    Betrag. Eine nicht unterstützte Präzision (mehr als 2 Nachkomma-
    stellen, keine gültige 3er-Tausendergruppe) muss die GESAMTE Zahl
    an dieser Stelle unerkannt lassen (kein Fund), niemals einen
    verkürzten Suffix als gültigen Betrag liefern - geprüft für BEIDE
    Alternativen (Betrag vor/nach Währung) und beide Dezimaltrennzeichen."""

    pdf = build_text_pdf([text])
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert "hauptmietzins_cent" not in ergebnis.treffer
    assert "hauptmietzins_cent" not in ergebnis.mehrdeutige_treffer


def test_mehrdeutiges_format_bleibt_als_mehrdeutigkeit_sichtbar_nach_der_korrektur():
    """Die Korrektur darf eine ECHTE (semantische) Mehrdeutigkeit -
    unterschiedliche Beträge für dasselbe Feld - nicht verdecken; sie
    behebt nur die Truncation einzelner, für sich genommen eindeutiger
    Zahlen."""

    pdf = build_text_pdf(["Hauptmietzins: 1200 EUR", "Hauptmietzins: 1300 EUR"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert "hauptmietzins_cent" not in ergebnis.treffer
    werte = {f.wert for f in ergebnis.mehrdeutige_treffer["hauptmietzins_cent"]}
    assert werte == {"1200", "1300"}


def test_mahngebuehr_synonym_ohne_spesen_wortlaut_wird_erkannt():
    """Reproduktion (Codex-Abnahme 20.10.2026): nur 'Mahnspesen' wurde
    erkannt, 'Mahngebühr' (ohne 'Spesen'-Wortlaut) nicht - beide Begriffe
    bezeichnen dieselbe Fachregel."""

    pdf = build_text_pdf(["Mahngebühr: 20,00 EUR"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert ergebnis.treffer["mahngebuehr_cent"].wert == "20,00"


def test_mahngebuehr_plural_wird_erkannt():
    pdf = build_text_pdf(["Es werden Mahngebühren in Höhe von EUR 15,00 verrechnet."])
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert ergebnis.treffer["mahngebuehr_cent"].wert == "15,00"


def test_keine_mahngebuehr_klausel_mit_pluralwortlaut_wird_erkannt():
    pdf = build_text_pdf(["Es werden keine Mahngebühren verrechnet."])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert vorschlaege["mahngebuehr_cent"].formularwert == "0"


def test_nettomiete_wird_nicht_als_hauptmietzins_brutto_uebernommen():
    """Auftrag Markus (Korrektur 20.10.2026): 'Nettomiete niemals still
    als Brutto übernehmen' - `hauptmietzins_cent` ist im System per
    Definition BRUTTO (siehe `backoffice/vertragsanlage_form.py`,
    Label "Hauptmietzins (brutto, EUR)"); eine ausdrücklich als NETTO
    bezeichnete Angabe darf NIE in dieses Feld einfließen."""

    pdf = build_text_pdf(["Nettomiete: 500,00 EUR"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert "hauptmietzins_cent" not in ergebnis.treffer
    assert ergebnis.treffer["nettomiete_cent"].wert == "500,00"

    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert "hauptmietzins_cent" not in vorschlaege  # NIE automatisch als Brutto übernommen
    assert vorschlaege["nettomiete_hinweis_cent"].formularwert == "500,00"


def test_hauptmietzins_und_nettomiete_nebeneinander_bleiben_getrennte_felder():
    """Nennt ein Vertrag BEIDE Begriffe (z. B. 'Hauptmietzins (brutto)
    550,00 EUR' und separat 'Nettomiete 500,00 EUR'), dürfen sie NICHT
    in dasselbe Extraktionsfeld vermischt werden - das würde sonst als
    Mehrdeutigkeit zwischen zwei eigentlich unterschiedlichen Größen
    (netto vs. brutto) fehlinterpretiert."""

    pdf = build_text_pdf(["Hauptmietzins: 550,00 EUR", "Nettomiete: 500,00 EUR"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert ergebnis.treffer["hauptmietzins_cent"].wert == "550,00"
    assert ergebnis.treffer["nettomiete_cent"].wert == "500,00"
    assert "hauptmietzins_cent" not in ergebnis.mehrdeutige_treffer


# -- Auftrag HV-20261020-VERTRAGSDATENBASIS: Termine/Befristung/BK-HK ----


def test_unterzeichnungsdatum_und_uebergabe_getrennt_von_mietbeginn_extrahiert():
    pdf = build_text_pdf([
        "Dieser Vertrag wurde unterzeichnet am 15.05.2015.",
        "Mietbeginn: 01.06.2015", "Übergeben am 02.06.2015",
    ])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert vorschlaege["unterzeichnungsdatum"].formularwert == "2015-05-15"
    assert vorschlaege["uebergabe_am"].formularwert == "2015-06-02"
    assert vorschlaege["urspruenglicher_mietbeginn"].formularwert == "2015-06-01"


def test_befristungsstatus_befristet_eindeutig_erkannt():
    pdf = build_text_pdf(["Dies ist ein befristeter Mietvertrag auf 5 Jahre."])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert vorschlaege["befristungsstatus"].formularwert == "BEFRISTET"


def test_befristungsstatus_unbefristet_eindeutig_erkannt():
    pdf = build_text_pdf(["Dies ist ein unbefristeter Mietvertrag."])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert vorschlaege["befristungsstatus"].formularwert == "UNBEFRISTET"


def test_befristungsstatus_mehrdeutig_bleibt_unbesetzt():
    """Enthält ein Dokument BEIDE Formulierungen (Mischtext/Nachtrag),
    darf das Feld NICHT geraten werden - bleibt UNGEKLAERT."""

    pdf = build_text_pdf(["Ursprünglich unbefristeter Mietvertrag.", "Nachtrag: befristeter Mietvertrag auf 3 Jahre."])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert "befristungsstatus" not in vorschlaege


def test_leeres_mietende_setzt_befristungsstatus_nicht_automatisch_auf_unbefristet():
    """Auftrag Markus: 'Leeres Ende darf NICHT automatisch unbefristet
    sein' - ein Dokument OHNE jede Befristungsformulierung und OHNE
    Mietende-Fund darf KEINEN Vorschlag für `befristungsstatus`
    erzeugen (das Feld bleibt beim Formular-Default UNGEKLAERT)."""

    pdf = build_text_pdf(["Wohnungsmietvertrag", "Mietbeginn: 01.06.2015"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert "gueltig_bis" not in vorschlaege
    assert "befristungsstatus" not in vorschlaege


def test_bk_hk_abrechnungsart_akonto_erkannt():
    pdf = build_text_pdf(["Der Mieter leistet eine monatliche Akontozahlung für die Betriebskosten."])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert vorschlaege["bk_hk_abrechnungsart"].formularwert == "AKONTO_VORAUSZAHLUNG"


def test_bk_hk_abrechnungsart_pauschale_erkannt():
    pdf = build_text_pdf(["Es handelt sich um eine Inklusivmiete."])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert vorschlaege["bk_hk_abrechnungsart"].formularwert == "PAUSCHALE_INKLUSIVMIETE"


def test_bk_hk_abrechnungsart_ohne_fund_bleibt_unbesetzt_nie_aus_blossem_betrag_geraten():
    """Auftrag Markus: 'Das entscheidet, ob später eine Nachverrechnung
    berechnet werden darf; aus einem bloßen BK-Betrag nicht raten.' Ein
    bloßer Betriebskosten-BETRAG ohne Akonto-/Pauschale-Formulierung
    darf KEINEN Abrechnungsart-Vorschlag erzeugen."""

    pdf = build_text_pdf(["Betriebskosten: 80,00 EUR"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    vorschlaege = vorschlaege_aus_extraktion(ergebnis)
    assert "bk_hk_abrechnungsart" not in vorschlaege


def test_fehlgeschlagene_seitenauswertung_wird_als_warnung_sichtbar(monkeypatch):
    """'Teilseitenerkennung nicht unbemerkt erfolgreich ausgeben' - schlägt
    eine einzelne Seite beim Textzugriff fehl, muss das als Warnung
    sichtbar werden statt stillschweigend als vollständiger Erfolg zu
    gelten."""

    import pypdf._page as pypdf_page

    original = pypdf_page.PageObject.extract_text
    aufrufe = {"n": 0}

    def kaputte_extraktion(self, *args, **kwargs):
        aufrufe["n"] += 1
        if aufrufe["n"] == 1:
            raise RuntimeError("simulierter Lesefehler")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pypdf_page.PageObject, "extract_text", kaputte_extraktion)
    pdf = build_text_pdf(["Wohnungsmietvertrag"])
    ergebnis = extrahiere(pdf, max_seiten=10)
    assert any("konnte kein Text gelesen werden" in w for w in ergebnis.warnungen)
