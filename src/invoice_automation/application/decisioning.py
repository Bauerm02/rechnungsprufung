from __future__ import annotations

from invoice_automation.application.decision_tables import build_xml_plan
from invoice_automation.domain.models import DecisionOutcome, ExtractedInvoiceData, ExtractionBundle
from invoice_automation.duplicate_detection.service import DuplicateDetectionService
from invoice_automation.notifications.policies import build_notification_draft
from invoice_automation.pdf_stamping.policies import build_stamp_plan
from invoice_automation.rule_engine.classifiers import derive_rule_facts
from invoice_automation.rule_engine.service import RuleEngine
from invoice_automation.storage_routing.policies import build_routing_plans
from invoice_automation.validation.service import ValidationService


class DecisioningService:
    def __init__(
        self,
        *,
        validation_service: ValidationService,
        duplicate_service: DuplicateDetectionService,
        rule_engine: RuleEngine,
        side_effects_enabled: bool = False,
    ):
        self._validation_service = validation_service
        self._duplicate_service = duplicate_service
        self._rule_engine = rule_engine
        self._side_effects_enabled = side_effects_enabled

    def decide(self, *, bundle: ExtractionBundle, extracted: ExtractedInvoiceData) -> DecisionOutcome:
        validation_result = self._validation_service.validate(extracted, bundle=bundle)
        duplicate_result = self._duplicate_service.find_duplicate(
            sender_name_normalized=validation_result.normalized.sender_name_normalized or "",
            invoice_number_normalized=validation_result.normalized.invoice_number_normalized,
            document_date=validation_result.normalized.document_date,
            recipient_name_normalized=validation_result.normalized.recipient_name_normalized,
            iban_normalized=validation_result.normalized.iban_normalized,
            content_hash=validation_result.normalized.content_hash_normalized,
            amount_decimal=validation_result.normalized.amount_decimal,
        )
        facts = derive_rule_facts(bundle, extracted, validation_result)
        rule_decision = self._rule_engine.evaluate(
            extracted=extracted,
            duplicate_result=duplicate_result,
            validation_result=validation_result,
            facts=facts,
        )
        xml_plan = build_xml_plan(
            processing_id=extracted.processing_id,
            status=rule_decision.status,
            payment_type=rule_decision.payment_type,
            payable_outcome=rule_decision.payable_outcome,
            has_credit_note=facts.storno_or_credit_detected,
            has_negative_amount=facts.amount_negative,
            has_creditor_iban=bool(validation_result.normalized.iban_normalized),
            is_employee_expense=facts.internal_expense_detected,
            side_effects_enabled=self._side_effects_enabled,
            amount=extracted.amount,
            currency=extracted.currency or "EUR",
            creditor_name=extracted.sender_name,
            creditor_iban=validation_result.normalized.iban_normalized or facts.internal_expense_creditor_iban_fallback,
            debtor_name=extracted.recipient_name,
            invoice_number=extracted.invoice_number,
            transfer_reference=facts.transfer_reference_value,
            project_code=rule_decision.project_code,
        )
        stamp_plan = build_stamp_plan(
            decision=rule_decision,
            validation_result=validation_result,
            facts=facts,
        )
        notification_draft = build_notification_draft(
            decision=rule_decision,
            extracted=extracted,
            facts=facts,
            duplicate_result=duplicate_result,
            validation_result=validation_result,
        )
        routing_plans = build_routing_plans(
            processing_id=extracted.processing_id,
            routing_family=rule_decision.routing_family,
            xml_plan=xml_plan,
            stamp_plan=stamp_plan,
            notification_draft=notification_draft,
            enabled=self._side_effects_enabled,
        )
        return DecisionOutcome(
            processing_id=extracted.processing_id,
            validation_result=validation_result,
            duplicate_result=duplicate_result,
            facts=facts,
            rule_decision=rule_decision,
            xml_plan=xml_plan,
            stamp_plan=stamp_plan,
            notification_draft=notification_draft,
            routing_plans=routing_plans,
        )
