from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.infrastructure.db.tables import BKAbrechnungTable, BKPositionTable, BKVertragsAnteilTable


class BKRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def get_or_create_abrechnung(self, *, objekt_id: str, abrechnungsjahr: int) -> BKAbrechnungTable:
        with self._session_factory() as session:
            existing = session.execute(
                select(BKAbrechnungTable)
                .where(BKAbrechnungTable.objekt_id == objekt_id)
                .where(BKAbrechnungTable.abrechnungsjahr == abrechnungsjahr)
            ).scalar_one_or_none()
            if existing is not None:
                return existing
            row = BKAbrechnungTable(objekt_id=objekt_id, abrechnungsjahr=abrechnungsjahr, status="ENTWURF")
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def get_abrechnung(self, id: int) -> BKAbrechnungTable | None:
        with self._session_factory() as session:
            return session.get(BKAbrechnungTable, id)

    def add_position(
        self, *, bk_abrechnung_id: int, bezeichnung: str, betrag_cent: int, art: str, quelle: str | None, profil_referenz: str | None
    ) -> BKPositionTable:
        with self._session_factory() as session:
            row = BKPositionTable(
                bk_abrechnung_id=bk_abrechnung_id,
                bezeichnung=bezeichnung,
                betrag_cent=betrag_cent,
                art=art,
                quelle=quelle,
                profil_referenz=profil_referenz,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def list_positionen(self, bk_abrechnung_id: int) -> list[BKPositionTable]:
        with self._session_factory() as session:
            return list(
                session.execute(
                    select(BKPositionTable).where(BKPositionTable.bk_abrechnung_id == bk_abrechnung_id)
                ).scalars().all()
            )

    def set_status(self, bk_abrechnung_id: int, status: str) -> BKAbrechnungTable:
        from datetime import datetime, timezone

        with self._session_factory() as session:
            row = session.get(BKAbrechnungTable, bk_abrechnung_id)
            if row is None:
                raise ValueError(f"Unbekannte BKAbrechnung {bk_abrechnung_id}")
            row.status = status
            if status == "FREIGEGEBEN":
                row.freigegeben_am = datetime.now(timezone.utc)
            session.commit()
            session.refresh(row)
            return row

    def save_anteil(self, anteil: BKVertragsAnteilTable) -> BKVertragsAnteilTable:
        with self._session_factory() as session:
            session.add(anteil)
            session.commit()
            session.refresh(anteil)
            return anteil

    def list_anteile(self, bk_abrechnung_id: int) -> list[BKVertragsAnteilTable]:
        with self._session_factory() as session:
            return list(
                session.execute(
                    select(BKVertragsAnteilTable).where(BKVertragsAnteilTable.bk_abrechnung_id == bk_abrechnung_id)
                ).scalars().all()
            )
