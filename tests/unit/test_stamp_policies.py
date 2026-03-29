from decimal import Decimal

from invoice_automation.domain.enums import InvoiceStatus
from invoice_automation.domain.models import ExtractedInvoiceData
from tests_support import make_bundle, make_decisioning_service


def test_stamp_plan_includes_uid_and_iban_warnings_for_manual_review_case(session_factory) -> None:
    service = make_decisioning_service(session_factory)
    extracted = ExtractedInvoiceData(
        processing_id="proc_stamp_warning",
        sender_name="Elektro Mayer GmbH",
        recipient_name="Sieben Dorfer Immobilien GmbH",
        invoice_number="STAMP-850",
        amount=Decimal("850.00"),
        iban="AT001234",
    )

    outcome = service.decide(
        bundle=make_bundle(processing_id=extracted.processing_id, text="Rechnung Elektro Mayer GmbH 850,00 EUR."),
        extracted=extracted,
    )

    assert outcome.rule_decision.status == InvoiceStatus.GUELTIG
    assert outcome.stamp_plan.allowed is True
    assert outcome.stamp_plan.should_render is True
    assert outcome.stamp_plan.draft_filename == "proc_stamp_warning_stamped_draft.pdf"
    assert "UID nicht lesbar - bitte pruefen" in outcome.stamp_plan.lines
    assert "IBAN fehlerhaft - bitte pruefen" in outcome.stamp_plan.lines
    assert "MANUELLE PRUEFUNG ERFORDERLICH" in outcome.stamp_plan.lines


def test_reminder_stamp_policy_stays_disabled(session_factory) -> None:
    service = make_decisioning_service(session_factory)
    extracted = ExtractedInvoiceData(
        processing_id="proc_stamp_reminder",
        sender_name="Lieferant GmbH",
        recipient_name="Sieben Dorfer Immobilien GmbH",
        invoice_number="REM-1",
        amount=Decimal("450.00"),
    )

    outcome = service.decide(
        bundle=make_bundle(processing_id=extracted.processing_id, text="Zahlungserinnerung fuer offene Rechnung 450,00 EUR."),
        extracted=extracted,
    )

    assert outcome.rule_decision.status == InvoiceStatus.MAHNUNG
    assert outcome.stamp_plan.allowed is False
    assert outcome.stamp_plan.should_render is False


def test_valid_invoice_without_overlay_lines_does_not_plan_stamped_pdf(session_factory) -> None:
    service = make_decisioning_service(session_factory)
    extracted = ExtractedInvoiceData(
        processing_id="proc_stamp_clean",
        sender_name="Objektpflege GmbH",
        recipient_name="Gutenberg Projekt GmbH",
        invoice_number="GB-1200",
        amount=Decimal("1200.00"),
        iban="AT611904300234573201",
        uid_sender="ATU12345678",
        uid_recipient="ATU87654321",
    )

    outcome = service.decide(
        bundle=make_bundle(
            processing_id=extracted.processing_id,
            text="Empfaenger Gutenberg Projekt GmbH. Rechnung fuer Objektpflege 1200,00 EUR.",
        ),
        extracted=extracted,
    )

    assert outcome.rule_decision.status == InvoiceStatus.GUELTIG
    assert outcome.stamp_plan.allowed is True
    assert outcome.stamp_plan.should_render is False
    assert outcome.stamp_plan.draft_filename is None
