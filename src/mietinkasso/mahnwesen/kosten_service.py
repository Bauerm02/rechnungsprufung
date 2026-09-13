"""Orchestriert Mahnkosten-Vorschau und -Buchung (Auftrag Markus
13.09.2026) - siehe `kosten.py` für die reine Berechnung und
`kosten_repository.py` für die Persistenz.

`buche_bei_versand` ist die EINZIGE Stelle, die tatsächlich eine
Zinsen-/Gebühren-OP-Position bucht, und wird AUSSCHLIESSLICH von
`mahnwesen/service.py::MahnwesenService.versenden()` unmittelbar NACH
einem durch `versand_belegen` bestätigten echten Versandnachweis
aufgerufen - eine bloße Vorschau, ein fehlgeschlagener Versand oder ein
Retry ohne neuen Nachweis bucht NICHTS (siehe dort). Ein wiederholter
Aufruf am selben Tag oder eine Stufe 2, die dieselben, bei Stufe 1
bereits fakturierten Zinstage beträfe, ist idempotent - nicht über eine
Existenzabfrage, sondern weil das vertragsweite Delta aus `vorschau()`
dann natürlich <= 0 ist (siehe `kosten_repository.py::
bereits_gebuchte_zinsen_cent`). Die DB-Unique-Constraint auf
`(vertrag_id, stufe, zins_bis)` ist nur ein Race-Condition-
Sicherheitsnetz für zwei gleichzeitige Aufrufe."""

from __future__ import annotations

from datetime import date

from sqlalchemy.exc import IntegrityError

from mietinkasso.auth.service import AuthContext, require_gesellschaft_access
from mietinkasso.domain.enums import OPTyp
from mietinkasso.mahnwesen.kosten import MahnkostenVorschau, berechne_mahnkosten_vorschau
from mietinkasso.mahnwesen.kosten_repository import MahnkostenRepository
from mietinkasso.op.service import OPService, compute_content_hash
from mietinkasso.stammdaten.repository import StammdatenRepository


def _mahnlauf_schluessel(op_position_ids: list[int]) -> str:
    return compute_content_hash({"forderung_ids": sorted(op_position_ids)})


