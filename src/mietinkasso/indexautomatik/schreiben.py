"""Gemeinsame Daten und Betragsdarstellung für die versionierte JLB-Vorlage in begehren.py."""

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
    gesetzliche_basis_cent: int | None = None
    gesetzliche_grenze_cent: int | None = None


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
    zeilen.append(kontext.jlb_signatur)
    return zeilen
