"""Versionierte Eigentümer-Freigabe des Rechtsprofils je Vertrag
(Auftrag 13.09., HV-20260913-INDEXAUTOMATIK) - liefert der monatlichen
Indexautomatik alle Eingaben für `mieweg_vorschau_service.
vorschau_erstellen`, OHNE dass jeden Monat erneut manuell bestätigt
werden muss ("Eigentümer-Rechtsprofilfreigabe einmal versioniert, danach
keine unnötige monatliche Einzelgenehmigung unveränderter Standardfälle").

`quelle_hash` bindet eine Freigabe an den Stand von Vertrag UND jeder
referenzierten Komponente zum Freigabezeitpunkt - ändert sich einer
dieser Werte danach (z. B. eine nachträglich korrigierte Komponenten-
Gültigkeit, eine geänderte Rechtsordnung-Klassifizierung, ein
verändertes Vertragsende), erkennt `ist_noch_gueltig` das bei der
nächsten Prüfung und die Freigabe gilt automatisch als entwertet
("Änderungen an Quelle/Basis/Vertrag/Profil entwerten alte Freigabe") -
ohne dass irgendjemand das manuell nachpflegen müsste."""

from __future__ import annotations

from datetime import date

from mietinkasso.auth.service import AuthContext, require_gesellschaft_access, require_schreibrecht
from mietinkasso.domain.enums import Rechtsordnung
from mietinkasso.domain.exceptions import QuellenbelegFehltError
from mietinkasso.indexautomatik.repository import RechtsprofilRepository
from mietinkasso.infrastructure.db.tables import RechtsprofilTable, VertragTable
from mietinkasso.op.service import compute_content_hash
from mietinkasso.stammdaten.repository import StammdatenRepository

#: Deckt sich bewusst mit `mieweg_vorschau/service.py::
#: _WOHNUNGSRECHNER_RECHTSORDNUNGEN` (eigene, lokale Kopie - siehe
#: dortige Begründung zur Modultrennung, AGENTS.md).
_WOHNUNGSRECHNER_RECHTSORDNUNGEN = {
    Rechtsordnung.OESTERREICH_MRG_VOLL.value,
    Rechtsordnung.OESTERREICH_MRG_TEIL.value,
}


