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
-OP-Position (auch bei wiederholtem Lauf) - ein ECHTER Retry (identische
Parameter) liefert die bereits bestehende Zeile zurück, statt erneut zu
buchen; ein zweiter Versuch mit ABWEICHENDEN Parametern (z. B. anderes
Bankentgelt) ist dagegen ein Konflikt und wird abgelehnt (siehe
`erfasse_und_buche`, Codex-Rückmeldung 19.10.2026 - vorher gab ein
zweiter Versuch mit abweichenden Werten still die alte Zeile zurück).

Zweischritt bei zunächst UNKLARER Zurechenbarkeit (Codex-Rückmeldung
19.10.2026): `erfasse_und_buche(dem_zahlungspflichtigen_zurechenbar=
False)` legt die Zeile dokumentarisch an (KEINE SOLL-Buchung). Kommt
SPÄTER ein Bank-/Rechtsnachweis, der die Zurechenbarkeit DOCH belegt,
übernimmt `uebernimm_zurechenbarkeit_nachtraeglich(...)` GENAU EINMAL den
bereits dokumentierten, UNVERÄNDERTEN `bankentgelt_cent` als
Erstattungsforderung - keine zweite Bankrückgabe/Kapitalbuchung, kein neu
eingegebener Betrag, keine automatische Verursachungsvermutung aus einem
bloßen Bank-Rückgabegrund-Code (z. B. SEPA-Reaktionscode "MS03" o. ä.).
Die ursprüngliche Begründung (`zurechenbarkeit_begruendung`) bleibt dabei
UNVERÄNDERT als Ursprungshistorie erhalten; die neue, separat belegte
Begründung landet in eigenen `nachtraeglich_zurechenbar_*`-Spalten."""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mietinkasso.auth.service import AuthContext, require_gesellschaft_access, require_schreibrecht
from mietinkasso.domain.enums import OPTyp
from mietinkasso.domain.exceptions import BindungInkonsistentError, VorgangIdKonfliktError
from mietinkasso.infrastructure.db.sqlite_write_lock import schreibgesperrte_session
from mietinkasso.infrastructure.db.tables import KontoTable, OPPositionTable, RuecklastschriftkostenTable
from mietinkasso.op.service import OPService


def _wiederholung_stimmt_ueberein(
    bestehend: RuecklastschriftkostenTable, *, bankentgelt_cent: int, rueckgabeursache: str,
    dem_zahlungspflichtigen_zurechenbar: bool, zurechenbarkeit_begruendung: str, beleg_referenz: str,
    bank_transaktion_id: int | None = None,
) -> bool:
    return (
        bestehend.bankentgelt_cent == bankentgelt_cent
        and bestehend.rueckgabeursache == rueckgabeursache
        and bestehend.dem_zahlungspflichtigen_zurechenbar == dem_zahlungspflichtigen_zurechenbar
        and bestehend.zurechenbarkeit_begruendung == zurechenbarkeit_begruendung
        and bestehend.beleg_referenz == beleg_referenz
        and bestehend.bank_transaktion_id == bank_transaktion_id
    )


class RuecklastschriftkostenRepository:
    def __init__(self, session_factory):
        self._session_factory = session_factory

    def fuer_ruecklastschrift(
        self, ruecklastschrift_op_position_id: int, *, with_for_update: bool = False, session: Session | None = None,
    ) -> RuecklastschriftkostenTable | None:
        def _lesen(active_session: Session) -> RuecklastschriftkostenTable | None:
            stmt = select(RuecklastschriftkostenTable).where(
                RuecklastschriftkostenTable.ruecklastschrift_op_position_id == ruecklastschrift_op_position_id
            )
            if with_for_update:
                stmt = stmt.with_for_update()
            return active_session.execute(stmt).scalar_one_or_none()

        if session is not None:
            return _lesen(session)
        with self._session_factory() as owned_session:
            return _lesen(owned_session)

    def anlegen(
        self, *, vertrag_id: str, ruecklastschrift_op_position_id: int, bankentgelt_cent: int,
        rueckgabeursache: str, dem_zahlungspflichtigen_zurechenbar: bool, zurechenbarkeit_begruendung: str,
        beleg_referenz: str, kosten_op_position_id: int | None, erstellt_von: str, session: Session,
        bank_transaktion_id: int | None = None,
    ) -> RuecklastschriftkostenTable:
        row = RuecklastschriftkostenTable(
            vertrag_id=vertrag_id, ruecklastschrift_op_position_id=ruecklastschrift_op_position_id,
            bank_transaktion_id=bank_transaktion_id,
            bankentgelt_cent=bankentgelt_cent, rueckgabeursache=rueckgabeursache,
            dem_zahlungspflichtigen_zurechenbar=dem_zahlungspflichtigen_zurechenbar,
            zurechenbarkeit_begruendung=zurechenbarkeit_begruendung, beleg_referenz=beleg_referenz,
            kosten_op_position_id=kosten_op_position_id, erstellt_von=erstellt_von,
        )
        session.add(row)
        session.flush()
        return row

    def uebernimm_zurechenbarkeit_nachtraeglich(
        self, *, row_id: int, kosten_op_position_id: int, begruendung: str, erklaert_von: str, session: Session,
    ) -> RuecklastschriftkostenTable:
        """Aktualisiert NUR die nachträglichen Zurechenbarkeits-Spalten
        und `kosten_op_position_id` - `bankentgelt_cent`/
        `rueckgabeursache`/`zurechenbarkeit_begruendung` (Ursprungs-
        historie) bleiben dabei UNVERÄNDERT."""

        row = session.get(RuecklastschriftkostenTable, row_id)
        if row is None:
            raise ValueError(f"Unbekannte Rücklastschriftkosten-Zeile {row_id}.")
        row.dem_zahlungspflichtigen_zurechenbar = True
        row.kosten_op_position_id = kosten_op_position_id
        row.nachtraeglich_zurechenbar_begruendung = begruendung
        row.nachtraeglich_zurechenbar_erklaert_von = erklaert_von
        row.nachtraeglich_zurechenbar_erklaert_am = datetime.now(timezone.utc)
        session.flush()
        return row


class RuecklastschriftkostenService:
    def __init__(
        self, *, session_factory, op_service: OPService, repository: RuecklastschriftkostenRepository | None = None,
    ):
        self._session_factory = session_factory
        self._op_service = op_service
        self._repository = repository or RuecklastschriftkostenRepository(session_factory)

    def vorhandene_erfassung(
        self, ruecklastschrift_op_position_id: int, *, session: Session | None = None,
    ) -> RuecklastschriftkostenTable | None:
        """Öffentlicher Lesezugriff auf eine ggf. bereits bestehende
        Erfassung - für Aufrufer (z. B. `bank/service.py::verarbeite_
        ruecklastschrift_mit_bankentgelt`), die VOR einer eigenen
        Split-/Verfügbarkeitsberechnung wissen müssen, ob (und mit
        welchem Bankentgelt) diese Rücklastschrift-OP bereits erfasst
        wurde, ohne dafür das `_repository`-Attribut anzufassen."""

        return self._repository.fuer_ruecklastschrift(ruecklastschrift_op_position_id, session=session)

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
        session: Session | None = None,
        bank_transaktion_id: int | None = None,
    ) -> RuecklastschriftkostenTable:
        """Erfasst das tatsächliche Bankentgelt EINER bereits verbuchten
        Rücklastschrift und bucht es - NUR bei belegter Zurechenbarkeit -
        als eigene SOLL-Position gegen den Zahlungspflichtigen. Ein
        wiederholter Aufruf für DIESELBE `ruecklastschrift_op_position`
        liefert die bereits bestehende Erfassung zurück (keine
        Doppelbuchung, siehe Moduldoc).

        `session` (Codex-Rückmeldung 19.10.2026, analog zu `op_service.
        buchen`/`bank/service.py::verarbeite_ruecklastschrift`): mit
        übergebener Session wird NICHT committet/zurückgerollt und die
        Idempotenz-/Konfliktprüfung liest ÜBER DIESELBE (bereits
        gesperrte) Session - der Aufrufer (`bank/service.py::
        verarbeite_ruecklastschrift_mit_bankentgelt`) führt diese Buchung
        als Teil einer größeren, gemeinsamen Transaktion aus. Ohne
        übergebene Session unverändert wie bisher: eigene kurzlebige
        Session, committet/rollt selbst zurück."""

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

        if session is not None:
            require_gesellschaft_access(ctx, konto.gesellschaft_id)
            require_schreibrecht(ctx)
            bestehend = self._repository.fuer_ruecklastschrift(
                ruecklastschrift_op_position.id, with_for_update=True, session=session,
            )
            if bestehend is not None:
                if _wiederholung_stimmt_ueberein(
                    bestehend, bankentgelt_cent=bankentgelt_cent, rueckgabeursache=rueckgabeursache,
                    dem_zahlungspflichtigen_zurechenbar=dem_zahlungspflichtigen_zurechenbar,
                    zurechenbarkeit_begruendung=zurechenbarkeit_begruendung, beleg_referenz=beleg_referenz,
                    bank_transaktion_id=bank_transaktion_id,
                ):
                    # KEIN `session.expunge` hier: der Aufrufer (`bank/
                    # service.py::verarbeite_ruecklastschrift_mit_
                    # bankentgelt`) hält die Transaktion und
                    # committet/refresht selbst - ein hier expungtes
                    # Objekt wäre für dessen `session.refresh(...)` nicht
                    # mehr persistent.
                    return bestehend  # echter Retry, No-Op
                raise VorgangIdKonfliktError(
                    f"Rücklastschriftkosten für OPPosition {ruecklastschrift_op_position.id} wurden bereits "
                    f"MIT ABWEICHENDEN Werten erfasst (Bankentgelt {bestehend.bankentgelt_cent}, zurechenbar "
                    f"{bestehend.dem_zahlungspflichtigen_zurechenbar}); angefordert wurden Bankentgelt "
                    f"{bankentgelt_cent}, zurechenbar {dem_zahlungspflichtigen_zurechenbar}."
                )
            return self._buchen_in_session(
                session, ctx=ctx, ruecklastschrift_op_position=ruecklastschrift_op_position, vertrag_id=vertrag_id,
                konto=konto, bankentgelt_cent=bankentgelt_cent, rueckgabeursache=rueckgabeursache,
                dem_zahlungspflichtigen_zurechenbar=dem_zahlungspflichtigen_zurechenbar,
                zurechenbarkeit_begruendung=zurechenbarkeit_begruendung, beleg_referenz=beleg_referenz,
                heute=heute, akteur=akteur, bank_transaktion_id=bank_transaktion_id,
            )

        require_gesellschaft_access(ctx, konto.gesellschaft_id)
        require_schreibrecht(ctx)

        bestehend = self._repository.fuer_ruecklastschrift(ruecklastschrift_op_position.id)
        if bestehend is not None:
            if _wiederholung_stimmt_ueberein(
                bestehend, bankentgelt_cent=bankentgelt_cent, rueckgabeursache=rueckgabeursache,
                dem_zahlungspflichtigen_zurechenbar=dem_zahlungspflichtigen_zurechenbar,
                zurechenbarkeit_begruendung=zurechenbarkeit_begruendung, beleg_referenz=beleg_referenz,
                bank_transaktion_id=bank_transaktion_id,
            ):
                return bestehend  # echter Retry, No-Op
            raise VorgangIdKonfliktError(
                f"Rücklastschriftkosten für OPPosition {ruecklastschrift_op_position.id} wurden bereits MIT "
                f"ABWEICHENDEN Werten erfasst (Bankentgelt {bestehend.bankentgelt_cent}, zurechenbar "
                f"{bestehend.dem_zahlungspflichtigen_zurechenbar}); angefordert wurden Bankentgelt "
                f"{bankentgelt_cent}, zurechenbar {dem_zahlungspflichtigen_zurechenbar}. Für eine nachträglich "
                "belegte Zurechenbarkeit siehe `uebernimm_zurechenbarkeit_nachtraeglich`, für einen echten "
                "zweiten, unabhängigen Vorgang ist keine zweite Erfassung je Rücklastschrift-OP vorgesehen."
            )

        with self._session_factory() as eigene_session:
            try:
                row = self._buchen_in_session(
                    eigene_session, ctx=ctx, ruecklastschrift_op_position=ruecklastschrift_op_position,
                    vertrag_id=vertrag_id, konto=konto, bankentgelt_cent=bankentgelt_cent,
                    rueckgabeursache=rueckgabeursache,
                    dem_zahlungspflichtigen_zurechenbar=dem_zahlungspflichtigen_zurechenbar,
                    zurechenbarkeit_begruendung=zurechenbarkeit_begruendung, beleg_referenz=beleg_referenz,
                    heute=heute, akteur=akteur, bank_transaktion_id=bank_transaktion_id,
                )
                eigene_session.commit()
                return row
            except IntegrityError:
                # Ein gleichzeitiger zweiter Versuch für DIESELBE
                # Rücklastschrift (`uq_ruecklastschriftkosten_je_rls`) MIT
                # DENSELBEN Werten - kein Fehler, sondern der Idempotenz-
                # Fall (siehe Moduldoc). Findet sich KEINE eigene Zeile
                # oder weicht sie ab, war die IntegrityError ein echter,
                # ungeklärter Konflikt.
                eigene_session.rollback()
                bestehend = self._repository.fuer_ruecklastschrift(ruecklastschrift_op_position.id)
                if bestehend is None:
                    raise RuntimeError(
                        f"Rücklastschriftkosten-Erfassung für OPPosition {ruecklastschrift_op_position.id} "
                        "ist an einer IntegrityError gescheitert, aber es existiert KEINE eigene Zeile - "
                        "manuelle Klärung erforderlich."
                    ) from None
                if not _wiederholung_stimmt_ueberein(
                    bestehend, bankentgelt_cent=bankentgelt_cent, rueckgabeursache=rueckgabeursache,
                    dem_zahlungspflichtigen_zurechenbar=dem_zahlungspflichtigen_zurechenbar,
                    zurechenbarkeit_begruendung=zurechenbarkeit_begruendung, beleg_referenz=beleg_referenz,
                    bank_transaktion_id=bank_transaktion_id,
                ):
                    raise VorgangIdKonfliktError(
                        f"Rücklastschriftkosten für OPPosition {ruecklastschrift_op_position.id} wurden "
                        "gleichzeitig MIT ABWEICHENDEN Werten erfasst."
                    ) from None
                return bestehend

    def _buchen_in_session(
        self,
        session: Session,
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
        bank_transaktion_id: int | None = None,
    ) -> RuecklastschriftkostenTable:
        kosten_position_id = None
        if dem_zahlungspflichtigen_zurechenbar:
            # KEINE `bezieht_sich_auf_id` auf die Rücklastschrift: eine
            # explizite OP-Bindung ist laut `op/validierung.py::pruefe_bezug`
            # nur für ZAHLUNG/GUTSCHRIFT (-> Forderung) und RUECKLASTSCHRIFT
            # (-> Zahlung) definiert, NICHT für SOLL. Die Rückverfolgbarkeit
            # läuft stattdessen über `RuecklastschriftkostenTable.
            # ruecklastschrift_op_position_id`/`kosten_op_position_id` (siehe
            # `anlegen` unten) sowie `import_id`/`beleg_referenz` dieser Position.
            kosten_position = self._op_service.buchen(
                ctx=ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=bankentgelt_cent,
                belegdatum=heute, buchungsdatum=heute, faelligkeit=None,
                beleg_referenz=beleg_referenz,
                aenderungsgrund="Rücklastschriftkosten - tatsächliches Originalbankentgelt",
                import_id=f"RUECKLASTSCHRIFTKOSTEN:{ruecklastschrift_op_position.id}",
                quelle_system="ruecklastschriftkosten",
                session=session,
            )
            kosten_position_id = kosten_position.id

        return self._repository.anlegen(
            vertrag_id=vertrag_id, ruecklastschrift_op_position_id=ruecklastschrift_op_position.id,
            bank_transaktion_id=bank_transaktion_id,
            bankentgelt_cent=bankentgelt_cent, rueckgabeursache=rueckgabeursache,
            dem_zahlungspflichtigen_zurechenbar=dem_zahlungspflichtigen_zurechenbar,
            zurechenbarkeit_begruendung=zurechenbarkeit_begruendung, beleg_referenz=beleg_referenz,
            kosten_op_position_id=kosten_position_id, erstellt_von=akteur, session=session,
        )

    def uebernimm_zurechenbarkeit_nachtraeglich(
        self,
        *,
        ctx: AuthContext,
        ruecklastschrift_op_position_id: int,
        konto: KontoTable,
        begruendung: str,
        heute: date,
        akteur: str,
    ) -> RuecklastschriftkostenTable:
        """Übernimmt NACHTRÄGLICH den bereits dokumentierten, UNVERÄNDERTEN
        Bankaufwand als Erstattungsforderung, wenn zuerst keine/unklare
        Zurechenbarkeit vorlag und ERST SPÄTER ein Bank-/Rechtsnachweis
        die Zurechenbarkeit belegt - siehe Moduldoc. Bucht GENAU EINMAL
        eine neue SOLL-Position für den unveränderten `bankentgelt_cent`
        (kein neu eingegebener Betrag, keine zweite Bankrückgabe/
        Kapitalbuchung).

        Codex-Rückprüfung (unabhängiges Review f68b1ae): das übergebene
        `konto`-Objekt kann veraltet sein, UND es wurde bislang nie
        geprüft, ob die über `ruecklastschrift_op_position_id` gefundene
        Kostenzeile/Rücklastschrift-OP überhaupt zu DIESEM Konto gehört -
        ein eigenes, erlaubtes Konto mit einer FREMDEN Rücklastschrift-OP-
        ID hätte deren Bankentgelt auf das falsche Konto/Vertrag gebucht
        (CrossTenant-artige Lücke). Lesen (Idempotenz-/Konfliktprüfung),
        Bindungsprüfung UND Buchung laufen deshalb jetzt - wie bei
        `bank/service.py::verarbeite_ruecklastschrift` - in EINER
        gesperrten DB-Transaktion, die Konto/Kostenzeile/Rücklastschrift-
        OP per ID frisch (`with_for_update`) lädt und ausschließlich auf
        Basis dieser frischen Zeilen validiert; das schließt auch das
        TOCTOU-Fenster, in dem zwei konkurrierende, ABWEICHENDE
        nachträgliche Begründungen sich sonst hätten überschreiben
        können."""

        if not begruendung.strip():
            raise ValueError("Begründung für die nachträgliche Zurechenbarkeit muss belegt angegeben werden.")

        konto_id = konto.id

        with schreibgesperrte_session(self._session_factory) as session:
            try:
                frisches_konto = session.get(KontoTable, konto_id)
                if frisches_konto is None:
                    raise ValueError(f"Unbekanntes Konto {konto_id}")
                require_gesellschaft_access(ctx, frisches_konto.gesellschaft_id)
                require_schreibrecht(ctx)

                bestehend = self._repository.fuer_ruecklastschrift(
                    ruecklastschrift_op_position_id, with_for_update=True, session=session,
                )
                if bestehend is None:
                    raise ValueError(
                        f"Für Rücklastschrift-OP {ruecklastschrift_op_position_id} liegt noch keine "
                        "Bankentgelt-Erfassung vor; eine nachträgliche Zurechenbarkeit setzt eine "
                        "bestehende, dokumentarische Erfassung voraus."
                    )

                # Bindungsprüfung: die gefundene Kostenzeile muss TATSÄCHLICH
                # zu DIESEM Konto gehören - nie allein auf Basis der
                # (vom Aufrufer kontrollierten) `ruecklastschrift_op_
                # position_id` vertrauen.
                frische_rls_op = session.get(
                    OPPositionTable, bestehend.ruecklastschrift_op_position_id, with_for_update=True,
                )
                if frische_rls_op is None or frische_rls_op.konto_id != frisches_konto.id:
                    raise BindungInkonsistentError(
                        f"Rücklastschriftkosten-Zeile {bestehend.id} (Rücklastschrift-OP "
                        f"{bestehend.ruecklastschrift_op_position_id}) gehört nicht zu Konto {konto_id}."
                    )

                if bestehend.dem_zahlungspflichtigen_zurechenbar:
                    if bestehend.nachtraeglich_zurechenbar_begruendung == begruendung:
                        session.expunge(bestehend)
                        return bestehend  # echter Retry, No-Op
                    raise VorgangIdKonfliktError(
                        f"Rücklastschriftkosten für OP {ruecklastschrift_op_position_id} sind bereits "
                        "zurechenbar (entweder von Anfang an oder bereits nachträglich übernommen, mit "
                        "einer ABWEICHENDEN Begründung) - eine zweite SOLL-Buchung für denselben Aufwand "
                        "ist nicht zulässig."
                    )

                kosten_position = self._op_service.buchen(
                    ctx=ctx, konto=frisches_konto, typ=OPTyp.SOLL, betrag_cent=bestehend.bankentgelt_cent,
                    belegdatum=heute, buchungsdatum=heute, faelligkeit=None,
                    beleg_referenz=bestehend.beleg_referenz,
                    aenderungsgrund="Rücklastschriftkosten - nachträglich übernommene Zurechenbarkeit",
                    import_id=f"RUECKLASTSCHRIFTKOSTEN-NACHTRAEGLICH:{bestehend.id}",
                    quelle_system="ruecklastschriftkosten",
                    session=session,
                )
                aktualisiert = self._repository.uebernimm_zurechenbarkeit_nachtraeglich(
                    row_id=bestehend.id, kosten_op_position_id=kosten_position.id,
                    begruendung=begruendung, erklaert_von=akteur, session=session,
                )
                session.commit()
            except Exception:
                session.rollback()
                raise
            session.refresh(aktualisiert)
            return aktualisiert
