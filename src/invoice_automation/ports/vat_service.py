from __future__ import annotations

from typing import Protocol

from invoice_automation.domain.models import VatValidationResponse


class VatServicePort(Protocol):
    def validate(self, vat_id: str) -> VatValidationResponse:
        ...

