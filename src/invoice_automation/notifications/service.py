from __future__ import annotations

from invoice_automation.domain.models import NotificationDraft
from invoice_automation.ports.notification import NotificationPort


class NotificationService:
    def __init__(self, adapter: NotificationPort):
        self._adapter = adapter

    def queue(self, draft: NotificationDraft) -> NotificationDraft:
        return self._adapter.queue(draft)

