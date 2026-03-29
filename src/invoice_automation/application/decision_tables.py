from __future__ import annotations

from dataclasses import dataclass
from xml.sax.saxutils import escape

from invoice_automation.domain.enums import (
    InvoiceStatus,
    NotificationType,
    PaymentType,
    RoutingFamily,
    XmlDisposition,
)
from invoice_automation.domain.models import PaymentXmlPlan


@dataclass(frozen=True)
class StatusPolicy:
    status: InvoiceStatus
    stamp_allowed: bool
    xml_allowed: bool
    notification_type: NotificationType
    routing_family: RoutingFamily
    payable_outcome: bool
    manual_review_required: bool


STATUS_POLICIES: dict[InvoiceStatus, StatusPolicy] = {
    InvoiceStatus.GUELTIG: StatusPolicy(
        status=InvoiceStatus.GUELTIG,
        stamp_allowed=True,
        xml_allowed=True,
        notification_type=NotificationType.NONE,
        routing_family=RoutingFamily.COMPANY_PAYABLE,
        payable_outcome=True,
        manual_review_required=False,
    ),
    InvoiceStatus.UNGUELTIG: StatusPolicy(
        status=InvoiceStatus.UNGUELTIG,
        stamp_allowed=False,
        xml_allowed=False,
        notification_type=NotificationType.ACCOUNTING_INFO,
        routing_family=RoutingFamily.INVALID_HOLD,
        payable_outcome=False,
        manual_review_required=True,
    ),
    InvoiceStatus.MAHNUNG: StatusPolicy(
        status=InvoiceStatus.MAHNUNG,
        stamp_allowed=False,
        xml_allowed=False,
        notification_type=NotificationType.ACCOUNTING_INFO,
        routing_family=RoutingFamily.REMINDER_REVIEW_HOLD,
        payable_outcome=False,
        manual_review_required=True,
    ),
    InvoiceStatus.PRIVAT_RECHNUNG: StatusPolicy(
        status=InvoiceStatus.PRIVAT_RECHNUNG,
        stamp_allowed=False,
        xml_allowed=False,
        notification_type=NotificationType.ACCOUNTING_INFO,
        routing_family=RoutingFamily.PRIVATE_REVIEW_HOLD,
        payable_outcome=False,
        manual_review_required=True,
    ),
    InvoiceStatus.DUPLIKAT: StatusPolicy(
        status=InvoiceStatus.DUPLIKAT,
        stamp_allowed=False,
        xml_allowed=False,
        notification_type=NotificationType.DUPLICATE_ALERT,
        routing_family=RoutingFamily.DUPLICATE_HOLD,
        payable_outcome=False,
        manual_review_required=False,
    ),
}


ROUTING_TARGET_KEYS: dict[RoutingFamily, str] = {
    RoutingFamily.COMPANY_PAYABLE: "company_payable",
    RoutingFamily.PRIVATE_PAYABLE: "private_payable",
    RoutingFamily.PRIVATE_REVIEW_HOLD: "private_review_hold",
    RoutingFamily.AUTO_PAID_CARD: "auto_paid_card",
    RoutingFamily.REMINDER_REVIEW_HOLD: "reminder_review_hold",
    RoutingFamily.INVALID_HOLD: "invalid_hold",
    RoutingFamily.DUPLICATE_HOLD: "duplicate_hold",
}


LEGACY_BEHAVIORS_TO_FIX: list[str] = [
    "Step 21 MAIL_AN_FIRMA recipient mismatch: do_not_preserve_blindly.",
    "Google Sheets is legacy reference only; database is the primary system of record.",
    "PRIVAT_RECHNUNG and MAHNUNG are terminal review states, not automatically payable.",
    "Eligible company transfer invoices should support XML in the new system.",
]


def get_status_policy(status: InvoiceStatus) -> StatusPolicy:
    return STATUS_POLICIES[status]


def determine_routing_family(
    status: InvoiceStatus,
    payment_type: PaymentType | None,
    project_code: str | None = None,
) -> RoutingFamily:
    if status != InvoiceStatus.GUELTIG:
        return get_status_policy(status).routing_family

    if payment_type == PaymentType.AUTOMATISCH_BEZAHLT and status == InvoiceStatus.GUELTIG:
        return RoutingFamily.AUTO_PAID_CARD

    if project_code == "MARKUS_PRIVAT":
        return RoutingFamily.PRIVATE_PAYABLE

    return get_status_policy(status).routing_family


