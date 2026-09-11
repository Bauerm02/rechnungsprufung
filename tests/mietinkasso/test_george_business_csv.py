"""Tests für den rein lesenden George-Business-CSV-Vorschau-Adapter.

Alle Testdaten sind frei erfunden (synthetisch) — keine echten Konten,
IBANs oder Buchungen. Diese Datei enthält gezielte Regressionstests für
die 6 Befunde der unabhängigen Codeprüfung (Codex) plus 2 weitere
gemeldete Fehler (Betragsüberlauf, Feldhash-Kollision) - NICHT Tests,
die nur die (fehlerhafte) vorherige Implementierung spiegeln.
"""

from __future__ import annotations

import hashlib
import inspect
from datetime import date

import pytest

from mietinkasso.bank import george_business_csv as gbc
from mietinkasso.bank.george_business_csv import (
    ERWARTETE_SPALTEN,
    GeorgeFormatFehlerError,
    erstelle_preview,
    parse_oesterreichischen_betrag,
)

KONTO_A = "AT611000000000601001"
KONTO_B = "AT611000000000616001"


def _sd_id(iban: str, jahr: int, marker: str, praefix: str = "000000001", hash_suffix: str | None = None) -> str:
    """Baut eine synthetische 'Enthaltene Überweisung ID' nach dem
    bestätigten 107-Zeichen-Profil: IBAN(20)+Padding(14)+EUR(3)+
    Präfix(9)+Jahr(4)+Marker(1)+Hex-Hash(56)."""

    hash_suffix = hash_suffix if hash_suffix is not None else "A" * 56
    assert len(iban) == 20
    assert len(praefix) == 9
    assert len(hash_suffix) == 56
    assert marker in ("S", "D")
    return f"{iban}{'0' * 14}EUR{praefix}{jahr:04d}{marker}{hash_suffix}"


def _sd_id_variante(
    iban: str,
    jahr: int,
    marker: str,
    *,
    praefix: str = "000000001",
    hash_suffix: str | None = None,
    hash_laenge: int = 56,
    eingebettetes_konto: str | None = None,
    waehrung: str = "EUR",
    padding: str | None = None,
) -> str:
    """Wie `_sd_id`, aber erlaubt gezielt EINEN Aspekt des 107-Zeichen-
    Profils zu verletzen (falsches Konto/Währung/Padding/Jahr/Hex,
    abgeschnittener Suffix) - für die Regressionstests zu Befund 1
    (kaputtes Sammelprofil darf nie normaler Einzelumsatz werden)."""

    konto_teil = eingebettetes_konto if eingebettetes_konto is not None else iban
    padding_teil = padding if padding is not None else "0" * 14
    suffix = hash_suffix if hash_suffix is not None else "A" * hash_laenge
    return f"{konto_teil}{padding_teil}{waehrung}{praefix}{jahr:04d}{marker}{suffix}"


def _zeile(**overrides: str) -> dict[str, str]:
    basis = {spalte: "" for spalte in ERWARTETE_SPALTEN}
    basis.update(
        {
            "Eigene IBAN": KONTO_A,
            "Eigener Kontoname": "Testkonto",
            "Buchungsdatum": "15.01.2026",
            "Durchführungsdatum": "15.01.2026",
            "Durchführungszeit": "10:00:00",
            "Kontoauszug / Rechnung": "1",
            "Betrag": "100,00",
            "Währung": "EUR",
            "Valutadatum": "15.01.2026",
        }
    )
    basis.update(overrides)
    return basis


def _csv_text(zeilen: list[dict[str, str]]) -> str:
    kopf = ",".join(f'"{spalte}"' for spalte in ERWARTETE_SPALTEN)
    datenzeilen = [",".join(f'"{zeile.get(spalte, "")}"' for spalte in ERWARTETE_SPALTEN) for zeile in zeilen]
    return "\r\n".join([kopf, *datenzeilen]) + "\r\n"


def _bytes(zeilen: list[dict[str, str]], *, bom: bool = True) -> bytes:
    text = _csv_text(zeilen)
    encoded = text.encode("utf-8")
    return (b"\xef\xbb\xbf" + encoded) if bom else encoded


def _preview(zeilen, **kwargs):
    kwargs.setdefault("erwartetes_konto_iban", KONTO_A)
    kwargs.setdefault("von", date(2026, 1, 1))
    kwargs.setdefault("bis", date(2026, 1, 31))
    return erstelle_preview(_bytes(zeilen), **kwargs)


# ---------------------------------------------------------------------------
# Betragsparsing (österreichische Notation, strikt)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "erwartete_cent"),
    [
        ("100,00", 10_000),
        ("1.234,56", 123_456),
        ("-123,45", -12_345),
        ("0,05", 5),
        ("1.234.567,89", 123_456_789),
    ],
)
def test_parse_oesterreichischen_betrag_akzeptiert_gueltige_werte(text, erwartete_cent):
    assert parse_oesterreichischen_betrag(text) == erwartete_cent


@pytest.mark.parametrize(
    "text",
    [
        "1,234.56",  # US-Format
        "1.5",  # nur eine Nachkommastelle
        "100",  # kein Komma/Dezimalteil
        "1.23,45",  # unvollständige Tausendergruppierung
        "1e3",
        "NaN",
        "Infinity",
        "100,000",  # drei Nachkommastellen statt zwei
        "999999999999999999999,99",  # Überlauf
    ],
)
def test_parse_oesterreichischen_betrag_lehnt_mehrdeutige_werte_ab(text):
    with pytest.raises(ValueError):
        parse_oesterreichischen_betrag(text)


