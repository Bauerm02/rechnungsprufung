from decimal import Decimal

from invoice_automation.domain.models import ExtractedInvoiceData
from invoice_automation.rule_engine.classifiers import derive_rule_facts
from invoice_automation.validation.service import ValidationService
from tests_support import make_bundle, make_vat_service


def _derive(text: str, extracted: ExtractedInvoiceData):
    bundle = make_bundle(processing_id=extracted.processing_id, text=text)
    validation = ValidationService(vat_service=make_vat_service()).validate(extracted, bundle=bundle)
    return derive_rule_facts(bundle, extracted, validation)


def test_rule_facts_detect_authority_even_when_reminder_keywords_are_present() -> None:
    facts = _derive(
        "Finanzamt Oesterreich Zahlungserinnerung fuer Abgabenkonto 123. Gesamtbetrag 250,00 EUR.",
        ExtractedInvoiceData(
            processing_id="proc_authority",
            sender_name="Finanzamt Oesterreich",
            amount=Decimal("250.00"),
        ),
    )

    assert facts.authority_detected is True
    assert facts.reminder_detected is True


def test_rule_facts_detect_redlinghofer_special_case() -> None:
    facts = _derive(
        "Rechnung an Pension Redlinghofer GmbH ueber 200,00 EUR.",
        ExtractedInvoiceData(
            processing_id="proc_redlinghofer",
            sender_name="Installateur Graz GmbH",
            recipient_name="Pension Redlinghofer GmbH",
            amount=Decimal("200.00"),
        ),
    )

    assert facts.redlinghofer_only_recipient is True
    assert facts.project_code == "HTV"


def test_rule_facts_force_internal_expense_to_mbb_project() -> None:
    facts = _derive(
        "Abrechnung Stromkosten Wallbox fuer betriebliche Ausgaben 89,00 EUR.",
        ExtractedInvoiceData(
            processing_id="proc_internal_expense",
            sender_name="Markus Bauer",
            recipient_name="Mabau Beteiligungs GmbH",
            amount=Decimal("89.00"),
        ),
    )

    assert facts.internal_expense_detected is True
    assert facts.project_code == "MBB"


def test_rule_facts_build_stamp_text_for_allowed_project() -> None:
    facts = _derive(
        "Empfaenger: Sieben Dorfer Immobilien GmbH. Rechnung Grundsteuer und Kanalgebuehren 400,00 EUR.",
        ExtractedInvoiceData(
            processing_id="proc_stamp",
            sender_name="Stadt Graz",
            recipient_name="Sieben Dorfer Immobilien GmbH",
            amount=Decimal("400.00"),
        ),
    )

    assert facts.project_code == "7DI"
    assert facts.stamp_text == "HAUSVERWALTUNG: Betriebskosten (MRG Vollanwendung)"


def test_rule_facts_extract_transfer_reference_from_explicit_payment_instruction() -> None:
    facts = _derive(
        "Empfaenger Gutenberg Projekt GmbH. Bitte bei der Ueberweisung Verwendungszweck: GB-2026-44 angeben.",
        ExtractedInvoiceData(
            processing_id="proc_transfer_reference",
            sender_name="Dienstleister GmbH",
            recipient_name="Gutenberg Projekt GmbH",
            amount=Decimal("400.00"),
        ),
    )

    assert facts.transfer_reference_detected is True
    assert facts.transfer_reference_value == "gb-2026-44"


def test_rule_facts_extract_transfer_reference_from_unlabeled_instruction_line() -> None:
    facts = _derive(
        "Empfaenger Gutenberg Projekt GmbH.\nBitte bei der Ueberweisung angeben:\nGB 2026 77\nBetrag 400,00 EUR.",
        ExtractedInvoiceData(
            processing_id="proc_transfer_reference_unlabeled",
            sender_name="Dienstleister GmbH",
            recipient_name="Gutenberg Projekt GmbH",
            amount=Decimal("400.00"),
        ),
    )

    assert facts.transfer_reference_detected is True
    assert facts.transfer_reference_value == "gb-2026-77"


def test_rule_facts_prioritize_explicit_foreign_supplier_signals_over_generic_austria_text() -> None:
    facts = _derive(
        "Empfaenger Sieben Dorfer Immobilien GmbH Austria. Reverse Charge gemaess Steuerschuldnerschaft.",
        ExtractedInvoiceData(
            processing_id="proc_reverse_charge_foreign_signal",
            sender_name="Irish Software Ltd",
            recipient_name="Sieben Dorfer Immobilien GmbH",
            amount=Decimal("800.00"),
            uid_sender="IE1234567A",
        ),
    )

    assert facts.reverse_charge_detected is True
    assert facts.reverse_charge_domestic is False


def test_rule_facts_detect_reverse_charge_conflict_when_supplier_signals_disagree() -> None:
    facts = _derive(
        "Empfaenger Sieben Dorfer Immobilien GmbH. Reverse Charge gemaess Steuerschuldnerschaft.",
        ExtractedInvoiceData(
            processing_id="proc_reverse_charge_conflict",
            sender_name="Bau GmbH",
            recipient_name="Sieben Dorfer Immobilien GmbH",
            amount=Decimal("800.00"),
            iban="DE89370400440532013000",
            uid_sender="ATU12345678",
        ),
    )

    assert facts.reverse_charge_detected is True
    assert facts.reverse_charge_conflict_detected is True
    assert facts.reverse_charge_domestic is None