class RechtsprofilService:
    def __init__(self, repository: RechtsprofilRepository, stammdaten_repository: StammdatenRepository):
        self._repository = repository
        self._stammdaten_repository = stammdaten_repository

    def _vertrag_oder_fehler(self, vertrag_id: str) -> VertragTable:
        vertrag = self._stammdaten_repository.get_vertrag(vertrag_id)
        if vertrag is None:
            raise ValueError(f"Unbekannter Vertrag {vertrag_id}")
        return vertrag

    def entwurf_anlegen(
        self,
        *,
        ctx: AuthContext,
        vertrag_id: str,
        rechtsordnung: str,
        ist_wohnungsnutzung: bool | None,
        mrg_zinsbeschraenkung: bool,
        ist_altvertrag: bool,
        ist_hauptmiete: bool | None,
        foerderbindung: bool,
        mietzinsobergrenze_cent: int | None,
        mietzinsobergrenze_quellenbeleg: str | None,
        mietzinsobergrenze_gueltig_bis: date | None,
        bezugsjahr: int | None,
        bezugsmonat: int | None,
        letzte_basis_war_jahresdurchschnitt: bool,
        basis_komponenten_ids: list[str],
        vertraglich_zulaessiger_betrag_cent: int | None,
        vertraglicher_quellenbeleg: str | None,
        vertraglicher_fruehestmoeglicher_termin: date | None,
        vertrag_beleg_referenz: str,
        klausel_referenz: str | None,
        erstellt_von: str,
    ) -> RechtsprofilTable:
        vertrag = self._vertrag_oder_fehler(vertrag_id)
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)
        self._stammdaten_repository.pruefe_vertrag_nicht_ausgeschlossen(vertrag_id)

        if not (vertrag_beleg_referenz or "").strip():
            raise QuellenbelegFehltError(
                "Ein Rechtsprofil ohne Vertragsbeleg-Referenz wird abgelehnt - keine beleglose "
                "Klassifizierung (wie jede andere Rechtsklassifizierung in diesem Repository)."
            )
        if foerderbindung and (
            mietzinsobergrenze_cent is None or not (mietzinsobergrenze_quellenbeleg or "").strip()
        ):
            raise QuellenbelegFehltError(
                "Förderbindung ist gesetzt, aber Mietzinsobergrenze/Quellenbeleg fehlen - eine "
                "gesetzliche Obergrenze ist eine harte Bedingung: fehlend blockiert, wird nie "
                "fiktiv als unbegrenzt behandelt."
            )
        if mietzinsobergrenze_cent is not None and mietzinsobergrenze_cent < 0:
            raise ValueError(f"mietzinsobergrenze_cent ({mietzinsobergrenze_cent}) ist negativ.")

        version = self._repository.naechste_version(vertrag_id)
        row = RechtsprofilTable(
            vertrag_id=vertrag_id,
            version=version,
            rechtsordnung=rechtsordnung,
            ist_wohnungsnutzung=ist_wohnungsnutzung,
            mrg_zinsbeschraenkung=mrg_zinsbeschraenkung,
            ist_altvertrag=ist_altvertrag,
            ist_hauptmiete=ist_hauptmiete,
            foerderbindung=foerderbindung,
            mietzinsobergrenze_cent=mietzinsobergrenze_cent,
            mietzinsobergrenze_quellenbeleg=mietzinsobergrenze_quellenbeleg,
            mietzinsobergrenze_gueltig_bis=mietzinsobergrenze_gueltig_bis,
            bezugsjahr=bezugsjahr,
            bezugsmonat=bezugsmonat,
            letzte_basis_war_jahresdurchschnitt=letzte_basis_war_jahresdurchschnitt,
            basis_komponenten_ids=list(basis_komponenten_ids),
            vertraglich_zulaessiger_betrag_cent=vertraglich_zulaessiger_betrag_cent,
            vertraglicher_quellenbeleg=vertraglicher_quellenbeleg,
            vertraglicher_fruehestmoeglicher_termin=vertraglicher_fruehestmoeglicher_termin,
            vertrag_beleg_referenz=vertrag_beleg_referenz,
            klausel_referenz=klausel_referenz,
            status="ENTWURF",
            erstellt_von=erstellt_von,
        )
        return self._repository.anlegen(row)

    def _quelle_snapshot(self, profil: RechtsprofilTable, vertrag: VertragTable) -> dict:
        komponenten_snapshot = []
        for komponente_id in sorted(profil.basis_komponenten_ids):
            komponente = self._stammdaten_repository.get_komponente(komponente_id)
            komponenten_snapshot.append(
                {
                    "id": komponente_id,
                    "vorhanden": komponente is not None,
                    "betrag_cent": komponente.betrag_cent if komponente else None,
                    "indexierbar": komponente.indexierbar if komponente else None,
                    "art": komponente.art if komponente else None,
                    "gueltig_von": komponente.gueltig_von.isoformat() if komponente else None,
                    "gueltig_bis": (
                        komponente.gueltig_bis.isoformat() if komponente and komponente.gueltig_bis else None
                    ),
                }
            )
        return {
            "vertrag_rechtsordnung": vertrag.rechtsordnung,
            "vertrag_gueltig_von": vertrag.gueltig_von.isoformat(),
            "vertrag_gueltig_bis": vertrag.gueltig_bis.isoformat() if vertrag.gueltig_bis else None,
            "profil_felder": {
                "rechtsordnung": profil.rechtsordnung,
                "ist_wohnungsnutzung": profil.ist_wohnungsnutzung,
                "mrg_zinsbeschraenkung": profil.mrg_zinsbeschraenkung,
                "ist_altvertrag": profil.ist_altvertrag,
                "ist_hauptmiete": profil.ist_hauptmiete,
                "foerderbindung": profil.foerderbindung,
                "mietzinsobergrenze_cent": profil.mietzinsobergrenze_cent,
                "bezugsjahr": profil.bezugsjahr,
                "bezugsmonat": profil.bezugsmonat,
                "letzte_basis_war_jahresdurchschnitt": profil.letzte_basis_war_jahresdurchschnitt,
                "basis_komponenten_ids": sorted(profil.basis_komponenten_ids),
                "vertraglich_zulaessiger_betrag_cent": profil.vertraglich_zulaessiger_betrag_cent,
            },
            "komponenten": komponenten_snapshot,
        }

    def freigeben(self, rechtsprofil_id: int, *, ctx: AuthContext, freigegeben_von: str) -> RechtsprofilTable:
        profil = self._repository.get(rechtsprofil_id)
        if profil is None:
            raise ValueError(f"Unbekanntes Rechtsprofil {rechtsprofil_id}")
        vertrag = self._vertrag_oder_fehler(profil.vertrag_id)
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)
        quelle_hash = compute_content_hash(self._quelle_snapshot(profil, vertrag))
        return self._repository.freigeben(rechtsprofil_id, freigegeben_von=freigegeben_von, quelle_hash=quelle_hash)

    def ist_noch_gueltig(self, profil: RechtsprofilTable) -> bool:
        """Prüft, ob der bei `freigeben()` festgehaltene `quelle_hash`
        noch zum AKTUELLEN Stand von Vertrag/referenzierten Komponenten
        passt - unabhängig vom gespeicherten `status`-Feld (das erst
        beim NÄCHSTEN Lauf nachgezogen wird, siehe
        `indexautomatik/service.py`)."""

        if profil.status != "FREIGEGEBEN" or not profil.quelle_hash:
            return False
        vertrag = self._stammdaten_repository.get_vertrag(profil.vertrag_id)
        if vertrag is None:
            return False
        aktueller_hash = compute_content_hash(self._quelle_snapshot(profil, vertrag))
        return aktueller_hash == profil.quelle_hash

    def aktives_gueltiges_profil(self, vertrag_id: str) -> RechtsprofilTable | None:
        """Liefert das freigegebene Profil NUR, wenn es auch inhaltlich
        noch gültig ist - eine bereits entwertete, aber noch nicht
        `INVALIDIERT` markierte Zeile wird hier automatisch mit
        `invalidieren()` nachgezogen (kein stiller Weiterbetrieb auf
        veraltetem Stand)."""

        profil = self._repository.freigegebenes_profil(vertrag_id)
        if profil is None:
            return None
        if self.ist_noch_gueltig(profil):
            return profil
        self._repository.invalidieren(profil.id)
        return None

    def liste_fuer_vertrag(self, vertrag_id: str) -> list[RechtsprofilTable]:
        return self._repository.liste_fuer_vertrag(vertrag_id)