def test_parse_oesterreichischen_betrag_100_stellige_zahl_kontrolliert_abgelehnt():
    """Regression: löste zuvor eine unbehandelte decimal.InvalidOperation
    aus (Decimal-Standardpräzision 28 Stellen überschritten), statt einer
    kontrollierten ValueError."""

    ueberlange_zahl = "9" * 100 + ",00"
    with pytest.raises(ValueError):
        parse_oesterreichischen_betrag(ueberlange_zahl)


# ---------------------------------------------------------------------------
# Kanonischer Feldhash - keine Kollision durch Feldwerte mit Komma/"="
# ---------------------------------------------------------------------------


def test_feldhash_kollidiert_nicht_bei_feldwerten_mit_kommas_und_gleichheitszeichen():
    """Regression: die frühere 'Spalte=Wert,Spalte=Wert'-Verkettung ließ
    sich durch Feldwerte, die selbst Komma/'=' enthalten, mehrdeutig
    machen - zwei inhaltlich unterschiedliche Zeilen erzeugten denselben
    Hash. Kanonisches JSON (json.dumps) darf das nicht mehr zulassen."""

    zeile_a = _zeile(**{"Partner Name": "alpha,Partner IBAN=beta", "Partner IBAN": "gamma", "Enthaltene Überweisung ID": "ID-A"})
    zeile_b = _zeile(**{"Partner Name": "alpha", "Partner IBAN": "beta,Partner IBAN=gamma", "Enthaltene Überweisung ID": "ID-B"})

    preview = _preview([zeile_a, zeile_b])
    assert len(preview.kandidaten) == 2
    hashes = {z.sha256_zeile for z in preview.kandidaten}
    assert len(hashes) == 2, "Zwei inhaltlich unterschiedliche Zeilen dürfen nicht denselben Feldhash ergeben."


# ---------------------------------------------------------------------------
# Struktur/Format
# ---------------------------------------------------------------------------


def test_lehnt_xlsx_dateien_ab_statt_falsch_als_csv_zu_lesen():
    xlsx_signatur = b"PK\x03\x04" + b"\x00" * 20
    with pytest.raises(GeorgeFormatFehlerError):
        erstelle_preview(xlsx_signatur, erwartetes_konto_iban=KONTO_A, von=date(2026, 1, 1), bis=date(2026, 1, 31))


def test_lehnt_abweichende_kopfzeile_ab():
    kaputt = "a,b,c\r\n1,2,3\r\n".encode("utf-8")
    with pytest.raises(GeorgeFormatFehlerError):
        erstelle_preview(kaputt, erwartetes_konto_iban=KONTO_A, von=date(2026, 1, 1), bis=date(2026, 1, 31))


def test_lehnt_doppelte_spalten_in_kopfzeile_ab():
    spalten = list(ERWARTETE_SPALTEN) + ["Betrag"]  # "Betrag" doppelt
    kopf = ",".join(f'"{s}"' for s in spalten)
    text = kopf + "\r\n"
    with pytest.raises(GeorgeFormatFehlerError):
        erstelle_preview(text.encode("utf-8"), erwartetes_konto_iban=KONTO_A, von=date(2026, 1, 1), bis=date(2026, 1, 31))


def test_lehnt_leere_datei_ab():
    with pytest.raises(GeorgeFormatFehlerError):
        erstelle_preview(b"", erwartetes_konto_iban=KONTO_A, von=date(2026, 1, 1), bis=date(2026, 1, 31))


def test_lehnt_defekte_quote_struktur_ab():
    kopf = ",".join(f'"{s}"' for s in ERWARTETE_SPALTEN)
    kaputte_zeile = '"abc"def' + ",".join([""] * (len(ERWARTETE_SPALTEN) - 1))
    text = f"{kopf}\r\n{kaputte_zeile}\r\n"
    with pytest.raises(GeorgeFormatFehlerError):
        erstelle_preview(text.encode("utf-8"), erwartetes_konto_iban=KONTO_A, von=date(2026, 1, 1), bis=date(2026, 1, 31))


def test_zeile_mit_zu_vielen_feldern_wird_abgelehnt_ohne_absturz():
    kopf = ",".join(f'"{s}"' for s in ERWARTETE_SPALTEN)
    zu_viele_felder = ",".join(['"x"'] * (len(ERWARTETE_SPALTEN) + 2))
    text = f"{kopf}\r\n{zu_viele_felder}\r\n"
    preview = erstelle_preview(text.encode("utf-8"), erwartetes_konto_iban=KONTO_A, von=date(2026, 1, 1), bis=date(2026, 1, 31))
    assert len(preview.abgelehnt) == 1
    assert preview.kandidaten == []


def test_zeile_mit_zu_wenigen_feldern_wird_abgelehnt_ohne_absturz():
    kopf = ",".join(f'"{s}"' for s in ERWARTETE_SPALTEN)
    zu_wenige_felder = ",".join(['"x"'] * (len(ERWARTETE_SPALTEN) - 3))
    text = f"{kopf}\r\n{zu_wenige_felder}\r\n"
    preview = erstelle_preview(text.encode("utf-8"), erwartetes_konto_iban=KONTO_A, von=date(2026, 1, 1), bis=date(2026, 1, 31))
    assert len(preview.abgelehnt) == 1
    assert preview.kandidaten == []


def test_akzeptiert_bom_komma_und_crlf_zeilenumbrueche():
    rohbytes = _bytes([_zeile(Betrag="50,00", **{"Enthaltene Überweisung ID": "REF-BOM-TEST"})], bom=True)
    assert rohbytes.startswith(b"\xef\xbb\xbf")
    preview = erstelle_preview(rohbytes, erwartetes_konto_iban=KONTO_A, von=date(2026, 1, 1), bis=date(2026, 1, 31))
    assert len(preview.zeilen) == 1
    assert preview.kandidaten[0].kandidat.betrag_cent == 5_000


