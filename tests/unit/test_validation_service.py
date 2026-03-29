from decimal import Decimal

from invoice_automation.domain.enums import ValidationSeverity
from invoice_automation.domain.models import ExtractedInvoiceData
from invoice_automation.validation.service import ValidationService
from tests_support import make_bundle, make_vat_service


def test_validation_service_normalizes_and_warns_on_invalid_iban() -> None:
    service = ValidationService(vat_service=make_vat_service())
    extracted = ExtractedInvoiceData(
        processing_id="proc_validate",
        sender_name="Mabau Beteiligungs GmbH",
        invoice_number="INV 100/2026",
        amount=Decimal("120.00"),
        iban="AT001234",
    )

    result = service.validate(extracted, bundle=make_bundle(processing_id=extracted.processing_id, text="Rechnung 120,00 EUR."))

    assert result.normalized.sender_name_normalized == "mabau beteiligungs gmbh"
    assert result.normalized.invoice_number_normalized == "INV1002026"
    assert any(message.severity == ValidationSeverity.WARNING for message in result.messages)


def test_validation_service_flags_missing_document_text_as_error() -> None:
    service = ValidationService(vat_service=make_vat_service())
    extracted = ExtractedInvoiceData(
        processing_id="proc_empty_text",
        sender_name="Lieferant GmbH",
        invoice_number="INV-1",
        amount=Decimal("42.00"),
    )

    result = service.validate(extracted, bundle=make_bundle(processing_id=extracted.processing_id, text=""))

    assert any(message.code == "document_text_missing" for message in result.messages)


def test_validation_service_reports_invalid_vat_checks() -> None:
    service = ValidationService(
        vat_service=make_vat_service(
            {
                "ATU12345678": {"valid": False, "checked": True, "reason": "Mock invalid supplier VAT."},
                "ATU87654321": {"valid": False, "checked": True, "reason": "Mock invalid recipient VAT."},
            }
        )
    )
    extracted = ExtractedInvoiceData(
        processing_id="proc_invalid_vat",
        sender_name="Lieferant GmbH",
        invoice_number="INV-2",
        amount=Decimal("120.00"),
        uid_sender="ATU12345678",
        uid_recipient="ATU87654321",
    )

    result = service.validate(extracted, bundle=make_bundle(processing_id=extracted.processing_id, text="Rechnung 120,00 EUR."))

    assert any(message.code == "sender_vat_invalid" for message in result.messages)
    assert any(message.code == "recipient_vat_invalid" for message in result.messages)
