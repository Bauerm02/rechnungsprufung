"""Automatisches Mahnwesen, genau zwei Stufen (Fachregel 7).

Der Mahnzyklus (Stufe1 -> Stufe2 -> nur noch interner Fall) hängt an
einer EINZELNEN FORDERUNG (identifiziert über die OP-Zeile, die sie
erzeugt hat: Eröffnung/Soll/Rücklastschrift), NICHT am Vertrag als
Ganzes. Ein neuer Rückstand (eine neue Forderung) beginnt seinen eigenen
Zyklus, unabhängig davon, wie weit ältere Forderungen desselben Vertrags
schon gediehen sind - die Nettosumme des Kontos ist keine Mahngrundlage.

Ablauf: `plane_forderung()` prüft Sperren, Bankfrische, Policy-Freigabe,
Wartefristen (Stufe1 X Tage nach Fälligkeit; Stufe2 erst nach gesendeter
Stufe1 plus Mindestabstand) und legt bei Bedarf einen GEPLANT-Fall an
(idempotent über outbox_key). `versenden()` lädt Vertrag/Konto/Policy
serverseitig frisch nach (nie den Aufrufer-Objekten vertrauen), prüft
UNMITTELBAR vor dem eigentlichen Versand nochmal ALLES (Sperren, frischer
Bankstand, ob GENAU DIESE Forderung noch in mindestens der geplanten Höhe
offen ist) und claimt den Fall atomar (GEPLANT->IN_VERSAND), bevor der
Versand-Provider aufgerufen wird. Ein Provider-Timeout landet in
UNSICHER und wird nie automatisch erneut versucht; ein Absturz zwischen
Claim und Ergebnis bleibt IN_VERSAND und wird nur über die explizite
Recovery-Funktion aufgelöst, nie automatisch erneut angestoßen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from mietinkasso.auth.service import AuthContext, require_gesellschaft_access, require_schreibrecht
from mietinkasso.domain.enums import MahnStatus, MahnStufe
from mietinkasso.domain.exceptions import BindungInkonsistentError, MahnstufeReihenfolgeError
from mietinkasso.infrastructure.db.tables import KontoTable, MahnFallTable, MahnPolicyTable, VertragTable
from mietinkasso.mahnwesen.repository import MahnFallRepository
from mietinkasso.op.service import OffeneForderung, OPService, compute_content_hash
from mietinkasso.stammdaten.repository import StammdatenRepository


class VersandUngewissError(Exception):
    """Der Versand-Provider hat einen Timeout geliefert, nachdem die
    Nachricht möglicherweise bereits angenommen wurde. Niemals blind
    erneut senden; der Fall bleibt UNSICHER bis zur manuellen Klärung."""


class PolicyNichtFreigegebenError(Exception):
    """Eine MahnPolicy im Status ENTWURF darf keine Fälle planen."""


@dataclass(frozen=True)
class PlanungsErgebnis:
    status: str  # GEPLANT | BLOCKIERT | KEIN_BETRAG | ZU_FRUEH
    mahnfall_id: int | None
    stufe: int | None
    forderung_op_position_id: int | None
    grund: str


@dataclass(frozen=True)
class VersandErgebnis:
    status: str  # GESENDET | UNSICHER | UEBERSPRUNGEN | BLOCKIERT | BEREITS_VERARBEITET
    grund: str


def _validiere_bindung(mahnfall: MahnFallTable, vertrag: VertragTable | None, konto: KontoTable | None) -> None:
    if vertrag is None or konto is None:
        raise BindungInkonsistentError(f"MahnFall {mahnfall.id}: Vertrag/Konto nicht auffindbar.")
    if vertrag.id != mahnfall.vertrag_id or vertrag.gesellschaft_id != mahnfall.gesellschaft_id:
        raise BindungInkonsistentError(
            f"MahnFall {mahnfall.id}: Vertrag/Gesellschaft stimmen nicht mit den gespeicherten Werten überein."
        )
    if konto.vertrag_id != vertrag.id:
        raise BindungInkonsistentError(f"Konto {konto.id} gehört nicht zu Vertrag {vertrag.id}.")


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

    def _naechste_stufe_fuer_forderung(self, forderung_op_position_id: int) -> MahnStufe | None:
        letzter = self._repository.letzter_mahnfall_fuer_forderung(forderung_op_position_id)
        if letzter is None:
            return MahnStufe.STUFE_1
        if letzter.stufe == MahnStufe.STUFE_1.value:
            if letzter.status == MahnStatus.GESENDET.value:
                return MahnStufe.STUFE_2
            return MahnStufe.STUFE_1
        if letzter.stufe == MahnStufe.STUFE_2.value:
            return None  # nach Stufe 2 nur noch interner Bearbeitungsfall
        return MahnStufe.STUFE_1

    def plane_forderung(
        self,
        *,
        ctx: AuthContext,
        vertrag: VertragTable,
        konto: KontoTable,
        forderung: OffeneForderung,
        policy: MahnPolicyTable,
        heute: date,
        bank_stand_alter_tage: int | None,
    ) -> PlanungsErgebnis:
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)
        if konto.vertrag_id != vertrag.id:
            raise BindungInkonsistentError(f"Konto {konto.id} gehört nicht zu Vertrag {vertrag.id}.")
        if policy.status != "FREIGEGEBEN":
            raise PolicyNichtFreigegebenError(
                f"MahnPolicy Version {policy.version} ist im Status {policy.status}; nur eine FREIGEGEBENE "
                "Policy darf Mahnfälle planen."
            )

        aktive_sperren = self._stammdaten_repository.aktive_sperren(vertrag.id)
        if aktive_sperren:
            gruende = ", ".join(s.grund for s in aktive_sperren)
            return PlanungsErgebnis("BLOCKIERT", None, None, forderung.op_position_id, f"Aktive Sperre(n): {gruende}")

        if bank_stand_alter_tage is None or bank_stand_alter_tage > self._bank_stand_max_age_days:
            return PlanungsErgebnis(
                "BLOCKIERT", None, None, forderung.op_position_id,
                f"Bankstand veraltet oder unbekannt (Alter: {bank_stand_alter_tage}, max {self._bank_stand_max_age_days} Tage).",
            )

        if not forderung.faelligkeit_bekannt or forderung.faelligkeit is None:
            return PlanungsErgebnis(
                "KEIN_BETRAG", None, None, forderung.op_position_id,
                "Fälligkeit unbekannt (z. B. Gesamtsaldo-Eröffnung); wird nie automatisch gemahnt.",
            )
        if forderung.rest_cent <= 0:
            return PlanungsErgebnis("KEIN_BETRAG", None, None, forderung.op_position_id, "Forderung ist bereits ausgeglichen.")

        stufe = self._naechste_stufe_fuer_forderung(forderung.op_position_id)
        if stufe is None:
            return PlanungsErgebnis(
                "BLOCKIERT", None, None, forderung.op_position_id,
                "Nach Stufe 2 ist nur ein interner Bearbeitungsfall zulässig, keine Stufe 3.",
            )

        if stufe is MahnStufe.STUFE_1:
            faellig_seit_tagen = (heute - forderung.faelligkeit).days
            if faellig_seit_tagen < policy.stufe1_tage_nach_faelligkeit:
                return PlanungsErgebnis(
                    "ZU_FRUEH", None, None, forderung.op_position_id,
                    f"Erst {faellig_seit_tagen}/{policy.stufe1_tage_nach_faelligkeit} Tage seit Fälligkeit vergangen.",
                )
        else:
            letzte_stufe1 = self._repository.letzter_mahnfall_je_stufe_fuer_forderung(
                forderung.op_position_id, MahnStufe.STUFE_1.value
            )
            if letzte_stufe1 is None or letzte_stufe1.status != MahnStatus.GESENDET.value:
                raise MahnstufeReihenfolgeError(
                    f"Forderung {forderung.op_position_id}: Stufe 2 verlangt eine erfolgreich gesendete Stufe 1."
                )
            tage_seit_versand = (heute - letzte_stufe1.gesendet_am.date()).days
            mindest_tage = max(policy.stufe2_mindesttage_nach_stufe1_versand, vertrag.zahlungsfrist_tage)
            if tage_seit_versand < mindest_tage:
                return PlanungsErgebnis(
                    "ZU_FRUEH", None, None, forderung.op_position_id,
                    f"Mindestabstand zu Stufe 1 noch nicht erreicht ({tage_seit_versand}/{mindest_tage} Tagen).",
                )

        forderungsumfang_hash = compute_content_hash(
            {"forderung_op_position_id": forderung.op_position_id, "betrag_cent": forderung.rest_cent, "stufe": stufe.value}
        )
        outbox_key = f"{vertrag.gesellschaft_id}:{vertrag.id}:{forderung.op_position_id}:{stufe.value}"
        mahnfall = self._repository.get_or_create(
            outbox_key=outbox_key,
            vertrag_id=vertrag.id,
            gesellschaft_id=vertrag.gesellschaft_id,
            forderung_op_position_id=forderung.op_position_id,
            forderungsumfang_hash=forderungsumfang_hash,
            stufe=stufe.value,
            policy_version=policy.version,
            betrag_cent=forderung.rest_cent,
            bank_stand_datum=heute,
            snapshot={
                "vertrag_id": vertrag.id,
                "debitor_id": konto.debitor_id,
                "forderung_op_position_id": forderung.op_position_id,
                "betrag_cent": forderung.rest_cent,
                "stufe": stufe.value,
                "policy_version": policy.version,
                "zinsen_prozent": str(policy.zinsen_prozent),
                "gebuehr_cent": policy.gebuehr_cent,
            },
        )
        return PlanungsErgebnis("GEPLANT", mahnfall.id, mahnfall.stufe, forderung.op_position_id, "Geplant.")

    def plane_alle_offenen_forderungen(
        self,
        *,
        ctx: AuthContext,
        vertrag: VertragTable,
        konto: KontoTable,
        policy: MahnPolicyTable,
        heute: date,
        bank_stand_alter_tage: int | None,
    ) -> list[PlanungsErgebnis]:
        forderungen = self._op_service.offene_forderungen(konto.id, heute=heute)
        return [
            self.plane_forderung(
                ctx=ctx, vertrag=vertrag, konto=konto, forderung=forderung, policy=policy, heute=heute,
                bank_stand_alter_tage=bank_stand_alter_tage,
            )
            for forderung in forderungen
        ]

    def versenden(
        self,
        *,
        ctx: AuthContext,
        mahnfall_id: int,
        heute: date,
        bank_stand_alter_tage: int | None,
        send_enabled: bool,
        versand_fn: Callable[[dict], None],
    ) -> VersandErgebnis:
        """`bank_stand_alter_tage` MUSS unmittelbar vor diesem Aufruf frisch
        ermittelt werden (z. B. via `BankImportService.bankstand_alter_tage`)
        - ein beim Planen gemessener, inzwischen veralteter Wert darf hier
        nicht wiederverwendet werden."""

        mahnfall = self._repository.get(mahnfall_id)
        if mahnfall is None:
            raise ValueError(f"Unbekannter MahnFall {mahnfall_id}")

        # Serverseitig neu laden statt Aufrufer-Objekten zu vertrauen.
        vertrag = self._stammdaten_repository.get_vertrag(mahnfall.vertrag_id)
        konto = self._stammdaten_repository.get_konto_by_vertrag(mahnfall.vertrag_id)
        _validiere_bindung(mahnfall, vertrag, konto)

        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)

        if mahnfall.status != MahnStatus.GEPLANT.value:
            return VersandErgebnis("BEREITS_VERARBEITET", f"Status ist bereits {mahnfall.status}; kein Doppelversand.")

        aktive_sperren = self._stammdaten_repository.aktive_sperren(vertrag.id)
        if aktive_sperren:
            self._repository.set_status(mahnfall_id, MahnStatus.BLOCKIERT.value)
            return VersandErgebnis("BLOCKIERT", "Sperre wurde nach der Planung gesetzt.")

        if bank_stand_alter_tage is None or bank_stand_alter_tage > self._bank_stand_max_age_days:
            self._repository.set_status(mahnfall_id, MahnStatus.BLOCKIERT.value)
            return VersandErgebnis(
                "BLOCKIERT",
                f"Bankstand ist beim Versand veraltet oder unbekannt (Alter: {bank_stand_alter_tage}, "
                f"max {self._bank_stand_max_age_days} Tage).",
            )

        # Identitätsbasierte Neuprüfung: nicht nur "ist die Kontosumme noch
        # groß genug" (das würde eine andere, zufällig gleich große
        # Veränderung nicht bemerken), sondern "ist GENAU DIESE Forderung
        # noch in mindestens der geplanten Höhe offen".
        offene_forderungen = self._op_service.offene_forderungen(konto.id, heute=heute)
        passende_forderung = next(
            (f for f in offene_forderungen if f.op_position_id == mahnfall.forderung_op_position_id), None
        )
        if passende_forderung is None or passende_forderung.rest_cent < mahnfall.betrag_cent:
            self._repository.set_status(mahnfall_id, MahnStatus.UEBERSPRUNGEN.value)
            return VersandErgebnis(
                "UEBERSPRUNGEN",
                "Die Forderung ist zwischen Planung und Versand nicht mehr in geplanter Höhe offen "
                "(Zahlung, Korrektur oder Storno).",
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

    def markiere_verwaiste_als_unsicher(self, *, jetzt: datetime | None = None, max_alter: timedelta = timedelta(minutes=15)) -> list[MahnFallTable]:
        """Recovery für einen Absturz zwischen `claim_fuer_versand` und dem
        Auflösen des Ergebnisses: ein IN_VERSAND-Fall, der seit `max_alter`
        feststeckt, wird NICHT automatisch erneut versucht, sondern auf
        UNSICHER gesetzt und damit explizit der manuellen Klärung
        zugeführt - derselbe Grundsatz wie bei einem Provider-Timeout."""

        grenze = (jetzt or datetime.now(timezone.utc)) - max_alter
        verwaiste = self._repository.verwaiste_in_versand(aelter_als=grenze)
        for fall in verwaiste:
            self._repository.set_status(fall.id, MahnStatus.UNSICHER.value)
        return verwaiste

    def manuell_abklaeren(self, *, mahnfall_id: int, neuer_status: MahnStatus) -> MahnFallTable:
        """Löst einen UNSICHER-Fall gezielt manuell auf (z. B. nach Rückfrage
        beim Provider), statt ihn blind erneut zu versenden."""

        zusatz = {}
        if neuer_status is MahnStatus.GESENDET:
            zusatz["gesendet_am"] = datetime.now(timezone.utc)
        return self._repository.set_status(mahnfall_id, neuer_status.value, **zusatz)
