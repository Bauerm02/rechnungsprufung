from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import JSON, Date, DateTime, Index, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from invoice_automation.infrastructure.db.base import Base


class DocumentRecordTable(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    processing_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    source_system: Mapped[str] = mapped_column(String(64))
    source_path: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(String(512))
    content_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProcessedInvoiceTable(Base):
    __tablename__ = "processed_invoices"
    __table_args__ = (
        UniqueConstraint("sender_name_normalized", "invoice_number_normalized", name="uq_invoice_sender_number"),
        Index("ix_invoice_sender_number", "sender_name_normalized", "invoice_number_normalized"),
        Index("ix_invoice_sender_content_hash", "sender_name_normalized", "content_hash"),
        Index("ix_invoice_sender_amount", "sender_name_normalized", "amount_decimal"),
        Index("ix_invoice_sender_amount_date", "sender_name_normalized", "amount_decimal", "document_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    processing_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    sender_name_normalized: Mapped[str] = mapped_column(String(512))
    invoice_number_normalized: Mapped[str | None] = mapped_column(String(256), nullable=True)
    document_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    recipient_name_normalized: Mapped[str | None] = mapped_column(String(512), nullable=True)
    iban_normalized: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    amount_decimal: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(64))
    stored_filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLogTable(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    processing_id: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    step_name: Mapped[str] = mapped_column(String(128))
    input_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    output_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    decision_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NotificationOutboxTable(Base):
    __tablename__ = "notification_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    processing_id: Mapped[str] = mapped_column(String(64), index=True)
    notification_type: Mapped[str] = mapped_column(String(64))
    recipients: Mapped[list] = mapped_column(JSON, default=list)
    subject: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
