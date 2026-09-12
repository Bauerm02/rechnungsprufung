"""Reines CRUD für `VertragPruefungTable`/`IndexPruefbedarfTable` -
keine Auth-/Fachregel-Prüfung (das lebt in `service.py`, siehe
bestehende Konvention: `op/`, `bank/`, `vorschreibung/`,
`mahnwesen/`, `index/`)."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.infrastructure.db.tables import IndexPruefbedarfTable, VertragPruefungTable


class VertragPruefungRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    @property
    def session_factory(self) -> sessionmaker[Session]:
        return self._session_factory

    def naechste_version(self, vertrag_id: str, *, session: Session | None = None) -> int:
        def _lesen(active_session: Session) -> int:
            bisher = active_session.execute(
                select(func.max(VertragPruefungTable.version)).where(VertragPruefungTable.vertrag_id == vertrag_id)
            ).scalar_one_or_none()
            return (bisher or 0) + 1

        if session is not None:
            return _lesen(session)
        with self._session_factory() as owned_session:
            return _lesen(owned_session)

    def anlegen(
        self,
        *,
        vertrag_id: str,
        version: int,
        rechtsordnung: str,
        fachstatus: str,
        quellenbeleg_referenz: str,
        kommentar: str | None,
        erstellt_von: str,
        session: Session | None = None,
    ) -> VertragPruefungTable:
        """`session`: siehe `StammdatenRepository.upsert_gesellschaft` -
        erlaubt `VertragspruefungService.pruefung_anlegen`, diesen Insert
        und ein bei GEPRUEFT nötiges `upsert_vertrag` als EINE gemeinsame
        Transaktion mit gemeinsamem Rollback auszuführen (Codex-
        Rückprüfung Paket B: keine halb gespeicherte Freigabe, bei der die
        Prüfungszeile GEPRUEFT sagt, der Vertrag die Rechtsordnung aber
        nie übernommen hat)."""

        def _schreiben(active_session: Session) -> VertragPruefungTable:
            row = VertragPruefungTable(
                vertrag_id=vertrag_id,
                version=version,
                rechtsordnung=rechtsordnung,
                fachstatus=fachstatus,
                quellenbeleg_referenz=quellenbeleg_referenz,
                kommentar=kommentar,
                erstellt_von=erstellt_von,
            )
            active_session.add(row)
            return row

        if session is not None:
            row = _schreiben(session)
            session.flush()
            return row
        with self._session_factory() as owned_session:
            row = _schreiben(owned_session)
            owned_session.commit()
            owned_session.refresh(row)
            return row

    def liste_fuer_vertrag(self, vertrag_id: str) -> list[VertragPruefungTable]:
        with self._session_factory() as session:
            statement = (
                select(VertragPruefungTable)
                .where(VertragPruefungTable.vertrag_id == vertrag_id)
                .order_by(VertragPruefungTable.version.desc())
            )
            return list(session.execute(statement).scalars().all())

    def aktuelle(self, vertrag_id: str) -> VertragPruefungTable | None:
        with self._session_factory() as session:
            statement = (
                select(VertragPruefungTable)
                .where(VertragPruefungTable.vertrag_id == vertrag_id)
                .order_by(VertragPruefungTable.version.desc())
                .limit(1)
            )
            return session.execute(statement).scalars().first()


class IndexPruefbedarfRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def anlegen(
        self,
        *,
        vertrag_id: str,
        rechtsordnung: str | None,
        basis_reihe: str | None,
        basis_wert: Decimal | None,
        basis_monat: str | None,
        kommentar: str | None,
        erstellt_von: str,
    ) -> IndexPruefbedarfTable:
        with self._session_factory() as session:
            row = IndexPruefbedarfTable(
                vertrag_id=vertrag_id,
                rechtsordnung=rechtsordnung,
                basis_reihe=basis_reihe,
                basis_wert=basis_wert,
                basis_monat=basis_monat,
                kommentar=kommentar,
                erstellt_von=erstellt_von,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def liste_fuer_vertrag(self, vertrag_id: str) -> list[IndexPruefbedarfTable]:
        with self._session_factory() as session:
            # `id.desc()` als Tie-Breaker: `erstellt_am` (server_default
            # now()) kann bei schnell aufeinanderfolgenden Inserts
            # (insbesondere SQLite) identisch sein - die autoincrement-ID
            # ist die einzige garantiert monotone Reihenfolge.
            statement = (
                select(IndexPruefbedarfTable)
                .where(IndexPruefbedarfTable.vertrag_id == vertrag_id)
                .order_by(IndexPruefbedarfTable.erstellt_am.desc(), IndexPruefbedarfTable.id.desc())
            )
            return list(session.execute(statement).scalars().all())
