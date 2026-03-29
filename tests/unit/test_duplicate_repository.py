from datetime import date
from decimal import Decimal

from invoice_automation.domain.enums import InvoiceStatus
from invoice_automation.domain.models import InvoiceRegistryEntry
from invoice_automation.infrastructure.db.repositories.duplicate_repository import SqlAlchemyDuplicateRepository


def test_duplicate_repository_returns_miss_for_unknown_invoice(session_factory) -> None:
    repository = SqlAlchemyDuplicateRepository(session_factory)

    result = repository.find_duplicate(sender_name_normalized="acme gmbh", invoice_number_normalized="INV100")

    assert result.is_duplicate is False


def test_duplicate_repository_finds_registered_invoice(session_factory) -> None:
    repository = SqlAlchemyDuplicateRepository(session_factory)
    repository.register(
        InvoiceRegistryEntry(
            processing_id="proc_existing",
            sender_name_normalized="acme gmbh",
            invoice_number_normalized="INV100",
            status=InvoiceStatus.GUELTIG,
        )
    )

    result = repository.find_duplicate(sender_name_normalized="acme gmbh", invoice_number_normalized="INV100")

    assert result.is_duplicate is True
    assert result.matched_processing_id == "proc_existing"


def test_duplicate_repository_uses_fallback_signature_when_invoice_number_is_missing(session_factory) -> None:
    repository = SqlAlchemyDuplicateRepository(session_factory)
    repository.register(
        InvoiceRegistryEntry(
            processing_id="proc_existing_fallback",
            sender_name_normalized="acme gmbh",
            invoice_number_normalized=None,
            recipient_name_normalized="sieben dorfer immobilien gmbh",
            iban_normalized="AT611904300234573201",
            amount_decimal=Decimal("120.00"),
            status=InvoiceStatus.GUELTIG,
        )
    )

    result = repository.find_duplicate(
        sender_name_normalized="acme gmbh",
        invoice_number_normalized=None,
        recipient_name_normalized="sieben dorfer immobilien gmbh",
        iban_normalized="AT611904300234573201",
        amount_decimal=Decimal("120.00"),
    )

    assert result.is_duplicate is True
    assert result.matched_processing_id == "proc_existing_fallback"


def test_duplicate_repository_uses_document_date_to_disambiguate_fallback_signature(session_factory) -> None:
    repository = SqlAlchemyDuplicateRepository(session_factory)
    repository.register(
        InvoiceRegistryEntry(
            processing_id="proc_existing_early",
            sender_name_normalized="acme gmbh",
            invoice_number_normalized=None,
            document_date=date(2026, 3, 10),
            recipient_name_normalized="sieben dorfer immobilien gmbh",
            amount_decimal=Decimal("120.00"),
            status=InvoiceStatus.GUELTIG,
        )
    )
    repository.register(
        InvoiceRegistryEntry(
            processing_id="proc_existing_late",
            sender_name_normalized="acme gmbh",
            invoice_number_normalized=None,
            document_date=date(2026, 3, 15),
            recipient_name_normalized="sieben dorfer immobilien gmbh",
            amount_decimal=Decimal("120.00"),
            status=InvoiceStatus.GUELTIG,
        )
    )

    result = repository.find_duplicate(
        sender_name_normalized="acme gmbh",
        invoice_number_normalized=None,
        document_date=date(2026, 3, 15),
        recipient_name_normalized="sieben dorfer immobilien gmbh",
        amount_decimal=Decimal("120.00"),
    )

    assert result.is_duplicate is True
    assert result.matched_processing_id == "proc_existing_late"


def test_duplicate_repository_uses_content_hash_fallback_when_other_fingerprint_fields_are_missing(session_factory) -> None:
    repository = SqlAlchemyDuplicateRepository(session_factory)
    repository.register(
        InvoiceRegistryEntry(
            processing_id="proc_existing_hash",
            sender_name_normalized="acme gmbh",
            invoice_number_normalized=None,
            document_date=date(2026, 3, 11),
            content_hash="sha256:abc123",
            status=InvoiceStatus.GUELTIG,
        )
    )

    result = repository.find_duplicate(
        sender_name_normalized="acme gmbh",
        invoice_number_normalized=None,
        document_date=date(2026, 3, 11),
        content_hash="sha256:abc123",
    )

    assert result.is_duplicate is True
    assert result.matched_processing_id == "proc_existing_hash"


