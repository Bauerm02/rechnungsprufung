"""Nativer Parser für die amtlichen Statistik-Austria-VPI-OGD-Dateien
(Auftrag 13.09., Betriebspräzisierung + Zwischenreview 34c6fdd: "die
VIER echten öffentlichen OGD-Dateien heruntergeladen... Jahr/Monat sind
eine kombinierte Periodenspalte, Statusspalte fehlt, Gesamtindex muss
gefiltert werden").

VERIFIZIERTES Schema (vom Betreiber gegen echte Dateien geprüft):
Semikolon-getrennt, UTF-8-BOM. Periodenspalte `C-VPIZR-0` mit Werten
`VPIZR-YYYYMM` (Monat) oder `VPIZR-YYYY` (amtlicher Jahresdurchschnitt).
Wertspalte `F-VPIMZBM`, Dezimalkomma. Dimension/Gesamtindex-Filter:
VPI2020/VPI2015 -> Spalte `C-VPICOICOP18_5-0` == `VPICOICOP18-0`;
VPI2000/VPI1996 -> Spalte `C-VPI1-0` == `VPI-0`. Alle anderen Zeilen
(Teilindizes) werden übersprungen, nicht importiert.

Finalität wird NICHT aus einer Statusspalte gelesen (die gibt es
nicht), sondern aus der amtlichen Publikationsregel abgeleitet: der
JÜNGSTE in der Datei enthaltene Monat ist stets VORLAEUFIG (wird erst
mit dem nächsten Monatsbericht final), alle früheren Monate gelten als
ENDGUELTIG. Ein in der Datei enthaltener Jahresdurchschnitt (`VPIZR-
YYYY`) ist der AMTLICH veröffentlichte Wert und wird direkt als
`VpiJahreswertTable`-Override übernommen (Vorrang vor einem selbst
gemittelten, ungerundeten Monatsdurchschnitt).

Vor jedem Schreiben wird die GESAMTE Datei validiert (endliche positive
Werte, plausible Perioden, keine doppelten Perioden) und in EINER
Transaktion geschrieben (`VpiRepository.batch_erfassen`) - ein Fehler
irgendwo verwirft den gesamten Import, kein Teilimport (Zwischenreview
34c6fdd: die vorherige Zeile-für-Zeile-Commit-Schleife war trotz
gegenteiligem Docstring nicht atomar).

Herkunftsnachweis: `quelle_hash` (SHA256 der Rohdatei), `quelle_url`
und die vom Aufrufer explizit anzugebende `abgerufen_am`-Zeit werden je
Zeile gespeichert - "Kalenderdatum allein garantiert keine tatsächliche
Veröffentlichung"."""

from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from mietinkasso.indexautomatik.repository import VpiRepository

PERIODENSPALTE = "C-VPIZR-0"
WERTSPALTE = "F-VPIMZBM"

#: Für VPI15C18/VPI00/VPI96 ist NUR das Namensmuster aus der einen
#: verbal bestätigten URL (VPI20C18) übernommen - NICHT selbst gegen
#: eine echte Datei verifiziert. Codex muss diese drei URLs vor
#: produktivem Einsatz gegenprüfen (siehe OFFENE_PUNKTE.md).
_REIHEN_SCHEMA = {
    "VPI20C18": {
        "dimension_spalte": "C-VPICOICOP18_5-0",
        "gesamtindex_wert": "VPICOICOP18-0",
        "url": "https://data.statistik.gv.at/data/OGD_vpi20c18_VPI_2020COICOP18_1.csv",
    },
    "VPI15C18": {
        "dimension_spalte": "C-VPICOICOP18_5-0",
        "gesamtindex_wert": "VPICOICOP18-0",
        "url": "https://data.statistik.gv.at/data/OGD_vpi15c18_VPI_2015COICOP18_1.csv",
    },
    "VPI00": {
        "dimension_spalte": "C-VPI1-0",
        "gesamtindex_wert": "VPI-0",
        "url": "https://data.statistik.gv.at/data/OGD_vpi00_VPI_2000_1.csv",
    },
    "VPI96": {
        "dimension_spalte": "C-VPI1-0",
        "gesamtindex_wert": "VPI-0",
        "url": "https://data.statistik.gv.at/data/OGD_vpi96_VPI_1996_1.csv",
    },
}

