"""Nachvollziehbare, versionierte Vertragsprüfung (Auftrag 12.09.,
Paket B, Punkt 1):

- Jede Prüfung ist eine neue, unveränderliche Version mit Pflicht-
  Quellenbeleg ("keine beleglose Klassifizierung") und explizitem
  Rechtsprofil (`Rechtsordnung`, inkl. `UNGEKLAERT`). Nur
  `fachstatus == GEPRUEFT` schreibt die gewählte Rechtsordnung
  tatsächlich auf den Vertrag zurück (Freigabe) - ein `ENTWURF` bleibt
  sichtbar, aber wirkungslos.
- Eine einzelne Sperre wird nur EINZELN, mit Pflicht-Begründung und
  Audit/Scope aufgehoben - es gibt HIER (und nirgendwo sonst im
  Modul) einen automatischen/gebündelten Aufhebungspfad; RATENPLAN/
  RECHTSANWALT-Sperren sind davon nicht ausgenommen, aber auch nicht
  bevorzugt - jede Aufhebung braucht denselben Beleg/dieselbe
  Begründung.
- Unvollständige Indexangaben landen in der getrennten
  `IndexPruefbedarfTable` (keine Fachfeld-Pflicht, kein
  Freigabemechanismus, keine Wirkung auf `IndexKlauselTable`/Buchungen)."""

from __future__ import annotations

from decimal import Decimal

from mietinkasso.auth.service import AuthContext, require_gesellschaft_access, require_schreibrecht
from mietinkasso.domain.enums import Rechtsordnung, VertragPruefungStatus
from mietinkasso.domain.exceptions import BindungInkonsistentError, QuellenbelegFehltError
from mietinkasso.infrastructure.db.tables import IndexPruefbedarfTable, SperreTable, VertragPruefungTable, VertragTable
from mietinkasso.stammdaten.repository import StammdatenRepository
from mietinkasso.vertragspruefung.repository import IndexPruefbedarfRepository, VertragPruefungRepository

_GUELTIGE_RECHTSORDNUNGEN = {e.value for e in Rechtsordnung}
_GUELTIGE_FACHSTATUS = {e.value for e in VertragPruefungStatus}