class MahnkostenService:
    def __init__(
        self, repository: MahnkostenRepository, op_service: OPService, stammdaten_repository: StammdatenRepository,
    ):
        self._repository = repository
        self._op_service = op_service
        self._stammdaten_repository = stammdaten_repository

    def vorschau(self, *, vertrag_id: str, stufe: int, heute: date) -> MahnkostenVorschau | None:
        konto = self._stammdaten_repository.get_konto_by_vertrag(vertrag_id)
        if konto is None:
            return None
        forderungen = self._op_service.offene_forderungen(konto.id, heute=heute)
        alle_positionen = self._op_service.berechne_saldo(konto.id, stichtag=heute).positionen
        zinsprofil = self._repository.geprueftes_zinsprofil(vertrag_id)
        # Vertragsweit (NICHT je Stufe) - Stufe 2 rechnet dieselben, bei
        # Stufe 1 bereits fakturierten Zinstage nie erneut ab (siehe
        # `kosten_repository.py::bereits_gebuchte_zinsen_cent`-Docstring).
        bereits_gebuchte_zinsen = self._repository.bereits_gebuchte_zinsen_cent(vertrag_id=vertrag_id)
        gebuehr_bereits_gebucht = self._repository.gebuehr_bereits_gebucht(vertrag_id=vertrag_id)
        return berechne_mahnkosten_vorschau(
            vertrag_id=vertrag_id, stufe=stufe, forderungen=forderungen, alle_positionen=alle_positionen,
            heute=heute, zinsprofil=zinsprofil, basiszinssatz_lookup=self._repository.basiszinssatz_fuer_datum,
            bereits_gebuchte_zinsen_cent=bereits_gebuchte_zinsen, gebuehr_bereits_gebucht=gebuehr_bereits_gebucht,
        )

    def buche_bei_versand(
        self, *, ctx: AuthContext, vertrag_id: str, stufe: int, heute: date,
        versandnachweis_referenz: str, akteur: str,
    ):
        """Bucht NUR das Delta (neue Zinsen seit der zuletzt für DIESEN
        VERTRAG - über alle Stufen hinweg - gebuchten Zinssumme, plus eine
        ggf. noch nicht gebuchte Gebühr) - NIE die volle
        `neue_zinsen_cent`-Summe erneut, sonst würden bereits fakturierte
        Zinstage bei einem zweiten Mahnlauf/einer zweiten Stufe doppelt
        angesetzt. Gibt `None` zurück, wenn nichts zu buchen ist (kein
        Konto, keine offene Forderung, kein geprüftes Zinsprofil, oder das
        Delta ist bereits <= 0 und keine neue Gebühr fällig -
        Idempotenz)."""

        vertrag = self._stammdaten_repository.get_vertrag(vertrag_id)
        konto = self._stammdaten_repository.get_konto_by_vertrag(vertrag_id)
        if vertrag is None or konto is None:
            return None
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)

        vorschau = self.vorschau(vertrag_id=vertrag_id, stufe=stufe, heute=heute)
        if vorschau is None or not vorschau.forderung_op_position_ids:
            return None

        mahnlauf_schluessel = _mahnlauf_schluessel(list(vorschau.forderung_op_position_ids))

        # Primäre Idempotenz: NICHT über eine "existiert bereits für
        # diesen Schlüssel"-Abfrage, sondern über das vertragsweite Delta
        # aus `vorschau()` - das ist bei einem Wiederholaufruf am selben
        # Tag oder bei Stufe 2 (dieselben, bei Stufe 1 bereits
        # fakturierten Zinstage) natürlich <= 0/None, siehe
        # `kosten_repository.py::bereits_gebuchte_zinsen_cent`.
        neu_anzusetzende_zinsen = max(vorschau.neue_zinsen_cent - vorschau.bereits_gebuchte_zinsen_cent, 0)
        if neu_anzusetzende_zinsen <= 0 and vorschau.gebuehr_cent is None:
            return None  # nichts Neues zu buchen (u.a. der Idempotenz-Fall)

        session_factory = self._repository._session_factory
        try:
            with session_factory() as session:
                zinsen_position_id = None
                if neu_anzusetzende_zinsen > 0:
                    zinsen_position = self._op_service.buchen(
                        ctx=ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=neu_anzusetzende_zinsen,
                        belegdatum=heute, buchungsdatum=heute, faelligkeit=None,
                        beleg_referenz=f"Verzugszinsen {vorschau.zinssatz_prozent}% p.a. ({vorschau.zinsbasis}), "
                                       f"{vorschau.zins_von}–{vorschau.zins_bis}",
                        aenderungsgrund="Mahnkosten - Verzugszinsen",
                        import_id=f"MAHNKOSTEN-ZINSEN:{vertrag_id}:{stufe}:{mahnlauf_schluessel}",
                        quelle_system="mahnkosten", session=session,
                    )
                    zinsen_position_id = zinsen_position.id
                gebuehr_position_id = None
                if vorschau.gebuehr_cent is not None:
                    gebuehr_position = self._op_service.buchen(
                        ctx=ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=vorschau.gebuehr_cent,
                        belegdatum=heute, buchungsdatum=heute, faelligkeit=None,
                        beleg_referenz=f"Mahnspesen ({vorschau.gebuehr_rechtsgrundlage})",
                        aenderungsgrund="Mahnkosten - Mahnspesen",
                        import_id=f"MAHNKOSTEN-GEBUEHR:{vertrag_id}:{stufe}:{mahnlauf_schluessel}",
                        quelle_system="mahnkosten", session=session,
                    )
                    gebuehr_position_id = gebuehr_position.id

                ledger = self._repository.buchung_anlegen(
                    vertrag_id=vertrag_id, stufe=stufe, mahnlauf_schluessel=mahnlauf_schluessel,
                    forderung_op_position_ids=list(vorschau.forderung_op_position_ids),
                    hauptforderung_cent=vorschau.hauptforderung_cent, zinsbasis=vorschau.zinsbasis,
                    zinssatz_prozent=vorschau.zinssatz_prozent or 0, zins_von=vorschau.zins_von or heute,
                    zins_bis=vorschau.zins_bis or heute, zinsen_cent=neu_anzusetzende_zinsen,
                    gebuehr_cent=vorschau.gebuehr_cent, rechtsgrundlage_gebuehr=vorschau.gebuehr_rechtsgrundlage,
                    versandnachweis_referenz=versandnachweis_referenz, zinsen_op_position_id=zinsen_position_id,
                    gebuehr_op_position_id=gebuehr_position_id, erstellt_von=akteur, session=session,
                )
                session.commit()
                return ledger
        except IntegrityError:
            # Ein anderer, gleichzeitiger Aufruf hat exakt denselben
            # (vertrag_id, stufe, zins_bis)-Mahnlauf zwischenzeitlich
            # bereits gebucht (uq_mahnkosten_lauf) - reines Race-Condition-
            # Sicherheitsnetz, kein Fehler; die PRIMÄRE Idempotenz ist das
            # Delta oben.
            return self._repository.buchung_fuer_stichtag(vertrag_id=vertrag_id, stufe=stufe, zins_bis=vorschau.zins_bis or heute)
