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
- Rundung: GENAU EINMAL je Funktionsaufruf, auf den EINEN tatsächlich
  wirksam werdenden (kumulierten) Betrag - ein halber Cent oder weniger
  wird abgerundet, mehr als ein halber Cent aufgerundet
  (`ROUND_HALF_DOWN` auf Cent-Ebene, ausschließlich mit `Decimal`).
  KLARSTELLUNG (Codex-Rückprüfung, unabhängige Prüfung Auftrag Markus
  13.09.: "derzeit Doku sagt je Anpassung, Implementation rundet
  anscheinend nur Endprodukt"): diese Funktion berechnet und rundet
  GENAU EINE tatsächliche Anpassung - auch wenn ihre interne Schleife
  mehrere übersprungene Kalenderjahre (Nachholung/Altvertrag ohne
  zwischenzeitliche Vorschreibung) durchläuft, gibt es dafür KEINE
  mehreren, separat gerundeten Zwischenbeträge, weil in diesen
  übersprungenen Jahren nie tatsächlich vorgeschrieben wurde - es gibt
  nichts, was dort real auf den Cent zu runden wäre (siehe Tests
  `test_altvertrag_mehrjaehrige_historie_kumuliert_je_jahr_getrennt`,
  `test_kumulierung_bleibt_unabhaengig_von_zwischenzeitlich_
  niedrigerer_miete`: NUR die Dämpfung/der Deckel wird dort "je Jahr
  getrennt" verwendet, NICHT die Rundung). GEFAHR bei Fremdverwendung:
  eine RÜCKWIRKENDE REKONSTRUKTION, die vorgibt, eine Kette TATSÄCHLICH
  Jahr für Jahr umgesetzter (und damit real gerundeter) Anpassungen
  nachzubilden, darf diese Funktion NICHT mit einer mehrjährigen Spanne
  in einem einzigen Aufruf verwenden - das ergibt ein anderes Ergebnis
  als die Kette echter Einzeljahres-Rundungen (Beispiel: Basis 100,00 €,
  Jahr 1 +0,125%, Jahr 2 +0,5% - Einzelrundung je Jahr ergibt 100,62 €
  [Jahr1: 100,125€ exakter Halb-Cent-Fall -> ab auf 100,12€; Jahr2:
  100,12€×1,005=100,6206€ -> 100,62€], diese Funktion in einem Aufruf
  ergibt 100,63 € [1,00125×1,005=1,00625625 -> 100,625625€ -> 100,63€] -
  1 Cent Differenz bei nur zwei Jahren und homöopathischen Sätzen, bei
  längeren Ketten ggf. mehr). Für eine solche Rekonstruktion muss diese
  Funktion (oder ein äquivalenter Einzeljahresschritt) JE JAHR EINZELN
  aufgerufen werden, mit dem gerundeten Cent-Ergebnis von Jahr N als
  `basis_betrag_cent` für Jahr N+1 - das ist eine bewusste Design-
  entscheidung dieser Funktion, keine unentdeckte Ungenauigkeit, und
  wird hier nicht geändert (Nachholung/Altvertrag-Verhalten ist
  bestehend getestet und produktiv genutzt).
- Fehlt ein benötigter VPI-Jahresdurchschnittswert, wird NICHTS
  erfunden: die Berechnung bricht an dieser Stelle ab und meldet das
  fehlende Jahr."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_DOWN, Decimal
from typing import Mapping

from mietinkasso.domain.enums import Rechtsordnung

_DAEMPFUNGS_SCHWELLE = Decimal("0.03")
_UEBERGANGSDECKEL = {2025: Decimal("0.01"), 2026: Decimal("0.02")}

#: §49k Abs 4 MRG (5. MILG/MieWeG-Reform 2025): die neue Mindestbefristung
#: gilt nur für einen Abschluss/eine vertragliche oder gesetzliche
#: Erneuerung NACH diesem Datum (exklusiv) - am oder vor dem Stichtag
#: bleibt eine bereits VEREINBARTE Befristung dem ALTEN Recht
#: unterworfen. Kein weiterer, insbesondere kein zusätzlicher
#: Juni-Stichtag existiert in der Primärquelle.
_STICHTAG_MINDESTBEFRISTUNG_NEU = date(2025, 12, 31)

#: Rechtsordnungen, für die MRG-WOHNUNGSREGELN (hier: §49k-Mindest-
#: befristung) überhaupt in Frage kommen - dieselbe Abgrenzung wie
#: `mieweg_vorschau/service.py::_WOHNUNGSRECHNER_RECHTSORDNUNGEN`/
#: `indexautomatik/service.py::_WOHNUNGSRECHNER_RECHTSORDNUNGEN` (hier
#: bewusst nicht importiert, um keine Modulabhängigkeit in die andere
#: Richtung zu erzeugen - dieselben zwei Enum-Werte, siehe dort).
_MRG_WOHNUNGSFAEHIGE_RECHTSORDNUNGEN = {
    Rechtsordnung.OESTERREICH_MRG_VOLL.value,
    Rechtsordnung.OESTERREICH_MRG_TEIL.value,
}


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
    """Rundung EINER tatsächlich wirksam werdenden Entgeltanpassung: ein
    halber Cent oder weniger wird abgerundet, mehr als ein halber Cent
    aufgerundet. `cent_wert` ist bereits in CENT ausgedrückt (kann noch
    Sub-Cent-Nachkommastellen tragen); das Ergebnis ist eine ganze
    Cent-Zahl. `ROUND_HALF_DOWN` rundet exakte Hälften Richtung Null,
    was für positive Cent-Beträge exakt "halber Cent -> ab" bedeutet.
    Für eine Kette MEHRERER, TATSÄCHLICH separat umgesetzter
    Jahresanpassungen (z. B. eine Rekonstruktion) muss diese Funktion je
    Jahr EINZELN aufgerufen werden (siehe Modul-Docstring, Abschnitt
    "Rundung") - ein einziger Aufruf über mehrere Jahre rundet nur EINEN
    Gesamtbetrag, nicht mehrere Zwischenbeträge."""

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


@dataclass(frozen=True)
class MindestbefristungErgebnis:
    anwendbar: bool
    mindestdauer_jahre: int | None
    gruende: list[str] = field(default_factory=list)


def pruefe_mindestbefristung_wohnung(
    *,
    rechtsordnung: str,
    ist_wohnungsnutzung: bool | None,
    abschluss_oder_erneuerungsdatum: date,
    ist_unternehmerischer_vermieter: bool,
) -> MindestbefristungErgebnis:
    """§49k Abs 4 MRG (5. MILG/MieWeG-Reform 2025, RIS BGBl. I Nr.
    114/2025): neue Mindestbefristung für WOHNUNGS-Mietverträge im
    MRG-Voll-/Teilanwendungsbereich - 5 Jahre bei unternehmerischer
    Vermietung, sonst 3 Jahre (unverändert gegenüber altem Recht).

    Reine, seiteneffektfreie Prüf-/Klassifikationsfunktion (Auftrag
    Markus, unabhängige Rückprüfung 13.09.: "Fünfjahres-/Gewerbe-
    Abgrenzung als reine Helper-Prüfung ... ohne echte Vertragsdaten
    anzufassen") - liest/schreibt KEINE Vertragsdaten, ändert NIE eine
    bestehende `VertragTable.gueltig_bis`. `ist_unternehmerischer_
    vermieter` ist bewusst ein Eingabeparameter: OB ein konkreter
    Vermieter im Einzelfall "unternehmerisch" iSd §49k Abs 4 MRG
    vermietet, ist eine vertragsindividuelle Tatsachen-/Rechtsfrage, die
    diese Funktion NICHT selbst herleitet (siehe AGENTS.md: "Claude baut
    keine eigenen Rechtsregeln").

    Bestätigte Fachregel (Auftrag Markus): NIEMALS für Geschäftsräume -
    `anwendbar=False`, sobald `rechtsordnung` außerhalb MRG-Voll/-Teil
    liegt ODER `ist_wohnungsnutzung` nicht `True` ist (Geschäftsraum kann
    unbefristet sein). `ist_wohnungsnutzung=None` (ungeprüft) blockiert
    ebenso wie `False` - kein Rateversuch zur Nutzungsart.

    Zeitliche Anwendbarkeit: NUR bei Abschluss ODER vertraglicher/
    gesetzlicher ERNEUERUNG NACH dem 31.12.2025 (exklusiv) - eine an
    diesem Stichtag oder davor bereits VEREINBARTE Befristung bleibt dem
    ALTEN Recht unterworfen. KEINE rückwirkende Verlängerung bereits
    bestehender Wohnungsmietverträge, KEIN zusätzlicher (insbesondere
    kein Juni-)Stichtag - die Primärquelle kennt nur den 31.12.2025."""

    gruende: list[str] = []
    if rechtsordnung not in _MRG_WOHNUNGSFAEHIGE_RECHTSORDNUNGEN:
        gruende.append(
            f"§49k Abs 4 MRG gilt nur im MRG-Voll-/Teilanwendungsbereich, nicht für Rechtsordnung "
            f"'{rechtsordnung}' - Geschäftsräume/Gewerbe können unbefristet sein."
        )
        return MindestbefristungErgebnis(anwendbar=False, mindestdauer_jahre=None, gruende=gruende)
    if ist_wohnungsnutzung is not True:
        gruende.append(
            "Wohnungsnutzung ist nicht bestätigt (ungeprüft oder Geschäftsraum) - §49k Abs 4 MRG betrifft "
            "ausschließlich Wohnungs-Hauptmiete, keine automatische Annahme."
        )
        return MindestbefristungErgebnis(anwendbar=False, mindestdauer_jahre=None, gruende=gruende)
    if abschluss_oder_erneuerungsdatum <= _STICHTAG_MINDESTBEFRISTUNG_NEU:
        gruende.append(
            f"Abschluss/Erneuerung am {abschluss_oder_erneuerungsdatum.isoformat()} liegt nicht NACH dem "
            f"{_STICHTAG_MINDESTBEFRISTUNG_NEU.isoformat()} - eine bereits vereinbarte Befristung bleibt dem "
            "alten Recht unterworfen, keine rückwirkende Verlängerung."
        )
        return MindestbefristungErgebnis(anwendbar=False, mindestdauer_jahre=None, gruende=gruende)
    return MindestbefristungErgebnis(
        anwendbar=True, mindestdauer_jahre=5 if ist_unternehmerischer_vermieter else 3, gruende=[]
    )
