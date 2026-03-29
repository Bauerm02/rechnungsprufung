from fastapi import FastAPI

from invoice_automation.infrastructure.config import get_settings


settings = get_settings()
app = FastAPI(title="invoice-automation", version="0.1.0")


@app.get("/health")
def healthcheck() -> dict[str, object]:
    return {
        "status": "ok",
        "environment": settings.environment,
        "notifications_enabled": settings.enable_notifications,
        "storage_writes_enabled": settings.enable_storage_writes,
        "file_deletes_enabled": settings.enable_file_deletes,
        "xml_publish_enabled": settings.enable_xml_publish,
    }

