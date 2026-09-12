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


class CamtKontoMismatchError(Exception):
    """Mindestens ein `Stmt`/`Acct`-Block der CAMT.053-Datei führt keine
    IBAN oder eine ANDERE IBAN als das explizit für diesen Import
    ausgewählte Bankkonto. Der GESAMTE Import wird abgelehnt, BEVOR
    irgendeine Zeile eingelesen wird - kein stilles Herausfiltern
    fremder Konten, keine pauschale Zuordnung einer gemischten
    Mehrkonten-Datei auf das ausgewählte Konto.

    Hintergrund: eine künftige EBICS-C53-Anbindung kann kundenweite
    Sammeldateien mit MEHREREN Konten in einer Antwort liefern (mehrere
    `Stmt`/`Acct`-Blöcke mit unterschiedlicher IBAN in einer einzigen
    CAMT.053-Datei) - ohne diese Prüfung könnten Umsätze eines fremden
    Kontos fälschlich dem hier ausgewählten Mietkonto gutgeschrieben
    werden. Der aktuelle Ein-Konto-Dateiupload bleibt unverändert;
    dieser Import lehnt lediglich Dateien ab, die (auch versehentlich)
    mehr als das ausgewählte Konto enthalten."""


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


def _direct_children(element: ET.Element, localname: str) -> list[ET.Element]:
    return [child for child in element if _localname(child.tag) == localname]


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


def _normalisiere_iban(iban: str | None) -> str:
    return (iban or "").strip().upper().replace(" ", "")


def _eindeutige_iban(acct: ET.Element) -> str | None:
    """Verlangt eine EINDEUTIGE IBAN im Acct-Block - anders als `_find_text`
    (nimmt den ersten Treffer) wird MEHR als eine gefundene IBAN-Angabe als
    nicht eindeutig zuordenbar abgelehnt, statt stillschweigend die erste
    zu verwenden."""

    ibans = [
        (child.text or "").strip()
        for child in acct.iter()
        if _localname(child.tag) == "IBAN" and (child.text or "").strip()
    ]
    eindeutige_werte = sorted(set(ibans))
    if len(eindeutige_werte) > 1:
        raise CamtKontoMismatchError(
            f"Acct-Block enthält mehrere unterschiedliche IBAN-Angaben ({eindeutige_werte}); nicht eindeutig "
            "zuordenbar - Import abgelehnt."
        )
    return ibans[0] if ibans else None


def _pruefe_stmt_konten(root: ET.Element, erwartete_iban: str) -> list[ET.Element]:
    """Validiert JEDEN `Stmt`/`Acct`-Block gegen das explizit ausgewählte
    Bankkonto, BEVOR auch nur eine `Ntry` gelesen wird - siehe
    `CamtKontoMismatchError`. Ein fehlendes `Stmt`-Element (untypisch für
    eine echte CAMT.053-Datei) wird ebenfalls abgelehnt statt stillschweigend
    durchgereicht, damit eine strukturell unerwartete Datei nie ungeprüft
    Kontenzuordnungen auslöst. Liefert die Liste der validierten `Stmt`-
    Elemente zurück, damit der Aufrufer AUSSCHLIESSLICH deren direkte
    `Ntry`-Kinder einliest (siehe `parse_camt053`) - eine `Ntry` außerhalb
    jedes validierten `Stmt`, oder tiefer verschachtelt als ein direktes
    Kind, würde sonst trotz bestandener Kontoprüfung mitgelesen."""

    erwartete_iban_norm = _normalisiere_iban(erwartete_iban)
    if not erwartete_iban_norm:
        raise CamtKontoMismatchError("Kein IBAN für das ausgewählte Bankkonto hinterlegt; Import abgelehnt.")

    stmt_elemente = [el for el in root.iter() if _localname(el.tag) == "Stmt"]
    if not stmt_elemente:
        raise CamtKontoMismatchError(
            "CAMT.053-Datei enthält kein Stmt-Element; Kontozugehörigkeit nicht prüfbar - Import abgelehnt."
        )

    gefundene_ibans: set[str] = set()
    for stmt in stmt_elemente:
        accts = _direct_children(stmt, "Acct")
        if len(accts) > 1:
            raise CamtKontoMismatchError(
                "CAMT.053-Statement enthält mehrere Acct-Blöcke; nicht eindeutig zuordenbar - Import abgelehnt."
            )
        acct = accts[0] if accts else None
        iban = _eindeutige_iban(acct) if acct is not None else None
        iban_norm = _normalisiere_iban(iban)
        if not iban_norm:
            raise CamtKontoMismatchError(
                "CAMT.053-Statement ohne (oder mit leerer) IBAN im Acct-Block gefunden; der gesamte Import "
                "wird abgelehnt - keine pauschale Zuordnung ohne geprüfte Kontokennung."
            )
        gefundene_ibans.add(iban_norm)

    fremde_ibans = gefundene_ibans - {erwartete_iban_norm}
    if fremde_ibans:
        raise CamtKontoMismatchError(
            f"CAMT.053-Datei enthält Statement(s) für nicht ausgewählte(s) Konto(en) {sorted(fremde_ibans)} "
            f"(ausgewähltes Konto: {erwartete_iban_norm}) - der GESAMTE Import wird abgelehnt, auch wenn "
            "daneben Statements für das richtige Konto enthalten sind (keine gemischte Mehrkonten-Datei wird "
            "pauschal einem einzelnen Konto zugeordnet)."
        )
    if erwartete_iban_norm not in gefundene_ibans:
        raise CamtKontoMismatchError(
            f"CAMT.053-Datei enthält kein Statement für das ausgewählte Konto {erwartete_iban_norm}."
        )
    return stmt_elemente


