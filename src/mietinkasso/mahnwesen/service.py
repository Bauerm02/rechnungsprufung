"""Automatisches Mahnwesen, genau zwei Stufen (Fachregel 7).

Ablauf: `planen()` prüft Sperren, Bankfrische und bestätigten fälligen
Rest und legt bei Bedarf einen GEPLANT-Fall an (idempotent über
outbox_key). `versenden()` prüft unmittelbar vor dem eigentlichen
Versand ALLES nochmal (Sperren können neu gesetzt worden sein, eine
Zahlung kann zwischen Planung und Versand eingetroffen sein) und claimt
den Fall atomar, bevor der Versand-Provider aufgerufen wird. Ein
Provider-Timeout landet in UNSICHER und wird nie automatisch erneut
versucht.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable

from mietinkasso.auth.service import AuthContext, require_gesellschaft_access
from mietinkasso.domain.enums import MahnStatus, MahnStufe
from mietinkasso.domain.exceptions import MahnstufeReihenfolgeError
from mietinkasso.infrastructure.db.tables import KontoTable, MahnFallTable, MahnPolicyTable, VertragTable
from mietinkasso.mahnwesen.repository import MahnFallRepository
from mietinkasso.op.service import OPService, compute_content_hash
from mietinkasso.stammdaten.repository import StammdatenRepository


class VersandUngewissError(Exception):
    """Der Versand-Provider hat einen Timeout geliefert, nachdem die
    Nachricht möglicherweise bereits angenommen wurde. Niemals blind
    erneut senden; der Fall bleibt UNSICHER bis zur manuellen Klärung."""


@dataclass(frozen=True)
class PlanungsErgebnis:
    status: str  # GEPLANT | BLOCKIERT | KEIN_BETRAG
    mahnfall_id: int | None
    stufe: int | None
    grund: str


@dataclass(frozen=True)
class VersandErgebnis:
    status: str  # GESENDET | UNSICHER | UEBERSPRUNGEN | BLOCKIERT | BEREITS_VERARBEITET
    grund: str


class MahnwesenService:
    def __init__(
        self,
        repository: MahnFallRepository,
        stammdaten_repository: StammdatenRepository,
        op_service: OPService,
        *,
        bank_stand_max_age_days: int = 2,
    ):
        self._repository = repository
        self._stammdaten_repository = stammdaten_repository
        self._op_service = op_service
        self._bank_stand_max_age_days = bank_stand_max_age_days

    def naechste_stufe(self, vertrag_id: str) -> MahnStufe | None:
        letzter = self._repository.letzter_mahnfall(vertrag_id)
        if letzter is None:
            return MahnStufe.STUFE_1
        if letzter.stufe == MahnStufe.STUFE_1.value:
            if letzter.status == MahnStatus.GESENDET.value:
                return MahnStufe.STUFE_2
            return MahnStufe.STUFE_1  # Stufe 1 noch nicht erfolgreich versandt
        if letzter.stufe == MahnStufe.STUFE_2.value:
            return None  # nach Stufe 2 nur noch interner Bearbeitungsfall
        return MahnStufe.STUFE_1

    def planen(
        self,
        *,
        ctx: AuthContext,
        vertrag: VertragTable,
        konto: KontoTable,
        policy: MahnPolicyTable,
        heute: date,
        bank_stand_alter_tage: int | None,
    ) -> PlanungsErgebnis:
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)

        aktive_sperren = self._stammdaten_repository.aktive_sperren(vertrag.id)
        if aktive_sperren:
            gruende = ", ".join(s.grund for s in aktive_sperren)
            return PlanungsErgebnis("BLOCKIERT", None, None, f"Aktive Sperre(n): {gruende}")

        if bank_stand_alter_tage is None or bank_stand_alter_tage > self._bank_stand_max_age_days:
            return PlanungsErgebnis(
                "BLOCKIERT", None, None,
                f"Bankstand veraltet oder unbekannt (Alter: {bank_stand_alter_tage}, max {self._bank_stand_max_age_days} Tage).",
            )

        saldo = self._op_service.berechne_saldo(konto.id, stichtag=heute)
        betrag_cent = saldo.faelliger_unstrittiger_rest_cent
        if betrag_cent <= 0:
            return PlanungsErgebnis("KEIN_BETRAG", None, None, "Kein bestätigter fälliger Rest zum Stichtag.")

        stufe = self.naechste_stufe(vertrag.id)
        if stufe is None:
            return PlanungsErgebnis(
                "BLOCKIERT", None, None, "Nach Stufe 2 ist nur ein interner Bearbeitungsfall zulässig, keine Stufe 3."
            )

        if stufe is MahnStufe.STUFE_2:
            letzte_stufe1 = self._repository.letzter_mahnfall_je_stufe(vertrag.id, MahnStufe.STUFE_1.value)
            if letzte_stufe1 is None or letzte_stufe1.status != MahnStatus.GESENDET.value:
                raise MahnstufeReihenfolgeError(
                    f"Vertrag {vertrag.id}: Stufe 2 verlangt eine erfolgreich gesendete Stufe 1."
                )
            tage_seit_versand = (heute - letzte_stufe1.gesendet_am.date()).days
            mindest_tage = max(policy.stufe2_mindesttage_nach_stufe1_versand, vertrag.zahlungsfrist_tage)
            if tage_seit_versand < mindest_tage:
                return PlanungsErgebnis(
                    "BLOCKIERT", None, None,
                    f"Mindestabstand zu Stufe 1 noch nicht erreicht ({tage_seit_versand}/{mindest_tage} Tagen).",
                )

        forderungsumfang_hash = compute_content_hash(
            {"vertrag_id": vertrag.id, "betrag_cent": betrag_cent, "stufe": stufe.value}
        )
        outbox_key = f"{vertrag.gesellschaft_id}:{vertrag.id}:{forderungsumfang_hash}:{stufe.value}"
        mahnfall = self._repository.get_or_create(
            outbox_key=outbox_key,
            vertrag_id=vertrag.id,
            gesellschaft_id=vertrag.gesellschaft_id,
            forderungsumfang_hash=forderungsumfang_hash,
            stufe=stufe.value,
            policy_version=policy.version,
            betrag_cent=betrag_cent,
            bank_stand_datum=heute,
            snapshot={
                "vertrag_id": vertrag.id,
                "debitor_id": konto.debitor_id,
                "betrag_cent": betrag_cent,
                "stufe": stufe.value,
                "policy_version": policy.version,
                "zinsen_prozent": str(policy.zinsen_prozent),
                "gebuehr_cent": policy.gebuehr_cent,
            },
        )
        return PlanungsErgebnis("GEPLANT", mahnfall.id, mahnfall.stufe, "Geplant.")

    def versenden(
        self,
        *,
        ctx: AuthContext,
        mahnfall_id: int,
        vertrag: VertragTable,
        konto: KontoTable,
        heute: date,
        send_enabled: bool,
        versand_fn: Callable[[dict], None],
    ) -> VersandErgebnis:
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        mahnfall = self._repository.get(mahnfall_id)
        if mahnfall is None:
            raise ValueError(f"Unbekannter MahnFall {mahnfall_id}")
        if mahnfall.status != MahnStatus.GEPLANT.value:
            return VersandErgebnis("BEREITS_VERARBEITET", f"Status ist bereits {mahnfall.status}; kein Doppelversand.")

        aktive_sperren = self._stammdaten_repository.aktive_sperren(vertrag.id)
        if aktive_sperren:
            self._repository.set_status(mahnfall_id, MahnStatus.BLOCKIERT.value)
            return VersandErgebnis("BLOCKIERT", "Sperre wurde nach der Planung gesetzt.")

        saldo = self._op_service.berechne_saldo(konto.id, stichtag=heute)
        if saldo.faelliger_unstrittiger_rest_cent < mahnfall.betrag_cent:
            self._repository.set_status(mahnfall_id, MahnStatus.UEBERSPRUNGEN.value)
            return VersandErgebnis(
                "UEBERSPRUNGEN", "Zahlung zwischen Planung und Versand eingetroffen; Versand gestoppt."
            )

        if not send_enabled:
            return VersandErgebnis("BEREITS_VERARBEITET", "SEND_ENABLED=false: nur Preview/Outbox, kein realer Versand.")

        if not self._repository.claim_fuer_versand(mahnfall_id):
            return VersandErgebnis("BEREITS_VERARBEITET", "Ein anderer Worker verarbeitet diesen Fall bereits.")

        try:
            versand_fn(mahnfall.snapshot)
        except VersandUngewissError:
            self._repository.set_status(mahnfall_id, MahnStatus.UNSICHER.value)
            return VersandErgebnis("UNSICHER", "Provider-Timeout nach möglicher Annahme; kein automatischer Retry.")

        gesendet_am = datetime.combine(heute, datetime.min.time(), tzinfo=timezone.utc)
        self._repository.set_status(mahnfall_id, MahnStatus.GESENDET.value, gesendet_am=gesendet_am)
        return VersandErgebnis("GESENDET", "Erfolgreich versendet.")

    def manuell_abklaeren(self, *, mahnfall_id: int, neuer_status: MahnStatus) -> MahnFallTable:
        """Löst einen UNSICHER-Fall gezielt manuell auf (z. B. nach Rückfrage
        beim Provider), statt ihn blind erneut zu versenden."""

        zusatz = {}
        if neuer_status is MahnStatus.GESENDET:
            zusatz["gesendet_am"] = datetime.now(timezone.utc)
        return self._repository.set_status(mahnfall_id, neuer_status.value, **zusatz)
