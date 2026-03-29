from invoice_automation.domain.enums import AuditEventType
from invoice_automation.domain.models import ProcessAuditRecord
from invoice_automation.infrastructure.db.repositories.audit_repository import SqlAlchemyAuditRepository


def test_audit_repository_persists_and_lists_records(session_factory) -> None:
    repository = SqlAlchemyAuditRepository(session_factory)
    record = ProcessAuditRecord(
        processing_id="proc_audit",
        event_type=AuditEventType.INGESTED,
        step_name="ingestion.create_document",
        input_snapshot={"filename": "invoice.pdf"},
        output_snapshot={"processing_id": "proc_audit"},
    )

    repository.append(record)
    records = repository.list_for_processing_id("proc_audit")

    assert len(records) == 1
    assert records[0].step_name == "ingestion.create_document"

