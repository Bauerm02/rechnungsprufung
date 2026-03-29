from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from invoice_automation.domain.enums import (
    AmountBand,
    ArtifactType,
    AuditEventType,
    InvoiceStatus,
    NotificationType,
    PaymentType,
    RoutingFamily,
    ValidationSeverity,
    XmlDisposition,
)
from invoice_automation.domain.value_objects import EvidenceItem


class SourceFileMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_system: str = "dropbox"
    source_path: str
    filename: str
    source_identifier: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    modified_at: datetime | None = None


class RawDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    processing_id: str
    metadata: SourceFileMetadata
    local_path: str | None = None
    content_hash: str | None = None
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class DocumentPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_number: int
    raw_text: str = ""
    normalized_text: str = ""
    image_ref: str | None = None
    ocr_used: bool = False


class ExtractionBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    processing_id: str
    pages: list[DocumentPage] = Field(default_factory=list)
    full_raw_text: str = ""
    full_normalized_text: str = ""
    warnings: list[str] = Field(default_factory=list)


class ExtractedInvoiceData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    processing_id: str
    status_candidate: InvoiceStatus | None = None
    reason_candidate: str | None = None
    sender_name: str | None = None
    recipient_name: str | None = None
    invoice_number: str | None = None
    document_date: date | None = None
    amount: Decimal | None = None
    currency: str | None = "EUR"
    supplier_email: str | None = None
    iban: str | None = None
    uid_sender: str | None = None
    uid_recipient: str | None = None
    content_hash: str | None = None
    payment_type_candidate: PaymentType | None = None
    evidence: list[EvidenceItem] = Field(default_factory=list)


class EnrichedInvoiceData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    processing_id: str
    project_code: str | None = None
    sender_name_candidate: str | None = None
    payment_type_candidate: PaymentType | None = None
    stamp_text_candidate: str | None = None
    xml_payload_candidate: str | None = None
    evidence: list[EvidenceItem] = Field(default_factory=list)


class NormalizedInvoiceData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    processing_id: str
    sender_name_normalized: str | None = None
    recipient_name_normalized: str | None = None
    invoice_number_normalized: str | None = None
    document_date: date | None = None
    iban_normalized: str | None = None
    uid_sender_normalized: str | None = None
    uid_recipient_normalized: str | None = None
    content_hash_normalized: str | None = None
    amount_decimal: Decimal | None = None


class DuplicateCheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_duplicate: bool
    match_key: str | None = None
    matched_processing_id: str | None = None
    match_reason: str | None = None


class VatValidationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vat_id: str
    valid: bool | None = None
    checked: bool = False
    reason: str | None = None


class ValidationMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: ValidationSeverity
    code: str
    message: str
    field_name: str | None = None


class ValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    processing_id: str
    normalized: NormalizedInvoiceData
    sender_vat_result: VatValidationResponse | None = None
    recipient_vat_result: VatValidationResponse | None = None
    amount_present: bool = False
    iban_checksum_valid: bool | None = None
    messages: list[ValidationMessage] = Field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(message.severity == ValidationSeverity.ERROR for message in self.messages)

    @property
    def has_warnings(self) -> bool:
        return any(message.severity == ValidationSeverity.WARNING for message in self.messages)


class DocumentRuleFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    processing_id: str
    normalized_text: str = ""
    authority_detected: bool = False
    authority_keyword: str | None = None
    reminder_detected: bool = False
    storno_or_credit_detected: bool = False
    private_detected: bool = False
    organschaft_detected: bool = False
    internal_deposit_detected: bool = False
    internal_expense_detected: bool = False
    internal_expense_employee_name: str | None = None
    internal_expense_creditor_iban_fallback: str | None = None
    redlinghofer_only_recipient: bool = False
    reverse_charge_detected: bool = False
    reverse_charge_conflict_detected: bool = False
    reverse_charge_domestic: bool | None = None
    amount_band: AmountBand = AmountBand.MISSING
    supplier_uid_required: bool = False
    recipient_uid_required: bool = False
    supplier_uid_present: bool = False
    recipient_uid_present: bool = False
    supplier_uid_unreadable: bool = False
    sender_legal_entity_detected: bool = False
    supplier_email_present: bool = False
    government_penalty_private_markus: bool = False
    project_code: str | None = None
    transfer_reference_detected: bool = False
    transfer_reference_value: str | None = None
    payment_type_determined: PaymentType = PaymentType.UEBERWEISUNG
    stamp_text: str | None = None
    amount_negative: bool = False
    payability_blocked: bool = False
    payability_block_reason: str | None = None
    safety_net_applied: bool = False


class RuleDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    processing_id: str
    status: InvoiceStatus
    reason: str
    notification_type: NotificationType
    routing_family: RoutingFamily
    payable_outcome: bool
    manual_review_required: bool
    stamp_allowed: bool
    xml_allowed: bool
    payment_type: PaymentType | None = None
    project_code: str | None = None
    policy_notes: list[str] = Field(default_factory=list)


class StampPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    processing_id: str
    allowed: bool
    advisory: bool = True
    should_render: bool = False
    should_store: bool = False
    draft_filename: str | None = None
    lines: list[str] = Field(default_factory=list)
    reason: str | None = None


class PaymentXmlPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    processing_id: str
    disposition: XmlDisposition
    should_generate: bool
    should_store: bool
    planned_only: bool
    draft_filename: str | None = None
    draft_payload: str | None = None
    reference_id: str | None = None
    reason: str


class DecisionOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    processing_id: str
    validation_result: ValidationResult
    duplicate_result: DuplicateCheckResult
    facts: DocumentRuleFacts
    rule_decision: RuleDecision
    xml_plan: PaymentXmlPlan
    stamp_plan: StampPlan
    notification_draft: NotificationDraft | None = None
    routing_plans: list[RoutingPlan] = Field(default_factory=list)


class RoutingPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    processing_id: str
    routing_family: RoutingFamily
    artifact_type: ArtifactType
    target_directory: str | None = None
    target_filename: str | None = None
    enabled: bool = False
    reason: str | None = None


class NotificationDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    processing_id: str
    notification_type: NotificationType
    recipients: list[str] = Field(default_factory=list)
    subject: str
    body: str
    draft_filename: str | None = None
    enabled: bool = False


class ProcessAuditRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    processing_id: str
    event_type: AuditEventType
    step_name: str
    input_snapshot: dict[str, Any] = Field(default_factory=dict)
    output_snapshot: dict[str, Any] = Field(default_factory=dict)
    decision_summary: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class InvoiceRegistryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    processing_id: str
    sender_name_normalized: str
    invoice_number_normalized: str | None = None
    document_date: date | None = None
    recipient_name_normalized: str | None = None
    iban_normalized: str | None = None
    content_hash: str | None = None
    amount_decimal: Decimal | None = None
    status: InvoiceStatus
    stored_filename: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class LegacyMirrorRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    processing_id: str
    target: str
    record_key: str
    mirrored_at: datetime | None = None
    status: str = "PENDING"
