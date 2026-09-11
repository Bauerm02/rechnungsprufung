"""Unit-Tests für die zentrale Formular-Betragskonvertierung des
Backoffice (`backoffice/views.py::parse_eur_betrag`).

Regression (Browser-Befund auf e16914d): die manuelle Bankzuordnung
befüllt das Betragsfeld selbst mit dem von `eur()` erzeugten deutschen
Format (z. B. "1.500,00" - Punkt als Tausender-, Komma als
Dezimaltrennzeichen). Ein unverändert abgeschicktes Formular rief bisher
`to_cents(betrag.replace(",", "."))` auf, was "1.500,00" zu "1.500.00"
verstümmelte und ein unbehandeltes `decimal.InvalidOperation` (HTTP 500)
auslöste - an allen drei Formularstellen (Nachbuchung, Korrektur,
manuelle Bankzuordnung)."""

from __future__ import annotations

import pytest

from mietinkasso.backoffice.views import eur, parse_eur_betrag


@pytest.mark.parametrize(
    ("eingabe", "erwartete_cent"),
    [
        ("1.500,00", 150_000),  # deutsche Notation: Punkt=Tausender, Komma=Dezimal
        ("1500,00", 150_000),  # deutsche Notation ohne Tausenderpunkt
        ("1500.00", 150_000),  # bisher akzeptiertes einfaches Punkt-Dezimalformat
        ("1500", 150_000),  # bisher akzeptiert: reine Ganzzahl
        ("0,05", 5),
        ("  1.234,56  ", 123_456),  # umgebende Leerzeichen werden toleriert
        ("1.234.567,89", 123_456_789),  # mehrstellige Tausendergruppierung
        ("-1.500,00", -150_000),  # negativer Betrag (z. B. Korrektur nach unten)
        ("1.5", 150),  # einfaches Format mit einer Nachkommastelle
    ],
)
def test_parse_eur_betrag_akzeptiert_deutsche_und_einfache_notation(eingabe, erwartete_cent):
    assert parse_eur_betrag(eingabe) == erwartete_cent


def test_parse_eur_betrag_rundtrip_mit_eur_formatierung():
    """Das von `eur()` erzeugte Format (das die manuelle Bankzuordnung als
    Vorschlag ins Formularfeld schreibt) muss unverändert wieder
    einlesbar sein - genau der gemeldete Bedienungsfehler."""

    for cent in (150_000, 76_000, 5, 100_000_000):
        assert parse_eur_betrag(eur(cent).split(" ")[0]) == cent


@pytest.mark.parametrize(
    "eingabe",
    [
        "",
        "   ",
        None,
        "abc",
        "1,500.00",  # US-Format (Komma=Tausender, Punkt=Dezimal) wird NICHT unterstützt -> mehrdeutig
        "1.500.00",  # zwei Punkte ohne Komma - kaputt
        "12,34,56",  # mehrere Kommas - kaputt
        "€ 100",
        "1.500",  # ohne Komma mehrdeutig: Tausenderpunkt oder Dezimalpunkt?
        "1,500",  # ohne führende Zifferngruppierung mehrdeutig
        "1.50,00",  # unvollständige Tausendergruppierung vor dem Komma
        "1234.567,89",  # erste Gruppe hat vier statt drei Ziffern
        "1 5,00",  # Leerzeichen innerhalb des Betrags
        "1e3",  # wissenschaftliche Notation
        "NaN",
        "Infinity",
        "999999999999999999999.99",  # überschreitet den zulässigen Speicherbereich
    ],
)
def test_parse_eur_betrag_lehnt_mehrdeutige_oder_kaputte_eingaben_verstaendlich_ab(eingabe):
    with pytest.raises(ValueError):
        parse_eur_betrag(eingabe)