def _normalize_reference_token(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = "".join(char if char.isalnum() else "-" for char in value.strip().lower())
    normalized = normalized.strip("-")
    return normalized or None


def _build_xml_payload(
    *,
    processing_id: str,
    creditor_name: str | None,
    creditor_iban: str | None,
    amount,
    currency: str,
    reference_id: str,
    invoice_number: str | None,
    debtor_name: str | None,
    project_code: str | None,
) -> str:
    creditor_name_value = creditor_name or "UNBEKANNT"
    creditor_iban_value = creditor_iban or "HINTERLEGT"
    amount_value = "" if amount is None else str(amount)

    return "\n".join(
        (
            '<SepaPaymentDraft version="pain.001.001.03">',
            f"  <ProcessingId>{escape(processing_id)}</ProcessingId>",
            f"  <ReferenceId>{escape(reference_id)}</ReferenceId>",
            f"  <InvoiceNumber>{escape(invoice_number or '')}</InvoiceNumber>",
            f"  <CreditorName>{escape(creditor_name_value)}</CreditorName>",
            f"  <CreditorIban>{escape(creditor_iban_value)}</CreditorIban>",
            f"  <DebtorName>{escape(debtor_name or '')}</DebtorName>",
            f"  <ProjectCode>{escape(project_code or '')}</ProjectCode>",
            f'  <Amount currency="{escape(currency)}">{escape(amount_value)}</Amount>',
            "</SepaPaymentDraft>",
        )
    )


def build_xml_plan(
    *,
    processing_id: str,
    status: InvoiceStatus,
    payment_type: PaymentType | None,
    payable_outcome: bool = True,
    has_credit_note: bool = False,
    has_negative_amount: bool = False,
    has_creditor_iban: bool = True,
    is_employee_expense: bool = False,
    side_effects_enabled: bool = False,
    amount=None,
    currency: str = "EUR",
    creditor_name: str | None = None,
    creditor_iban: str | None = None,
    debtor_name: str | None = None,
    invoice_number: str | None = None,
    transfer_reference: str | None = None,
    project_code: str | None = None,
) -> PaymentXmlPlan:
    if status != InvoiceStatus.GUELTIG:
        return PaymentXmlPlan(
            processing_id=processing_id,
            disposition=XmlDisposition.NOT_ALLOWED,
            should_generate=False,
            should_store=False,
            planned_only=False,
            draft_filename=None,
            draft_payload=None,
            reference_id=None,
            reason=f"XML not allowed for terminal status {status.value}.",
        )

    if not payable_outcome:
        return PaymentXmlPlan(
            processing_id=processing_id,
            disposition=XmlDisposition.NOT_ALLOWED,
            should_generate=False,
            should_store=False,
            planned_only=False,
            draft_filename=None,
            draft_payload=None,
            reference_id=None,
            reason="XML blocked because the decision is not currently payable.",
        )

    if payment_type != PaymentType.UEBERWEISUNG:
        return PaymentXmlPlan(
            processing_id=processing_id,
            disposition=XmlDisposition.NOT_ALLOWED,
            should_generate=False,
            should_store=False,
            planned_only=False,
            draft_filename=None,
            draft_payload=None,
            reference_id=None,
            reason="XML only applies to transfer payments.",
        )

    if has_credit_note or has_negative_amount:
        return PaymentXmlPlan(
            processing_id=processing_id,
            disposition=XmlDisposition.NOT_ALLOWED,
            should_generate=False,
            should_store=False,
            planned_only=False,
            draft_filename=None,
            draft_payload=None,
            reference_id=None,
            reason="XML blocked for credit notes, cancellations, or negative totals.",
        )

    if not has_creditor_iban and not is_employee_expense:
        return PaymentXmlPlan(
            processing_id=processing_id,
            disposition=XmlDisposition.NOT_ALLOWED,
            should_generate=False,
            should_store=False,
            planned_only=False,
            draft_filename=None,
            draft_payload=None,
            reference_id=None,
            reason="XML blocked because creditor IBAN is missing.",
        )

    reference_id = (
        _normalize_reference_token(transfer_reference)
        or _normalize_reference_token(invoice_number)
        or _normalize_reference_token(processing_id)
        or processing_id
    )
    draft_filename = f"{processing_id}_payment_draft.xml"
    draft_payload = _build_xml_payload(
        processing_id=processing_id,
        creditor_name=creditor_name,
        creditor_iban=creditor_iban if has_creditor_iban else None,
        amount=amount,
        currency=currency or "EUR",
        reference_id=reference_id,
        invoice_number=invoice_number,
        debtor_name=debtor_name,
        project_code=project_code,
    )

    if is_employee_expense and not has_creditor_iban:
        return PaymentXmlPlan(
            processing_id=processing_id,
            disposition=XmlDisposition.PLAN_ONLY,
            should_generate=True,
            should_store=False,
            planned_only=True,
            draft_filename=draft_filename,
            draft_payload=draft_payload,
            reference_id=reference_id,
            reason="Employee expense fallback requires explicit product approval before storage.",
        )

    return PaymentXmlPlan(
        processing_id=processing_id,
        disposition=XmlDisposition.GENERATE_AND_STORE if side_effects_enabled else XmlDisposition.PLAN_ONLY,
        should_generate=True,
        should_store=side_effects_enabled,
        planned_only=not side_effects_enabled,
        draft_filename=draft_filename,
        draft_payload=draft_payload,
        reference_id=reference_id,
        reason="Eligible transfer invoice.",
    )
