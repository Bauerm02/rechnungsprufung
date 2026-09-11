"""Bankabgleich: Import, automatische/manuelle Zuordnung, Rücklastschrift,
Bankvollständigkeit. Fachregel 4: Zahlung und Zuordnung sind getrennte
Schritte; automatisch wird nur bei eindeutiger Referenz UND eindeutigem
Konto zugeordnet, niemals allein über Namensgleichheit oder gleichen
Betrag."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from mietinkasso.auth.service import AuthContext, require_gesellschaft_access, require_schreibrecht
from mietinkasso.bank.importer import CsvSpaltenMapping, RohTransaktion, parse_camt053, parse_csv
from mietinkasso.bank.repository import BankRepository
from mietinkasso.domain.enums import OPTyp, ZahlungsMatchTyp
from mietinkasso.infrastructure.db.tables import BankKontoTable, BankTransaktionTable, OPPositionTable
from mietinkasso.op.service import OPService
from mietinkasso.stammdaten.repository import StammdatenRepository

_VERTRAG_REFERENZ = re.compile(r"VERTRAG:([A-Za-z0-9\-]+)")


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
            quelle_hash=roh.quelle_hash,
            import_id=roh.import_id,
            roh_zeile=roh.roh_zeile,
        )
        return self._repository.insert_transaktion_idempotent(row)

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
        bank_konto = self._repository.get_bank_konto(transaktion.bank_konto_id)
        if bank_konto is None or bank_konto.gesellschaft_id != konto.gesellschaft_id:
            return ZuordnungsErgebnis(False, "Bankkonto und Ziel-Konto gehören zu unterschiedlichen Gesellschaften.")

        op_row = self._op_service.buchen(
            ctx=ctx,
            konto=konto,
            typ=OPTyp.ZAHLUNG,
            betrag_cent=transaktion.betrag_cent,
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
            betrag_cent=transaktion.betrag_cent,
            match_typ=ZahlungsMatchTyp.AUTOMATISCH_EINDEUTIG.value,
        )
        return ZuordnungsErgebnis(True, "Eindeutige Vertragsreferenz gefunden.", zuordnung.id, op_row.id)

    def zuordnen_manuell(
        self, *, ctx: AuthContext, transaktion: BankTransaktionTable, konto, betrag_cent: int, beleg_referenz: str
    ):
        require_gesellschaft_access(ctx, konto.gesellschaft_id)
        require_schreibrecht(ctx)
        op_row = self._op_service.buchen(
            ctx=ctx,
            konto=konto,
            typ=OPTyp.ZAHLUNG,
            betrag_cent=betrag_cent,
            belegdatum=transaktion.buchungsdatum,
            buchungsdatum=transaktion.buchungsdatum,
            faelligkeit=None,
            beleg_referenz=beleg_referenz,
            import_id=f"ZAHLUNG-BANK-{transaktion.id}-MANUELL-{konto.id}",
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
        konto,
    ) -> OPPositionTable:
        """Bucht eine Rücklastschrift als eigene RUECKLASTSCHRIFT-Zeile in
        Höhe der ursprünglich zugeordneten Zahlung. Der OP wird dadurch
        wieder offen, ohne die ursprüngliche Zahlungszeile zu verändern."""

        require_gesellschaft_access(ctx, konto.gesellschaft_id)
        require_schreibrecht(ctx)
        betrag_cent = abs(original_op_position.betrag_cent)
        return self._op_service.buchen(
            ctx=ctx,
            konto=konto,
            typ=OPTyp.RUECKLASTSCHRIFT,
            betrag_cent=betrag_cent,
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
