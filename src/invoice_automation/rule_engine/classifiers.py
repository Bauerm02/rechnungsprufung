from __future__ import annotations

import re
from decimal import Decimal

from invoice_automation.domain.enums import AmountBand, InvoiceStatus, PaymentType
from invoice_automation.domain.models import DocumentRuleFacts, ExtractedInvoiceData, ExtractionBundle, ValidationResult
from invoice_automation.rule_engine.catalogs import (
    AUTHORITY_DOCUMENT_PATTERNS,
    AUTHORITY_SENDER_PATTERNS,
    AUTHORITY_SENDER_WHITELIST,
    AUTO_PAID_TEXT_KEYWORDS,
    AUTO_PAID_VENDORS,
    AUSTRIA_HINTS,
    INTERNAL_DEPOSIT_KEYWORDS,
    INTERNAL_EXPENSE_DETAIL_KEYWORDS,
    INTERNAL_EXPENSE_EMPLOYEE_NAMES,
    INTERNAL_EXPENSE_TRIGGER_KEYWORDS,
    LEGAL_ENTITY_KEYWORDS,
    ORGANSCHAFT_KEYWORDS,
    OWN_COMPANY_KEYWORDS,
    PRIVATE_DOCUMENT_KEYWORDS,
    PRIVATE_PERSON_KEYWORDS,
    PROJECT_CODE_ALIASES,
    REMINDER_KEYWORDS,
    REVERSE_CHARGE_KEYWORDS,
    STAMP_ALLOWED_PROJECTS,
    STAMP_MRG_KEYWORDS,
    STAMP_TEILANWENDUNG_KEYWORDS,
    STAMP_TENANT_DAMAGE_KEYWORDS,
    STORNO_KEYWORDS,
    SUPPLIER_LEGAL_ENTITY_FORMS,
    SUPPLIER_LEGAL_ENTITY_PATTERNS,
    TRANSFER_REFERENCE_PATTERNS,
    TRANSFER_EXCEPTION_VENDORS,
)
from invoice_automation.validation.normalizers import normalize_sender_name


_VAT_PERCENTAGE_RE = re.compile(r"\b\d{1,2}\s*%\s*(ust|mwst)\b")
_TRANSFER_REFERENCE_VALUE_RE = re.compile(r"[a-z0-9].*\d")
_TRANSFER_REFERENCE_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[\/-][a-z0-9]+)+")
_MILD_OCR_DIGIT_REPAIRS = {"0": "o", "1": "i", "5": "s", "6": "b", "8": "b"}
_TRANSFER_REFERENCE_BLOCKLIST = ("iban", "bic", "betrag", "summe", "rechnung", "invoice", "konto")
_UNLABELED_TRANSFER_REFERENCE_HINTS = (
    "bei der ueberweisung",
    "fuer die ueberweisung",
    "bei zahlung",
    "bei der zahlung",
    "please use",
    "bitte verwenden",
    "folgende referenz",
)
_PROJECT_ALIAS_GENERIC_TOKENS = {
    "gmbh",
    "gesellschaft",
    "mbh",
    "ag",
    "kg",
    "og",
    "projekt",
    "projektentwicklung",
    "projektentwicklungs",
    "projekterrichtungs",
    "projektgesellschaft",
    "projekterrichtungsgesellschaft",
}


def _contains_any(text: str, keywords: tuple[str, ...] | list[str] | set[str]) -> str | None:
    for keyword in keywords:
        if keyword in text:
            return keyword
    return None


def _matches_any_pattern(text: str, patterns: tuple[re.Pattern[str], ...]) -> str | None:
    for pattern in patterns:
        if pattern.search(text):
            return pattern.pattern
    return None


def _determine_amount_band(amount: Decimal | None) -> AmountBand:
    if amount is None:
        return AmountBand.MISSING
    absolute_amount = abs(amount)
    if absolute_amount <= Decimal("400"):
        return AmountBand.LE_400
    if absolute_amount <= Decimal("10000"):
        return AmountBand.LE_10000
    return AmountBand.GT_10000


def _recipient_source(extracted: ExtractedInvoiceData, text: str) -> str:
    recipient = normalize_sender_name(extracted.recipient_name) or ""
    return recipient or text


def _uid_candidate_is_unreadable(value: str | None) -> bool:
    normalized = normalize_sender_name(value) or ""
    return normalized in {"nicht lesbar", "nichtlesbar", "unleserlich"}


