from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def build_processing_id() -> str:
    return f"proc_{uuid4().hex}"


class ProcessingId(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str = Field(default_factory=build_processing_id)


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_name: str
    raw_value: str
    confidence: float | None = None
    source: str | None = None


class DecisionNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

