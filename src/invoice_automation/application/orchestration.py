from __future__ import annotations

from dataclasses import dataclass

from invoice_automation.ai_extraction.service import AiExtractionService
from invoice_automation.audit_log.service import AuditLogService
from invoice_automation.application.decisioning import DecisioningService
from invoice_automation.document_extraction.service import DocumentExtractionService
from invoice_automation.duplicate_detection.service import DuplicateDetectionService
from invoice_automation.ingestion.service import IngestionService
from invoice_automation.notifications.service import NotificationService
from invoice_automation.rule_engine.service import RuleEngine
from invoice_automation.validation.service import ValidationService


@dataclass
class ProcessingPipeline:
    ingestion_service: IngestionService
    extraction_service: DocumentExtractionService
    ai_service: AiExtractionService
    duplicate_service: DuplicateDetectionService
    validation_service: ValidationService
    rule_engine: RuleEngine
    decisioning_service: DecisioningService
    audit_log_service: AuditLogService
    notification_service: NotificationService
