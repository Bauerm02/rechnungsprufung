from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="INVOICE_AUTOMATION_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "development"
    database_url: str = "sqlite:///./data/invoice_automation.db"
    enable_notifications: bool = False
    enable_storage_writes: bool = False
    enable_file_deletes: bool = False
    enable_xml_publish: bool = False
    enable_google_sheets_mirror: bool = False
    dropbox_input_path: str = "./incoming"
    default_timezone: str = Field(default="Europe/Vienna")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

