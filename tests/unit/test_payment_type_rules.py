from decimal import Decimal

from invoice_automation.domain.enums import PaymentType
from invoice_automation.domain.models import ExtractedInvoiceData
from invoice_automation.rule_engine.classifiers import derive_rule_facts
from invoice_automation.validation.service import ValidationService
from tests_support import make_bundle, make_vat_service


def _payment_type(text: str, *, sender_name: str) -> PaymentType:
    extracted = ExtractedInvoiceData(
        processing_id="proc_payment",
        sender_name=sender_name,
        amount=Decimal("99.00"),
    )
    bundle = make_bundle(processing_id=extracted.processing_id, text=text)
    validation = ValidationService(vat_service=make_vat_service()).validate(extracted, bundle=bundle)
    return derive_rule_facts(bundle, extracted, validation).payment_type_determined


def test_auto_paid_vendor_list_marks_saas_invoice_as_auto_paid() -> None:
    assert _payment_type("OpenAI API subscription 99,00 EUR", sender_name="OpenAI") == PaymentType.AUTOMATISCH_BEZAHLT


def test_exception_vendor_stays_transfer_even_when_google_supplier_is_used() -> None:
    assert _payment_type("Google Ireland Limited Ads Rechnung 99,00 EUR", sender_name="Google Ireland Limited") == PaymentType.UEBERWEISUNG


def test_reverse_charge_forces_transfer_even_for_typical_saas_vendor() -> None:
    assert (
        _payment_type(
            "OpenAI Rechnung Reverse Charge gemaess Steuerschuldnerschaft 99,00 EUR",
            sender_name="OpenAI",
        )
        == PaymentType.UEBERWEISUNG
    )


def test_transfer_reference_forces_transfer_even_for_typical_auto_paid_vendor() -> None:
    assert (
        _payment_type(
            "OpenAI Rechnung 99,00 EUR. Kreditkarte hinterlegt. Bitte bei der Ueberweisung Verwendungszweck: AI-2026-44 angeben.",
            sender_name="OpenAI",
        )
        == PaymentType.UEBERWEISUNG
    )
