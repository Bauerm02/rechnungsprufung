"""Generischer CSV-Import für amtliche VPI-Monatswerte (Auftrag 13.09.,
Betriebspräzisierung: "VPI-Werte nicht auf den vorhandenen Stand
einfrieren... amtlicher Statistik-Austria-Import mit Serien-/Monats-
und Jahreswerten, Publikations-/Finalitätsbeleg und sicherem
Blockieren fehlender oder vorläufiger Werte").

WICHTIGE EHRLICHE EINSCHRÄNKUNG: das exakte Spaltenlayout der realen
Statistik-Austria-OGD-Dateien (z. B. data.statistik.gv.at/data/
OGD_vpi20c18_VPI_2020COICOP18_1.csv) wurde in dieser Sitzung NICHT
gegen eine echte heruntergeladene Datei verifiziert - Claude hat
keinen Server-/Internetzugriff (siehe AGENTS.md). Der Importer nimmt
deshalb eine EXPLIZITE, konfigurierbare Spaltenzuordnung entgegen
(`VpiSpaltenzuordnung`) statt ein geratenes Schema fix zu verdrahten -
Codex/der Betreiber passt die Zuordnung an die tatsächliche
Dateistruktur an, ohne Code ändern zu müssen. Siehe OFFENE_PUNKTE.md.

"Ein Fehler => gesamter Lauf unverändert": eine nicht parsebare Zeile
lehnt die GESAMTE Datei ab, es wird nie ein Teilimport committet."""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

from mietinkasso.indexautomatik.repository import VpiRepository


class VpiImportFehlerError(Exception):
    """Die CSV-Datei enthält eine nicht parsebare/unplausible Zeile -
    der gesamte Import wird verworfen, nichts wird geschrieben."""


@dataclass(frozen=True)
class VpiSpaltenzuordnung:
    jahr_spalte: str
    monat_spalte: str
    wert_spalte: str
    status_spalte: str | None = None
    #: Werte in `status_spalte`, die als ENDGUELTIG gelten. OHNE
    #: konfigurierte `status_spalte` wird JEDE Zeile sicherheitshalber
    #: als VORLAEUFIG behandelt (kein blindes "alles ist final") - ein
    #: bewusster, expliziter Konfigurationsschritt ist Pflicht, um
    #: Werte als ENDGUELTIG in die Jahresdurchschnittsbildung
    #: einfließen zu lassen.
    endgueltig_werte: frozenset[str] = field(default_factory=lambda: frozenset({"ENDGUELTIG", "FINAL", "final"}))
    delimiter: str = ";"
    dezimaltrennzeichen: str = ","


@dataclass(frozen=True)
class VpiImportErgebnis:
    reihe: str
    importierte_zeilen: int
    endgueltige_zeilen: int
    vorlaeufige_zeilen: int


def importiere_monatswerte_csv(
    pfad: str,
    *,
    reihe: str,
    spalten: VpiSpaltenzuordnung,
    repository: VpiRepository,
    importiert_von: str,
    abgerufen_am: datetime,
) -> VpiImportErgebnis:
    """`abgerufen_am` MUSS vom Aufrufer explizit angegeben werden (WANN
    die Datei tatsächlich von Statistik Austria abgerufen wurde) -
    "Kalenderdatum allein garantiert keine tatsächliche
    Veröffentlichung" (Betriebspräzisierung 13.09.); ein Rateversuch
    über die Systemuhr zum Importzeitpunkt würde das verschleiern,
    falls die Datei erst später importiert wird."""

    try:
        with open(pfad, "rb") as roh_datei:
            quelle_hash = hashlib.sha256(roh_datei.read()).hexdigest()
    except FileNotFoundError as exc:
        raise VpiImportFehlerError(f"Datei nicht gefunden: {pfad}") from exc

    zeilen: list[dict] = []
    try:
        with open(pfad, newline="", encoding="utf-8-sig") as datei:
            reader = csv.DictReader(datei, delimiter=spalten.delimiter)
            for zeilennummer, zeile in enumerate(reader, start=2):
                try:
                    jahr = int(zeile[spalten.jahr_spalte])
                    monat = int(zeile[spalten.monat_spalte])
                    wert_text = zeile[spalten.wert_spalte].strip().replace(spalten.dezimaltrennzeichen, ".")
                    wert = Decimal(wert_text)
                except (KeyError, ValueError, InvalidOperation) as exc:
                    raise VpiImportFehlerError(
                        f"{pfad}: Zeile {zeilennummer} nicht parsebar ({exc}) - gesamter Import verworfen, "
                        "keine Zeile wird übernommen."
                    ) from exc
                if not (1 <= monat <= 12):
                    raise VpiImportFehlerError(f"{pfad}: Zeile {zeilennummer} hat ungültigen Monat {monat}.")
                if spalten.status_spalte is not None:
                    status_wert = (zeile.get(spalten.status_spalte) or "").strip()
                    finalitaet = "ENDGUELTIG" if status_wert in spalten.endgueltig_werte else "VORLAEUFIG"
                else:
                    finalitaet = "VORLAEUFIG"
                zeilen.append(
                    {
                        "jahr": jahr,
                        "monat": monat,
                        "wert": wert,
                        "finalitaet": finalitaet,
                        "quelle_zeile": zeilennummer,
                    }
                )
    except FileNotFoundError as exc:
        raise VpiImportFehlerError(f"Datei nicht gefunden: {pfad}") from exc

    if not zeilen:
        raise VpiImportFehlerError(f"{pfad} enthält keine verwertbaren Zeilen - Import verworfen.")

    for eintrag in zeilen:
        repository.monatswert_erfassen(
            reihe=reihe,
            jahr=eintrag["jahr"],
            monat=eintrag["monat"],
            wert=eintrag["wert"],
            finalitaet=eintrag["finalitaet"],
            quelle_datei=pfad,
            quelle_zeile=eintrag["quelle_zeile"],
            quelle_hash=quelle_hash,
            abgerufen_am=abgerufen_am,
            importiert_von=importiert_von,
        )

    endgueltig = sum(1 for z in zeilen if z["finalitaet"] == "ENDGUELTIG")
    return VpiImportErgebnis(
        reihe=reihe,
        importierte_zeilen=len(zeilen),
        endgueltige_zeilen=endgueltig,
        vorlaeufige_zeilen=len(zeilen) - endgueltig,
    )
