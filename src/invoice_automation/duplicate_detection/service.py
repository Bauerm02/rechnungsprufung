from __future__ import annotations

from datetime import date
from decimal import Decimal

from invoice_automation.domain.models import DuplicateCheckResult, InvoiceRegistryEntry
from invoice_automation.duplicate_detection.interfaces import DuplicateRepository


class DuplicateDetectionService:
    def __init__(self, repository: DuplicateRepository):
        self._repository = repository

    def find_duplicate(
        self,
        *,
        sender_name_normalized: str,
        invoice_number_normalized: str | None,
        document_date: date | None = None,
        recipient_name_normalized: str | None = None,
        iban_normalized: str | None = None,
        content_hash: str | None = None,
        amount_decimal: Decimal | None = None,
    ) -> DuplicateCheckResult:
        return self._repository.find_duplicate(
            sender_name_normalized=sender_name_normalized,
            invoice_number_normalized=invoice_number_normalized,
            document_date=document_date,
            recipient_name_normalized=recipient_name_normalized,
            iban_normalized=iban_normalized,
            content_hash=content_hash,
            amount_decimal=amount_decimal,
        )

    def register_processed_invoice(self, entry: InvoiceRegistryEntry) -> InvoiceRegistryEntry:
        return self._repository.register(entry)
