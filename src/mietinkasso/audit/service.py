from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.infrastructure.db.tables import AuditEventTable


class AuditService:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def log(
        self,
        *,
        entity_typ: str,
        entity_id: str,
        aktion: str,
        akteur: str,
        payload: dict | None = None,
        session: Session | None = None,
    ) -> None:
        """Mit einer übergebenen `session` wird dieser Audit-Eintrag Teil
        einer größeren, vom Aufrufer verwalteten Transaktion (flush statt
        commit) - z. B. OP-Buchung + Zuordnung + Audit als eine
        DB-Transaktion mit gemeinsamem Rollback."""

        event = AuditEventTable(
            entity_typ=entity_typ,
            entity_id=entity_id,
            aktion=aktion,
            akteur=akteur,
            payload=payload or {},
        )
        if session is not None:
            session.add(event)
            session.flush()
            return
        with self._session_factory() as owned_session:
            owned_session.add(event)
            owned_session.commit()
