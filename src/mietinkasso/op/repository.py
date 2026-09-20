from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.domain.enums import OPPositionStatus, OPTyp
from mietinkasso.domain.exceptions import ImportConflictError
from mietinkasso.infrastructure.db.tables import OPPositionTable, ZuordnungTable
from mietinkasso.infrastructure.db.sqlite_write_lock import schreibgesperrte_session
from mietinkasso.op.validierung import pruefe_betrag_positiv, pruefe_bezug


class OPRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    @property
    def session_factory(self) -> sessionmaker[Session]:
        return self._session_factory

    def find_by_import_id(self, import_id: str) -> OPPositionTable | None:
        with self._session_factory() as session:
            return session.execute(
                select(OPPositionTable).where(OPPositionTable.import_id == import_id)
            ).scalar_one_or_none()

    def insert_idempotent(self, row: OPPositionTable, *, session: Session | None = None) -> OPPositionTable:
        """Insert `row`, unless its import_id already exists.

        - Same import_id + same quelle_hash -> replay, return the existing
          row untouched (Doppelimport wirkungslos).
        - Same import_id + different quelle_hash -> real conflict.
        - No import_id -> always inserted (manual/system-internal postings
          that do not claim idempotency, e.g. Storno rows).

        Pass an existing `session` to make this insert part of a larger,
        caller-managed transaction (e.g. OP-Buchung + Zuordnung + Audit in
        einer DB-Transaktion) - in that case this method flushes but does
        NOT commit; the caller commits/rolls back everything together.
        Without a `session`, this method opens and commits its own
        transaction as before."""

        if session is not None:
            return self._insert_idempotent(session, row)
        with schreibgesperrte_session(self._session_factory) as owned_session:
            result = self._insert_idempotent(owned_session, row)
            owned_session.commit()
            owned_session.refresh(result)
            return result

    def _insert_idempotent(self, session: Session, row: OPPositionTable) -> OPPositionTable:
        if row.import_id is not None:
            existing = session.execute(
                select(OPPositionTable).where(OPPositionTable.import_id == row.import_id)
            ).scalar_one_or_none()
            if existing is not None:
                if existing.quelle_hash != row.quelle_hash:
                    raise ImportConflictError(
                        f"import_id '{row.import_id}' bereits mit anderem Inhalt vorhanden "
                        f"(gespeichert: {existing.quelle_hash}, neu: {row.quelle_hash})."
                    )
                return existing
        pruefe_betrag_positiv(OPTyp(row.typ), row.betrag_cent)
        if row.bezieht_sich_auf_id is not None:
            ziel = session.get(OPPositionTable, row.bezieht_sich_auf_id, with_for_update=True)
            pruefe_bezug(row, ziel)
        session.add(row)
        session.flush()
        return row

    def find_eroeffnung(self, konto_id: str, *, session: Session | None = None) -> OPPositionTable | None:
        def _query(active_session: Session) -> OPPositionTable | None:
            return active_session.execute(
                select(OPPositionTable)
                .where(OPPositionTable.konto_id == konto_id)
                .where(OPPositionTable.typ == "EROEFFNUNG")
                .where(OPPositionTable.status == OPPositionStatus.AKTIV.value)
            ).scalar_one_or_none()

        if session is not None:
            return _query(session)
        with self._session_factory() as owned_session:
            return _query(owned_session)

    def list_aktiv(self, konto_id: str, *, session: Session | None = None) -> list[OPPositionTable]:
        """`session`: siehe `insert_idempotent` - übergeben, um diese
        Leseabfrage Teil einer größeren, vom Aufrufer verwalteten
        (ggf. schreibgesperrten) Transaktion zu machen, statt eine eigene,
        separate Session zu öffnen (Codex-Rückprüfung b8d700d: eine
        Perioden-/Zielauflösung VOR einem Schreib-Lock ist ein TOCTOU-
        Fenster - siehe `bank.service._zuordnen_atomar`)."""

        def _query(active_session: Session) -> list[OPPositionTable]:
            statement = (
                select(OPPositionTable)
                .where(OPPositionTable.konto_id == konto_id)
                .where(OPPositionTable.status == OPPositionStatus.AKTIV.value)
                .order_by(OPPositionTable.belegdatum)
            )
            return list(active_session.execute(statement).scalars().all())

        if session is not None:
            return _query(session)
        with self._session_factory() as owned_session:
            return _query(owned_session)

    def list_alle(self, konto_id: str) -> list[OPPositionTable]:
        with self._session_factory() as session:
            statement = (
                select(OPPositionTable)
                .where(OPPositionTable.konto_id == konto_id)
                .order_by(OPPositionTable.belegdatum)
            )
            return list(session.execute(statement).scalars().all())

    def get(self, op_position_id: int, *, session: Session | None = None, with_for_update: bool = False) -> OPPositionTable | None:
        if session is not None:
            return session.get(OPPositionTable, op_position_id, with_for_update=with_for_update)
        with self._session_factory() as session:
            return session.get(OPPositionTable, op_position_id)

    def storno(
        self, *, original_id: int, neue_row: OPPositionTable | None, akteur: str,
        vorgang_id: str | None = None, session: Session | None = None,
    ) -> OPPositionTable | None:
        """Atomarer Storno/Ersatz; identische Vorgänge bleiben wirkungslose Retries.

        Eine übergebene Session gehört dem Aufrufer und wird nicht committet.
        Ohne Session wird dieselbe Schreibsperre wie im Bankdienst verwendet.
        Bankgebundene oder aktiv referenzierte Positionen bleiben unverändert.
        """
        if session is not None:
            return self._storno(session, original_id, neue_row, vorgang_id)
        with schreibgesperrte_session(self._session_factory) as owned_session:
            result = self._storno(owned_session, original_id, neue_row, vorgang_id)
            owned_session.commit()
            if result is not None:
                owned_session.refresh(result)
            return result

    def _storno(
        self, session: Session, original_id: int,
        neue_row: OPPositionTable | None, vorgang_id: str | None,
    ) -> OPPositionTable | None:
        from mietinkasso.domain.exceptions import StornierungKonfliktError

        original = session.get(OPPositionTable, original_id, with_for_update=True)
        if original is None:
            raise ValueError(f"Unbekannte OPPosition {original_id}")

        if original.status == OPPositionStatus.STORNIERT.value:
            bestehender_ersatz = (
                session.get(OPPositionTable, original.storniert_durch_id)
                if original.storniert_durch_id is not None else None
            )
            if neue_row is None and bestehender_ersatz is None:
                return None
            if (
                neue_row is not None and bestehender_ersatz is not None
                and vorgang_id is not None and neue_row.import_id is not None
                and bestehender_ersatz.import_id == neue_row.import_id
                and bestehender_ersatz.quelle_hash == neue_row.quelle_hash
            ):
                return bestehender_ersatz
            raise StornierungKonfliktError(
                f"OPPosition {original_id} ist bereits storniert "
                f"(Ersatz: {original.storniert_durch_id}); eine erneute, abweichende "
                "Stornierung/Korrektur desselben Originals wird abgelehnt."
            )

        if neue_row is not None:
            if (neue_row.konto_id != original.konto_id or neue_row.typ != original.typ
                    or neue_row.bezieht_sich_auf_id != original.bezieht_sich_auf_id
                    or neue_row.bank_transaktion_id is not None):
                raise ValueError("Korrektur muss Konto, Buchungstyp und ausdrückliche Bindung erhalten.")
            pruefe_betrag_positiv(OPTyp(neue_row.typ), neue_row.betrag_cent)
            if neue_row.bezieht_sich_auf_id is not None:
                ziel = session.get(OPPositionTable, neue_row.bezieht_sich_auf_id, with_for_update=True)
                pruefe_bezug(neue_row, ziel)

        banklink = session.scalar(select(ZuordnungTable.id).where(
            ZuordnungTable.op_position_id == original_id
        ).limit(1))
        if original.bank_transaktion_id is not None or banklink is not None:
            raise ValueError(
                f"OPPosition #{original_id} ist mit der Bank verknüpft. Manuelle OP-Korrektur/Storno "
                "würde Bankzuordnungen verändern; Bankbeleg und Zuordnung müssen gemeinsam geklärt werden."
            )
        referenz = session.scalar(select(OPPositionTable.id).where(
            OPPositionTable.bezieht_sich_auf_id == original_id,
            OPPositionTable.status == OPPositionStatus.AKTIV.value,
        ).limit(1))
        if referenz is not None:
            raise ValueError(
                f"OPPosition #{original_id} wird von aktiver Position #{referenz} referenziert. "
                "Bitte zuerst die abhängige Buchung klären."
            )

        # Erst die alte Eröffnung deaktivieren, sonst verletzt der Ersatz den
        # Unique-Index. Alles bleibt in EINER Transaktion rückrollbar.
        original.status = OPPositionStatus.STORNIERT.value
        session.flush()
        if neue_row is not None:
            session.add(neue_row)
            session.flush()
            original.storniert_durch_id = neue_row.id
        session.flush()
        return neue_row
