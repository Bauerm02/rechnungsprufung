from __future__ import annotations

from invoice_automation.domain.models import ProcessAuditRecord
from invoice_automation.infrastructure.db.repositories.audit_repository import SqlAlchemyAuditRepository


class AuditLogService:
    def __init__(self, repository: SqlAlchemyAuditRepository):
        self._repository = repository

    def append(self, record: ProcessAuditRecord) -> ProcessAuditRecord:
        return self._repository.append(record)

    def list_for_processing_id(self, processing_id: str) -> list[ProcessAuditRecord]:
        return self._repository.list_for_processing_id(processing_id)

