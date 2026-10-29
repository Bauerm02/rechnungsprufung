"""Kontrollierte Übernahme vorab abgeholter EBICS-CAMT.053-Dateien in den
bestehenden Bankdatenweg (Auftrag HV-20261029-EBICS-INTEGRATION).

Ein vorgelagerter, lokaler und rein lesender Transport (z. B.
`ebics-downloader/`) liefert je IBAN getrennte, freigegebene CAMT.053-
Dateien. Dieses Modul baut daraus KEINE eigene Bankanbindung, keinen
Scheduler und keine zweite OP-Liste, sondern:

- `plan_ebics_camt053` - rein lesender Dry-run: parst die Datei mit dem
  aktuellen `parse_camt053`, prüft Konto/Format und klassifiziert jede
  Zeile gegen den VORHANDENEN Bankbestand des Kontos (alle Quellen:
  CAMT-Upload, CSV, George-/Agicap-Übernahmen über CSV, frühere EBICS-
  Läufe) als NEU / VORHANDEN / UNKLAR / KONFLIKT. Liefert Quellhash,
  Dokument-Metadaten, Quellzeilen je Vorgang, einen Bankbestands-Snapshot
  und einen stabilen Planhash.
- `apply_ebics_camt053` - schreibt AUSSCHLIESSLICH die NEU-Zeilen über
  `BankImportService.importiere_ausgewaehlt_atomar` (dieselbe
  `_speichere_roh`-Speicherung, dieselbe SQLite-Schreibsperre). Der Plan
  wird UNTER der Sperre neu berechnet; nur bei identischem Planhash wird
  geschrieben, atomar in einer Transaktion. Keine OP-Buchung, keine
  Zuordnung, keine Forderung/Gebühr - Datenimport und Mietbuchung bleiben
  getrennt.

Klassifizierungsregeln (konservativ; siehe docs/hausverwaltung/
EBICS_INGEST.md):

1. Bankidentität je Zeile (`_bestimme_identitaeten`): eine in der Datei
   eindeutige AcctSvcrRef unverändert; eine mehrfach vergebene
   AcctSvcrRef (Sammel-/Berichtsreferenz) qualifiziert mit originaler
   Statement-Id und Ntry-/TxDtls-Position; ohne AcctSvcrRef ein reiner
   Positionsschlüssel. Ohne eindeutige Statement-Id keine Qualifizierung
   (mehrfache Referenz -> UNKLAR). Diese Identität wird QUELLENÜBERGREIFEND
   gegen den Bestand verglichen: gleicher Inhalt -> VORHANDEN (auch
   Wiederholung derselben Datei ohne native ID), widersprechender Inhalt
   oder geänderter Beleg an gleicher Originalposition -> KONFLIKT (Plan
   nicht anwendbar, Apply bricht vollständig ab).
2. Ohne Identitätstreffer zählt nur ein AUSDRÜCKLICHER Exact-Match:
   gleicher Centbetrag, gleiche Währung, gleiches Datum (Buchungsdatum
   oder kreuzweise Valuta) UND hinreichende Identität (gleiche
   Gegenkonto-IBAN oder gleiche/enthaltene Referenz, ohne Widerspruch im
   jeweils anderen Merkmal). Nur eine eindeutige 1:1-Zuordnung (bzw. eine
   vollständig ununterscheidbare Gruppe gleicher Größe) dedupliziert.
   Eine bestätigt ANDERE CAMT-AcctSvcrRef ist nie dieselbe Zahlung
   (gleich aussehend -> UNKLAR, sonst kein Klärfall); eine andere Position
   desselben Original-Statements ist eine eigenständige Buchung.
3. Gleicher Betrag in der Datumstoleranz ohne Exact-Match, mehrdeutige
   Exact-Matches, mögliche Sammelbuchung (bestehende Zeile = Summe von
   >= 2 Dateizeilen) oder mögliche Zerlegung (Dateizeile = Summe von >= 2
   bestehenden Zeilen) -> UNKLAR: nicht geschrieben, vollständig im Plan
   berichtet, kein lückenloser Abgleichstatus.

Salden und Auszugsabdeckung werden NICHT ermittelt oder behauptet - das
sind separate Transportdaten. Dokumentangaben (`FrToDt`, `CreDtTm`)
werden nur als "laut Dokument" durchgereicht, nie als geprüfte
Abdeckung.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import date
from xml.etree import ElementTree as ET

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.auth.service import AuthContext, require_gesellschaft_access
from mietinkasso.bank.importer import (
    RohTransaktion,
    _direct_child,
    _direct_children,
    _localname,
    _pruefe_stmt_konten,
    _pruefe_und_sammle_direkte_ntry,
    parse_camt053,
)
from mietinkasso.bank.repository import BankRepository
from mietinkasso.bank.service import BankImportService, _fingerprint, inhalt_hash
from mietinkasso.domain.exceptions import MietinkassoError
from mietinkasso.infrastructure.db.tables import BankKontoTable, BankTransaktionTable
from mietinkasso.op.repository import OPRepository
from mietinkasso.op.service import OPService
from mietinkasso.stammdaten.repository import StammdatenRepository

REGELVERSION = "EBICS-INGEST-2"
QUELLE_TYP = "CAMT053"  # identisch zum CAMT-Upload: gleiche import_id je native ID
DATUM_TOLERANZ_TAGE_STANDARD = 4
MAX_SUMMEN_KANDIDATEN = 18
_CAMT053_NAMESPACE_PRAEFIX = "urn:iso:std:iso:20022:tech:xsd:camt.053."
_REFERENZ_MIN_TEILLAENGE = 8

STATUS_NEU = "NEU"
STATUS_VORHANDEN = "VORHANDEN"
STATUS_UNKLAR = "UNKLAR"
STATUS_KONFLIKT = "KONFLIKT"

GRUND_NEU = "NEU_KEIN_BESTAND"
GRUND_NATIVE_ID_GLEICH = "VORHANDEN_NATIVE_ID"
GRUND_WIRTSCHAFTLICH_IDENTISCH = "VORHANDEN_WIRTSCHAFTLICH_IDENTISCH"
GRUND_NATIVE_ID_WIDERSPRUCH = "KONFLIKT_NATIVE_ID_INHALT"
GRUND_ORIGINALPOSITION_WIDERSPRUCH = "KONFLIKT_ORIGINALPOSITION_INHALT"
GRUND_REFERENZ_NICHT_EINDEUTIG = "UNKLAR_REFERENZ_NICHT_EINDEUTIG"
GRUND_ANDERE_BANKIDENTITAET = "UNKLAR_ANDERE_BANKIDENTITAET"
GRUND_MEHRDEUTIG = "UNKLAR_MEHRDEUTIGER_EXACT_MATCH"
GRUND_GLEICHBETRAG = "UNKLAR_GLEICHBETRAG_IDENTITAET_UNZUREICHEND"
GRUND_SAMMELBUCHUNG = "UNKLAR_SAMMELBUCHUNG_VORHANDEN"
GRUND_EINZELPOSTEN = "UNKLAR_EINZELPOSTEN_VORHANDEN"
GRUND_SUMMENPRUEFUNG_ZU_GROSS = "UNKLAR_SUMMENPRUEFUNG_NICHT_MOEGLICH"
GRUND_DATEI_INTERN = "UNKLAR_DATEI_INTERN_UNUNTERSCHEIDBAR"

# Identitätsarten je Dateizeile (siehe `_bestimme_identitaeten`)
ART_ACCTSVCRREF = "ACCTSVCRREF"
ART_ACCTSVCRREF_POSITION = "ACCTSVCRREF_STMT_POSITION"
ART_POSITION = "STMT_POSITION"
ART_REFERENZ_NICHT_EINDEUTIG = "ACCTSVCRREF_NICHT_EINDEUTIG"
ART_KEINE = "KEINE"

BEZIEHUNG_GETRENNT = "GETRENNT"
BEZIEHUNG_ANDERE_BANKID = "ANDERE_BANKID"


class EbicsIngestError(MietinkassoError):
    """Basisklasse aller Ablehnungen dieses Moduls."""


class EbicsDokumentAbgelehntError(EbicsIngestError):
    """Kein finales EUR-camt.053 (falscher Namespace/Nachrichtentyp, nicht
    gebuchte Einträge, fehlende/fremde Währung, DOCTYPE/Entity,
    ungültiges XML) - die gesamte Datei wird abgelehnt."""


class EbicsKontoAbweichungError(EbicsIngestError):
    """Die vom Transport erwartete IBAN passt nicht zum Bankkonto in der
    Datenbank (oder das Konto existiert nicht)."""


class EbicsPlanHashAbweichungError(EbicsIngestError):
    """Der unter der Schreibsperre neu berechnete Plan weicht vom
    bestätigten Planhash ab (Datei, Bankbestand oder Regeln geändert) -
    nichts wurde geschrieben."""


class EbicsPlanNichtAnwendbarError(EbicsIngestError):
    """Der Plan enthält KONFLIKT-Einträge - nichts wurde geschrieben."""


@dataclass(frozen=True)
class PlanEintrag:
    position: int
    ntry_index: int
    txdtls_index: int | None
    ntry_teilzeilen: int
    ntry_ref: str | None
    ntry_status: str
    ntry_sha256: str
    status: str
    grund_code: str
    grund: str
    bestehende_transaktion_ids: tuple[int, ...]
    identitaet_art: str
    original_acct_svcr_ref: str | None
    ntry_acct_svcr_ref: str | None
    stmt_id: str | None
    stmt_position: tuple[int, int | None] | None
    positions_schluessel: str | None
    roh: RohTransaktion = field(repr=False, compare=False)

    def to_dict(self) -> dict:
        roh = self.roh
        ntry_pos, tx_pos = self.stmt_position if self.stmt_position else (None, None)
        return {
            "position": self.position,
            "status": self.status,
            "grund_code": self.grund_code,
            "grund": self.grund,
            "bestehende_transaktion_ids": list(self.bestehende_transaktion_ids),
            "betrag_cent": roh.betrag_cent,
            "waehrung": roh.waehrung,
            "buchungsdatum": roh.buchungsdatum.isoformat(),
            "valuta": roh.valuta.isoformat() if roh.valuta else None,
            "referenz": roh.referenz,
            "gegenkonto_iban": roh.gegenkonto_iban,
            "gegenkonto_name": roh.gegenkonto_name,
            "native_id": roh.native_id,
            "identitaet": {
                "art": self.identitaet_art,
                "original_acct_svcr_ref": self.original_acct_svcr_ref,
                "ntry_acct_svcr_ref": self.ntry_acct_svcr_ref,
                "stmt_id": self.stmt_id,
                "stmt_ntry_position": ntry_pos,
                "stmt_txdtls_position": tx_pos,
                "positions_schluessel": self.positions_schluessel,
            },
            "quelle": {
                "ntry_index": self.ntry_index,
                "txdtls_index": self.txdtls_index,
                "ntry_teilzeilen": self.ntry_teilzeilen,
                "ntry_ref": self.ntry_ref,
                "ntry_status": self.ntry_status,
                "ntry_sha256": self.ntry_sha256,
                "roh_zeile": roh.roh_zeile,
            },
        }


@dataclass(frozen=True)
class EbicsIngestPlan:
    regelversion: str
    bank_konto_id: str
    gesellschaft_id: str
    iban: str
    quelle_sha256: str
    quelle_bytes: int
    datum_toleranz_tage: int
    dokument: dict
    eintraege: tuple[PlanEintrag, ...]
    bankbestand_snapshot: dict
    plan_hash: str

    def _mit_status(self, status: str) -> list[PlanEintrag]:
        return [e for e in self.eintraege if e.status == status]

    @property
    def neu(self) -> list[PlanEintrag]:
        return self._mit_status(STATUS_NEU)

    @property
    def vorhanden(self) -> list[PlanEintrag]:
        return self._mit_status(STATUS_VORHANDEN)

    @property
    def unklar(self) -> list[PlanEintrag]:
        return self._mit_status(STATUS_UNKLAR)

    @property
    def konflikte(self) -> list[PlanEintrag]:
        return self._mit_status(STATUS_KONFLIKT)

    @property
    def anwendbar(self) -> bool:
        return not self.konflikte

    @property
    def abgleich_vollstaendig(self) -> bool:
        """True NUR, wenn kein Eintrag zurückgehalten wird. Sagt nichts über
        Auszugsabdeckung oder Salden aus (separate Transportdaten)."""

        return not self.konflikte and not self.unklar

    def to_dict(self) -> dict:
        daten = dict(
            regelversion=self.regelversion, bank_konto_id=self.bank_konto_id, gesellschaft_id=self.gesellschaft_id,
            iban=self.iban, quelle_sha256=self.quelle_sha256, quelle_bytes=self.quelle_bytes,
            datum_toleranz_tage=self.datum_toleranz_tage, dokument=self.dokument,
            eintraege=[e.to_dict() for e in self.eintraege], bankbestand_snapshot=self.bankbestand_snapshot,
        )
        daten["plan_hash"] = self.plan_hash
        daten["zusammenfassung"] = {
            "eintraege": len(self.eintraege),
            "neu": len(self.neu),
            "vorhanden": len(self.vorhanden),
            "unklar": len(self.unklar),
            "konflikte": len(self.konflikte),
            "neu_summe_cent": sum(e.roh.betrag_cent for e in self.neu),
            "unklar_summe_cent": sum(e.roh.betrag_cent for e in self.unklar),
            "anwendbar": self.anwendbar,
            "abgleich_vollstaendig": self.abgleich_vollstaendig,
        }
        return daten


@dataclass(frozen=True)
class EbicsIngestErgebnis:
    plan: EbicsIngestPlan
    geschriebene_transaktion_ids: tuple[int, ...]

    @property
    def abgleich_vollstaendig(self) -> bool:
        return self.plan.abgleich_vollstaendig

    def to_dict(self) -> dict:
        return {
            "plan_hash": self.plan.plan_hash,
            "bank_konto_id": self.plan.bank_konto_id,
            "quelle_sha256": self.plan.quelle_sha256,
            "geschrieben": [
                {"position": e.position, "bank_transaktion_id": tid, "betrag_cent": e.roh.betrag_cent,
                 "buchungsdatum": e.roh.buchungsdatum.isoformat()}
                for e, tid in zip(self.plan.neu, self.geschriebene_transaktion_ids)
            ],
            "anzahl_geschrieben": len(self.geschriebene_transaktion_ids),
            "anzahl_vorhanden": len(self.plan.vorhanden),
            "zurueckgehalten": [e.to_dict() for e in self.plan.unklar],
            "abgleich_vollstaendig": self.abgleich_vollstaendig,
            "op_oder_zuordnung_geaendert": False,
        }


# -- öffentliche API ------------------------------------------------------------


def plan_ebics_camt053(
    *,
    session_factory: sessionmaker[Session],
    bank_konto_id: str,
    erwartete_iban: str,
    camt_bytes: bytes,
    ctx: AuthContext,
    datum_toleranz_tage: int = DATUM_TOLERANZ_TAGE_STANDARD,
) -> EbicsIngestPlan:
    """Rein lesend. Verlangt Gesellschaftszugriff auf das Konto (kein
    Schreibrecht nötig). Funktioniert mit einer Read-only-Verbindung
    (SQLite `mode=ro`). Wirft `EbicsIngestError`-Unterklassen bzw. die
    Parser-Fehler aus `bank.importer` bei abgelehnten Dateien."""

    with session_factory() as session:
        bank_konto = _lade_konto(session, bank_konto_id)
        require_gesellschaft_access(ctx, bank_konto.gesellschaft_id)
        plan = _erstelle_plan(session, bank_konto, erwartete_iban, camt_bytes, datum_toleranz_tage)
        session.rollback()
        return plan


def apply_ebics_camt053(
    *,
    session_factory: sessionmaker[Session],
    bank_konto_id: str,
    erwartete_iban: str,
    camt_bytes: bytes,
    ctx: AuthContext,
    erwarteter_plan_hash: str,
    datum_toleranz_tage: int = DATUM_TOLERANZ_TAGE_STANDARD,
    bank_service: BankImportService | None = None,
) -> EbicsIngestErgebnis:
    """Schreibt die NEU-Zeilen des Plans atomar, wenn der UNTER der
    SQLite-Schreibsperre neu berechnete Planhash `erwarteter_plan_hash`
    entspricht. Verlangt Gesellschaftszugriff UND Schreibrecht. UNKLAR-
    Zeilen werden nicht geschrieben (im Ergebnis vollständig berichtet),
    VORHANDEN-Zeilen nicht erneut; ein Plan mit KONFLIKT wird abgelehnt."""

    if not erwarteter_plan_hash or not erwarteter_plan_hash.strip():
        raise EbicsPlanHashAbweichungError("Kein bestätigter Planhash angegeben - Apply verweigert.")
    service = bank_service or _baue_bank_service(session_factory)
    erfasst: dict[str, EbicsIngestPlan] = {}

    def _auswahl(session: Session, bank_konto: BankKontoTable) -> list[tuple[RohTransaktion, str | None]]:
        plan = _erstelle_plan(session, bank_konto, erwartete_iban, camt_bytes, datum_toleranz_tage)
        if plan.plan_hash != erwarteter_plan_hash.strip():
            raise EbicsPlanHashAbweichungError(
                f"Unter der Schreibsperre neu berechneter Planhash {plan.plan_hash} weicht vom bestätigten "
                f"Planhash {erwarteter_plan_hash} ab (Datei, Bankbestand oder Regelversion geändert) - "
                "nichts wurde geschrieben; bitte neu planen."
            )
        if not plan.anwendbar:
            raise EbicsPlanNichtAnwendbarError(
                f"Plan enthält {len(plan.konflikte)} Konflikt(e) (widersprechende native ID) - nichts wurde "
                "geschrieben."
            )
        erfasst["plan"] = plan
        return [(e.roh, e.positions_schluessel) for e in plan.neu]

    zeilen = service.importiere_ausgewaehlt_atomar(
        ctx=ctx, bank_konto_id=bank_konto_id, quelle_typ=QUELLE_TYP, auswahl=_auswahl
    )
    return EbicsIngestErgebnis(plan=erfasst["plan"], geschriebene_transaktion_ids=tuple(z.id for z in zeilen))


# -- Planerstellung ---------------------------------------------------------------


def _baue_bank_service(session_factory: sessionmaker[Session]) -> BankImportService:
    stammdaten = StammdatenRepository(session_factory)
    return BankImportService(
        BankRepository(session_factory), stammdaten, OPService(OPRepository(session_factory), stammdaten)
    )


def _lade_konto(session: Session, bank_konto_id: str) -> BankKontoTable:
    bank_konto = session.get(BankKontoTable, bank_konto_id)
    if bank_konto is None:
        raise EbicsKontoAbweichungError(f"Unbekanntes Bankkonto {bank_konto_id}.")
    return bank_konto


def _norm_iban(iban: str | None) -> str:
    return re.sub(r"\s+", "", iban or "").upper()


def _sha256(daten: bytes) -> str:
    return hashlib.sha256(daten).hexdigest()


def _kanonisch_hash(daten: dict) -> str:
    return _sha256(json.dumps(daten, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


@dataclass(frozen=True)
class _DateiZeile:
    position: int
    ntry_index: int
    txdtls_index: int | None
    ntry_teilzeilen: int
    ntry_ref: str | None
    ntry_status: str
    ntry_sha256: str
    roh: RohTransaktion  # effektive Rohzeile (ggf. qualifizierte native_id; roh_zeile unverändert)
    ntry_acct_svcr_ref: str | None = None
    stmt_id: str | None = None
    stmt_schluessel: str | None = None  # nur bei eindeutiger Statement-Id
    stmt_position: tuple[int, int | None] | None = None  # (Ntry im Stmt, TxDtls)
    identitaet_art: str = ""
    original_acct_svcr_ref: str | None = None
    positions_schluessel: str | None = None


@dataclass(frozen=True)
class _BestandIdentitaet:
    schluessel: str | None  # native ID (hat_native_id) bzw. Positionsschlüssel (NOID)
    original_acct_svcr_ref: str | None
    stmt_schluessel: str | None
    stmt_position: tuple[int, int | None] | None


def _stmt_schluessel(stmt_id: str) -> str:
    """Kurzform der originalen Statement-Id für die import_id (Längengrenze
    128 Zeichen); die Klartext-Id steht im Plan/Ergebnis."""

    return hashlib.sha256(stmt_id.encode("utf-8")).hexdigest()[:12]


def _positions_suffix(stmt_schluessel: str, position: tuple[int, int | None]) -> str:
    ntry, tx = position
    return f"#S{stmt_schluessel}N{ntry}" + (f"T{tx}" if tx is not None else "")


def _positions_text(position: tuple[int, int | None] | None) -> str:
    if position is None:
        return "-"
    ntry, tx = position
    return f"Ntry {ntry}" + (f"/TxDtls {tx}" if tx is not None else "")


def _bestimme_identitaeten(zeilen: list[_DateiZeile]) -> list[_DateiZeile]:
    """Belegte Identität je Zeile, ohne Ableitung aus Namen/Beträgen:

    - AcctSvcrRef genau einmal in der Datei -> diese Referenz (unverändert,
      kompatibel zum CAMT-Upload).
    - AcctSvcrRef mehrfach in der Datei (Bank nutzt sie als Sammel-/
      Berichtsreferenz) -> nur zusammen mit originaler Statement-Id und
      Ntry-/TxDtls-Position eindeutig: `<Ref>#S<stmt>N<ntry>[T<tx>]`.
      Ohne eindeutige Statement-Id -> UNKLAR.
    - Keine AcctSvcrRef -> Positionsschlüssel `EBICS#S<stmt>N<ntry>[T<tx>]`
      (nur bei eindeutiger Statement-Id), sonst keine Identität (bisheriger
      Fingerprint-Weg).

    Die originale AcctSvcrRef bleibt als Herkunft erhalten (Plan und die
    unveränderte `roh_zeile`)."""

    referenz_anzahl: dict[str, int] = {}
    for zeile in zeilen:
        if zeile.roh.native_id:
            referenz_anzahl[zeile.roh.native_id] = referenz_anzahl.get(zeile.roh.native_id, 0) + 1
    ergebnis = []
    for zeile in zeilen:
        original = zeile.roh.native_id
        stmt = zeile.stmt_schluessel
        position = zeile.stmt_position
        if original and referenz_anzahl[original] == 1:
            ergebnis.append(replace(zeile, identitaet_art=ART_ACCTSVCRREF, original_acct_svcr_ref=original))
        elif original and stmt is not None and position is not None:
            ergebnis.append(replace(
                zeile, identitaet_art=ART_ACCTSVCRREF_POSITION, original_acct_svcr_ref=original,
                roh=replace(zeile.roh, native_id=f"{original}{_positions_suffix(stmt, position)}"),
            ))
        elif original:
            ergebnis.append(
                replace(zeile, identitaet_art=ART_REFERENZ_NICHT_EINDEUTIG, original_acct_svcr_ref=original)
            )
        elif stmt is not None and position is not None:
            ergebnis.append(replace(
                zeile, identitaet_art=ART_POSITION, positions_schluessel=f"EBICS{_positions_suffix(stmt, position)}",
            ))
        else:
            ergebnis.append(replace(zeile, identitaet_art=ART_KEINE))
    return ergebnis


def _ntry_status(ntry: ET.Element) -> str | None:
    sts = _direct_child(ntry, "Sts")
    if sts is None:
        return None
    cd = _direct_child(sts, "Cd")
    text = (cd.text if cd is not None else sts.text) or ""
    return text.strip().upper() or None


def _text(element: ET.Element | None, *pfad: str) -> str | None:
    aktuell = element
    for teil in pfad:
        if aktuell is None:
            return None
        aktuell = _direct_child(aktuell, teil)
    if aktuell is None or not (aktuell.text or "").strip():
        return None
    return aktuell.text.strip()


def _lese_dokument(camt_bytes: bytes, iban: str) -> tuple[list[_DateiZeile], dict]:
    if not camt_bytes:
        raise EbicsDokumentAbgelehntError("Leere Datei - abgelehnt.")
    kopf = camt_bytes.upper()
    if b"<!DOCTYPE" in kopf or b"<!ENTITY" in kopf:
        raise EbicsDokumentAbgelehntError("CAMT-Datei mit DOCTYPE/ENTITY-Deklaration - abgelehnt.")
    try:
        root = ET.fromstring(camt_bytes)
    except ET.ParseError as exc:
        raise EbicsDokumentAbgelehntError(f"Ungültiges XML: {exc}") from exc

    namespace = root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else ""
    if _localname(root.tag) != "Document" or not namespace.startswith(_CAMT053_NAMESPACE_PRAEFIX):
        raise EbicsDokumentAbgelehntError(
            f"Kein camt.053-Dokument (Namespace '{namespace or '-'}'); nur finale camt.053-Kontoauszüge werden "
            "übernommen (kein camt.052/054)."
        )
    stmt_root = _direct_children(root, "BkToCstmrStmt")
    if len(stmt_root) != 1:
        raise EbicsDokumentAbgelehntError("camt.053-Dokument ohne genau ein BkToCstmrStmt - abgelehnt.")

    # Erst der aktuelle Parser (Kontoprüfung, Fremdkonten, Sammelbuchungs-
    # Summen, Pflichtfelder, Währung) - seine Fehler werden unverändert
    # weitergereicht.
    rohdaten = parse_camt053(camt_bytes, erwartete_iban=iban)
    stmt_elemente = _pruefe_stmt_konten(root, iban)
    ntry_liste = _pruefe_und_sammle_direkte_ntry(root, stmt_elemente)

    stmt_meta = []
    for stmt in stmt_elemente:
        acct = _direct_child(stmt, "Acct")
        acct_ccy = _text(acct, "Ccy")
        if acct_ccy is not None and acct_ccy.upper() != "EUR":
            raise EbicsDokumentAbgelehntError(f"Kontowährung '{acct_ccy}' - nur EUR wird übernommen.")
        fr_to = _direct_child(stmt, "FrToDt")
        stmt_meta.append(
            {
                "stmt_id": _text(stmt, "Id"),
                "elektronische_sequenz": _text(stmt, "ElctrncSeqNb"),
                "rechtliche_sequenz": _text(stmt, "LglSeqNb"),
                "erstellt_laut_dokument": _text(stmt, "CreDtTm"),
                "zeitraum_von_laut_dokument": _text(fr_to, "FrDtTm"),
                "zeitraum_bis_laut_dokument": _text(fr_to, "ToDtTm"),
                "anzahl_ntry": len(_direct_children(stmt, "Ntry")),
            }
        )

    stmt_ids = [_text(stmt, "Id") for stmt in stmt_elemente]
    ntry_herkunft: dict[int, tuple[str | None, str | None, int]] = {}
    for stmt, stmt_id in zip(stmt_elemente, stmt_ids):
        eindeutig = stmt_id is not None and stmt_ids.count(stmt_id) == 1
        for stmt_pos, ntry in enumerate(_direct_children(stmt, "Ntry"), start=1):
            ntry_herkunft[id(ntry)] = (stmt_id, _stmt_schluessel(stmt_id) if eindeutig else None, stmt_pos)

    zeilen: list[_DateiZeile] = []
    roh_index = 0
    for ntry_index, ntry in enumerate(ntry_liste, start=1):
        stmt_id, stmt_schluessel, stmt_pos = ntry_herkunft[id(ntry)]
        status = _ntry_status(ntry)
        if status != "BOOK":
            raise EbicsDokumentAbgelehntError(
                f"Ntry {ntry_index} hat Status '{status or 'fehlt'}' - nur gebuchte (BOOK) Einträge eines finalen "
                "camt.053 werden übernommen; die gesamte Datei wird abgelehnt."
            )
        amt = _direct_child(ntry, "Amt")
        if amt is None or amt.attrib.get("Ccy") != "EUR":
            raise EbicsDokumentAbgelehntError(
                f"Ntry {ntry_index} ohne ausdrückliche Währung EUR (Amt/@Ccy) - abgelehnt, keine Standardwährung."
            )
        tx_dtls = [el for el in ntry.iter() if _localname(el.tag) == "TxDtls"]
        teilzeilen = max(1, len(tx_dtls))
        ntry_sha = _sha256(ET.tostring(ntry, encoding="utf-8"))
        for teil in range(teilzeilen):
            if roh_index >= len(rohdaten):
                raise EbicsDokumentAbgelehntError("Interne Zuordnung Ntry->Parserzeile inkonsistent - abgelehnt.")
            txdtls_index = (teil + 1) if len(tx_dtls) > 1 else None
            zeilen.append(
                _DateiZeile(
                    position=roh_index + 1, ntry_index=ntry_index, txdtls_index=txdtls_index,
                    ntry_teilzeilen=teilzeilen, ntry_ref=_text(ntry, "NtryRef"), ntry_status=status,
                    ntry_sha256=ntry_sha, roh=rohdaten[roh_index], ntry_acct_svcr_ref=_text(ntry, "AcctSvcrRef"),
                    stmt_id=stmt_id, stmt_schluessel=stmt_schluessel, stmt_position=(stmt_pos, txdtls_index),
                )
            )
            roh_index += 1
    if roh_index != len(rohdaten):
        raise EbicsDokumentAbgelehntError("Interne Zuordnung Ntry->Parserzeile inkonsistent - abgelehnt.")

    gruppenkopf = _direct_child(stmt_root[0], "GrpHdr")
    buchungsdaten = [z.roh.buchungsdatum for z in zeilen]
    dokument = {
        "namespace": namespace,
        "nachricht_id": _text(gruppenkopf, "MsgId"),
        "erstellt_laut_dokument": _text(gruppenkopf, "CreDtTm"),
        "statements": stmt_meta,
        "buchungsdatum_min": min(buchungsdaten).isoformat() if buchungsdaten else None,
        "buchungsdatum_max": max(buchungsdaten).isoformat() if buchungsdaten else None,
        "abdeckung_und_salden": "nicht geprüft (separate Transportdaten)",
    }
    return _bestimme_identitaeten(zeilen), dokument


_QUALIFIZIERTE_REFERENZ = re.compile(r"^(?P<ref>.+)#S(?P<s>[0-9a-f]{12})N(?P<n>\d+)(?:T(?P<t>\d+))?$")
_POSITIONS_SCHLUESSEL = re.compile(r"^EBICS#S(?P<s>[0-9a-f]{12})N(?P<n>\d+)(?:T(?P<t>\d+))?$")


def _position_aus(treffer: re.Match) -> tuple[int, int | None]:
    return int(treffer["n"]), (int(treffer["t"]) if treffer["t"] else None)


def _bestand_identitaet(bank_konto_id: str, row: BankTransaktionTable) -> _BestandIdentitaet:
    """Identität aus der gespeicherten `import_id` (`<konto>:<quelle>:<id>`
    bzw. `<konto>:<quelle>:NOID:<schluessel>`) - quellenunabhängig, damit
    CSV-/CAMT-/frühere EBICS-Zeilen gleich behandelt werden. Statement-/
    Positionsangaben nur aus den von diesem Modul gebildeten CAMT053-
    Schlüsseln; eine UUID-NOID-Zeile hat keine Identität."""

    leer = _BestandIdentitaet(None, None, None, None)
    praefix = f"{bank_konto_id}:"
    if not row.import_id.startswith(praefix):
        return leer
    rest = row.import_id[len(praefix):]
    if ":" not in rest:
        return leer
    quelle, wert = rest.split(":", 1)
    if row.hat_native_id:
        if not wert:
            return leer
        treffer = _QUALIFIZIERTE_REFERENZ.match(wert) if quelle == QUELLE_TYP else None
        if treffer:
            return _BestandIdentitaet(wert, treffer["ref"], treffer["s"], _position_aus(treffer))
        return _BestandIdentitaet(wert, wert, None, None)
    if quelle == QUELLE_TYP and wert.startswith("NOID:"):
        schluessel = wert.removeprefix("NOID:")
        treffer = _POSITIONS_SCHLUESSEL.match(schluessel)
        if treffer:
            return _BestandIdentitaet(schluessel, None, treffer["s"], _position_aus(treffer))
    return leer


def _referenz_norm(text: str | None) -> str:
    return re.sub(r"[^0-9a-z]", "", (text or "").casefold())


def _identitaet(roh: RohTransaktion, bestand: BankTransaktionTable, eigene_iban: str) -> tuple[bool, bool]:
    """Liefert (hinreichend_identisch, widerspruch). Eine gespeicherte
    Gegenkonto-IBAN gleich der EIGENEN Kontokennung (Altparser-Fehler bei
    DBIT) zählt als fehlend, nie als Identitätsbeleg."""

    iban_r = _norm_iban(roh.gegenkonto_iban)
    iban_b = _norm_iban(bestand.gegenkonto_iban)
    if iban_r == eigene_iban:
        iban_r = ""
    if iban_b == eigene_iban:
        iban_b = ""
    iban_gleich = bool(iban_r and iban_b and iban_r == iban_b)
    iban_widerspruch = bool(iban_r and iban_b and iban_r != iban_b)

    ref_r = _referenz_norm(roh.referenz)
    ref_b = _referenz_norm(bestand.referenz)
    ref_gleich = False
    ref_widerspruch = False
    if ref_r and ref_b:
        if ref_r == ref_b:
            ref_gleich = True
        else:
            kurz, lang = sorted((ref_r, ref_b), key=len)
            if len(kurz) >= _REFERENZ_MIN_TEILLAENGE and kurz in lang:
                ref_gleich = True
            else:
                ref_widerspruch = True
    widerspruch = iban_widerspruch or ref_widerspruch
    return (iban_gleich or ref_gleich) and not widerspruch, widerspruch


def _daten(buchungsdatum: date, valuta: date | None) -> list[date]:
    return [buchungsdatum] + ([valuta] if valuta else [])


def _datum_exakt(roh: RohTransaktion, bestand: BankTransaktionTable) -> bool:
    return (
        roh.buchungsdatum == bestand.buchungsdatum
        or (roh.valuta is not None and roh.valuta == bestand.buchungsdatum)
        or (bestand.valuta is not None and roh.buchungsdatum == bestand.valuta)
    )


def _im_fenster(roh: RohTransaktion, bestand: BankTransaktionTable, toleranz: int) -> bool:
    return min(
        abs((a - b).days)
        for a in _daten(roh.buchungsdatum, roh.valuta)
        for b in _daten(bestand.buchungsdatum, bestand.valuta)
    ) <= toleranz


def _erreichbar(ziel: int, werte: list[int]) -> bool:
    """Gibt es eine Teilmenge (>= 1 Element) positiver `werte` mit Summe
    exakt `ziel`? Summen > ziel werden verworfen (alle Werte positiv)."""

    summen: set[int] = set()
    for wert in werte:
        neue = {wert} | {s + wert for s in summen if s + wert <= ziel}
        summen |= neue
        if ziel in summen:
            return True
    return ziel in summen


def _kern_gleich(roh: RohTransaktion, bestand: BankTransaktionTable) -> bool:
    """Wirtschaftlicher Kern bei gleicher native ID: Centbetrag, Währung,
    Datum. Gleiche Quelle (CAMT) -> Buchungsdatum/Valuta strikt; andere
    Quelle (z. B. CSV mit Valuta als Datumsspalte) -> Datum kreuzweise
    Buchung/Valuta exakt, siehe `_datum_exakt`."""

    if roh.betrag_cent != bestand.betrag_cent or roh.waehrung != bestand.waehrung:
        return False
    if bestand.quelle_typ != QUELLE_TYP:
        return _datum_exakt(roh, bestand)
    if roh.buchungsdatum != bestand.buchungsdatum:
        return False
    return not (roh.valuta and bestand.valuta and roh.valuta != bestand.valuta)


def _erstelle_plan(
    session: Session, bank_konto: BankKontoTable, erwartete_iban: str, camt_bytes: bytes, toleranz: int
) -> EbicsIngestPlan:
    if toleranz < 0:
        raise ValueError("datum_toleranz_tage darf nicht negativ sein.")
    eigene_iban = _norm_iban(bank_konto.iban)
    if not eigene_iban or _norm_iban(erwartete_iban) != eigene_iban:
        raise EbicsKontoAbweichungError(
            f"Vom Transport erwartete IBAN passt nicht zum Bankkonto {bank_konto.id} in der Datenbank - abgelehnt."
        )
    zeilen, dokument = _lese_dokument(camt_bytes, bank_konto.iban)

    bestand = list(
        session.execute(
            select(BankTransaktionTable)
            .where(BankTransaktionTable.bank_konto_id == bank_konto.id)
            .order_by(BankTransaktionTable.id)
        ).scalars().all()
    )
    bestand_ident = {row.id: _bestand_identitaet(bank_konto.id, row) for row in bestand}
    nach_native: dict[str, list[BankTransaktionTable]] = {}
    nach_positionsschluessel: dict[str, list[BankTransaktionTable]] = {}
    nach_stmt_position: dict[tuple[str, tuple[int, int | None]], list[BankTransaktionTable]] = {}
    for row in bestand:
        ident = bestand_ident[row.id]
        if ident.schluessel is not None:
            ziel = nach_native if row.hat_native_id else nach_positionsschluessel
            ziel.setdefault(ident.schluessel, []).append(row)
        if ident.stmt_schluessel is not None and ident.stmt_position is not None:
            nach_stmt_position.setdefault((ident.stmt_schluessel, ident.stmt_position), []).append(row)

    ergebnis: dict[int, tuple[str, str, str, tuple[int, ...]]] = {}
    verbraucht: set[int] = set()

    def _setze(position: int, status: str, code: str, grund: str, ids=()) -> None:
        ergebnis[position] = (status, code, grund, tuple(sorted(set(ids))))

    # 1. belegte Bankidentität: native ID (quellenübergreifend), qualifizierte
    #    Statement-/Positions-Identität und gleiche Originalposition
    for zeile in zeilen:
        art = zeile.identitaet_art
        if art == ART_REFERENZ_NICHT_EINDEUTIG:
            _setze(zeile.position, STATUS_UNKLAR, GRUND_REFERENZ_NICHT_EINDEUTIG,
                   f"AcctSvcrRef '{zeile.original_acct_svcr_ref}' kommt mehrfach in der Datei vor und das Statement "
                   "hat keine eindeutige Id - keine belastbare Einzelidentität, zurückgehalten (kein Raten).")
            continue
        if art in (ART_ACCTSVCRREF, ART_ACCTSVCRREF_POSITION):
            treffer = nach_native.get(zeile.roh.native_id, [])
        elif art == ART_POSITION:
            treffer = nach_positionsschluessel.get(zeile.positions_schluessel, [])
        else:
            treffer = []
        if treffer:
            ids = [row.id for row in treffer]
            eigener_hash = inhalt_hash(bank_konto.id, zeile.roh)
            widersprueche = []
            for row in treffer:
                if row.quelle_hash == eigener_hash:
                    continue
                if art != ART_ACCTSVCRREF:
                    widersprueche.append(row.id)  # zusammengesetzte Originalidentität: strikt
                    continue
                _ident, widerspruch = _identitaet(zeile.roh, row, eigene_iban)
                if not _kern_gleich(zeile.roh, row) or widerspruch:
                    widersprueche.append(row.id)
            if widersprueche:
                _setze(zeile.position, STATUS_KONFLIKT, GRUND_NATIVE_ID_WIDERSPRUCH,
                       f"Bankidentität '{zeile.roh.native_id or zeile.positions_schluessel}' existiert bereits mit "
                       f"widersprechendem Inhalt (Transaktion(en) {sorted(widersprueche)}).", ids)
            else:
                quellen = sorted({row.quelle_typ for row in treffer})
                _setze(zeile.position, STATUS_VORHANDEN, GRUND_NATIVE_ID_GLEICH,
                       f"Bankidentität '{zeile.roh.native_id or zeile.positions_schluessel}' bereits vorhanden "
                       f"(Quelle {', '.join(quellen)}), Inhalt übereinstimmend.", ids)
            verbraucht.update(ids)
            continue
        if zeile.stmt_schluessel is not None:
            gleiche_position = nach_stmt_position.get((zeile.stmt_schluessel, zeile.stmt_position), [])
            if gleiche_position:
                ids = [row.id for row in gleiche_position]
                _setze(zeile.position, STATUS_KONFLIKT, GRUND_ORIGINALPOSITION_WIDERSPRUCH,
                       f"Statement '{zeile.stmt_id}' Position {_positions_text(zeile.stmt_position)} ist bereits "
                       f"mit anderer Identität/anderem Inhalt gespeichert (Transaktion(en) {sorted(ids)}) - "
                       "geänderter Beleg unter gleicher Originalidentität.", ids)
                verbraucht.update(ids)

    offen = [z for z in zeilen if z.position not in ergebnis]
    verfuegbar = [row for row in bestand if row.id not in verbraucht]

    def _beziehung(zeile: _DateiZeile, row: BankTransaktionTable) -> str | None:
        """GETRENNT: dieselbe originale Statement-Id, andere Position ->
        belegt eine eigenständige Buchung. ANDERE_BANKID: beide tragen eine
        CAMT-AcctSvcrRef, die sich unterscheidet -> nie stillschweigend
        dieselbe Zahlung."""

        ident = bestand_ident[row.id]
        if zeile.stmt_schluessel is not None and ident.stmt_schluessel == zeile.stmt_schluessel:
            return BEZIEHUNG_GETRENNT
        if (
            zeile.original_acct_svcr_ref and ident.original_acct_svcr_ref and row.quelle_typ == QUELLE_TYP
            and zeile.original_acct_svcr_ref != ident.original_acct_svcr_ref
        ):
            return BEZIEHUNG_ANDERE_BANKID
        return None

    # 2. ausdrücklicher Exact-Match, nur eindeutig (Komponentenanalyse)
    kanten: dict[int, set[int]] = {}
    for zeile in offen:
        zeilen_kanten: set[int] = set()
        andere_bankid: set[int] = set()
        for row in verfuegbar:
            if row.betrag_cent != zeile.roh.betrag_cent or row.waehrung != zeile.roh.waehrung:
                continue
            if not _datum_exakt(zeile.roh, row):
                continue
            identisch, _widerspruch = _identitaet(zeile.roh, row, eigene_iban)
            if not identisch:
                continue
            beziehung = _beziehung(zeile, row)
            if beziehung == BEZIEHUNG_GETRENNT:
                continue
            if beziehung == BEZIEHUNG_ANDERE_BANKID:
                andere_bankid.add(row.id)
                continue
            zeilen_kanten.add(row.id)
        if andere_bankid:
            _setze(zeile.position, STATUS_UNKLAR, GRUND_ANDERE_BANKIDENTITAET,
                   f"Wirtschaftlich gleich aussehend wie bestehende Transaktion(en) {sorted(andere_bankid)}, aber mit "
                   f"anderer Bankreferenz (AcctSvcrRef '{zeile.original_acct_svcr_ref}') - weder dieselbe Zahlung "
                   "noch sicher neu, zurückgehalten.", andere_bankid | zeilen_kanten)
        elif zeilen_kanten:
            kanten[zeile.position] = zeilen_kanten
    eltern: dict[tuple[str, int], tuple[str, int]] = {}

    def _finde(knoten):
        while eltern.setdefault(knoten, knoten) != knoten:
            eltern[knoten] = eltern[eltern[knoten]]
            knoten = eltern[knoten]
        return knoten

    for position, ids in kanten.items():
        for tid in ids:
            eltern[_finde(("d", tid))] = _finde(("f", position))
    komponenten: dict[tuple[str, int], tuple[set[int], set[int]]] = {}
    for position, ids in kanten.items():
        wurzel = _finde(("f", position))
        dateiseite, bestandsseite = komponenten.setdefault(wurzel, (set(), set()))
        dateiseite.add(position)
        bestandsseite.update(ids)
    for dateiseite, bestandsseite in komponenten.values():
        vollstaendig = len(dateiseite) == len(bestandsseite) and all(
            kanten[p] == bestandsseite for p in dateiseite
        )
        if vollstaendig:
            for position, tid in zip(sorted(dateiseite), sorted(bestandsseite)):
                _setze(position, STATUS_VORHANDEN, GRUND_WIRTSCHAFTLICH_IDENTISCH,
                       f"Wirtschaftlich identisch mit bestehender Transaktion {tid} (Datum, Centbetrag, Währung, "
                       "Referenz/Gegenkonto) - nicht erneut geschrieben.", [tid])
            verbraucht.update(bestandsseite)
        else:
            for position in dateiseite:
                _setze(position, STATUS_UNKLAR, GRUND_MEHRDEUTIG,
                       f"Mehrere mögliche identische Bestandszeilen/Dateizeilen ({len(dateiseite)} Datei, "
                       f"{len(bestandsseite)} Bestand) - keine eindeutige 1:1-Zuordnung, zurückgehalten.",
                       bestandsseite)

    offen = [z for z in zeilen if z.position not in ergebnis]
    verfuegbar = [row for row in bestand if row.id not in verbraucht]

    # 3. ununterscheidbare Zeilen ohne jede belegte Identität (weder native
    #    ID noch eindeutige Statement-Id/Position) innerhalb der Datei
    nach_fingerprint: dict[str, list[_DateiZeile]] = {}
    for zeile in offen:
        if zeile.identitaet_art == ART_KEINE:
            nach_fingerprint.setdefault(_fingerprint(bank_konto.id, zeile.roh), []).append(zeile)
    for gruppe in nach_fingerprint.values():
        if len(gruppe) > 1:
            for zeile in gruppe:
                _setze(zeile.position, STATUS_UNKLAR, GRUND_DATEI_INTERN,
                       f"{len(gruppe)} Dateizeilen ohne native ID und ohne eindeutige Statement-Id mit identischem "
                       "Datum/Betrag/Referenz/Gegenkonto - Doppelausweis oder zwei Zahlungen nicht unterscheidbar, "
                       "zurückgehalten.")

    offen = [z for z in zeilen if z.position not in ergebnis]

    # 4. Sammelbuchung / Zerlegung / Gleichbetrag - Gründe sammeln, dann setzen
    gruende: dict[int, list[tuple[str, str, set[int]]]] = {}

    def _merke(position: int, code: str, grund: str, ids) -> None:
        gruende.setdefault(position, []).append((code, grund, set(ids)))

    for row in verfuegbar:
        if row.betrag_cent == 0:
            continue
        vorzeichen = 1 if row.betrag_cent > 0 else -1
        ziel = abs(row.betrag_cent)
        kandidaten = [
            z for z in offen
            if z.roh.waehrung == row.waehrung and z.roh.betrag_cent * vorzeichen > 0
            and abs(z.roh.betrag_cent) < ziel and _im_fenster(z.roh, row, toleranz)
            and _beziehung(z, row) != BEZIEHUNG_GETRENNT
        ]
        if len(kandidaten) < 2:
            continue
        if len(kandidaten) > MAX_SUMMEN_KANDIDATEN:
            for z in kandidaten:
                _merke(z.position, GRUND_SUMMENPRUEFUNG_ZU_GROSS,
                       f"Bestehende Transaktion {row.id} könnte eine Sammelbuchung dieser Zeilen sein; zu viele "
                       f"Kandidaten ({len(kandidaten)}) für eine sichere Summenprüfung - zurückgehalten.", [row.id])
            continue
        betraege = [abs(z.roh.betrag_cent) for z in kandidaten]
        for i, z in enumerate(kandidaten):
            andere = betraege[:i] + betraege[i + 1:]
            if _erreichbar(ziel - betraege[i], andere):
                _merke(z.position, GRUND_SAMMELBUCHUNG,
                       f"Bestehende Transaktion {row.id} ({row.betrag_cent} Cent, {row.buchungsdatum.isoformat()}) "
                       "entspricht der Summe mehrerer Dateizeilen inkl. dieser - möglicher Sammelkopf, Zerlegung "
                       "ungeklärt, zurückgehalten.", [row.id])

    for zeile in offen:
        roh = zeile.roh
        if roh.betrag_cent == 0:
            continue
        vorzeichen = 1 if roh.betrag_cent > 0 else -1
        ziel = abs(roh.betrag_cent)
        teile = [
            row for row in verfuegbar
            if row.waehrung == roh.waehrung and row.betrag_cent * vorzeichen > 0
            and abs(row.betrag_cent) < ziel and _im_fenster(roh, row, toleranz)
            and _beziehung(zeile, row) != BEZIEHUNG_GETRENNT
        ]
        if len(teile) < 2:
            continue
        if len(teile) > MAX_SUMMEN_KANDIDATEN:
            _merke(zeile.position, GRUND_SUMMENPRUEFUNG_ZU_GROSS,
                   f"Zu viele bestehende Teilbeträge ({len(teile)}) für eine sichere Prüfung auf bereits "
                   "importierte Einzelposten - zurückgehalten.", [row.id for row in teile])
        elif _erreichbar(ziel, [abs(row.betrag_cent) for row in teile]):
            _merke(zeile.position, GRUND_EINZELPOSTEN,
                   "Betrag entspricht der Summe mehrerer bestehender Transaktionen im Datumsfenster - mögliche "
                   "bereits importierte Einzelposten dieser Sammelbuchung, zurückgehalten.",
                   [row.id for row in teile])

    for zeile in offen:
        gleich = [
            row.id for row in verfuegbar
            if row.betrag_cent == zeile.roh.betrag_cent and row.waehrung == zeile.roh.waehrung
            and _im_fenster(zeile.roh, row, toleranz) and _beziehung(zeile, row) is None
        ]
        if gleich:
            _merke(zeile.position, GRUND_GLEICHBETRAG,
                   f"Bestehende Transaktion(en) {gleich} mit gleichem Betrag im Datumsfenster (±{toleranz} Tage), "
                   "aber ohne ausreichende Identität (Datum/Referenz/Gegenkonto) - weder sichere Dublette noch "
                   "sichere neue Zahlung, zurückgehalten.", gleich)

    for position, liste in gruende.items():
        ids: set[int] = set()
        for _code, _grund, teil_ids in liste:
            ids |= teil_ids
        _setze(position, STATUS_UNKLAR, liste[0][0], " | ".join(g for _c, g, _i in liste), ids)

    for zeile in zeilen:
        if zeile.position not in ergebnis:
            _setze(zeile.position, STATUS_NEU, GRUND_NEU, "Kein übereinstimmender Bestand - wird neu importiert.")

    eintraege = tuple(
        PlanEintrag(
            position=z.position, ntry_index=z.ntry_index, txdtls_index=z.txdtls_index,
            ntry_teilzeilen=z.ntry_teilzeilen, ntry_ref=z.ntry_ref, ntry_status=z.ntry_status,
            ntry_sha256=z.ntry_sha256, status=ergebnis[z.position][0], grund_code=ergebnis[z.position][1],
            grund=ergebnis[z.position][2], bestehende_transaktion_ids=ergebnis[z.position][3],
            identitaet_art=z.identitaet_art, original_acct_svcr_ref=z.original_acct_svcr_ref,
            ntry_acct_svcr_ref=z.ntry_acct_svcr_ref, stmt_id=z.stmt_id, stmt_position=z.stmt_position,
            positions_schluessel=z.positions_schluessel, roh=z.roh,
        )
        for z in zeilen
    )
    snapshot = _bankbestand_snapshot(bestand)
    quelle_sha = _sha256(camt_bytes)
    kern = dict(
        regelversion=REGELVERSION, bank_konto_id=bank_konto.id, gesellschaft_id=bank_konto.gesellschaft_id,
        iban=eigene_iban, quelle_sha256=quelle_sha, quelle_bytes=len(camt_bytes), datum_toleranz_tage=toleranz,
        dokument=dokument, eintraege=[e.to_dict() for e in eintraege], bankbestand_snapshot=snapshot,
    )
    return EbicsIngestPlan(
        regelversion=REGELVERSION, bank_konto_id=bank_konto.id, gesellschaft_id=bank_konto.gesellschaft_id,
        iban=eigene_iban, quelle_sha256=quelle_sha, quelle_bytes=len(camt_bytes), datum_toleranz_tage=toleranz,
        dokument=dokument, eintraege=eintraege, bankbestand_snapshot=snapshot, plan_hash=_kanonisch_hash(kern),
    )


def _bankbestand_snapshot(bestand: list[BankTransaktionTable]) -> dict:
    """Bindet den Plan an den exakten Bankbestand des Kontos: jede neue,
    gelöschte oder geänderte Zeile ändert den Snapshot und damit den
    Planhash."""

    zeilen = [
        [row.id, row.import_id, row.quelle_typ, row.quelle_hash, row.betrag_cent, row.waehrung,
         row.buchungsdatum.isoformat(), row.valuta.isoformat() if row.valuta else None]
        for row in bestand
    ]
    return {
        "anzahl": len(bestand),
        "summe_cent": sum(row.betrag_cent for row in bestand),
        "max_id": max((row.id for row in bestand), default=None),
        "inhalt_sha256": _sha256(json.dumps(zeilen, separators=(",", ":")).encode("utf-8")),
    }
