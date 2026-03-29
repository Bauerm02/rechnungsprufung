from __future__ import annotations

from invoice_automation.domain.models import VatValidationResponse


class MockVatService:
    def __init__(self, responses: dict[str, VatValidationResponse] | None = None):
        self._responses = responses or {}

    def validate(self, vat_id: str) -> VatValidationResponse:
        if vat_id in self._responses:
            return self._responses[vat_id]
        return VatValidationResponse(vat_id=vat_id, valid=None, checked=False, reason="Mock VAT service did not validate.")