def _pruefe_und_sammle_direkte_ntry(root: ET.Element, stmt_elemente: list[ET.Element]) -> list[ET.Element]:
    """Sammelt AUSSCHLIESSLICH `Ntry`-Elemente, die DIREKTE Kinder eines
    bereits gegen das ausgewählte Konto validierten `Stmt` sind (siehe
    `_pruefe_stmt_konten`). Jede `Ntry` irgendwo sonst im Dokument -
    außerhalb jedes `Stmt` (z. B. fälschlich auf `BkToCstmrStmt`-Ebene)
    oder tiefer verschachtelt als ein direktes `Stmt`-Kind - lehnt den
    GESAMTEN Import ab, statt sie unvalidiert mitzulesen (Codex-Fund:
    `root.iter()` fand bisher JEDE `Ntry` im Dokument, unabhängig davon,
    ob sie überhaupt zu einem geprüften `Stmt` gehörte)."""

    valide: list[ET.Element] = []
    valide_ids: set[int] = set()
    for stmt in stmt_elemente:
        for ntry in _direct_children(stmt, "Ntry"):
            valide.append(ntry)
            valide_ids.add(id(ntry))

    alle_ntry = [el for el in root.iter() if _localname(el.tag) == "Ntry"]
    if any(id(el) not in valide_ids for el in alle_ntry):
        raise CamtKontoMismatchError(
            "CAMT.053-Datei enthält mindestens eine Ntry außerhalb (oder tiefer verschachtelt als ein "
            "direktes Kind) eines geprüften Stmt-Blocks - Import abgelehnt, keine ungeprüfte Kontobindung."
        )
    return valide


def parse_camt053(xml_bytes: bytes, *, erwartete_iban: str) -> list[RohTransaktion]:
    """`erwartete_iban`: die IBAN des für DIESEN Import explizit
    ausgewählten Bankkontos - Pflichtparameter (kein Default), damit kein
    Aufrufer die Kontenprüfung versehentlich auslässt. Siehe
    `CamtKontoMismatchError`/`_pruefe_stmt_konten`: jede Datei mit einem
    fehlenden, fremden oder zusätzlichen (gemischten) Konto wird VOR jedem
    Einlesen einer `Ntry` vollständig abgelehnt."""

    root = ET.fromstring(xml_bytes)
    stmt_elemente = _pruefe_stmt_konten(root, erwartete_iban)
    valide_ntry = _pruefe_und_sammle_direkte_ntry(root, stmt_elemente)
    ergebnisse: list[RohTransaktion] = []
    for entry in valide_ntry:
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
