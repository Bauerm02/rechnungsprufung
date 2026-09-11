"""Idempotente Job-Ausführung für Scheduler/Worker, unabhängig von Browser
oder Chat-Session (Fachregel 8). Jeder fachliche Vorgang in diesem Modul
ist bereits für sich idempotent (eindeutige import_id, DB-Unique-
Constraints auf Vertrag+Monat bzw. outbox_key); `JobRunner` ergänzt das
um eine generische Sperre für Jobs, die selbst keine solche natürliche
Eindeutigkeit haben (z. B. ein täglicher Portfolio-Lauf), damit ein
Neustart oder ein zweiter gleichzeitiger Worker denselben Lauf nicht ein
zweites Mal anstößt.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.domain.enums import JobLaufStatus
from mietinkasso.infrastructure.db.tables import JobLockTable


class JobRunner:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def einmalig_ausfuehren(self, *, job_name: str, fachschluessel: str, fn: Callable[[], dict | None]) -> dict | None:
        """Führt `fn` genau einmal für (job_name, fachschluessel) aus. Ein
        zweiter Aufruf (paralleler Worker oder Neustart nach Absturz vor
        Abschluss) sieht den Lock bereits vorhanden und führt `fn` NICHT
        erneut aus, sondern gibt None zurück."""

        with self._session_factory() as session:
            lock = JobLockTable(job_name=job_name, fachschluessel=fachschluessel, status=JobLaufStatus.LAEUFT.value)
            session.add(lock)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return None

        try:
            ergebnis = fn() or {}
        except Exception:
            with self._session_factory() as session:
                row = session.execute(
                    select(JobLockTable)
                    .where(JobLockTable.job_name == job_name)
                    .where(JobLockTable.fachschluessel == fachschluessel)
                ).scalar_one()
                row.status = JobLaufStatus.FEHLGESCHLAGEN.value
                row.beendet_am = datetime.now(timezone.utc)
                session.commit()
            raise

        with self._session_factory() as session:
            row = session.execute(
                select(JobLockTable)
                .where(JobLockTable.job_name == job_name)
                .where(JobLockTable.fachschluessel == fachschluessel)
            ).scalar_one()
            row.status = JobLaufStatus.ERFOLGREICH.value
            row.ergebnis = ergebnis
            row.beendet_am = datetime.now(timezone.utc)
            session.commit()
        return ergebnis
