"""Append-only Erfassung und reiner Read der Abgleich-/Einzugsnachweise.

Erfassung (nur über den privaten Operator-Intake, kein Portalformular):

- validiert Scope VOR dem Schreiben: Objekt/Vertrag bekannt, zugänglich
  (`ctx`) und nicht ausgeschlossen; das Bankkonto gehört DERSELBEN
  Gesellschaft wie Objekt (und Vertrag). Kein Ersatzkonto wird aus der
  Gesellschaft abgeleitet - fehlt die ausdrückliche Zuordnung, wird nichts
  geschrieben.
- idempotent: dieselbe `import_id` (Abgleich) bzw. `referenz` (Einzug)
  mit identischem Inhalt liefert die bestehende Zeile zurück; abweichender
  Inhalt unter derselben Kennung ist ein `ImportConflictError`. Bestehende
  Zeilen werden nie verändert (auch kein Statuswechsel).
- schreibt AUSSCHLIESSLICH in `abgleich_nachweise`/`einzug_nachweise` -
  nie in OP-, Bank-, Bankvollständigkeits- oder Mahntabellen."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.abgleichstatus.models import EINZUG_STATUS, AbgleichNachweisTable, EinzugNachweisTable, EinzugStatusNachweisTable
from mietinkasso.auth.service import AuthContext
from mietinkasso.domain.exceptions import ImportConflictError, MietinkassoError
from mietinkasso.indexautomatik.zeit import WIEN
from mietinkasso.infrastructure.db.tables import BankKontoTable, EinheitTable, ObjektTable, VertragTable

_SHA256 = re.compile(r"[0-9a-f]{64}")


class NachweisUngueltigError(MietinkassoError):
    """Nachweis verletzt Pflichtfelder oder Scope - es wurde nichts
    geschrieben."""


def _utc_naiv(zeitpunkt: datetime) -> datetime:
    """Speicherform: UTC ohne tzinfo. Ein naiver Wert wird abgelehnt
    statt einer Zeitzone unterstellt."""

    if zeitpunkt.tzinfo is None:
        raise NachweisUngueltigError("Zeitpunkt braucht eine Zeitzone (z. B. Europe/Vienna oder UTC).")
    return zeitpunkt.astimezone(timezone.utc).replace(tzinfo=None)


def gespeichert_als_utc_naiv(zeitpunkt: datetime) -> datetime:
    """Gegenstück für gelesene Zeilen (SQLite: naiv = UTC, andere DBs
    liefern ggf. tz-aware)."""

    return zeitpunkt if zeitpunkt.tzinfo is None else zeitpunkt.astimezone(timezone.utc).replace(tzinfo=None)


def _pflichttext(wert: str, feld: str) -> str:
    if not isinstance(wert, str) or not wert.strip():
        raise NachweisUngueltigError(f"Pflichtfeld '{feld}' fehlt.")
    return wert.strip()


def _scope_fehler() -> NachweisUngueltigError:
    # Bewusst EINE Meldung für unbekannt/fremd/ausgeschlossen - wie
    # `rueckstaende.service.UnbekanntesObjektFilterError`.
    return NachweisUngueltigError("Objekt/Vertrag ist unbekannt, nicht zugänglich oder ausgeschlossen.")


def _pruefe_objekt(session: Session, ctx: AuthContext, objekt_id: str) -> ObjektTable:
    objekt = session.get(ObjektTable, objekt_id)
    if objekt is None or objekt.ausgeschlossen or not ctx.has_zugriff(objekt.gesellschaft_id):
        raise _scope_fehler()
    return objekt


def _pruefe_bank(session: Session, bank_konto_id: str, gesellschaft_id: str) -> BankKontoTable:
    bank = session.get(BankKontoTable, bank_konto_id)
    if bank is None or bank.gesellschaft_id != gesellschaft_id:
        raise NachweisUngueltigError(
            "Bankkonto ist unbekannt oder gehört nicht zur Gesellschaft des Objekts - keine ersatzweise Zuordnung."
        )
    return bank


def _vergleich(row, werte: dict, zeitfeld: str) -> bool:
    for feld, wert in werte.items():
        gespeichert = getattr(row, feld)
        if feld == zeitfeld:
            gespeichert = gespeichert_als_utc_naiv(gespeichert)
        if gespeichert != wert:
            return False
    return True


class AbgleichNachweisRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    # -- Erfassung (append-only) -------------------------------------------
    def erfasse_abgleichnachweis(
        self,
        *,
        ctx: AuthContext,
        objekt_id: str,
        bank_konto_id: str,
        geprueft_von: date,
        geprueft_bis: date,
        geprueft_am: datetime,
        geprueft_durch: str,
        umfang: str,
        quelle_ref: str,
        quelle_sha256: str,
        import_id: str,
    ) -> AbgleichNachweisTable:
        if not ctx.kann_schreiben():
            raise NachweisUngueltigError("Keine Schreibberechtigung.")
        if geprueft_von > geprueft_bis:
            raise NachweisUngueltigError("Prüfzeitraum: 'geprüft von' liegt nach 'geprüft bis'.")
        geprueft_am_utc = _utc_naiv(geprueft_am)
        if geprueft_bis > geprueft_am.astimezone(WIEN).date():
            raise NachweisUngueltigError("Prüfzeitraum endet nach dem Prüfzeitpunkt - künftige Eingänge sind nicht geprüft.")
        if not isinstance(quelle_sha256, str) or not _SHA256.fullmatch(quelle_sha256):
            raise NachweisUngueltigError("quelle_sha256 muss 64 Hex-Zeichen (klein) enthalten.")
        werte = dict(
            objekt_id=objekt_id, bank_konto_id=bank_konto_id, geprueft_von=geprueft_von, geprueft_bis=geprueft_bis,
            geprueft_am=geprueft_am_utc, geprueft_durch=_pflichttext(geprueft_durch, "geprueft_durch"),
            umfang=_pflichttext(umfang, "umfang"), quelle_ref=_pflichttext(quelle_ref, "quelle_ref"),
            quelle_sha256=quelle_sha256, import_id=_pflichttext(import_id, "import_id"),
        )
        with self._session_factory() as session:
            objekt = _pruefe_objekt(session, ctx, objekt_id)
            _pruefe_bank(session, bank_konto_id, objekt.gesellschaft_id)
            return self._einfuegen(
                session, AbgleichNachweisTable, AbgleichNachweisTable.import_id, werte["import_id"], werte, "geprueft_am",
            )

    def erfasse_einzugnachweis(
        self,
        *,
        ctx: AuthContext,
        vertrag_id: str,
        bank_konto_id: str,
        betrag_cent: int,
        einzug_am: date,
        eingereicht_am: datetime,
        referenz: str,
        status: str,
        nachweis: str,
    ) -> EinzugNachweisTable:
        if not ctx.kann_schreiben():
            raise NachweisUngueltigError("Keine Schreibberechtigung.")
        if type(betrag_cent) is not int or betrag_cent <= 0:
            raise NachweisUngueltigError("betrag_cent muss eine positive ganze Zahl sein.")
        if status not in EINZUG_STATUS:
            raise NachweisUngueltigError(f"Unbekannter Einzugsstatus '{status}' - erlaubt: {', '.join(EINZUG_STATUS)}.")
        werte = dict(
            vertrag_id=vertrag_id, bank_konto_id=bank_konto_id, betrag_cent=betrag_cent, einzug_am=einzug_am,
            eingereicht_am=_utc_naiv(eingereicht_am), referenz=_pflichttext(referenz, "referenz"), status=status,
            nachweis=_pflichttext(nachweis, "nachweis"),
        )
        with self._session_factory() as session:
            vertrag = session.get(VertragTable, vertrag_id)
            if vertrag is None or not ctx.has_zugriff(vertrag.gesellschaft_id):
                raise _scope_fehler()
            einheit = session.get(EinheitTable, vertrag.einheit_id)
            if einheit is None:
                raise _scope_fehler()
            objekt = _pruefe_objekt(session, ctx, einheit.objekt_id)
            if objekt.gesellschaft_id != vertrag.gesellschaft_id:
                raise _scope_fehler()
            _pruefe_bank(session, bank_konto_id, objekt.gesellschaft_id)
            return self._einfuegen(
                session, EinzugNachweisTable, EinzugNachweisTable.referenz, werte["referenz"], werte, "eingereicht_am",
            )

    @staticmethod
    def _einfuegen(session: Session, tabelle, kennung_spalte, kennung: str, werte: dict, zeitfeld: str):
        def _bestehend():
            return session.execute(select(tabelle).where(kennung_spalte == kennung)).scalar_one_or_none()

        def _idempotent(row):
            if not _vergleich(row, werte, zeitfeld):
                raise ImportConflictError(f"Nachweis '{kennung}' ist bereits mit anderem Inhalt erfasst.")
            return row

        row = _bestehend()
        if row is not None:
            return _idempotent(row)
        row = tabelle(**werte)
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            # Paralleler Retry derselben Kennung: erneut vergleichen.
            session.rollback()
            row = _bestehend()
            if row is None:
                raise
            return _idempotent(row)
        session.refresh(row)
        return row

    # -- Read ----------------------------------------------------------------
    def erfasse_einzugsstatus(
        self, *, ctx: AuthContext, referenz: str, status: str, erfasst_am: datetime,
        nachweis: str, import_id: str,
    ) -> EinzugStatusNachweisTable:
        """Expliziter belegter Statuswechsel, niemals aus Saldo/Datum abgeleitet.

        Rückgaben nach bestätigtem Eingang sind neue Ereignisse. Die
        ursprüngliche Einreichung bleibt erhalten; dies bucht KEIN Geld.
        """
        if not ctx.kann_schreiben() or status not in EINZUG_STATUS:
            raise NachweisUngueltigError("Keine Schreibberechtigung oder ungültiger Status.")
        zeit = _utc_naiv(erfasst_am)
        if zeit > datetime.now(timezone.utc).replace(tzinfo=None):
            raise NachweisUngueltigError("Statusnachweis liegt in der Zukunft.")
        with self._session_factory() as session:
            einzug = session.execute(select(EinzugNachweisTable).where(EinzugNachweisTable.referenz == referenz)).scalar_one_or_none()
            if einzug is None:
                raise _scope_fehler()
            vertrag = session.get(VertragTable, einzug.vertrag_id)
            if vertrag is None or not ctx.has_zugriff(vertrag.gesellschaft_id):
                raise _scope_fehler()
            einheit = session.get(EinheitTable, vertrag.einheit_id)
            if einheit is None:
                raise _scope_fehler()
            objekt = _pruefe_objekt(session, ctx, einheit.objekt_id)
            if objekt.gesellschaft_id != vertrag.gesellschaft_id:
                raise _scope_fehler()
            _pruefe_bank(session, einzug.bank_konto_id, objekt.gesellschaft_id)
            if zeit < gespeichert_als_utc_naiv(einzug.eingereicht_am):
                raise NachweisUngueltigError("Statusnachweis liegt vor der Einreichung.")
            werte = dict(einzug_id=einzug.id, status=status, erfasst_am=zeit,
                         erfasst_durch=ctx.user_id, nachweis=_pflichttext(nachweis, "nachweis"),
                         import_id=_pflichttext(import_id, "import_id"))
            return self._einfuegen(session, EinzugStatusNachweisTable, EinzugStatusNachweisTable.import_id,
                                  werte["import_id"], werte, "erfasst_am")

    def aktuelle_einzugsstatus(self, einzug_ids: list[int]) -> dict[int, str]:
        if not einzug_ids:
            return {}
        with self._session_factory() as session:
            rows = session.scalars(select(EinzugStatusNachweisTable).where(
                EinzugStatusNachweisTable.einzug_id.in_(einzug_ids)
            ).order_by(EinzugStatusNachweisTable.erfasst_am, EinzugStatusNachweisTable.id)).all()
            return {row.einzug_id: row.status for row in rows}

    def list_abgleichnachweise(self, objekt_ids: list[str]) -> list[AbgleichNachweisTable]:
        if not objekt_ids:
            return []
        with self._session_factory() as session:
            return list(session.execute(
                select(AbgleichNachweisTable).where(AbgleichNachweisTable.objekt_id.in_(objekt_ids))
                .order_by(AbgleichNachweisTable.id)
            ).scalars().all())

    def list_einzugnachweise(self, vertrag_ids: list[str]) -> list[EinzugNachweisTable]:
        if not vertrag_ids:
            return []
        with self._session_factory() as session:
            return list(session.execute(
                select(EinzugNachweisTable).where(EinzugNachweisTable.vertrag_id.in_(vertrag_ids))
                .order_by(EinzugNachweisTable.einzug_am, EinzugNachweisTable.id)
            ).scalars().all())
