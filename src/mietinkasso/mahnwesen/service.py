"""Automatisches Mahnwesen, genau zwei Stufen (Fachregel 7).

Der Mahnzyklus (Stufe1 -> Stufe2 -> nur noch interner Fall) hängt an
einer EINZELNEN FORDERUNG (identifiziert über die OP-Zeile, die sie
erzeugt hat: Eröffnung/Soll/Rücklastschrift), NICHT am Vertrag als
Ganzes. Ein neuer Rückstand (eine neue Forderung) beginnt seinen eigenen
Zyklus, unabhängig davon, wie weit ältere Forderungen desselben Vertrags
schon gediehen sind - die Nettosumme des Kontos ist keine Mahngrundlage.

Ablauf: `plane_forderung()` prüft Sperren, eine BESTÄTIGTE
Bankvollständigkeit (nicht bloß das Datum der letzten importierten
Zeile - das beweist nur, dass irgendeine Zeile existiert, nicht dass der
Import lückenlos war), ungeklärte/teilzugeordnete Bankeingänge, die
diesen Vertrag betreffen, Policy-Freigabe, einen hinterlegten Empfänger
und Wartefristen (Stufe1 X Tage nach Fälligkeit; Stufe2 erst nach
gesendeter Stufe1 plus Mindestabstand) und legt bei Bedarf einen
GEPLANT-Fall an (idempotent über outbox_key). `versenden()` lädt
Vertrag/Konto/Policy/Empfänger serverseitig frisch nach (nie den
Aufrufer-Objekten oder dem alten Planungs-Snapshot vertrauen), prüft
UNMITTELBAR vor dem eigentlichen Versand nochmal ALLES (Sperren,
Objektausschluss, frische Bankbestätigung, ungeklärte Eingänge, ob die
Policy noch dieselbe freigegebene Version ist, ob GENAU DIESE Forderung
noch in mindestens der geplanten Höhe offen ist) und claimt den Fall
atomar (GEPLANT->IN_VERSAND), bevor der Versand-Provider aufgerufen
wird. Ein Provider-Timeout landet in UNSICHER und wird nie automatisch
erneut versucht; ein Absturz zwischen Claim und Ergebnis bleibt
IN_VERSAND und wird nur über die explizite Recovery-Funktion aufgelöst,
nie automatisch erneut angestoßen.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from mietinkasso.auth.service import AuthContext, require_gesellschaft_access, require_schreibrecht
from mietinkasso.domain.enums import MahnStatus, MahnStufe, rechtsordnung_geklaert
from mietinkasso.domain.exceptions import BindungInkonsistentError, MahnstufeReihenfolgeError
from mietinkasso.infrastructure.db.tables import KontoTable, MahnFallTable, MahnLaufTable, MahnPolicyTable, VertragTable
from mietinkasso.mahnwesen.repository import MahnFallRepository, MahnLaufRepository, MahnPolicyRepository
from mietinkasso.op.service import OffeneForderung, OPService, compute_content_hash
from mietinkasso.stammdaten.repository import StammdatenRepository
from mietinkasso.indexautomatik.mailnachweis import nachweis_daten, versand_belegen
from mietinkasso.domain.exceptions import TransportFehlerUngewissError
from mietinkasso.indexautomatik.zeit import heute_wien


def _versandtag_wien(value):
    # SQLite drops timezone information. Persisted sending times are UTC.
    return heute_wien(value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value)


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
        mahn_policy_repository: MahnPolicyRepository,
        *,
        bank_stand_max_age_days: int = 2,
        mahnkosten_service=None,
        mahnlauf_repository: MahnLaufRepository | None = None,
    ):
        self._repository = repository
        self._stammdaten_repository = stammdaten_repository
        self._op_service = op_service
        self._mahn_policy_repository = mahn_policy_repository
        self._bank_stand_max_age_days = bank_stand_max_age_days
        # Optional (siehe `mahnwesen/kosten_service.py`) - ohne konfigurierten
        # Service bleibt das Verhalten UNVERÄNDERT gegenüber vor Auftrag
        # HV-20260913-MAHNKOSTEN (keine Kostenbuchung beim Versand), damit
        # bestehende Aufrufer/Tests ohne Anpassung weiterlaufen.
        self._mahnkosten_service = mahnkosten_service
        # Optional (siehe `plane_mahnlauf`/`versende_mahnlauf`) - ohne
        # konfiguriertes Repository bleiben nur die bestehenden
        # Einzelfall-Methoden (`plane_forderung`/`versenden`) nutzbar,
        # bestehende Aufrufer/Tests laufen unverändert weiter.
        self._mahnlauf_repository = mahnlauf_repository

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

    def _bankstand_veraltet(self, *, heute: date, bank_bestaetigt_bis: date | None) -> bool:
        if bank_bestaetigt_bis is None:
            return True
        return (heute - bank_bestaetigt_bis).days > self._bank_stand_max_age_days

    def plane_forderung(
        self,
        *,
        ctx: AuthContext,
        vertrag: VertragTable,
        konto: KontoTable,
        forderung: OffeneForderung,
        policy: MahnPolicyTable,
        heute: date,
        bank_bestaetigt_bis: date | None,
        ungeklaerte_eingaenge_vorhanden: bool = False,
    ) -> PlanungsErgebnis:
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)
        if konto.vertrag_id != vertrag.id:
            raise BindungInkonsistentError(f"Konto {konto.id} gehört nicht zu Vertrag {vertrag.id}.")
        self._stammdaten_repository.pruefe_vertrag_nicht_ausgeschlossen(vertrag.id)
        if policy.status != "FREIGEGEBEN":
            raise PolicyNichtFreigegebenError(
                f"MahnPolicy Version {policy.version} ist im Status {policy.status}; nur eine FREIGEGEBENE "
                "Policy darf Mahnfälle planen."
            )

        if not rechtsordnung_geklaert(vertrag.rechtsordnung):
            return PlanungsErgebnis(
                "BLOCKIERT", None, None, forderung.op_position_id,
                "Rechtsordnung des Vertrags ist UNGEKLAERT; Mahnung gesperrt, bis die rechtliche "
                "Einordnung feststeht.",
            )

        aktive_sperren = self._stammdaten_repository.aktive_sperren(vertrag.id)
        if aktive_sperren:
            gruende = ", ".join(s.grund for s in aktive_sperren)
            return PlanungsErgebnis("BLOCKIERT", None, None, forderung.op_position_id, f"Aktive Sperre(n): {gruende}")

        if self._bankstand_veraltet(heute=heute, bank_bestaetigt_bis=bank_bestaetigt_bis):
            return PlanungsErgebnis(
                "BLOCKIERT", None, None, forderung.op_position_id,
                f"Keine ausreichend aktuelle BESTÄTIGTE Bankvollständigkeit vorhanden "
                f"(bestätigt bis: {bank_bestaetigt_bis}, max {self._bank_stand_max_age_days} Tage alt). "
                "Das Datum der letzten importierten Zeile allein beweist keine Vollständigkeit.",
            )
        if ungeklaerte_eingaenge_vorhanden:
            return PlanungsErgebnis(
                "BLOCKIERT", None, None, forderung.op_position_id,
                "Es gibt ungeklärte oder nur teilzugeordnete Bankeingänge, die diesen Vertrag betreffen "
                "könnten; Mahnung bleibt geschlossen, bis das aufgeklärt ist.",
            )

        debitor = self._stammdaten_repository.get_debitor(konto.debitor_id)
        if debitor is None or not (debitor.email or "").strip():
            return PlanungsErgebnis(
                "BLOCKIERT", None, None, forderung.op_position_id,
                f"Kein gültiger Empfänger (E-Mail) für Debitor {konto.debitor_id} hinterlegt.",
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
            if letzte_stufe1 is None or letzte_stufe1.status != MahnStatus.GESENDET.value or letzte_stufe1.gesendet_am is None:
                raise MahnstufeReihenfolgeError(
                    f"Forderung {forderung.op_position_id}: Stufe 2 verlangt eine erfolgreich gesendete Stufe 1."
                )
            tage_seit_versand = (heute - _versandtag_wien(letzte_stufe1.gesendet_am)).days
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
                "empfaenger_email": debitor.email,
                "empfaenger_name": debitor.name,
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
        bank_bestaetigt_bis: date | None,
        ungeklaerte_eingaenge_vorhanden: bool = False,
    ) -> list[PlanungsErgebnis]:
        forderungen = self._op_service.offene_forderungen(konto.id, heute=heute)
        return [
            self.plane_forderung(
                ctx=ctx, vertrag=vertrag, konto=konto, forderung=forderung, policy=policy, heute=heute,
                bank_bestaetigt_bis=bank_bestaetigt_bis, ungeklaerte_eingaenge_vorhanden=ungeklaerte_eingaenge_vorhanden,
            )
            for forderung in forderungen
        ]

    def _pruefe_frisch_versandbereit(
        self, *, mahnfall: MahnFallTable, vertrag: VertragTable, konto: KontoTable,
        aktuelle_policy: MahnPolicyTable | None, heute: date,
        bank_bestaetigt_bis: date | None, ungeklaerte_eingaenge_vorhanden: bool,
    ) -> tuple[str | None, str, OffeneForderung | None]:
        """Reine Prüfung OHNE Nebenwirkung aller unmittelbar-vor-Versand
        Bedingungen für EINEN MahnFall - von `versenden()` (das je nach
        Ergebnis selbst `set_status` aufruft) UND von der reinen
        Batch-Vorprüfung für die Mahnlauf-Bündelung in
        `indexautomatik/mailversand_service.py` gemeinsam genutzt, damit
        beide NIE auseinanderlaufen können.

        Rückgabe `(status_code, grund, passende_forderung)`:
        `status_code` ist `None`, wenn alles bereit ist (der Aufrufer darf
        claimen/senden); sonst `"BLOCKIERT_PERSIST"`/`"UEBERSPRUNGEN_PERSIST"`
        (der Fall wird dauerhaft auf diesen Status gesetzt) oder
        `"BLOCKIERT_TRANSIENT"` (bloß "noch nicht fällig" - bleibt GEPLANT,
        kann bei einem späteren Lauf alleine oder gemeinsam mit anderen
        Forderungen wieder versandbereit werden)."""

        if not rechtsordnung_geklaert(vertrag.rechtsordnung):
            return "BLOCKIERT_PERSIST", "Rechtsordnung des Vertrags ist UNGEKLAERT.", None
        aktive_sperren = self._stammdaten_repository.aktive_sperren(vertrag.id)
        if aktive_sperren:
            return "BLOCKIERT_PERSIST", "Sperre wurde nach der Planung gesetzt.", None
        if self._bankstand_veraltet(heute=heute, bank_bestaetigt_bis=bank_bestaetigt_bis):
            return "BLOCKIERT_PERSIST", (
                f"Keine ausreichend aktuelle BESTÄTIGTE Bankvollständigkeit beim Versand "
                f"(bestätigt bis: {bank_bestaetigt_bis}, max {self._bank_stand_max_age_days} Tage alt)."
            ), None
        if ungeklaerte_eingaenge_vorhanden:
            return "BLOCKIERT_PERSIST", "Ungeklärte/teilzugeordnete Bankeingänge sind zwischen Planung und Versand aufgetaucht.", None

        # Policy frisch prüfen: wurde sie seit der Planung zurückgezogen
        # oder durch eine neue Version ersetzt, wird NICHT mit der alten,
        # zum Planungszeitpunkt gültigen Policy weiterversendet.
        if aktuelle_policy is None or aktuelle_policy.version != mahnfall.policy_version:
            return "BLOCKIERT_PERSIST", (
                f"Policy-Version {mahnfall.policy_version} ist nicht mehr die aktuell freigegebene Policy "
                f"(aktuell: {aktuelle_policy.version if aktuelle_policy else None})."
            ), None

        # Empfänger frisch prüfen UND gegen den bei der Planung freigegebenen
        # Snapshot vergleichen: eine bloße Nicht-Leer-Prüfung würde eine
        # zwischenzeitlich KORRIGIERTE/GEÄNDERTE Adresse (anderer Name oder
        # andere E-Mail als zum Planungszeitpunkt) nicht bemerken und mit der
        # ungeprüft neuen Adresse weiterversenden - eine Empfänger-Änderung
        # nach der Planung braucht eine neue Freigabe, kein stillschweigendes
        # Mitziehen.
        debitor = self._stammdaten_repository.get_debitor(konto.debitor_id)
        if debitor is None or not (debitor.email or "").strip():
            return "BLOCKIERT_PERSIST", f"Kein gültiger Empfänger (E-Mail) für Debitor {konto.debitor_id} mehr hinterlegt.", None
        geplante_email = mahnfall.snapshot.get("empfaenger_email")
        geplanter_name = mahnfall.snapshot.get("empfaenger_name")
        if debitor.email != geplante_email or debitor.name != geplanter_name:
            return "BLOCKIERT_PERSIST", (
                f"Empfänger hat sich seit der Planung geändert (geplant: {geplanter_name!r} <{geplante_email!r}>, "
                f"jetzt: {debitor.name!r} <{debitor.email!r}>); eine neue Freigabe/Planung ist erforderlich."
            ), None

        # Identitätsbasierte Neuprüfung: nicht nur "ist die Kontosumme noch
        # groß genug" (das würde eine andere, zufällig gleich große
        # Veränderung nicht bemerken), sondern "ist GENAU DIESE Forderung
        # noch in mindestens der geplanten Höhe offen". Erfasst insbesondere
        # eine kurz vor dem Versand neu eingegangene (Teil-)Zahlung.
        offene_forderungen = self._op_service.offene_forderungen(konto.id, heute=heute)
        passende_forderung = next(
            (f for f in offene_forderungen if f.op_position_id == mahnfall.forderung_op_position_id), None
        )
        if passende_forderung is None or passende_forderung.rest_cent < mahnfall.betrag_cent:
            return "UEBERSPRUNGEN_PERSIST", (
                "Die Forderung ist zwischen Planung und Versand nicht mehr in geplanter Höhe offen "
                "(Zahlung, Korrektur oder Storno)."
            ), None

        # Re-evaluate timing after a possibly delayed approval/dispatch.
        if not passende_forderung.faelligkeit_bekannt or passende_forderung.faelligkeit is None:
            return "BLOCKIERT_PERSIST", "Fälligkeit ist inzwischen ungeklärt.", None
        if mahnfall.stufe == 1:
            fruehestens = passende_forderung.faelligkeit + timedelta(days=aktuelle_policy.stufe1_tage_nach_faelligkeit)
        else:
            first = self._repository.letzter_mahnfall_je_stufe_fuer_forderung(mahnfall.forderung_op_position_id, 1)
            if first is None or first.status != "GESENDET" or first.gesendet_am is None:
                return "BLOCKIERT_PERSIST", "Zweite Mahnung benötigt die tatsächlich gesendete erste Mahnung.", None
            fruehestens = _versandtag_wien(first.gesendet_am) + timedelta(days=max(
                aktuelle_policy.stufe2_mindesttage_nach_stufe1_versand, vertrag.zahlungsfrist_tage))
        if heute < fruehestens:
            return "BLOCKIERT_TRANSIENT", "Mahnfrist ist noch nicht abgelaufen.", None

        return None, "", passende_forderung

    def versenden(
        self,
        *,
        ctx: AuthContext,
        mahnfall_id: int,
        heute: date,
        bank_bestaetigt_bis: date | None,
        ungeklaerte_eingaenge_vorhanden: bool,
        send_enabled: bool,
        versand_fn: Callable[[dict], object],
        mahnkosten_vorschau_slot: dict | None = None,
    ) -> VersandErgebnis:
        """`bank_bestaetigt_bis`/`ungeklaerte_eingaenge_vorhanden` MÜSSEN
        unmittelbar vor diesem Aufruf frisch ermittelt werden - ein bei der
        Planung gemessener, inzwischen veralteter Wert darf hier nicht
        wiederverwendet werden.

        `mahnkosten_vorschau_slot`: optionales, vom Aufrufer bereitgestelltes
        Dict. Ist es leer (Default `None`/`{}`), berechnet diese Methode die
        Mahnkosten-Vorschau für die Buchung INTERN frisch (altes Verhalten,
        für einfache, nicht gebündelte Aufrufer). Trägt `versand_fn` als
        Seiteneffekt selbst einen Schlüssel `"vorschau"` ein (z. B. weil es
        dieselbe Vorschau bereits für den Brieftext verwendet hat), wird
        GENAU DIESES Objekt für die Buchung übernommen - niemals eine
        zweite, potenziell abweichende Neuberechnung. Das garantiert die
        exakte Übereinstimmung zwischen gesendetem Kosten-/Zinsnachweis und
        gebuchten Zusatzpositionen (siehe `kosten_service.py`-Moduldoc)."""

        mahnfall = self._repository.get(mahnfall_id)
        if mahnfall is None:
            raise ValueError(f"Unbekannter MahnFall {mahnfall_id}")

        # Serverseitig neu laden statt Aufrufer-Objekten oder dem alten
        # Planungs-Snapshot zu vertrauen.
        vertrag = self._stammdaten_repository.get_vertrag(mahnfall.vertrag_id)
        konto = self._stammdaten_repository.get_konto_by_vertrag(mahnfall.vertrag_id)
        _validiere_bindung(mahnfall, vertrag, konto)

        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)

        if mahnfall.status != MahnStatus.GEPLANT.value:
            return VersandErgebnis("BEREITS_VERARBEITET", f"Status ist bereits {mahnfall.status}; kein Doppelversand.")

        self._stammdaten_repository.pruefe_vertrag_nicht_ausgeschlossen(vertrag.id)

        aktuelle_policy = self._mahn_policy_repository.aktuelle_freigegebene()
        status_code, grund, passende_forderung = self._pruefe_frisch_versandbereit(
            mahnfall=mahnfall, vertrag=vertrag, konto=konto, aktuelle_policy=aktuelle_policy, heute=heute,
            bank_bestaetigt_bis=bank_bestaetigt_bis, ungeklaerte_eingaenge_vorhanden=ungeklaerte_eingaenge_vorhanden,
        )
        if status_code == "BLOCKIERT_PERSIST":
            self._repository.set_status(mahnfall_id, MahnStatus.BLOCKIERT.value)
            return VersandErgebnis("BLOCKIERT", grund)
        if status_code == "UEBERSPRUNGEN_PERSIST":
            self._repository.set_status(mahnfall_id, MahnStatus.UEBERSPRUNGEN.value)
            return VersandErgebnis("UEBERSPRUNGEN", grund)
        if status_code == "BLOCKIERT_TRANSIENT":
            return VersandErgebnis("BLOCKIERT", grund)
        assert status_code is None  # bereit für Claim/Versand

        if not send_enabled:
            return VersandErgebnis("BEREITS_VERARBEITET", "SEND_ENABLED=false: nur Preview/Outbox, kein realer Versand.")

        if not self._repository.claim_fuer_versand(mahnfall_id):
            return VersandErgebnis("BEREITS_VERARBEITET", "Ein anderer Worker verarbeitet diesen Fall bereits.")

        try:
            beleg = versand_fn(mahnfall.snapshot)
        except (VersandUngewissError, TransportFehlerUngewissError):
            self._repository.set_status(mahnfall_id, MahnStatus.UNSICHER.value)
            return VersandErgebnis("UNSICHER", "Provider-Timeout nach möglicher Annahme; kein automatischer Retry.")
        except ValueError:
            self._repository.set_status(mahnfall_id, MahnStatus.BLOCKIERT.value)
            return VersandErgebnis("BLOCKIERT", "Mailauftrag oder private Mailkonfiguration unvollständig.")
        if nachweis_daten(beleg) is None:
            self._repository.set_status(mahnfall_id, MahnStatus.UNSICHER.value)
            return VersandErgebnis("UNSICHER", "Noch kein tatsächlicher Versandnachweis; Status wird abgefragt, nicht erneut gesendet.")
        versand_belegen(self._repository._session_factory, MahnFallTable, mahnfall_id,
            ergebnis=beleg, erlaubt={"IN_VERSAND", "UNSICHER"}, neuer_status="GESENDET", zeitfeld="gesendet_am",
            referenz="mahnung:" + mahnfall.outbox_key)

        # Mahnkosten (Verzugszinsen/Mahnspesen, Auftrag Markus 13.09.2026)
        # werden AUSSCHLIESSLICH hier, unmittelbar nach dem bestätigten
        # Versandnachweis, gebucht - nie bei einer Vorschau, einem
        # fehlgeschlagenen Versand oder einem Retry ohne neuen Nachweis
        # (siehe `kosten_service.py`-Moduldoc). Ein Fehler hier darf den
        # bereits abgeschlossenen Versand NICHT rückgängig machen - die
        # Mahnung ist unabhängig von der Kostenbuchung bereits gültig
        # zugestellt; ein Buchungsfehler wird geloggt/propagiert nicht
        # als Versandfehler.
        if self._mahnkosten_service is not None:
            vorschau = None
            if mahnkosten_vorschau_slot is not None and "vorschau" in mahnkosten_vorschau_slot:
                vorschau = mahnkosten_vorschau_slot["vorschau"]
            else:
                vorschau = self._mahnkosten_service.vorschau(vertrag_id=vertrag.id, stufe=mahnfall.stufe, heute=heute)
            if vorschau is not None:
                self._mahnkosten_service.buche_vorschau(
                    ctx=ctx, vorschau=vorschau, heute=heute,
                    versandnachweis_referenz="mahnung:" + mahnfall.outbox_key, akteur=ctx.user_id,
                )
        return VersandErgebnis("GESENDET", "Tatsächlicher Versand im Maildienst nachgewiesen.")

    def _freigebe_gebuendelte_mitglieder(self, mitglieder: list[MahnFallTable | None], *, ausser: int | None = None) -> None:
        """Setzt jedes noch GEBUENDELTE Mitglied (außer `ausser`, das
        bereits selbst einen konkreten Status erhalten hat) zurück auf
        GEPLANT - unabhängige Rückprüfung Codex 14.09.2026: scheitert
        die Vorprüfung EINES Gruppenmitglieds, bevor überhaupt ein
        tatsächlicher/unklarer Provideraufruf stattgefunden hat, dürfen
        die ÜBRIGEN, für sich genommen weiterhin versandfähigen
        Mitglieder NICHT für immer GEBUENDELT (und damit für jede
        künftige Planung unsichtbar) bleiben - eine neue
        `plane_mahnlauf`-Bildung mit dem dann aktuellen Stand holt sie
        wieder ab. Diese Freigabe ist NUR vor einem tatsächlichen
        Provideraufruf zulässig: sobald `versand_fn` aufgerufen wurde,
        werden Mitglieder NIE wieder auf GEPLANT zurückgesetzt (siehe
        die UNSICHER/BLOCKIERT-Zweige weiter unten in `versende_
        mahnlauf`), da zu diesem Zeitpunkt ein echter Versand bereits
        stattgefunden haben könnte."""

        for mahnfall in mitglieder:
            if mahnfall is not None and mahnfall.id != ausser and mahnfall.status == MahnStatus.GEBUENDELT.value:
                self._repository.set_status(mahnfall.id, MahnStatus.GEPLANT.value)

    def plane_mahnlauf(
        self,
        *,
        ctx: AuthContext,
        vertrag: VertragTable,
        konto: KontoTable,
        stufe: int,
        heute: date,
        bank_bestaetigt_bis: date | None,
        ungeklaerte_eingaenge_vorhanden: bool = False,
    ) -> MahnLaufTable | None:
        """Bildet die GENAU EINE, deterministische, tatsächlich
        sendeberechtigte Gruppe aller GEPLANTEN MahnFälle desselben
        (`vertrag`, `stufe`) - über dieselbe, bereits für den
        Einzelversand genutzte reine Prüfung `_pruefe_frisch_
        versandbereit` (KEINE kleinste-Id-Heuristik: JEDES Mitglied wird
        einzeln geprüft, NICHT nur eines stellvertretend - Rückprüfung
        14.09.2026, Risiko 2) und friert deren exakte Mitgliedermenge in
        einer persistenten `MahnLaufTable`-Zeile ein (`outbox_key`
        deterministisch aus Vertrag/Stufe/Mitgliedermenge - ein
        wiederholter Aufruf mit UNVERÄNDERTER Menge liefert dieselbe
        Zeile, idempotent). Mitglieder, die bereits jetzt dauerhaft
        nicht mehr versandfähig sind (Sperre, veraltete Forderung, ...),
        werden HIER bereits final auf BLOCKIERT/UEBERSPRUNGEN gesetzt -
        exakt wie es die bisherige Einzelfall-Prüfung in `versenden()`
        auch getan hätte. Gibt `None` zurück, wenn AKTUELL kein
        Mitglied sendebereit ist (nichts zu bündeln)."""

        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)
        if self._mahnlauf_repository is None:
            raise ValueError("plane_mahnlauf benötigt ein konfiguriertes MahnLaufRepository.")

        # Gibt es bereits eine NICHT-abgeschlossene (GEPLANT, also noch
        # nicht dispatchte) Gruppe für (Vertrag, Stufe)? Diese wird
        # UNVERÄNDERT zurückgegeben (Wiederaufnahme nach z. B. einem
        # Absturz zwischen `plane_mahnlauf` und `versende_mahnlauf") -
        # es wird NIE eine zweite, potenziell überlappende Gruppe für
        # denselben (Vertrag, Stufe) gebildet, solange eine bereits
        # offene existiert (echte Rückprüfung Codex 14.09.2026:
        # reproduzierter Doppelversand, siehe `MahnFallRepository.
        # claim_fuer_buendelung`-Docstring).
        bestehende_offene_gruppe = next(
            (m for m in self._mahnlauf_repository.list_fuer_vertrag(vertrag.id) if m.stufe == stufe and m.status == "GEPLANT"),
            None,
        )
        if bestehende_offene_gruppe is not None:
            return bestehende_offene_gruppe

        aktuelle_policy = self._mahn_policy_repository.aktuelle_freigegebene()
        kandidaten = [
            f for f in self._repository.list_fuer_vertrag(vertrag.id)
            if f.stufe == stufe and f.status == MahnStatus.GEPLANT.value
        ]
        bereit_ids: list[int] = []
        for mahnfall in kandidaten:
            status_code, _grund, _forderung = self._pruefe_frisch_versandbereit(
                mahnfall=mahnfall, vertrag=vertrag, konto=konto, aktuelle_policy=aktuelle_policy, heute=heute,
                bank_bestaetigt_bis=bank_bestaetigt_bis, ungeklaerte_eingaenge_vorhanden=ungeklaerte_eingaenge_vorhanden,
            )
            if status_code is None:
                bereit_ids.append(mahnfall.id)
            elif status_code == "BLOCKIERT_PERSIST":
                self._repository.set_status(mahnfall.id, MahnStatus.BLOCKIERT.value)
            elif status_code == "UEBERSPRUNGEN_PERSIST":
                self._repository.set_status(mahnfall.id, MahnStatus.UEBERSPRUNGEN.value)
            # BLOCKIERT_TRANSIENT (Frist noch nicht abgelaufen): bleibt
            # GEPLANT, ist nur (noch) nicht Teil DIESER Gruppe.

        if not bereit_ids:
            return None

        bereit_ids_sortiert = sorted(bereit_ids)
        mitglieder_hash = compute_content_hash({"mitglieder": bereit_ids_sortiert})
        # Der Kanal ist BEWUSST NICHT Teil des Schlüssels (siehe
        # `MahnLaufRepository`-Docstring): dieselbe Mitgliedermenge muss
        # kanalübergreifend auf dieselbe Sperre treffen.
        outbox_key = f"{vertrag.gesellschaft_id}:{vertrag.id}:{stufe}:{mitglieder_hash}"

        # Mitglieder-Claim (GEPLANT->GEBUENDELT) UND Gruppenzeilenanlage
        # in EINER Transaktion (Rückprüfung Codex 14.09.2026, siehe
        # `MahnLaufRepository.claim_mitglieder_und_erstelle_gruppe`-
        # Docstring) - ein Absturz zwischen beiden Schritten darf NIE
        # Mitglieder ohne zugehörige Gruppe zurücklassen.
        return self._mahnlauf_repository.claim_mitglieder_und_erstelle_gruppe(
            mahnfall_ids=bereit_ids_sortiert, outbox_key=outbox_key, vertrag_id=vertrag.id,
            gesellschaft_id=vertrag.gesellschaft_id, stufe=stufe,
        )

    def versende_mahnlauf(
        self,
        *,
        ctx: AuthContext,
        mahnlauf_id: int,
        heute: date,
        bank_bestaetigt_bis: date | None,
        ungeklaerte_eingaenge_vorhanden: bool,
        send_enabled: bool,
        versand_fn: Callable[[list[MahnFallTable]], object],
        mahnkosten_vorschau_slot: dict | None = None,
    ) -> VersandErgebnis:
        """Versendet EINEN bereits über `plane_mahnlauf` gebildeten,
        eingefrorenen Mahnlauf GENAU EINMAL - `versand_fn` wird mit der
        vollständigen Mitgliederliste (nicht nur einem "führenden"
        Fall) aufgerufen. Die atomare Exklusivität hängt an der
        `MahnLaufTable`-Zeile SELBST (`claim_fuer_versand`), NICHT an
        einem einzelnen Mitglied - ein zweiter, gleichzeitiger Aufruf
        (auch über einen künftigen anderen Kanal) für DIESELBE Gruppe
        sieht entweder `mahnlauf.status != GEPLANT` (bereits
        IN_VERSAND/UNSICHER/GESENDET) oder verliert den CAS.

        JEDES eingefrorene Mitglied wird UNMITTELBAR vor dem Versand
        NOCHMAL einzeln mit `_pruefe_frisch_versandbereit` geprüft; ist
        auch nur eines nicht mehr bereit (z. B. eine inzwischen
        eingegangene Zahlung), wird die GESAMTE Gruppe blockiert statt
        eine Teilmenge zu versenden - ein neuer `plane_mahnlauf`-Aufruf
        bildet dann die aktuell tatsächlich passende, neue Gruppe."""

        mahnlauf = self._mahnlauf_repository.get(mahnlauf_id)
        if mahnlauf is None:
            raise ValueError(f"Unbekannter MahnLauf {mahnlauf_id}")

        vertrag = self._stammdaten_repository.get_vertrag(mahnlauf.vertrag_id)
        konto = self._stammdaten_repository.get_konto_by_vertrag(mahnlauf.vertrag_id)
        if vertrag is None or konto is None or vertrag.gesellschaft_id != mahnlauf.gesellschaft_id:
            raise BindungInkonsistentError(f"MahnLauf {mahnlauf.id}: Vertrag/Konto/Gesellschaft inkonsistent.")

        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)

        if mahnlauf.status != "GEPLANT":
            return VersandErgebnis(
                "BEREITS_VERARBEITET",
                f"Mahnlauf-Status ist bereits {mahnlauf.status}; kein zweiter/kanalübergreifender Versand für dieselbe Gruppe.",
            )

        self._stammdaten_repository.pruefe_vertrag_nicht_ausgeschlossen(vertrag.id)

        mitglieder_ids = MahnLaufRepository.mitglieder_ids(mahnlauf)
        mitglieder = [self._repository.get(i) for i in mitglieder_ids]
        if not mitglieder_ids or any(m is None for m in mitglieder):
            self._mahnlauf_repository.set_status(
                mahnlauf_id, "BLOCKIERT", fehlergrund="Mindestens ein eingefrorenes Gruppenmitglied existiert nicht mehr."
            )
            self._freigebe_gebuendelte_mitglieder(mitglieder)
            return VersandErgebnis("BLOCKIERT", "Mahnlauf-Gruppe inkonsistent (Mitglied fehlt) - neue Planung erforderlich.")

        aktuelle_policy = self._mahn_policy_repository.aktuelle_freigegebene()
        for mahnfall in mitglieder:
            if mahnfall.status != MahnStatus.GEBUENDELT.value:
                self._mahnlauf_repository.set_status(
                    mahnlauf_id, "BLOCKIERT",
                    fehlergrund=f"Mitglied {mahnfall.id} ist nicht mehr GEBUENDELT (Status {mahnfall.status}).",
                )
                self._freigebe_gebuendelte_mitglieder(mitglieder, ausser=mahnfall.id)
                return VersandErgebnis("BLOCKIERT", "Ein Gruppenmitglied hat seinen Status seit der Planung verändert - neue Planung erforderlich.")
            status_code, grund, _forderung = self._pruefe_frisch_versandbereit(
                mahnfall=mahnfall, vertrag=vertrag, konto=konto, aktuelle_policy=aktuelle_policy, heute=heute,
                bank_bestaetigt_bis=bank_bestaetigt_bis, ungeklaerte_eingaenge_vorhanden=ungeklaerte_eingaenge_vorhanden,
            )
            if status_code is not None:
                if status_code == "BLOCKIERT_PERSIST":
                    self._repository.set_status(mahnfall.id, MahnStatus.BLOCKIERT.value)
                elif status_code == "UEBERSPRUNGEN_PERSIST":
                    self._repository.set_status(mahnfall.id, MahnStatus.UEBERSPRUNGEN.value)
                else:
                    # BLOCKIERT_TRANSIENT ("Frist noch nicht abgelaufen"):
                    # dieses Mitglied bekommt KEINEN dauerhaften Status -
                    # es wird stattdessen selbst auf GEPLANT zurückgesetzt,
                    # damit eine spätere Planung es erneut berücksichtigt,
                    # sobald es tatsächlich fällig ist.
                    self._repository.set_status(mahnfall.id, MahnStatus.GEPLANT.value)
                self._mahnlauf_repository.set_status(
                    mahnlauf_id, "BLOCKIERT", fehlergrund=f"Mitglied {mahnfall.id}: {grund}"
                )
                # Die ÜBRIGEN, unauffälligen Mitglieder werden ebenfalls
                # freigegeben (Rückprüfung Codex 14.09.2026: sonst
                # blieben sie für immer GEBUENDELT/unsichtbar, obwohl
                # noch KEIN Provideraufruf stattgefunden hat und sie für
                # sich genommen weiterhin versandfähig sein könnten).
                self._freigebe_gebuendelte_mitglieder(mitglieder, ausser=mahnfall.id)
                return VersandErgebnis(
                    "BLOCKIERT",
                    f"Gruppe ist seit der Planung nicht mehr vollständig versandbereit (Mitglied {mahnfall.id}: {grund}); "
                    "neue Planung erforderlich.",
                )

        if not send_enabled:
            return VersandErgebnis("BEREITS_VERARBEITET", "SEND_ENABLED=false: nur Preview/Outbox, kein realer Versand.")

        if not self._mahnlauf_repository.claim_fuer_versand(mahnlauf_id):
            return VersandErgebnis("BEREITS_VERARBEITET", "Ein anderer Worker/Kanal verarbeitet diese Gruppe bereits.")

        try:
            beleg = versand_fn(mitglieder)
        except (VersandUngewissError, TransportFehlerUngewissError):
            self._mahnlauf_repository.set_status(mahnlauf_id, "UNSICHER")
            for mahnfall in mitglieder:
                self._repository.set_status(mahnfall.id, MahnStatus.UNSICHER.value)
            return VersandErgebnis("UNSICHER", "Provider-Timeout nach möglicher Annahme; kein automatischer Retry.")
        except ValueError as exc:
            self._mahnlauf_repository.set_status(mahnlauf_id, "BLOCKIERT", fehlergrund=str(exc))
            for mahnfall in mitglieder:
                self._repository.set_status(mahnfall.id, MahnStatus.BLOCKIERT.value)
            return VersandErgebnis("BLOCKIERT", "Mailauftrag oder private Mailkonfiguration unvollständig.")
        if nachweis_daten(beleg) is None:
            self._mahnlauf_repository.set_status(mahnlauf_id, "UNSICHER")
            for mahnfall in mitglieder:
                self._repository.set_status(mahnfall.id, MahnStatus.UNSICHER.value)
            return VersandErgebnis("UNSICHER", "Noch kein tatsächlicher Versandnachweis; Status wird abgefragt, nicht erneut gesendet.")

        versand_belegen(
            self._mahnlauf_repository._session_factory, MahnLaufTable, mahnlauf_id,
            ergebnis=beleg, erlaubt={"IN_VERSAND", "UNSICHER"}, neuer_status="GESENDET", zeitfeld="gesendet_am",
            referenz="mahnungslauf:" + mahnlauf.outbox_key,
        )
        # Jedes Mitglied wurde bereits bei `plane_mahnlauf` atomar auf
        # GEBUENDELT geclaimt (siehe `MahnFallRepository.
        # claim_fuer_buendelung`) - der Übergang GEBUENDELT -> GESENDET
        # erfolgt hier direkt, je Mitglied einzeln protokolliert (eigener
        # Audit-Eintrag, eigenes `gesendet_am`).
        for mahnfall in mitglieder:
            versand_belegen(
                self._repository._session_factory, MahnFallTable, mahnfall.id,
                ergebnis=beleg, erlaubt={"GEBUENDELT"}, neuer_status="GESENDET", zeitfeld="gesendet_am",
                referenz="mahnungslauf:" + mahnlauf.outbox_key,
            )

        if self._mahnkosten_service is not None:
            vorschau = None
            if mahnkosten_vorschau_slot is not None and "vorschau" in mahnkosten_vorschau_slot:
                vorschau = mahnkosten_vorschau_slot["vorschau"]
            else:
                # An die eingefrorene Gruppe gebunden (siehe
                # `kosten_service.py::vorschau`-Docstring, Rückprüfung
                # Codex 14.09.2026) - keine Kosten auf andere, nicht Teil
                # dieses Mahnlaufs seiende offene Forderungen desselben
                # Vertrags.
                vorschau = self._mahnkosten_service.vorschau(
                    vertrag_id=vertrag.id, stufe=mahnlauf.stufe, heute=heute,
                    nur_op_position_ids=frozenset(m.forderung_op_position_id for m in mitglieder),
                )
            if vorschau is not None:
                self._mahnkosten_service.buche_vorschau(
                    ctx=ctx, vorschau=vorschau, heute=heute,
                    versandnachweis_referenz="mahnungslauf:" + mahnlauf.outbox_key, akteur=ctx.user_id,
                )
        return VersandErgebnis("GESENDET", "Tatsächlicher Versand im Maildienst nachgewiesen (gebündelter Mahnlauf).")

    def markiere_verwaiste_mahnlaeufe_als_unsicher(
        self, *, jetzt: datetime | None = None, max_alter: timedelta = timedelta(minutes=15),
    ) -> list[MahnLaufTable]:
        """Recovery-Pendant zu `markiere_verwaiste_als_unsicher` für
        gebündelte Mahnläufe: eine seit `max_alter` in IN_VERSAND
        feststeckende Gruppe (Absturz zwischen Claim und Ergebnis) wird
        NIE automatisch erneut versucht, sondern auf UNSICHER gesetzt -
        das blockiert (siehe `versende_mahnlauf`) weiterhin JEDEN Kanal
        für dieselbe Mitgliedermenge, bis sie manuell geklärt ist.

        Setzt ZUSÄTZLICH jedes noch GEBUENDELTE Mitglied dieser Gruppe
        ebenfalls auf UNSICHER (Rückprüfung Codex 14.09.2026): sonst
        blieben diese Forderungen für IMMER GEBUENDELT und damit für
        JEDE künftige `plane_mahnlauf`-Kandidatenauswahl unsichtbar -
        eine abgestürzte Gruppe würde ihre Mitglieder sonst dauerhaft
        "verschlucken", ohne sie gleichzeitig tatsächlich zu versenden."""

        if self._mahnlauf_repository is None:
            return []
        grenze = (jetzt or datetime.now(timezone.utc)) - max_alter
        verwaiste = self._mahnlauf_repository.verwaiste_in_versand(aelter_als=grenze)
        for lauf in verwaiste:
            self._mahnlauf_repository.set_status(lauf.id, "UNSICHER")
            for mahnfall_id in MahnLaufRepository.mitglieder_ids(lauf):
                mahnfall = self._repository.get(mahnfall_id)
                if mahnfall is not None and mahnfall.status == MahnStatus.GEBUENDELT.value:
                    self._repository.set_status(mahnfall_id, MahnStatus.UNSICHER.value)
        return verwaiste

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

    def manuell_abklaeren(self, *, mahnfall_id: int, neuer_status: MahnStatus, versandnachweis=None) -> MahnFallTable:
        """Löst einen UNSICHER-Fall gezielt manuell auf (z. B. nach Rückfrage
        beim Provider), statt ihn blind erneut zu versenden."""

        if neuer_status is MahnStatus.GESENDET:
            row = self._repository.get(mahnfall_id)
            if row is None:
                raise ValueError("Mahnfall fehlt.")
            versand_belegen(self._repository._session_factory, MahnFallTable, row.id,
                ergebnis=versandnachweis, erlaubt={"UNSICHER"}, neuer_status="GESENDET",
                zeitfeld="gesendet_am", referenz="mahnung:" + row.outbox_key)
            return self._repository.get(row.id)
        if neuer_status is MahnStatus.GEPLANT:
            raise ValueError("Unklarer Versand wird nicht zurück auf GEPLANT gesetzt; zuerst Providerstatus klären.")
        return self._repository.set_status(mahnfall_id, neuer_status.value)
