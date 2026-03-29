from __future__ import annotations

from invoice_automation.domain.models import EnrichedInvoiceData, ExtractedInvoiceData, ExtractionBundle


class MockLlmAdapter:
    def __init__(
        self,
        screening_result: ExtractedInvoiceData | None = None,
        enrichment_result: EnrichedInvoiceData | None = None,
    ):
        self._screening_result = screening_result
        self._enrichment_result = enrichment_result

    def screen(self, bundle: ExtractionBundle) -> ExtractedInvoiceData:
        if self._screening_result is not None:
            return self._screening_result
        return ExtractedInvoiceData(processing_id=bundle.processing_id)

    def enrich(self, bundle: ExtractionBundle, screening: ExtractedInvoiceData) -> EnrichedInvoiceData:
        if self._enrichment_result is not None:
            return self._enrichment_result
        return EnrichedInvoiceData(processing_id=bundle.processing_id)

