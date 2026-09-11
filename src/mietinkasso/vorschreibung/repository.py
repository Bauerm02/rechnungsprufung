from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.infrastructure.db.tables import VorschreibungPositionTable, VorschreibungTable


class VorschreibungRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def get(self, vertrag_id: str, monat: str) -> VorschreibungTable | None:
        with self._session_factory() as session:
            return session.execute(
                select(VorschreibungTable)
                .where(VorschreibungTable.vertrag_id == vertrag_id)
                .where(VorschreibungTable.monat == monat)
            ).scalar_one_or_none()

    def get_or_create_entwurf(self, *, vertrag_id: str, monat: str, faelligkeit) -> VorschreibungTable:
        """Race-safe: relies on the DB unique constraint (vertrag_id, monat)
        so that two workers racing here end up with exactly one row, no
        matter which one wins the insert."""

        with self._session_factory() as session:
            existing = session.execute(
                select(VorschreibungTable)
                .where(VorschreibungTable.vertrag_id == vertrag_id)
                .where(VorschreibungTable.monat == monat)
            ).scalar_one_or_none()
            if existing is not None:
                return existing
            row = VorschreibungTable(vertrag_id=vertrag_id, monat=monat, status="ENTWURF", faelligkeit=faelligkeit)
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return session.execute(
                    select(VorschreibungTable)
                    .where(VorschreibungTable.vertrag_id == vertrag_id)
                    .where(VorschreibungTable.monat == monat)
                ).scalar_one()
            session.refresh(row)
            return row

    def add_position(
        self, *, vorschreibung_id: int, art: str, bezeichnung: str, betrag_cent: int, ust_satz_promille: int
    ) -> None:
        with self._session_factory() as session:
            session.add(
                VorschreibungPositionTable(
                    vorschreibung_id=vorschreibung_id,
                    art=art,
                    bezeichnung=bezeichnung,
                    betrag_cent=betrag_cent,
                    ust_satz_promille=ust_satz_promille,
                )
            )
            session.commit()

    def list_positionen(self, vorschreibung_id: int) -> list[VorschreibungPositionTable]:
        with self._session_factory() as session:
            statement = select(VorschreibungPositionTable).where(
                VorschreibungPositionTable.vorschreibung_id == vorschreibung_id
            )
            return list(session.execute(statement).scalars().all())

    def update_status(self, vorschreibung_id: int, **fields) -> VorschreibungTable:
        with self._session_factory() as session:
            row = session.get(VorschreibungTable, vorschreibung_id)
            if row is None:
                raise ValueError(f"Unbekannte Vorschreibung {vorschreibung_id}")
            for key, value in fields.items():
                setattr(row, key, value)
            session.commit()
            session.refresh(row)
            return row
