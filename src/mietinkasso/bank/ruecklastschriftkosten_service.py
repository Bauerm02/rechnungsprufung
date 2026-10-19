"""Auftrag HV-20261019-MAHNKOSTEN: Weiterverrechnung des TATSÄCHLICHEN
Bankentgelts für eine bereits verbuchte Rücklastschrift (`OPTyp.
RUECKLASTSCHRIFT`, siehe `bank/service.py::verarbeite_ruecklastschrift`)
- STRUKTURELL GETRENNT von der Rückbuchung selbst: `verarbeite_
ruecklastschrift` bucht NUR die zurückgebuchte Zahlung als eigene
RUECKLASTSCHRIFT-Zeile und leitet daraus NIE automatisch ein Bankentgelt
ab oder schätzt eines (Auftrag Markus: "Rücklastschriftkosten getrennt:
nur Originalbankentgelt, Rückgabeursache/Zurechenbarkeit und keine
Doppelbuchung").

Fachregel: ausschließlich das tatsächlich von der Bank verrechnete,
belegte Entgelt (kein geschätzter/pauschaler Betrag) - und NUR bei
belegter Zurechenbarkeit zum Zahlungspflichtigen wird daraus eine
Forderung gebucht; ohne Zurechenbarkeit bleibt die Erfassung rein
dokumentarisch (`kosten_op_position_id` bleibt `None`, keine SOLL-Zeile).

`RuecklastschriftkostenTable.uq_ruecklastschriftkosten_je_rls` ist das
fachliche Gate gegen eine doppelte Erfassung für DIESELBE Rücklastschrift
-OP-Position (auch bei wiederholtem Lauf) - ein zweiter Versuch für
dieselbe Rücklastschrift liefert die bereits bestehende Zeile zurück,
statt erneut zu buchen."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mietinkasso.auth.service import AuthContext, require_gesellschaft_access, require_schreibrecht
from mietinkasso.domain.enums import OPTyp
from mietinkasso.infrastructure.db.tables import KontoTable, OPPositionTable, RuecklastschriftkostenTable
from mietinkasso.op.service import OPService


class RuecklastschriftkostenRepository:
    def __init__(self, session_factory):
        self._session_factory = session_factory

    def fuer_ruecklastschrift(
        self, ruecklastschrift_op_position_id: int, *, session: Session | None = None,
    ) -> RuecklastschriftkostenTable | None:
        def _lesen(active_session: Session) -> RuecklastschriftkostenTable | None:
            return active_session.execute(
                select(RuecklastschriftkostenTable).where(
                    RuecklastschriftkostenTable.ruecklastschrift_op_position_id == ruecklastschrift_op_position_id
                )
            ).scalar_one_or_none()

        if session is not None:
            return _lesen(session)
        with self._session_factory() as owned_session:
            return _lesen(owned_session)

    def anlegen(
        self, *, vertrag_id: str, ruecklastschrift_op_position_id: int, bankentgelt_cent: int,
        rueckgabeursache: str, dem_zahlungspflichtigen_zurechenbar: bool, zurechenbarkeit_begruendung: str,
        beleg_referenz: str, kosten_op_position_id: int | None, erstellt_von: str, session: Session,
    ) -> RuecklastschriftkostenTable:
        row = RuecklastschriftkostenTable(
            vertrag_id=vertrag_id, ruecklastschrift_op_position_id=ruecklastschrift_op_position_id,
            bankentgelt_cent=bankentgelt_cent, rueckgabeursache=rueckgabeursache,
            dem_zahlungspflichtigen_zurechenbar=dem_zahlungspflichtigen_zurechenbar,
            zurechenbarkeit_begruendung=zurechenbarkeit_begruendung, beleg_referenz=beleg_referenz,
            kosten_op_position_id=kosten_op_position_id, erstellt_von=erstellt_von,
        )
        session.add(row)
        session.flush()
        return row


class RuecklastschriftkostenService:
    def __init__(
        self, *, session_factory, op_service: OPService, repository: RuecklastschriftkostenRepository | None = None,
    ):
        self._session_factory = session_factory
        self._op_service = op_service
        self._repository = repository or RuecklastschriftkostenRepository(session_factory)

    def erfasse_und_buche(
        self,
        *,
        ctx: AuthContext,
        ruecklastschrift_op_position: OPPositionTable,
        vertrag_id: str,
        konto: KontoTable,
        bankentgelt_cent: int,
        rueckgabeursache: str,
        dem_zahlungspflichtigen_zurechenbar: bool,
        zurechenbarkeit_begruendung: str,
        beleg_referenz: str,
        heute: date,
        akteur: str,
    ) -> RuecklastschriftkostenTable:
        """Erfasst das tatsächliche Bankentgelt EINER bereits verbuchten
        Rücklastschrift und bucht es - NUR bei belegter Zurechenbarkeit -
        als eigene SOLL-Position gegen den Zahlungspflichtigen. Ein
        wiederholter Aufruf für DIESELBE `ruecklastschrift_op_position`
        liefert die bereits bestehende Erfassung zurück (keine
        Doppelbuchung, siehe Moduldoc)."""

        require_gesellschaft_access(ctx, konto.gesellschaft_id)
        require_schreibrecht(ctx)

        if OPTyp(ruecklastschrift_op_position.typ) is not OPTyp.RUECKLASTSCHRIFT:
            raise ValueError(
                f"OPPosition {ruecklastschrift_op_position.id} ist vom Typ "
                f"{ruecklastschrift_op_position.typ}, keine Rücklastschrift."
            )
        if bankentgelt_cent <= 0:
            raise ValueError("Bankentgelt muss belegt und positiv sein.")
        if not rueckgabeursache.strip():
            raise ValueError("Rückgabeursache muss belegt angegeben werden.")
        if not zurechenbarkeit_begruendung.strip():
            raise ValueError("Zurechenbarkeit (Verschulden) muss fallbezogen begründet werden.")

        bestehend = self._repository.fuer_ruecklastschrift(ruecklastschrift_op_position.id)
        if bestehend is not None:
            return bestehend

        with self._session_factory() as session:
            try:
                kosten_position_id = None
                if dem_zahlungspflichtigen_zurechenbar:
                    kosten_position = self._op_service.buchen(
                        ctx=ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=bankentgelt_cent,
                        belegdatum=heute, buchungsdatum=heute, faelligkeit=None,
                        beleg_referenz=beleg_referenz,
                        aenderungsgrund="Rücklastschriftkosten - tatsächliches Originalbankentgelt",
                        import_id=f"RUECKLASTSCHRIFTKOSTEN:{ruecklastschrift_op_position.id}",
                        quelle_system="ruecklastschriftkosten",
                        bezieht_sich_auf_id=ruecklastschrift_op_position.id,
                        session=session,
                    )
                    kosten_position_id = kosten_position.id

                row = self._repository.anlegen(
                    vertrag_id=vertrag_id, ruecklastschrift_op_position_id=ruecklastschrift_op_position.id,
                    bankentgelt_cent=bankentgelt_cent, rueckgabeursache=rueckgabeursache,
                    dem_zahlungspflichtigen_zurechenbar=dem_zahlungspflichtigen_zurechenbar,
                    zurechenbarkeit_begruendung=zurechenbarkeit_begruendung, beleg_referenz=beleg_referenz,
                    kosten_op_position_id=kosten_position_id, erstellt_von=akteur, session=session,
                )
                session.commit()
                return row
            except IntegrityError:
                # Ein gleichzeitiger zweiter Versuch für DIESELBE
                # Rücklastschrift (`uq_ruecklastschriftkosten_je_rls`) -
                # kein Fehler, sondern der Idempotenz-Fall (siehe
                # Moduldoc). Findet sich KEINE eigene Zeile, war die
                # IntegrityError ein echter, ungeklärter Konflikt.
                session.rollback()
                bestehend = self._repository.fuer_ruecklastschrift(ruecklastschrift_op_position.id)
                if bestehend is None:
                    raise RuntimeError(
                        f"Rücklastschriftkosten-Erfassung für OPPosition {ruecklastschrift_op_position.id} "
                        "ist an einer IntegrityError gescheitert, aber es existiert KEINE eigene Zeile - "
                        "manuelle Klärung erforderlich."
                    ) from None
                return bestehend
