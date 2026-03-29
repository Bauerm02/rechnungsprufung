from __future__ import annotations

from invoice_automation.domain.models import EnrichedInvoiceData, ExtractedInvoiceData, ExtractionBundle
from invoice_automation.ports.llm import LlmPort


class AiExtractionService:
    def __init__(self, adapter: LlmPort):
        self._adapter = adapter

    def screen(self, bundle: ExtractionBundle) -> ExtractedInvoiceData:
        return self._adapter.screen(bundle)

    def enrich(self, bundle: ExtractionBundle, screening: ExtractedInvoiceData) -> EnrichedInvoiceData:
        return self._adapter.enrich(bundle, screening)

