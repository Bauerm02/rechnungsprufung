"""Deterministischer Textgenerator für ein MieWeG-Erhöhungsschreiben
(Auftrag 13.09.) - reine, überprüfbare String-Zusammensetzung aus bereits
berechneten/persistierten Werten, KEIN KI-Aufruf.

"Qualifiziert" heißt hier fachlich-inhaltlich vollständig und korrekt
(Gesellschaft, Adresse/Top/Mieter, Klausel, Basis-/Vergleichswerte,
Rechenweg, Positionen, USt/Gesamtsumme, Termine, Belege/Version) - AUSDRÜCKLICH
KEINE pauschale Pflicht zur qualifizierten elektronischen Signatur (siehe
Fachlicher Nachtrag 13.09.: "'qualifiziert richtig' heißt fachlich
korrektes Schreiben, NICHT pauschal QES-Pflicht"). Die tatsächliche
Zustellform (Einschreiben/Übergabe/E-Mail mit Lesebestätigung/...) wird
separat je Fall im Outbox-Datensatz (`ErhoehungsschreibenTable.zugangsform`)
gepflegt, nicht hier textlich vorweggenommen.

Bei mehr als einer referenzierten Komponente wird BEWUSST KEINE
Verteilungsregel für den neuen Gesamtbetrag auf die einzelnen Positionen
erfunden (siehe OFFENE_PUNKTE.md) - nur der Einzelfall mit GENAU einer
referenzierten Komponente zeigt einen exakten alten/neuen Betrag für
diese eine Position; bei mehreren Positionen wird nur der jeweils alte
Betrag je Position sowie der neue GESAMTbetrag ausgewiesen."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from mietinkasso.domain.money import cents_to_decimal, zerlege_brutto_cent


@dataclass(frozen=True)
class SchreibenKomponente:
    art: str
    bezeichnung: str
    betrag_cent: int
    ust_satz_promille: int


@dataclass(frozen=True)
class SchreibenJahresschritt:
    jahr: int
    vpi_vorjahr: str
    vpi_jahr: str
    rohe_veraenderung: str
    gedaempfte_veraenderung: str
    angewandte_veraenderung: str


@dataclass(frozen=True)
class SchreibenKontext:
    gesellschaft_name: str
    objekt_bezeichnung: str
    objekt_adresse: str | None
    einheit_bezeichnung: str
    mieter_name: str
    mieter_adresse: str | None
    rechtsordnung: str
    klausel_referenz: str | None
    bezugsjahr: int
    bezugsmonat: int
    ziel_bewertungsjahr: int
    jahresschritte: list[SchreibenJahresschritt]
    komponenten: list[SchreibenKomponente]
    aktuell_verrechneter_betrag_cent: int
    massgeblicher_hoechstbetrag_cent: int
    erhoehung_cent: int
    massgeblicher_termin: date
    vertraglich_zulaessiger_betrag_cent: int | None
    vertraglicher_quellenbeleg: str | None
    vertrag_beleg_referenz: str
    rechtsprofil_version: int
    jlb_signatur: str
    erstellt_am: date = field(default_factory=date.today)


def _eur(cent: int) -> str:
    return f"{cents_to_decimal(cent):,.2f}".replace(",", "§").replace(".", ",").replace("§", ".") + " €"


def erhoehungsschreiben_text(kontext: SchreibenKontext) -> str:
    zeilen: list[str] = []
    zeilen.append(kontext.gesellschaft_name)
    if kontext.objekt_adresse:
        zeilen.append(kontext.objekt_adresse)
    zeilen.append("")
    zeilen.append(kontext.mieter_name)
    if kontext.mieter_adresse:
        zeilen.append(kontext.mieter_adresse)
    zeilen.append("")
    zeilen.append(f"Datum: {kontext.erstellt_am.isoformat()}")
    zeilen.append(
        f"Betrifft: Mietobjekt {kontext.objekt_bezeichnung}, {kontext.einheit_bezeichnung} - "
        "Anhebung des Hauptmietzinses nach MieWeG 2026"
    )
    zeilen.append("")
    zeilen.append(f"Sehr geehrte(r) {kontext.mieter_name},")
    zeilen.append("")
    zeilen.append(
        "hiermit teilen wir Ihnen gemäß § 1 MieWeG 2026 in Verbindung mit § 16 Abs 9 MRG die Anhebung "
        f"des wertgesicherten Hauptmietzinses für {kontext.einheit_bezeichnung} mit."
    )
    zeilen.append("")
    zeilen.append(
        f"Vertragliche Grundlage: {kontext.klausel_referenz or '(keine gesonderte Klauselreferenz erfasst)'}; "
        f"Vertragsbeleg: {kontext.vertrag_beleg_referenz}."
    )
    zeilen.append(
        f"Rechtsordnung/Anwendbarkeit: {kontext.rechtsordnung}, Bezugszeitraum "
        f"{kontext.bezugsjahr}-{kontext.bezugsmonat:02d} bis Ziel-Bewertungsjahr {kontext.ziel_bewertungsjahr}."
    )
    zeilen.append("")
    zeilen.append("Rechenweg (VPI-Jahresdurchschnitte, jeweils amtlich veröffentlicht):")
    for schritt in kontext.jahresschritte:
        zeilen.append(
            f"  {schritt.jahr}: VPI {schritt.vpi_vorjahr} -> {schritt.vpi_jahr}, "
            f"rohe Veränderung {schritt.rohe_veraenderung}, gedämpft {schritt.gedaempfte_veraenderung}, "
            f"angewandt {schritt.angewandte_veraenderung}"
        )
    zeilen.append("")
    zeilen.append("Betroffene Positionen (bisheriger Stand, brutto):")
    summe_ust_cent = 0
    for komponente in kontext.komponenten:
        netto_cent, ust_cent = zerlege_brutto_cent(komponente.betrag_cent, komponente.ust_satz_promille)
        summe_ust_cent += ust_cent
        zeilen.append(
            f"  - {komponente.bezeichnung} ({komponente.art}): {_eur(komponente.betrag_cent)} brutto "
            f"(davon {_eur(ust_cent)} USt bei {komponente.ust_satz_promille / 10}% netto {_eur(netto_cent)})"
        )
    if len(kontext.komponenten) == 1:
        einzelkomponente = kontext.komponenten[0]
        zeilen.append("")
        zeilen.append(
            f"Neuer Betrag für {einzelkomponente.bezeichnung}: {_eur(kontext.massgeblicher_hoechstbetrag_cent)} "
            "brutto (bisher " + _eur(einzelkomponente.betrag_cent) + ")."
        )
    zeilen.append("")
    zeilen.append(f"Bisher verrechneter Gesamtbetrag (brutto): {_eur(kontext.aktuell_verrechneter_betrag_cent)}")
    zeilen.append(
        f"Neuer, gesetzlich/vertraglich zulässiger Höchstbetrag (brutto): "
        f"{_eur(kontext.massgeblicher_hoechstbetrag_cent)}"
    )
    if kontext.vertraglich_zulaessiger_betrag_cent is not None:
        zeilen.append(
            f"Vertragliche Obergrenze laut {kontext.vertraglicher_quellenbeleg or '(kein Beleg erfasst)'}: "
            f"{_eur(kontext.vertraglich_zulaessiger_betrag_cent)}"
        )
    zeilen.append(f"Erhöhungsbetrag (brutto): {_eur(kontext.erhoehung_cent)}")
    zeilen.append("")
    zeilen.append(
        f"Wirksamkeitstermin der gesetzlichen/vertraglichen Höchstgrenze: {kontext.massgeblicher_termin.isoformat()} "
        "(1. April des Ziel-Bewertungsjahres)."
    )
    zeilen.append(
        "Ihre tatsächliche Zahlungspflicht für den erhöhten Betrag beginnt gemäß § 16 Abs 9 MRG erst mit dem "
        "nächsten Zinstermin, der mindestens 14 Tage NACH dem nachgewiesenen Zugang dieses Schreibens bei "
        "Ihnen liegt - nicht rückwirkend und nicht automatisch zum oben genannten Wirksamkeitstermin."
    )
    zeilen.append("")
    zeilen.append(
        "Alle sonstigen Vorschreibungspositionen (insbesondere Betriebs-/Heizkosten und Vorauszahlungen) "
        "bleiben von dieser Anhebung UNVERÄNDERT."
    )
    zeilen.append("")
    zeilen.append(
        f"Beleg-/Versionsreferenz: Rechtsprofil Version {kontext.rechtsprofil_version}, "
        f"Vertragsbeleg {kontext.vertrag_beleg_referenz}."
    )
    zeilen.append("")
    zeilen.append(kontext.jlb_signatur)
    return "\n".join(zeilen)
