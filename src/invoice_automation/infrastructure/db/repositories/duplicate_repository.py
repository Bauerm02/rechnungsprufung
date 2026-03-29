from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from invoice_automation.domain.models import DuplicateCheckResult, InvoiceRegistryEntry
from invoice_automation.infrastructure.db.tables import ProcessedInvoiceTable


class SqlAlchemyDuplicateRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def find_duplicate(
        self,
        *,
        sender_name_normalized: str,
        invoice_number_normalized: str | None,
        document_date: date | None = None,
        recipient_name_normalized: str | None = None,
        iban_normalized: str | None = None,
        content_hash: str | None = None,
        amount_decimal: Decimal | None = None,
    ) -> DuplicateCheckResult:
        with self._session_factory() as session:
            if not sender_name_normalized:
                return DuplicateCheckResult(
                    is_duplicate=False,
                    match_reason="Invoice number missing and duplicate fingerprint is incomplete because sender normalization is unavailable.",
                )

            if invoice_number_normalized:
                statement = (
                    select(ProcessedInvoiceTable)
                    .where(ProcessedInvoiceTable.sender_name_normalized == sender_name_normalized)
                    .where(ProcessedInvoiceTable.invoice_number_normalized == invoice_number_normalized)
                    .limit(1)
                )
                row = session.execute(statement).scalar_one_or_none()
                if row is None:
                    return DuplicateCheckResult(is_duplicate=False, match_reason="No prior processed invoice found.")
                return DuplicateCheckResult(
                    is_duplicate=True,
                    match_key=f"{sender_name_normalized}::{invoice_number_normalized}",
                    matched_processing_id=row.processing_id,
                    match_reason="Matched prior processed invoice in the primary database registry.",
                )

            usable_content_hash = self._usable_content_hash(content_hash)

            if content_hash and not usable_content_hash:
                if amount_decimal is None:
                    return DuplicateCheckResult(
                        is_duplicate=False,
                        match_reason="Invoice number missing and content hash is incomplete; no stronger duplicate fingerprint is available.",
                    )

            if usable_content_hash:
                content_hash_statement = (
                    select(ProcessedInvoiceTable)
                    .where(ProcessedInvoiceTable.sender_name_normalized == sender_name_normalized)
                    .where(ProcessedInvoiceTable.content_hash == usable_content_hash)
                )
                hash_rows = session.execute(content_hash_statement.limit(3)).scalars().all()
                if hash_rows:
                    if document_date is not None:
                        exact_date_rows = [row for row in hash_rows if row.document_date == document_date]
                        if len(exact_date_rows) == 1:
                            return DuplicateCheckResult(
                                is_duplicate=True,
                                match_key=f"{sender_name_normalized}::{usable_content_hash}::{document_date.isoformat()}",
                                matched_processing_id=exact_date_rows[0].processing_id,
                                match_reason="Matched prior processed invoice via content hash and document date fallback.",
                            )
                        if len(exact_date_rows) > 1:
                            return DuplicateCheckResult(
                                is_duplicate=False,
                                match_reason="Ambiguous content-hash duplicate signature even after document-date filtering.",
                            )
                        if len(hash_rows) == 1 and hash_rows[0].document_date is None:
                            return DuplicateCheckResult(
                                is_duplicate=True,
                                match_key=f"{sender_name_normalized}::{usable_content_hash}",
                                matched_processing_id=hash_rows[0].processing_id,
                                match_reason="Matched prior processed invoice via conservative content-hash fallback.",
                            )
                        return DuplicateCheckResult(
                            is_duplicate=False,
                            match_reason="Content hash matched but document date did not match a unique prior invoice.",
                        )

                    if len(hash_rows) > 1:
                        return DuplicateCheckResult(
                            is_duplicate=False,
                            match_reason="Ambiguous content-hash duplicate signature; no automatic duplicate override was applied.",
                        )

                    return DuplicateCheckResult(
                        is_duplicate=True,
                        match_key=f"{sender_name_normalized}::{usable_content_hash}",
                        matched_processing_id=hash_rows[0].processing_id,
                        match_reason="Matched prior processed invoice via conservative content-hash fallback.",
                    )

            if not sender_name_normalized or amount_decimal is None:
                return DuplicateCheckResult(
                    is_duplicate=False,
                    match_reason="Invoice number missing and fallback duplicate signature is incomplete.",
                )

            fallback_statement = (
                select(ProcessedInvoiceTable)
                .where(ProcessedInvoiceTable.sender_name_normalized == sender_name_normalized)
                .where(ProcessedInvoiceTable.amount_decimal == amount_decimal)
            )
            signature_parts = [sender_name_normalized, f"{amount_decimal:.2f}"]

            if document_date is not None:
                fallback_statement = fallback_statement.where(ProcessedInvoiceTable.document_date == document_date)
                signature_parts.append(document_date.isoformat())

            if iban_normalized:
                fallback_statement = fallback_statement.where(ProcessedInvoiceTable.iban_normalized == iban_normalized)
                signature_parts.append(iban_normalized)
            elif recipient_name_normalized and document_date is not None:
                fallback_statement = fallback_statement.where(ProcessedInvoiceTable.recipient_name_normalized == recipient_name_normalized)
                signature_parts.append(recipient_name_normalized)
            elif recipient_name_normalized:
                return DuplicateCheckResult(
                    is_duplicate=False,
                    match_reason="Invoice number missing and recipient-only fallback is too weak without a usable content hash, document date, or IBAN.",
                )
            else:
                return DuplicateCheckResult(
                    is_duplicate=False,
                    match_reason="Invoice number missing and no conservative fallback fingerprint is available.",
                )

            rows = session.execute(fallback_statement.limit(2)).scalars().all()
            if not rows:
                return DuplicateCheckResult(
                    is_duplicate=False,
                    match_reason="No prior processed invoice matched the fallback duplicate signature.",
                )
            if len(rows) > 1:
                return DuplicateCheckResult(
                    is_duplicate=False,
                    match_reason="Ambiguous fallback duplicate signature; no automatic duplicate override was applied.",
                )

            return DuplicateCheckResult(
                is_duplicate=True,
                match_key="::".join(signature_parts),
                matched_processing_id=rows[0].processing_id,
                match_reason="Matched prior processed invoice via conservative fallback duplicate signature.",
            )

    def register(self, entry: InvoiceRegistryEntry) -> InvoiceRegistryEntry:
        with self._session_factory() as session:
            row = ProcessedInvoiceTable(
                processing_id=entry.processing_id,
                sender_name_normalized=entry.sender_name_normalized,
                invoice_number_normalized=entry.invoice_number_normalized,
                document_date=entry.document_date,
                recipient_name_normalized=entry.recipient_name_normalized,
                iban_normalized=entry.iban_normalized,
                content_hash=entry.content_hash,
                amount_decimal=entry.amount_decimal,
                status=entry.status.value,
                stored_filename=entry.stored_filename,
            )
            session.add(row)
            session.commit()
            return entry

    def _usable_content_hash(self, content_hash: str | None) -> str | None:
        if not content_hash:
            return None
        if len(content_hash) < 8:
            return None
        if not any(character.isalpha() for character in content_hash):
            return None
        if not any(character.isdigit() for character in content_hash):
            return None
        return content_hash
