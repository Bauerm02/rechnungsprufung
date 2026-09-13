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
from mietinkasso.domain.exceptions import ObjektAusgeschlossenError
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
        ergebnisse: list[VertragsendeErinnerungTable] = []
        for vertrag in self._stammdaten_repository.list_alle_vertraege():
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
        """Empfänger ist HART `owner_email` - fehlt diese Konfiguration,
        wird NICHTS versendet (blockiert), statt an niemanden oder eine
        geratene Adresse zu gehen. "Fällige verpasste Hinweise einmal
        nachholen": `liste_faellig` liefert jede OFFENE Zeile mit
        `faellig_am <= heute`, unabhängig davon, wie lange das schon
        zurückliegt - aber wegen des `status`-Wechsels auf BENACHRICHTIGT
        wird sie nie ein zweites Mal ausgegeben."""

        if not (self._owner_email or "").strip():
            return []
        benachrichtigt: list[VertragsendeErinnerungTable] = []
        for erinnerung in self._repository.liste_faellig(heute=heute):
            vertrag = self._stammdaten_repository.get_vertrag(erinnerung.vertrag_id)
            if vertrag is None or vertrag.gueltig_bis != erinnerung.end_datum:
                continue  # veraltet - sollte bereits UNGUELTIG sein, defensiv trotzdem übersprungen
            objekt = self._stammdaten_repository.objekt_fuer_vertrag(vertrag.id)
            einheit = self._stammdaten_repository.get_einheit(vertrag.einheit_id)
            debitor = self._stammdaten_repository.get_debitor(vertrag.debitor_id)
            text = (
                f"Vertragsende-Erinnerung: Objekt {objekt.bezeichnung}, "
                f"{einheit.bezeichnung if einheit else vertrag.einheit_id}, Mieter "
                f"{debitor.name if debitor else vertrag.debitor_id}, Vertragsende "
                f"{erinnerung.end_datum.isoformat()}. Bitte im Portal entscheiden: 'verlängern prüfen'/"
                "'nicht verlängern prüfen'/'Rückfrage'."
            )
            if send_enabled:
                versand_fn({"empfaenger": self._owner_email, "text": text, "vertrag_id": vertrag.id})
            self._repository.set_status(
                erinnerung.id, "BENACHRICHTIGT", benachrichtigt_am=datetime.now(timezone.utc)
            )
            benachrichtigt.append(erinnerung)
        return benachrichtigt

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
        if erinnerung.status == "UNGUELTIG":
            raise ValueError("Diese Erinnerung ist ungültig (veraltetes Enddatum) - keine Entscheidung mehr möglich.")
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
        if erinnerung.status == "UNGUELTIG":
            raise ValueError("Diese Erinnerung ist ungültig (veraltetes Enddatum).")
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
