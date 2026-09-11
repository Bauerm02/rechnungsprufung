from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.domain.exceptions import (
    CrossTenantError,
    FremdwaehrungNichtUnterstuetztError,
    ImportConflictError,
    VorgangIdKonfliktError,
    ZuordnungUngueltigError,
)
from mietinkasso.infrastructure.db.tables import (
    BankKontoTable,
    BankTransaktionTable,
    KontoTable,
    OPPositionTable,
    ZuordnungTable,
)


def _vorgang_stimmt_ueberein(
    bestehende_zuordnung: ZuordnungTable, bank_transaktion_id: int, op_position_id: int, betrag_cent: int
) -> bool:
    """True nur, wenn die bereits unter dieser `vorgang_id` gespeicherte
    Zuordnung EXAKT dieselbe Operation ist (echter Retry) - nicht bloß
    zufällig irgendeine Zuordnung mit demselben Vorgangs-Label."""

    return (
        bestehende_zuordnung.bank_transaktion_id == bank_transaktion_id
        and bestehende_zuordnung.op_position_id == op_position_id
        and bestehende_zuordnung.betrag_cent == betrag_cent
    )


class BankRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    @property
    def session_factory(self) -> sessionmaker[Session]:
        return self._session_factory

    def upsert_bank_konto(self, *, id: str, gesellschaft_id: str, iban: str, bezeichnung: str) -> None:
        with self._session_factory() as session:
            row = session.get(BankKontoTable, id)
            if row is None:
                session.add(BankKontoTable(id=id, gesellschaft_id=gesellschaft_id, iban=iban, bezeichnung=bezeichnung))
            else:
                row.gesellschaft_id = gesellschaft_id
                row.iban = iban
                row.bezeichnung = bezeichnung
            session.commit()

    def get_bank_konto(self, id: str) -> BankKontoTable | None:
        with self._session_factory() as session:
            return session.get(BankKontoTable, id)

    def insert_transaktion_idempotent(
        self, row: BankTransaktionTable, *, session: Session | None = None
    ) -> BankTransaktionTable:
        """Wie `OPRepository.insert_idempotent`: mit `session` wird dieser
        Insert Teil einer größeren, vom Aufrufer verwalteten Transaktion
        (flush statt commit); ohne `session` verwaltet diese Methode ihre
        eigene Transaktion wie bisher."""

        if session is not None:
            return self._insert_transaktion(session, row)
        with self._session_factory() as owned_session:
            result = self._insert_transaktion(owned_session, row)
            owned_session.commit()
            owned_session.refresh(result)
            return result

    def _insert_transaktion(self, session: Session, row: BankTransaktionTable) -> BankTransaktionTable:
        existing = session.execute(
            select(BankTransaktionTable).where(BankTransaktionTable.import_id == row.import_id)
        ).scalar_one_or_none()
        if existing is not None:
            if existing.quelle_hash != row.quelle_hash:
                raise ImportConflictError(
                    f"Banktransaktion import_id '{row.import_id}' bereits mit anderem Inhalt vorhanden."
                )
            return existing
        session.add(row)
        session.flush()
        return row

    def find_by_fingerprint(
        self, bank_konto_id: str, fingerprint_hash: str, *, session: Session | None = None
    ) -> BankTransaktionTable | None:
        """Nur für Zeilen OHNE bankseitig eindeutige Kennung (hat_native_id
        =False) relevant: findet eine bereits importierte Zeile mit
        identischem wirtschaftlichem Fingerabdruck auf demselben Bankkonto.
        Mit einer übergebenen `session` sieht diese Abfrage (dank Autoflush)
        auch noch nicht committete Geschwisterzeilen DESSELBEN Dateiimports -
        wichtig, um Dubletten INNERHALB einer einzigen Importdatei zu
        erkennen, nicht nur gegenüber früheren, bereits committeten
        Importen."""

        def _query(active_session: Session) -> BankTransaktionTable | None:
            return active_session.execute(
                select(BankTransaktionTable)
                .where(BankTransaktionTable.bank_konto_id == bank_konto_id)
                .where(BankTransaktionTable.fingerprint_hash == fingerprint_hash)
                .where(BankTransaktionTable.hat_native_id.is_(False))
            ).scalars().first()

        if session is not None:
            return _query(session)
        with self._session_factory() as owned_session:
            return _query(owned_session)

    def list_unzugeordnet(self, bank_konto_id: str) -> list[BankTransaktionTable]:
        with self._session_factory() as session:
            zugeordnete_ids = select(ZuordnungTable.bank_transaktion_id)
            statement = (
                select(BankTransaktionTable)
                .where(BankTransaktionTable.bank_konto_id == bank_konto_id)
                .where(BankTransaktionTable.id.not_in(zugeordnete_ids))
            )
            return list(session.execute(statement).scalars().all())

    def get_transaktion(self, id: int) -> BankTransaktionTable | None:
        with self._session_factory() as session:
            return session.get(BankTransaktionTable, id)

    def letztes_buchungsdatum(self, bank_konto_id: str) -> date | None:
        with self._session_factory() as session:
            return session.execute(
                select(func.max(BankTransaktionTable.buchungsdatum)).where(
                    BankTransaktionTable.bank_konto_id == bank_konto_id
                )
            ).scalar_one_or_none()

    def zugeordneter_betrag(self, bank_transaktion_id: int, *, session: Session | None = None) -> int:
        def _query(active_session: Session) -> int:
            summe = active_session.execute(
                select(func.coalesce(func.sum(ZuordnungTable.betrag_cent), 0)).where(
                    ZuordnungTable.bank_transaktion_id == bank_transaktion_id
                )
            ).scalar_one()
            return int(summe)

        if session is not None:
            return _query(session)
        with self._session_factory() as owned_session:
            return _query(owned_session)

    def verwendeter_betrag_rueckbuchung(self, bank_transaktion_id: int, *, session: Session | None = None) -> int:
        """Summe der bereits als RUECKLASTSCHRIFT verbuchten Beträge, die
        sich auf diese (negative) Transaktion berufen - begrenzt, wie viel
        von ihrem eigenen Betrag noch für weitere Rückbuchungen verfügbar
        ist (eine Sammel-Rücklastschrift kann mehrere Ursprungszahlungen
        abdecken, aber nie mehr als ihren eigenen Betrag)."""

        def _query(active_session: Session) -> int:
            summe = active_session.execute(
                select(func.coalesce(func.sum(OPPositionTable.betrag_cent), 0))
                .where(OPPositionTable.bank_transaktion_id == bank_transaktion_id)
                .where(OPPositionTable.typ == "RUECKLASTSCHRIFT")
                .where(OPPositionTable.status == "AKTIV")
            ).scalar_one()
            return int(summe)

        if session is not None:
            return _query(session)
        with self._session_factory() as owned_session:
            return _query(owned_session)

    def kumulativ_zurueckgebucht(self, original_op_position_id: int, *, session: Session | None = None) -> int:
        """Summe aller bereits gebuchten RUECKLASTSCHRIFT-Zeilen, die sich
        auf diese ursprüngliche Zahlung beziehen - die kumulative
        Rückbuchungsgrenze (nie mehr zurückbuchen als ursprünglich bezahlt
        wurde, auch nicht über mehrere Teil-Rücklastschriften hinweg)."""

        def _query(active_session: Session) -> int:
            summe = active_session.execute(
                select(func.coalesce(func.sum(OPPositionTable.betrag_cent), 0))
                .where(OPPositionTable.bezieht_sich_auf_id == original_op_position_id)
                .where(OPPositionTable.typ == "RUECKLASTSCHRIFT")
                .where(OPPositionTable.status == "AKTIV")
            ).scalar_one()
            return int(summe)

        if session is not None:
            return _query(session)
        with self._session_factory() as owned_session:
            return _query(owned_session)

    def create_zuordnung(
        self,
        *,
        bank_transaktion_id: int,
        op_position_id: int,
        betrag_cent: int,
        match_typ: str,
        vorgang_id: str,
        session: Session | None = None,
    ) -> ZuordnungTable:
        """Validiert UND erstellt die Zuordnung in einer einzigen
        DB-Transaktion (frisch gelesene Zeilen, nicht die vom Aufrufer
        übergebenen Objekte):

        - Gesellschaft von Banktransaktion und Zielkonto muss übereinstimmen.
        - Zuordnungsbetrag muss positiv sein.
        - Währung von Transaktion und Konto muss übereinstimmen.
        - Zuordnungsbetrag darf den noch NICHT zugeordneten Restbetrag der
          Transaktion nicht überschreiten (Teilzuordnungen bleiben mit
          ihrem Rest sichtbar unzugeordnet).
        - Idempotenz hängt an `vorgang_id`: derselbe Vorgang (Retry) ist ein
          No-Op: JEDE andere `vorgang_id` erzeugt eine NEUE Zuordnung, auch
          wenn Transaktion/OP/Betrag zufällig identisch zu einer
          bestehenden Zuordnung sind (zwei echte, unabhängige
          Teilzuordnungen mit gleichem Betrag müssen möglich bleiben).

        Mit einer übergebenen `session` wird NICHT committet - der Aufrufer
        (z. B. `BankImportService`) führt Buchung, Zuordnung und Audit als
        eine gemeinsame Transaktion mit gemeinsamem Rollback aus.
        """

        if session is not None:
            return self._create_zuordnung(session, bank_transaktion_id, op_position_id, betrag_cent, match_typ, vorgang_id)
        with self._session_factory() as owned_session:
            try:
                zuordnung = self._create_zuordnung(
                    owned_session, bank_transaktion_id, op_position_id, betrag_cent, match_typ, vorgang_id
                )
                owned_session.commit()
            except IntegrityError:
                owned_session.rollback()
                # Echte Nebenläufigkeit: zwei Prozesse haben den obigen
                # Vorab-Check gleichzeitig passiert. Auch hier gilt derselbe
                # Vergleich wie im Vorab-Check - ein Konflikt darf nicht
                # unbemerkt als "es existiert ja schon etwas" durchgehen.
                bestehende = owned_session.execute(
                    select(ZuordnungTable).where(ZuordnungTable.vorgang_id == vorgang_id)
                ).scalar_one_or_none()
                if bestehende is None:
                    raise
                if not _vorgang_stimmt_ueberein(bestehende, bank_transaktion_id, op_position_id, betrag_cent):
                    raise VorgangIdKonfliktError(
                        f"vorgang_id '{vorgang_id}' wurde bereits für eine ANDERE Zuordnung verwendet "
                        f"(Transaktion {bestehende.bank_transaktion_id}, OP {bestehende.op_position_id}, "
                        f"Betrag {bestehende.betrag_cent}); angefordert wurde Transaktion "
                        f"{bank_transaktion_id}, OP {op_position_id}, Betrag {betrag_cent}. Für eine tatsächlich "
                        "neue, unabhängige Zuordnung muss eine NEUE, eindeutige vorgang_id vergeben werden."
                    )
                return bestehende
            owned_session.refresh(zuordnung)
            return zuordnung

    def _create_zuordnung(
        self,
        session: Session,
        bank_transaktion_id: int,
        op_position_id: int,
        betrag_cent: int,
        match_typ: str,
        vorgang_id: str,
    ) -> ZuordnungTable:
        bestehender_vorgang = session.execute(
            select(ZuordnungTable).where(ZuordnungTable.vorgang_id == vorgang_id)
        ).scalar_one_or_none()
        if bestehender_vorgang is not None:
            if _vorgang_stimmt_ueberein(bestehender_vorgang, bank_transaktion_id, op_position_id, betrag_cent):
                return bestehender_vorgang  # Retry DESSELBEN Vorgangs -> No-Op
            # Dieselbe vorgang_id wurde für eine ANDERE Transaktion/OP/Betrag
            # verwendet - das ist kein Retry, sondern eine Verwechslung/ein
            # Konflikt. Ohne diesen Vergleich würde die alte Zuordnung
            # unverändert zurückgegeben, während ein zuvor in DERSELBEN
            # Transaktion bereits gebuchter neuer OP (siehe
            # `BankImportService._zuordnen_atomar`) stehen bliebe - der Fehler
            # muss die GESAMTE aufrufende Transaktion zum Rollback bringen.
            raise VorgangIdKonfliktError(
                f"vorgang_id '{vorgang_id}' wurde bereits für eine ANDERE Zuordnung verwendet "
                f"(Transaktion {bestehender_vorgang.bank_transaktion_id}, OP {bestehender_vorgang.op_position_id}, "
                f"Betrag {bestehender_vorgang.betrag_cent}); angefordert wurde Transaktion {bank_transaktion_id}, "
                f"OP {op_position_id}, Betrag {betrag_cent}. Für eine tatsächlich neue, unabhängige Zuordnung "
                "muss eine NEUE, eindeutige vorgang_id vergeben werden."
            )

        transaktion = session.get(BankTransaktionTable, bank_transaktion_id)
        if transaktion is None:
            raise ValueError(f"Unbekannte Banktransaktion {bank_transaktion_id}")
        op_position = session.get(OPPositionTable, op_position_id)
        if op_position is None:
            raise ValueError(f"Unbekannte OPPosition {op_position_id}")

        bank_konto = session.get(BankKontoTable, transaktion.bank_konto_id)
        ziel_konto = session.get(KontoTable, op_position.konto_id)
        if bank_konto is None or ziel_konto is None:
            raise ValueError("Bank- oder Zielkonto nicht auffindbar.")
        if bank_konto.gesellschaft_id != ziel_konto.gesellschaft_id:
            raise CrossTenantError(
                f"Banktransaktion (Gesellschaft {bank_konto.gesellschaft_id}) darf nicht mit Konto "
                f"{ziel_konto.id} (Gesellschaft {ziel_konto.gesellschaft_id}) verrechnet werden."
            )

        if betrag_cent <= 0:
            raise ZuordnungUngueltigError("Zuordnungsbetrag muss positiv sein.")
        if transaktion.betrag_cent <= 0:
            raise ZuordnungUngueltigError(
                "Nur Zahlungseingänge (positiver Transaktionsbetrag) können regulär zugeordnet werden; "
                "für Rücklastschriften siehe bank.service.verarbeite_ruecklastschrift."
            )
        if transaktion.waehrung != ziel_konto.waehrung:
            raise FremdwaehrungNichtUnterstuetztError(
                f"Transaktion {bank_transaktion_id} ({transaktion.waehrung}) und Konto {ziel_konto.id} "
                f"({ziel_konto.waehrung}) haben unterschiedliche Währungen."
            )

        bereits_zugeordnet = session.execute(
            select(func.coalesce(func.sum(ZuordnungTable.betrag_cent), 0)).where(
                ZuordnungTable.bank_transaktion_id == bank_transaktion_id
            )
        ).scalar_one()
        verbleibend = transaktion.betrag_cent - bereits_zugeordnet
        if betrag_cent > verbleibend:
            raise ZuordnungUngueltigError(
                f"Zuordnungsbetrag {betrag_cent} überschreitet den verbleibenden, noch nicht zugeordneten "
                f"Betrag der Transaktion {bank_transaktion_id} ({verbleibend} von {transaktion.betrag_cent})."
            )

        zuordnung = ZuordnungTable(
            bank_transaktion_id=bank_transaktion_id,
            op_position_id=op_position_id,
            betrag_cent=betrag_cent,
            match_typ=match_typ,
            vorgang_id=vorgang_id,
        )
        session.add(zuordnung)
        session.flush()
        return zuordnung

    def list_zuordnungen(self, bank_transaktion_id: int) -> list[ZuordnungTable]:
        with self._session_factory() as session:
            return list(
                session.execute(
                    select(ZuordnungTable).where(ZuordnungTable.bank_transaktion_id == bank_transaktion_id)
                )
                .scalars()
                .all()
            )

    def list_zuordnungen_fuer_op(self, op_position_id: int) -> list[ZuordnungTable]:
        with self._session_factory() as session:
            return list(
                session.execute(
                    select(ZuordnungTable).where(ZuordnungTable.op_position_id == op_position_id)
                )
                .scalars()
                .all()
            )

    def bestaetige_bankvollstaendigkeit(self, *, bank_konto_id: str, bestaetigt_bis: date, bestaetigt_von: str) -> None:
        from mietinkasso.infrastructure.db.tables import BankVollstaendigkeitTable

        with self._session_factory() as session:
            session.add(
                BankVollstaendigkeitTable(
                    bank_konto_id=bank_konto_id, bestaetigt_bis=bestaetigt_bis, bestaetigt_von=bestaetigt_von
                )
            )
            session.commit()

    def letzte_bankvollstaendigkeit(self, bank_konto_id: str) -> date | None:
        from mietinkasso.infrastructure.db.tables import BankVollstaendigkeitTable

        with self._session_factory() as session:
            return session.execute(
                select(func.max(BankVollstaendigkeitTable.bestaetigt_bis)).where(
                    BankVollstaendigkeitTable.bank_konto_id == bank_konto_id
                )
            ).scalar_one_or_none()

    def hat_ungeklaerte_relevante_eingaenge(self, *, bank_konto_id: str, vertrag_id: str) -> bool:
        """True, wenn eine positive Banktransaktion auf diesem Bankkonto
        existiert, deren Referenz EXPLIZIT auf `vertrag_id` verweist
        (VERTRAG:<id>), aber noch einen unzugeordneten Restbetrag > 0 hat -
        z. B. weil der Automatch aus einem anderen Grund verweigert wurde
        oder nur teilweise manuell zugeordnet wurde. Solange das offen ist,
        darf für diesen Vertrag nicht automatisch gemahnt werden."""

        import re

        muster = re.compile(rf"VERTRAG:{re.escape(vertrag_id)}(\b|$)")
        with self._session_factory() as session:
            transaktionen = session.execute(
                select(BankTransaktionTable)
                .where(BankTransaktionTable.bank_konto_id == bank_konto_id)
                .where(BankTransaktionTable.betrag_cent > 0)
            ).scalars().all()
            for transaktion in transaktionen:
                if not transaktion.referenz or not muster.search(transaktion.referenz):
                    continue
                zugeordnet = self.zugeordneter_betrag(transaktion.id)
                if transaktion.betrag_cent - zugeordnet > 0:
                    return True
        return False
