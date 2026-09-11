from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. No AI/LLM settings exist here on purpose:

    MVP1 runs as a deterministic rule engine with zero LLM dependency at
    runtime. KI is only used offline during development for first-draft
    data extraction, never invoked from this service.
    """

    model_config = SettingsConfigDict(
        env_prefix="MIETINKASSO_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "development"
    database_url: str = "sqlite:///./data/mietinkasso.db"
    default_timezone: str = Field(default="Europe/Vienna")

    # Hard safety defaults for the pilot phase. Never flip these true
    # outside an explicit, reviewed production cutover.
    send_enabled: bool = False
    bank_stand_max_age_days: int = 2
    mahn_stufe1_tage_nach_faelligkeit: int = 7
    mahn_stufe2_mindesttage_nach_stufe1: int = 14

    ausgeschlossene_objekte: tuple[str, ...] = ("107",)
    pilot_objekte: tuple[str, ...] = ("601", "616", "617", "619")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
