from invoice_automation.domain.enums import InvoiceStatus, NotificationType, RoutingFamily
from invoice_automation.domain.models import (
    DocumentRuleFacts,
    DuplicateCheckResult,
    ExtractedInvoiceData,
    NormalizedInvoiceData,
    RuleDecision,
    ValidationResult,
)
from invoice_automation.notifications.policies import build_notification_draft


def _validation_result(processing_id: str) -> ValidationResult:
    return ValidationResult(
        processing_id=processing_id,
        normalized=NormalizedInvoiceData(processing_id=processing_id),
        amount_present=True,
    )


def test_notification_draft_builds_supplier_correction_email_without_queueing() -> None:
    decision = RuleDecision(
        processing_id="proc_notify",
        status=InvoiceStatus.UNGUELTIG,
        reason="Lieferanten-UID fehlt fuer diese Rechnung.",
        notification_type=NotificationType.SUPPLIER_CORRECTION,
        routing_family=RoutingFamily.INVALID_HOLD,
        payable_outcome=False,
        manual_review_required=True,
        stamp_allowed=False,
        xml_allowed=False,
        project_code="7DI",
    )
    extracted = ExtractedInvoiceData(
        processing_id="proc_notify",
        sender_name="Max Muster",
        recipient_name="Sieben Dorfer Immobilien GmbH",
        invoice_number="UID-850-B",
        supplier_email="office@example.com",
    )

    draft = build_notification_draft(
        decision=decision,
        extracted=extracted,
        facts=DocumentRuleFacts(processing_id="proc_notify", project_code="7DI"),
        duplicate_result=DuplicateCheckResult(is_duplicate=False),
        validation_result=_validation_result("proc_notify"),
    )

    assert draft is not None
    assert draft.enabled is False
    assert draft.recipients == ["office@example.com"]
    assert draft.draft_filename == "proc_notify_notification_draft.txt"
    assert "Lieferanten-UID fehlt" in draft.body


def test_notification_draft_is_omitted_when_no_notification_is_required() -> None:
    decision = RuleDecision(
        processing_id="proc_no_notify",
        status=InvoiceStatus.GUELTIG,
        reason="Invoice passed deterministic rule set.",
        notification_type=NotificationType.NONE,
        routing_family=RoutingFamily.COMPANY_PAYABLE,
        payable_outcome=True,
        manual_review_required=False,
        stamp_allowed=True,
        xml_allowed=True,
        project_code="GB",
    )

    draft = build_notification_draft(
        decision=decision,
        extracted=ExtractedInvoiceData(processing_id="proc_no_notify"),
        facts=DocumentRuleFacts(processing_id="proc_no_notify", project_code="GB"),
        duplicate_result=DuplicateCheckResult(is_duplicate=False),
        validation_result=_validation_result("proc_no_notify"),
    )

    assert draft is None
