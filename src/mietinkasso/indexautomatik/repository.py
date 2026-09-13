"""Reines CRUD für die Indexautomatik-Tabellen - keine Auth-/Fachregel-
Prüfung (die lebt in den `*_service.py`-Dateien, siehe bestehende
Konvention aus `mieweg_vorschau/`, `mahnwesen/`)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.infrastructure.db.tables import (
    ErhoehungsschreibenTable,
    IndexautomatikLaufTable,
    RechtsprofilTable,
    VertragsendeErinnerungTable,
    VpiJahreswertTable,
)


class VpiRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def erfassen(
        self, *, jahr: int, wert: Decimal, quelle: str, quelle_datum: date, erfasst_von: str
    ) -> VpiJahreswertTable:
        with self._session_factory() as session:
            row = session.get(VpiJahreswertTable, jahr)
            if row is None:
                row = VpiJahreswertTable(
                    jahr=jahr, wert=wert, quelle=quelle, quelle_datum=quelle_datum, erfasst_von=erfasst_von
                )
                session.add(row)
            else:
                row.wert = wert
                row.quelle = quelle
                row.quelle_datum = quelle_datum
                row.erfasst_von = erfasst_von
            session.commit()
            session.refresh(row)
            return row

    def get(self, jahr: int) -> VpiJahreswertTable | None:
        with self._session_factory() as session:
            return session.get(VpiJahreswertTable, jahr)

    def alle_als_dict(self) -> dict[int, Decimal]:
        with self._session_factory() as session:
            rows = session.execute(select(VpiJahreswertTable)).scalars().all()
            return {row.jahr: row.wert for row in rows}

    def liste(self) -> list[VpiJahreswertTable]:
        with self._session_factory() as session:
            return list(session.execute(select(VpiJahreswertTable).order_by(VpiJahreswertTable.jahr)).scalars().all())


class RechtsprofilRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def naechste_version(self, vertrag_id: str) -> int:
        with self._session_factory() as session:
            bisher = session.execute(
                select(func.max(RechtsprofilTable.version)).where(RechtsprofilTable.vertrag_id == vertrag_id)
            ).scalar_one_or_none()
            return (bisher or 0) + 1

    def anlegen(self, row: RechtsprofilTable) -> RechtsprofilTable:
        with self._session_factory() as session:
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def get(self, id: int) -> RechtsprofilTable | None:
        with self._session_factory() as session:
            return session.get(RechtsprofilTable, id)

    def liste_fuer_vertrag(self, vertrag_id: str) -> list[RechtsprofilTable]:
        with self._session_factory() as session:
            statement = (
                select(RechtsprofilTable)
                .where(RechtsprofilTable.vertrag_id == vertrag_id)
                .order_by(RechtsprofilTable.version.desc())
            )
            return list(session.execute(statement).scalars().all())

    def freigegebenes_profil(self, vertrag_id: str) -> RechtsprofilTable | None:
        with self._session_factory() as session:
            statement = (
                select(RechtsprofilTable)
                .where(RechtsprofilTable.vertrag_id == vertrag_id)
                .where(RechtsprofilTable.status == "FREIGEGEBEN")
                .order_by(RechtsprofilTable.version.desc())
                .limit(1)
            )
            return session.execute(statement).scalars().first()

    def freigeben(self, id: int, *, freigegeben_von: str, quelle_hash: str) -> RechtsprofilTable:
        with self._session_factory() as session:
            row = session.get(RechtsprofilTable, id)
            if row is None:
                raise ValueError(f"Unbekanntes Rechtsprofil {id}")
            # Genau ein FREIGEGEBENES Profil je Vertrag: eine neue Freigabe
            # invalidiert automatisch jede vorher freigegebene Version
            # desselben Vertrags (kein doppelt "aktives" Profil möglich).
            session.execute(
                update(RechtsprofilTable)
                .where(RechtsprofilTable.vertrag_id == row.vertrag_id)
                .where(RechtsprofilTable.status == "FREIGEGEBEN")
                .values(status="INVALIDIERT")
            )
            row.status = "FREIGEGEBEN"
            row.freigegeben_von = freigegeben_von
            row.freigegeben_am = datetime.now(timezone.utc)
            row.quelle_hash = quelle_hash
            session.commit()
            session.refresh(row)
            return row

    def invalidieren(self, id: int) -> None:
        with self._session_factory() as session:
            row = session.get(RechtsprofilTable, id)
            if row is None:
                raise ValueError(f"Unbekanntes Rechtsprofil {id}")
            row.status = "INVALIDIERT"
            session.commit()


class IndexautomatikLaufRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def get_by_periode(self, vertrag_id: str, periode: str) -> IndexautomatikLaufTable | None:
        with self._session_factory() as session:
            statement = select(IndexautomatikLaufTable).where(
                IndexautomatikLaufTable.vertrag_id == vertrag_id, IndexautomatikLaufTable.periode == periode
            )
            return session.execute(statement).scalars().first()

    def anlegen(self, row: IndexautomatikLaufTable) -> IndexautomatikLaufTable:
        """Idempotent über den (vertrag_id, periode)-Unique-Constraint:
        ein zweiter Versuch für denselben Monat (Parallelstart/Neustart
        nach Absturz) schlägt mit `IntegrityError` fehl; der Aufrufer
        bekommt dann die bereits existierende Zeile zurück statt eines
        Duplikats."""

        with self._session_factory() as session:
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                bestehend = self.get_by_periode(row.vertrag_id, row.periode)
                if bestehend is None:
                    raise
                return bestehend
            session.refresh(row)
            return row

    def liste_fuer_vertrag(self, vertrag_id: str) -> list[IndexautomatikLaufTable]:
        with self._session_factory() as session:
            statement = (
                select(IndexautomatikLaufTable)
                .where(IndexautomatikLaufTable.vertrag_id == vertrag_id)
                .order_by(IndexautomatikLaufTable.periode.desc())
            )
            return list(session.execute(statement).scalars().all())


class ErhoehungsschreibenRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def get(self, id: int) -> ErhoehungsschreibenTable | None:
        with self._session_factory() as session:
            return session.get(ErhoehungsschreibenTable, id)

    def get_by_ziel(self, vertrag_id: str, ziel_bewertungsjahr: int) -> ErhoehungsschreibenTable | None:
        with self._session_factory() as session:
            statement = select(ErhoehungsschreibenTable).where(
                ErhoehungsschreibenTable.vertrag_id == vertrag_id,
                ErhoehungsschreibenTable.ziel_bewertungsjahr == ziel_bewertungsjahr,
            )
            return session.execute(statement).scalars().first()

    def get_by_index_anpassung(self, index_anpassung_id: int) -> ErhoehungsschreibenTable | None:
        with self._session_factory() as session:
            statement = select(ErhoehungsschreibenTable).where(
                ErhoehungsschreibenTable.index_anpassung_id == index_anpassung_id
            )
            return session.execute(statement).scalars().first()

    def anlegen(self, row: ErhoehungsschreibenTable) -> ErhoehungsschreibenTable:
        """Idempotent - für den MieWeG-Pfad (`ziel_bewertungsjahr`
        gesetzt) über den partiellen (vertrag_id, ziel_bewertungsjahr)-
        Unique-Index; für den Geschäftsraum-/Klausel-Pfad
        (`index_anpassung_id` gesetzt, `ziel_bewertungsjahr` NULL) ist
        die Idempotenz bereits vorgelagert über
        `IndexautomatikLaufRepository` (höchstens ein Versuch pro
        Kalendermonat) sichergestellt - ein `IntegrityError` sollte für
        diesen Pfad nicht auftreten, wird hier aber trotzdem defensiv
        über `index_anpassung_id` aufgelöst statt weiterzuwerfen."""

        with self._session_factory() as session:
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                if row.ziel_bewertungsjahr is not None:
                    bestehend = self.get_by_ziel(row.vertrag_id, row.ziel_bewertungsjahr)
                elif row.index_anpassung_id is not None:
                    bestehend = self.get_by_index_anpassung(row.index_anpassung_id)
                else:
                    bestehend = None
                if bestehend is None:
                    raise
                return bestehend
            session.refresh(row)
            return row

    def claim_fuer_versand(self, id: int, *, jetzt: datetime | None = None) -> bool:
        """Atomarer Compare-and-Swap BEREIT->IN_VERSAND (analog
        `MahnFallRepository.claim_fuer_versand`): nur der Worker, dessen
        UPDATE tatsächlich eine Zeile trifft, darf den Transportadapter
        aufrufen. Unabhängiger Review (0d65e2b): die vorherige Fassung
        setzte NUR `versand_beansprucht_am`, ließ `status` aber auf
        BEREIT stehen - das WHERE-Kriterium eines zweiten, praktisch
        gleichzeitigen Aufrufs traf dadurch weiterhin zu und beide
        Worker erhielten `True` (Doppelversand). Der Status wechselt
        jetzt selbst TEIL des atomaren UPDATE-Prädikats zu IN_VERSAND,
        exakt wie beim bestehenden Mahnwesen-Muster."""

        with self._session_factory() as session:
            result = session.execute(
                update(ErhoehungsschreibenTable)
                .where(ErhoehungsschreibenTable.id == id)
                .where(ErhoehungsschreibenTable.status == "BEREIT")
                .values(status="IN_VERSAND", versand_beansprucht_am=jetzt or datetime.now(timezone.utc))
            )
            session.commit()
            return result.rowcount > 0

    def verwaiste_in_versand(self, *, aelter_als: datetime) -> list[ErhoehungsschreibenTable]:
        """Recovery für einen Absturz zwischen `claim_fuer_versand` und
        dem Auflösen des Ergebnisses (analog
        `mahnwesen/service.py::markiere_verwaiste_als_unsicher`)."""

        with self._session_factory() as session:
            statement = select(ErhoehungsschreibenTable).where(
                ErhoehungsschreibenTable.status == "IN_VERSAND",
                ErhoehungsschreibenTable.versand_beansprucht_am < aelter_als,
            )
            return list(session.execute(statement).scalars().all())

    def set_status(self, id: int, status: str, **felder) -> ErhoehungsschreibenTable:
        with self._session_factory() as session:
            row = session.get(ErhoehungsschreibenTable, id)
            if row is None:
                raise ValueError(f"Unbekanntes Erhoehungsschreiben {id}")
            row.status = status
            for feld, wert in felder.items():
                setattr(row, feld, wert)
            session.commit()
            session.refresh(row)
            return row

    def liste_fuer_vertrag(self, vertrag_id: str) -> list[ErhoehungsschreibenTable]:
        with self._session_factory() as session:
            statement = (
                select(ErhoehungsschreibenTable)
                .where(ErhoehungsschreibenTable.vertrag_id == vertrag_id)
                .order_by(ErhoehungsschreibenTable.ziel_bewertungsjahr.desc())
            )
            return list(session.execute(statement).scalars().all())

    def liste_nach_status(self, status: str) -> list[ErhoehungsschreibenTable]:
        with self._session_factory() as session:
            statement = select(ErhoehungsschreibenTable).where(ErhoehungsschreibenTable.status == status)
            return list(session.execute(statement).scalars().all())

    def liste_alle(self) -> list[ErhoehungsschreibenTable]:
        with self._session_factory() as session:
            statement = select(ErhoehungsschreibenTable).order_by(ErhoehungsschreibenTable.id.desc())
            return list(session.execute(statement).scalars().all())


class VertragsendeErinnerungRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def get(self, id: int) -> VertragsendeErinnerungTable | None:
        with self._session_factory() as session:
            return session.get(VertragsendeErinnerungTable, id)

    def get_by_enddatum(self, vertrag_id: str, end_datum: date) -> VertragsendeErinnerungTable | None:
        with self._session_factory() as session:
            statement = select(VertragsendeErinnerungTable).where(
                VertragsendeErinnerungTable.vertrag_id == vertrag_id,
                VertragsendeErinnerungTable.end_datum == end_datum,
            )
            return session.execute(statement).scalars().first()

    def anlegen(self, row: VertragsendeErinnerungTable) -> VertragsendeErinnerungTable:
        with self._session_factory() as session:
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                bestehend = self.get_by_enddatum(row.vertrag_id, row.end_datum)
                if bestehend is None:
                    raise
                return bestehend
            session.refresh(row)
            return row

    def invalidiere_veraltete(
        self, vertrag_id: str, aktuelles_end_datum: date | None
    ) -> list[VertragsendeErinnerungTable]:
        """Fachregel: ändert sich `VertragTable.gueltig_bis` (Verlängerung/
        Verkürzung ODER Wechsel auf unbefristet), werden alle noch nicht
        ungültigen Erinnerungen für ein ANDERES Enddatum desselben
        Vertrags ungültig - eine neue Zeile für ein neues, weiterhin
        befristetes Enddatum wird separat (idempotent über den
        Unique-Constraint) angelegt.

        Unabhängiger Review (0d65e2b): zwei bisherige Lücken behoben -
        (1) `aktuelles_end_datum=None` (Vertrag wurde unbefristet)
        invalidiert jetzt ALLE noch nicht ungültigen Erinnerungen
        dieses Vertrags, unabhängig vom jeweiligen `end_datum` (ein
        SQL-`!=`-Vergleich gegen NULL wäre sonst nie wahr gewesen und
        hätte stattdessen fälschlich GAR NICHTS invalidiert). (2) auch
        eine bereits `ENTSCHIEDEN`e, aber laut Fachregel NIE automatisch
        versendete Zeile (der Mieterentwurf bleibt bis zur manuellen
        Freigabe durch einen Menschen ungesendet) wird bei einer
        Enddatum-Änderung mit invalidiert, nicht nur OFFEN/
        BENACHRICHTIGT."""

        with self._session_factory() as session:
            statement = (
                select(VertragsendeErinnerungTable)
                .where(VertragsendeErinnerungTable.vertrag_id == vertrag_id)
                .where(VertragsendeErinnerungTable.status.in_(["OFFEN", "BENACHRICHTIGT", "ENTSCHIEDEN"]))
            )
            if aktuelles_end_datum is not None:
                statement = statement.where(VertragsendeErinnerungTable.end_datum != aktuelles_end_datum)
            betroffene = list(session.execute(statement).scalars().all())
            for row in betroffene:
                row.status = "UNGUELTIG"
            session.commit()
            for row in betroffene:
                session.refresh(row)
            return betroffene

    def set_status(self, id: int, status: str, **felder) -> VertragsendeErinnerungTable:
        with self._session_factory() as session:
            row = session.get(VertragsendeErinnerungTable, id)
            if row is None:
                raise ValueError(f"Unbekannte VertragsendeErinnerung {id}")
            row.status = status
            for feld, wert in felder.items():
                setattr(row, feld, wert)
            session.commit()
            session.refresh(row)
            return row

    def liste_faellig(self, *, heute: date) -> list[VertragsendeErinnerungTable]:
        with self._session_factory() as session:
            statement = (
                select(VertragsendeErinnerungTable)
                .where(VertragsendeErinnerungTable.status == "OFFEN")
                .where(VertragsendeErinnerungTable.faellig_am <= heute)
            )
            return list(session.execute(statement).scalars().all())

    def liste_alle(self) -> list[VertragsendeErinnerungTable]:
        with self._session_factory() as session:
            statement = select(VertragsendeErinnerungTable).order_by(VertragsendeErinnerungTable.faellig_am)
            return list(session.execute(statement).scalars().all())
