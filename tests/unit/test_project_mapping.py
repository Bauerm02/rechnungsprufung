from decimal import Decimal

from invoice_automation.domain.models import ExtractedInvoiceData
from invoice_automation.rule_engine.classifiers import derive_rule_facts
from invoice_automation.validation.service import ValidationService
from tests_support import make_bundle, make_vat_service


def _project_code(text: str, *, sender_name: str, recipient_name: str | None = None) -> str | None:
    extracted = ExtractedInvoiceData(
        processing_id="proc_project",
        sender_name=sender_name,
        recipient_name=recipient_name,
        amount=Decimal("500.00"),
    )
    bundle = make_bundle(processing_id=extracted.processing_id, text=text)
    validation = ValidationService(vat_service=make_vat_service()).validate(extracted, bundle=bundle)
    return derive_rule_facts(bundle, extracted, validation).project_code


def test_project_mapping_uses_company_aliases() -> None:
    assert (
        _project_code(
            "Empfaenger Gutenberg Projekt GmbH, Rechnung fuer Objektbetreuung 500,00 EUR.",
            sender_name="Hausverwaltung Muster GmbH",
            recipient_name="Gutenberg Projekt GmbH",
        )
        == "GB"
    )


def test_project_mapping_marks_government_penalty_for_markus_as_private() -> None:
    assert (
        _project_code(
            "Magistrat der Stadt Wien Zwangsstrafverfuegung fuer Markus Bauer 500,00 EUR.",
            sender_name="Magistrat der Stadt Wien",
            recipient_name="Markus Bauer",
        )
        == "MARKUS_PRIVAT"
    )


def test_project_mapping_uses_private_person_fallback() -> None:
    assert (
        _project_code(
            "Empfaenger Markus Bauer, Rechnung fuer privat gebuchte Leistung 500,00 EUR.",
            sender_name="Privater Dienstleister",
            recipient_name="Markus Bauer",
        )
        == "MARKUS_PRIVAT"
    )


def test_project_mapping_repairs_mild_ocr_damage_in_recipient_name() -> None:
    assert (
        _project_code(
            "Empfaenger 5ieben D0rfer 1mmobilien Gm6H, Rechnung fuer Objektbetreuung 500,00 EUR.",
            sender_name="Hausverwaltung Muster GmbH",
            recipient_name="5ieben D0rfer 1mmobilien Gm6H",
        )
        == "7DI"
    )


def test_project_mapping_tolerates_mild_token_order_and_spacing_damage() -> None:
    assert (
        _project_code(
            "Empfaenger Immobilien GmbH SiebenDorfer, Rechnung fuer Objektbetreuung 500,00 EUR.",
            sender_name="Hausverwaltung Muster GmbH",
            recipient_name="Immobilien GmbH SiebenDorfer",
        )
        == "7DI"
    )
