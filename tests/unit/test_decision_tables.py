from decimal import Decimal

from invoice_automation.application.decision_tables import build_xml_plan, determine_routing_family, get_status_policy
from invoice_automation.domain.enums import InvoiceStatus, PaymentType, RoutingFamily, XmlDisposition


def test_private_invoice_policy_is_terminal_and_no_xml() -> None:
    policy = get_status_policy(InvoiceStatus.PRIVAT_RECHNUNG)

    assert policy.manual_review_required is True
    assert policy.xml_allowed is False
    assert policy.stamp_allowed is False
    assert policy.payable_outcome is False
    assert policy.routing_family == RoutingFamily.PRIVATE_REVIEW_HOLD


def test_reminder_policy_requires_manual_review_and_no_xml() -> None:
    policy = get_status_policy(InvoiceStatus.MAHNUNG)

    assert policy.manual_review_required is True
    assert policy.xml_allowed is False
    assert policy.stamp_allowed is False
    assert policy.payable_outcome is False
    assert policy.routing_family == RoutingFamily.REMINDER_REVIEW_HOLD


def test_duplicate_policy_is_terminal_hold() -> None:
    policy = get_status_policy(InvoiceStatus.DUPLIKAT)

    assert policy.stamp_allowed is False
    assert policy.xml_allowed is False
    assert policy.payable_outcome is False
    assert policy.routing_family == RoutingFamily.DUPLICATE_HOLD


def test_auto_paid_valid_invoice_routes_to_card_family() -> None:
    routing_family = determine_routing_family(InvoiceStatus.GUELTIG, PaymentType.AUTOMATISCH_BEZAHLT)
    assert routing_family == RoutingFamily.AUTO_PAID_CARD


def test_markus_private_valid_transfer_routes_to_private_payable_family() -> None:
    routing_family = determine_routing_family(InvoiceStatus.GUELTIG, PaymentType.UEBERWEISUNG, project_code="MARKUS_PRIVAT")
    assert routing_family == RoutingFamily.PRIVATE_PAYABLE


def test_company_transfer_xml_is_planned_when_side_effects_are_disabled() -> None:
    plan = build_xml_plan(
        processing_id="proc_test",
        status=InvoiceStatus.GUELTIG,
        payment_type=PaymentType.UEBERWEISUNG,
        has_credit_note=False,
        has_negative_amount=False,
        has_creditor_iban=True,
        side_effects_enabled=False,
    )

    assert plan.should_generate is True
    assert plan.should_store is False
    assert plan.planned_only is True
    assert plan.disposition == XmlDisposition.PLAN_ONLY


def test_reminder_never_gets_xml() -> None:
    plan = build_xml_plan(
        processing_id="proc_test",
        status=InvoiceStatus.MAHNUNG,
        payment_type=PaymentType.UEBERWEISUNG,
    )

    assert plan.should_generate is False
    assert plan.disposition == XmlDisposition.NOT_ALLOWED


def test_xml_plan_builds_deterministic_draft_payload_from_transfer_reference() -> None:
    plan = build_xml_plan(
        processing_id="proc_xml",
        status=InvoiceStatus.GUELTIG,
        payment_type=PaymentType.UEBERWEISUNG,
        has_credit_note=False,
        has_negative_amount=False,
        has_creditor_iban=True,
        amount=Decimal("99.00"),
        creditor_name="OpenAI",
        creditor_iban="DE89370400440532013000",
        debtor_name="Gutenberg Projekt GmbH",
        invoice_number="AI-99",
        transfer_reference="AI-2026-77",
        project_code="GB",
        side_effects_enabled=False,
    )

    assert plan.should_generate is True
    assert plan.reference_id == "ai-2026-77"
    assert plan.draft_filename == "proc_xml_payment_draft.xml"
    assert "<ReferenceId>ai-2026-77</ReferenceId>" in (plan.draft_payload or "")
    assert "<CreditorName>OpenAI</CreditorName>" in (plan.draft_payload or "")


def test_employee_expense_xml_draft_uses_placeholder_when_iban_is_missing() -> None:
    plan = build_xml_plan(
        processing_id="proc_employee_xml",
        status=InvoiceStatus.GUELTIG,
        payment_type=PaymentType.UEBERWEISUNG,
        has_credit_note=False,
        has_negative_amount=False,
        has_creditor_iban=False,
        is_employee_expense=True,
        amount=Decimal("89.00"),
        creditor_name="Markus Bauer",
        creditor_iban=None,
        debtor_name="Mabau Beteiligungs GmbH",
        invoice_number="EXP-89",
        project_code="MBB",
        side_effects_enabled=False,
    )

    assert plan.disposition == XmlDisposition.PLAN_ONLY
    assert plan.should_generate is True
    assert "<CreditorIban>HINTERLEGT</CreditorIban>" in (plan.draft_payload or "")
