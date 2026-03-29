from __future__ import annotations

from invoice_automation.domain.models import RawDocument, SourceFileMetadata
from invoice_automation.domain.value_objects import build_processing_id


class IngestionService:
    def create_document(self, metadata: SourceFileMetadata, *, local_path: str | None = None, content_hash: str | None = None) -> RawDocument:
        return RawDocument(
            processing_id=build_processing_id(),
            metadata=metadata,
            local_path=local_path,
            content_hash=content_hash,
        )

