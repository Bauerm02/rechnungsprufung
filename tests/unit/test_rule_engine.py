from decimal import Decimal

from invoice_automation.domain.enums import AmountBand, InvoiceStatus, NotificationType, PaymentType, RoutingFamily, ValidationSeverity
from invoice_automation.domain.models import (
    DocumentRuleFacts,
    DuplicateCheckResult,
    ExtractedInvoiceData,
    NormalizedInvoiceData,
    ValidationMessage,
    ValidationResult,
)
from invoice_automation.rule_engine.service import RuleEngine


def _validation_result(*, error_codes: list[str] | None = None) -> ValidationResult:
    messages = [
        ValidationMessage(
            severity=ValidationSeverity.ERROR,
            code=code,
            message=f"Validation error: {code}",
        )
        for code in (error_codes or [])
    ]
    return ValidationResult(
        processing_id="proc_rule",
        normalized=NormalizedInvoiceData(processing_id="proc_rule"),
        amount_present="amount_missing" not in (error_codes or []),
        messages=messages,
    )


def _facts(**overrides) -> DocumentRuleFacts:
    defaults = {
        "processing_id": "proc_rule",
        "amount_band": AmountBand.LE_10000,
        "payment_type_determined": PaymentType.UEBERWEISUNG,
        "supplier_uid_required": False,
        "recipient_uid_required": False,
    }
    defaults.update(overrides)
    return DocumentRuleFacts(**defaults)


def test_rule_engine_defaults_to_valid_when_no_blocking_rule_matches() -> None:
    engine = RuleEngine()
    decision = engine.evaluate(
        extracted=ExtractedInvoiceData(
            processing_id="proc_rule",
            amount=Decimal("42.00"),
        ),
        duplicate_result=DuplicateCheckResult(is_duplicate=False),
        validation_result=_validation_result(),
        facts=_facts(project_code="GB"),
    )

    assert decision.status == InvoiceStatus.GUELTIG
    assert decision.routing_family == RoutingFamily.COMPANY_PAYABLE
    assert decision.payable_outcome is True
    assert decision.manual_review_required is False
    assert decision.payment_type == PaymentType.UEBERWEISUNG


def test_rule_engine_uses_conservative_hold_on_unhandled_validation_errors() -> None:
    engine = RuleEngine()
    decision = engine.evaluate(
        extracted=ExtractedInvoiceData(
            processing_id="proc_rule",
            amount=Decimal("120.00"),
        ),
        duplicate_result=DuplicateCheckResult(is_duplicate=False),
        validation_result=_validation_result(error_codes=["document_text_missing"]),
        facts=_facts(project_code="GB"),
    )

    assert decision.status == InvoiceStatus.UNGUELTIG
    assert decision.notification_type == NotificationType.ACCOUNTING_INFO
    assert decision.routing_family == RoutingFamily.INVALID_HOLD
    assert decision.payable_outcome is False
    assert decision.manual_review_required is True
    assert decision.payment_type is None


def test_rule_engine_preserves_duplicate_override() -> None:
    engine = RuleEngine()
    decision = engine.evaluate(
        extracted=ExtractedInvoiceData(
            processing_id="proc_rule",
            amount=Decimal("42.00"),
        ),
        duplicate_result=DuplicateCheckResult(
            is_duplicate=True,
            matched_processing_id="proc_existing",
            match_reason="Matched duplicate.",
        ),
        validation_result=_validation_result(error_codes=["document_text_missing"]),
        facts=_facts(project_code="GB"),
    )

    assert decision.status == InvoiceStatus.DUPLIKAT
    assert decision.routing_family == RoutingFamily.DUPLICATE_HOLD
    assert decision.payable_outcome is False


def test_rule_engine_keeps_legal_entity_uid_issue_as_manual_review() -> None:
    engine = RuleEngine()
    decision = engine.evaluate(
        extracted=ExtractedInvoiceData(
            processing_id="proc_rule",
            amount=Decimal("850.00"),
        ),
        duplicate_result=DuplicateCheckResult(is_duplicate=False),
        validation_result=_validation_result(),
        facts=_facts(
            project_code="GB",
            supplier_uid_required=True,
            supplier_uid_present=False,
            sender_legal_entity_detected=True,
        ),
    )

    assert decision.status == InvoiceStatus.GUELTIG
    assert decision.notification_type == NotificationType.ACCOUNTING_INFO
    assert decision.payable_outcome is False
    assert decision.manual_review_required is True


def test_rule_engine_marks_reverse_charge_conflict_as_non_payable_review() -> None:
    engine = RuleEngine()
    decision = engine.evaluate(
        extracted=ExtractedInvoiceData(
            processing_id="proc_rule",
            amount=Decimal("800.00"),
        ),
        duplicate_result=DuplicateCheckResult(is_duplicate=False),
        validation_result=_validation_result(),
        facts=_facts(
            project_code="GB",
            reverse_charge_detected=True,
            reverse_charge_conflict_detected=True,
            payment_type_determined=PaymentType.UEBERWEISUNG,
        ),
    )

    assert decision.status == InvoiceStatus.GUELTIG
    assert decision.notification_type == NotificationType.ACCOUNTING_INFO
    assert decision.routing_family == RoutingFamily.COMPANY_PAYABLE
    assert decision.payable_outcome is False
    assert decision.manual_review_required is True
    assert decision.payment_type == PaymentType.UEBERWEISUNG
