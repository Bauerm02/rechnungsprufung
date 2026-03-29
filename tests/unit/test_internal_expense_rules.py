from decimal import Decimal

from invoice_automation.domain.enums import InvoiceStatus, PaymentType, XmlDisposition
from invoice_automation.domain.models import ExtractedInvoiceData
from tests_support import make_bundle, make_decisioning_service


def test_internal_expense_detects_employee_reimbursement_without_invoice_keyword(session_factory) -> None:
    service = make_decisioning_service(session_factory)
    extracted = ExtractedInvoiceData(
        processing_id="proc_internal_employee",
        sender_name="Markus Bauer",
        recipient_name="Mabau Beteiligungs GmbH",
        invoice_number="INT-1",
        amount=Decimal("89.00"),
    )

    outcome = service.decide(
        bundle=make_bundle(
            processing_id=extracted.processing_id,
            text="Markus Bauer hat privat vorgestreckt: Wallbox Stromkosten 89,00 EUR.",
        ),
        extracted=extracted,
    )

    assert outcome.rule_decision.status == InvoiceStatus.GUELTIG
    assert outcome.facts.internal_expense_detected is True
    assert outcome.facts.internal_expense_employee_name == "markus bauer"
    assert outcome.rule_decision.project_code == "MBB"
    assert outcome.rule_decision.payment_type == PaymentType.UEBERWEISUNG


def test_internal_expense_without_iban_uses_hinterlegt_fallback_marker(session_factory) -> None:
    service = make_decisioning_service(session_factory)
    extracted = ExtractedInvoiceData(
        processing_id="proc_internal_hinterlegt",
        sender_name="Markus Bauer",
        recipient_name="Mabau Beteiligungs GmbH",
        invoice_number="INT-2",
        amount=Decimal("89.00"),
    )

    outcome = service.decide(
        bundle=make_bundle(
            processing_id=extracted.processing_id,
            text="Markus Bauer hat privat vorgestreckt: Wallbox Stromkosten 89,00 EUR.",
        ),
        extracted=extracted,
    )

    assert outcome.facts.internal_expense_creditor_iban_fallback == "HINTERLEGT"
    assert outcome.xml_plan.disposition == XmlDisposition.PLAN_ONLY
