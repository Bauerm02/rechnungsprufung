from __future__ import annotations

from invoice_automation.domain.enums import NotificationType
from invoice_automation.domain.models import (
    DocumentRuleFacts,
    DuplicateCheckResult,
    ExtractedInvoiceData,
    NotificationDraft,
    RuleDecision,
    ValidationResult,
)


def _notification_recipients(
    notification_type: NotificationType,
    extracted: ExtractedInvoiceData,
) -> list[str]:
    if notification_type == NotificationType.ACCOUNTING_INFO:
        return ["buchhaltung@local.invalid"]
    if notification_type == NotificationType.DUPLICATE_ALERT:
        return ["duplikat-pruefung@local.invalid"]
    if notification_type == NotificationType.REDLINGHOFER_CORRECTION:
        return ["redlinghofer-korrektur@local.invalid"]
    if notification_type == NotificationType.SUPPLIER_CORRECTION:
        return [extracted.supplier_email] if extracted.supplier_email else ["lieferant-korrektur@local.invalid"]
    return []


def _draft_subject(decision: RuleDecision, extracted: ExtractedInvoiceData) -> str:
    sender_name = extracted.sender_name or "Unbekannter Absender"
    return f"[{decision.notification_type.value}] {decision.processing_id} | {sender_name}"


def _draft_body(
    *,
    decision: RuleDecision,
    extracted: ExtractedInvoiceData,
    facts: DocumentRuleFacts,
    duplicate_result: DuplicateCheckResult,
    validation_result: ValidationResult,
) -> str:
    lines = [
        f"Processing-ID: {decision.processing_id}",
        f"Status: {decision.status.value}",
        f"Grund: {decision.reason}",
        f"Absender: {extracted.sender_name or ''}",
        f"Empfaenger: {extracted.recipient_name or ''}",
        f"Projekt: {decision.project_code or ''}",
        f"Zahlungsart: {decision.payment_type.value if decision.payment_type else ''}",
        f"Betrag: {extracted.amount if extracted.amount is not None else ''} {extracted.currency or 'EUR'}".strip(),
        f"Rechnungsnummer: {extracted.invoice_number or ''}",
    ]

    if duplicate_result.matched_processing_id:
        lines.append(f"Duplikat zu: {duplicate_result.matched_processing_id}")
    if facts.transfer_reference_value:
        lines.append(f"Transferreferenz: {facts.transfer_reference_value}")
    if validation_result.messages:
        message_codes = ", ".join(message.code for message in validation_result.messages)
        lines.append(f"Validierung: {message_codes}")

    return "\n".join(lines)


def build_notification_draft(
    *,
    decision: RuleDecision,
    extracted: ExtractedInvoiceData,
    facts: DocumentRuleFacts,
    duplicate_result: DuplicateCheckResult,
    validation_result: ValidationResult,
) -> NotificationDraft | None:
    if decision.notification_type == NotificationType.NONE:
        return None

    return NotificationDraft(
        processing_id=decision.processing_id,
        notification_type=decision.notification_type,
        recipients=_notification_recipients(decision.notification_type, extracted),
        subject=_draft_subject(decision, extracted),
        body=_draft_body(
            decision=decision,
            extracted=extracted,
            facts=facts,
            duplicate_result=duplicate_result,
            validation_result=validation_result,
        ),
        draft_filename=f"{decision.processing_id}_notification_draft.txt",
        enabled=False,
    )
