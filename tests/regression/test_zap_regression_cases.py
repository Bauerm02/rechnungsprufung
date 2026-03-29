from __future__ import annotations

import json
from pathlib import Path

import pytest

from invoice_automation.domain.models import ExtractedInvoiceData
from tests_support import make_bundle, make_decisioning_service, register_existing_invoice


def _load_cases() -> list[dict]:
    fixture_path = Path(__file__).parent.parent / "fixtures" / "zap_regression_cases.json"
    return json.loads(fixture_path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", _load_cases(), ids=lambda case: case["id"])
def test_zap_regression_cases(case: dict, session_factory) -> None:
    extracted = ExtractedInvoiceData(processing_id=case["id"], **case["extracted"])
    if case.get("seed_duplicate"):
        seed_duplicate = case["seed_duplicate"]
        seed_overrides = seed_duplicate if isinstance(seed_duplicate, dict) else {}
        register_existing_invoice(
            session_factory,
            processing_id=seed_overrides.get("processing_id", f"{case['id']}_existing"),
            sender_name=seed_overrides.get("sender_name", extracted.sender_name or ""),
            invoice_number=seed_overrides.get("invoice_number", extracted.invoice_number),
            document_date=seed_overrides.get("document_date", extracted.document_date),
            recipient_name=seed_overrides.get("recipient_name", extracted.recipient_name),
            iban=seed_overrides.get("iban", extracted.iban),
            content_hash=seed_overrides.get("content_hash", extracted.content_hash),
            amount=seed_overrides.get("amount", extracted.amount),
        )

    service = make_decisioning_service(session_factory, vat_responses=case.get("vat_responses"))
    outcome = service.decide(
        bundle=make_bundle(processing_id=extracted.processing_id, text=case["text"]),
        extracted=extracted,
    )
    expected = case["expected"]

    assert outcome.rule_decision.status.value == expected["status"]
    assert outcome.rule_decision.notification_type.value == expected["notification_type"]
    assert outcome.rule_decision.routing_family.value == expected["routing_family"]
    assert (outcome.rule_decision.payment_type.value if outcome.rule_decision.payment_type else None) == expected["payment_type"]
    assert outcome.rule_decision.project_code == expected["project_code"]
    assert outcome.rule_decision.payable_outcome is expected["payable_outcome"]
    assert outcome.rule_decision.manual_review_required is expected["manual_review_required"]
    assert outcome.rule_decision.stamp_allowed is expected["stamp_allowed"]
    assert outcome.xml_plan.disposition.value == expected["xml_disposition"]

    if "notification_present" in expected:
        assert (outcome.notification_draft is not None) is expected["notification_present"]
    if "notification_recipients" in expected:
        assert outcome.notification_draft is not None
        assert outcome.notification_draft.recipients == expected["notification_recipients"]
    if "notification_subject_contains" in expected:
        assert outcome.notification_draft is not None
        assert expected["notification_subject_contains"] in outcome.notification_draft.subject
    if "notification_body_contains" in expected:
        assert outcome.notification_draft is not None
        assert expected["notification_body_contains"] in outcome.notification_draft.body

    if "routing_artifacts" in expected:
        assert [plan.artifact_type.value for plan in outcome.routing_plans] == expected["routing_artifacts"]
    if "routing_filenames" in expected:
        assert {plan.artifact_type.value: plan.target_filename for plan in outcome.routing_plans} == expected["routing_filenames"]

    if "xml_should_generate" in expected:
        assert outcome.xml_plan.should_generate is expected["xml_should_generate"]
    if "xml_filename" in expected:
        assert outcome.xml_plan.draft_filename == expected["xml_filename"]
    if "xml_reference" in expected:
        assert outcome.xml_plan.reference_id == expected["xml_reference"]
    if "xml_contains" in expected:
        assert expected["xml_contains"] in (outcome.xml_plan.draft_payload or "")

    if "stamp_should_render" in expected:
        assert outcome.stamp_plan.should_render is expected["stamp_should_render"]
    if "stamp_filename" in expected:
        assert outcome.stamp_plan.draft_filename == expected["stamp_filename"]
    if "stamp_lines_include" in expected:
        for line in expected["stamp_lines_include"]:
            assert line in outcome.stamp_plan.lines
