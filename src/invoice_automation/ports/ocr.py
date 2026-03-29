from __future__ import annotations

from typing import Protocol

from invoice_automation.domain.models import ExtractionBundle, RawDocument


class OcrPort(Protocol):
    def extract(self, document: RawDocument) -> ExtractionBundle:
        ...

