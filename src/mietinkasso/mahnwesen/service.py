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
from mietinkasso.domain.enums import ZUGANGSFORMEN_ALLE, ZUGANGSFORMEN_AUSREICHEND, MahnStatus, MahnStufe, rechtsordnung_geklaert
from mietinkasso.domain.exceptions import BindungInkonsistentError, CrossTenantError, MahnstufeReihenfolgeError, QuellenbelegFehltError
from mietinkasso.infrastructure.db.tables import KontoTable, MahnFallTable, MahnLaufTable, MahnPolicyTable, VertragTable
from mietinkasso.mahnwesen.fristen import frist_abgelaufen, fristende_ab_zugang
from mietinkasso.mahnwesen.kosten import snapshot_aus_json, snapshot_zu_json
from mietinkasso.mahnwesen.repository import MahnEmpfaengerZugangRepository, MahnFallRepository, MahnLaufRepository, MahnPolicyRepository
from mietinkasso.op.service import OffeneForderung, OPService, compute_content_hash
from mietinkasso.stammdaten.repository import StammdatenRepository
from mietinkasso.indexautomatik.mailnachweis import beleg_aus_bestaetigtem_audit, nachweis_daten, versand_belegen
from mietinkasso.domain.exceptions import TransportFehlerUngewissError
from mietinkasso.indexautomatik.zeit import heute_wien

