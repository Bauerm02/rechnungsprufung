"""Vertragsende-Erinnerung (Auftrag 13.09.) - Trigger `end_date` minus
DREI KALENDERmonate (nicht 90 Tage), Empfänger IMMER der intern
konfigurierte Eigentümer (`Settings.owner_email`), NIE ein
Mieter-Fallback/CC. Der Mieter wird ERST nach einer ausdrücklichen,
gespeicherten Entscheidung im authentifizierten Portal überhaupt
adressiert - und selbst dann entsteht höchstens ein manuell
freizugebender ENTWURF, NIE ein automatisches, rechtlich bindendes
Kündigungs-/Verlängerungsschreiben und NIE ein neuer Vertrag."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Callable

from mietinkasso.auth.service import AuthContext, require_gesellschaft_access, require_schreibrecht
from mietinkasso.domain.enums import VertragsendeEntscheidung
from mietinkasso.domain.exceptions import ObjektAusgeschlossenError, TransportFehlerUngewissError
from mietinkasso.indexautomatik.repository import VertragsendeErinnerungRepository
from mietinkasso.indexautomatik.zeit import kalendermonate_subtrahieren
from mietinkasso.infrastructure.db.tables import VertragsendeErinnerungTable, VertragTable
from mietinkasso.stammdaten.repository import StammdatenRepository

_VORLAUF_MONATE = 3
_GUELTIGE_ENTSCHEIDUNGEN = {e.value for e in VertragsendeEntscheidung}


class VertragsendeErinnerungService:
    def __init__(
        self,
        repository: VertragsendeErinnerungRepository,
        stammdaten_repository: StammdatenRepository,
        *,
        owner_email: str | None,
    ):
        self._repository = repository
        self._stammdaten_repository = stammdaten_repository
        self._owner_email = owner_email

    def plane_fuer_vertrag(self, *, ctx: AuthContext, vertrag: VertragTable, heute: date) -> VertragsendeErinnerungTable | None:
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)
        self._stammdaten_repository.pruefe_vertrag_nicht_ausgeschlossen(vertrag.id)

        # Ändert sich das Enddatum (Verlängerung/Verkürzung/unbefristet),
        # werden alle noch nicht ungültigen Erinnerungen für ein ANDERES
        # Enddatum sofort entwertet - VOR dem Anlegen einer neuen Zeile.
        self._repository.invalidiere_veraltete(vertrag.id, vertrag.gueltig_bis)

        if vertrag.gueltig_bis is None:
            return None  # unbefristet - kein erfundener Leerstand/Endtermin

        vorhanden = self._repository.get_by_enddatum(vertrag.id, vertrag.gueltig_bis)
        if vorhanden is not None:
            return vorhanden

        faellig_am = kalendermonate_subtrahieren(vertrag.gueltig_bis, _VORLAUF_MONATE)
        return self._repository.anlegen(
            VertragsendeErinnerungTable(
                vertrag_id=vertrag.id, end_datum=vertrag.gueltig_bis, faellig_am=faellig_am, status="OFFEN"
            )
        )

    def plane_alle(self, *, ctx: AuthContext, heute: date) -> list[VertragsendeErinnerungTable]:
        """Unabhängiger Review (b31-Folgereview): derselbe
        Gesellschaftsscope-Filter wie `IndexautomatikService.
        monatslauf_alle` - ein Vertrag außerhalb von
        `ctx.gesellschaft_ids` wird übersprungen, BEVOR
        `plane_fuer_vertrag` (das intern `require_gesellschaft_access`
        aufruft und sonst `CrossTenantError` werfen würde) überhaupt
        aufgerufen wird."""

        ergebnisse: list[VertragsendeErinnerungTable] = []
        for vertrag in self._stammdaten_repository.list_alle_vertraege():
            if not ctx.has_zugriff(vertrag.gesellschaft_id):
                continue
            try:
                self._stammdaten_repository.pruefe_vertrag_nicht_ausgeschlossen(vertrag.id)
            except ObjektAusgeschlossenError:
                continue
            ergebnis = self.plane_fuer_vertrag(ctx=ctx, vertrag=vertrag, heute=heute)
            if ergebnis is not None:
                ergebnisse.append(ergebnis)
        return ergebnisse

    def benachrichtige_faellige(
        self, *, heute: date, send_enabled: bool, versand_fn: Callable[[dict], None]
    ) -> list[VertragsendeErinnerungTable]:
        """Empfänger ist HART `owner_email` - fehlt diese Konfiguration
        ODER ist `send_enabled=False`, wird NICHTS als versendet
        markiert (bleibt OFFEN), statt einen nie tatsächlich
        stattgefundenen Versand als erledigt zu verbuchen.

        Unabhängiger Review (fd8c2b2-Folgereview, synthetisch mit "0
        Senderaufrufe, Status trotzdem BENACHRICHTIGT" reproduziert):
        die vorherige Fassung setzte den Status IMMER auf BENACHRICHTIGT,
        auch wenn `send_enabled=False` `versand_fn` nie aufgerufen hatte
        - eine später tatsächlich aktivierte Erinnerung wäre für diesen
        Fall dann NIE mehr gesendet worden (als "bereits benachrichtigt"
        fälschlich verbraucht). Jetzt: ohne aktivierten Versand bleibt
        die Zeile unverändert OFFEN und wird beim nächsten Lauf erneut
        geprüft. Mit aktiviertem Versand läuft ein echter atomarer Claim
        (OFFEN->IN_VERSAND, analog zur Erhöhungsschreiben-Outbox) vor
        dem eigentlichen Aufruf, und ein `TransportFehlerUngewissError`
        landet auf UNKLAR statt eines blinden Retries.

        "Fällige verpasste Hinweise einmal nachholen": `liste_faellig`
        liefert jede OFFENE Zeile mit `faellig_am <= heute`, unabhängig
        davon, wie lange das schon zurückliegt."""

        if not send_enabled or not (self._owner_email or "").strip():
            return []
        benachrichtigt: list[VertragsendeErinnerungTable] = []
        for erinnerung in self._repository.liste_faellig(heute=heute):
            vertrag = self._stammdaten_repository.get_vertrag(erinnerung.vertrag_id)
            if vertrag is None or vertrag.gueltig_bis != erinnerung.end_datum:
                continue  # veraltet - sollte bereits UNGUELTIG sein, defensiv trotzdem übersprungen
            if not self._repository.claim_fuer_versand(erinnerung.id):
                continue  # bereits von einem anderen Worker geclaimt
            objekt = self._stammdaten_repository.objekt_fuer_vertrag(vertrag.id)
            einheit = self._stammdaten_repository.get_einheit(vertrag.einheit_id)
            debitor = self._stammdaten_repository.get_debitor(vertrag.debitor_id)
            idempotenzschluessel = f"vertragsende:{vertrag.id}:{erinnerung.end_datum.isoformat()}"
            text = (
                f"Vertragsende-Erinnerung: Objekt {objekt.bezeichnung}, "
                f"{einheit.bezeichnung if einheit else vertrag.einheit_id}, Mieter "
                f"{debitor.name if debitor else vertrag.debitor_id}, Vertragsende "
                f"{erinnerung.end_datum.isoformat()}. Bitte im Portal entscheiden: 'verlängern prüfen'/"
                "'nicht verlängern prüfen'/'Rückfrage'."
            )
            try:
                versand_fn({
                    "empfaenger": self._owner_email, "text": text, "vertrag_id": vertrag.id,
                    "idempotenzschluessel": idempotenzschluessel,
                })
            except TransportFehlerUngewissError as exc:
                self._repository.set_status(erinnerung.id, "UNKLAR", fehlergrund=str(exc))
                continue
            self._repository.set_status(erinnerung.id, "BENACHRICHTIGT", benachrichtigt_am=datetime.now(timezone.utc))
            benachrichtigt.append(erinnerung)
        return benachrichtigt

    def markiere_verwaiste_als_unklar(self, *, jetzt: datetime | None = None, max_alter=None):
        from datetime import timedelta

        grenze = (jetzt or datetime.now(timezone.utc)) - (max_alter or timedelta(minutes=15))
        verwaiste = self._repository.verwaiste_in_versand(aelter_als=grenze)
        for row in verwaiste:
            self._repository.set_status(row.id, "UNKLAR", fehlergrund="Verwaist zwischen Claim und Versandergebnis (Absturz-Recovery).")
        return verwaiste

    def entscheiden(
        self, *, ctx: AuthContext, erinnerung_id: int, entscheidung: str, entschieden_von: str
    ) -> VertragsendeErinnerungTable:
        erinnerung = self._repository.get(erinnerung_id)
        if erinnerung is None:
            raise ValueError(f"Unbekannte VertragsendeErinnerung {erinnerung_id}")
        vertrag = self._stammdaten_repository.get_vertrag(erinnerung.vertrag_id)
        if vertrag is None:
            raise ValueError(f"Unbekannter Vertrag {erinnerung.vertrag_id}")
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)
        if entscheidung not in _GUELTIGE_ENTSCHEIDUNGEN:
            raise ValueError(f"Unbekannte Entscheidung '{entscheidung}'.")
        if erinnerung.status == "UNGUELTIG" or vertrag.gueltig_bis != erinnerung.end_datum:
            raise ValueError(
                "Diese Erinnerung ist ungültig oder das Vertragsende hat sich seither geändert - keine "
                "Entscheidung mehr auf diesem veralteten Stand möglich."
            )
        return self._repository.set_status(
            erinnerung.id, "ENTSCHIEDEN", entscheidung=entscheidung, entschieden_von=entschieden_von,
            entschieden_am=datetime.now(timezone.utc),
        )

    def mieterentwurf_erzeugen(self, *, ctx: AuthContext, erinnerung_id: int) -> VertragsendeErinnerungTable:
        """Erzeugt HÖCHSTENS einen manuell zu prüfenden/freizugebenden
        Textentwurf - kein Versand-, kein Freigabemechanismus in diesem
        Service (bewusst: "niemals automatisches rechtlich bindendes
        Kündigungsschreiben oder einen neuen Vertrag")."""

        erinnerung = self._repository.get(erinnerung_id)
        if erinnerung is None:
            raise ValueError(f"Unbekannte VertragsendeErinnerung {erinnerung_id}")
        vertrag = self._stammdaten_repository.get_vertrag(erinnerung.vertrag_id)
        if vertrag is None:
            raise ValueError(f"Unbekannter Vertrag {erinnerung.vertrag_id}")
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)
        if erinnerung.status == "UNGUELTIG" or vertrag.gueltig_bis != erinnerung.end_datum:
            raise ValueError("Diese Erinnerung ist ungültig oder das Vertragsende hat sich seither geändert.")
        if erinnerung.entscheidung is None:
            raise ValueError("Ein Mieterentwurf darf erst NACH einer gespeicherten Entscheidung erzeugt werden.")

        debitor = self._stammdaten_repository.get_debitor(vertrag.debitor_id)
        text = "\n".join(
            [
                "ENTWURF - NICHT VERSENDET - vor jeder Verwendung eigenständig prüfen und freigeben.",
                "",
                f"Sehr geehrte(r) {debitor.name if debitor else vertrag.debitor_id},",
                "",
                f"Ihr Mietverhältnis (Vertrag {vertrag.id}) endet laut Vertrag am {erinnerung.end_datum.isoformat()}.",
                f"Interne Entscheidung (Stand): {erinnerung.entscheidung}.",
                "",
                "[Dieser Text ist ein reiner Entwurf ohne rechtliche Bindungswirkung - kein automatisches "
                "Kündigungs-/Verlängerungsschreiben. Bitte manuell vervollständigen.]",
            ]
        )
        return self._repository.set_status(
            erinnerung.id, erinnerung.status, mieterentwurf_text=text, mieterentwurf_erstellt_am=datetime.now(timezone.utc)
        )