_MONATS_PATTERN = re.compile(r"^VPIZR-(\d{4})(\d{2})$")
_JAHRES_PATTERN = re.compile(r"^VPIZR-(\d{4})$")


class VpiImportFehlerError(Exception):
    """Die Datei entspricht nicht dem erwarteten Schema oder enthält
    eine unplausible/doppelte Zeile - der gesamte Import wird
    verworfen, nichts wird geschrieben."""


@dataclass(frozen=True)
class VpiImportErgebnis:
    reihe: str
    monatszeilen: int
    endgueltige_monatszeilen: int
    vorlaeufige_monatszeilen: int
    jahreszeilen: int


def _parse_periode(pfad: str, zeilennummer: int, roh_wert: str) -> tuple[str, int, int | None]:
    monat_treffer = _MONATS_PATTERN.match(roh_wert)
    if monat_treffer:
        jahr, monat = int(monat_treffer.group(1)), int(monat_treffer.group(2))
        if not (1 <= monat <= 12):
            raise VpiImportFehlerError(f"{pfad}: Zeile {zeilennummer} hat ungültigen Monat in Periode '{roh_wert}'.")
        return "MONAT", jahr, monat
    jahr_treffer = _JAHRES_PATTERN.match(roh_wert)
    if jahr_treffer:
        return "JAHR", int(jahr_treffer.group(1)), None
    raise VpiImportFehlerError(f"{pfad}: Zeile {zeilennummer} hat unbekanntes Periodenformat '{roh_wert}'.")


def _parse_wert(pfad: str, zeilennummer: int, roh_wert: str) -> Decimal:
    normalisiert = (roh_wert or "").strip().replace(".", "").replace(",", ".")
    try:
        wert = Decimal(normalisiert)
    except InvalidOperation as exc:
        raise VpiImportFehlerError(f"{pfad}: Zeile {zeilennummer} hat keinen gültigen Wert '{roh_wert}'.") from exc
    if not wert.is_finite() or wert <= 0:
        raise VpiImportFehlerError(f"{pfad}: Zeile {zeilennummer} hat unplausiblen Wert {wert} (nicht endlich/positiv).")
    return wert


