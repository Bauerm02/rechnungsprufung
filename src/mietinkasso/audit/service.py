from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.infrastructure.db.tables import AuditEventTable


class AuditService:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def log(self, *, entity_typ: str, entity_id: str, aktion: str, akteur: str, payload: dict | None = None) -> None:
        with self._session_factory() as session:
            session.add(
                AuditEventTable(
                    entity_typ=entity_typ,
                    entity_id=entity_id,
                    aktion=aktion,
                    akteur=akteur,
                    payload=payload or {},
                )
            )
            session.commit()
