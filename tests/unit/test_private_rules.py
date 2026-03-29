from decimal import Decimal

from invoice_automation.domain.enums import InvoiceStatus
from invoice_automation.domain.models import ExtractedInvoiceData
from tests_support import make_bundle, make_decisioning_service


def test_explicit_private_keyword_causes_private_review_hold(session_factory) -> None:
    service = make_decisioning_service(session_factory)
    extracted = ExtractedInvoiceData(
        processing_id="proc_private_keyword",
        sender_name="Shop GmbH",
        recipient_name="Markus Bauer",
        invoice_number="PRIV-1",
        amount=Decimal("55.00"),
    )

    outcome = service.decide(
        bundle=make_bundle(
            processing_id=extracted.processing_id,
            text="Privatrechnung fuer private Zwecke 55,00 EUR.",
        ),
        extracted=extracted,
    )

    assert outcome.rule_decision.status == InvoiceStatus.PRIVAT_RECHNUNG
    assert outcome.rule_decision.payable_outcome is False


def test_government_penalty_private_markus_is_not_reclassified_as_private_hold(session_factory) -> None:
    service = make_decisioning_service(session_factory)
    extracted = ExtractedInvoiceData(
        processing_id="proc_markus_penalty",
        sender_name="Magistrat der Stadt Wien",
        recipient_name="Markus Bauer",
        invoice_number="PEN-1",
        amount=Decimal("100.00"),
    )

    outcome = service.decide(
        bundle=make_bundle(
            processing_id=extracted.processing_id,
            text="Magistrat der Stadt Wien Zwangsstrafverfuegung fuer Markus Bauer 100,00 EUR.",
        ),
        extracted=extracted,
    )

    assert outcome.rule_decision.status == InvoiceStatus.GUELTIG
    assert outcome.facts.private_detected is False
