from __future__ import annotations

from invoice_automation.application.decision_tables import ROUTING_TARGET_KEYS
from invoice_automation.domain.enums import ArtifactType, RoutingFamily
from invoice_automation.domain.models import NotificationDraft, PaymentXmlPlan, RoutingPlan, StampPlan


ARTIFACT_TARGET_KEYS: dict[ArtifactType, str] = {
    ArtifactType.SOURCE_DOCUMENT: "source_documents",
    ArtifactType.STAMPED_PDF: "stamped_pdfs",
    ArtifactType.PAYMENT_XML: "payment_xml_drafts",
    ArtifactType.NOTIFICATION_DRAFT: "notification_drafts",
}


def _default_filename(processing_id: str, artifact_type: ArtifactType) -> str:
    suffix_map = {
        ArtifactType.SOURCE_DOCUMENT: "source_document.pdf",
        ArtifactType.STAMPED_PDF: "stamped_draft.pdf",
        ArtifactType.PAYMENT_XML: "payment_draft.xml",
        ArtifactType.NOTIFICATION_DRAFT: "notification_draft.txt",
    }
    return f"{processing_id}_{suffix_map[artifact_type]}"


def build_routing_plan(
    *,
    processing_id: str,
    routing_family: RoutingFamily,
    artifact_type: ArtifactType,
    target_filename: str | None = None,
    enabled: bool = False,
) -> RoutingPlan:
    family_directory = ROUTING_TARGET_KEYS.get(routing_family)
    artifact_directory = ARTIFACT_TARGET_KEYS.get(artifact_type)
    target_directory = (
        f"{family_directory}/{artifact_directory}"
        if family_directory is not None and artifact_directory is not None
        else family_directory
    )
    return RoutingPlan(
        processing_id=processing_id,
        routing_family=routing_family,
        artifact_type=artifact_type,
        target_directory=target_directory,
        target_filename=target_filename or _default_filename(processing_id, artifact_type),
        enabled=enabled,
        reason="Deterministic routing plan only; side effects remain disabled.",
    )


def build_routing_plans(
    *,
    processing_id: str,
    routing_family: RoutingFamily,
    xml_plan: PaymentXmlPlan,
    stamp_plan: StampPlan,
    notification_draft: NotificationDraft | None,
    enabled: bool = False,
) -> list[RoutingPlan]:
    plans = [
        build_routing_plan(
            processing_id=processing_id,
            routing_family=routing_family,
            artifact_type=ArtifactType.SOURCE_DOCUMENT,
            enabled=enabled,
        )
    ]

    if stamp_plan.should_render:
        plans.append(
            build_routing_plan(
                processing_id=processing_id,
                routing_family=routing_family,
                artifact_type=ArtifactType.STAMPED_PDF,
                target_filename=stamp_plan.draft_filename,
                enabled=enabled,
            )
        )

    if xml_plan.should_generate:
        plans.append(
            build_routing_plan(
                processing_id=processing_id,
                routing_family=routing_family,
                artifact_type=ArtifactType.PAYMENT_XML,
                target_filename=xml_plan.draft_filename,
                enabled=enabled,
            )
        )

    if notification_draft is not None:
        plans.append(
            build_routing_plan(
                processing_id=processing_id,
                routing_family=routing_family,
                artifact_type=ArtifactType.NOTIFICATION_DRAFT,
                target_filename=notification_draft.draft_filename,
                enabled=enabled,
            )
        )

    return plans
