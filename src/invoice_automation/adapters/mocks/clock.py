from __future__ import annotations

from datetime import datetime, timezone


class MockClock:
    def __init__(self, now_value: datetime | None = None):
        self._now_value = now_value or datetime.now(timezone.utc)

    def now(self) -> datetime:
        return self._now_value

