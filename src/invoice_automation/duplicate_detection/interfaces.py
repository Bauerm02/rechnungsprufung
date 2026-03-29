from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Protocol

from invoice_automation.domain.models import DuplicateCheckResult, InvoiceRegistryEntry


class DuplicateRepository(Protocol):
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
        ...

    def register(self, entry: InvoiceRegistryEntry) -> InvoiceRegistryEntry:
        ...
