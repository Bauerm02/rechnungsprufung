from invoice_automation.domain.enums import ArtifactType, NotificationType, RoutingFamily, XmlDisposition
from invoice_automation.domain.models import NotificationDraft, PaymentXmlPlan, StampPlan
from invoice_automation.storage_routing.policies import build_routing_plans


def test_routing_plans_include_only_planned_artifacts() -> None:
    plans = build_routing_plans(
        processing_id="proc_route",
        routing_family=RoutingFamily.COMPANY_PAYABLE,
        xml_plan=PaymentXmlPlan(
            processing_id="proc_route",
            disposition=XmlDisposition.PLAN_ONLY,
            should_generate=True,
            should_store=False,
            planned_only=True,
            draft_filename="proc_route_payment_draft.xml",
            draft_payload="<SepaPaymentDraft />",
            reference_id="gb-1200",
            reason="Eligible transfer invoice.",
        ),
        stamp_plan=StampPlan(
            processing_id="proc_route",
            allowed=True,
            should_render=True,
            should_store=False,
            draft_filename="proc_route_stamped_draft.pdf",
            lines=["MANUELLE PRUEFUNG ERFORDERLICH"],
            reason="Deterministic stamp policy applied.",
        ),
        notification_draft=NotificationDraft(
            processing_id="proc_route",
            notification_type=NotificationType.ACCOUNTING_INFO,
            recipients=["buchhaltung@local.invalid"],
            subject="[ACCOUNTING_INFO] proc_route",
            body="Body",
            draft_filename="proc_route_notification_draft.txt",
            enabled=False,
        ),
        enabled=False,
    )

    assert [plan.artifact_type for plan in plans] == [
        ArtifactType.SOURCE_DOCUMENT,
        ArtifactType.STAMPED_PDF,
        ArtifactType.PAYMENT_XML,
        ArtifactType.NOTIFICATION_DRAFT,
    ]
    assert plans[0].target_directory == "company_payable/source_documents"
    assert plans[1].target_filename == "proc_route_stamped_draft.pdf"
    assert plans[2].target_filename == "proc_route_payment_draft.xml"
    assert plans[3].target_filename == "proc_route_notification_draft.txt"


def test_routing_plans_skip_absent_artifacts() -> None:
    plans = build_routing_plans(
        processing_id="proc_route_min",
        routing_family=RoutingFamily.INVALID_HOLD,
        xml_plan=PaymentXmlPlan(
            processing_id="proc_route_min",
            disposition=XmlDisposition.NOT_ALLOWED,
            should_generate=False,
            should_store=False,
            planned_only=False,
            reason="XML not allowed.",
        ),
        stamp_plan=StampPlan(
            processing_id="proc_route_min",
            allowed=False,
            should_render=False,
            should_store=False,
            lines=[],
            reason="Stamping disabled.",
        ),
        notification_draft=None,
        enabled=False,
    )

    assert [plan.artifact_type for plan in plans] == [ArtifactType.SOURCE_DOCUMENT]
    assert plans[0].target_filename == "proc_route_min_source_document.pdf"