def _repair_mild_ocr_digits(value: str) -> str:
    characters = list(value)
    for index, char in enumerate(characters):
        replacement = _MILD_OCR_DIGIT_REPAIRS.get(char)
        if replacement is None:
            continue
        previous_char = characters[index - 1] if index > 0 else " "
        next_char = characters[index + 1] if index + 1 < len(characters) else " "
        if previous_char.isalpha() and next_char.isalpha():
            characters[index] = replacement
        elif next_char.isalpha() and not previous_char.isdigit():
            characters[index] = replacement
    return "".join(characters)


def _condense_match_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value)


def _reference_parts(value: str) -> list[str]:
    return [part for part in re.split(r"[\s/-]+", value) if part]


def _normalize_transfer_reference_candidate(candidate: str) -> str | None:
    normalized = normalize_sender_name(candidate) or ""
    normalized = normalized.strip(" .,:;-")
    if not normalized:
        return None
    if any(blocked in normalized for blocked in _TRANSFER_REFERENCE_BLOCKLIST):
        return None

    structured_reference = _TRANSFER_REFERENCE_TOKEN_RE.search(normalized)
    if structured_reference:
        structured_candidate = structured_reference.group(0)
        if any(char.isdigit() for char in structured_candidate):
            return structured_candidate

    parts = _reference_parts(normalized)
    if len(parts) < 2 or len(parts) > 6:
        return None
    if not any(char.isdigit() for char in normalized):
        return None
    if not any(char.isalpha() for char in normalized):
        return None
    return "-".join(parts)


def _extract_transfer_reference(raw_text: str, normalized_text: str) -> str | None:
    for pattern in TRANSFER_REFERENCE_PATTERNS:
        match = pattern.search(normalized_text)
        if not match:
            continue
        candidate = _normalize_transfer_reference_candidate(match.group(1))
        if candidate:
            return candidate

    normalized_lines = [normalize_sender_name(line) or "" for line in raw_text.splitlines()]
    non_empty_lines = [line for line in normalized_lines if line]
    for index, line in enumerate(non_empty_lines[:-1]):
        if not any(hint in line for hint in _UNLABELED_TRANSFER_REFERENCE_HINTS):
            continue
        candidate = _normalize_transfer_reference_candidate(non_empty_lines[index + 1])
        if candidate:
            return candidate
    return None


def _token_signature(value: str) -> tuple[str, ...]:
    return tuple(sorted(token for token in re.split(r"[\s/-]+", value) if token))


def _alias_tokens_present_in_condensed_text(alias: str, candidate: str) -> bool:
    alias_tokens = [token for token in re.split(r"[\s/-]+", alias) if token]
    significant_tokens = [token for token in alias_tokens if len(token) >= 4 and token not in _PROJECT_ALIAS_GENERIC_TOKENS]
    if len(significant_tokens) < 2:
        return False

    condensed_candidate = _condense_match_text(candidate)
    return all(token in condensed_candidate for token in significant_tokens)


def _project_alias_matches(alias: str, recipient_source: str) -> bool:
    if alias in recipient_source:
        return True

    repaired_recipient_source = _repair_mild_ocr_digits(recipient_source)
    if alias in repaired_recipient_source:
        return True

    if _condense_match_text(alias) == _condense_match_text(repaired_recipient_source):
        return True

    if _token_signature(alias) == _token_signature(repaired_recipient_source):
        return True

    return _alias_tokens_present_in_condensed_text(alias, repaired_recipient_source)


def _detect_authority(sender: str, text: str) -> tuple[bool, str | None]:
    for entry in AUTHORITY_SENDER_WHITELIST:
        if entry and (sender == entry or sender.startswith(entry) or entry in sender):
            return True, entry

    sender_pattern = _matches_any_pattern(sender, AUTHORITY_SENDER_PATTERNS)
    if sender_pattern:
        return True, sender_pattern

    document_pattern = _matches_any_pattern(text, AUTHORITY_DOCUMENT_PATTERNS)
    if document_pattern:
        return True, document_pattern

    return False, None


def _detect_supplier_legal_entity(sender: str) -> bool:
    if not sender:
        return False
    if _contains_any(sender, SUPPLIER_LEGAL_ENTITY_FORMS):
        return True
    return _matches_any_pattern(sender, SUPPLIER_LEGAL_ENTITY_PATTERNS) is not None


def _detect_internal_expense(text: str, sender: str) -> tuple[bool, str | None]:
    employee_name = next((name for name in INTERNAL_EXPENSE_EMPLOYEE_NAMES if name in sender or name in text), None)
    trigger_present = _contains_any(text, INTERNAL_EXPENSE_TRIGGER_KEYWORDS) is not None
    detail_present = _contains_any(text, INTERNAL_EXPENSE_DETAIL_KEYWORDS) is not None
    explicit_employee_reimbursement = employee_name is not None and detail_present
    return detail_present and (trigger_present or explicit_employee_reimbursement), employee_name


