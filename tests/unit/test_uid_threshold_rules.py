from decimal import Decimal

from invoice_automation.domain.enums import InvoiceStatus, NotificationType
from invoice_automation.domain.models import ExtractedInvoiceData
from tests_support import make_bundle, make_decisioning_service


def test_amount_up_to_400_does_not_require_supplier_uid(session_factory) -> None:
    service = make_decisioning_service(session_factory)
    extracted = ExtractedInvoiceData(
        processing_id="proc_small_amount",
        sender_name="Max Muster",
        recipient_name="Sieben Dorfer Immobilien GmbH",
        invoice_number="INV-400",
        amount=Decimal("399.99"),
    )

    outcome = service.decide(
        bundle=make_bundle(processing_id=extracted.processing_id, text="Rechnung 399,99 EUR fuer Objektpflege."),
        extracted=extracted,
    )

    assert outcome.rule_decision.status == InvoiceStatus.GUELTIG
    assert outcome.rule_decision.payable_outcome is True


def test_missing_supplier_uid_with_legal_entity_goes_to_manual_review(session_factory) -> None:
    service = make_decisioning_service(session_factory)
    extracted = ExtractedInvoiceData(
        processing_id="proc_uid_legal_entity",
        sender_name="Elektro Mayer GmbH",
        recipient_name="Sieben Dorfer Immobilien GmbH",
        invoice_number="INV-850",
        amount=Decimal("850.00"),
    )

    outcome = service.decide(
        bundle=make_bundle(processing_id=extracted.processing_id, text="Rechnung Elektro Mayer GmbH 850,00 EUR."),
        extracted=extracted,
    )

    assert outcome.rule_decision.status == InvoiceStatus.GUELTIG
    assert outcome.rule_decision.manual_review_required is True
    assert outcome.rule_decision.payable_outcome is False
    assert outcome.rule_decision.notification_type == NotificationType.ACCOUNTING_INFO


def test_missing_supplier_uid_without_legal_entity_requests_supplier_correction(session_factory) -> None:
    service = make_decisioning_service(session_factory)
    extracted = ExtractedInvoiceData(
        processing_id="proc_uid_private",
        sender_name="Max Muster",
        recipient_name="Sieben Dorfer Immobilien GmbH",
        invoice_number="INV-851",
        amount=Decimal("850.00"),
        supplier_email="office@example.com",
    )

    outcome = service.decide(
        bundle=make_bundle(processing_id=extracted.processing_id, text="Rechnung Max Muster 850,00 EUR."),
        extracted=extracted,
    )

    assert outcome.rule_decision.status == InvoiceStatus.UNGUELTIG
    assert outcome.rule_decision.notification_type == NotificationType.SUPPLIER_CORRECTION


def test_missing_recipient_uid_over_10000_is_invalid(session_factory) -> None:
    service = make_decisioning_service(session_factory)
    extracted = ExtractedInvoiceData(
        processing_id="proc_uid_recipient_missing",
        sender_name="Bau GmbH",
        recipient_name="Sieben Dorfer Immobilien GmbH",
        invoice_number="INV-10001",
        amount=Decimal("12000.00"),
        uid_sender="ATU12345678",
    )

    outcome = service.decide(
        bundle=make_bundle(processing_id=extracted.processing_id, text="Rechnung Bau GmbH 12.000,00 EUR."),
        extracted=extracted,
    )

    assert outcome.rule_decision.status == InvoiceStatus.UNGUELTIG
    assert outcome.rule_decision.notification_type == NotificationType.ACCOUNTING_INFO
