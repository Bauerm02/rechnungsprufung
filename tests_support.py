from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session, sessionmaker

from invoice_automation.adapters.mocks.vat_service import MockVatService
from invoice_automation.application.decisioning import DecisioningService
from invoice_automation.domain.enums import InvoiceStatus
from invoice_automation.domain.models import ExtractionBundle, InvoiceRegistryEntry, VatValidationResponse
from invoice_automation.duplicate_detection.service import DuplicateDetectionService
from invoice_automation.infrastructure.db.repositories.duplicate_repository import SqlAlchemyDuplicateRepository
from invoice_automation.rule_engine.service import RuleEngine
from invoice_automation.validation.normalizers import normalize_content_hash, normalize_iban, normalize_invoice_number, normalize_sender_name
from invoice_automation.validation.service import ValidationService


def make_bundle(*, processing_id: str, text: str) -> ExtractionBundle:
    return ExtractionBundle(
        processing_id=processing_id,
        full_raw_text=text,
        full_normalized_text=text,
    )


def make_vat_service(responses: dict[str, dict[str, object]] | None = None) -> MockVatService:
    vat_responses = {
        vat_id: VatValidationResponse(
            vat_id=vat_id,
            valid=payload.get("valid"),
            checked=bool(payload.get("checked", False)),
            reason=payload.get("reason"),
        )
        for vat_id, payload in (responses or {}).items()
    }
    return MockVatService(responses=vat_responses)


def make_decisioning_service(
    session_factory: sessionmaker[Session],
    *,
    vat_responses: dict[str, dict[str, object]] | None = None,
    side_effects_enabled: bool = False,
) -> DecisioningService:
    validation_service = ValidationService(vat_service=make_vat_service(vat_responses))
    duplicate_service = DuplicateDetectionService(SqlAlchemyDuplicateRepository(session_factory))
    return DecisioningService(
        validation_service=validation_service,
        duplicate_service=duplicate_service,
        rule_engine=RuleEngine(),
        side_effects_enabled=side_effects_enabled,
    )


def register_existing_invoice(
    session_factory: sessionmaker[Session],
    *,
    processing_id: str,
    sender_name: str,
    invoice_number: str | None,
    document_date: date | None = None,
    recipient_name: str | None = None,
    iban: str | None = None,
    content_hash: str | None = None,
    amount: Decimal | None = None,
    status: InvoiceStatus = InvoiceStatus.GUELTIG,
) -> None:
    repository = SqlAlchemyDuplicateRepository(session_factory)
    repository.register(
        InvoiceRegistryEntry(
            processing_id=processing_id,
            sender_name_normalized=normalize_sender_name(sender_name) or "",
            invoice_number_normalized=normalize_invoice_number(invoice_number),
            document_date=document_date,
            recipient_name_normalized=normalize_sender_name(recipient_name),
            iban_normalized=normalize_iban(iban),
            content_hash=normalize_content_hash(content_hash),
            amount_decimal=amount,
            status=status,
        )
    )