def _determine_project_code(text: str, extracted: ExtractedInvoiceData) -> tuple[str | None, bool]:
    recipient_source = _recipient_source(extracted, text)
    government_penalty_private_markus = (
        "markus bauer" in text
        and ("zwangsstrafverfuegung" in text or "hat gegen die verpflichtung" in text)
    )
    if government_penalty_private_markus:
        return "MARKUS_PRIVAT", True

    for alias, project_code in PROJECT_CODE_ALIASES:
        if _project_alias_matches(alias, recipient_source):
            return project_code, False

    if any(person in recipient_source for person in PRIVATE_PERSON_KEYWORDS) and not any(
        keyword in recipient_source for keyword in LEGAL_ENTITY_KEYWORDS
    ):
        return "MARKUS_PRIVAT", False

    return None, False


def _infer_reverse_charge_domestic(text: str, validation_result: ValidationResult) -> tuple[bool | None, bool]:
    sender_iban = validation_result.normalized.iban_normalized or ""
    sender_vat = validation_result.normalized.uid_sender_normalized or ""

    iban_prefix = sender_iban[:2] if len(sender_iban) >= 2 else ""
    vat_prefix = sender_vat[:2] if len(sender_vat) >= 2 else ""

    explicit_domestic = [
        signal
        for signal, active in (
            ("vat", sender_vat.startswith("ATU")),
            ("iban", sender_iban.startswith("AT")),
        )
        if active
    ]
    explicit_foreign = [
        signal
        for signal, active in (
            ("vat", bool(sender_vat) and vat_prefix != "AT"),
            ("iban", bool(sender_iban) and iban_prefix != "AT"),
        )
        if active
    ]

    if explicit_domestic and explicit_foreign:
        return None, True
    if explicit_foreign:
        return False, False
    if explicit_domestic:
        return True, False
    if _contains_any(text, AUSTRIA_HINTS) is not None:
        return True, False
    return None, False


def _determine_payment_type(
    text: str,
    extracted: ExtractedInvoiceData,
    *,
    reverse_charge_detected: bool,
    transfer_reference_detected: bool,
) -> PaymentType:
    sender = normalize_sender_name(extracted.sender_name) or ""

    if reverse_charge_detected:
        return PaymentType.UEBERWEISUNG

    if transfer_reference_detected:
        return PaymentType.UEBERWEISUNG

    if any(exception in sender or exception in text for exception in TRANSFER_EXCEPTION_VENDORS):
        return PaymentType.UEBERWEISUNG

    if any(vendor in sender or vendor in text for vendor in AUTO_PAID_VENDORS):
        return PaymentType.AUTOMATISCH_BEZAHLT

    if any(keyword in text for keyword in AUTO_PAID_TEXT_KEYWORDS):
        return PaymentType.AUTOMATISCH_BEZAHLT

    return PaymentType.UEBERWEISUNG


def _determine_stamp_text(text: str, project_code: str | None) -> str | None:
    if not project_code or project_code.upper() not in STAMP_ALLOWED_PROJECTS:
        return None

    if _contains_any(text, STAMP_TENANT_DAMAGE_KEYWORDS):
        return "HAUSVERWALTUNG: Mieter-Beschaedigung"
    if _contains_any(text, STAMP_TEILANWENDUNG_KEYWORDS):
        return "HAUSVERWALTUNG: Betriebskosten (Teilanwendung)"
    if _contains_any(text, STAMP_MRG_KEYWORDS):
        return "HAUSVERWALTUNG: Betriebskosten (MRG Vollanwendung)"
    return None


