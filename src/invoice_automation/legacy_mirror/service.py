from __future__ import annotations

from invoice_automation.domain.models import LegacyMirrorRecord


class LegacyMirrorService:
    def __init__(self, enabled: bool = False):
        self._enabled = enabled
        self.records: list[LegacyMirrorRecord] = []

    def queue(self, record: LegacyMirrorRecord) -> LegacyMirrorRecord:
        queued = record.model_copy(update={"status": "DISABLED" if not self._enabled else "QUEUED"})
        self.records.append(queued)
        return queued