class VertragspruefungService:
    def __init__(
        self,
        pruefung_repository: VertragPruefungRepository,
        index_pruefbedarf_repository: IndexPruefbedarfRepository,
        stammdaten_repository: StammdatenRepository,
    ):
        self._pruefung_repository = pruefung_repository
        self._index_pruefbedarf_repository = index_pruefbedarf_repository
        self._stammdaten_repository = stammdaten_repository
        self._session_factory = pruefung_repository.session_factory

    def pruefung_anlegen(
        self,
        *,
        ctx: AuthContext,
        vertrag: VertragTable,
        rechtsordnung: str,
        fachstatus: str,
        quellenbeleg_referenz: str,
        kommentar: str | None,
        akteur: str,
    ) -> VertragPruefungTable:
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)
        self._stammdaten_repository.pruefe_vertrag_nicht_ausgeschlossen(vertrag.id)

        if rechtsordnung not in _GUELTIGE_RECHTSORDNUNGEN:
            raise ValueError(f"Ungültige Rechtsordnung '{rechtsordnung}'.")
        if fachstatus not in _GUELTIGE_FACHSTATUS:
            raise ValueError(f"Ungültiger Fachstatus '{fachstatus}' (erlaubt: {sorted(_GUELTIGE_FACHSTATUS)}).")
        if not (quellenbeleg_referenz or "").strip():
            raise QuellenbelegFehltError(
                f"Vertrag {vertrag.id}: eine Prüfung ohne Quellenbeleg-Referenz wird abgelehnt - "
                "keine beleglose Klassifizierung."
            )

        # Prüfungszeile + (bei GEPRUEFT) Rückschreibung der Rechtsordnung auf
        # den Vertrag laufen in EINER gemeinsamen DB-Transaktion (Codex-
        # Rückprüfung Paket B: sonst könnte ein Fehler zwischen beiden
        # Schritten eine "halb gespeicherte Freigabe" hinterlassen - eine
        # GEPRUEFT-Prüfungszeile, deren Rechtsordnung der Vertrag nie
        # übernommen hat). Der Audit-Eintrag bleibt bewusst ein separater,
        # nachgelagerter Schritt der aufrufenden Backoffice-Route - wie bei
        # JEDER anderen Fachaktion in diesem Modul (Mahnpolicy-Freigabe,
        # Nachbuchung, ...); das ist die bestehende, durchgängige
        # Architekturgrenze dieser Codebasis, keine neue Ausnahme.
        with self._session_factory() as session:
            try:
                version = self._pruefung_repository.naechste_version(vertrag.id, session=session)
                pruefung = self._pruefung_repository.anlegen(
                    vertrag_id=vertrag.id,
                    version=version,
                    rechtsordnung=rechtsordnung,
                    fachstatus=fachstatus,
                    quellenbeleg_referenz=quellenbeleg_referenz.strip(),
                    kommentar=kommentar,
                    erstellt_von=akteur,
                    session=session,
                )

                if fachstatus == VertragPruefungStatus.GEPRUEFT.value:
                    # Freigabe: erst JETZT wirkt sich die geprüfte
                    # Rechtsordnung tatsächlich auf Mahnung/Index/
                    # Sollstellung aus (siehe
                    # domain/enums.py::rechtsordnung_geklaert) - ein
                    # ENTWURF (siehe oben, kein Aufruf hier) rührt den
                    # Vertrag nicht an.
                    self._stammdaten_repository.upsert_vertrag(
                        id=vertrag.id,
                        einheit_id=vertrag.einheit_id,
                        debitor_id=vertrag.debitor_id,
                        gesellschaft_id=vertrag.gesellschaft_id,
                        rechtsordnung=rechtsordnung,
                        gueltig_von=vertrag.gueltig_von,
                        gueltig_bis=vertrag.gueltig_bis,
                        faelligkeit_tag=vertrag.faelligkeit_tag,
                        zahlungsfrist_tage=vertrag.zahlungsfrist_tage,
                        session=session,
                    )
                session.commit()
            except Exception:
                session.rollback()
                raise
            session.refresh(pruefung)
            return pruefung

    def liste_pruefungen(self, vertrag_id: str) -> list[VertragPruefungTable]:
        return self._pruefung_repository.liste_fuer_vertrag(vertrag_id)

    def sperre_aufheben(
        self,
        *,
        ctx: AuthContext,
        vertrag: VertragTable,
        sperre_id: int,
        begruendung: str,
        akteur: str,
    ) -> SperreTable:
        """Hebt GENAU EINE, explizit ausgewählte Sperre auf - es gibt
        keinen Sammel-/Automatik-Pfad, der mehrere Sperren (insbesondere
        RATENPLAN/RECHTSANWALT) auf einmal oder ohne Begründung entfernen
        könnte. `begruendung` ist Pflicht (keine beleglose
        Klassifizierung) und wird vom Aufrufer (Backoffice-Route) ins
        Audit-Log geschrieben."""

        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)
        self._stammdaten_repository.pruefe_vertrag_nicht_ausgeschlossen(vertrag.id)
        if not (begruendung or "").strip():
            raise QuellenbelegFehltError(
                f"Sperre {sperre_id}: eine Aufhebung ohne Begründung/Beleg wird abgelehnt."
            )

        sperre = self._stammdaten_repository.get_sperre(sperre_id)
        if sperre is None:
            raise ValueError(f"Unbekannte Sperre {sperre_id}")
        if sperre.vertrag_id != vertrag.id:
            raise BindungInkonsistentError(
                f"Sperre {sperre_id} gehört zu Vertrag {sperre.vertrag_id}, nicht zu {vertrag.id}."
            )
        if sperre.aufgehoben_am is not None:
            raise ValueError(f"Sperre {sperre_id} ist bereits aufgehoben.")

        self._stammdaten_repository.sperre_aufheben(sperre_id)
        return sperre

    def index_pruefbedarf_speichern(
        self,
        *,
        ctx: AuthContext,
        vertrag: VertragTable,
        rechtsordnung: str | None,
        basis_reihe: str | None,
        basis_wert: Decimal | None,
        basis_monat: str | None,
        kommentar: str | None,
        akteur: str,
    ) -> IndexPruefbedarfTable:
        """Speichert auch UNVOLLSTÄNDIGE Indexangaben als reinen
        Entwurf/Prüfbedarf - bewusst KEINE Pflichtfeld-Prüfung hier
        (das Fehlen von Feldern ist der ganze Zweck dieser Ablage) und
        KEINE Verbindung zu `IndexKlauselTable`/`index/service.py`;
        eine spätere, tatsächlich freigebbare Klausel entsteht
        weiterhin ausschließlich über `index/service.py::klausel_anlegen`,
        wo alle Pflichtfelder unverändert Pflicht bleiben."""

        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)
        self._stammdaten_repository.pruefe_vertrag_nicht_ausgeschlossen(vertrag.id)
        if rechtsordnung is not None and rechtsordnung not in _GUELTIGE_RECHTSORDNUNGEN:
            raise ValueError(f"Ungültige Rechtsordnung '{rechtsordnung}'.")
        return self._index_pruefbedarf_repository.anlegen(
            vertrag_id=vertrag.id,
            rechtsordnung=rechtsordnung,
            basis_reihe=basis_reihe,
            basis_wert=basis_wert,
            basis_monat=basis_monat,
            kommentar=kommentar,
            erstellt_von=akteur,
        )

    def liste_index_pruefbedarf(self, vertrag_id: str) -> list[IndexPruefbedarfTable]:
        return self._index_pruefbedarf_repository.liste_fuer_vertrag(vertrag_id)