def test_ueberspringt_vollstaendig_leere_zeile_am_dateiende():
    text = _csv_text([_zeile()]) + "\r\n"
    preview = erstelle_preview(text.encode("utf-8"), erwartetes_konto_iban=KONTO_A, von=date(2026, 1, 1), bis=date(2026, 1, 31))
    assert len(preview.zeilen) == 1


def test_datei_sha256_ist_hash_der_rohen_bytes_nicht_des_dekodierten_texts():
    rohbytes = _bytes([_zeile()], bom=True)
    preview = erstelle_preview(rohbytes, erwartetes_konto_iban=KONTO_A, von=date(2026, 1, 1), bis=date(2026, 1, 31))
    assert preview.datei_sha256 == hashlib.sha256(rohbytes).hexdigest()
    # Insbesondere NICHT der Hash des BOM-befreiten dekodierten Texts:
    assert preview.datei_sha256 != hashlib.sha256(rohbytes.decode("utf-8-sig").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Datum-/Konten-/Währungsfehler -> ABGELEHNT, nie stillschweigend interpretiert
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("feld", "wert"),
    [
        ("Buchungsdatum", "2026-01-15"),
        ("Buchungsdatum", "1.1.2026"),
        ("Buchungsdatum", "31.02.2026"),
        ("Eigene IBAN", ""),
        ("Währung", "USD"),
        ("Währung", ""),
    ],
)
def test_ungueltige_pflichtfelder_fuehren_zu_abgelehnt(feld, wert):
    preview = _preview([_zeile(**{feld: wert})])
    assert len(preview.abgelehnt) == 1
    assert preview.kandidaten == []


def test_ungueltiges_valutadatum_fuehrt_zu_abgelehnt_statt_stillschweigend_ignoriert():
    preview = _preview([_zeile(Valutadatum="32.01.2026")])
    assert len(preview.abgelehnt) == 1


# ---------------------------------------------------------------------------
# Konto/Zeitraum außerhalb der Preview-Parameter -> nie KANDIDAT
# ---------------------------------------------------------------------------


def test_zeile_auf_anderem_konto_wird_abgelehnt_nicht_kandidat():
    preview = _preview([_zeile(**{"Eigene IBAN": KONTO_B})])
    assert preview.kandidaten == []
    assert len(preview.abgelehnt) == 1
    assert "Eigene IBAN" in preview.abgelehnt[0].grund


def test_zeile_ausserhalb_des_erwarteten_zeitraums_wird_pruefffall_nicht_kandidat():
    preview = _preview([_zeile(Buchungsdatum="01.02.2026")])
    assert preview.kandidaten == []
    assert len(preview.pruefffaelle) == 1
    assert preview.ausgaenge_cent == 0
    assert preview.eingaenge_cent == 0


def test_gleiche_referenz_auf_mehreren_konten_eine_wird_abgelehnt():
    preview = _preview(
        [
            _zeile(Betrag="-50,00", Buchungsreferenz="REF-SHARED", **{"Eigene IBAN": KONTO_A, "Enthaltene Überweisung ID": "REF-KONTO-A"}),
            _zeile(Betrag="-50,00", Buchungsreferenz="REF-SHARED", **{"Eigene IBAN": KONTO_B, "Enthaltene Überweisung ID": "REF-KONTO-B"}),
        ]
    )
    assert len(preview.kandidaten) == 1
    assert len(preview.abgelehnt) == 1
    assert preview.ausgaenge_cent == -5_000


# ---------------------------------------------------------------------------
# Einzelumsätze
# ---------------------------------------------------------------------------


def test_einzelumsatz_mit_echter_id_wird_kandidat():
    preview = _preview(
        [_zeile(Betrag="-45,50", **{"(Sammel-) Überweisung ID": "", "Enthaltene Überweisung ID": "REF-EINZEL-001"})]
    )
    assert len(preview.kandidaten) == 1
    kandidat = preview.kandidaten[0].kandidat
    assert kandidat.betrag_cent == -4_550
    assert kandidat.sammel_id is None
    assert kandidat.enthaltene_id == "REF-EINZEL-001"


def test_zeile_ohne_jede_id_wird_nie_automatischer_kandidat():
    """Nach unabhängiger Gegenprobe verschärft: fehlen SOWOHL 'Enthaltene
    Überweisung ID' ALS AUCH '(Sammel-) Überweisung ID' (leer oder
    NOTPROVIDED), gibt es keine unabhängig eindeutige Identität - eine
    Buchungsreferenz allein reicht nicht. Gilt unabhängig von der
    Zeilenanzahl der Datei; frühere Erwartung (automatischer Kandidat)
    war zu lax und wurde entsprechend korrigiert."""

    preview = _preview(
        [_zeile(Betrag="-45,50", **{"(Sammel-) Überweisung ID": "", "Enthaltene Überweisung ID": ""})]
    )
    assert preview.kandidaten == []
    assert len(preview.pruefffaelle) == 1


