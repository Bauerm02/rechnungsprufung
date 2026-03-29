from __future__ import annotations

from invoice_automation.domain.models import DocumentPage, ExtractionBundle, RawDocument


class MockOcrAdapter:
    def __init__(self, page_texts: list[str] | None = None):
        self._page_texts = page_texts or [""]

    def extract(self, document: RawDocument) -> ExtractionBundle:
        pages = [
            DocumentPage(
                page_number=index + 1,
                raw_text=text,
                normalized_text=text.strip(),
                image_ref=f"mock://page/{index + 1}",
                ocr_used=True,
            )
            for index, text in enumerate(self._page_texts)
        ]
        return ExtractionBundle(
            processing_id=document.processing_id,
            pages=pages,
            full_raw_text="\n".join(page.raw_text for page in pages),
            full_normalized_text="\n".join(page.normalized_text for page in pages),
            warnings=[],
        )

