from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.domain.exceptions import (
    CrossTenantError,
    FremdwaehrungNichtUnterstuetztError,
    ImportConflictError,
    ZuordnungUngueltigError,
)
from mietinkasso.infrastructure.db.tables import (
    BankKontoTable,
    BankTransaktionTable,
    KontoTable,
    OPPositionTable,
    ZuordnungTable,
)


class BankRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def upsert_bank_konto(self, *, id: str, gesellschaft_id: str, iban: str, bezeichnung: str) -> None:
        with self._session_factory() as session:
            row = session.get(BankKontoTable, id)
            if row is None:
                session.add(BankKontoTable(id=id, gesellschaft_id=gesellschaft_id, iban=iban, bezeichnung=bezeichnung))
            else:
                row.gesellschaft_id = gesellschaft_id
                row.iban = iban
                row.bezeichnung = bezeichnung
            session.commit()

    def get_bank_konto(self, id: str) -> BankKontoTable | None:
        with self._session_factory() as session:
            return session.get(BankKontoTable, id)

    def insert_transaktion_idempotent(self, row: BankTransaktionTable) -> BankTransaktionTable:
        with self._session_factory() as session:
            existing = session.execute(
                select(BankTransaktionTable).where(BankTransaktionTable.import_id == row.import_id)
            ).scalar_one_or_none()
            if existing is not None:
                if existing.quelle_hash != row.quelle_hash:
                    raise ImportConflictError(
                        f"Banktransaktion import_id '{row.import_id}' bereits mit anderem Inhalt vorhanden."
                    )
                return existing
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def find_by_fingerprint(self, bank_konto_id: str, fingerprint_hash: str) -> BankTransaktionTable | None:
        """Nur für Zeilen OHNE bankseitig eindeutige Kennung (hat_native_id
        =False) relevant: findet eine bereits importierte Zeile mit
        identischem wirtschaftlichem Fingerabdruck auf demselben Bankkonto."""

        with self._session_factory() as session:
            return session.execute(
                select(BankTransaktionTable)
                .where(BankTransaktionTable.bank_konto_id == bank_konto_id)
                .where(BankTransaktionTable.fingerprint_hash == fingerprint_hash)
                .where(BankTransaktionTable.hat_native_id.is_(False))
            ).scalars().first()

    def list_unzugeordnet(self, bank_konto_id: str) -> list[BankTransaktionTable]:
        with self._session_factory() as session:
            zugeordnete_ids = select(ZuordnungTable.bank_transaktion_id)
            statement = (
                select(BankTransaktionTable)
                .where(BankTransaktionTable.bank_konto_id == bank_konto_id)
                .where(BankTransaktionTable.id.not_in(zugeordnete_ids))
            )
            return list(session.execute(statement).scalars().all())

    def get_transaktion(self, id: int) -> BankTransaktionTable | None:
        with self._session_factory() as session:
            return session.get(BankTransaktionTable, id)

    def letztes_buchungsdatum(self, bank_konto_id: str) -> date | None:
        with self._session_factory() as session:
            return session.execute(
                select(func.max(BankTransaktionTable.buchungsdatum)).where(
                    BankTransaktionTable.bank_konto_id == bank_konto_id
                )
            ).scalar_one_or_none()

    def zugeordneter_betrag(self, bank_transaktion_id: int) -> int:
        with self._session_factory() as session:
            summe = session.execute(
                select(func.coalesce(func.sum(ZuordnungTable.betrag_cent), 0)).where(
                    ZuordnungTable.bank_transaktion_id == bank_transaktion_id
                )
            ).scalar_one()
            return int(summe)

    def create_zuordnung(
        self, *, bank_transaktion_id: int, op_position_id: int, betrag_cent: int, match_typ: str
    ) -> ZuordnungTable:
        """Validiert UND erstellt die Zuordnung in einer einzigen
        DB-Transaktion (frisch gelesene Zeilen, nicht die vom Aufrufer
        übergebenen Objekte):

        - Gesellschaft von Banktransaktion und Zielkonto muss übereinstimmen.
        - Zuordnungsbetrag muss positiv sein.
        - Währung von Transaktion und Konto muss übereinstimmen.
        - Zuordnungsbetrag darf den noch NICHT zugeordneten Restbetrag der
          Transaktion nicht überschreiten (Teilzuordnungen bleiben mit
          ihrem Rest sichtbar unzugeordnet).
        - Ein Replay derselben (Transaktion, OP)-Kombination mit gleichem
          Betrag ist ein No-Op; mit abweichendem Betrag ein Konflikt.
        """

        with self._session_factory() as session:
            transaktion = session.get(BankTransaktionTable, bank_transaktion_id)
            if transaktion is None:
                raise ValueError(f"Unbekannte Banktransaktion {bank_transaktion_id}")
            op_position = session.get(OPPositionTable, op_position_id)
            if op_position is None:
                raise ValueError(f"Unbekannte OPPosition {op_position_id}")

            bank_konto = session.get(BankKontoTable, transaktion.bank_konto_id)
            ziel_konto = session.get(KontoTable, op_position.konto_id)
            if bank_konto is None or ziel_konto is None:
                raise ValueError("Bank- oder Zielkonto nicht auffindbar.")
            if bank_konto.gesellschaft_id != ziel_konto.gesellschaft_id:
                raise CrossTenantError(
                    f"Banktransaktion (Gesellschaft {bank_konto.gesellschaft_id}) darf nicht mit Konto "
                    f"{ziel_konto.id} (Gesellschaft {ziel_konto.gesellschaft_id}) verrechnet werden."
                )

            bestehende = session.execute(
                select(ZuordnungTable)
                .where(ZuordnungTable.bank_transaktion_id == bank_transaktion_id)
                .where(ZuordnungTable.op_position_id == op_position_id)
            ).scalar_one_or_none()
            if bestehende is not None:
                if bestehende.betrag_cent == betrag_cent:
                    return bestehende
                raise ZuordnungUngueltigError(
                    f"Zuordnung Transaktion {bank_transaktion_id} <-> OP {op_position_id} existiert bereits "
                    f"mit abweichendem Betrag ({bestehende.betrag_cent} statt {betrag_cent})."
                )

            if betrag_cent <= 0:
                raise ZuordnungUngueltigError("Zuordnungsbetrag muss positiv sein.")
            if transaktion.betrag_cent <= 0:
                raise ZuordnungUngueltigError(
                    "Nur Zahlungseingänge (positiver Transaktionsbetrag) können regulär zugeordnet werden; "
                    "für Rücklastschriften siehe bank.service.verarbeite_ruecklastschrift."
                )
            if transaktion.waehrung != ziel_konto.waehrung:
                raise FremdwaehrungNichtUnterstuetztError(
                    f"Transaktion {bank_transaktion_id} ({transaktion.waehrung}) und Konto {ziel_konto.id} "
                    f"({ziel_konto.waehrung}) haben unterschiedliche Währungen."
                )

            bereits_zugeordnet = session.execute(
                select(func.coalesce(func.sum(ZuordnungTable.betrag_cent), 0)).where(
                    ZuordnungTable.bank_transaktion_id == bank_transaktion_id
                )
            ).scalar_one()
            verbleibend = transaktion.betrag_cent - bereits_zugeordnet
            if betrag_cent > verbleibend:
                raise ZuordnungUngueltigError(
                    f"Zuordnungsbetrag {betrag_cent} überschreitet den verbleibenden, noch nicht zugeordneten "
                    f"Betrag der Transaktion {bank_transaktion_id} ({verbleibend} von {transaktion.betrag_cent})."
                )

            zuordnung = ZuordnungTable(
                bank_transaktion_id=bank_transaktion_id,
                op_position_id=op_position_id,
                betrag_cent=betrag_cent,
                match_typ=match_typ,
            )
            session.add(zuordnung)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return session.execute(
                    select(ZuordnungTable)
                    .where(ZuordnungTable.bank_transaktion_id == bank_transaktion_id)
                    .where(ZuordnungTable.op_position_id == op_position_id)
                ).scalar_one()
            session.refresh(zuordnung)
            return zuordnung

    def list_zuordnungen(self, bank_transaktion_id: int) -> list[ZuordnungTable]:
        with self._session_factory() as session:
            return list(
                session.execute(
                    select(ZuordnungTable).where(ZuordnungTable.bank_transaktion_id == bank_transaktion_id)
                )
                .scalars()
                .all()
            )

    def list_zuordnungen_fuer_op(self, op_position_id: int) -> list[ZuordnungTable]:
        with self._session_factory() as session:
            return list(
                session.execute(
                    select(ZuordnungTable).where(ZuordnungTable.op_position_id == op_position_id)
                )
                .scalars()
                .all()
            )
