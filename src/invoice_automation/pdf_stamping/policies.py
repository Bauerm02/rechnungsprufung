from __future__ import annotations

from invoice_automation.domain.models import DocumentRuleFacts, RuleDecision, StampPlan, ValidationResult


def build_stamp_plan(
    *,
    decision: RuleDecision,
    validation_result: ValidationResult,
    facts: DocumentRuleFacts,
) -> StampPlan:
    if not decision.stamp_allowed:
        return StampPlan(
            processing_id=decision.processing_id,
            allowed=False,
            advisory=True,
            should_render=False,
            should_store=False,
            draft_filename=None,
            lines=[],
            reason=f"Stamping disabled for terminal status {decision.status.value}.",
        )

    lines: list[str] = []
    if facts.stamp_text:
        lines.append(facts.stamp_text)

    if facts.supplier_uid_required and not facts.supplier_uid_present:
        lines.append("UID nicht lesbar - bitte pruefen")
    elif validation_result.sender_vat_result and validation_result.sender_vat_result.checked and validation_result.sender_vat_result.valid is False:
        lines.append("UID fehlerhaft - bitte pruefen")

    if validation_result.iban_checksum_valid is False:
        lines.append("IBAN fehlerhaft - bitte pruefen")

    if decision.manual_review_required:
        lines.append("MANUELLE PRUEFUNG ERFORDERLICH")

    deduped_lines = list(dict.fromkeys(line for line in lines if line))
    should_render = bool(deduped_lines)
    return StampPlan(
        processing_id=decision.processing_id,
        allowed=True,
        advisory=True,
        should_render=should_render,
        should_store=False,
        draft_filename=f"{decision.processing_id}_stamped_draft.pdf" if should_render else None,
        lines=deduped_lines,
        reason="Deterministic stamp policy applied." if should_render else "Stamping allowed but no overlay lines were planned.",
    )
