"""Bank-Importformate: CAMT.053 (ISO 20022) und konfigurierbares CSV.

Beide liefern eine Liste normalisierter dict-Zeilen, die
`bank.service.BankImportService` in Banktransaktionen umwandelt. Diese
Parser sind bewusst konservativ (kein Auto-Erraten von Spalten, keine
stille Fehlertoleranz) und für die Pilotphase gedacht; ein produktiver
Rollout mit echten Bank-Exportformaten muss sie gegen reale Dateien der
Bank(en) von JLB/7DI härten (siehe docs/hausverwaltung/OFFENE_PUNKTE.md).
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from xml.etree import ElementTree as ET


@dataclass(frozen=True)
class RohTransaktion:
    betrag_cent: int
    waehrung: str
    buchungsdatum: date
    valuta: date | None
    referenz: str | None
    gegenkonto_iban: str | None
    gegenkonto_name: str | None
    import_id: str
    quelle_hash: str
    roh_zeile: str


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_text(element: ET.Element, localname: str) -> str | None:
    for child in element.iter():
        if _localname(child.tag) == localname and child.text:
            return child.text.strip()
    return None


def _to_cents(value: str) -> int:
    return int((Decimal(value) * 100).to_integral_value())


def parse_camt053(xml_bytes: bytes) -> list[RohTransaktion]:
    root = ET.fromstring(xml_bytes)
    ergebnisse: list[RohTransaktion] = []
    for entry in root.iter():
        if _localname(entry.tag) != "Ntry":
            continue
        betrag_text = _find_text(entry, "Amt")
        if betrag_text is None:
            continue
        richtung = _find_text(entry, "CdtDbtInd") or "CRDT"
        betrag_cent = _to_cents(betrag_text)
        if richtung == "DBIT":
            betrag_cent = -betrag_cent
        waehrung = "EUR"
        for child in entry.iter():
            if _localname(child.tag) == "Amt" and "Ccy" in child.attrib:
                waehrung = child.attrib["Ccy"]
                break
        buchungsdatum_text = None
        valuta_text = None
        for child in entry.iter():
            local = _localname(child.tag)
            if local == "BookgDt":
                buchungsdatum_text = _find_text(child, "Dt") or _find_text(child, "DtTm")
            if local == "ValDt":
                valuta_text = _find_text(child, "Dt") or _find_text(child, "DtTm")
        if buchungsdatum_text is None:
            continue
        buchungsdatum = _parse_iso_date(buchungsdatum_text)
        valuta = _parse_iso_date(valuta_text) if valuta_text else None

        referenz = _find_text(entry, "Ustrd") or _find_text(entry, "AcctSvcrRef") or _find_text(entry, "EndToEndId")
        gegenkonto_iban = _find_text(entry, "IBAN")
        gegenkonto_name = _find_text(entry, "Nm")
        acct_svcr_ref = _find_text(entry, "AcctSvcrRef")

        roh_zeile = ET.tostring(entry, encoding="unicode")
        content_hash = hashlib.sha256(roh_zeile.encode("utf-8")).hexdigest()
        import_id = f"CAMT053:{acct_svcr_ref or content_hash}"
        ergebnisse.append(
            RohTransaktion(
                betrag_cent=betrag_cent,
                waehrung=waehrung,
                buchungsdatum=buchungsdatum,
                valuta=valuta,
                referenz=referenz,
                gegenkonto_iban=gegenkonto_iban,
                gegenkonto_name=gegenkonto_name,
                import_id=import_id,
                quelle_hash=content_hash,
                roh_zeile=roh_zeile,
            )
        )
    return ergebnisse


def _parse_iso_date(value: str) -> date:
    return datetime.fromisoformat(value[:10]).date()


@dataclass(frozen=True)
class CsvSpaltenMapping:
    betrag: str
    buchungsdatum: str
    waehrung: str | None = None
    referenz: str | None = None
    gegenkonto_iban: str | None = None
    gegenkonto_name: str | None = None
    valuta: str | None = None
    datumsformat: str = "%Y-%m-%d"
    dezimaltrennzeichen: str = "."


def parse_csv(text: str, mapping: CsvSpaltenMapping) -> list[RohTransaktion]:
    ergebnisse: list[RohTransaktion] = []
    reader = csv.DictReader(io.StringIO(text))
    for zeilennummer, zeile in enumerate(reader, start=1):
        betrag_rohtext = zeile[mapping.betrag].strip()
        if mapping.dezimaltrennzeichen == ",":
            betrag_rohtext = betrag_rohtext.replace(".", "").replace(",", ".")
        betrag_cent = _to_cents(betrag_rohtext)
        buchungsdatum = datetime.strptime(zeile[mapping.buchungsdatum].strip(), mapping.datumsformat).date()
        valuta = None
        if mapping.valuta and zeile.get(mapping.valuta):
            valuta = datetime.strptime(zeile[mapping.valuta].strip(), mapping.datumsformat).date()
        referenz = zeile.get(mapping.referenz, "").strip() if mapping.referenz else None
        gegenkonto_iban = zeile.get(mapping.gegenkonto_iban, "").strip() if mapping.gegenkonto_iban else None
        gegenkonto_name = zeile.get(mapping.gegenkonto_name, "").strip() if mapping.gegenkonto_name else None
        waehrung = zeile.get(mapping.waehrung, "EUR").strip() if mapping.waehrung else "EUR"
        roh_zeile = ",".join(f"{k}={v}" for k, v in zeile.items())
        content_hash = hashlib.sha256(roh_zeile.encode("utf-8")).hexdigest()
        import_id = f"CSV:{referenz or ''}:{buchungsdatum}:{betrag_cent}:{zeilennummer}:{content_hash[:12]}"
        ergebnisse.append(
            RohTransaktion(
                betrag_cent=betrag_cent,
                waehrung=waehrung or "EUR",
                buchungsdatum=buchungsdatum,
                valuta=valuta,
                referenz=referenz or None,
                gegenkonto_iban=gegenkonto_iban or None,
                gegenkonto_name=gegenkonto_name or None,
                import_id=import_id,
                quelle_hash=content_hash,
                roh_zeile=roh_zeile,
            )
        )
    return ergebnisse
