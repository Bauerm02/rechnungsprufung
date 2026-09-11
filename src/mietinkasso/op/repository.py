from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.domain.enums import OPPositionStatus
from mietinkasso.domain.exceptions import ImportConflictError
from mietinkasso.infrastructure.db.tables import OPPositionTable


class OPRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def find_by_import_id(self, import_id: str) -> OPPositionTable | None:
        with self._session_factory() as session:
            return session.execute(
                select(OPPositionTable).where(OPPositionTable.import_id == import_id)
            ).scalar_one_or_none()

    def insert_idempotent(self, row: OPPositionTable) -> OPPositionTable:
        """Insert `row`, unless its import_id already exists.

        - Same import_id + same quelle_hash -> replay, return the existing
          row untouched (Doppelimport wirkungslos).
        - Same import_id + different quelle_hash -> real conflict.
        - No import_id -> always inserted (manual/system-internal postings
          that do not claim idempotency, e.g. Storno rows).
        """

        with self._session_factory() as session:
            if row.import_id is not None:
                existing = session.execute(
                    select(OPPositionTable).where(OPPositionTable.import_id == row.import_id)
                ).scalar_one_or_none()
                if existing is not None:
                    if existing.quelle_hash != row.quelle_hash:
                        raise ImportConflictError(
                            f"import_id '{row.import_id}' bereits mit anderem Inhalt vorhanden "
                            f"(gespeichert: {existing.quelle_hash}, neu: {row.quelle_hash})."
                        )
                    return existing
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def find_eroeffnung(self, konto_id: str) -> OPPositionTable | None:
        with self._session_factory() as session:
            return session.execute(
                select(OPPositionTable)
                .where(OPPositionTable.konto_id == konto_id)
                .where(OPPositionTable.typ == "EROEFFNUNG")
                .where(OPPositionTable.status == OPPositionStatus.AKTIV.value)
            ).scalar_one_or_none()

    def list_aktiv(self, konto_id: str) -> list[OPPositionTable]:
        with self._session_factory() as session:
            statement = (
                select(OPPositionTable)
                .where(OPPositionTable.konto_id == konto_id)
                .where(OPPositionTable.status == OPPositionStatus.AKTIV.value)
                .order_by(OPPositionTable.belegdatum)
            )
            return list(session.execute(statement).scalars().all())

    def list_alle(self, konto_id: str) -> list[OPPositionTable]:
        with self._session_factory() as session:
            statement = (
                select(OPPositionTable)
                .where(OPPositionTable.konto_id == konto_id)
                .order_by(OPPositionTable.belegdatum)
            )
            return list(session.execute(statement).scalars().all())

    def get(self, op_position_id: int) -> OPPositionTable | None:
        with self._session_factory() as session:
            return session.get(OPPositionTable, op_position_id)

    def storno(self, *, original_id: int, neue_row: OPPositionTable | None, akteur: str) -> OPPositionTable | None:
        """Mark `original_id` STORNIERT and optionally insert a replacement
        AKTIV row (the Korrektur). Never edits the original row's amount."""

        with self._session_factory() as session:
            original = session.get(OPPositionTable, original_id)
            if original is None:
                raise ValueError(f"Unbekannte OPPosition {original_id}")
            if neue_row is not None:
                session.add(neue_row)
                session.flush()
                original.storniert_durch_id = neue_row.id
            original.status = OPPositionStatus.STORNIERT.value
            session.commit()
            if neue_row is not None:
                session.refresh(neue_row)
                return neue_row
            return None
