from invoice_automation.domain.enums import InvoiceStatus, NotificationType, RoutingFamily


def test_invoice_status_contains_duplicate_terminal_state() -> None:
    assert InvoiceStatus.DUPLIKAT.value == "DUPLIKAT"


def test_terminal_review_states_are_present() -> None:
    assert InvoiceStatus.MAHNUNG in InvoiceStatus
    assert InvoiceStatus.PRIVAT_RECHNUNG in InvoiceStatus
    assert InvoiceStatus.UNGUELTIG in InvoiceStatus


def test_notification_and_routing_enums_expose_expected_values() -> None:
    assert NotificationType.DUPLICATE_ALERT.value == "DUPLICATE_ALERT"
    assert RoutingFamily.INVALID_HOLD.value == "INVALID_HOLD"
    assert RoutingFamily.PRIVATE_PAYABLE.value == "PRIVATE_PAYABLE"
    assert RoutingFamily.PRIVATE_REVIEW_HOLD.value == "PRIVATE_REVIEW_HOLD"
    assert RoutingFamily.REMINDER_REVIEW_HOLD.value == "REMINDER_REVIEW_HOLD"
