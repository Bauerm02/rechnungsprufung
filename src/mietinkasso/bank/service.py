"""Bankabgleich: Import, automatische/manuelle Zuordnung, Rücklastschrift,
Bankvollständigkeit. Fachregel 4: Zahlung und Zuordnung sind getrennte
Schritte; automatisch wird nur bei eindeutiger Referenz UND eindeutigem
Konto zugeordnet, niemals allein über Namensgleichheit oder gleichen
Betrag. Jede Buchung/Zuordnung wird VOR dem eigentlichen Verbuchen der
OP-Zeile vollständig validiert (Gesellschaft, Betrag, Währung), damit
eine fehlgeschlagene Prüfung nie eine Ledger-Nebenwirkung hinterlässt."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import date

from mietinkasso.auth.service import AuthContext, require_gesellschaft_access, require_schreibrecht
from mietinkasso.bank.importer import CsvSpaltenMapping, RohTransaktion, parse_camt053, parse_csv
from mietinkasso.bank.repository import BankRepository
from mietinkasso.domain.enums import OPTyp, ZahlungsMatchTyp
from mietinkasso.domain.exceptions import (
    BindungInkonsistentError,
    CrossTenantError,
    FremdwaehrungNichtUnterstuetztError,
    MehrfachbuchungsKonfliktError,
    ZuordnungUngueltigError,
)
from mietinkasso.infrastructure.db.tables import BankKontoTable, BankTransaktionTable, KontoTable, OPPositionTable
from mietinkasso.op.service import OPService
from mietinkasso.stammdaten.repository import StammdatenRepository

_VERTRAG_REFERENZ = re.compile(r"VERTRAG:([A-Za-z0-9\-]+)")


def _hash(fields: dict) -> str:
    canonical = json.dumps(fields, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _fingerprint(bank_konto_id: str, roh: RohTransaktion) -> str:
    """Formatierungsunabhängiger, konto-gescopter Fingerabdruck der
    wirtschaftlich relevanten Felder - für Zeilen OHNE bankseitig
    eindeutige Kennung. Zeilennummern/Roh-Layout fließen bewusst NICHT
    ein, damit ein überlappender Re-Export mit anderer Zeilenreihenfolge
    dieselbe Zahlung wiedererkennbar macht."""

    return _hash(
        {
            "bank_konto_id": bank_konto_id,
            "buchungsdatum": str(roh.buchungsdatum),
            "betrag_cent": roh.betrag_cent,
            "waehrung": roh.waehrung,
            "referenz": (roh.referenz or "").strip().lower(),
            "gegenkonto_iban": (roh.gegenkonto_iban or "").strip().upper(),
        }
    )


@dataclass(frozen=True)
class ZuordnungsErgebnis:
    zugeordnet: bool
    grund: str
    zuordnung_id: int | None = None
    op_position_id: int | None = None


class BankImportService:
    def __init__(
        self,
        repository: BankRepository,
        stammdaten_repository: StammdatenRepository,
        op_service: OPService,
    ):
        self._repository = repository
        self._stammdaten_repository = stammdaten_repository
        self._op_service = op_service

    # -- Import -----------------------------------------------------------
    def importiere_camt053(
        self, *, ctx: AuthContext, bank_konto: BankKontoTable, xml_bytes: bytes
    ) -> list[BankTransaktionTable]:
        require_gesellschaft_access(ctx, bank_konto.gesellschaft_id)
        require_schreibrecht(ctx)
        rohdaten = parse_camt053(xml_bytes)
        return [self._speichere_roh(bank_konto, roh, "CAMT053") for roh in rohdaten]

    def importiere_csv(
        self, *, ctx: AuthContext, bank_konto: BankKontoTable, text: str, mapping: CsvSpaltenMapping
    ) -> list[BankTransaktionTable]:
        require_gesellschaft_access(ctx, bank_konto.gesellschaft_id)
        require_schreibrecht(ctx)
        rohdaten = parse_csv(text, mapping)
        return [self._speichere_roh(bank_konto, roh, "CSV") for roh in rohdaten]

    def _speichere_roh(self, bank_konto: BankKontoTable, roh: RohTransaktion, quelle_typ: str) -> BankTransaktionTable:
        content_hash = _hash(
            {
                "bank_konto_id": bank_konto.id,
                "betrag_cent": roh.betrag_cent,
                "waehrung": roh.waehrung,
                "buchungsdatum": str(roh.buchungsdatum),
                "valuta": str(roh.valuta) if roh.valuta else None,
                "referenz": roh.referenz,
                "gegenkonto_iban": roh.gegenkonto_iban,
                "gegenkonto_name": roh.gegenkonto_name,
                "native_id": roh.native_id,
            }
        )
        if roh.native_id:
            hat_native_id = True
            fingerprint_hash = None
            import_id = f"{bank_konto.id}:{quelle_typ}:{roh.native_id}"
        else:
            hat_native_id = False
            fingerprint_hash = _fingerprint(bank_konto.id, roh)
            bestehende = self._repository.find_by_fingerprint(bank_konto.id, fingerprint_hash)
            if bestehende is not None:
                raise MehrfachbuchungsKonfliktError(
                    f"Banktransaktion ohne bankseitig eindeutige Kennung auf Bankkonto {bank_konto.id}: "
                    f"Betrag/Datum/Referenz sind identisch zur bereits importierten Transaktion "
                    f"#{bestehende.id}. Das kann dieselbe Zahlung aus einem überlappenden Export sein ODER "
                    f"eine zweite, echte Zahlung mit zufällig identischen Merkmalen - das lässt sich nicht "
                    f"automatisch entscheiden und wird daher NICHT still zusammengelegt oder dupliziert, "
                    f"sondern zur manuellen Klärung verweigert."
                )
            # Bewusst kein wiederverwendbarer Schlüssel: ohne native ID kann
            # ein Replay nicht von einer echten zweiten Zahlung
            # unterschieden werden, daher entscheidet ausschließlich der
            # obige Fingerprint-Konflikt, nicht die import_id-Idempotenz.
            import_id = f"{bank_konto.id}:{quelle_typ}:NOID:{uuid.uuid4()}"

        row = BankTransaktionTable(
            bank_konto_id=bank_konto.id,
            betrag_cent=roh.betrag_cent,
            waehrung=roh.waehrung,
            buchungsdatum=roh.buchungsdatum,
            valuta=roh.valuta,
            referenz=roh.referenz,
            gegenkonto_iban=roh.gegenkonto_iban,
            gegenkonto_name=roh.gegenkonto_name,
            quelle_typ=quelle_typ,
            quelle_hash=content_hash,
            import_id=import_id,
            hat_native_id=hat_native_id,
            fingerprint_hash=fingerprint_hash,
            roh_zeile=roh.roh_zeile,
        )
        return self._repository.insert_transaktion_idempotent(row)

    # -- Validierung (immer VOR jeder OP-Buchung) ----------------------------
    def _pruefe_vor_buchung(self, *, transaktion: BankTransaktionTable, konto: KontoTable, betrag_cent: int) -> None:
        bank_konto = self._repository.get_bank_konto(transaktion.bank_konto_id)
        if bank_konto is None:
            raise ValueError(f"Unbekanntes Bankkonto {transaktion.bank_konto_id}")
        if bank_konto.gesellschaft_id != konto.gesellschaft_id:
            raise CrossTenantError(
                f"Banktransaktion (Gesellschaft {bank_konto.gesellschaft_id}) darf nicht mit Konto "
                f"{konto.id} (Gesellschaft {konto.gesellschaft_id}) verrechnet werden."
            )
        if betrag_cent <= 0:
            raise ZuordnungUngueltigError("Zuordnungsbetrag muss positiv sein.")
        if transaktion.betrag_cent <= 0:
            raise ZuordnungUngueltigError(
                "Nur Zahlungseingänge (positiver Transaktionsbetrag) können regulär zugeordnet werden."
            )
        if transaktion.waehrung != konto.waehrung:
            raise FremdwaehrungNichtUnterstuetztError(
                f"Transaktion {transaktion.id} ({transaktion.waehrung}) und Konto {konto.id} "
                f"({konto.waehrung}) haben unterschiedliche Währungen."
            )
        bereits_zugeordnet = self._repository.zugeordneter_betrag(transaktion.id)
        verbleibend = transaktion.betrag_cent - bereits_zugeordnet
        if betrag_cent > verbleibend:
            raise ZuordnungUngueltigError(
                f"Zuordnungsbetrag {betrag_cent} überschreitet den verbleibenden, noch nicht zugeordneten "
                f"Betrag der Transaktion {transaktion.id} ({verbleibend} von {transaktion.betrag_cent})."
            )

    # -- Zuordnung ----------------------------------------------------------
    def automatisch_zuordnen(self, *, ctx: AuthContext, transaktion: BankTransaktionTable) -> ZuordnungsErgebnis:
        if transaktion.betrag_cent <= 0:
            return ZuordnungsErgebnis(False, "Nur Zahlungseingänge (positiver Betrag) werden automatisch zugeordnet.")
        if not transaktion.referenz:
            return ZuordnungsErgebnis(False, "Keine Referenz vorhanden; nur manuelle Zuordnung möglich.")
        treffer = _VERTRAG_REFERENZ.search(transaktion.referenz)
        if not treffer:
            return ZuordnungsErgebnis(
                False, "Referenz enthält keine eindeutige Vertragskennung; Name/Betrag allein reichen nicht."
            )
        vertrag_id = treffer.group(1)
        konto = self._stammdaten_repository.get_konto_by_vertrag(vertrag_id)
        if konto is None:
            return ZuordnungsErgebnis(False, f"Referenzierter Vertrag {vertrag_id} hat kein Konto.")

        verbleibend = transaktion.betrag_cent - self._repository.zugeordneter_betrag(transaktion.id)
        if verbleibend <= 0:
            return ZuordnungsErgebnis(False, "Transaktion ist bereits vollständig zugeordnet.")
        try:
            self._pruefe_vor_buchung(transaktion=transaktion, konto=konto, betrag_cent=verbleibend)
        except (CrossTenantError, ZuordnungUngueltigError, FremdwaehrungNichtUnterstuetztError) as exc:
            return ZuordnungsErgebnis(False, str(exc))

        op_row = self._op_service.buchen(
            ctx=ctx,
            konto=konto,
            typ=OPTyp.ZAHLUNG,
            betrag_cent=verbleibend,
            belegdatum=transaktion.buchungsdatum,
            buchungsdatum=transaktion.buchungsdatum,
            faelligkeit=None,
            beleg_referenz=f"Bankzahlung {transaktion.referenz}",
            import_id=f"ZAHLUNG-BANK-{transaktion.id}",
            quelle_system="bank_auto_match",
        )
        zuordnung = self._repository.create_zuordnung(
            bank_transaktion_id=transaktion.id,
            op_position_id=op_row.id,
            betrag_cent=verbleibend,
            match_typ=ZahlungsMatchTyp.AUTOMATISCH_EINDEUTIG.value,
        )
        return ZuordnungsErgebnis(True, "Eindeutige Vertragsreferenz gefunden.", zuordnung.id, op_row.id)

    def zuordnen_manuell(
        self, *, ctx: AuthContext, transaktion: BankTransaktionTable, konto: KontoTable, betrag_cent: int, beleg_referenz: str
    ):
        require_gesellschaft_access(ctx, konto.gesellschaft_id)
        require_schreibrecht(ctx)
        self._pruefe_vor_buchung(transaktion=transaktion, konto=konto, betrag_cent=betrag_cent)

        op_row = self._op_service.buchen(
            ctx=ctx,
            konto=konto,
            typ=OPTyp.ZAHLUNG,
            betrag_cent=betrag_cent,
            belegdatum=transaktion.buchungsdatum,
            buchungsdatum=transaktion.buchungsdatum,
            faelligkeit=None,
            beleg_referenz=beleg_referenz,
            import_id=f"ZAHLUNG-BANK-{transaktion.id}-MANUELL-{konto.id}-{betrag_cent}",
            quelle_system="bank_manuell",
        )
        return self._repository.create_zuordnung(
            bank_transaktion_id=transaktion.id,
            op_position_id=op_row.id,
            betrag_cent=betrag_cent,
            match_typ=ZahlungsMatchTyp.MANUELL.value,
        )

    def verarbeite_ruecklastschrift(
        self,
        *,
        ctx: AuthContext,
        transaktion: BankTransaktionTable,
        original_op_position: OPPositionTable,
        konto: KontoTable,
        betrag_cent: int | None = None,
    ) -> OPPositionTable:
        """Bucht eine Rücklastschrift als eigene RUECKLASTSCHRIFT-Zeile.
        Prüft Original-Zuordnung, Konto-Zugehörigkeit, Vorzeichen/Typ und
        erlaubt einen Teilbetrag (z. B. wenn nur ein Teil einer
        Sammelzahlung zurückgebucht wurde), begrenzt aber auf die
        ursprünglich zugeordnete Zahlungshöhe."""

        require_gesellschaft_access(ctx, konto.gesellschaft_id)
        require_schreibrecht(ctx)

        if original_op_position.konto_id != konto.id:
            raise BindungInkonsistentError(
                f"OPPosition {original_op_position.id} gehört zu Konto {original_op_position.konto_id}, "
                f"nicht zu {konto.id}."
            )
        if OPTyp(original_op_position.typ) is not OPTyp.ZAHLUNG:
            raise ZuordnungUngueltigError(
                f"Rücklastschrift muss sich auf eine Zahlung beziehen, OPPosition {original_op_position.id} "
                f"ist aber vom Typ {original_op_position.typ}."
            )
        if original_op_position.status != "AKTIV":
            raise ZuordnungUngueltigError(
                f"OPPosition {original_op_position.id} ist bereits storniert und kann nicht zurückgebucht werden."
            )
        zuordnungen = self._repository.list_zuordnungen_fuer_op(original_op_position.id)
        if not zuordnungen:
            raise ZuordnungUngueltigError(
                f"Zur ursprünglichen Zahlung {original_op_position.id} existiert keine Bank-Zuordnung; "
                "eine Rücklastschrift ohne belegte Ursprungszahlung wird abgelehnt."
            )

        ursprungsbetrag = abs(original_op_position.betrag_cent)
        effektiver_betrag = betrag_cent if betrag_cent is not None else ursprungsbetrag
        if effektiver_betrag <= 0:
            raise ZuordnungUngueltigError("Rücklastschriftbetrag muss positiv sein.")
        if effektiver_betrag > ursprungsbetrag:
            raise ZuordnungUngueltigError(
                f"Rücklastschriftbetrag {effektiver_betrag} übersteigt die ursprüngliche Zahlung "
                f"({ursprungsbetrag})."
            )
        if transaktion.waehrung != konto.waehrung:
            raise FremdwaehrungNichtUnterstuetztError(
                f"Rücklastschrift-Transaktion {transaktion.id} ({transaktion.waehrung}) und Konto {konto.id} "
                f"({konto.waehrung}) haben unterschiedliche Währungen."
            )

        return self._op_service.buchen(
            ctx=ctx,
            konto=konto,
            typ=OPTyp.RUECKLASTSCHRIFT,
            betrag_cent=effektiver_betrag,
            belegdatum=transaktion.buchungsdatum,
            buchungsdatum=transaktion.buchungsdatum,
            faelligkeit=transaktion.buchungsdatum,
            beleg_referenz=f"Rücklastschrift zu OP #{original_op_position.id}",
            import_id=f"RUECKLASTSCHRIFT-{transaktion.id}-{original_op_position.id}",
            quelle_system="bank_ruecklastschrift",
        )

    # -- Bankvollständigkeit -------------------------------------------------
    def bankstand_alter_tage(self, bank_konto_id: str, *, heute: date | None = None) -> int | None:
        letztes = self._repository.letztes_buchungsdatum(bank_konto_id)
        if letztes is None:
            return None
        return ((heute or date.today()) - letztes).days
