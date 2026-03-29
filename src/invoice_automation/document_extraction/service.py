from __future__ import annotations

from invoice_automation.domain.models import ExtractionBundle, RawDocument
from invoice_automation.ports.ocr import OcrPort


class DocumentExtractionService:
    def __init__(self, ocr_adapter: OcrPort):
        self._ocr_adapter = ocr_adapter

    def extract(self, document: RawDocument) -> ExtractionBundle:
        return self._ocr_adapter.extract(document)