def importiere_ogd_csv(
    pfad: str,
    *,
    reihe: str,
    repository: VpiRepository,
    importiert_von: str,
    abgerufen_am: datetime,
    quelle_url: str | None = None,
) -> VpiImportErgebnis:
    schema = _REIHEN_SCHEMA.get(reihe)
    if schema is None:
        raise VpiImportFehlerError(f"Unbekannte VPI-Reihe '{reihe}' - unterstützt: {sorted(_REIHEN_SCHEMA)}.")
    dimension_spalte = schema["dimension_spalte"]
    gesamtindex_wert = schema["gesamtindex_wert"]

    try:
        with open(pfad, "rb") as roh_datei:
            inhalt = roh_datei.read()
    except FileNotFoundError as exc:
        raise VpiImportFehlerError(f"Datei nicht gefunden: {pfad}") from exc
    quelle_hash = hashlib.sha256(inhalt).hexdigest()

    monats_kandidaten: dict[tuple[int, int], tuple[Decimal, int]] = {}
    jahres_kandidaten: dict[int, tuple[Decimal, int]] = {}
    gesehene_perioden: set[str] = set()

    with open(pfad, newline="", encoding="utf-8-sig") as datei:
        reader = csv.DictReader(datei, delimiter=";")
        pflichtspalten = {PERIODENSPALTE, WERTSPALTE, dimension_spalte}
        if not pflichtspalten.issubset(set(reader.fieldnames or [])):
            raise VpiImportFehlerError(
                f"{pfad}: erwartete Spalten {sorted(pflichtspalten)} nicht vollständig gefunden "
                f"(vorhanden: {reader.fieldnames}) - Datei entspricht nicht dem erwarteten OGD-Schema."
            )
        for zeilennummer, zeile in enumerate(reader, start=2):
            if zeile.get(dimension_spalte) != gesamtindex_wert:
                continue  # Teilindex - nicht der Gesamtindex, wird nicht importiert
            periode_roh = (zeile.get(PERIODENSPALTE) or "").strip()
            art, jahr, monat = _parse_periode(pfad, zeilennummer, periode_roh)
            wert = _parse_wert(pfad, zeilennummer, zeile.get(WERTSPALTE, ""))

            schluessel = f"{art}:{jahr}:{monat}"
            if schluessel in gesehene_perioden:
                raise VpiImportFehlerError(f"{pfad}: Periode '{periode_roh}' kommt mehrfach vor (Zeile {zeilennummer}).")
            gesehene_perioden.add(schluessel)

            if art == "MONAT":
                monats_kandidaten[(jahr, monat)] = (wert, zeilennummer)
            else:
                jahres_kandidaten[jahr] = (wert, zeilennummer)

    if not monats_kandidaten and not jahres_kandidaten:
        raise VpiImportFehlerError(
            f"{pfad} enthält keine Gesamtindex-Zeilen für Dimension {gesamtindex_wert} - Import verworfen."
        )

    letzte_periode = max(monats_kandidaten) if monats_kandidaten else None
    tatsaechliche_url = quelle_url or schema["url"]

    monatszeilen = [
        {
            "reihe": reihe,
            "jahr": jahr,
            "monat": monat,
            "wert": wert,
            "finalitaet": "VORLAEUFIG" if (jahr, monat) == letzte_periode else "ENDGUELTIG",
            "quelle_datei": pfad,
            "quelle_zeile": zeilennummer,
            "quelle_hash": quelle_hash,
            "abgerufen_am": abgerufen_am,
            "importiert_von": importiert_von,
        }
        for (jahr, monat), (wert, zeilennummer) in monats_kandidaten.items()
    ]
    # Ein Jahresdurchschnitt ist erst ENDGUELTIG, wenn dieselbe Datei
    # auch den Jänner des FOLGEJahres enthält ("Jahresdurchschnitt
    # endgültig mit Jänner-Publikation im Februar") - unabhängiger
    # Review (fd8c2b2-Folgereview): eine Januar-Publikation mit
    # Dezember+Jahreswert desselben Jahres hätte den Jahreswert sonst
    # fälschlich sofort als verwendbar ausgewiesen, obwohl er nur eine
    # vorläufige Schätzung ist.
    jahreszeilen = [
        {
            "reihe": reihe,
            "jahr": jahr,
            "wert": wert,
            "finalitaet": "ENDGUELTIG" if (jahr + 1, 1) in monats_kandidaten else "VORLAEUFIG",
            "quelle": f"{tatsaechliche_url} (amtlicher Jahresdurchschnitt, Zeile {zeilennummer})",
            "quelle_datum": abgerufen_am.date(),
            "erfasst_von": importiert_von,
        }
        for jahr, (wert, zeilennummer) in jahres_kandidaten.items()
    ]

    repository.batch_erfassen(monatszeilen=monatszeilen, jahreszeilen=jahreszeilen)

    endgueltig = sum(1 for z in monatszeilen if z["finalitaet"] == "ENDGUELTIG")
    return VpiImportErgebnis(
        reihe=reihe,
        monatszeilen=len(monatszeilen),
        endgueltige_monatszeilen=endgueltig,
        vorlaeufige_monatszeilen=len(monatszeilen) - endgueltig,
        jahreszeilen=len(jahreszeilen),
    )
