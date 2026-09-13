"""Persistenz für versionierte, unveränderliche
`VariableAbrechnungTable`-Zeilen (Auftrag 13.09., HV-20260913-DASHBOARD).
Reines CRUD/Lesen - alle Fachregeln (Auth, Objektausschluss, optimistic
lock, genau eine aktuelle Version je Gruppe) leben in
`variableabrechnung/service.py`."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.infrastructure.db.tables import VariableAbrechnungTable


class VariableAbrechnungRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    @property
    def session_factory(self) -> sessionmaker[Session]:
        return self._session_factory

    def get(self, id: int, *, session: Session | None = None) -> VariableAbrechnungTable | None:
        if session is not None:
            return session.get(VariableAbrechnungTable, id)
        with self._session_factory() as owned_session:
            return owned_session.get(VariableAbrechnungTable, id)

    def by_import_id(self, import_id: str, *, session: Session | None = None) -> VariableAbrechnungTable | None:
        def _lesen(active_session: Session) -> VariableAbrechnungTable | None:
            statement = select(VariableAbrechnungTable).where(VariableAbrechnungTable.import_id == import_id)
            return active_session.execute(statement).scalar_one_or_none()

        if session is not None:
            return _lesen(session)
        with self._session_factory() as owned_session:
            return _lesen(owned_session)

    def aktuelle_version(
        self, einheit_id: str, art: str, leistungsmonat: str, *, session: Session | None = None
    ) -> VariableAbrechnungTable | None:
        def _lesen(active_session: Session) -> VariableAbrechnungTable | None:
            statement = (
                select(VariableAbrechnungTable)
                .where(VariableAbrechnungTable.einheit_id == einheit_id)
                .where(VariableAbrechnungTable.art == art)
                .where(VariableAbrechnungTable.leistungsmonat == leistungsmonat)
                .where(VariableAbrechnungTable.ist_aktuell.is_(True))
            )
            return active_session.execute(statement).scalar_one_or_none()

        if session is not None:
            return _lesen(session)
        with self._session_factory() as owned_session:
            return _lesen(owned_session)

    def liste_versionen(self, einheit_id: str, art: str, leistungsmonat: str) -> list[VariableAbrechnungTable]:
        with self._session_factory() as session:
            statement = (
                select(VariableAbrechnungTable)
                .where(VariableAbrechnungTable.einheit_id == einheit_id)
                .where(VariableAbrechnungTable.art == art)
                .where(VariableAbrechnungTable.leistungsmonat == leistungsmonat)
                .order_by(VariableAbrechnungTable.version.desc())
            )
            return list(session.execute(statement).scalars().all())

    def liste_aktuelle(
        self, *, leistungsmonat: str | None = None, gesellschaft_id: str | None = None
    ) -> list[VariableAbrechnungTable]:
        with self._session_factory() as session:
            statement = select(VariableAbrechnungTable).where(VariableAbrechnungTable.ist_aktuell.is_(True))
            if leistungsmonat is not None:
                statement = statement.where(VariableAbrechnungTable.leistungsmonat == leistungsmonat)
            if gesellschaft_id is not None:
                statement = statement.where(VariableAbrechnungTable.gesellschaft_id == gesellschaft_id)
            statement = statement.order_by(VariableAbrechnungTable.einheit_id, VariableAbrechnungTable.art)
            return list(session.execute(statement).scalars().all())

    def liste_alle(self) -> list[VariableAbrechnungTable]:
        with self._session_factory() as session:
            statement = select(VariableAbrechnungTable).order_by(VariableAbrechnungTable.id.desc())
            return list(session.execute(statement).scalars().all())

    def neue_version_anlegen(
        self, row: VariableAbrechnungTable, *, alte_id: int | None, session: Session | None = None
    ) -> VariableAbrechnungTable:
        """Setzt (falls vorhanden) die bisherige aktuelle Zeile
        `ist_aktuell=False` und fügt `row` (bereits `ist_aktuell=True`)
        EINEM einzigen DB-Vorgang hinzu - der partielle Unique-Index
        `uq_variable_abrechnung_aktuell` verhindert zusätzlich auf
        DB-Ebene, dass jemals zwei aktuelle Zeilen derselben Gruppe
        gleichzeitig bestehen (auch bei einer Race zwischen zwei
        gleichzeitigen Korrekturen)."""

        def _schreiben(active_session: Session) -> VariableAbrechnungTable:
            if alte_id is not None:
                alte = active_session.get(VariableAbrechnungTable, alte_id)
                if alte is not None:
                    alte.ist_aktuell = False
            active_session.add(row)
            active_session.flush()
            return row

        if session is not None:
            return _schreiben(session)
        with self._session_factory() as owned_session:
            ergebnis = _schreiben(owned_session)
            owned_session.commit()
            owned_session.refresh(ergebnis)
            return ergebnis