def test_leere_und_notprovided_id_werden_nicht_als_eindeutiger_schluessel_verwendet():
    """Nach unabhängiger Gegenprobe verschärft: beide Zeilen haben weder
    eine brauchbare 'Enthaltene Überweisung ID' noch eine brauchbare
    '(Sammel-) Überweisung ID' (NOTPROVIDED zählt nicht) - keine wird
    automatischer Kandidat. Der ursprüngliche Kern der Regression bleibt
    aber bestätigt: NOTPROVIDED führt NICHT dazu, dass beide Zeilen zu
    EINEM Ergebnis zusammengelegt werden - es bleiben zwei getrennte
    Prüffälle, keine stille Verschmelzung."""

    preview = _preview(
        [
            _zeile(Betrag="-10,00", Buchungsreferenz="NOTPROVIDED", **{"Enthaltene Überweisung ID": ""}),
            _zeile(Betrag="-20,00", Buchungsreferenz="NOTPROVIDED", **{"Enthaltene Überweisung ID": ""}),
        ]
    )
    assert preview.kandidaten == []
    assert len(preview.pruefffaelle) == 2


def test_id_ausserhalb_des_107_zeichen_profils_wird_nie_als_sd_geraten():
    """Regression: die alte Markierungslogik las den letzten Buchstaben
    VOR einer Endziffernfolge - bei einer gewöhnlichen (kurzen) Hash-ID
    konnte das zufällig wie ein 'S'/'D' aussehen. Eine ID, die nicht
    exakt dem bestätigten 107-Zeichen-Profil entspricht, muss als
    gewöhnlicher Einzelumsatz behandelt werden, nie als Sammelmarkierung."""

    zufaelliger_hash = "3F8A9C2D1B7E4056"  # endet auf 'B', 16 Zeichen - kein 107-Zeichen-Profil
    preview = _preview([_zeile(Betrag="-30,00", **{"Enthaltene Überweisung ID": zufaelliger_hash})])
    assert len(preview.kandidaten) == 1
    assert preview.kandidaten[0].kandidat.betrag_cent == -3_000


# ---------------------------------------------------------------------------
# Dublettenprüfung
# ---------------------------------------------------------------------------


def test_identische_enthaltene_id_auf_demselben_konto_wird_nie_doppelt_kandidat():
    preview = _preview(
        [
            _zeile(Betrag="-10,00", **{"Enthaltene Überweisung ID": "DUP-ID-1"}),
            _zeile(Betrag="-20,00", **{"Enthaltene Überweisung ID": "DUP-ID-1"}),
        ]
    )
    assert preview.kandidaten == []
    assert len(preview.pruefffaelle) == 2


def test_identische_rohdatensaetze_ohne_brauchbare_id_werden_pruefffall():
    zeile = _zeile(Betrag="-15,00", Buchungsreferenz="REF-X", **{"Enthaltene Überweisung ID": ""})
    preview = _preview([dict(zeile), dict(zeile)])
    assert preview.kandidaten == []
    assert len(preview.pruefffaelle) == 2


def test_unterscheidbare_echte_zahlungen_mit_gleicher_summe_bleiben_getrennt():
    preview = _preview(
        [
            _zeile(Betrag="-50,00", Buchungsreferenz="REF-001", Zahlungsreferenz="Miete Jaenner", **{"Enthaltene Überweisung ID": "REF-001-ID"}),
            _zeile(Betrag="-50,00", Buchungsreferenz="REF-002", Zahlungsreferenz="Miete Jaenner", **{"Enthaltene Überweisung ID": "REF-002-ID"}),
        ]
    )
    assert len(preview.kandidaten) == 2
    assert preview.ausgaenge_cent == -10_000


def test_kandidaten_id_bleibt_bei_dateireihenfolgeaenderung_stabil():
    zeile_a = _zeile(Betrag="-10,00", Buchungsreferenz="REF-A", **{"Enthaltene Überweisung ID": "REF-A-ID"})
    zeile_b = _zeile(Betrag="-20,00", Buchungsreferenz="REF-B", **{"Enthaltene Überweisung ID": "REF-B-ID"})

    vorwaerts = _preview([zeile_a, zeile_b])
    rueckwaerts = _preview([zeile_b, zeile_a])

    ids_vorwaerts = {k.kandidat.kandidaten_id for k in vorwaerts.kandidaten}
    ids_rueckwaerts = {k.kandidat.kandidaten_id for k in rueckwaerts.kandidaten}
    assert len(ids_vorwaerts) == 2
    assert ids_vorwaerts == ids_rueckwaerts


# ---------------------------------------------------------------------------
# Sammelüberweisungen: Summe + Details (korrigierte Gruppierung)
# ---------------------------------------------------------------------------


def test_sammelgruppe_ueber_referenz_und_sd_markierung_mit_unterschiedlichen_sammel_ids():
    """Kernregression: die Summenzeile teilt sich die '(Sammel-)
    Überweisung ID' NUR mit dem ersten Detailposten; die übrigen Details
    tragen eigene IDs. Gruppierung läuft über (Konto, Währung, Datum,
    echte Buchungsreferenz) + S/D-Markierung + Gruppenpräfix-Konsistenz +
    centgenaue Summenprüfung - NICHT über eine gemeinsame Sammel-ID."""

    referenz = "SAMMELUEBERWEISUNG-JAENNER"
    zeilen = [
        _zeile(
            Betrag="-80,00",
            Buchungsreferenz=referenz,
            **{
                "(Sammel-) Überweisung ID": "D-EIGENE-ID-1",
                "Enthaltene Überweisung ID": _sd_id(KONTO_A, 2026, "D", hash_suffix="A" * 56),
            },
        ),
        _zeile(
            Betrag="-20,00",
            Buchungsreferenz=referenz,
            **{
                "(Sammel-) Überweisung ID": "D-EIGENE-ID-2",  # ANDERE ID als der erste Detailposten
                "Enthaltene Überweisung ID": _sd_id(KONTO_A, 2026, "D", hash_suffix="B" * 56),
            },
        ),
        _zeile(
            Betrag="-100,00",
            Buchungsreferenz=referenz,
            **{
                "(Sammel-) Überweisung ID": "D-EIGENE-ID-1",  # teilt sich ID mit dem ERSTEN Detail
                "Enthaltene Überweisung ID": _sd_id(KONTO_A, 2026, "S", hash_suffix="C" * 56),
            },
        ),
    ]
    preview = _preview(zeilen)
    assert len(preview.kandidaten) == 2
    assert len(preview.sammel_summen) == 1
    assert preview.pruefffaelle == []
    kandidat_betraege = sorted(k.kandidat.betrag_cent for k in preview.kandidaten)
    assert kandidat_betraege == [-8_000, -2_000]
    assert preview.ausgaenge_cent == -10_000


