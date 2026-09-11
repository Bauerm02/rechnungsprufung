"""Tests für den rein lesenden George-Business-CSV-Vorschau-Adapter.

Alle Testdaten sind frei erfunden (synthetisch) — keine echten Konten,
IBANs oder Buchungen. Der Adapter selbst greift nie auf eine Datenbank
oder das Netzwerk zu; ein eigener Test (`test_modul_hat_keine_db_oder_
netzwerk_abhaengigkeit`) prüft das auch strukturell ab.
"""

from __future__ import annotations

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
    datenzeilen = []
    for zeile in zeilen:
        datenzeilen.append(",".join(f'"{zeile.get(spalte, "")}"' for spalte in ERWARTETE_SPALTEN))
    return "\r\n".join([kopf, *datenzeilen]) + "\r\n"


def _bytes(zeilen: list[dict[str, str]], *, bom: bool = True) -> bytes:
    text = _csv_text(zeilen)
    encoded = text.encode("utf-8")
    return (b"\xef\xbb\xbf" + encoded) if bom else encoded


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
        "1.5",  # nur eine Nachkommastelle - Bankexport liefert immer zwei
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


def test_akzeptiert_bom_komma_und_crlf_zeilenumbrueche():
    rohbytes = _bytes([_zeile(Betrag="50,00")], bom=True)
    assert rohbytes.startswith(b"\xef\xbb\xbf")
    preview = erstelle_preview(rohbytes, erwartetes_konto_iban=KONTO_A, von=date(2026, 1, 1), bis=date(2026, 1, 31))
    assert len(preview.zeilen) == 1
    assert preview.kandidaten[0].kandidat.betrag_cent == 5_000


def test_ueberspringt_vollstaendig_leere_zeile_am_dateiende():
    text = _csv_text([_zeile()]) + "\r\n"  # zusätzliche Leerzeile
    preview = erstelle_preview(text.encode("utf-8"), erwartetes_konto_iban=KONTO_A, von=date(2026, 1, 1), bis=date(2026, 1, 31))
    assert len(preview.zeilen) == 1


# ---------------------------------------------------------------------------
# Datum-/Konten-/Währungsfehler -> ABGELEHNT, nie stillschweigend interpretiert
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("feld", "wert"),
    [
        ("Buchungsdatum", "2026-01-15"),  # ISO statt TT.MM.JJJJ
        ("Buchungsdatum", "1.1.2026"),  # nicht zweistellig
        ("Buchungsdatum", "31.02.2026"),  # kalendarisch ungültig
        ("Eigene IBAN", ""),  # fehlt
        ("Währung", "USD"),  # Fremdwährung
        ("Währung", ""),  # fehlt
    ],
)
def test_ungueltige_pflichtfelder_fuehren_zu_abgelehnt(feld, wert):
    rohbytes = _bytes([_zeile(**{feld: wert})])
    preview = erstelle_preview(rohbytes, erwartetes_konto_iban=KONTO_A, von=date(2026, 1, 1), bis=date(2026, 1, 31))
    assert len(preview.abgelehnt) == 1
    assert preview.kandidaten == []


def test_ungueltiges_valutadatum_fuehrt_zu_abgelehnt_statt_stillschweigend_ignoriert():
    rohbytes = _bytes([_zeile(Valutadatum="32.01.2026")])
    preview = erstelle_preview(rohbytes, erwartetes_konto_iban=KONTO_A, von=date(2026, 1, 1), bis=date(2026, 1, 31))
    assert len(preview.abgelehnt) == 1


# ---------------------------------------------------------------------------
# Einzelumsätze
# ---------------------------------------------------------------------------


def test_einfacher_einzelumsatz_ohne_sammel_id_wird_kandidat():
    rohbytes = _bytes(
        [_zeile(Betrag="-45,50", **{"(Sammel-) Überweisung ID": "", "Enthaltene Überweisung ID": ""})]
    )
    preview = erstelle_preview(rohbytes, erwartetes_konto_iban=KONTO_A, von=date(2026, 1, 1), bis=date(2026, 1, 31))
    assert len(preview.kandidaten) == 1
    kandidat = preview.kandidaten[0].kandidat
    assert kandidat.betrag_cent == -4_550
    assert kandidat.sammel_id is None
    assert kandidat.enthaltene_id is None


