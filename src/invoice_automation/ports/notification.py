from __future__ import annotations

from typing import Protocol

from invoice_automation.domain.models import NotificationDraft


class NotificationPort(Protocol):
    def queue(self, draft: NotificationDraft) -> NotificationDraft:
        ...

