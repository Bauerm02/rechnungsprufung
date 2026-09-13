"""Deterministischer Textgenerator für ein Erhöhungsschreiben (Auftrag
13.09.) - reine, überprüfbare String-Zusammensetzung aus bereits
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

ZWEI getrennte Textfunktionen (Modellreview 13.09.: "§16(9) gilt im
Schreiben nicht pauschal für MRG-Teil; Geschäftsraum benötigt eigenes
Schreiben"): `erhoehungsschreiben_text_mieweg` zitiert MieWeG + § 16
Abs 9 MRG (nur für den geprüften Wohnungsrechner-Fall);
`erhoehungsschreiben_text_klausel` (Geschäftsraum/generischer
Klausel-Pfad) zitiert AUSSCHLIESSLICH die vertragliche Klausel und
verweist auf die im Einzelfall zu prüfenden gesetzlichen Bestimmungen,
OHNE § 16 Abs 9 pauschal zu behaupten.

Beide Funktionen verlangen zwingend `unveraenderte_komponenten` (alle
sonstigen aktiven Positionen, z. B. BK/HK/Wasser/Strom) und weisen den
NEUEN MONATLICHEN GESAMTBETRAG als Summe aus geänderten UND
unveränderten Positionen aus - "keine irreführende Gesamtsumme nur aus
HMZ ohne unveränderte BK". Netto/USt/Brutto wird für JEDE betroffene
Position einzeln ausgewiesen, korrekt aus `ust_satz_promille` skaliert
(10000 Promille = 10 %, NICHT Division durch 10 - das hätte 1000 %
ausgegeben)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from mietinkasso.domain.money import cents_to_decimal, zerlege_brutto_cent


@dataclass(frozen=True)
class SchreibenKomponente:
    art: str
    bezeichnung: str
    alter_betrag_cent: int
    neuer_betrag_cent: int
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
    geaenderte_komponenten: list[SchreibenKomponente]
    unveraenderte_komponenten: list[SchreibenKomponente]
    erhoehung_cent: int
    massgeblicher_termin: date
    vertrag_beleg_referenz: str
    rechtsprofil_version: int
    jlb_signatur: str
    # Nur für den MieWeG-Pfad genutzt (leer beim Klausel-Pfad zulässig).
    bezugsjahr: int | None = None
    bezugsmonat: int | None = None
    ziel_bewertungsjahr: int | None = None
    jahresschritte: list[SchreibenJahresschritt] = field(default_factory=list)
    vertraglich_zulaessiger_betrag_cent: int | None = None
    vertraglicher_quellenbeleg: str | None = None
    erstellt_am: date = field(default_factory=date.today)


def _eur(cent: int) -> str:
    return f"{cents_to_decimal(cent):,.2f}".replace(",", "§").replace(".", ",").replace("§", ".") + " €"


def _prozent(ust_satz_promille: int) -> str:
    """10000 Promille -> "10,0" (Prozent) - NICHT /10 (das ergäbe 1000%,
    der ursprüngliche Bugfund aus dem Zwischenreview zu 3cec004)."""

    return f"{ust_satz_promille / 1000:.1f}".replace(".", ",")


def _positionen_block(titel: str, komponenten: list[SchreibenKomponente], *, mit_neu: bool) -> list[str]:
    zeilen = [titel]
    for komponente in komponenten:
        netto_alt_cent, ust_alt_cent = zerlege_brutto_cent(komponente.alter_betrag_cent, komponente.ust_satz_promille)
        if mit_neu and komponente.neuer_betrag_cent != komponente.alter_betrag_cent:
            netto_neu_cent, ust_neu_cent = zerlege_brutto_cent(komponente.neuer_betrag_cent, komponente.ust_satz_promille)
            zeilen.append(
                f"  - {komponente.bezeichnung} ({komponente.art}): bisher {_eur(komponente.alter_betrag_cent)} "
                f"brutto (netto {_eur(netto_alt_cent)} + {_prozent(komponente.ust_satz_promille)}% USt "
                f"{_eur(ust_alt_cent)}) -> neu {_eur(komponente.neuer_betrag_cent)} brutto (netto "
                f"{_eur(netto_neu_cent)} + {_prozent(komponente.ust_satz_promille)}% USt {_eur(ust_neu_cent)})"
            )
        else:
            zeilen.append(
                f"  - {komponente.bezeichnung} ({komponente.art}): {_eur(komponente.alter_betrag_cent)} brutto "
                f"(netto {_eur(netto_alt_cent)} + {_prozent(komponente.ust_satz_promille)}% USt {_eur(ust_alt_cent)}) "
                "- unverändert"
            )
    return zeilen


def _gesamtbetraege(kontext: SchreibenKontext) -> tuple[int, int]:
    alt = sum(k.alter_betrag_cent for k in kontext.geaenderte_komponenten) + sum(
        k.alter_betrag_cent for k in kontext.unveraenderte_komponenten
    )
    neu = sum(k.neuer_betrag_cent for k in kontext.geaenderte_komponenten) + sum(
        k.alter_betrag_cent for k in kontext.unveraenderte_komponenten
    )
    return alt, neu


def _kopf_und_positionen(kontext: SchreibenKontext, *, betreff_zusatz: str) -> list[str]:
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
        f"Betrifft: Mietobjekt {kontext.objekt_bezeichnung}, {kontext.einheit_bezeichnung} - {betreff_zusatz}"
    )
    zeilen.append("")
    zeilen.append(f"Sehr geehrte(r) {kontext.mieter_name},")
    zeilen.append("")
    return zeilen


def _abschluss(kontext: SchreibenKontext) -> list[str]:
    alter_gesamt_cent, neuer_gesamt_cent = _gesamtbetraege(kontext)
    zeilen: list[str] = []
    zeilen.append("Betroffene Positionen:")
    zeilen.extend(_positionen_block("", kontext.geaenderte_komponenten, mit_neu=True))
    if kontext.unveraenderte_komponenten:
        zeilen.append("")
        zeilen.append("Unveränderte Positionen (z. B. Betriebs-/Heizkosten, Vorauszahlungen):")
        zeilen.extend(_positionen_block("", kontext.unveraenderte_komponenten, mit_neu=False))
    zeilen.append("")
    zeilen.append(f"Bisheriger monatlicher Gesamtbetrag (brutto, alle Positionen): {_eur(alter_gesamt_cent)}")
    zeilen.append(f"Neuer monatlicher Gesamtbetrag (brutto, alle Positionen): {_eur(neuer_gesamt_cent)}")
    zeilen.append(f"Erhöhungsbetrag (brutto, nur die geänderten Positionen): {_eur(kontext.erhoehung_cent)}")
    zeilen.append("")
    zeilen.append(
        f"Beleg-/Versionsreferenz: Rechtsprofil Version {kontext.rechtsprofil_version}, "
        f"Vertragsbeleg {kontext.vertrag_beleg_referenz}."
    )
    zeilen.append("")
    zeilen.append(kontext.jlb_signatur)
    return zeilen


def erhoehungsschreiben_text_mieweg(kontext: SchreibenKontext) -> str:
    zeilen = _kopf_und_positionen(kontext, betreff_zusatz="Anhebung des Hauptmietzinses nach MieWeG 2026")
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
        f"Rechtsordnung/Anwendbarkeit: {kontext.rechtsordnung} (geprüfter Wohnungsrechner-Fall), "
        f"Bezugszeitraum {kontext.bezugsjahr}-{kontext.bezugsmonat:02d} bis Ziel-Bewertungsjahr "
        f"{kontext.ziel_bewertungsjahr}."
    )
    zeilen.append("")
    if kontext.jahresschritte:
        zeilen.append("Rechenweg (VPI-Jahresdurchschnitte, jeweils amtlich veröffentlicht):")
        for schritt in kontext.jahresschritte:
            zeilen.append(
                f"  {schritt.jahr}: VPI {schritt.vpi_vorjahr} -> {schritt.vpi_jahr}, "
                f"rohe Veränderung {schritt.rohe_veraenderung}, gedämpft {schritt.gedaempfte_veraenderung}, "
                f"angewandt {schritt.angewandte_veraenderung}"
            )
        zeilen.append("")
    if kontext.vertraglich_zulaessiger_betrag_cent is not None:
        zeilen.append(
            f"Vertragliche Obergrenze laut {kontext.vertraglicher_quellenbeleg or '(kein Beleg erfasst)'}: "
            f"{_eur(kontext.vertraglich_zulaessiger_betrag_cent)}"
        )
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
    zeilen.extend(_abschluss(kontext))
    return "\n".join(zeilen)


def erhoehungsschreiben_text_klausel(kontext: SchreibenKontext) -> str:
    """Geschäftsraum-/generischer-Klausel-Pfad - zitiert BEWUSST NICHT
    pauschal § 16 Abs 9 MRG (dessen Anwendbarkeit auf diesen konkreten
    Vertrag wurde in dieser Sitzung nicht unabhängig verifiziert), nur
    die vertragliche Klausel selbst; Zustellform/Fristen bleiben ein
    ausdrücklich zu prüfender Einzelfall."""

    zeilen = _kopf_und_positionen(kontext, betreff_zusatz="Anhebung des Mietzinses laut Vertragsklausel")
    zeilen.append(
        f"hiermit teilen wir Ihnen aufgrund der vertraglich vereinbarten Wertsicherungsklausel "
        f"({kontext.klausel_referenz or 'siehe Vertragsbeleg'}) die Anhebung des Mietzinses für "
        f"{kontext.einheit_bezeichnung} mit."
    )
    zeilen.append("")
    zeilen.append(
        f"Vertragsbeleg: {kontext.vertrag_beleg_referenz}. Rechtsordnung: {kontext.rechtsordnung}. Die genaue "
        "Zustellform und Fristenlage für diesen Vertrag ist im Einzelfall zu prüfen; dieses Schreiben "
        "behauptet insbesondere KEINE pauschale Anwendung von § 16 Abs 9 MRG."
    )
    zeilen.append("")
    zeilen.append(f"Wirksamkeitstermin laut Klauselberechnung: {kontext.massgeblicher_termin.isoformat()}.")
    zeilen.append("")
    zeilen.extend(_abschluss(kontext))
    return "\n".join(zeilen)
