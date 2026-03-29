from decimal import Decimal

from invoice_automation.domain.enums import InvoiceStatus, NotificationType
from invoice_automation.domain.models import ExtractedInvoiceData
from tests_support import make_bundle, make_decisioning_service


def test_law_firm_sender_uses_legal_entity_uid_fallback(session_factory) -> None:
    service = make_decisioning_service(session_factory)
    extracted = ExtractedInvoiceData(
        processing_id="proc_law_firm",
        sender_name="Rechtsanwaltskanzlei Muster",
        recipient_name="Sieben Dorfer Immobilien GmbH",
        invoice_number="LAW-1",
        amount=Decimal("900.00"),
    )

    outcome = service.decide(
        bundle=make_bundle(processing_id=extracted.processing_id, text="Honorarrechnung 900,00 EUR."),
        extracted=extracted,
    )

    assert outcome.rule_decision.status == InvoiceStatus.GUELTIG
    assert outcome.rule_decision.notification_type == NotificationType.ACCOUNTING_INFO
    assert outcome.rule_decision.payable_outcome is False
    assert outcome.facts.sender_legal_entity_detected is True
