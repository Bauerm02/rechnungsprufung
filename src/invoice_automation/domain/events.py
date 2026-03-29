from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field

from invoice_automation.domain.enums import AuditEventType


class DomainEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    processing_id: str
    event_type: AuditEventType
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    summary: str | None = None