def test_leere_und_notprovided_id_werden_nicht_als_eindeutiger_schluessel_verwendet():
    rohbytes = _bytes(
        [
            _zeile(Betrag="-10,00", Buchungsreferenz="NOTPROVIDED", **{"Enthaltene Überweisung ID": ""}),
            _zeile(Betrag="-20,00", Buchungsreferenz="NOTPROVIDED", **{"Enthaltene Überweisung ID": ""}),
        ]
    )
    preview = erstelle_preview(rohbytes, erwartetes_konto_iban=KONTO_A, von=date(2026, 1, 1), bis=date(2026, 1, 31))
    # Beide Zeilen bleiben trotz identischer (Platzhalter-)Buchungsreferenz
    # getrennt erhalten - NOTPROVIDED darf nie als Dedup-Schlüssel wirken.
    assert len(preview.kandidaten) == 2
    betraege = sorted(k.kandidat.betrag_cent for k in preview.kandidaten)
    assert betraege == [-2_000, -1_000]


# ---------------------------------------------------------------------------
# Sammelüberweisungen: Summe + Details
# ---------------------------------------------------------------------------


def test_sammelgruppe_mit_gueltiger_ds_markierung_erkennt_summenzeile():
    sammel_id = "SAMMEL-A"
    rohbytes = _bytes(
        [
            _zeile(Betrag="-80,00", **{"(Sammel-) Überweisung ID": sammel_id, "Enthaltene Überweisung ID": "AT61EUR20260115D1"}),
            _zeile(Betrag="-20,00", **{"(Sammel-) Überweisung ID": sammel_id, "Enthaltene Überweisung ID": "AT61EUR20260115D2"}),
            _zeile(Betrag="-100,00", **{"(Sammel-) Überweisung ID": sammel_id, "Enthaltene Überweisung ID": "AT61EUR20260115S"}),
        ]
    )
    preview = erstelle_preview(rohbytes, erwartetes_konto_iban=KONTO_A, von=date(2026, 1, 1), bis=date(2026, 1, 31))
    assert len(preview.kandidaten) == 2
    assert len(preview.sammel_summen) == 1
    assert preview.pruefffaelle == []
    kandidat_betraege = sorted(k.kandidat.betrag_cent for k in preview.kandidaten)
    assert kandidat_betraege == [-8_000, -2_000]
    # Die Summenzeile selbst zählt NICHT doppelt in den Ausgängen.
    assert preview.ausgaenge_cent == -10_000


def test_unvollstaendige_sammelgruppe_ohne_detailzeilen_wird_pruefffall():
    sammel_id = "SAMMEL-B"
    rohbytes = _bytes(
        [
            _zeile(Betrag="-100,00", **{"(Sammel-) Überweisung ID": sammel_id, "Enthaltene Überweisung ID": "AT61EUR20260115S"}),
            _zeile(Betrag="-30,00", **{"(Sammel-) Überweisung ID": sammel_id, "Enthaltene Überweisung ID": "AT61EUR20260115D1"}),
            # Zweite Detailzeile fehlt: -30,00 summiert sich NICHT auf -100,00.
        ]
    )
    preview = erstelle_preview(rohbytes, erwartetes_konto_iban=KONTO_A, von=date(2026, 1, 1), bis=date(2026, 1, 31))
    assert len(preview.pruefffaelle) == 2
    assert preview.kandidaten == []
    assert preview.sammel_summen == []


