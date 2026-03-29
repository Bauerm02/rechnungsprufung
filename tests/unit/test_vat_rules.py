from decimal import Decimal

from invoice_automation.domain.enums import InvoiceStatus, NotificationType
from invoice_automation.domain.models import ExtractedInvoiceData
from tests_support import make_bundle, make_decisioning_service


def test_invalid_supplier_vat_with_legal_entity_stays_valid_but_requires_review(session_factory) -> None:
    service = make_decisioning_service(
        session_factory,
        vat_responses={
            "ATU12345678": {"valid": False, "checked": True, "reason": "Mock invalid supplier VAT."},
        },
    )
    extracted = ExtractedInvoiceData(
        processing_id="proc_sender_vat_invalid",
        sender_name="Elektro Mayer GmbH",
        recipient_name="Sieben Dorfer Immobilien GmbH",
        invoice_number="VAT-850",
        amount=Decimal("850.00"),
        uid_sender="ATU12345678",
    )

    outcome = service.decide(
        bundle=make_bundle(processing_id=extracted.processing_id, text="Rechnung Elektro Mayer GmbH 850,00 EUR."),
        extracted=extracted,
    )

    assert outcome.rule_decision.status == InvoiceStatus.GUELTIG
    assert outcome.rule_decision.notification_type == NotificationType.ACCOUNTING_INFO
    assert outcome.rule_decision.manual_review_required is True
    assert outcome.rule_decision.payable_outcome is False


def test_invalid_recipient_vat_invalidates_invoice(session_factory) -> None:
    service = make_decisioning_service(
        session_factory,
        vat_responses={
            "ATU12345678": {"valid": True, "checked": True, "reason": "Mock valid supplier VAT."},
            "ATU87654321": {"valid": False, "checked": True, "reason": "Mock invalid recipient VAT."},
        },
    )
    extracted = ExtractedInvoiceData(
        processing_id="proc_recipient_vat_invalid",
        sender_name="Bau GmbH",
        recipient_name="Sieben Dorfer Immobilien GmbH",
        invoice_number="VAT-12000",
        amount=Decimal("12000.00"),
        uid_sender="ATU12345678",
        uid_recipient="ATU87654321",
        iban="AT611904300234573201",
    )

    outcome = service.decide(
        bundle=make_bundle(processing_id=extracted.processing_id, text="Rechnung Bau GmbH 12000,00 EUR."),
        extracted=extracted,
    )

    assert outcome.rule_decision.status == InvoiceStatus.UNGUELTIG
    assert outcome.rule_decision.notification_type == NotificationType.ACCOUNTING_INFO