def test_duplicate_repository_keeps_content_hash_fallback_conservative_when_document_date_conflicts(session_factory) -> None:
    repository = SqlAlchemyDuplicateRepository(session_factory)
    repository.register(
        InvoiceRegistryEntry(
            processing_id="proc_existing_hash_conflict",
            sender_name_normalized="acme gmbh",
            invoice_number_normalized=None,
            document_date=date(2026, 3, 11),
            content_hash="sha256:abc123",
            status=InvoiceStatus.GUELTIG,
        )
    )

    result = repository.find_duplicate(
        sender_name_normalized="acme gmbh",
        invoice_number_normalized=None,
        document_date=date(2026, 3, 12),
        content_hash="sha256:abc123",
    )

    assert result.is_duplicate is False
    assert "document date" in (result.match_reason or "").lower()


def test_duplicate_repository_rejects_recipient_only_fallback_without_hash_date_or_iban(session_factory) -> None:
    repository = SqlAlchemyDuplicateRepository(session_factory)
    repository.register(
        InvoiceRegistryEntry(
            processing_id="proc_existing_recipient_only",
            sender_name_normalized="acme gmbh",
            invoice_number_normalized=None,
            recipient_name_normalized="sieben dorfer immobilien gmbh",
            amount_decimal=Decimal("120.00"),
            status=InvoiceStatus.GUELTIG,
        )
    )

    result = repository.find_duplicate(
        sender_name_normalized="acme gmbh",
        invoice_number_normalized=None,
        recipient_name_normalized="sieben dorfer immobilien gmbh",
        amount_decimal=Decimal("120.00"),
    )

    assert result.is_duplicate is False
    assert "too weak" in (result.match_reason or "").lower()


def test_duplicate_repository_rejects_incomplete_content_hash_without_stronger_fingerprint(session_factory) -> None:
    repository = SqlAlchemyDuplicateRepository(session_factory)
    repository.register(
        InvoiceRegistryEntry(
            processing_id="proc_existing_short_hash",
            sender_name_normalized="acme gmbh",
            invoice_number_normalized=None,
            content_hash="sha256:abcdef123456",
            status=InvoiceStatus.GUELTIG,
        )
    )

    result = repository.find_duplicate(
        sender_name_normalized="acme gmbh",
        invoice_number_normalized=None,
        content_hash="abc",
    )

    assert result.is_duplicate is False
    assert "content hash is incomplete" in (result.match_reason or "").lower()


def test_duplicate_repository_does_not_override_on_ambiguous_fallback_signature(session_factory) -> None:
    repository = SqlAlchemyDuplicateRepository(session_factory)
    repository.register(
        InvoiceRegistryEntry(
            processing_id="proc_existing_1",
            sender_name_normalized="acme gmbh",
            invoice_number_normalized=None,
            document_date=date(2026, 3, 20),
            recipient_name_normalized="sieben dorfer immobilien gmbh",
            amount_decimal=Decimal("120.00"),
            status=InvoiceStatus.GUELTIG,
        )
    )
    repository.register(
        InvoiceRegistryEntry(
            processing_id="proc_existing_2",
            sender_name_normalized="acme gmbh",
            invoice_number_normalized=None,
            document_date=date(2026, 3, 20),
            recipient_name_normalized="sieben dorfer immobilien gmbh",
            amount_decimal=Decimal("120.00"),
            status=InvoiceStatus.GUELTIG,
        )
    )

    result = repository.find_duplicate(
        sender_name_normalized="acme gmbh",
        invoice_number_normalized=None,
        document_date=date(2026, 3, 20),
        recipient_name_normalized="sieben dorfer immobilien gmbh",
        amount_decimal=Decimal("120.00"),
    )

    assert result.is_duplicate is False
    assert "Ambiguous fallback duplicate signature" in (result.match_reason or "")
