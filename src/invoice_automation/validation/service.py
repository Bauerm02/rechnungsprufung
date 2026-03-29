from __future__ import annotations

from invoice_automation.domain.enums import ValidationSeverity
from invoice_automation.domain.models import (
    ExtractedInvoiceData,
    ExtractionBundle,
    NormalizedInvoiceData,
    ValidationMessage,
    ValidationResult,
)
from invoice_automation.ports.vat_service import VatServicePort
from invoice_automation.validation.normalizers import (
    is_valid_iban_checksum,
    normalize_content_hash,
    normalize_iban,
    normalize_invoice_number,
    normalize_sender_name,
    normalize_vat_id,
)


class ValidationService:
    def __init__(self, vat_service: VatServicePort):
        self._vat_service = vat_service

    def normalize(self, extracted: ExtractedInvoiceData) -> NormalizedInvoiceData:
        return NormalizedInvoiceData(
            processing_id=extracted.processing_id,
            sender_name_normalized=normalize_sender_name(extracted.sender_name),
            recipient_name_normalized=normalize_sender_name(extracted.recipient_name),
            invoice_number_normalized=normalize_invoice_number(extracted.invoice_number),
            document_date=extracted.document_date,
            iban_normalized=normalize_iban(extracted.iban),
            uid_sender_normalized=normalize_vat_id(extracted.uid_sender),
            uid_recipient_normalized=normalize_vat_id(extracted.uid_recipient),
            content_hash_normalized=normalize_content_hash(extracted.content_hash),
            amount_decimal=extracted.amount,
        )

    def validate(self, extracted: ExtractedInvoiceData, bundle: ExtractionBundle | None = None) -> ValidationResult:
        normalized = self.normalize(extracted)
        messages: list[ValidationMessage] = []
        if bundle is not None:
            raw_document_text = bundle.full_raw_text or bundle.full_normalized_text or ""
            cleaned_document_text = (
                raw_document_text.replace("<<BEGIN_DOKUMENT>>", "")
                .replace("<<END_DOKUMENT>>", "")
                .replace("<<begin_dokument>>", "")
                .replace("<<end_dokument>>", "")
                .strip()
            )

            if not cleaned_document_text or ("{{" in raw_document_text and "}}" in raw_document_text):
                messages.append(
                    ValidationMessage(
                        severity=ValidationSeverity.ERROR,
                        code="document_text_missing",
                        message="No usable document text was available for deterministic validation.",
                        field_name="document_text",
                    )
                )

        if not normalized.invoice_number_normalized:
            messages.append(
                ValidationMessage(
                    severity=ValidationSeverity.WARNING,
                    code="invoice_number_missing",
                    message="Invoice number candidate is missing.",
                    field_name="invoice_number",
                )
            )

        if normalized.amount_decimal is None:
            messages.append(
                ValidationMessage(
                    severity=ValidationSeverity.ERROR,
                    code="amount_missing",
                    message="No gross amount could be determined.",
                    field_name="amount",
                )
            )

        iban_valid = is_valid_iban_checksum(normalized.iban_normalized)
        if iban_valid is False:
            messages.append(
                ValidationMessage(
                    severity=ValidationSeverity.WARNING,
                    code="iban_checksum_invalid",
                    message="IBAN candidate failed checksum validation.",
                    field_name="iban",
                )
            )

        sender_vat_result = None
        if normalized.uid_sender_normalized and not normalized.uid_sender_normalized.startswith("EU"):
            sender_vat_result = self._vat_service.validate(normalized.uid_sender_normalized)
            if sender_vat_result.checked and sender_vat_result.valid is False:
                messages.append(
                    ValidationMessage(
                        severity=ValidationSeverity.ERROR,
                        code="sender_vat_invalid",
                        message="Supplier VAT ID could not be validated.",
                        field_name="uid_sender",
                    )
                )

        recipient_vat_result = None
        if normalized.uid_recipient_normalized and not normalized.uid_recipient_normalized.startswith("EU"):
            recipient_vat_result = self._vat_service.validate(normalized.uid_recipient_normalized)
            if recipient_vat_result.checked and recipient_vat_result.valid is False:
                messages.append(
                    ValidationMessage(
                        severity=ValidationSeverity.ERROR,
                        code="recipient_vat_invalid",
                        message="Recipient VAT ID could not be validated.",
                        field_name="uid_recipient",
                    )
                )

        return ValidationResult(
            processing_id=extracted.processing_id,
            normalized=normalized,
            sender_vat_result=sender_vat_result,
            recipient_vat_result=recipient_vat_result,
            amount_present=normalized.amount_decimal is not None,
            iban_checksum_valid=iban_valid,
            messages=messages,
        )
