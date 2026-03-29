from __future__ import annotations

from invoice_automation.domain.models import NotificationDraft


class MockNotificationAdapter:
    def __init__(self):
        self.queued: list[NotificationDraft] = []

    def queue(self, draft: NotificationDraft) -> NotificationDraft:
        safe_draft = draft.model_copy(update={"enabled": False})
        self.queued.append(safe_draft)
        return safe_draft