def test_summenzeile_kann_vor_den_detailzeilen_stehen():
    referenz = "SAMMELUEBERWEISUNG-REIHENFOLGE"
    zeilen = [
        _zeile(
            Betrag="-100,00",
            Buchungsreferenz=referenz,
            **{
                "(Sammel-) Überweisung ID": "ID-1",
                "Enthaltene Überweisung ID": _sd_id(KONTO_A, 2026, "S", hash_suffix="C" * 56),
            },
        ),
        _zeile(
            Betrag="-80,00",
            Buchungsreferenz=referenz,
            **{
                "(Sammel-) Überweisung ID": "ID-1",
                "Enthaltene Überweisung ID": _sd_id(KONTO_A, 2026, "D", hash_suffix="A" * 56),
            },
        ),
        _zeile(
            Betrag="-20,00",
            Buchungsreferenz=referenz,
            **{
                "(Sammel-) Überweisung ID": "ID-2",
                "Enthaltene Überweisung ID": _sd_id(KONTO_A, 2026, "D", hash_suffix="B" * 56),
            },
        ),
    ]
    preview = _preview(zeilen)
    assert len(preview.kandidaten) == 2
    assert len(preview.sammel_summen) == 1


def test_isolierte_d_zeile_ohne_gegengruppe_wird_pruefffall_nicht_kandidat():
    """Regression: eine einzelne S/D-markierte Zeile ohne vollständige
    Gegengruppe wurde zuvor automatisch zum Kandidaten - jetzt Prüffall."""

    preview = _preview(
        [
            _zeile(
                Betrag="-15,00",
                Buchungsreferenz="EINZELNE-DETAILZEILE",
                **{
                    "(Sammel-) Überweisung ID": "ID-X",
                    "Enthaltene Überweisung ID": _sd_id(KONTO_A, 2026, "D"),
                },
            )
        ]
    )
    assert preview.kandidaten == []
    assert len(preview.pruefffaelle) == 1


def test_sammelgruppe_ohne_brauchbare_referenz_wird_pruefffall():
    preview = _preview(
        [
            _zeile(
                Betrag="-15,00",
                Buchungsreferenz="",
                **{
                    "(Sammel-) Überweisung ID": "ID-X",
                    "Enthaltene Überweisung ID": _sd_id(KONTO_A, 2026, "D"),
                },
            )
        ]
    )
    assert preview.kandidaten == []
    assert len(preview.pruefffaelle) == 1


def test_sammelgruppe_mit_inkonsistentem_gruppenpraefix_wird_pruefffall():
    referenz = "SAMMEL-INKONSISTENTES-PRAEFIX"
    zeilen = [
        _zeile(
            Betrag="-80,00",
            Buchungsreferenz=referenz,
            **{
                "(Sammel-) Überweisung ID": "ID-1",
                "Enthaltene Überweisung ID": _sd_id(KONTO_A, 2026, "D", praefix="000000001", hash_suffix="A" * 56),
            },
        ),
        _zeile(
            Betrag="-20,00",
            Buchungsreferenz=referenz,
            **{
                "(Sammel-) Überweisung ID": "ID-2",
                # ANDERES 9-stelliges Präfix - Gruppenpräfix-Konsistenz verletzt.
                "Enthaltene Überweisung ID": _sd_id(KONTO_A, 2026, "D", praefix="999999999", hash_suffix="B" * 56),
            },
        ),
        _zeile(
            Betrag="-100,00",
            Buchungsreferenz=referenz,
            **{
                "(Sammel-) Überweisung ID": "ID-1",
                "Enthaltene Überweisung ID": _sd_id(KONTO_A, 2026, "S", praefix="000000001", hash_suffix="C" * 56),
            },
        ),
    ]
    preview = _preview(zeilen)
    assert preview.kandidaten == []
    assert len(preview.pruefffaelle) == 3


def test_sammelgruppe_ohne_gemeinsame_sammel_id_zur_summenzeile_wird_pruefffall():
    referenz = "SAMMEL-OHNE-ID-BEZUG"
    zeilen = [
        _zeile(
            Betrag="-80,00",
            Buchungsreferenz=referenz,
            **{
                "(Sammel-) Überweisung ID": "ID-DETAIL-1",
                "Enthaltene Überweisung ID": _sd_id(KONTO_A, 2026, "D", hash_suffix="A" * 56),
            },
        ),
        _zeile(
            Betrag="-20,00",
            Buchungsreferenz=referenz,
            **{
                "(Sammel-) Überweisung ID": "ID-DETAIL-2",
                "Enthaltene Überweisung ID": _sd_id(KONTO_A, 2026, "D", hash_suffix="B" * 56),
            },
        ),
        _zeile(
            Betrag="-100,00",
            Buchungsreferenz=referenz,
            **{
                # Sammel-ID stimmt mit KEINEM Detailposten überein.
                "(Sammel-) Überweisung ID": "ID-VOELLIG-ANDERS",
                "Enthaltene Überweisung ID": _sd_id(KONTO_A, 2026, "S", hash_suffix="C" * 56),
            },
        ),
    ]
    preview = _preview(zeilen)
    assert preview.kandidaten == []
    assert len(preview.pruefffaelle) == 3


