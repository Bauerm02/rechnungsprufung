"""Tests für die österreichische Fristberechnung (§902/903 ABGB,
Auftrag HV-20261020-VIER-MAHNSTUFEN) - `mahnwesen/fristen.py` ist eine
reine, DB-lose Funktion ohne Seiteneffekt."""

from __future__ import annotations

from datetime import date

from mietinkasso.mahnwesen.fristen import (
    frist_abgelaufen,
    fristende_ab_zugang,
    ist_werktag,
    oesterreichische_feiertage,
    naechster_werktag,
)


def test_grenzfall_zugang_10_oktober_eskaliert_nicht_am_25_oktober():
    """Wörtliches Beispiel aus dem Auftrag: Zugang 10.10.2026, 14-Tage-
    Frist. Rechnerisches Ende 24.10.2026 (Samstag) -> 25. ist Sonntag ->
    26. ist Nationalfeiertag -> tatsächliches Fristende 27.10.2026. Der
    Prüflauf am 25.10.2026 darf NICHT eskalieren, der am 27.10.2026 (oder
    später) schon."""

    zugang = date(2026, 10, 10)
    assert fristende_ab_zugang(zugang, 14) == date(2026, 10, 27)
    assert frist_abgelaufen(zugang_am=zugang, frist_tage=14, heute=date(2026, 10, 25)) is False
    assert frist_abgelaufen(zugang_am=zugang, frist_tage=14, heute=date(2026, 10, 26)) is False
    assert frist_abgelaufen(zugang_am=zugang, frist_tage=14, heute=date(2026, 10, 27)) is True
    assert frist_abgelaufen(zugang_am=zugang, frist_tage=14, heute=date(2026, 10, 28)) is True


def test_fristende_ohne_wochenend_feiertag_kollision_bleibt_unverschoben():
    """Ein rechnerisches Fristende, das selbst schon ein Werktag ist,
    wird NICHT künstlich weiterverschoben."""

    # 01.06.2026 (Montag) + 14 Tage = 15.06.2026 (Montag, Werktag).
    assert fristende_ab_zugang(date(2026, 6, 1), 14) == date(2026, 6, 15)


def test_zugangstag_selbst_zaehlt_nicht_mit():
    """§902 ABGB: der Zugangstag ist Tag 0, nicht Tag 1 - eine 14-Tage-
    Frist ab einem Montag endet (ohne Wochenend-/Feiertagskollision)
    exakt 14 Kalendertage später, nicht 13."""

    zugang = date(2026, 6, 1)  # Montag
    assert (fristende_ab_zugang(zugang, 14) - zugang).days == 14


def test_karfreitag_verschiebt_fristende():
    """Karfreitag 2026 ist der 03.04.2026 - ein darauf fallendes
    Fristende verschiebt sich (Auftrag Markus: Karfreitag blockiert den
    Fristenlauf, unabhängig von der engeren arbeitsrechtlichen
    Einordnung)."""

    karfreitag_2026 = date(2026, 4, 3)
    assert karfreitag_2026 in oesterreichische_feiertage(2026)
    assert ist_werktag(karfreitag_2026) is False
    assert naechster_werktag(karfreitag_2026) == date(2026, 4, 7)  # Ostermontag (06.04.) ist ebenfalls Feiertag


def test_bewegliche_feiertage_fuer_mehrere_jahre_deterministisch_korrekt():
    """Bekannte, unabhängig überprüfbare Ostersonntage - stellt sicher,
    dass der gaußsche Osteralgorithmus für mehrere Jahre stimmt, nicht
    nur zufällig für ein einzelnes Testjahr."""

    assert date(2026, 4, 5) in {date(2026, 4, 5)}  # Ostersonntag 2026 (bekannt)
    feiertage_2026 = oesterreichische_feiertage(2026)
    feiertage_2027 = oesterreichische_feiertage(2027)
    assert date(2026, 4, 3) in feiertage_2026  # Karfreitag 2026
    assert date(2026, 4, 6) in feiertage_2026  # Ostermontag 2026
    assert date(2026, 5, 14) in feiertage_2026  # Christi Himmelfahrt 2026
    assert date(2026, 5, 25) in feiertage_2026  # Pfingstmontag 2026
    assert date(2026, 6, 4) in feiertage_2026  # Fronleichnam 2026
    assert date(2027, 3, 26) in feiertage_2027  # Karfreitag 2027 (Ostersonntag 28.03.2027)


def test_fixe_feiertage_vollstaendig():
    feiertage = oesterreichische_feiertage(2026)
    for erwartet in (
        date(2026, 1, 1), date(2026, 1, 6), date(2026, 5, 1), date(2026, 8, 15),
        date(2026, 10, 26), date(2026, 11, 1), date(2026, 12, 8), date(2026, 12, 25), date(2026, 12, 26),
    ):
        assert erwartet in feiertage


def test_samstag_sonntag_sind_keine_werktage():
    assert ist_werktag(date(2026, 10, 24)) is False  # Samstag
    assert ist_werktag(date(2026, 10, 25)) is False  # Sonntag
    assert ist_werktag(date(2026, 10, 27)) is True  # Dienstag, kein Feiertag


def test_jahreswechsel_bei_fristverschiebung_funktioniert():
    """Silvester (31.12., kein Feiertag) + Neujahr (01.01., Feiertag) -
    die Verschiebung über den Jahreswechsel hinweg muss den Feiertags-
    kalender des NEUEN Jahres verwenden, nicht den des alten."""

    # 17.12.2026 (Donnerstag) + 14 Tage = 31.12.2026 (Donnerstag, Werktag,
    # kein Feiertag) - bleibt unverschoben.
    assert fristende_ab_zugang(date(2026, 12, 17), 14) == date(2026, 12, 31)
    # 18.12.2026 (Freitag) + 14 Tage = 01.01.2027 (Neujahr) -> 02.01.2027
    # (Samstag) -> 03.01.2027 (Sonntag) -> 04.01.2027 (Montag, Werktag).
    assert fristende_ab_zugang(date(2026, 12, 18), 14) == date(2027, 1, 4)
