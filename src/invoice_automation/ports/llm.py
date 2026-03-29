from __future__ import annotations

from typing import Protocol

from invoice_automation.domain.models import EnrichedInvoiceData, ExtractedInvoiceData, ExtractionBundle


class LlmPort(Protocol):
    def screen(self, bundle: ExtractionBundle) -> ExtractedInvoiceData:
        ...

    def enrich(self, bundle: ExtractionBundle, screening: ExtractedInvoiceData) -> EnrichedInvoiceData:
        ...