def test_sammelgruppe_mit_nicht_passender_summe_wird_pruefffall():
    referenz = "SAMMEL-FALSCHE-SUMME"
    zeilen = [
        _zeile(
            Betrag="-80,00",
            Buchungsreferenz=referenz,
            **{
                "(Sammel-) Überweisung ID": "ID-1",
                "Enthaltene Überweisung ID": _sd_id(KONTO_A, 2026, "D", hash_suffix="A" * 56),
            },
        ),
        _zeile(
            Betrag="-19,00",
            Buchungsreferenz=referenz,
            **{
                "(Sammel-) Überweisung ID": "ID-2",
                "Enthaltene Überweisung ID": _sd_id(KONTO_A, 2026, "D", hash_suffix="B" * 56),
            },
        ),
        _zeile(
            Betrag="-100,00",
            Buchungsreferenz=referenz,
            **{
                "(Sammel-) Überweisung ID": "ID-1",
                "Enthaltene Überweisung ID": _sd_id(KONTO_A, 2026, "S", hash_suffix="C" * 56),
            },
        ),
    ]
    preview = _preview(zeilen)
    assert preview.kandidaten == []
    assert len(preview.pruefffaelle) == 3


def test_verschiedene_konten_mit_gleicher_referenz_bleiben_getrennt():
    """Zwei Sammelgruppen mit derselben Buchungsreferenz, aber
    unterschiedlichem Konto, dürfen NIE vermischt werden - das eine
    Konto liegt hier ohnehin außerhalb der Vorschau und wird separat
    ABGELEHNT, das andere normal verarbeitet."""

    referenz = "SAMMEL-KONTO-GETRENNT"
    zeilen = [
        _zeile(
            Betrag="-80,00",
            Buchungsreferenz=referenz,
            **{
                "Eigene IBAN": KONTO_A,
                "(Sammel-) Überweisung ID": "ID-1",
                "Enthaltene Überweisung ID": _sd_id(KONTO_A, 2026, "D", hash_suffix="A" * 56),
            },
        ),
        _zeile(
            Betrag="-20,00",
            Buchungsreferenz=referenz,
            **{
                "Eigene IBAN": KONTO_A,
                "(Sammel-) Überweisung ID": "ID-2",
                "Enthaltene Überweisung ID": _sd_id(KONTO_A, 2026, "D", hash_suffix="B" * 56),
            },
        ),
        _zeile(
            Betrag="-100,00",
            Buchungsreferenz=referenz,
            **{
                "Eigene IBAN": KONTO_A,
                "(Sammel-) Überweisung ID": "ID-1",
                "Enthaltene Überweisung ID": _sd_id(KONTO_A, 2026, "S", hash_suffix="C" * 56),
            },
        ),
        # Fremdes Konto, zufällig dieselbe Buchungsreferenz - darf die
        # obige, vollständig gültige Gruppe nicht stören.
        _zeile(
            Betrag="-999,00",
            Buchungsreferenz=referenz,
            **{
                "Eigene IBAN": KONTO_B,
                "(Sammel-) Überweisung ID": "ID-FREMD",
                "Enthaltene Überweisung ID": _sd_id(KONTO_B, 2026, "S", hash_suffix="D" * 56),
            },
        ),
    ]
    preview = _preview(zeilen)
    assert len(preview.kandidaten) == 2
    assert len(preview.sammel_summen) == 1
    assert len(preview.abgelehnt) == 1  # das fremde Konto
    assert preview.ausgaenge_cent == -10_000


# ---------------------------------------------------------------------------
# Saldenkontrolle
# ---------------------------------------------------------------------------


def test_saldenkontrolle_nur_wenn_beide_werte_angegeben():
    zeilen = [_zeile(Betrag="100,00", **{"Enthaltene Überweisung ID": "REF-SALDO-001"})]
    ohne_saldo = _preview(zeilen)
    assert ohne_saldo.saldo_kontrolle is None

    with pytest.raises(ValueError):
        _preview(zeilen, anfangssaldo_cent=1_000)

    mit_saldo = _preview(zeilen, anfangssaldo_cent=1_000, endsaldo_cent=11_000)
    assert mit_saldo.saldo_kontrolle is not None
    assert mit_saldo.saldo_kontrolle.stimmt_ueberein is True
    assert mit_saldo.vollstaendig is True

    abweichung = _preview(zeilen, anfangssaldo_cent=1_000, endsaldo_cent=99_999)
    assert abweichung.saldo_kontrolle.stimmt_ueberein is False
    assert abweichung.vollstaendig is False


def test_vollstaendig_ist_false_trotz_rechnerisch_passender_saldenkontrolle_bei_pruefffall():
    """Regression: 0=0 (oder ein zufällig passender Saldo) darf NICHT als
    Erfolg gelten, wenn gleichzeitig Zeilen als Prüffall/Abgelehnt aus der
    Summenbildung herausgefallen sind."""

    zeilen = [
        _zeile(Betrag="-10,00", **{"Enthaltene Überweisung ID": "DUP"}),
        _zeile(Betrag="-20,00", **{"Enthaltene Überweisung ID": "DUP"}),
    ]
    preview = _preview(zeilen, anfangssaldo_cent=0, endsaldo_cent=0)
    assert preview.saldo_kontrolle.stimmt_ueberein is True  # rechnerisch: 0 Kandidaten, 0=0
    assert preview.pruefffaelle  # aber es gibt ausgeschlossene Zeilen
    assert preview.vollstaendig is False


