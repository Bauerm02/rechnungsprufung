"""Betriebskostenabrechnung (Fachregel 6).

Eine WEG-Eigentümerabrechnung ist keine Mieterabrechnung: Rücklage,
Finanzierung, Sonderumlage, Reparatur sowie Verwaltungs-/
Abrechnungskosten werden nur dann auf Mieter umgelegt, wenn eine
Position explizit als UMLAGEFAEHIG (mit Quelle+Profil) erfasst wurde.
Alles andere bleibt EIGENTUEMER oder UNGEKLAERT und erzeugt nie einen
Mieter-OP. Nachbelastung/Gutschrift werden erst nach FREIGEGEBEN
gebucht - ein ENTWURF kann strukturell keine Forderung und damit auch
keine Mahnung auslösen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from mietinkasso.auth.service import AuthContext, require_gesellschaft_access, require_schreibrecht
from mietinkasso.bk.repository import BKRepository
from mietinkasso.domain.enums import BKAbrechnungStatus, BKPositionsart, OPTyp
from mietinkasso.domain.exceptions import BindungInkonsistentError
from mietinkasso.infrastructure.db.tables import BKVertragsAnteilTable
from mietinkasso.op.service import OPService
from mietinkasso.stammdaten.repository import StammdatenRepository


class BKAbrechnungNichtFreigegebenError(Exception):
    pass


@dataclass(frozen=True)
class BKAnteilEingabe:
    vertrag_id: str
    anteil_prozent: Decimal
    vorauszahlung_cent: int


class BKService:
    def __init__(self, repository: BKRepository, op_service: OPService, stammdaten_repository: StammdatenRepository):
        self._repository = repository
        self._op_service = op_service
        self._stammdaten_repository = stammdaten_repository

    def _gesellschaft_der_abrechnung(self, bk_abrechnung_id: int) -> str:
        abrechnung = self._repository.get_abrechnung(bk_abrechnung_id)
        if abrechnung is None:
            raise ValueError(f"Unbekannte BKAbrechnung {bk_abrechnung_id}")
        objekt = self._stammdaten_repository.get_objekt(abrechnung.objekt_id)
        if objekt is None:
            raise ValueError(f"Unbekanntes Objekt {abrechnung.objekt_id}")
        return objekt.gesellschaft_id

    def abrechnung_anlegen(self, *, ctx: AuthContext, objekt_id: str, abrechnungsjahr: int):
        objekt = self._stammdaten_repository.get_objekt(objekt_id)
        if objekt is None:
            raise ValueError(f"Unbekanntes Objekt {objekt_id}")
        require_gesellschaft_access(ctx, objekt.gesellschaft_id)
        require_schreibrecht(ctx)
        return self._repository.get_or_create_abrechnung(objekt_id=objekt_id, abrechnungsjahr=abrechnungsjahr)

    def position_hinzufuegen(
        self,
        *,
        ctx: AuthContext,
        bk_abrechnung_id: int,
        bezeichnung: str,
        betrag_cent: int,
        art: BKPositionsart,
        quelle: str,
        profil_referenz: str,
    ):
        require_gesellschaft_access(ctx, self._gesellschaft_der_abrechnung(bk_abrechnung_id))
        require_schreibrecht(ctx)
        return self._repository.add_position(
            bk_abrechnung_id=bk_abrechnung_id,
            bezeichnung=bezeichnung,
            betrag_cent=betrag_cent,
            art=art.value,
            quelle=quelle,
            profil_referenz=profil_referenz,
        )

    def umlagefaehige_summe_cent(self, bk_abrechnung_id: int) -> int:
        positionen = self._repository.list_positionen(bk_abrechnung_id)
        return sum(p.betrag_cent for p in positionen if p.art == BKPositionsart.UMLAGEFAEHIG.value)

    def anteile_berechnen(
        self, *, ctx: AuthContext, bk_abrechnung_id: int, eingaben: list[BKAnteilEingabe]
    ) -> list[BKVertragsAnteilTable]:
        require_gesellschaft_access(ctx, self._gesellschaft_der_abrechnung(bk_abrechnung_id))
        require_schreibrecht(ctx)
        umlagefaehig_cent = self.umlagefaehige_summe_cent(bk_abrechnung_id)
        ergebnisse = []
        for eingabe in eingaben:
            umlage_cent_decimal = (Decimal(umlagefaehig_cent) * eingabe.anteil_prozent / 100).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
            umlage_cent = int(umlage_cent_decimal)
            ergebnis_cent = umlage_cent - eingabe.vorauszahlung_cent
            anteil = BKVertragsAnteilTable(
                bk_abrechnung_id=bk_abrechnung_id,
                vertrag_id=eingabe.vertrag_id,
                anteil_prozent=eingabe.anteil_prozent,
                umlage_cent=umlage_cent,
                vorauszahlung_cent=eingabe.vorauszahlung_cent,
                ergebnis_cent=ergebnis_cent,
            )
            ergebnisse.append(self._repository.save_anteil(anteil))
        return ergebnisse

    def pruefen(self, bk_abrechnung_id: int, *, ctx: AuthContext):
        require_gesellschaft_access(ctx, self._gesellschaft_der_abrechnung(bk_abrechnung_id))
        require_schreibrecht(ctx)
        return self._repository.set_status(bk_abrechnung_id, BKAbrechnungStatus.GEPRUEFT.value)

    def freigeben(self, bk_abrechnung_id: int, *, ctx: AuthContext):
        require_gesellschaft_access(ctx, self._gesellschaft_der_abrechnung(bk_abrechnung_id))
        require_schreibrecht(ctx)
        return self._repository.set_status(bk_abrechnung_id, BKAbrechnungStatus.FREIGEGEBEN.value)

    def ergebnisse_buchen(self, *, ctx: AuthContext, bk_abrechnung_id: int, konten_je_vertrag: dict[str, object], heute: date | None = None):
        """`konten_je_vertrag` mappt vertrag_id -> KontoTable. Bucht je Anteil
        eine Nachbelastung (SOLL) oder Gutschrift (GUTSCHRIFT); nur möglich,
        wenn die Abrechnung FREIGEGEBEN ist."""

        abrechnung = self._repository.get_abrechnung(bk_abrechnung_id)
        if abrechnung is None:
            raise ValueError(f"Unbekannte BKAbrechnung {bk_abrechnung_id}")
        require_gesellschaft_access(ctx, self._gesellschaft_der_abrechnung(bk_abrechnung_id))
        require_schreibrecht(ctx)
        if abrechnung.status != BKAbrechnungStatus.FREIGEGEBEN.value:
            raise BKAbrechnungNichtFreigegebenError(
                f"BKAbrechnung {bk_abrechnung_id} ist im Status {abrechnung.status}; "
                "eine Nachbelastung/Gutschrift darf erst nach FREIGEGEBEN gebucht werden."
            )
        gebuchte = []
        heute = heute or date.today()
        for anteil in self._repository.list_anteile(bk_abrechnung_id):
            konto = konten_je_vertrag[anteil.vertrag_id]
            if konto.vertrag_id != anteil.vertrag_id:
                raise BindungInkonsistentError(
                    f"Konto {konto.id} gehört zu Vertrag {konto.vertrag_id}, nicht zu {anteil.vertrag_id}."
                )
            require_gesellschaft_access(ctx, konto.gesellschaft_id)
            if anteil.ergebnis_cent == 0:
                continue
            typ = OPTyp.SOLL if anteil.ergebnis_cent > 0 else OPTyp.GUTSCHRIFT
            op_row = self._op_service.buchen(
                ctx=ctx,
                konto=konto,
                typ=typ,
                betrag_cent=abs(anteil.ergebnis_cent),
                belegdatum=heute,
                buchungsdatum=heute,
                faelligkeit=heute,
                beleg_referenz=f"BK-Abrechnung {bk_abrechnung_id} Jahresergebnis",
                import_id=f"BK-{bk_abrechnung_id}-{anteil.vertrag_id}",
                quelle_system="bk_abrechnung",
            )
            gebuchte.append(op_row)
        return gebuchte
