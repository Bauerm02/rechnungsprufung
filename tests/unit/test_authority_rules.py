from decimal import Decimal

from invoice_automation.domain.enums import InvoiceStatus
from invoice_automation.domain.models import ExtractedInvoiceData
from tests_support import make_bundle, make_decisioning_service


def test_authority_whitelist_sender_triggers_authority_rule(session_factory) -> None:
    service = make_decisioning_service(session_factory)
    extracted = ExtractedInvoiceData(
        processing_id="proc_authority_whitelist",
        sender_name="Finanzamt Oesterreich",
        recipient_name="Sieben Dorfer Immobilien GmbH",
        invoice_number="FA-1",
        amount=Decimal("100.00"),
    )

    outcome = service.decide(
        bundle=make_bundle(processing_id=extracted.processing_id, text="Abgabenkonto 123, Gesamtbetrag 100,00 EUR."),
        extracted=extracted,
    )

    assert outcome.rule_decision.status == InvoiceStatus.GUELTIG
    assert outcome.facts.authority_detected is True


def test_authority_pattern_sender_triggers_authority_rule(session_factory) -> None:
    service = make_decisioning_service(session_factory)
    extracted = ExtractedInvoiceData(
        processing_id="proc_authority_pattern",
        sender_name="Bezirksgericht Graz-Ost",
        recipient_name="Sieben Dorfer Immobilien GmbH",
        invoice_number="BG-1",
        amount=Decimal("180.00"),
    )

    outcome = service.decide(
        bundle=make_bundle(processing_id=extracted.processing_id, text="Gerichtliche Gebuehr 180,00 EUR."),
        extracted=extracted,
    )

    assert outcome.rule_decision.status == InvoiceStatus.GUELTIG
    assert outcome.facts.authority_detected is True
