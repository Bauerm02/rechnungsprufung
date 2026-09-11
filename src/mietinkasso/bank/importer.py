"""Bank-Importformate: CAMT.053 (ISO 20022) und konfigurierbares CSV.

Beide liefern eine Liste normalisierter `RohTransaktion`-Zeilen. Jede
Zeile trägt entweder eine bankseitig eindeutige `native_id` (z. B.
AcctSvcrRef bei CAMT, eine dedizierte Referenzspalte bei CSV) oder
keine. Nur mit `native_id` ist ein Wiederholimport sicher als Replay
erkennbar; ohne sie entscheidet `bank.service` konservativ per
Fingerprint-Konflikt statt stillschweigend zusammenzulegen oder zu
duplizieren (siehe `MehrfachbuchungsKonfliktError`).

Diese Parser sind bewusst konservativ (kein stilles Erraten, kein
stilles Überspringen unvollständiger Zeilen) und für die Pilotphase
gedacht; ein produktiver Rollout mit echten Bank-Exportformaten muss sie
gegen reale Dateien der Bank(en) von JLB/7DI härten (siehe
docs/hausverwaltung/OFFENE_PUNKTE.md).
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree as ET

from mietinkasso.domain.exceptions import FremdwaehrungNichtUnterstuetztError

_UNTERSTUETZTE_WAEHRUNG = "EUR"


class CamtUnvollstaendigError(Exception):
    """Eine Ntry fehlt ein Pflichtfeld (Betrag/Buchungsdatum). Wird nicht
    still übersprungen, weil das die Bankvollständigkeit unsichtbar
    verletzen würde."""


class CamtMehrteiligeBuchungError(Exception):
    """Eine Ntry bündelt mehrere TxDtls (Sammelbuchung), deren Einzelbeträge
    sich nicht eindeutig und vollständig auf den Ntry-Gesamtbetrag
    zurückführen lassen. Wird nicht der ersten Referenz zugeschlagen,
    sondern zur manuellen Klärung verweigert."""


@dataclass(frozen=True)
class RohTransaktion:
    betrag_cent: int
    waehrung: str
    buchungsdatum: date
    valuta: date | None
    referenz: str | None
    gegenkonto_iban: str | None
    gegenkonto_name: str | None
    native_id: str | None
    roh_zeile: str


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _direct_child(element: ET.Element, localname: str) -> ET.Element | None:
    for child in element:
        if _localname(child.tag) == localname:
            return child
    return None


def _find_text(element: ET.Element, localname: str) -> str | None:
    for child in element.iter():
        if _localname(child.tag) == localname and child.text:
            return child.text.strip()
    return None


def _to_cents(value: str) -> int:
    try:
        return int((Decimal(value) * 100).to_integral_value())
    except InvalidOperation as exc:
        raise CamtUnvollstaendigError(f"Ungültiger Betrag '{value}'.") from exc


def _pruefe_waehrung(waehrung: str, kontext: str) -> None:
    if waehrung != _UNTERSTUETZTE_WAEHRUNG:
        raise FremdwaehrungNichtUnterstuetztError(
            f"{kontext}: Währung '{waehrung}' wird nicht unterstützt (nur {_UNTERSTUETZTE_WAEHRUNG})."
        )


def parse_camt053(xml_bytes: bytes) -> list[RohTransaktion]:
    root = ET.fromstring(xml_bytes)
    ergebnisse: list[RohTransaktion] = []
    for entry in root.iter():
        if _localname(entry.tag) != "Ntry":
            continue

        amt_element = None
        for child in entry.iter():
            if _localname(child.tag) == "Amt":
                amt_element = child
                break
        if amt_element is None or not (amt_element.text or "").strip():
            raise CamtUnvollstaendigError("Ntry ohne Amt-Element gefunden; Zeile wird nicht still übersprungen.")
        entry_betrag_cent = _to_cents(amt_element.text.strip())
        waehrung = amt_element.attrib.get("Ccy", _UNTERSTUETZTE_WAEHRUNG)
        _pruefe_waehrung(waehrung, "CAMT.053 Ntry")

        richtung = _find_text(entry, "CdtDbtInd")
        if richtung is None:
            raise CamtUnvollstaendigError("Ntry ohne CdtDbtInd (Soll/Haben-Kennzeichen) gefunden.")
        if richtung == "DBIT":
            entry_betrag_cent = -entry_betrag_cent

        buchungsdatum_text = None
        valuta_text = None
        for child in entry:
            local = _localname(child.tag)
            if local == "BookgDt":
                buchungsdatum_text = _find_text(child, "Dt") or _find_text(child, "DtTm")
            if local == "ValDt":
                valuta_text = _find_text(child, "Dt") or _find_text(child, "DtTm")
        if buchungsdatum_text is None:
            raise CamtUnvollstaendigError("Ntry ohne BookgDt (Buchungsdatum) gefunden.")
        buchungsdatum = _parse_iso_date(buchungsdatum_text)
        valuta = _parse_iso_date(valuta_text) if valuta_text else None

        tx_dtls_liste = [child for child in entry.iter() if _localname(child.tag) == "TxDtls"]

        if len(tx_dtls_liste) <= 1:
            quelle = tx_dtls_liste[0] if tx_dtls_liste else entry
            referenz = _find_text(quelle, "Ustrd")
            gegenkonto_iban = _find_text(quelle, "IBAN")
            gegenkonto_name = _find_text(quelle, "Nm")
            native_id = _find_text(quelle, "AcctSvcrRef") or _find_text(entry, "AcctSvcrRef")
            ergebnisse.append(
                _bauen_camt_zeile(
                    entry_betrag_cent, waehrung, buchungsdatum, valuta, referenz, gegenkonto_iban,
                    gegenkonto_name, native_id,
                )
            )
            continue

        # Sammelbuchung mit mehreren TxDtls: NICHT stillschweigend der
        # ersten Referenz zuschlagen. Nur auto-splitten, wenn jede TxDtls
        # einen eigenen, in sich stimmigen Betrag trägt, der exakt auf den
        # Ntry-Gesamtbetrag aufsummiert - sonst zur manuellen Klärung
        # verweigern.
        teil_betraege: list[int] = []
        teil_zeilen: list[RohTransaktion] = []
        for tx_dtls in tx_dtls_liste:
            tx_amt_element = _direct_child(tx_dtls, "Amt")
            if tx_amt_element is None:
                for child in tx_dtls.iter():
                    if _localname(child.tag) == "Amt":
                        tx_amt_element = child
                        break
            if tx_amt_element is None or not (tx_amt_element.text or "").strip():
                raise CamtMehrteiligeBuchungError(
                    "Ntry mit mehreren TxDtls, aber nicht jede TxDtls trägt einen eigenen Betrag; "
                    "wird zur manuellen Klärung verweigert statt der ersten Referenz zugeschlagen."
                )
            tx_waehrung = tx_amt_element.attrib.get("Ccy", waehrung)
            _pruefe_waehrung(tx_waehrung, "CAMT.053 TxDtls")
            tx_betrag_cent = _to_cents(tx_amt_element.text.strip())
            if richtung == "DBIT":
                tx_betrag_cent = -tx_betrag_cent
            teil_betraege.append(tx_betrag_cent)
            teil_zeilen.append(
                _bauen_camt_zeile(
                    tx_betrag_cent, tx_waehrung, buchungsdatum, valuta,
                    _find_text(tx_dtls, "Ustrd"), _find_text(tx_dtls, "IBAN"), _find_text(tx_dtls, "Nm"),
                    _find_text(tx_dtls, "AcctSvcrRef"),
                )
            )
        if sum(teil_betraege) != entry_betrag_cent:
            raise CamtMehrteiligeBuchungError(
                f"Ntry mit mehreren TxDtls: Summe der Einzelbeträge ({sum(teil_betraege)}) entspricht nicht "
                f"dem Ntry-Gesamtbetrag ({entry_betrag_cent}); wird zur manuellen Klärung verweigert."
            )
        ergebnisse.extend(teil_zeilen)
    return ergebnisse


def _bauen_camt_zeile(
    betrag_cent: int,
    waehrung: str,
    buchungsdatum: date,
    valuta: date | None,
    referenz: str | None,
    gegenkonto_iban: str | None,
    gegenkonto_name: str | None,
    native_id: str | None,
) -> RohTransaktion:
    roh_zeile = (
        f"betrag_cent={betrag_cent};waehrung={waehrung};buchungsdatum={buchungsdatum};valuta={valuta};"
        f"referenz={referenz};gegenkonto_iban={gegenkonto_iban};gegenkonto_name={gegenkonto_name};"
        f"native_id={native_id}"
    )
    return RohTransaktion(
        betrag_cent=betrag_cent,
        waehrung=waehrung,
        buchungsdatum=buchungsdatum,
        valuta=valuta,
        referenz=referenz,
        gegenkonto_iban=gegenkonto_iban,
        gegenkonto_name=gegenkonto_name,
        native_id=native_id,
        roh_zeile=roh_zeile,
    )


def _parse_iso_date(value: str) -> date:
    return datetime.fromisoformat(value[:10]).date()


@dataclass(frozen=True)
class CsvSpaltenMapping:
    betrag: str
    buchungsdatum: str
    waehrung: str | None = None
    referenz: str | None = None
    eindeutige_referenz: str | None = None
    gegenkonto_iban: str | None = None
    gegenkonto_name: str | None = None
    valuta: str | None = None
    datumsformat: str = "%Y-%m-%d"
    dezimaltrennzeichen: str = "."


def parse_csv(text: str, mapping: CsvSpaltenMapping) -> list[RohTransaktion]:
    ergebnisse: list[RohTransaktion] = []
    reader = csv.DictReader(io.StringIO(text))
    for zeile in reader:
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
        waehrung = zeile.get(mapping.waehrung, _UNTERSTUETZTE_WAEHRUNG).strip() if mapping.waehrung else _UNTERSTUETZTE_WAEHRUNG
        _pruefe_waehrung(waehrung or _UNTERSTUETZTE_WAEHRUNG, "CSV-Zeile")
        native_id = zeile.get(mapping.eindeutige_referenz, "").strip() if mapping.eindeutige_referenz else None
        roh_zeile = ",".join(f"{k}={v}" for k, v in zeile.items())
        ergebnisse.append(
            RohTransaktion(
                betrag_cent=betrag_cent,
                waehrung=waehrung or _UNTERSTUETZTE_WAEHRUNG,
                buchungsdatum=buchungsdatum,
                valuta=valuta,
                referenz=referenz or None,
                gegenkonto_iban=gegenkonto_iban or None,
                gegenkonto_name=gegenkonto_name or None,
                native_id=native_id or None,
                roh_zeile=roh_zeile,
            )
        )
    return ergebnisse
