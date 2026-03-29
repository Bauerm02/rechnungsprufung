from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from invoice_automation.domain.models import ProcessAuditRecord
from invoice_automation.infrastructure.db.tables import AuditLogTable


class SqlAlchemyAuditRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def append(self, record: ProcessAuditRecord) -> ProcessAuditRecord:
        with self._session_factory() as session:
            row = AuditLogTable(
                processing_id=record.processing_id,
                event_type=record.event_type.value,
                step_name=record.step_name,
                input_snapshot=record.input_snapshot,
                output_snapshot=record.output_snapshot,
                decision_summary=record.decision_summary,
            )
            session.add(row)
            session.commit()
            return record

    def list_for_processing_id(self, processing_id: str) -> list[ProcessAuditRecord]:
        with self._session_factory() as session:
            statement = (
                select(AuditLogTable)
                .where(AuditLogTable.processing_id == processing_id)
                .order_by(AuditLogTable.id.asc())
            )
            rows = session.execute(statement).scalars().all()
            return [
                ProcessAuditRecord(
                    processing_id=row.processing_id,
                    event_type=row.event_type,
                    step_name=row.step_name,
                    input_snapshot=row.input_snapshot,
                    output_snapshot=row.output_snapshot,
                    decision_summary=row.decision_summary,
                    created_at=row.created_at,
                )
                for row in rows
            ]

