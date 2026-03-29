from decimal import Decimal

from invoice_automation.domain.enums import AuditEventType, InvoiceStatus, NotificationType, RoutingFamily
from invoice_automation.domain.models import ProcessAuditRecord, RawDocument, RuleDecision, SourceFileMetadata
from invoice_automation.domain.value_objects import ProcessingId


def test_processing_id_uses_prefixed_value() -> None:
    processing_id = ProcessingId()
    assert processing_id.value.startswith("proc_")


def test_raw_document_and_rule_decision_can_be_instantiated() -> None:
    document = RawDocument(
        processing_id="proc_test",
        metadata=SourceFileMetadata(
            source_path="/incoming/invoice.pdf",
            filename="invoice.pdf",
            size_bytes=1234,
        ),
    )
    decision = RuleDecision(
        processing_id=document.processing_id,
        status=InvoiceStatus.GUELTIG,
        reason="Scaffold decision.",
        notification_type=NotificationType.NONE,
        routing_family=RoutingFamily.COMPANY_PAYABLE,
        payable_outcome=True,
        manual_review_required=False,
        stamp_allowed=True,
        xml_allowed=True,
    )

    assert document.metadata.filename == "invoice.pdf"
    assert decision.status == InvoiceStatus.GUELTIG


def test_audit_record_accepts_structured_payloads() -> None:
    record = ProcessAuditRecord(
        processing_id="proc_test",
        event_type=AuditEventType.DECISION_MADE,
        step_name="rule_engine.evaluate",
        input_snapshot={"amount": str(Decimal("12.30"))},
        output_snapshot={"status": "GUELTIG"},
        decision_summary="Accepted in scaffold.",
    )

    assert record.step_name == "rule_engine.evaluate"
    assert record.output_snapshot["status"] == "GUELTIG"
