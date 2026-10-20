"""Rechtssichere Fristberechnung für das Mahnwesen (Auftrag
HV-20261020-VIER-MAHNSTUFEN): §902/§903 ABGB (Fristenlauf) + die
österreichischen gesetzlichen Feiertage (Bundesgesetz vom 1. März 1961
betreffend die Arbeitsruhe an Feiertagen, BGBl. Nr. 37/1961 - "Karfreitag"
wird hier auf AUSDRÜCKLICHEN Auftrag Markus als fristenhemmender Tag
behandelt, unabhängig von der engeren arbeitsrechtlichen Diskussion um
seinen Status als "gesetzlicher Feiertag" für alle Arbeitnehmer).

**§902/§903 ABGB - Grundregel:** der Tag des fristauslösenden Ereignisses
(hier: der nachgewiesene Zugang eines Mahnschreibens) wird NICHT
mitgezählt (`Zugangstag` ist Tag 0). Eine nach Tagen bestimmte Frist endet
mit Ablauf des letzten Tages - für eine 14-Tage-Frist ab Zugang also mit
Ablauf von `Zugang + 14 Kalendertagen`. Fällt dieses rechnerische
Fristende auf einen Samstag, Sonntag oder gesetzlichen Feiertag, verschiebt
sich das Fristende auf den NÄCHSTFOLGENDEN Tag, der weder Samstag noch
Sonntag noch Feiertag ist (§903 ABGB) - NIEMALS eine abweichende
vertragliche Regelung erfinden, diese Funktion kennt nur die gesetzliche
Grundregel.

**Bewegliche Feiertage:** Ostersonntag wird deterministisch über den
gaußschen Osteralgorithmus berechnet (siehe `_ostersonntag`) - Karfreitag,
Ostermontag, Christi Himmelfahrt, Pfingstmontag und Fronleichnam hängen
alle vom Ostersonntag ab und sind damit für JEDES Jahr ohne externe
Kalenderdaten/Tabellen berechenbar.

**Konkretes Grenzfall-Beispiel (Auftrag Markus, Regressionstest Pflicht):**
Zugang am 10.10.2026, 14-Tage-Frist -> rechnerisches Ende 24.10.2026
(Samstag) -> 25.10.2026 ist Sonntag -> 26.10.2026 ist Nationalfeiertag ->
tatsächliches Fristende ist der 27.10.2026 (Dienstag). Ein Prüflauf am
25.10.2026 darf diese Forderung deshalb NOCH NICHT eskalieren."""

from __future__ import annotations

from datetime import date, timedelta

#: Bewegliche Feiertage, deren Abstand zum Ostersonntag IMMER gleich ist
#: (siehe Moduldoc) - `tage_nach_ostersonntag`: negativ für Karfreitag.
_BEWEGLICHE_FEIERTAGE_OFFSET_TAGE = (-2, 1, 39, 50, 60)  # Karfreitag, Ostermontag, Christi Himmelfahrt, Pfingstmontag, Fronleichnam


def _ostersonntag(jahr: int) -> date:
    """Gaußscher Osteralgorithmus (gregorianischer Kalender) - liefert
    den Ostersonntag für `jahr` ohne externe Tabellen/Netzzugriff."""

    a = jahr % 19
    b = jahr // 100
    c = jahr % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    monat = (h + l - 7 * m + 114) // 31
    tag = ((h + l - 7 * m + 114) % 31) + 1
    return date(jahr, monat, tag)


def oesterreichische_feiertage(jahr: int) -> frozenset[date]:
    """Alle bundesweit gesetzlichen Feiertage EINES Jahres - fixe Daten
    plus die vom Ostersonntag abhängigen beweglichen (siehe Moduldoc)."""

    ostersonntag = _ostersonntag(jahr)
    fixe_feiertage = (
        date(jahr, 1, 1),  # Neujahr
        date(jahr, 1, 6),  # Heilige Drei Könige
        date(jahr, 5, 1),  # Staatsfeiertag
        date(jahr, 8, 15),  # Mariä Himmelfahrt
        date(jahr, 10, 26),  # Nationalfeiertag
        date(jahr, 11, 1),  # Allerheiligen
        date(jahr, 12, 8),  # Mariä Empfängnis
        date(jahr, 12, 25),  # Christtag
        date(jahr, 12, 26),  # Stefanitag
    )
    bewegliche_feiertage = tuple(
        ostersonntag + timedelta(days=offset) for offset in _BEWEGLICHE_FEIERTAGE_OFFSET_TAGE
    )
    return frozenset(fixe_feiertage) | frozenset(bewegliche_feiertage)


def ist_werktag(tag: date) -> bool:
    """`False` an Samstagen, Sonntagen und gesetzlichen Feiertagen (siehe
    `oesterreichische_feiertage`) - sonst `True`. Keine Kenntnis von
    betriebsinternen/vertraglichen Zusatz-Schließtagen (die erfindet diese
    Funktion NICHT)."""

    return tag.weekday() < 5 and tag not in oesterreichische_feiertage(tag.year)


def naechster_werktag(tag: date) -> date:
    """Liefert `tag` selbst, wenn er bereits ein Werktag ist - sonst den
    nächstfolgenden Werktag (§903 ABGB: ein auf einen Samstag, Sonntag
    oder Feiertag fallendes Fristende rückt auf den nächsten Werktag
    vor). Ein Jahreswechsel innerhalb der Verschiebung ist unproblematisch,
    da `oesterreichische_feiertage` pro tatsächlichem `tag.year` neu
    berechnet wird."""

    while not ist_werktag(tag):
        tag += timedelta(days=1)
    return tag


def fristende_ab_zugang(zugang_am: date, frist_tage: int) -> date:
    """Das TATSÄCHLICHE, bereits auf den nächsten Werktag verschobene
    Fristende einer `frist_tage`-Tage-Frist, die mit dem Zugang eines
    Schreibens an `zugang_am` zu laufen beginnt (§902 ABGB: der
    Zugangstag selbst zählt nicht mit, siehe Moduldoc). Für die in
    diesem Auftrag durchgehend verwendete 14-Tage-Frist ist das
    rechnerische (noch unverschobene) Ende deshalb `zugang_am + 14
    Tage` - NICHT `zugang_am + 13` (Tag 0 = Zugang, Tag 14 = Fristende)."""

    rohe_frist_ende = zugang_am + timedelta(days=frist_tage)
    return naechster_werktag(rohe_frist_ende)


def frist_abgelaufen(*, zugang_am: date, frist_tage: int, heute: date) -> bool:
    """`True`, wenn `heute` das (werktagsverschobene) Fristende aus
    `fristende_ab_zugang` bereits ERREICHT ODER ÜBERSCHRITTEN hat (die
    Frist endet mit ABLAUF des Fristendetages - `heute == Fristende`
    zählt deshalb bereits als abgelaufen, nicht erst der Folgetag)."""

    return heute >= fristende_ab_zugang(zugang_am, frist_tage)