# ---------------------------------------------------------------------------
# Replay / Determinismus
# ---------------------------------------------------------------------------


def test_erneuter_lauf_auf_identischer_datei_liefert_identisches_ergebnis():
    referenz = "SAMMEL-REPLAY"
    zeilen = [
        _zeile(
            Betrag="-80,00",
            Buchungsreferenz=referenz,
            **{
                "(Sammel-) Überweisung ID": "ID-1",
                "Enthaltene Überweisung ID": _sd_id(KONTO_A, 2026, "D", hash_suffix="A" * 56),
            },
        ),
        _zeile(
            Betrag="-20,00",
            Buchungsreferenz=referenz,
            **{
                "(Sammel-) Überweisung ID": "ID-2",
                "Enthaltene Überweisung ID": _sd_id(KONTO_A, 2026, "D", hash_suffix="B" * 56),
            },
        ),
        _zeile(
            Betrag="-100,00",
            Buchungsreferenz=referenz,
            **{
                "(Sammel-) Überweisung ID": "ID-1",
                "Enthaltene Überweisung ID": _sd_id(KONTO_A, 2026, "S", hash_suffix="C" * 56),
            },
        ),
    ]
    rohbytes = _bytes(zeilen)
    erster_lauf = erstelle_preview(rohbytes, erwartetes_konto_iban=KONTO_A, von=date(2026, 1, 1), bis=date(2026, 1, 31))
    zweiter_lauf = erstelle_preview(rohbytes, erwartetes_konto_iban=KONTO_A, von=date(2026, 1, 1), bis=date(2026, 1, 31))
    assert erster_lauf == zweiter_lauf


def test_jede_zeile_traegt_sha256_und_felder():
    preview = _preview([_zeile(Betrag="10,00")])
    zeile = preview.zeilen[0]
    assert len(zeile.sha256_zeile) == 64
    assert zeile.felder["Betrag"] == "10,00"
    assert len(preview.datei_sha256) == 64
    assert zeile.zeile_nr == 1
    assert zeile.csv_zeile >= 2  # nach der Kopfzeile


# ---------------------------------------------------------------------------
# Null DB-/Netzwerknebenwirkung
# ---------------------------------------------------------------------------


def test_modul_hat_keine_db_oder_netzwerk_abhaengigkeit():
    quelltext = inspect.getsource(gbc)
    verbotene_begriffe = ["sqlalchemy", "session_factory", "socket", "requests", "httpx", "urllib", "smtplib"]
    for begriff in verbotene_begriffe:
        assert begriff not in quelltext.lower(), f"Unerwartete Abhängigkeit '{begriff}' im Vorschau-Adapter gefunden."


def test_erstelle_preview_signatur_verlangt_kein_db_argument():
    parameter = inspect.signature(erstelle_preview).parameters
    for name in parameter:
        assert "session" not in name.lower()
        assert "repo" not in name.lower()


# ---------------------------------------------------------------------------
# Befund 1 (2. Runde): kaputtes/inkonsistentes Sammelprofil darf NIE
# stillschweigend wie ein normaler Einzelumsatz durchgereicht werden.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kaputte_id",
    [
        pytest.param(_sd_id_variante(KONTO_A, 2025, "D"), id="falsches_jahr"),
        pytest.param(_sd_id_variante(KONTO_A, 2026, "D", eingebettetes_konto=KONTO_B), id="falsches_konto"),
        pytest.param(_sd_id_variante(KONTO_A, 2026, "D", waehrung="USD"), id="falsche_waehrung"),
        pytest.param(_sd_id_variante(KONTO_A, 2026, "D", padding="1" * 14), id="falsches_padding"),
        pytest.param(_sd_id_variante(KONTO_A, 2026, "D", hash_suffix="G" * 56), id="ungueltiges_hex"),
        pytest.param(_sd_id_variante(KONTO_A, 2026, "D", hash_laenge=20), id="abgeschnittener_suffix"),
    ],
)
def test_kaputtes_sammelprofil_wird_nie_stillschweigend_normaler_einzelumsatz(kaputte_id):
    """Konkrete Regression: eine isolierte 107-Zeichen-D-ID mit falschem
    Jahr (07.09.2026 vs. eingebettetem 2025) - sowie Varianten mit
    falschem Konto/Währung/Padding/Hex/abgeschnittenem Suffix - wurden
    zuvor als gewöhnlicher Einzelumsatz akzeptiert, weil
    `_sammel_id_analyse` None lieferte und das direkt als "andere ID"
    gewertet wurde. Jetzt: lang genug für einen Profilversuch -> Prüffall,
    nie ein normaler Kandidat."""

    preview = _preview(
        [_zeile(Betrag="-30,00", Buchungsreferenz="EGAL", **{"Enthaltene Überweisung ID": kaputte_id})],
        von=date(2026, 9, 1),
        bis=date(2026, 9, 30),
    )
    assert preview.kandidaten == []
    assert len(preview.pruefffaelle) == 1