def derive_rule_facts(bundle: ExtractionBundle, extracted: ExtractedInvoiceData, validation_result: ValidationResult) -> DocumentRuleFacts:
    raw_text = bundle.full_raw_text or bundle.full_normalized_text or ""
    text = normalize_sender_name(bundle.full_normalized_text or bundle.full_raw_text or "") or ""
    sender = normalize_sender_name(extracted.sender_name) or ""
    reason_candidate = normalize_sender_name(extracted.reason_candidate) or ""
    recipient_source = _recipient_source(extracted, text)
    amount_band = _determine_amount_band(extracted.amount)
    authority_detected, authority_keyword = _detect_authority(sender, text)
    reminder_detected = _contains_any(text, REMINDER_KEYWORDS) is not None
    storno_detected = _contains_any(text, STORNO_KEYWORDS) is not None or (extracted.amount is not None and extracted.amount < 0)
    reverse_charge_detected = _contains_any(text, REVERSE_CHARGE_KEYWORDS) is not None and not _VAT_PERCENTAGE_RE.search(text)
    transfer_reference_value = _extract_transfer_reference(raw_text, text)
    reverse_charge_domestic, reverse_charge_conflict = (
        _infer_reverse_charge_domestic(text, validation_result) if reverse_charge_detected else (None, False)
    )

    project_code, markus_private_penalty = _determine_project_code(text, extracted)
    internal_expense, internal_expense_employee_name = _detect_internal_expense(text, sender)
    if internal_expense:
        project_code = "MBB"

    payment_type = (
        PaymentType.UEBERWEISUNG
        if internal_expense
        else _determine_payment_type(
            text,
            extracted,
            reverse_charge_detected=reverse_charge_detected,
            transfer_reference_detected=transfer_reference_value is not None,
        )
    )

    supplier_uid_present = validation_result.normalized.uid_sender_normalized is not None
    recipient_uid_present = validation_result.normalized.uid_recipient_normalized is not None
    supplier_uid_unreadable = _uid_candidate_is_unreadable(extracted.uid_sender)
    sender_legal_entity_detected = _detect_supplier_legal_entity(sender)

    supplier_uid_required = amount_band in {AmountBand.LE_10000, AmountBand.GT_10000}
    recipient_uid_required = amount_band == AmountBand.GT_10000

    private_detected = (
        not authority_detected
        and not internal_expense
        and not markus_private_penalty
        and (
            extracted.status_candidate == InvoiceStatus.PRIVAT_RECHNUNG
            or reason_candidate == "privatausgabe"
            or _contains_any(text, PRIVATE_DOCUMENT_KEYWORDS) is not None
            or (
                any(person in recipient_source for person in PRIVATE_PERSON_KEYWORDS)
                and not any(keyword in recipient_source for keyword in LEGAL_ENTITY_KEYWORDS)
                and project_code == "MARKUS_PRIVAT"
            )
        )
    )

    redlinghofer_only = "redlinghofer" in recipient_source and "hotel villa flora" not in recipient_source
    internal_deposit = _contains_any(text, INTERNAL_DEPOSIT_KEYWORDS) is not None and any(company in sender for company in OWN_COMPANY_KEYWORDS)
    reminder_detected = reminder_detected or extracted.status_candidate == InvoiceStatus.MAHNUNG
    if reason_candidate in {"behoerde", "behorde"}:
        authority_detected = True
        authority_keyword = authority_keyword or "behorde"

    return DocumentRuleFacts(
        processing_id=extracted.processing_id,
        normalized_text=text,
        authority_detected=authority_detected,
        authority_keyword=authority_keyword,
        reminder_detected=reminder_detected,
        storno_or_credit_detected=storno_detected,
        private_detected=private_detected,
        organschaft_detected=_contains_any(text, ORGANSCHAFT_KEYWORDS) is not None,
        internal_deposit_detected=internal_deposit,
        internal_expense_detected=internal_expense,
        internal_expense_employee_name=internal_expense_employee_name,
        internal_expense_creditor_iban_fallback="HINTERLEGT" if internal_expense and not validation_result.normalized.iban_normalized else None,
        redlinghofer_only_recipient=redlinghofer_only,
        reverse_charge_detected=reverse_charge_detected,
        reverse_charge_conflict_detected=reverse_charge_conflict,
        reverse_charge_domestic=reverse_charge_domestic,
        amount_band=amount_band,
        supplier_uid_required=supplier_uid_required,
        recipient_uid_required=recipient_uid_required,
        supplier_uid_present=supplier_uid_present,
        recipient_uid_present=recipient_uid_present,
        supplier_uid_unreadable=supplier_uid_unreadable,
        sender_legal_entity_detected=sender_legal_entity_detected,
        supplier_email_present=bool(extracted.supplier_email and extracted.supplier_email.upper() != "FEHLT"),
        government_penalty_private_markus=markus_private_penalty,
        project_code=project_code,
        transfer_reference_detected=transfer_reference_value is not None,
        transfer_reference_value=transfer_reference_value,
        payment_type_determined=payment_type,
        stamp_text=_determine_stamp_text(text, project_code),
        amount_negative=bool(extracted.amount is not None and extracted.amount < 0),
        payability_blocked=storno_detected,
        payability_block_reason="Credit note, cancellation, or negative amount." if storno_detected else None,
        safety_net_applied=not supplier_uid_present and sender_legal_entity_detected,
    )
