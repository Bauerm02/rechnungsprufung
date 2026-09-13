"""Fachlogik für monatliche variable Nutzungsentgelt-Meldungen
(KURZZEITVERMIETUNG/SELFSTORAGE, Auftrag 13.09., HV-20260913-DASHBOARD).

Jede Erfassung/Korrektur ist eine neue, unveränderliche Version
(`VariableAbrechnungTable`); eine Korrektur bindet sich per optimistic
lock an die Version, die der Aufrufer tatsächlich gesehen hat
(`ausgehend_von_id`) - eine inzwischen überholte Ausgangsversion wird
abgelehnt statt eine zwischenzeitliche fremde Korrektur stillschweigend
zu überschreiben. Genau eine Version je (Einheit, Art, Leistungsmonat)
ist `ist_aktuell` (zusätzlich über einen partiellen Unique-Index in der
DB erzwungen, siehe `infrastructure/db/tables.py`)."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from mietinkasso.auth.service import AuthContext, require_gesellschaft_access, require_schreibrecht
from mietinkasso.domain.enums import VariableAbrechnungArt, VariableAbrechnungBetragsart, VariableAbrechnungStatus
from mietinkasso.domain.exceptions import OptimistischerLockKonfliktError, VariableAbrechnungKonfliktError
from mietinkasso.infrastructure.db.tables import VariableAbrechnungTable
from mietinkasso.stammdaten.repository import StammdatenRepository
from mietinkasso.variableabrechnung.repository import VariableAbrechnungRepository

_GUELTIGE_ARTEN = {e.value for e in VariableAbrechnungArt}
_GUELTIGE_STATUS = {e.value for e in VariableAbrechnungStatus}
_GUELTIGE_BETRAGSARTEN = {e.value for e in VariableAbrechnungBetragsart}
_LEISTUNGSMONAT_MUSTER = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def inhalts_felder(zeile: VariableAbrechnungTable) -> dict:
    """Fachlich relevante Felder für den Inhaltsvergleich (Idempotenz/
    Konflikterkennung) - bewusst OHNE id/version/ist_aktuell/erstellt_*/
    import_id/quelle_system, analog zu `intake/planner.py::_felder_hash`."""

    return {
        "einheit_id": zeile.einheit_id,
        "art": zeile.art,
        "leistungsmonat": zeile.leistungsmonat,
        "status": zeile.status,
        "belegdatum": str(zeile.belegdatum),
        "quelle_referenz": zeile.quelle_referenz,
        "quelle_hash": zeile.quelle_hash,
        "berichteter_betrag_cent": zeile.berichteter_betrag_cent,
        "berichteter_betragsart": zeile.berichteter_betragsart,
        "unser_netto_anteil_cent": zeile.unser_netto_anteil_cent,
        "betriebskosten_hinweis_cent": zeile.betriebskosten_hinweis_cent,
        "reinigungskosten_hinweis_cent": zeile.reinigungskosten_hinweis_cent,
        "verwaltungskosten_hinweis_cent": zeile.verwaltungskosten_hinweis_cent,
        "tatsaechlicher_zahlungseingang_cent": zeile.tatsaechlicher_zahlungseingang_cent,
        "vermietete_einheiten": zeile.vermietete_einheiten,
        "vermietete_flaeche_qm": str(zeile.vermietete_flaeche_qm) if zeile.vermietete_flaeche_qm is not None else None,
    }


def validiere_felder(
    *,
    art: str,
    leistungsmonat: str,
    status: str,
    berichteter_betrag_cent: int | None,
    berichteter_betragsart: str | None,
    unser_netto_anteil_cent: int | None,
    betriebskosten_hinweis_cent: int | None,
    reinigungskosten_hinweis_cent: int | None,
    verwaltungskosten_hinweis_cent: int | None,
    tatsaechlicher_zahlungseingang_cent: int | None,
    vermietete_einheiten: int | None,
    vermietete_flaeche_qm: Decimal | None,
    quelle_referenz: str,
) -> None:
    """Freie Funktion statt Service-Methode, damit `csv_import.py`s
    Dry-run-Prüfung (`erstelle_plan`) EXAKT dieselbe Validierung benutzt
    wie der schreibende Pfad (`erfassen`/`korrigieren`) - kein separater
    Prüfpfad, der von der tatsächlichen Schreiblogik abweichen könnte
    (analog zu `intake/planner.py::pruefe_paket`)."""

    if art not in _GUELTIGE_ARTEN:
        raise ValueError(f"Ungültige Art '{art}' - erlaubt: {sorted(_GUELTIGE_ARTEN)}.")
    if not _LEISTUNGSMONAT_MUSTER.match(leistungsmonat):
        raise ValueError(f"Leistungsmonat '{leistungsmonat}' muss dem Format YYYY-MM entsprechen.")
    if status not in _GUELTIGE_STATUS:
        raise ValueError(f"Ungültiger Status '{status}' - erlaubt: {sorted(_GUELTIGE_STATUS)}.")
    if not (quelle_referenz or "").strip():
        raise ValueError("quelle_referenz ist Pflicht - keine beleglose Erfassung.")
    # Quellenbedingte Präzisierung (13.09.): "berichteter Originalbetrag
    # plus Betragsart BRUTTO/NETTO/UNGEKLAERT separat, unbekannt bleibt
    # None" - ein Betrag ohne Betragsart (oder umgekehrt) ist eine
    # unvollständige, widersprüchliche Eingabe.
    if (berichteter_betrag_cent is None) != (berichteter_betragsart is None):
        raise ValueError(
            "berichteter_betrag_cent und berichteter_betragsart gehören zusammen - entweder beide "
            "gesetzt oder beide leer (unbekannt bleibt None, keine erfundene Betragsart)."
        )
    if berichteter_betragsart is not None and berichteter_betragsart not in _GUELTIGE_BETRAGSARTEN:
        raise ValueError(f"Ungültige Betragsart '{berichteter_betragsart}' - erlaubt: {sorted(_GUELTIGE_BETRAGSARTEN)}.")
    # "Nur geprüfte/eindeutig netto bestätigte eigene Anteile in die
    # Erlössumme" - eine BESTAETIGTe Zeile ohne unser_netto_anteil_cent
    # wäre ein Widerspruch in sich (bestätigt, aber nichts bestätigt).
    if status == VariableAbrechnungStatus.BESTAETIGT.value and unser_netto_anteil_cent is None:
        raise ValueError(
            "Status BESTAETIGT setzt einen erfassten unser_netto_anteil_cent voraus - ein "
            "unbekannter Nettoanteil kann nicht als bestätigt gelten."
        )
    for name, wert in (
        ("berichteter_betrag_cent", berichteter_betrag_cent),
        ("unser_netto_anteil_cent", unser_netto_anteil_cent),
        ("betriebskosten_hinweis_cent", betriebskosten_hinweis_cent),
        ("reinigungskosten_hinweis_cent", reinigungskosten_hinweis_cent),
        ("verwaltungskosten_hinweis_cent", verwaltungskosten_hinweis_cent),
        ("tatsaechlicher_zahlungseingang_cent", tatsaechlicher_zahlungseingang_cent),
        ("vermietete_einheiten", vermietete_einheiten),
    ):
        if wert is not None and wert < 0:
            raise ValueError(f"{name} darf nicht negativ sein (erhalten: {wert}).")
    if vermietete_flaeche_qm is not None and vermietete_flaeche_qm < 0:
        raise ValueError(f"vermietete_flaeche_qm darf nicht negativ sein (erhalten: {vermietete_flaeche_qm}).")


class VariableAbrechnungService:
    def __init__(self, repository: VariableAbrechnungRepository, stammdaten_repository: StammdatenRepository):
        self._repository = repository
        self._stammdaten_repository = stammdaten_repository

    def erfassen(
        self,
        *,
        ctx: AuthContext,
        einheit_id: str,
        art: str,
        leistungsmonat: str,
        belegdatum: date,
        quelle_referenz: str,
        status: str = VariableAbrechnungStatus.ENTWURF.value,
        quelle_hash: str | None = None,
        berichteter_betrag_cent: int | None = None,
        berichteter_betragsart: str | None = None,
        unser_netto_anteil_cent: int | None = None,
        betriebskosten_hinweis_cent: int | None = None,
        reinigungskosten_hinweis_cent: int | None = None,
        verwaltungskosten_hinweis_cent: int | None = None,
        tatsaechlicher_zahlungseingang_cent: int | None = None,
        vermietete_einheiten: int | None = None,
        vermietete_flaeche_qm: Decimal | None = None,
        erstellt_von: str,
        quelle_system: str = "MANUELL",
        import_id: str | None = None,
        session: Session | None = None,
    ) -> VariableAbrechnungTable:
        """Erste Version für (einheit_id, art, leistungsmonat). Existiert
        bereits eine aktuelle Version für dieselbe Gruppe, ist dieser
        Aufruf NUR dann ein sicherer No-Op, wenn der Inhalt identisch
        ist (Wiederholimport) - bei abweichendem Inhalt wird
        `VariableAbrechnungKonfliktError` geworfen und auf die explizite
        `korrigieren`-Route verwiesen ("Korrigierte Quelle nur explizit
        als neue Version")."""

        objekt = self._stammdaten_repository.objekt_fuer_einheit(einheit_id, session=session)
        require_gesellschaft_access(ctx, objekt.gesellschaft_id)
        require_schreibrecht(ctx)
        self._stammdaten_repository.pruefe_einheit_nicht_ausgeschlossen(einheit_id, session=session)
        validiere_felder(
            art=art, leistungsmonat=leistungsmonat, status=status,
            berichteter_betrag_cent=berichteter_betrag_cent, berichteter_betragsart=berichteter_betragsart,
            unser_netto_anteil_cent=unser_netto_anteil_cent,
            betriebskosten_hinweis_cent=betriebskosten_hinweis_cent,
            reinigungskosten_hinweis_cent=reinigungskosten_hinweis_cent,
            verwaltungskosten_hinweis_cent=verwaltungskosten_hinweis_cent,
            tatsaechlicher_zahlungseingang_cent=tatsaechlicher_zahlungseingang_cent,
            vermietete_einheiten=vermietete_einheiten, vermietete_flaeche_qm=vermietete_flaeche_qm,
            quelle_referenz=quelle_referenz,
        )

        neue_felder = {
            "einheit_id": einheit_id, "art": art, "leistungsmonat": leistungsmonat, "status": status,
            "belegdatum": str(belegdatum), "quelle_referenz": quelle_referenz, "quelle_hash": quelle_hash,
            "berichteter_betrag_cent": berichteter_betrag_cent, "berichteter_betragsart": berichteter_betragsart,
            "unser_netto_anteil_cent": unser_netto_anteil_cent,
            "betriebskosten_hinweis_cent": betriebskosten_hinweis_cent,
            "reinigungskosten_hinweis_cent": reinigungskosten_hinweis_cent,
            "verwaltungskosten_hinweis_cent": verwaltungskosten_hinweis_cent,
            "tatsaechlicher_zahlungseingang_cent": tatsaechlicher_zahlungseingang_cent,
            "vermietete_einheiten": vermietete_einheiten,
            "vermietete_flaeche_qm": str(vermietete_flaeche_qm) if vermietete_flaeche_qm is not None else None,
        }

        if import_id is not None:
            bestehende_mit_import_id = self._repository.by_import_id(import_id, session=session)
            if bestehende_mit_import_id is not None:
                if inhalts_felder(bestehende_mit_import_id) == neue_felder:
                    return bestehende_mit_import_id
                raise VariableAbrechnungKonfliktError(
                    f"import_id '{import_id}' existiert bereits mit abweichendem Inhalt."
                )

        aktuelle = self._repository.aktuelle_version(einheit_id, art, leistungsmonat, session=session)
        if aktuelle is not None:
            if inhalts_felder(aktuelle) == neue_felder:
                return aktuelle
            raise VariableAbrechnungKonfliktError(
                f"Für Einheit '{einheit_id}', Art '{art}', Leistungsmonat '{leistungsmonat}' existiert "
                f"bereits eine aktuelle Version (#{aktuelle.id}) mit abweichendem Inhalt - keine "
                "Summierung aus Import und manueller Erfassung. Bitte explizit über 'korrigieren' "
                f"(ausgehend_von_id={aktuelle.id}) mit Änderungsgrund aktualisieren."
            )

        row = VariableAbrechnungTable(
            einheit_id=einheit_id, gesellschaft_id=objekt.gesellschaft_id, art=art, leistungsmonat=leistungsmonat,
            version=1, ist_aktuell=True, status=status, belegdatum=belegdatum, quelle_referenz=quelle_referenz,
            quelle_hash=quelle_hash, berichteter_betrag_cent=berichteter_betrag_cent,
            berichteter_betragsart=berichteter_betragsart, unser_netto_anteil_cent=unser_netto_anteil_cent,
            betriebskosten_hinweis_cent=betriebskosten_hinweis_cent,
            reinigungskosten_hinweis_cent=reinigungskosten_hinweis_cent,
            verwaltungskosten_hinweis_cent=verwaltungskosten_hinweis_cent,
            tatsaechlicher_zahlungseingang_cent=tatsaechlicher_zahlungseingang_cent,
            vermietete_einheiten=vermietete_einheiten, vermietete_flaeche_qm=vermietete_flaeche_qm,
            aenderungsgrund=None, quelle_system=quelle_system, import_id=import_id, erstellt_von=erstellt_von,
        )
        return self._repository.neue_version_anlegen(row, alte_id=None, session=session)

    def korrigieren(
        self,
        *,
        ctx: AuthContext,
        ausgehend_von_id: int,
        aenderungsgrund: str,
        belegdatum: date,
        quelle_referenz: str,
        status: str,
        quelle_hash: str | None = None,
        berichteter_betrag_cent: int | None = None,
        berichteter_betragsart: str | None = None,
        unser_netto_anteil_cent: int | None = None,
        betriebskosten_hinweis_cent: int | None = None,
        reinigungskosten_hinweis_cent: int | None = None,
        verwaltungskosten_hinweis_cent: int | None = None,
        tatsaechlicher_zahlungseingang_cent: int | None = None,
        vermietete_einheiten: int | None = None,
        vermietete_flaeche_qm: Decimal | None = None,
        erstellt_von: str,
        quelle_system: str = "MANUELL",
        import_id: str | None = None,
        session: Session | None = None,
    ) -> VariableAbrechnungTable:
        """Legt Version `ausgehend.version + 1` an - NUR wenn
        `ausgehend_von_id` tatsächlich noch die aktuelle Version dieser
        Gruppe ist (optimistic lock). Ist sie das nicht mehr (eine
        andere Korrektur kam dazwischen), wird
        `OptimistischerLockKonfliktError` geworfen statt die fremde
        Korrektur stillschweigend zu überschreiben - der Aufrufer muss
        den aktuellen Stand neu laden."""

        ausgehend = self._repository.get(ausgehend_von_id, session=session)
        if ausgehend is None:
            raise ValueError(f"Unbekannte VariableAbrechnung {ausgehend_von_id}")
        objekt = self._stammdaten_repository.objekt_fuer_einheit(ausgehend.einheit_id, session=session)
        require_gesellschaft_access(ctx, objekt.gesellschaft_id)
        require_schreibrecht(ctx)
        self._stammdaten_repository.pruefe_einheit_nicht_ausgeschlossen(ausgehend.einheit_id, session=session)

        if not (aenderungsgrund or "").strip():
            raise ValueError("Eine Korrektur ohne Änderungsgrund wird abgelehnt.")

        aktuelle = self._repository.aktuelle_version(
            ausgehend.einheit_id, ausgehend.art, ausgehend.leistungsmonat, session=session
        )
        if aktuelle is None or aktuelle.id != ausgehend.id:
            raise OptimistischerLockKonfliktError(
                f"Ausgangsversion #{ausgehend_von_id} ist nicht mehr die aktuelle Version für Einheit "
                f"'{ausgehend.einheit_id}', Art '{ausgehend.art}', Leistungsmonat '{ausgehend.leistungsmonat}' "
                f"(aktuell: {aktuelle.id if aktuelle else 'keine'}) - zwischenzeitlich wurde bereits "
                "korrigiert. Bitte den aktuellen Stand neu laden und erneut korrigieren."
            )

        validiere_felder(
            art=ausgehend.art, leistungsmonat=ausgehend.leistungsmonat, status=status,
            berichteter_betrag_cent=berichteter_betrag_cent, berichteter_betragsart=berichteter_betragsart,
            unser_netto_anteil_cent=unser_netto_anteil_cent,
            betriebskosten_hinweis_cent=betriebskosten_hinweis_cent,
            reinigungskosten_hinweis_cent=reinigungskosten_hinweis_cent,
            verwaltungskosten_hinweis_cent=verwaltungskosten_hinweis_cent,
            tatsaechlicher_zahlungseingang_cent=tatsaechlicher_zahlungseingang_cent,
            vermietete_einheiten=vermietete_einheiten, vermietete_flaeche_qm=vermietete_flaeche_qm,
            quelle_referenz=quelle_referenz,
        )

        neue_zeile = VariableAbrechnungTable(
            einheit_id=ausgehend.einheit_id, gesellschaft_id=objekt.gesellschaft_id, art=ausgehend.art,
            leistungsmonat=ausgehend.leistungsmonat, version=ausgehend.version + 1, ist_aktuell=True,
            status=status, belegdatum=belegdatum, quelle_referenz=quelle_referenz, quelle_hash=quelle_hash,
            berichteter_betrag_cent=berichteter_betrag_cent, berichteter_betragsart=berichteter_betragsart,
            unser_netto_anteil_cent=unser_netto_anteil_cent,
            betriebskosten_hinweis_cent=betriebskosten_hinweis_cent,
            reinigungskosten_hinweis_cent=reinigungskosten_hinweis_cent,
            verwaltungskosten_hinweis_cent=verwaltungskosten_hinweis_cent,
            tatsaechlicher_zahlungseingang_cent=tatsaechlicher_zahlungseingang_cent,
            vermietete_einheiten=vermietete_einheiten, vermietete_flaeche_qm=vermietete_flaeche_qm,
            aenderungsgrund=aenderungsgrund, quelle_system=quelle_system, import_id=import_id,
            erstellt_von=erstellt_von,
        )
        return self._repository.neue_version_anlegen(neue_zeile, alte_id=ausgehend.id, session=session)

    def aktuelle_version(self, einheit_id: str, art: str, leistungsmonat: str) -> VariableAbrechnungTable | None:
        return self._repository.aktuelle_version(einheit_id, art, leistungsmonat)

    def liste_versionen(self, einheit_id: str, art: str, leistungsmonat: str) -> list[VariableAbrechnungTable]:
        return self._repository.liste_versionen(einheit_id, art, leistungsmonat)

    def liste_aktuelle(
        self, *, leistungsmonat: str | None = None, gesellschaft_id: str | None = None
    ) -> list[VariableAbrechnungTable]:
        return self._repository.liste_aktuelle(leistungsmonat=leistungsmonat, gesellschaft_id=gesellschaft_id)

    def liste_alle(self) -> list[VariableAbrechnungTable]:
        return self._repository.liste_alle()