def test_sammelgruppe_ohne_bestimmbare_markierung_wird_pruefffall():
    sammel_id = "SAMMEL-C"
    rohbytes = _bytes(
        [
            _zeile(Betrag="-80,00", **{"(Sammel-) Überweisung ID": sammel_id, "Enthaltene Überweisung ID": "123456789"}),
            _zeile(Betrag="-20,00", **{"(Sammel-) Überweisung ID": sammel_id, "Enthaltene Überweisung ID": "987654321"}),
            _zeile(Betrag="-100,00", **{"(Sammel-) Überweisung ID": sammel_id, "Enthaltene Überweisung ID": "555555555"}),
        ]
    )
    preview = erstelle_preview(rohbytes, erwartetes_konto_iban=KONTO_A, von=date(2026, 1, 1), bis=date(2026, 1, 31))
    assert len(preview.pruefffaelle) == 3
    assert preview.kandidaten == []


def test_sammelgruppe_mit_nicht_passender_summe_wird_pruefffall():
    sammel_id = "SAMMEL-D"
    rohbytes = _bytes(
        [
            _zeile(Betrag="-80,00", **{"(Sammel-) Überweisung ID": sammel_id, "Enthaltene Überweisung ID": "AT61EUR20260115D1"}),
            _zeile(Betrag="-19,00", **{"(Sammel-) Überweisung ID": sammel_id, "Enthaltene Überweisung ID": "AT61EUR20260115D2"}),
            _zeile(Betrag="-100,00", **{"(Sammel-) Überweisung ID": sammel_id, "Enthaltene Überweisung ID": "AT61EUR20260115S"}),
        ]
    )
    preview = erstelle_preview(rohbytes, erwartetes_konto_iban=KONTO_A, von=date(2026, 1, 1), bis=date(2026, 1, 31))
    assert len(preview.pruefffaelle) == 3
    assert preview.kandidaten == []


def test_sammel_id_mit_nur_einer_zeile_wird_normaler_kandidat():
    rohbytes = _bytes(
        [_zeile(Betrag="-15,00", **{"(Sammel-) Überweisung ID": "SAMMEL-EINZEL", "Enthaltene Überweisung ID": "AT61EUR20260115D1"})]
    )
    preview = erstelle_preview(rohbytes, erwartetes_konto_iban=KONTO_A, von=date(2026, 1, 1), bis=date(2026, 1, 31))
    assert len(preview.kandidaten) == 1
    assert preview.kandidaten[0].kandidat.betrag_cent == -1_500


# ---------------------------------------------------------------------------
# Echte Dubletten dürfen nie stillschweigend entfernt werden
# ---------------------------------------------------------------------------


def test_gleiche_echte_abbuchungen_mit_unterschiedlichen_referenzen_bleiben_beide_erhalten():
    rohbytes = _bytes(
        [
            _zeile(Betrag="-50,00", Buchungsreferenz="REF-001", Zahlungsreferenz="Miete Jaenner"),
            _zeile(Betrag="-50,00", Buchungsreferenz="REF-002", Zahlungsreferenz="Miete Jaenner"),
        ]
    )
    preview = erstelle_preview(rohbytes, erwartetes_konto_iban=KONTO_A, von=date(2026, 1, 1), bis=date(2026, 1, 31))
    assert len(preview.kandidaten) == 2
    assert preview.ausgaenge_cent == -10_000


def test_gleiche_referenz_auf_mehreren_konten_bleibt_getrennt():
    rohbytes = _bytes(
        [
            _zeile(Betrag="-50,00", Buchungsreferenz="REF-SHARED", **{"Eigene IBAN": KONTO_A}),
            _zeile(Betrag="-50,00", Buchungsreferenz="REF-SHARED", **{"Eigene IBAN": KONTO_B}),
        ]
    )
    preview = erstelle_preview(rohbytes, erwartetes_konto_iban=KONTO_A, von=date(2026, 1, 1), bis=date(2026, 1, 31))
    assert len(preview.kandidaten) == 2
    konto_a_kandidaten = [k for k in preview.kandidaten if k.kandidat.konto_stimmt_ueberein]
    konto_b_kandidaten = [k for k in preview.kandidaten if not k.kandidat.konto_stimmt_ueberein]
    assert len(konto_a_kandidaten) == 1
    assert len(konto_b_kandidaten) == 1
    # Nur der zum erwarteten Konto passende Umsatz fließt in die Summe ein.
    assert preview.ausgaenge_cent == -5_000


