from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.domain.enums import IndexAnpassungStatus, IndexKlauselStatus
from mietinkasso.infrastructure.db.tables import IndexAnpassungTable, IndexKlauselTable


class IndexRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def naechste_version(self, vertrag_id: str) -> int:
        with self._session_factory() as session:
            statement = select(IndexKlauselTable.version).where(IndexKlauselTable.vertrag_id == vertrag_id)
            versionen = [v for (v,) in session.execute(statement).all()]
            return (max(versionen) + 1) if versionen else 1

    def anlegen(self, klausel: IndexKlauselTable) -> IndexKlauselTable:
        with self._session_factory() as session:
            vorherige = session.execute(
                select(IndexKlauselTable)
                .where(IndexKlauselTable.vertrag_id == klausel.vertrag_id)
                .where(IndexKlauselTable.status == IndexKlauselStatus.FREIGEGEBEN.value)
            ).scalars().all()
            session.add(klausel)
            session.flush()
            for alte in vorherige:
                alte.status = IndexKlauselStatus.GESPERRT.value
                alte.ersetzt_id = klausel.id
                # Vorschläge auf Basis der alten, jetzt ersetzten Klausel sind ungültig,
                # solange sie nicht bereits selbst freigegeben wurden.
                offene_vorschlaege = session.execute(
                    select(IndexAnpassungTable)
                    .where(IndexAnpassungTable.index_klausel_id == alte.id)
                    .where(IndexAnpassungTable.status == IndexAnpassungStatus.VORSCHLAG.value)
                ).scalars().all()
                for vorschlag in offene_vorschlaege:
                    vorschlag.status = IndexAnpassungStatus.INVALIDIERT.value
            session.commit()
            session.refresh(klausel)
            return klausel

    def get_klausel(self, klausel_id: int) -> IndexKlauselTable | None:
        with self._session_factory() as session:
            return session.get(IndexKlauselTable, klausel_id)

    def freigeben(self, klausel_id: int, *, freigegeben_von: str) -> IndexKlauselTable:
        from datetime import datetime, timezone

        with self._session_factory() as session:
            row = session.get(IndexKlauselTable, klausel_id)
            if row is None:
                raise ValueError(f"Unbekannte IndexKlausel {klausel_id}")
            row.status = IndexKlauselStatus.FREIGEGEBEN.value
            row.freigegeben_am = datetime.now(timezone.utc)
            row.freigegeben_von = freigegeben_von
            session.commit()
            session.refresh(row)
            return row

    def freigegebene_klausel(self, vertrag_id: str) -> IndexKlauselTable | None:
        with self._session_factory() as session:
            statement = (
                select(IndexKlauselTable)
                .where(IndexKlauselTable.vertrag_id == vertrag_id)
                .where(IndexKlauselTable.status == IndexKlauselStatus.FREIGEGEBEN.value)
                .order_by(IndexKlauselTable.version.desc())
                .limit(1)
            )
            return session.execute(statement).scalar_one_or_none()

    def letzte_anpassung(self, vertrag_id: str) -> IndexAnpassungTable | None:
        with self._session_factory() as session:
            statement = (
                select(IndexAnpassungTable)
                .where(IndexAnpassungTable.vertrag_id == vertrag_id)
                .order_by(IndexAnpassungTable.id.desc())
                .limit(1)
            )
            return session.execute(statement).scalars().first()

    def speichere_anpassung(self, anpassung: IndexAnpassungTable) -> IndexAnpassungTable:
        with self._session_factory() as session:
            session.add(anpassung)
            session.commit()
            session.refresh(anpassung)
            return anpassung

    def get_anpassung(self, id: int) -> IndexAnpassungTable | None:
        with self._session_factory() as session:
            return session.get(IndexAnpassungTable, id)