#: Auftrag HV-20261020-VIER-MAHNSTUFEN: Feld auf `MahnPolicyTable`, das
#: die Mindesttage-ab-Zugang-Frist für den Übergang AUF Stufe `N`
#: (`N` in 2, 3, 4) trägt - siehe `MahnwesenService._mindesttage_fuer_
#: uebergang`.
_MINDESTTAGE_ZUGANG_FELD_JE_ZIELSTUFE = {
    2: "stufe2_mindesttage_nach_stufe1_zugang",
    3: "stufe3_mindesttage_nach_stufe2_zugang",
    4: "stufe4_mindesttage_nach_stufe3_zugang",
}


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
        kanalregel_repository=None,
        brief_anbieterprofil_repository=None,
        brief_transport_verfuegbar: bool = False,
        mitmieter_repository=None,
        empfaenger_zugang_repository: MahnEmpfaengerZugangRepository | None = None,
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
        # Kanalregel-Paket (Auftrag HV-20260914-MAHNUNG-BRIEF, Nutzer-
        # entscheidung 14.09.2026: "Stufe 1 EMAIL, Stufe 2 BRIEF") - ALLE
        # DREI optional und standardmäßig unwirksam, damit jeder
        # bestehende Aufrufer/Test OHNE Anpassung exakt das bisherige
        # Verhalten (EMAIL für beide Stufen) behält, bis Codex eine
        # Kanalregel tatsächlich anlegt+freigibt UND den echten Brief-
        # transport bereitstellt (`brief_transport_verfuegbar`bleibt in
        # dieser Umgebung IMMER `False` - kein erfundener Live-Provider,
        # EinfachBrief sFTP/API ist noch nicht freigeschaltet). Siehe
        # `_resolve_kanal`/`_pruefe_frisch_versandbereit`.
        self._kanalregel_repository = kanalregel_repository
        self._brief_anbieterprofil_repository = brief_anbieterprofil_repository
        self._brief_transport_verfuegbar = brief_transport_verfuegbar
        # Auftrag HV-20261020-VIER-MAHNSTUFEN: beide optional, Default
        # `None` - ohne konfiguriertes Repository bleibt das Verhalten
        # UNVERÄNDERT (kein Mitmieter-/Zugangsnachweis-Tracking, exakt
        # wie vor diesem Auftrag), siehe `_vervollstaendige_bestaetigten_
        # mahnlauf`/`zugang_bestaetigen_fuer_empfaenger`.
        self._mitmieter_repository = mitmieter_repository
        self._empfaenger_zugang_repository = empfaenger_zugang_repository

    def _resolve_kanal(self, stufe: int) -> str:
        """Liefert den für `stufe` (1-4) tatsächlich zu verwendenden
        Versandkanal ("EMAIL" | "BRIEF"). Default für ALLE Stufen bleibt
        "EMAIL", solange kein `kanalregel_repository` konfiguriert ist
        ODER keine `MahnKanalregelTable`-Version FREIGEGEBEN ist - das
        erhält 100%ige Rückwärtskompatibilität mit dem bisherigen,
        produktiv laufenden Verhalten (Stufe 2 sendet aktuell immer per
        E-Mail); die Nutzerentscheidung "Stufe1=EMAIL/Stufe2=BRIEF"
        entfaltet erst Wirkung, sobald Codex eine Kanalregel explizit
        anlegt UND freigibt - ein stillschweigender Default-Wechsel
        würde den heute funktionierenden Stufe-2-E-Mail-Versand
        andernfalls unangekündigt brechen, solange der Briefkanal noch
        keinen echten Transport hat.

        **Korrektur nach unabhängiger Abnahme (Codex, 20.10.2026):** die
        vorherige Fassung kannte nur `stufe1_kanal`/`stufe2_kanal` und
        lieferte für Stufe 3/4 FÄLSCHLICH immer `stufe2_kanal` zurück -
        die mit Auftrag HV-20261020-VIER-MAHNSTUFEN hinzugefügten Felder
        `stufe3_kanal`/`stufe4_kanal` (`MahnKanalregelTable`) wurden dabei
        ignoriert. Eine freigegebene Regel mit z. B.
        Stufe1=EMAIL/Stufe2=EMAIL/Stufe3=BRIEF/Stufe4=BRIEF sendete Stufe
        3/4 dadurch weiterhin per E-Mail statt per Brief."""

        if self._kanalregel_repository is None:
            return "EMAIL"
        regel = self._kanalregel_repository.aktuelle_freigegebene()
        if regel is None:
            return "EMAIL"
        kanal_je_stufe = {
            1: regel.stufe1_kanal,
            2: regel.stufe2_kanal,
            3: regel.stufe3_kanal,
            4: regel.stufe4_kanal,
        }
        return kanal_je_stufe[stufe]

    def _naechste_stufe_fuer_forderung(self, forderung_op_position_id: int, *, policy: MahnPolicyTable) -> MahnStufe | None:
        """Auftrag HV-20261020-VIER-MAHNSTUFEN: verallgemeinert von genau
        zwei auf bis zu vier Stufen - `policy.vier_stufen_profil_aktiv`
        entscheidet die oberste erreichbare Stufe (`max_stufe`). Für eine
        ALTE, NICHT auf das Vier-Stufen-Profil umgestellte Policy ist
        `max_stufe == 2` und das Verhalten IDENTISCH zur bisherigen
        Zwei-Stufen-Logik (kein stillschweigender Verhaltenswechsel für
        bereits laufende Mahnzyklen): existiert bereits ein MahnFall für
        die oberste erreichbare Stufe, wird NIE weiter eskaliert
        ("nach der letzten Stufe nur noch interner Bearbeitungsfall"),
        unabhängig vom Status dieses letzten Falls."""

        letzter = self._repository.letzter_mahnfall_fuer_forderung(forderung_op_position_id)
        if letzter is None:
            return MahnStufe.STUFE_1
        max_stufe = 4 if policy.vier_stufen_profil_aktiv else 2
        if letzter.stufe >= max_stufe:
            return None
        if letzter.status != MahnStatus.GESENDET.value:
            return MahnStufe(letzter.stufe)
        return MahnStufe(letzter.stufe + 1)

    def _bankstand_veraltet(self, *, heute: date, bank_bestaetigt_bis: date | None) -> bool:
        if bank_bestaetigt_bis is None:
            return True
        return (heute - bank_bestaetigt_bis).days > self._bank_stand_max_age_days

    def _pruefe_vorstufe_abstand(
        self, *, forderung_op_position_id: int, stufe: MahnStufe, policy: MahnPolicyTable,
        vertrag: VertragTable, heute: date,
    ) -> tuple[str, str] | None:
        """Prüft den Mindestabstand zur UNMITTELBAR VORHERIGEN Stufe für
        `stufe` (2, 3 oder 4) - `None`, wenn der Abstand bereits erreicht
        ist, sonst `("ZU_FRUEH", grund)`. Wirft `MahnstufeReihenfolgeError`,
        wenn die Vorstufe nicht nachweislich GESENDET ist (strukturelle
        Inkonsistenz - "Stufe N verlangt eine erfolgreich gesendete
        Stufe N-1" -, kein normaler "noch zu früh"-Fall).

        Auftrag HV-20261020-VIER-MAHNSTUFEN: unter `policy.vier_stufen_
        profil_aktiv` zählt die Frist AB BESTÄTIGTEM ZUGANG (§902/903
        ABGB, `mahnwesen/fristen.py::fristende_ab_zugang`) - NICHT mehr
        ab dem bloßen Versanddatum (Nutzervorgabe wörtlich: "Jede Stufe
        hat volle 14 Tage ab nachgewiesenem Zugang"). Ein Mahnlauf OHNE
        bestätigten Zugang ALLER erwarteten Empfänger (`MahnLaufTable.
        zugang_bestaetigt_am`, siehe `zugang_bestaetigen_fuer_
        empfaenger`) bleibt deshalb strukturell "noch zu früh", statt
        (wie bei der alten Zwei-Stufen-Logik) automatisch anhand des
        Versanddatums weiterzulaufen - "ein gewöhnlicher Mail-/SMTP-
        Versand oder eine bloße Brief-Einlieferung ist nicht automatisch
        ein rechtssicherer Zugang" (Nutzervorgabe). Für eine Policy OHNE
        aktiviertes Vier-Stufen-Profil (`vier_stufen_profil_aktiv ==
        False`, dort NUR für `stufe == 2` überhaupt erreichbar) bleibt
        die ALTE, versandzeitpunktbasierte Logik unverändert - kein
        stillschweigender Verhaltenswechsel für bereits laufende
        Zwei-Stufen-Mahnzyklen."""

        vorstufe_wert = stufe.value - 1
        letzte_vorstufe = self._repository.letzter_mahnfall_je_stufe_fuer_forderung(forderung_op_position_id, vorstufe_wert)
        if letzte_vorstufe is None or letzte_vorstufe.status != MahnStatus.GESENDET.value or letzte_vorstufe.gesendet_am is None:
            raise MahnstufeReihenfolgeError(
                f"Forderung {forderung_op_position_id}: Stufe {stufe.value} verlangt eine erfolgreich "
                f"gesendete Stufe {vorstufe_wert}."
            )

        if not policy.vier_stufen_profil_aktiv:
            tage_seit_versand = (heute - _versandtag_wien(letzte_vorstufe.gesendet_am)).days
            mindest_tage = max(policy.stufe2_mindesttage_nach_stufe1_versand, vertrag.zahlungsfrist_tage)
            if tage_seit_versand < mindest_tage:
                return "ZU_FRUEH", f"Mindestabstand zu Stufe {vorstufe_wert} noch nicht erreicht ({tage_seit_versand}/{mindest_tage} Tagen)."
            return None

        mahnlauf = None
        if self._mahnlauf_repository is not None and letzte_vorstufe.mahnlauf_id is not None:
            mahnlauf = self._mahnlauf_repository.get(letzte_vorstufe.mahnlauf_id)
        if mahnlauf is None or mahnlauf.zugang_bestaetigt_am is None:
            return "ZU_FRUEH", (
                f"Zugang des Schreibens zu Stufe {vorstufe_wert} ist noch nicht für ALLE erwarteten "
                "Empfänger bestätigt - die Frist zur nächsten Stufe läuft erst ab bestätigtem Zugang, "
                "nie automatisch ab dem Versanddatum."
            )
        mindest_tage = getattr(policy, _MINDESTTAGE_ZUGANG_FELD_JE_ZIELSTUFE[stufe.value])
        if not frist_abgelaufen(zugang_am=mahnlauf.zugang_bestaetigt_am, frist_tage=mindest_tage, heute=heute):
            fristende = fristende_ab_zugang(mahnlauf.zugang_bestaetigt_am, mindest_tage)
            return "ZU_FRUEH", (
                f"Die {mindest_tage}-Tage-Frist ab bestätigtem Zugang (§902/903 ABGB, Werktags-"
                f"verschiebung berücksichtigt) endet erst am {fristende.isoformat()}; heute ist "
                f"{heute.isoformat()}."
            )
        return None

    def _pruefe_forderung_planbar(
        self, *, vertrag: VertragTable, konto: KontoTable, forderung: OffeneForderung, policy: MahnPolicyTable,
        heute: date, bank_bestaetigt_bis: date | None, ungeklaerte_eingaenge_vorhanden: bool,
    ) -> tuple[str, str, MahnStufe | None, dict | None]:
        """Reine Prüfung OHNE Nebenwirkung ALLER Planungsvoraussetzungen
        für GENAU EINE Forderung (Auftrag Markus 14.09.2026: "GET
        /vertrag/{id}/mahnvorschau ruft plane_forderung auf und schreibt
        dabei Mahnfälle - die neue Vorschau soll ohne Datenänderung
        funktionieren"). Von `plane_forderung` (schreibt bei "GEPLANT"
        tatsächlich einen `MahnFallTable`-Eintrag) UND `vorschau_
        forderung` (reine Lesevorschau, NIE ein `get_or_create`) GEMEINSAM
        verwendet, damit beide NIE auseinanderlaufen können - exakt
        dasselbe Prinzip wie `_pruefe_frisch_versandbereit` für den
        Versand.

        Rückgabe `(status, grund, stufe, snapshot)`: `status` ist
        "GEPLANT" nur, wenn tatsächlich geplant werden DÜRFTE - ob das
        auch tatsächlich geschieht (ein `MahnFallTable`-Eintrag
        angelegt/aktualisiert wird), entscheidet AUSSCHLIESSLICH der
        Aufrufer. `stufe`/`snapshot` sind nur bei `status == "GEPLANT"`
        gesetzt."""

        if not rechtsordnung_geklaert(vertrag.rechtsordnung):
            return (
                "BLOCKIERT",
                "Rechtsordnung des Vertrags ist UNGEKLAERT; Mahnung gesperrt, bis die rechtliche "
                "Einordnung feststeht.",
                None, None,
            )

        aktive_sperren = self._stammdaten_repository.aktive_sperren(vertrag.id)
        if aktive_sperren:
            gruende = ", ".join(s.grund for s in aktive_sperren)
            return "BLOCKIERT", f"Aktive Sperre(n): {gruende}", None, None

        if self._bankstand_veraltet(heute=heute, bank_bestaetigt_bis=bank_bestaetigt_bis):
            return (
                "BLOCKIERT",
                f"Keine ausreichend aktuelle BESTÄTIGTE Bankvollständigkeit vorhanden "
                f"(bestätigt bis: {bank_bestaetigt_bis}, max {self._bank_stand_max_age_days} Tage alt). "
                "Das Datum der letzten importierten Zeile allein beweist keine Vollständigkeit.",
                None, None,
            )
        if ungeklaerte_eingaenge_vorhanden:
            return (
                "BLOCKIERT",
                "Es gibt ungeklärte oder nur teilzugeordnete Bankeingänge, die diesen Vertrag betreffen "
                "könnten; Mahnung bleibt geschlossen, bis das aufgeklärt ist.",
                None, None,
            )

        debitor = self._stammdaten_repository.get_debitor(konto.debitor_id)
        if debitor is None:
            return "BLOCKIERT", f"Kein Debitor {konto.debitor_id} hinterlegt.", None, None

        if not forderung.faelligkeit_bekannt or forderung.faelligkeit is None:
            return "KEIN_BETRAG", "Fälligkeit unbekannt (z. B. Gesamtsaldo-Eröffnung); wird nie automatisch gemahnt.", None, None
        if forderung.rest_cent <= 0:
            return "KEIN_BETRAG", "Forderung ist bereits ausgeglichen.", None, None

        stufe = self._naechste_stufe_fuer_forderung(forderung.op_position_id, policy=policy)
        if stufe is None:
            letzte_erreichbare_stufe = 4 if policy.vier_stufen_profil_aktiv else 2
            return (
                "BLOCKIERT",
                f"Nach Stufe {letzte_erreichbare_stufe} ist nur ein interner Bearbeitungsfall zulässig, "
                "keine weitere automatische Mahnstufe.",
                None, None,
            )

        # Empfänger-Voraussetzung hängt vom für DIESE Stufe tatsächlich
        # aufgelösten Kanal ab (siehe `_resolve_kanal`) - ERST HIER
        # geprüft, weil sie die Stufe kennen muss (Default bleibt EMAIL
        # für beide Stufen, solange keine Kanalregel freigegeben ist,
        # siehe dortiger Docstring).
        kanal = self._resolve_kanal(stufe.value)
        if kanal == "BRIEF":
            if not debitor.postadresse_geprueft:
                return (
                    "BLOCKIERT",
                    f"Kein geprüfter Postadressen-Nachweis für Debitor {konto.debitor_id} hinterlegt (Kanal BRIEF).",
                    None, None,
                )
        elif not (debitor.email or "").strip():
            return "BLOCKIERT", f"Kein gültiger Empfänger (E-Mail) für Debitor {konto.debitor_id} hinterlegt.", None, None

        if stufe is MahnStufe.STUFE_1:
            faellig_seit_tagen = (heute - forderung.faelligkeit).days
            if faellig_seit_tagen < policy.stufe1_tage_nach_faelligkeit:
                return (
                    "ZU_FRUEH",
                    f"Erst {faellig_seit_tagen}/{policy.stufe1_tage_nach_faelligkeit} Tage seit Fälligkeit vergangen.",
                    None, None,
                )
        else:
            ergebnis = self._pruefe_vorstufe_abstand(
                forderung_op_position_id=forderung.op_position_id, stufe=stufe, policy=policy, vertrag=vertrag, heute=heute,
            )
            if ergebnis is not None:
                status, grund = ergebnis
                return status, grund, None, None

        snapshot = {
            "vertrag_id": vertrag.id,
            "debitor_id": konto.debitor_id,
            "kanal": kanal,
            "empfaenger_email": debitor.email,
            "empfaenger_name": debitor.name,
            "forderung_op_position_id": forderung.op_position_id,
            "betrag_cent": forderung.rest_cent,
            "stufe": stufe.value,
            "policy_version": policy.version,
            "zinsen_prozent": str(policy.zinsen_prozent),
            "gebuehr_cent": policy.gebuehr_cent,
        }
        return "GEPLANT", "Geplant.", stufe, snapshot

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

        status, grund, stufe, snapshot = self._pruefe_forderung_planbar(
            vertrag=vertrag, konto=konto, forderung=forderung, policy=policy, heute=heute,
            bank_bestaetigt_bis=bank_bestaetigt_bis, ungeklaerte_eingaenge_vorhanden=ungeklaerte_eingaenge_vorhanden,
        )
        if status != "GEPLANT":
            return PlanungsErgebnis(status, None, None, forderung.op_position_id, grund)

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
            snapshot=snapshot,
        )
        return PlanungsErgebnis("GEPLANT", mahnfall.id, mahnfall.stufe, forderung.op_position_id, "Geplant.")

    def vorschau_forderung(
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
        """Reine Lesevorschau OHNE JEDE Datenbankänderung (Auftrag Markus
        14.09.2026: "die neue Vorschau soll ohne Datenänderung
        funktionieren, dauerhaftes Planen per ausdrücklich betätigtem
        POST") - identische Entscheidungslogik wie `plane_forderung`
        (`_pruefe_forderung_planbar`), aber `mahnfall_id` bleibt IMMER
        `None`, auch im GEPLANT-Fall - es wird NIE ein `MahnFallTable`-
        Eintrag angelegt. Ein tatsächliches, dauerhaftes Planen braucht
        den separaten, CSRF-geschützten POST-Endpunkt
        `/vertrag/{id}/mahnfall/planen`. Dieselben Zugriffsprüfungen wie
        `plane_forderung` außer `require_schreibrecht` (reine Lesevorschau
        braucht kein Schreibrecht)."""

        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        if konto.vertrag_id != vertrag.id:
            raise BindungInkonsistentError(f"Konto {konto.id} gehört nicht zu Vertrag {vertrag.id}.")
        self._stammdaten_repository.pruefe_vertrag_nicht_ausgeschlossen(vertrag.id)
        if policy.status != "FREIGEGEBEN":
            raise PolicyNichtFreigegebenError(
                f"MahnPolicy Version {policy.version} ist im Status {policy.status}; nur eine FREIGEGEBENE "
                "Policy darf Mahnfälle planen."
            )

        status, grund, stufe, _snapshot = self._pruefe_forderung_planbar(
            vertrag=vertrag, konto=konto, forderung=forderung, policy=policy, heute=heute,
            bank_bestaetigt_bis=bank_bestaetigt_bis, ungeklaerte_eingaenge_vorhanden=ungeklaerte_eingaenge_vorhanden,
        )
        if status != "GEPLANT":
            return PlanungsErgebnis(status, None, None, forderung.op_position_id, grund)

        # Reiner LESE-Zugriff (KEIN `get_or_create`): existiert bereits ein
        # zuvor über den POST-Endpunkt tatsächlich geplanter Fall für
        # GENAU diese Forderung/Stufe (derselbe deterministische
        # `outbox_key`), wird dessen Id angezeigt, damit die Vorschau nach
        # dem Planen weiterhin auf den bestehenden Fall (Sendebereitschaft/
        # Versand) verweisen kann - das ist ein Lesen bereits persistierter
        # Daten, keine neue Datenänderung.
        outbox_key = f"{vertrag.gesellschaft_id}:{vertrag.id}:{forderung.op_position_id}:{stufe.value}"
        bestehender = self._repository.get_by_outbox_key(outbox_key)
        mahnfall_id = bestehender.id if bestehender is not None else None
        return PlanungsErgebnis("GEPLANT", mahnfall_id, stufe.value, forderung.op_position_id, grund)

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
        kanal: str = "EMAIL",
    ) -> tuple[str | None, str, OffeneForderung | None]:
        """Reine Prüfung OHNE Nebenwirkung aller unmittelbar-vor-Versand
        Bedingungen für EINEN MahnFall - von `versenden()` (das je nach
        Ergebnis selbst `set_status` aufruft) UND von der reinen
        Batch-Vorprüfung für die Mahnlauf-Bündelung in
        `indexautomatik/mailversand_service.py` gemeinsam genutzt, damit
        beide NIE auseinanderlaufen können.

        `kanal`: der für DIESEN Mahnfall tatsächlich maßgebliche Kanal -
        bei `versende_mahnlauf` der bereits eingefrorene `mahnlauf.kanal`
        der Gruppe, bei `plane_mahnlauf` der für die Stufe frisch über
        `_resolve_kanal` aufgelöste Kanal (die Gruppe existiert dort noch
        nicht), bei `versenden()` der im Planungssnapshot eingefrorene
        Kanal (Default "EMAIL" für ältere, vor diesem Auftrag angelegte
        Snapshots ohne den Schlüssel). Bestimmt, ob die Empfänger-
        Voraussetzung E-Mail (`kanal=="EMAIL"`) oder geprüfte Postadresse
        (`kanal=="BRIEF"`, zusätzlich ein freigegebenes Brief-
        Anbieterprofil UND tatsächlich angebundener Transport) ist.

        Rückgabe `(status_code, grund, passende_forderung)`:
        `status_code` ist `None`, wenn alles bereit ist (der Aufrufer darf
        claimen/senden); sonst `"BLOCKIERT_PERSIST"`/`"UEBERSPRUNGEN_PERSIST"`
        (der Fall wird dauerhaft auf diesen Status gesetzt) oder
        `"BLOCKIERT_TRANSIENT"` (bloß "noch nicht fällig"/"Briefkanal
        systemisch noch nicht versandbereit" - bleibt GEPLANT, kann bei
        einem späteren Lauf alleine oder gemeinsam mit anderen
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
        # Mitziehen. Der NAME wird kanalunabhängig verglichen (Identitäts-
        # prüfung); E-Mail nur für Kanal EMAIL, Postadresse nur für BRIEF.
        debitor = self._stammdaten_repository.get_debitor(konto.debitor_id)
        if debitor is None:
            return "BLOCKIERT_PERSIST", f"Kein Debitor {konto.debitor_id} mehr hinterlegt.", None
        geplanter_name = mahnfall.snapshot.get("empfaenger_name")
        if debitor.name != geplanter_name:
            return "BLOCKIERT_PERSIST", (
                f"Empfänger hat sich seit der Planung geändert (geplant: {geplanter_name!r}, jetzt: {debitor.name!r}); "
                "eine neue Freigabe/Planung ist erforderlich."
            ), None

        if kanal == "BRIEF":
            # `postadresse_geprueft` wird bei jeder ECHTEN Adressänderung
            # automatisch zurückgesetzt (siehe `upsert_debitor`-Fix) - ein
            # frischer Prüf-Zugriff auf GENAU dieses Flag deckt eine
            # zwischenzeitliche Adressänderung deshalb bereits ab, ohne die
            # Adresse selbst textuell mit einem Snapshot vergleichen zu
            # müssen.
            if not debitor.postadresse_geprueft:
                return (
                    "BLOCKIERT_PERSIST",
                    f"Kein geprüfter Postadressen-Nachweis für Debitor {konto.debitor_id} mehr hinterlegt (Kanal BRIEF).",
                    None,
                )
            if self._brief_anbieterprofil_repository is None or self._brief_anbieterprofil_repository.aktuelle_freigegebene() is None:
                return (
                    "BLOCKIERT_TRANSIENT",
                    "Kein freigegebenes Brief-Anbieterprofil hinterlegt - Briefkanal systemisch (noch) nicht "
                    "versandbereit; ein späterer Lauf greift automatisch, sobald eines freigegeben ist.",
                    None,
                )
            if not self._brief_transport_verfuegbar:
                return (
                    "BLOCKIERT_TRANSIENT",
                    "Brief-Versandtransport ist (noch) nicht angebunden (kein erfundener Live-Provider) - "
                    "kein automatischer Versand möglich, bis ein echter Transport konfiguriert ist.",
                    None,
                )
        else:
            geplante_email = mahnfall.snapshot.get("empfaenger_email")
            if not (debitor.email or "").strip():
                return "BLOCKIERT_PERSIST", f"Kein gültiger Empfänger (E-Mail) für Debitor {konto.debitor_id} mehr hinterlegt.", None
            if debitor.email != geplante_email:
                return "BLOCKIERT_PERSIST", (
                    f"Empfänger-E-Mail hat sich seit der Planung geändert (geplant: {geplante_email!r}, "
                    f"jetzt: {debitor.email!r}); eine neue Freigabe/Planung ist erforderlich."
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
            if heute < fruehestens:
                return "BLOCKIERT_TRANSIENT", "Mahnfrist ist noch nicht abgelaufen.", None
        else:
            # Auftrag HV-20261020-VIER-MAHNSTUFEN: dieselbe verallgemeinerte
            # Vorstufen-/Zugangsprüfung wie bei der Planung (siehe
            # `_pruefe_vorstufe_abstand`) - hier wird eine strukturelle
            # Reihenfolgeninkonsistenz ABSICHTLICH nicht als Exception
            # durchgereicht (das würde den gesamten Mahnlauf/Batch abbrechen),
            # sondern dauerhaft blockiert (`BLOCKIERT_PERSIST`), exakt wie
            # das bisherige Verhalten für eine fehlende erste Mahnung.
            try:
                ergebnis = self._pruefe_vorstufe_abstand(
                    forderung_op_position_id=mahnfall.forderung_op_position_id, stufe=MahnStufe(mahnfall.stufe),
                    policy=aktuelle_policy, vertrag=vertrag, heute=heute,
                )
            except MahnstufeReihenfolgeError:
                return (
                    "BLOCKIERT_PERSIST",
                    f"Stufe {mahnfall.stufe} benötigt die tatsächlich gesendete Vorstufe {mahnfall.stufe - 1}.",
                    None,
                )
            if ergebnis is not None:
                _status, grund = ergebnis
                return "BLOCKIERT_TRANSIENT", grund, None

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
            kanal=mahnfall.snapshot.get("kanal", "EMAIL"),
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
                vorschau = self._mahnkosten_service.vorschau(
                    vertrag_id=vertrag.id, stufe=mahnfall.stufe, heute=heute,
                    kanal=mahnfall.snapshot.get("kanal", "EMAIL"),
                )
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
        # Der Kanal DIESER Gruppe wird HIER EINMAL für die ganze Stufe
        # aufgelöst (`_resolve_kanal`) und danach eingefroren - siehe
        # `claim_mitglieder_und_erstelle_gruppe(kanal=...)` weiter unten
        # und `MahnLaufTable.kanal`.
        kanal = self._resolve_kanal(stufe)
        kandidaten = [
            f for f in self._repository.list_fuer_vertrag(vertrag.id)
            if f.stufe == stufe and f.status == MahnStatus.GEPLANT.value
        ]
        bereit_ids: list[int] = []
        for mahnfall in kandidaten:
            status_code, _grund, _forderung = self._pruefe_frisch_versandbereit(
                mahnfall=mahnfall, vertrag=vertrag, konto=konto, aktuelle_policy=aktuelle_policy, heute=heute,
                bank_bestaetigt_bis=bank_bestaetigt_bis, ungeklaerte_eingaenge_vorhanden=ungeklaerte_eingaenge_vorhanden,
                kanal=kanal,
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
            gesellschaft_id=vertrag.gesellschaft_id, stufe=stufe, kanal=kanal,
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
                kanal=mahnlauf.kanal,
            )
            if status_code is not None:
                if status_code == "BLOCKIERT_PERSIST":
                    self._repository.set_status(mahnfall.id, MahnStatus.BLOCKIERT.value)
                elif status_code == "UEBERSPRUNGEN_PERSIST":
                    self._repository.set_status(mahnfall.id, MahnStatus.UEBERSPRUNGEN.value)
                else:
                    # BLOCKIERT_TRANSIENT ("Frist noch nicht abgelaufen"
                    # ODER, für den Briefkanal, "Anbieterprofil/Transport
                    # systemisch noch nicht verfügbar"): dieses Mitglied
                    # bekommt KEINEN dauerhaften Status - es wird
                    # stattdessen selbst auf GEPLANT zurückgesetzt, damit
                    # eine spätere Planung es erneut berücksichtigt,
                    # sobald es tatsächlich versandbereit ist.
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

        # Kosten-/Inhaltssnapshot wird BEREITS HIER (VOR dem Claim, also
        # VOR jedem Providerkontakt) bestimmt (Auftrag Markus 14.09.2026,
        # unabhängige Rückprüfung: "der Kosten-/Inhaltssnapshot muss
        # bereits VOR jedem Providerkontakt verfügbar sein, sonst
        # Verlust bei Providerannahme vor lokalem Ergebniscommit") - ein
        # Absturz NACH dem Claim, aber VOR/WÄHREND des Providerkontakts,
        # lässt den Snapshot trotzdem bereits durabel auf der Zeile
        # liegen; eine spätere Status-Abfrage für eine dann UNSICHERE
        # Gruppe (`vervollstaendige_unsichere_mahnlaeufe`) kann damit
        # Mitgliedsnachweise/Kostenbuchung IMMER anhand DIESES
        # eingefrorenen Objekts nachziehen, nie anhand einer zweiten,
        # potenziell abweichenden Neuberechnung.
        vorschau = None
        if self._mahnkosten_service is not None:
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
                    kanal=mahnlauf.kanal,
                )

        if not self._mahnlauf_repository.claim_fuer_versand(
            mahnlauf_id,
            zusatz={"mahnkosten_snapshot_json": snapshot_zu_json(vorschau)} if self._mahnkosten_service is not None else None,
        ):
            return VersandErgebnis("BEREITS_VERARBEITET", "Ein anderer Worker/Kanal verarbeitet diese Gruppe bereits.")

        # Reservierung ERST NACH gewonnenem Claim (Rückprüfung Codex
        # 14.09.2026, echter Bug: zwei DISJUNKTE, gleichzeitig in Arbeit
        # befindliche Gruppen konnten für dieselbe Entgeltforderung -
        # z. B. zwei Komponenten derselben Vorschreibungsperiode in
        # unterschiedlichen Gruppen - BEIDE unabhängig voneinander
        # dieselbe, noch unbestätigte Pauschale ankündigen). Eine
        # Reservierung VOR dem Claim würde bei zwei GLEICHZEITIGEN
        # Versandversuchen FÜR DIESELBE Gruppe die eigene, noch nicht
        # entschiedene Konkurrenzanfrage fälschlich als "fremde Gruppe"
        # werten - der Claim selbst entscheidet zuerst, WER überhaupt
        # weitermachen darf.
        if self._mahnkosten_service is not None and vorschau is not None and vorschau.gebuehr_segmente:
            gekuerzte_vorschau = self._mahnkosten_service.reserviere_und_kuerze_vorschau(
                vorschau=vorschau, mahnlauf_id=mahnlauf_id, akteur=ctx.user_id,
            )
            if gekuerzte_vorschau is not vorschau:
                vorschau = gekuerzte_vorschau
                # Der bereits (mit dem Claim) eingefrorene Snapshot
                # muss die KÜRZUNG widerspiegeln - IMMER NOCH VOR dem
                # eigentlichen Providerkontakt.
                self._mahnlauf_repository.aktualisiere_kosten_snapshot(mahnlauf_id, snapshot_zu_json(vorschau))
        if mahnkosten_vorschau_slot is not None and self._mahnkosten_service is not None:
            # Der Inhalt (Brief-/Mailtext, siehe `versand_fn`-Aufrufer in
            # `indexautomatik/mailversand_service.py`) verwendet GENAU
            # DIESES - ggf. bereits gekürzte - Objekt, NIE eine eigene
            # Neuberechnung.
            mahnkosten_vorschau_slot["vorschau"] = vorschau

        try:
            beleg = versand_fn(mitglieder)
        except (VersandUngewissError, TransportFehlerUngewissError):
            self._mahnlauf_repository.set_status(mahnlauf_id, "UNSICHER")
            for mahnfall in mitglieder:
                self._repository.set_status(mahnfall.id, MahnStatus.UNSICHER.value)
            # Reservierungen BLEIBEN bestehen (Auftrag Markus: "Sperre
            # über UNSICHER ... erhalten") - der Provider könnte das
            # Schreiben bereits angenommen haben.
            return VersandErgebnis("UNSICHER", "Provider-Timeout nach möglicher Annahme; kein automatischer Retry.")
        except ValueError as exc:
            self._mahnlauf_repository.set_status(mahnlauf_id, "BLOCKIERT", fehlergrund=str(exc))
            for mahnfall in mitglieder:
                self._repository.set_status(mahnfall.id, MahnStatus.BLOCKIERT.value)
            # VOR jedem Providerkontakt blockiert - Reservierungen werden
            # wieder freigegeben (analog zur Mitglieder-Freigabe), damit
            # eine spätere Planung dieselbe Entgeltforderung erneut
            # versuchen kann, statt sie dauerhaft zu verlieren.
            if self._mahnkosten_service is not None and vorschau is not None:
                self._mahnkosten_service.gib_reservierungen_frei(vorschau=vorschau, mahnlauf_id=mahnlauf_id)
            return VersandErgebnis("BLOCKIERT", "Mailauftrag oder private Mailkonfiguration unvollständig.")
        if nachweis_daten(beleg) is None:
            self._mahnlauf_repository.set_status(mahnlauf_id, "UNSICHER")
            for mahnfall in mitglieder:
                self._repository.set_status(mahnfall.id, MahnStatus.UNSICHER.value)
            return VersandErgebnis("UNSICHER", "Noch kein tatsächlicher Versandnachweis; Status wird abgefragt, nicht erneut gesendet.")

        self._vervollstaendige_bestaetigten_mahnlauf(
            mahnlauf=mahnlauf, mitglieder=mitglieder, beleg=beleg, ctx=ctx, akteur=ctx.user_id,
        )
        return VersandErgebnis("GESENDET", "Tatsächlicher Versand im Maildienst nachgewiesen (gebündelter Mahnlauf).")

    def _vervollstaendige_bestaetigten_mahnlauf(
        self, *, mahnlauf: MahnLaufTable, mitglieder: list[MahnFallTable], beleg, ctx: AuthContext, akteur: str,
    ) -> None:
        """Vervollständigt EINEN bereits durch `beleg` tatsächlich
        bestätigten Mahnlauf konsistent - Gruppenübergang, JEDES noch
        nicht GESENDETE Mitglied, und die Kostenbuchung, in genau
        DIESER Reihenfolge, idempotent wiederholbar. Wird von DREI
        Stellen mit unterschiedlicher Herkunft desselben `beleg`
        aufgerufen (frisch aus `versand_fn`, aus der persistierten
        Gruppenquittung rekonstruiert, oder aus einer Status-Abfrage) -
        Auftrag Markus 14.09.2026: "den vollständigen Zustandsweg
        zusammen korrigieren". Jeder Schritt prüft selbst, ob er
        überhaupt noch etwas zu tun hat (`versand_belegen` no-opt auf
        einer bereits GESENDETEN Zeile, Mitglieder werden einzeln
        geprüft, die Kostenbuchung nur bei fehlendem `mahnkosten_
        verarbeitet_am`) - ein wiederholter Aufruf für denselben,
        bereits vollständig abgeschlossenen Mahnlauf ändert nichts."""

        versand_belegen(
            self._mahnlauf_repository._session_factory, MahnLaufTable, mahnlauf.id,
            ergebnis=beleg, erlaubt={"IN_VERSAND", "UNSICHER"}, neuer_status="GESENDET", zeitfeld="gesendet_am",
            referenz="mahnungslauf:" + mahnlauf.outbox_key,
        )

        # Auftrag HV-20261020-VIER-MAHNSTUFEN: sobald der Mahnlauf
        # tatsächlich bestätigt GESENDET ist, werden die zu DIESEM
        # Zeitpunkt erwarteten Empfänger (Hauptmieter + jeder zu diesem
        # Zeitpunkt aktive Mitmieter) als NOCH UNBESTÄTIGTE Zugangs-
        # Platzhalter angelegt - idempotent (`erstelle_erwartete_
        # eintraege` überspringt bereits vorhandene Paare), optional
        # (ohne konfiguriertes Repository unverändertes alt-Verhalten).
        if self._empfaenger_zugang_repository is not None:
            vertrag_fuer_empfaenger = self._stammdaten_repository.get_vertrag(mahnlauf.vertrag_id)
            if vertrag_fuer_empfaenger is not None:
                debitor = self._stammdaten_repository.get_debitor(vertrag_fuer_empfaenger.debitor_id)
                erwartete_empfaenger: list[tuple[int | None, str]] = []
                if debitor is not None:
                    erwartete_empfaenger.append((None, debitor.name))
                if self._mitmieter_repository is not None:
                    erwartete_empfaenger.extend(
                        (m.id, m.name) for m in self._mitmieter_repository.list_aktiv(vertrag_fuer_empfaenger.id)
                    )
                if erwartete_empfaenger:
                    self._empfaenger_zugang_repository.erstelle_erwartete_eintraege(
                        mahnlauf_id=mahnlauf.id, empfaenger=erwartete_empfaenger, erstellt_von=akteur,
                    )
        # Jedes Mitglied wurde entweder bei `plane_mahnlauf` atomar auf
        # GEBUENDELT geclaimt (Normalfall) oder - bei einem zwischenzeitlich
        # UNSICHEREN Providerkontakt (siehe `versende_mahnlauf`) -
        # zusätzlich auf UNSICHER gesetzt; der Übergang zu GESENDET
        # erfolgt hier direkt, je Mitglied einzeln protokolliert (eigener
        # Audit-Eintrag, eigenes `gesendet_am`). NUR Mitglieder, die noch
        # NICHT GESENDET sind (unabhängige Rückprüfung Codex 14.09.2026,
        # echter Bug: ein Absturz MITTEN in dieser Schleife durfte die
        # bereits bestätigte Gruppe NICHT mit einem für immer GEBUENDELT
        # bleibenden Mitglied zurücklassen - eine Wiederholung dieses
        # Aufrufs muss GENAU die übrig gebliebenen Mitglieder nachziehen,
        # nicht die bereits erledigten erneut anfassen).
        for mahnfall in mitglieder:
            if mahnfall.status != MahnStatus.GESENDET.value:
                versand_belegen(
                    self._repository._session_factory, MahnFallTable, mahnfall.id,
                    ergebnis=beleg, erlaubt={"GEBUENDELT", "UNSICHER"}, neuer_status="GESENDET", zeitfeld="gesendet_am",
                    referenz="mahnungslauf:" + mahnlauf.outbox_key,
                )

        if self._mahnkosten_service is None:
            return
        aktuell = self._mahnlauf_repository.get(mahnlauf.id)
        if aktuell is None or aktuell.mahnkosten_verarbeitet_am is not None:
            return
        # IMMER aus dem persistierten Snapshot lesen (nie aus einem
        # potenziell veralteten In-Memory-Objekt) - das ist GENAU der
        # Stand, der bereits VOR dem Providerkontakt eingefroren wurde.
        vorschau = snapshot_aus_json(aktuell.mahnkosten_snapshot_json)
        if vorschau is not None:
            self._mahnkosten_service.buche_vorschau(
                ctx=ctx, vorschau=vorschau, heute=_versandtag_wien(aktuell.gesendet_am or beleg.versendet_am),
                versandnachweis_referenz="mahnungslauf:" + mahnlauf.outbox_key, akteur=akteur,
                mahnlauf_id=mahnlauf.id,
            )
        # Erst JETZT als abgeschlossen markiert - ein Absturz VOR diesem
        # Punkt (Snapshot bereits persistiert, Buchung aber noch offen)
        # wird durch `vervollstaendige_gesendete_mahnlaeufe` anhand
        # DESSELBEN eingefrorenen Snapshots sicher nachgeholt.
        self._mahnlauf_repository.markiere_mahnkosten_verarbeitet(mahnlauf.id)

    def vervollstaendige_mahnlauf_aus_echtem_beleg(
        self, *, ctx: AuthContext, mahnlauf_id: int, beleg, akteur: str,
    ) -> None:
        """Öffentlicher Einstiegspunkt für eine Vervollständigung aus
        einem AUSSERHALB dieser Klasse bereits geprüften, authentischen
        Beleg (Auftrag HV-20261019-VERSAND-KOSTEN-ABSCHLUSS, Teil 3:
        `indexautomatik/mailversand_service.py::HVMailversandService.
        vervollstaendige_mahnlauf_aus_anbieterbeleg` nach erfolgreichem
        `mahnbrief_archiv.py::importiere_anbieterbeleg`). Lädt den
        Mahnlauf+seine Mitglieder frisch und delegiert an
        `_vervollstaendige_bestaetigten_mahnlauf` - sicher wiederholbar
        (siehe dortige Docstring): ein zweiter Aufruf für denselben,
        bereits abgeschlossenen Mahnlauf ändert nichts, bucht nie doppelt."""

        if self._mahnlauf_repository is None:
            raise ValueError("Kein MahnLaufRepository konfiguriert.")
        mahnlauf = self._mahnlauf_repository.get(mahnlauf_id)
        if mahnlauf is None:
            raise ValueError(f"Unbekannter MahnLauf {mahnlauf_id}.")
        require_gesellschaft_access(ctx, mahnlauf.gesellschaft_id)
        require_schreibrecht(ctx)
        mitglieder_ids = MahnLaufRepository.mitglieder_ids(mahnlauf)
        mitglieder = [m for m in (self._repository.get(i) for i in mitglieder_ids) if m is not None]
        self._vervollstaendige_bestaetigten_mahnlauf(
            mahnlauf=mahnlauf, mitglieder=mitglieder, beleg=beleg, ctx=ctx, akteur=akteur,
        )

    def vervollstaendige_gesendete_mahnlaeufe(
        self, *, ctx: AuthContext, akteur: str,
    ) -> list[MahnLaufTable]:
        """Recovery-Pendant zur Kosten-/Inhaltssnapshot-Bindung in
        `versende_mahnlauf` (Auftrag Markus 14.09.2026). Findet jeden
        GESENDETEN Mahnlauf, dessen Kostenbuchung NOCH NICHT als
        abgeschlossen markiert ist (Absturz zwischen bestätigtem Versand
        und Buchung, oder ZWISCHEN dem GESENDET-Übergang der Gruppe und
        dem Nachziehen einzelner Mitglieder, unabhängige Rückprüfung
        Codex 14.09.2026), und vervollständigt ihn über
        `_vervollstaendige_bestaetigten_mahnlauf` - Mitgliedsnachweise
        UND Kostenbuchung GEMEINSAM, anhand des ATOMAR mit dem
        GESENDET-Übergang eingefrorenen Snapshots, NIE anhand einer
        frisch berechneten, ggf. durch eine inzwischen eingegangene
        Zahlung abweichenden Vorschau (das würde den tatsächlich
        gesendeten, angekündigten Betrag nachträglich stillschweigend
        verändern) und rekonstruiert dafür den ursprünglichen Beleg aus
        dem bereits persistierten Audit-Eintrag der Gruppe (`versand_
        fn` wird NICHT erneut aufgerufen - kein zweiter Versand).

        Sicher wiederholbar aufzurufen: `MahnkostenService.buche_vorschau`
        ist selbst idempotent (Ledger-Unique-Constraint auf
        `mahnlauf_schluessel`, siehe `kosten_repository.py`), ein zweiter
        Aufruf für dieselbe Gruppe bucht nie doppelt. Ein Vertrag, auf den
        `ctx` keinen Zugriff hat, wird übersprungen (nicht abgebrochen),
        damit EIN nicht zuständiger Mahnlauf nicht den gesamten
        Recovery-Durchlauf für alle anderen blockiert - er bleibt für
        einen späteren Durchlauf mit passendem `ctx` offen."""

        if self._mahnlauf_repository is None:
            return []
        abgeschlossen: list[MahnLaufTable] = []
        kandidaten = {lauf.id: lauf for lauf in self._mahnlauf_repository.gesendet_ohne_kostenabschluss()}
        # Zusätzlich: Mahnläufe, die zwar GESENDET sind (Kosten bereits
        # abgeschlossen ODER kein Kostenservice konfiguriert), aber
        # mindestens ein Mitglied noch nicht auf GESENDET nachgezogen
        # haben (Absturz zwischen Gruppen- und Mitgliederübergang).
        for lauf in self._mahnlauf_repository.list_gesendet_mit_offenen_mitgliedern(mitglieder_repo=self._repository):
            kandidaten.setdefault(lauf.id, lauf)
        for lauf in kandidaten.values():
            if lauf.gesendet_am is None:
                continue
            if not ctx.has_zugriff(lauf.gesellschaft_id):
                continue
            beleg = beleg_aus_bestaetigtem_audit(self._mahnlauf_repository._session_factory, MahnLaufTable, lauf.id)
            if beleg is None:
                continue  # sollte bei status==GESENDET nie vorkommen, sicherheitshalber übersprungen statt abgebrochen
            mitglieder_ids = MahnLaufRepository.mitglieder_ids(lauf)
            mitglieder = [m for m in (self._repository.get(i) for i in mitglieder_ids) if m is not None]
            try:
                self._vervollstaendige_bestaetigten_mahnlauf(
                    mahnlauf=lauf, mitglieder=mitglieder, beleg=beleg, ctx=ctx, akteur=akteur,
                )
                abgeschlossen.append(lauf)
            except CrossTenantError:
                continue
        return abgeschlossen

    def vervollstaendige_unsichere_mahnlaeufe(
        self, *, ctx: AuthContext, akteur: str, status_abfragen_fn: Callable[[str], object],
    ) -> list[MahnLaufTable]:
        """Fragt für jeden UNSICHEREN Mahnlauf (Absturz zwischen Claim
        und bestätigtem Ergebnis, siehe `markiere_verwaiste_mahnlaeufe_
        als_unsicher`) den TATSÄCHLICHEN Providerstatus ab
        (`status_abfragen_fn(referenz) -> Objekt mit status/
        versendet_am/externe_referenz/provider_referenz`, z. B.
        `MailOpsClient.status_abfragen`) und übernimmt eine bestätigte
        Quittung OHNE erneut zu senden - Auftrag Markus 14.09.2026,
        unabhängige Rückprüfung: "UNSICHER muss konkreten Gruppenauftrag
        abfragen und bestätigte Quittung ohne Neusenden übernehmen
        können". Der Kosten-/Inhaltssnapshot wurde bereits VOR dem
        ursprünglichen Providerkontakt eingefroren (siehe `versende_
        mahnlauf`) und steht deshalb unabhängig vom Ausgang dieser
        Abfrage weiterhin zur Verfügung.

        Liefert die Status-Abfrage (noch) keine Bestätigung oder scheitert
        sie (`TransportFehlerUngewissError`/`ValueError`), bleibt der
        Mahnlauf unverändert UNSICHER - kein Fehler, nur noch nicht
        geklärt. Ein Vertrag, auf den `ctx` keinen Zugriff hat, wird
        übersprungen (nicht abgebrochen).

        `status_abfragen_fn` ist IMMER die EMAIL-Provider-Abfrage (siehe
        Aufrufer `indexautomatik/mailversand_service.py::
        HVMailversandService.status_abgleichen`) - ein BRIEF-Mahnlauf wird
        deshalb HIER NIE abgefragt (Codex-Abnahme 19.10.2026: "BRIEF-
        Statusabgleich niemals an MailOps schicken" - es gibt für den
        Briefkanal keine maschinelle Status-API). Ein UNSICHERER
        BRIEF-Mahnlauf bleibt bewusst UNSICHER bis zur manuellen
        Vervollständigung über einen echten Anbieterbeleg
        (`HVMailversandService.vervollstaendige_mahnlauf_aus_
        anbieterbeleg`)."""

        if self._mahnlauf_repository is None:
            return []
        abgeschlossen: list[MahnLaufTable] = []
        for lauf in self._mahnlauf_repository.list_fuer_status("UNSICHER"):
            if lauf.kanal != "EMAIL":
                continue  # kein maschineller Statusabgleich für den Briefkanal
            if not ctx.has_zugriff(lauf.gesellschaft_id):
                continue  # KEINE Status-Abfrage für nicht zuständige Gesellschaften auslösen
            try:
                ergebnis = status_abfragen_fn("mahnungslauf:" + lauf.outbox_key)
            except (TransportFehlerUngewissError, ValueError):
                continue
            if nachweis_daten(ergebnis) is None:
                continue
            mitglieder_ids = MahnLaufRepository.mitglieder_ids(lauf)
            mitglieder = [m for m in (self._repository.get(i) for i in mitglieder_ids) if m is not None]
            try:
                self._vervollstaendige_bestaetigten_mahnlauf(
                    mahnlauf=lauf, mitglieder=mitglieder, beleg=ergebnis, ctx=ctx, akteur=akteur,
                )
                abgeschlossen.append(lauf)
            except CrossTenantError:
                continue
        return abgeschlossen

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

    def zugang_bestaetigen_fuer_empfaenger(
        self, *, ctx: AuthContext, mahnlauf_id: int, mitmieter_id: int | None, heute: date, zugang_datum: date,
        zugangsform: str, zugang_beleg: str, akteur: str,
    ) -> MahnLaufTable:
        """Bestätigt den Zugang EINES erwarteten Empfängers (`mitmieter_id
        is None` -> Hauptmieter/Debitor, sonst ein konkreter Mitmieter)
        eines bereits GESENDETEN Mahnlaufs - mirrors `indexautomatik/
        outbox_service.py::OutboxService.zugang_bestaetigen` bewusst
        1:1 (dieselbe Fachregel, dieselben `ZUGANGSFORMEN_ALLE`/
        `ZUGANGSFORMEN_AUSREICHEND`-Werte, siehe `domain/enums.py`): ein
        gewöhnlicher E-Mail-/SMTP-Versand ODER eine bloße Brief-
        Einlieferung bei der Post ist NIE automatisch ein rechtssicherer,
        fristauslösender Zugang (Nutzervorgabe wörtlich).

        Sobald ALLE zum Versandzeitpunkt erwarteten Empfänger dieses
        Mahnlaufs einen bestätigten, FORMAL AUSREICHENDEN Zugang haben,
        wird `MahnLaufTable.zugang_bestaetigt_am` automatisch auf den
        SPÄTESTEN (konservativsten) dieser Einzelzugänge gesetzt - das
        ist die Grundlage, auf der `_pruefe_vorstufe_abstand` die
        14-Tage-Frist zur NÄCHSTEN Stufe berechnet. Solange nicht ALLE
        bestätigt haben, bleibt `zugang_bestaetigt_am` `None` - die
        nächste Stufe bleibt dadurch strukturell "noch zu früh"."""

        mahnlauf = self._mahnlauf_repository.get(mahnlauf_id)
        if mahnlauf is None:
            raise ValueError(f"Unbekannter MahnLauf {mahnlauf_id}")
        vertrag = self._stammdaten_repository.get_vertrag(mahnlauf.vertrag_id)
        if vertrag is None:
            raise ValueError(f"Unbekannter Vertrag {mahnlauf.vertrag_id}")
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)

        if mahnlauf.status != "GESENDET":
            raise ValueError(
                f"Nur ein GESENDETer Mahnlauf kann einen Zugang bestätigt bekommen (aktueller Status: {mahnlauf.status})."
            )
        if zugang_datum > heute:
            raise ValueError("Zugangsdatum darf nicht in der Zukunft liegen.")
        versandtag = _versandtag_wien(mahnlauf.gesendet_am) if mahnlauf.gesendet_am is not None else None
        if versandtag is not None and zugang_datum < versandtag:
            raise ValueError(
                f"Zugangsdatum ({zugang_datum.isoformat()}) liegt vor dem tatsächlichen Versanddatum "
                f"({versandtag.isoformat()}) - unplausible Eingabe."
            )
        if zugangsform not in ZUGANGSFORMEN_ALLE:
            raise ValueError(f"Unbekannte Zugangsform '{zugangsform}'.")
        if not (zugang_beleg or "").strip():
            raise QuellenbelegFehltError("Eine Zugangsbestätigung ohne Belegreferenz wird abgelehnt.")
        if zugangsform not in ZUGANGSFORMEN_AUSREICHEND:
            raise ValueError(
                f"Zugangsform '{zugangsform}' gilt NICHT automatisch als rechtzeitiger, fristauslösender "
                "Zugang (z. B. eine bloß versendete, unbestätigte E-Mail oder eine bloße Brief-Einlieferung "
                "bei der Post). Bitte einen formal ausreichenden Nachweis erfassen oder den Fall manuell klären."
            )

        if self._empfaenger_zugang_repository is None:
            raise ValueError("Kein MahnEmpfaengerZugangRepository konfiguriert.")
        eintraege = self._empfaenger_zugang_repository.fuer_mahnlauf(mahnlauf_id)
        ziel = next((e for e in eintraege if e.mitmieter_id == mitmieter_id), None)
        if ziel is None:
            raise ValueError(f"Kein erwarteter Empfänger (mitmieter_id={mitmieter_id!r}) für Mahnlauf {mahnlauf_id} gefunden.")
        self._empfaenger_zugang_repository.bestaetige_zugang(
            ziel.id, zugangsform=zugangsform, zugang_bestaetigt_am=zugang_datum, zugang_beleg=zugang_beleg,
        )

        aktualisiert = self._empfaenger_zugang_repository.fuer_mahnlauf(mahnlauf_id)
        if self._empfaenger_zugang_repository.alle_bestaetigt(aktualisiert, ausreichende_formen=ZUGANGSFORMEN_AUSREICHEND):
            spaetester = self._empfaenger_zugang_repository.spaetester_zugang(aktualisiert)
            # Mehrere Empfänger können unterschiedliche Zugangsformen/
            # -belege haben - das aggregierte Feld auf `MahnLaufTable`
            # ist bewusst NUR die Fristengrundlage (spätestes Datum),
            # NICHT der vollständige Nachweis; der bleibt vollständig
            # und unverkürzt in `MahnEmpfaengerZugangTable` erhalten.
            aggregierte_form = zugangsform if len(aktualisiert) == 1 else "MEHRERE_EMPFAENGER_SIEHE_EINZELNACHWEISE"
            self._mahnlauf_repository.setze_zugang(
                mahnlauf_id, zugangsform=aggregierte_form, zugang_bestaetigt_am=spaetester,
                zugang_beleg=zugang_beleg, zugang_bestaetigt_von=akteur,
            )
        return self._mahnlauf_repository.get(mahnlauf_id)