# ---------------------------------------------------------------------------
# Konto-/Zeitraumprüfung und Saldenkontrolle
# ---------------------------------------------------------------------------


def test_zeile_ausserhalb_des_erwarteten_zeitraums_bleibt_sichtbar_aber_nicht_in_der_summe():
    rohbytes = _bytes([_zeile(Betrag="-10,00", Buchungsdatum="01.02.2026")])
    preview = erstelle_preview(rohbytes, erwartetes_konto_iban=KONTO_A, von=date(2026, 1, 1), bis=date(2026, 1, 31))
    assert len(preview.kandidaten) == 1
    assert preview.kandidaten[0].kandidat.im_erwarteten_zeitraum is False
    assert preview.ausgaenge_cent == 0


def test_saldenkontrolle_nur_wenn_beide_werte_angegeben():
    rohbytes = _bytes([_zeile(Betrag="100,00")])
    ohne_saldo = erstelle_preview(rohbytes, erwartetes_konto_iban=KONTO_A, von=date(2026, 1, 1), bis=date(2026, 1, 31))
    assert ohne_saldo.saldo_kontrolle is None

    with pytest.raises(ValueError):
        erstelle_preview(
            rohbytes, erwartetes_konto_iban=KONTO_A, von=date(2026, 1, 1), bis=date(2026, 1, 31),
            anfangssaldo_cent=1_000,
        )

    mit_saldo = erstelle_preview(
        rohbytes, erwartetes_konto_iban=KONTO_A, von=date(2026, 1, 1), bis=date(2026, 1, 31),
        anfangssaldo_cent=1_000, endsaldo_cent=11_000,
    )
    assert mit_saldo.saldo_kontrolle is not None
    assert mit_saldo.saldo_kontrolle.stimmt_ueberein is True

    abweichung = erstelle_preview(
        rohbytes, erwartetes_konto_iban=KONTO_A, von=date(2026, 1, 1), bis=date(2026, 1, 31),
        anfangssaldo_cent=1_000, endsaldo_cent=99_999,
    )
    assert abweichung.saldo_kontrolle.stimmt_ueberein is False


# ---------------------------------------------------------------------------
# Replay / Determinismus
# ---------------------------------------------------------------------------


def test_erneuter_lauf_auf_identischer_datei_liefert_identisches_ergebnis():
    rohbytes = _bytes(
        [
            _zeile(Betrag="-80,00", **{"(Sammel-) Überweisung ID": "S1", "Enthaltene Überweisung ID": "AT61EUR20260115D1"}),
            _zeile(Betrag="-20,00", **{"(Sammel-) Überweisung ID": "S1", "Enthaltene Überweisung ID": "AT61EUR20260115D2"}),
            _zeile(Betrag="-100,00", **{"(Sammel-) Überweisung ID": "S1", "Enthaltene Überweisung ID": "AT61EUR20260115S"}),
        ]
    )
    erster_lauf = erstelle_preview(rohbytes, erwartetes_konto_iban=KONTO_A, von=date(2026, 1, 1), bis=date(2026, 1, 31))
    zweiter_lauf = erstelle_preview(rohbytes, erwartetes_konto_iban=KONTO_A, von=date(2026, 1, 1), bis=date(2026, 1, 31))
    assert erster_lauf == zweiter_lauf


def test_jede_zeile_traegt_sha256_und_rohzeile():
    rohbytes = _bytes([_zeile(Betrag="10,00")])
    preview = erstelle_preview(rohbytes, erwartetes_konto_iban=KONTO_A, von=date(2026, 1, 1), bis=date(2026, 1, 31))
    zeile = preview.zeilen[0]
    assert len(zeile.sha256_zeile) == 64
    assert "Betrag=10,00" in zeile.roh_zeile
    assert len(preview.datei_sha256) == 64


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
