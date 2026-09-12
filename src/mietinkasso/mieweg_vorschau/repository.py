"""Reines CRUD für `MieWegVorschauTable` - keine Auth-/Fachregel-Prüfung
(das lebt in `service.py`, siehe bestehende Konvention aus `vertragspruefung/`,
`op/`, `bank/`)."""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.infrastructure.db.tables import MieWegVorschauTable


class MieWegVorschauRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    @property
    def session_factory(self) -> sessionmaker[Session]:
        return self._session_factory

    def naechste_version(self, vertrag_id: str) -> int:
        with self._session_factory() as session:
            bisher = session.execute(
                select(func.max(MieWegVorschauTable.version)).where(MieWegVorschauTable.vertrag_id == vertrag_id)
            ).scalar_one_or_none()
            return (bisher or 0) + 1

    def anlegen(
        self,
        *,
        vertrag_id: str,
        version: int,
        rechtsordnung: str,
        ist_wohnungsrechner_fall: bool,
        ziel_bewertungsjahr: int | None,
        vollstaendig: bool,
        massgeblicher_hoechstbetrag_cent: int | None,
        fruehester_termin: date | None,
        eingaben_json: str,
        ergebnis_json: str,
        erstellt_von: str,
    ) -> MieWegVorschauTable:
        with self._session_factory() as session:
            row = MieWegVorschauTable(
                vertrag_id=vertrag_id,
                version=version,
                rechtsordnung=rechtsordnung,
                ist_wohnungsrechner_fall=ist_wohnungsrechner_fall,
                ziel_bewertungsjahr=ziel_bewertungsjahr,
                vollstaendig=vollstaendig,
                massgeblicher_hoechstbetrag_cent=massgeblicher_hoechstbetrag_cent,
                fruehester_termin=fruehester_termin,
                eingaben_json=eingaben_json,
                ergebnis_json=ergebnis_json,
                erstellt_von=erstellt_von,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def liste_fuer_vertrag(self, vertrag_id: str) -> list[MieWegVorschauTable]:
        with self._session_factory() as session:
            statement = (
                select(MieWegVorschauTable)
                .where(MieWegVorschauTable.vertrag_id == vertrag_id)
                .order_by(MieWegVorschauTable.version.desc())
            )
            return list(session.execute(statement).scalars().all())

    def aktuelle(self, vertrag_id: str) -> MieWegVorschauTable | None:
        with self._session_factory() as session:
            statement = (
                select(MieWegVorschauTable)
                .where(MieWegVorschauTable.vertrag_id == vertrag_id)
                .order_by(MieWegVorschauTable.version.desc())
                .limit(1)
            )
            return session.execute(statement).scalars().first()
