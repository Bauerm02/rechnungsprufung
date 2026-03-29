from __future__ import annotations

from invoice_automation.application.decision_tables import determine_routing_family, get_status_policy
from invoice_automation.domain.enums import InvoiceStatus, NotificationType, ValidationSeverity
from invoice_automation.domain.models import (
    DocumentRuleFacts,
    DuplicateCheckResult,
    ExtractedInvoiceData,
    RuleDecision,
    ValidationResult,
)


class RuleEngine:
    def evaluate(
        self,
        *,
        extracted: ExtractedInvoiceData,
        duplicate_result: DuplicateCheckResult,
        validation_result: ValidationResult,
        facts: DocumentRuleFacts | None = None,
    ) -> RuleDecision:
        facts = facts or DocumentRuleFacts(processing_id=extracted.processing_id)

        if duplicate_result.is_duplicate:
            return self._build_decision(
                processing_id=extracted.processing_id,
                status=InvoiceStatus.DUPLIKAT,
                reason=duplicate_result.match_reason or "Matched prior processed invoice in the primary database registry.",
                notification_type=NotificationType.DUPLICATE_ALERT,
                validation_result=validation_result,
                facts=facts,
                payable_outcome=False,
                manual_review_required=False,
                payment_type=None,
            )

        status, reason, notification_type, policy_notes = self._classify_status(
            extracted=extracted,
            validation_result=validation_result,
            facts=facts,
        )

        payable_outcome = status == InvoiceStatus.GUELTIG
        manual_review_required = status != InvoiceStatus.GUELTIG
        payment_type = facts.payment_type_determined if status == InvoiceStatus.GUELTIG else None

        if status == InvoiceStatus.GUELTIG:
            (
                status,
                reason,
                notification_type,
                payable_outcome,
                manual_review_required,
                payment_type,
                additional_notes,
            ) = self._apply_gueltig_guards(
                validation_result=validation_result,
                facts=facts,
                current_reason=reason,
                current_notification=notification_type,
                current_payment_type=payment_type,
            )
            policy_notes.extend(additional_notes)

        if validation_result.has_warnings:
            policy_notes.append("Validation warnings present.")
        if validation_result.has_errors:
            policy_notes.append("Validation errors present.")

        return self._build_decision(
            processing_id=extracted.processing_id,
            status=status,
            reason=reason,
            notification_type=notification_type,
            validation_result=validation_result,
            facts=facts,
            payable_outcome=payable_outcome,
            manual_review_required=manual_review_required,
            payment_type=payment_type if status == InvoiceStatus.GUELTIG else None,
            extra_policy_notes=policy_notes,
        )

    def _classify_status(
        self,
        *,
        extracted: ExtractedInvoiceData,
        validation_result: ValidationResult,
        facts: DocumentRuleFacts,
    ) -> tuple[InvoiceStatus, str, NotificationType, list[str]]:
        notes: list[str] = []

        if facts.redlinghofer_only_recipient:
            return (
                InvoiceStatus.UNGUELTIG,
                "Redlinghofer als alleiniger Empfaenger ist nicht zulaessig; Hotel Villa Flora muss als Empfaenger erscheinen.",
                NotificationType.REDLINGHOFER_CORRECTION,
                ["Redlinghofer special-case rule matched."],
            )

        if facts.authority_detected:
            return (
                InvoiceStatus.GUELTIG,
                "Behoerde.",
                NotificationType.NONE,
                [f"Authority precedence applied via keyword '{facts.authority_keyword or 'unknown'}'."],
            )

        if facts.private_detected:
            return (
                InvoiceStatus.PRIVAT_RECHNUNG,
                extracted.reason_candidate or "Privatausgabe.",
                NotificationType.ACCOUNTING_INFO,
                notes,
            )

        if facts.reminder_detected:
            return (
                InvoiceStatus.MAHNUNG,
                extracted.reason_candidate or "Mahnung oder Zahlungserinnerung erkannt.",
                NotificationType.ACCOUNTING_INFO,
                notes,
            )

        if facts.storno_or_credit_detected:
            return (
                InvoiceStatus.GUELTIG,
                extracted.reason_candidate or "Storno, Gutschrift oder Stornierung erkannt.",
                NotificationType.NONE,
                ["Credit-note or cancellation rule matched."],
            )

        if facts.organschaft_detected:
            return (
                InvoiceStatus.GUELTIG,
                extracted.reason_candidate or "Organschaft oder Innenumsatz.",
                NotificationType.NONE,
                ["Organschaft/Innenumsatz rule matched."],
            )

        if facts.internal_deposit_detected:
            return (
                InvoiceStatus.GUELTIG,
                extracted.reason_candidate or "Interne Kautionsabrechnung.",
                NotificationType.NONE,
                ["Internal deposit rule matched."],
            )

        if facts.internal_expense_detected:
            return (
                InvoiceStatus.GUELTIG,
                extracted.reason_candidate or "Interne Abrechnung / Kostenersatz.",
                NotificationType.NONE,
                ["Internal expense reimbursement rule matched."],
            )

        if not validation_result.amount_present:
            return (
                InvoiceStatus.UNGUELTIG,
                "Kein Brutto-Betrag vorhanden.",
                NotificationType.ACCOUNTING_INFO,
                notes,
            )

        if facts.reverse_charge_detected and facts.reverse_charge_domestic is True:
            return (
                InvoiceStatus.UNGUELTIG,
                "Inlaendisches Reverse Charge als Bautraeger nicht erlaubt.",
                NotificationType.ACCOUNTING_INFO,
                ["Domestic reverse-charge rule matched."],
            )

        if facts.reverse_charge_detected and facts.reverse_charge_conflict_detected:
            return (
                InvoiceStatus.GUELTIG,
                "Reverse Charge mit widerspruechlichen inlaendischen/auslaendischen Lieferantensignalen erkannt.",
                NotificationType.ACCOUNTING_INFO,
                ["Conflicting reverse-charge country signals require manual review."],
            )

        if facts.reverse_charge_detected and facts.reverse_charge_domestic is False:
            return (
                InvoiceStatus.GUELTIG,
                "Reverse Charge mit auslaendischem Lieferanten erkannt.",
                NotificationType.NONE,
                ["Foreign reverse-charge rule matched."],
            )

        return (
            InvoiceStatus.GUELTIG,
            extracted.reason_candidate or "Invoice passed deterministic rule set.",
            NotificationType.NONE,
            notes,
        )

    def _apply_gueltig_guards(
        self,
        *,
        validation_result: ValidationResult,
        facts: DocumentRuleFacts,
        current_reason: str,
        current_notification: NotificationType,
        current_payment_type,
    ) -> tuple[
        InvoiceStatus,
        str,
        NotificationType,
        bool,
        bool,
        object | None,
        list[str],
    ]:
        notes: list[str] = []
        error_codes = self._error_codes(validation_result)
        handled_error_codes: set[str] = set()
        payable_outcome = True
        manual_review_required = False
        notification_type = current_notification

        if facts.payability_blocked:
            payable_outcome = False
            notes.append(facts.payability_block_reason or "Payability blocked by deterministic rule.")

        if facts.reverse_charge_conflict_detected:
            payable_outcome = False
            manual_review_required = True
            notification_type = NotificationType.ACCOUNTING_INFO
            notes.append("Conflicting domestic and foreign reverse-charge signals require manual review before payment.")

        if facts.authority_detected:
            authority_safe_errors = {"amount_missing", "sender_vat_invalid", "recipient_vat_invalid"}
            authority_matches = error_codes & authority_safe_errors
            if authority_matches:
                handled_error_codes.update(authority_matches)
                manual_review_required = True
                payable_outcome = False
                notification_type = NotificationType.ACCOUNTING_INFO
                notes.append("Authority precedence preserved despite validation findings.")

        if facts.recipient_uid_required and not facts.recipient_uid_present and not self._uid_rules_exempt(facts):
            return self._invalidate(
                reason="Empfaenger-UID fehlt fuer Rechnungen ueber 10.000 EUR.",
                notification_type=NotificationType.ACCOUNTING_INFO,
                payment_type=current_payment_type,
            )

        if "recipient_vat_invalid" in error_codes and not self._uid_rules_exempt(facts):
            return self._invalidate(
                reason="Empfaenger-UID konnte nicht validiert werden.",
                notification_type=NotificationType.ACCOUNTING_INFO,
                payment_type=current_payment_type,
            )

        if self._uid_rules_exempt(facts):
            handled_error_codes.update({"sender_vat_invalid", "recipient_vat_invalid"})

        if facts.supplier_uid_required and not facts.supplier_uid_present and not self._uid_rules_exempt(facts):
            if facts.sender_legal_entity_detected:
                manual_review_required = True
                payable_outcome = False
                notification_type = NotificationType.ACCOUNTING_INFO
                notes.append("Supplier UID missing but legal entity or law firm pattern detected.")
            else:
                return self._invalidate(
                    reason="Lieferanten-UID fehlt fuer diese Rechnung.",
                    notification_type=self._supplier_issue_notification(facts),
                    payment_type=current_payment_type,
                )

        if "sender_vat_invalid" in error_codes and "sender_vat_invalid" not in handled_error_codes:
            if facts.sender_legal_entity_detected:
                handled_error_codes.add("sender_vat_invalid")
                manual_review_required = True
                payable_outcome = False
                notification_type = NotificationType.ACCOUNTING_INFO
                notes.append("Supplier VAT invalid but legal entity or law firm pattern detected.")
            else:
                return self._invalidate(
                    reason="Lieferanten-UID konnte nicht validiert werden.",
                    notification_type=self._supplier_issue_notification(facts),
                    payment_type=current_payment_type,
                )

        remaining_error_codes = error_codes - handled_error_codes
        if remaining_error_codes:
            return self._invalidate(
                reason="Deterministic validation errors require manual review before payment.",
                notification_type=NotificationType.ACCOUNTING_INFO,
                payment_type=current_payment_type,
                notes=[f"Unhandled validation errors: {', '.join(sorted(remaining_error_codes))}."],
            )

        return (
            InvoiceStatus.GUELTIG,
            current_reason,
            notification_type,
            payable_outcome,
            manual_review_required,
            current_payment_type,
            notes,
        )

    def _invalidate(
        self,
        *,
        reason: str,
        notification_type: NotificationType,
        payment_type,
        notes: list[str] | None = None,
    ) -> tuple[InvoiceStatus, str, NotificationType, bool, bool, object | None, list[str]]:
        return (
            InvoiceStatus.UNGUELTIG,
            reason,
            notification_type,
            False,
            True,
            None if payment_type is not None else None,
            list(notes or []),
        )

    def _build_decision(
        self,
        *,
        processing_id: str,
        status: InvoiceStatus,
        reason: str,
        notification_type: NotificationType,
        validation_result: ValidationResult,
        facts: DocumentRuleFacts,
        payable_outcome: bool,
        manual_review_required: bool,
        payment_type,
        extra_policy_notes: list[str] | None = None,
    ) -> RuleDecision:
        policy = get_status_policy(status)
        resolved_notification = notification_type if notification_type != NotificationType.NONE or status == InvoiceStatus.GUELTIG else policy.notification_type
        routing_family = determine_routing_family(status, payment_type, facts.project_code)

        return RuleDecision(
            processing_id=processing_id,
            status=status,
            reason=reason,
            notification_type=resolved_notification if status == InvoiceStatus.GUELTIG else notification_type or policy.notification_type,
            routing_family=routing_family,
            payable_outcome=payable_outcome if status == InvoiceStatus.GUELTIG else policy.payable_outcome,
            manual_review_required=manual_review_required if status == InvoiceStatus.GUELTIG else policy.manual_review_required,
            stamp_allowed=policy.stamp_allowed if status != InvoiceStatus.GUELTIG else policy.stamp_allowed,
            xml_allowed=policy.xml_allowed if status != InvoiceStatus.GUELTIG else policy.xml_allowed,
            payment_type=payment_type if status == InvoiceStatus.GUELTIG else None,
            project_code=facts.project_code,
            policy_notes=list(extra_policy_notes or []),
        )

    def _error_codes(self, validation_result: ValidationResult) -> set[str]:
        return {
            message.code
            for message in validation_result.messages
            if message.severity == ValidationSeverity.ERROR
        }

    def _supplier_issue_notification(self, facts: DocumentRuleFacts) -> NotificationType:
        return NotificationType.SUPPLIER_CORRECTION if facts.supplier_email_present else NotificationType.ACCOUNTING_INFO

    def _uid_rules_exempt(self, facts: DocumentRuleFacts) -> bool:
        return any(
            (
                facts.authority_detected,
                facts.storno_or_credit_detected,
                facts.organschaft_detected,
                facts.internal_deposit_detected,
                facts.internal_expense_detected,
            )
        )
