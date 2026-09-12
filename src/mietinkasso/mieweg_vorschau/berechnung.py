"""Deterministischer Berechnungskern für die MieWeG-2026-Indexvorschau
(Auftrag 12.09., Paket C).

AUSDRÜCKLICH eine BERECHNUNGSVORSCHAU, kein Freigabe-/Buchungsmechanismus:
dieses Modul liest/schreibt keine `VertragTable`/`VertragsKomponenteTable`/
`OPPositionTable`-Zeilen, löst keine Vorschreibung/Mahnung/keinen Versand
aus und ruft KEIN KI-Modell auf - reine, deterministische Arithmetik auf
`Decimal`-Basis nach den in `docs/hausverwaltung/OFFENE_PUNKTE.md`
("Paket C") verlinkten RIS-/Parlaments-Quellen zum 5. MILG/MieWeG 2026.

Bewusst GETRENNT vom bestehenden `index/service.py::EINFACHER_
SCHWELLENVERGLEICH` - dessen Profil ist für diesen Zweck NICHT
rechtlich geeignet (siehe dessen eigene Sperre gegen andere Profile);
dieses Modul ersetzt es nicht, sondern deckt ausschließlich den hier
neu beauftragten MieWeG-Fall ab.

Kernregeln (siehe `docs/hausverwaltung/OFFENE_PUNKTE.md`, Abschnitt
"Paket C", für die vollständige Herleitung/Quellenlage):

- Inflationsrate = (VPI-Jahresdurchschnitt(Jahr) - VPI-Jahresdurchschnitt
  (Jahr-1)) / VPI-Jahresdurchschnitt(Jahr-1) - aus zwei VOLLSTÄNDIGEN
  Jahresdurchschnittswerten, nie aus gerundet veröffentlichten
  Prozentwerten.
- Allgemeine Dämpfung: NUR bei einer Erhöhung über 3 Prozentpunkte wird
  der übersteigende Teil zur Hälfte angerechnet (§1 Abs 2 Z1). KEINE
  symmetrische Dämpfung bei einer Senkung unter -3 % - eine Senkung
  wird immer in voller Höhe durchgereicht (siehe `daempfe()`-Docstring
  für die Korrekturhistorie).
- MRG-Vollanwendungs-Übergangsdeckel (nur wenn `mrg_zinsbeschraenkung`
  gesetzt ist, nur für eine POSITIVE Veränderung): Referenzjahr 2025
  höchstens 1 %, Referenzjahr 2026 höchstens 2 % - ersetzt für diese
  beiden Jahre die allgemeine Dämpfung vollständig; ab Referenzjahr 2027
  gilt wieder die allgemeine Dämpfung.
- Erste Jahresanteiligkeit: volle Monate NACH dem Bezugsmonat, bezogen
  auf das Bezugsjahr, geteilt durch 12 (Dezember → 0/12, Juni → 6/12).
  Dämpfung/Deckel werden auf die RATE angewendet, BEVOR die
  Erstjahresanteiligkeit multipliziert wird. Nur das ERSTE verarbeitete
  Jahr ist anteilig, alle folgenden Jahre zählen voll (12/12).
- Die gesetzliche Höchstgrenze kumuliert über mehrere Jahre auf ihrer
  EIGENEN Basis weiter (Multiplikator auf den ursprünglichen
  `basis_betrag_cent`) - sie wird NICHT jährlich auf einen tatsächlich
  niedrigeren, tatsächlich verrechneten Betrag zurückgesetzt.
- Rundung je gesetzlicher Entgeltanpassung: ein halber Cent oder
  weniger wird abgerundet, mehr als ein halber Cent aufgerundet
  (`ROUND_HALF_DOWN` auf Cent-Ebene, ausschließlich mit `Decimal`).
- Fehlt ein benötigter VPI-Jahresdurchschnittswert, wird NICHTS
  erfunden: die Berechnung bricht an dieser Stelle ab und meldet das
  fehlende Jahr."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_DOWN, Decimal
from typing import Mapping

_DAEMPFUNGS_SCHWELLE = Decimal("0.03")
_UEBERGANGSDECKEL = {2025: Decimal("0.01"), 2026: Decimal("0.02")}


def daempfe(rate: Decimal) -> Decimal:
    """Allgemeine MieWeG-Dämpfung: NUR bei einer ERHÖHUNG über 3
    Prozentpunkte wird der übersteigende Teil nur zur Hälfte angerechnet.

    KORRIGIERT (Folgeauftrag Markus, unabhängige Prüfung gegen die
    Primärquelle RIS BGBl. I Nr. 114/2025, §1 Abs 2 Z1 - in dieser
    Sitzung selbst nicht abrufbar, `ris.bka.gv.at` ist über die
    Netzwerk-Egress-Policy blockiert, daher hier nicht nachvollzogen,
    sondern auf ausdrücklichen, mit Fundstelle belegten Hinweis
    übernommen): §1 Abs 2 Z1 dämpft AUSSCHLIESSLICH eine Veränderung, die
    3 % ÜBERSTEIGT - das ist im Gesetzestext keine symmetrische Regel für
    Senkungen. Eine vorherige Fassung dieser Funktion halbierte den
    übersteigenden Teil AUCH bei einer Senkung unter -3 % ("symmetrisch
    auch bei Deflation") - dafür gab es keine Rechtsgrundlage; eine
    Senkung wird jetzt in voller Höhe durchgereicht, ohne jede Dämpfung.
    Nur der Sonderfall value == genau der Schwelle oder darunter bleibt
    unverändert (kein Rateversuch, keine unbegründete Kappung einer
    Senkung auf 0)."""

    if rate > _DAEMPFUNGS_SCHWELLE:
        return _DAEMPFUNGS_SCHWELLE + (rate - _DAEMPFUNGS_SCHWELLE) / 2
    return rate


def runde_halbcent(cent_wert: Decimal) -> Decimal:
    """Rundung je gesetzlicher Entgeltanpassung: ein halber Cent oder
    weniger wird abgerundet, mehr als ein halber Cent aufgerundet.
    `cent_wert` ist bereits in CENT ausgedrückt (kann noch
    Sub-Cent-Nachkommastellen tragen); das Ergebnis ist eine ganze
    Cent-Zahl. `ROUND_HALF_DOWN` rundet exakte Hälften Richtung Null,
    was für positive Cent-Beträge exakt "halber Cent -> ab" bedeutet."""

    return cent_wert.quantize(Decimal("1"), rounding=ROUND_HALF_DOWN)


@dataclass(frozen=True)
class JahresSchritt:
    jahr: int
    vpi_vorjahr: Decimal
    vpi_jahr: Decimal
    rohe_veraenderung: Decimal
    gedaempfte_veraenderung: Decimal
    anteil: Decimal
    angewandte_veraenderung: Decimal
    kumulierter_multiplikator: Decimal
    uebergangsdeckel_angewandt: bool = False


@dataclass(frozen=True)
class GesetzlicheHoechstgrenzeErgebnis:
    ziel_bewertungsjahr: int
    fruehester_termin: date
    jahresschritte: list[JahresSchritt] = field(default_factory=list)
    fehlende_jahre: list[int] = field(default_factory=list)
    hoechstbetrag_cent: int | None = None
    kumulierter_multiplikator: Decimal | None = None

    @property
    def vollstaendig(self) -> bool:
        return not self.fehlende_jahre


def berechne_gesetzliche_hoechstgrenze(
    *,
    mrg_zinsbeschraenkung: bool,
    erster_bezug_jahr: int,
    erster_bezug_monat: int,
    ziel_bewertungsjahr: int,
    vpi_jahresdurchschnitte: Mapping[int, Decimal],
    basis_betrag_cent: int,
) -> GesetzlicheHoechstgrenzeErgebnis:
    """Kumuliert die gesetzliche MieWeG-Höchstgrenze Jahr für Jahr von
    `erster_bezug_jahr` bis (ausschließlich) `ziel_bewertungsjahr` -
    Bewertung jeweils zum 1. April von `ziel_bewertungsjahr`
    (rein rechtlich-statutarisch; siehe Modul-Docstring zur getrennten
    Frage der tatsächlichen Fälligkeit/Mitteilungsfrist).

    `erster_bezug_jahr`/`erster_bezug_monat`: für einen NEUEN Vertrag der
    Abschlussmonat; für einen ALTVERTRAG stattdessen der Bezugsmonat des
    tatsächlich zuletzt verwendeten Indexwertes (ggf. Dezember, wenn diese
    bisherige Basis selbst ein Jahresdurchschnitt war) - diese
    Unterscheidung trifft die aufrufende Service-Schicht, nicht diese
    reine Funktion.

    Fehlt für ein benötigtes Jahr ein VPI-Jahresdurchschnittswert, bricht
    die Kumulierung an dieser Stelle ab (kein erfundener Wert) - das
    Ergebnis listet das fehlende Jahr und liefert `hoechstbetrag_cent=None`."""

    if not (1 <= erster_bezug_monat <= 12):
        raise ValueError(f"erster_bezug_monat muss 1-12 sein, war {erster_bezug_monat}.")
    if ziel_bewertungsjahr <= erster_bezug_jahr:
        raise ValueError(
            f"ziel_bewertungsjahr ({ziel_bewertungsjahr}) muss nach erster_bezug_jahr "
            f"({erster_bezug_jahr}) liegen."
        )
    if basis_betrag_cent <= 0:
        raise ValueError(f"basis_betrag_cent muss positiv sein, war {basis_betrag_cent}.")

    fruehester_termin = date(ziel_bewertungsjahr, 4, 1)
    schritte: list[JahresSchritt] = []
    kumulierter_multiplikator = Decimal("1")
    jahr = erster_bezug_jahr
    ist_erstes_jahr = True

    while jahr < ziel_bewertungsjahr:
        vorjahr = jahr - 1
        if jahr not in vpi_jahresdurchschnitte or vorjahr not in vpi_jahresdurchschnitte:
            return GesetzlicheHoechstgrenzeErgebnis(
                ziel_bewertungsjahr=ziel_bewertungsjahr,
                fruehester_termin=fruehester_termin,
                jahresschritte=schritte,
                fehlende_jahre=[jahr],
            )

        vpi_jahr = vpi_jahresdurchschnitte[jahr]
        vpi_vorjahr = vpi_jahresdurchschnitte[vorjahr]
        if vpi_vorjahr == 0:
            raise ValueError(f"VPI-Jahresdurchschnitt {vorjahr} ist 0 - Veränderung nicht berechenbar.")
        rohe_veraenderung = (vpi_jahr - vpi_vorjahr) / vpi_vorjahr

        uebergangsdeckel_angewandt = False
        if mrg_zinsbeschraenkung and jahr in _UEBERGANGSDECKEL and rohe_veraenderung > 0:
            deckel = _UEBERGANGSDECKEL[jahr]
            gedaempfte_veraenderung = min(rohe_veraenderung, deckel)
            uebergangsdeckel_angewandt = True
        else:
            gedaempfte_veraenderung = daempfe(rohe_veraenderung)

        anteil = Decimal(12 - erster_bezug_monat) / Decimal(12) if ist_erstes_jahr else Decimal(1)
        angewandte_veraenderung = gedaempfte_veraenderung * anteil
        kumulierter_multiplikator *= Decimal(1) + angewandte_veraenderung

        schritte.append(
            JahresSchritt(
                jahr=jahr,
                vpi_vorjahr=vpi_vorjahr,
                vpi_jahr=vpi_jahr,
                rohe_veraenderung=rohe_veraenderung,
                gedaempfte_veraenderung=gedaempfte_veraenderung,
                anteil=anteil,
                angewandte_veraenderung=angewandte_veraenderung,
                kumulierter_multiplikator=kumulierter_multiplikator,
                uebergangsdeckel_angewandt=uebergangsdeckel_angewandt,
            )
        )
        ist_erstes_jahr = False
        jahr += 1

    hoechstbetrag_cent = int(runde_halbcent(Decimal(basis_betrag_cent) * kumulierter_multiplikator))
    return GesetzlicheHoechstgrenzeErgebnis(
        ziel_bewertungsjahr=ziel_bewertungsjahr,
        fruehester_termin=fruehester_termin,
        jahresschritte=schritte,
        fehlende_jahre=[],
        hoechstbetrag_cent=hoechstbetrag_cent,
        kumulierter_multiplikator=kumulierter_multiplikator,
    )
