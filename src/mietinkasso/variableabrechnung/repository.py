"""Persistenz für versionierte, unveränderliche
`VariableAbrechnungTable`-Zeilen (Auftrag 13.09., HV-20260913-DASHBOARD).
Reines CRUD/Lesen - alle Fachregeln (Auth, Objektausschluss, optimistic
lock, genau eine aktuelle Version je Gruppe) leben in
`variableabrechnung/service.py`."""

from __future__ import annotations

from sqlalchemy import select, update
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
    ) -> VariableAbrechnungTable | None:
        """Wenn `alte_id` gesetzt ist, wird die bisherige aktuelle Zeile
        NUR über eine ATOMARE bedingte UPDATE-Anweisung
        (`ist_aktuell: True -> False`, `WHERE id=alte_id AND
        ist_aktuell=True`) entwertet - trifft diese Bedingung nicht mehr
        zu (`rowcount != 1`, weil zwischenzeitlich bereits eine andere
        Korrektur `alte_id` entwertet hat), wird GAR NICHTS geschrieben
        und `None` zurückgegeben.

        Unabhängiger Review: ein reines "lesen, dann schreiben"
        (`session.get` + Attributzuweisung) schließt die Lücke zwischen
        Prüfung und Schreibung NICHT - ein CSV-Import, dessen Plan-Hash
        beim erneuten Prüfen `unmittelbar vor dem Schreiben` zufällig
        wieder zur inzwischen aktuellen (aber fremden) Version passt,
        konnte so eine zwischenzeitliche fremde Korrektur stillschweigend
        überschreiben. Die bedingte UPDATE-Anweisung ist die
        tatsächliche, atomare Durchsetzung des optimistic locks - nicht
        nur eine vorgelagerte Prüfung.

        Der partielle Unique-Index `uq_variable_abrechnung_aktuell`
        bleibt als zusätzliche, unabhängige DB-Garantie bestehen (auch
        bei `alte_id=None`, also einer ganz neuen Gruppe, verhindert er
        zwei gleichzeitig aktuelle Zeilen)."""

        def _schreiben(active_session: Session) -> VariableAbrechnungTable | None:
            if alte_id is not None:
                ergebnis = active_session.execute(
                    update(VariableAbrechnungTable)
                    .where(VariableAbrechnungTable.id == alte_id)
                    .where(VariableAbrechnungTable.ist_aktuell.is_(True))
                    .values(ist_aktuell=False)
                )
                if ergebnis.rowcount != 1:
                    return None
            active_session.add(row)
            active_session.flush()
            return row

        if session is not None:
            return _schreiben(session)
        with self._session_factory() as owned_session:
            ergebnis = _schreiben(owned_session)
            if ergebnis is None:
                owned_session.rollback()
                return None
            owned_session.commit()
            owned_session.refresh(ergebnis)
            return ergebnis