def test_kaputtes_gruppenmitglied_poisoned_sonst_valide_erscheinende_restgruppe():
    """Ohne D1(-80) + S(-80) allein würde die Gruppe valide erscheinen
    (Summe stimmt exakt) - aber ein drittes Mitglied mit kaputtem Profil
    (gleiches Konto/Währung/Datum/Referenz) darf nicht einfach aus der
    Gruppenprüfung verschwinden, während die übrigen zufällig aufgehen."""

    referenz = "SAMMEL-POISON"
    zeilen = [
        _zeile(
            Betrag="-80,00",
            Buchungsreferenz=referenz,
            **{
                "(Sammel-) Überweisung ID": "ID-1",
                "Enthaltene Überweisung ID": _sd_id(KONTO_A, 2026, "D", hash_suffix="A" * 56),
            },
        ),
        _zeile(
            Betrag="-999,00",
            Buchungsreferenz=referenz,
            **{
                "(Sammel-) Überweisung ID": "ID-2",
                "Enthaltene Überweisung ID": _sd_id_variante(KONTO_A, 2026, "D", waehrung="USD"),
            },
        ),
        _zeile(
            Betrag="-80,00",
            Buchungsreferenz=referenz,
            **{
                "(Sammel-) Überweisung ID": "ID-1",
                "Enthaltene Überweisung ID": _sd_id(KONTO_A, 2026, "S", hash_suffix="C" * 56),
            },
        ),
    ]
    preview = _preview(zeilen)
    assert preview.kandidaten == []
    assert preview.sammel_summen == []
    assert len(preview.pruefffaelle) == 3


def test_zu_kurze_id_bleibt_andersartiges_einzelumsatz_format():
    """Gegenprobe zu obigen Tests: eine ID, die klar zu KURZ für einen
    Profilversuch ist (typisches anderes, opakes Einzelumsatz-Format),
    bleibt weiterhin ein gewöhnlicher Kandidat."""

    preview = _preview([_zeile(Betrag="-30,00", **{"Enthaltene Überweisung ID": "REF-12345"})])
    assert len(preview.kandidaten) == 1


# ---------------------------------------------------------------------------
# Befund 2 (2. Runde): leere Datenmenge ist keine bestätigte Vollständigkeit.
# ---------------------------------------------------------------------------


def test_datei_ohne_datenzeilen_wird_abgelehnt_statt_stillschweigend_vollstaendig():
    kopf = ",".join(f'"{s}"' for s in ERWARTETE_SPALTEN)
    text = kopf + "\r\n"
    with pytest.raises(GeorgeFormatFehlerError):
        erstelle_preview(text.encode("utf-8"), erwartetes_konto_iban=KONTO_A, von=date(2026, 1, 1), bis=date(2026, 1, 31))


def test_datei_mit_nur_leerzeilen_nach_kopfzeile_wird_ebenfalls_abgelehnt():
    kopf = ",".join(f'"{s}"' for s in ERWARTETE_SPALTEN)
    text = kopf + "\r\n\r\n\r\n"
    with pytest.raises(GeorgeFormatFehlerError):
        erstelle_preview(text.encode("utf-8"), erwartetes_konto_iban=KONTO_A, von=date(2026, 1, 1), bis=date(2026, 1, 31))


# ---------------------------------------------------------------------------
# Befund 3 (2. Runde): felder/sha256_zeile aus exakten dekodierten Werten,
# Normalisierung (Konto/Datum/Betrag) getrennt davon.
# ---------------------------------------------------------------------------


def test_felder_und_hash_bewahren_originale_leerzeichen_normalisierung_ist_getrennt():
    zeile = _zeile(Betrag="  50,00  ", **{"Eigene IBAN": f"  {KONTO_A}  ", "Enthaltene Überweisung ID": "REF-WHITESPACE"})
    preview = _preview([zeile])
    ergebnis = preview.zeilen[0]

    assert ergebnis.status == "KANDIDAT"
    # Fachlogik nutzt eine NORMALISIERTE (getrimmte) Kopie:
    assert ergebnis.kandidat.betrag_cent == 5_000
    assert ergebnis.kandidat.eigene_iban == KONTO_A
    # Audit-Trail (felder/Hash) bewahrt die EXAKTEN Originalwerte:
    assert ergebnis.felder["Betrag"] == "  50,00  "
    assert ergebnis.felder["Eigene IBAN"] == f"  {KONTO_A}  "


def test_feldhash_unterscheidet_originale_mit_unterschiedlichen_leerzeichen():
    """Regression: wurde vorher blind gestrippt, bevor Feld/Hash gebildet
    wurden - zwei Originalzeilen, die sich NUR durch Leerraum
    unterscheiden, ergaben denselben Hash."""

    zeile_a = _zeile(Betrag="50,00", **{"Enthaltene Überweisung ID": "REF-LEERRAUM-A"})
    zeile_b = _zeile(Betrag=" 50,00 ", **{"Enthaltene Überweisung ID": "REF-LEERRAUM-B"})
    preview = _preview([zeile_a, zeile_b])
    assert len(preview.kandidaten) == 2
    hashes = {z.sha256_zeile for z in preview.kandidaten}
    assert len(hashes) == 2


# ---------------------------------------------------------------------------
# Befund 4 (2. Runde): CLI/Modell dürfen "Saldenkontrolle OK" nie vor dem
# Gesamtstatus VOLLSTÄNDIG zeigen (siehe auch test_george_business_preview_cli.py).
# ---------------------------------------------------------------------------


def test_vollstaendig_bleibt_false_bei_rein_rechnerisch_passender_saldenkontrolle_trotz_kaputtem_profil():
    preview = _preview(
        [_zeile(Betrag="-30,00", Buchungsreferenz="EGAL", **{"Enthaltene Überweisung ID": _sd_id_variante(KONTO_A, 2025, "D")})],
        anfangssaldo_cent=0,
        endsaldo_cent=0,
    )
    assert preview.saldo_kontrolle.stimmt_ueberein is True
    assert preview.vollstaendig is False
