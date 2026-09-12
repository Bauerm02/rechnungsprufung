from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.domain.enums import MahnStatus
from mietinkasso.infrastructure.db.tables import MahnFallTable, MahnPolicyTable


class MahnPolicyRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def naechste_version(self) -> int:
        with self._session_factory() as session:
            versionen = [v for (v,) in session.execute(select(MahnPolicyTable.version)).all()]
            return (max(versionen) + 1) if versionen else 1

    def anlegen(self, **kwargs) -> MahnPolicyTable:
        with self._session_factory() as session:
            row = MahnPolicyTable(version=self.naechste_version(), **kwargs)
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def freigeben(self, policy_id: int) -> MahnPolicyTable:
        with self._session_factory() as session:
            row = session.get(MahnPolicyTable, policy_id)
            if row is None:
                raise ValueError(f"Unbekannte MahnPolicy {policy_id}")
            row.status = "FREIGEGEBEN"
            row.freigegeben_am = datetime.now(timezone.utc)
            session.commit()
            session.refresh(row)
            return row

    def aktuelle_freigegebene(self) -> MahnPolicyTable | None:
        with self._session_factory() as session:
            statement = (
                select(MahnPolicyTable)
                .where(MahnPolicyTable.status == "FREIGEGEBEN")
                .order_by(MahnPolicyTable.version.desc())
                .limit(1)
            )
            return session.execute(statement).scalar_one_or_none()

    def alle(self) -> list[MahnPolicyTable]:
        with self._session_factory() as session:
            statement = select(MahnPolicyTable).order_by(MahnPolicyTable.version.desc())
            return list(session.execute(statement).scalars().all())


class MahnFallRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def get_or_create(self, *, outbox_key: str, **kwargs) -> MahnFallTable:
        """Race-safe wie bei der Vorschreibung: zwei Worker, die gleichzeitig
        denselben Fall planen, landen wegen der Unique-Constraint auf
        outbox_key garantiert bei genau einer Zeile."""

        with self._session_factory() as session:
            existing = session.execute(
                select(MahnFallTable).where(MahnFallTable.outbox_key == outbox_key)
            ).scalar_one_or_none()
            if existing is not None:
                return existing
            row = MahnFallTable(outbox_key=outbox_key, **kwargs)
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return session.execute(
                    select(MahnFallTable).where(MahnFallTable.outbox_key == outbox_key)
                ).scalar_one()
            session.refresh(row)
            return row

    def get(self, mahnfall_id: int) -> MahnFallTable | None:
        with self._session_factory() as session:
            return session.get(MahnFallTable, mahnfall_id)

    def list_fuer_vertrag(self, vertrag_id: str) -> list[MahnFallTable]:
        with self._session_factory() as session:
            statement = (
                select(MahnFallTable)
                .where(MahnFallTable.vertrag_id == vertrag_id)
                .order_by(MahnFallTable.id.desc())
            )
            return list(session.execute(statement).scalars().all())

    def letzter_mahnfall_fuer_forderung(self, forderung_op_position_id: int) -> MahnFallTable | None:
        """Jede Forderung (identifiziert über die sie erzeugende OP-Zeile)
        hat ihren EIGENEN Stufe1->Stufe2-Zyklus, unabhängig davon, wie weit
        andere Forderungen desselben Vertrags schon gediehen sind."""

        with self._session_factory() as session:
            statement = (
                select(MahnFallTable)
                .where(MahnFallTable.forderung_op_position_id == forderung_op_position_id)
                .order_by(MahnFallTable.id.desc())
                .limit(1)
            )
            return session.execute(statement).scalar_one_or_none()

    def letzter_mahnfall_je_stufe_fuer_forderung(self, forderung_op_position_id: int, stufe: int) -> MahnFallTable | None:
        with self._session_factory() as session:
            statement = (
                select(MahnFallTable)
                .where(MahnFallTable.forderung_op_position_id == forderung_op_position_id)
                .where(MahnFallTable.stufe == stufe)
                .order_by(MahnFallTable.id.desc())
                .limit(1)
            )
            return session.execute(statement).scalar_one_or_none()

    def claim_fuer_versand(self, mahnfall_id: int, *, jetzt: datetime | None = None) -> bool:
        """Atomarer Compare-and-Swap GEPLANT -> IN_VERSAND: nur der Worker,
        dessen UPDATE eine Zeile trifft, darf tatsächlich den
        Versand-Provider aufrufen. Ein zweiter Worker (oder ein Neustart,
        der denselben Fall erneut anstößt) sieht rowcount==0 und bricht ab,
        statt doppelt zu versenden. Der Status bleibt danach so lange
        IN_VERSAND, bis `versenden()` ihn auf GESENDET/UNSICHER auflöst -
        ein Absturz mittendrin macht den Fall NICHT wieder als GEPLANT
        greifbar (siehe `verwaiste_in_versand` für die Recovery)."""

        with self._session_factory() as session:
            result = session.execute(
                update(MahnFallTable)
                .where(MahnFallTable.id == mahnfall_id)
                .where(MahnFallTable.status == MahnStatus.GEPLANT.value)
                .values(status=MahnStatus.IN_VERSAND.value, versand_beansprucht_am=jetzt or datetime.now(timezone.utc))
            )
            session.commit()
            return result.rowcount > 0

    def verwaiste_in_versand(self, *, aelter_als: datetime) -> list[MahnFallTable]:
        """Fälle, die seit `aelter_als` in IN_VERSAND feststecken - z. B.
        weil der Worker zwischen `claim_fuer_versand` und dem Auflösen des
        Ergebnisses abgestürzt ist. Werden NICHT automatisch erneut
        versucht (kein blinder Retry), sondern für die manuelle Klärung als
        Kandidaten zurückgegeben."""

        with self._session_factory() as session:
            statement = (
                select(MahnFallTable)
                .where(MahnFallTable.status == MahnStatus.IN_VERSAND.value)
                .where(MahnFallTable.versand_beansprucht_am < aelter_als)
            )
            return list(session.execute(statement).scalars().all())

    def set_status(self, mahnfall_id: int, status: str, **zusatz) -> MahnFallTable:
        with self._session_factory() as session:
            row = session.get(MahnFallTable, mahnfall_id)
            if row is None:
                raise ValueError(f"Unbekannter MahnFall {mahnfall_id}")
            row.status = status
            for key, value in zusatz.items():
                setattr(row, key, value)
            session.commit()
            session.refresh(row)
            return row
