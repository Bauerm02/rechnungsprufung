"""Auftrag HV-20261022-EIN-KONTOBLATT: Persistenz für den kontrollierten
Backoffice-Import tatsächlich manuell (außerhalb des automatisierten
Mahnlaufs) versendeter Mahnungen - siehe
`infrastructure/db/tables.py::ManuellerMahnversandTable`-Docstring für
die fachliche Begründung des Idempotenz-Schlüssels.

`importieren` ist idempotent über `idempotenz_schluessel` (Belegidentität
+ Forderungsbindung + Betragshistorie, NIE eine bloße stufe+zins_bis-
oder stufe+Forderungsgruppe-Kombination) - ein echter Retry mit
IDENTISCHEM Inhalt liefert dieselbe Zeile zurück (sicherer No-Op), ein
Datensatz mit abweichendem Inhalt erzeugt bewusst eine NEUE, eigenständige
Zeile (eigene Kostenfolge)."""

from __future__ import annotations

import hashlib
import json
from datetime import date

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mietinkasso.infrastructure.db.tables import ManuellerMahnversandTable


def berechne_idempotenz_schluessel(
    *, vertrag_id: str, stufe: int, forderung_op_position_ids: list[int], beleg_sha256: str,
    versanddatum: date, kanal: str,
) -> str:
    """Belegidentität (`beleg_sha256`) + Forderungsbindung (sortierte
    `forderung_op_position_ids`) + Betragshistorie (`versanddatum`,
    `kanal`) - unabhängige Rückprüfung Codex 22.10.2026: bewusst NICHT
    nur `stufe`+`zins_bis` oder `stufe`+Forderungsgruppe, damit eine am
    selben Tag zusätzlich gebuchte, aber tatsächlich ANDERE Zinsabrechnung
    oder Mahngebühr diesen Datensatz hier NIE verdrängt oder fälschlich
    als identisch erkennt."""

    rohdaten = {
        "vertrag_id": vertrag_id, "stufe": stufe,
        "forderung_op_position_ids": sorted(forderung_op_position_ids),
        "beleg_sha256": beleg_sha256, "versanddatum": versanddatum.isoformat(), "kanal": kanal,
    }
    return hashlib.sha256(json.dumps(rohdaten, sort_keys=True).encode()).hexdigest()


class ManuellerMahnversandRepository:
    def __init__(self, session_factory):
        self._session_factory = session_factory

    def get(self, id_: int) -> ManuellerMahnversandTable | None:
        with self._session_factory() as session:
            return session.get(ManuellerMahnversandTable, id_)

    def list_fuer_vertrag(self, vertrag_id: str) -> list[ManuellerMahnversandTable]:
        with self._session_factory() as session:
            return list(session.execute(
                select(ManuellerMahnversandTable)
                .where(ManuellerMahnversandTable.vertrag_id == vertrag_id)
                .order_by(ManuellerMahnversandTable.erfasst_am.desc())
            ).scalars())

    def vorhanden_fuer_stufe(self, *, vertrag_id: str, stufe: int) -> bool:
        """Für das Doppelversand-Gate in `MahnwesenService.plane_mahnlauf`:
        existiert bereits IRGENDEIN manuell importierter Versandnachweis
        für (vertrag_id, stufe)? (Unabhängig davon, ob daraus bereits eine
        Kostenbuchung entstanden ist.)"""

        with self._session_factory() as session:
            return session.execute(
                select(ManuellerMahnversandTable.id)
                .where(ManuellerMahnversandTable.vertrag_id == vertrag_id, ManuellerMahnversandTable.stufe == stufe)
                .limit(1)
            ).scalar_one_or_none() is not None

    def fuer_idempotenz_schluessel(self, idempotenz_schluessel: str, *, session: Session | None = None) -> ManuellerMahnversandTable | None:
        def _lesen(active_session: Session) -> ManuellerMahnversandTable | None:
            return active_session.execute(
                select(ManuellerMahnversandTable).where(ManuellerMahnversandTable.idempotenz_schluessel == idempotenz_schluessel)
            ).scalar_one_or_none()

        if session is not None:
            return _lesen(session)
        with self._session_factory() as owned_session:
            return _lesen(owned_session)

    def importieren(
        self, *, vertrag_id: str, stufe: int, forderung_op_position_ids: list[int], versanddatum: date, kanal: str,
        empfaenger_name: str, empfaenger_adresse: str, beleg_referenz: str, beleg_pdf_bytes: bytes,
        beleg_sha256: str, erfasst_von: str,
    ) -> ManuellerMahnversandTable:
        idempotenz_schluessel = berechne_idempotenz_schluessel(
            vertrag_id=vertrag_id, stufe=stufe, forderung_op_position_ids=forderung_op_position_ids,
            beleg_sha256=beleg_sha256, versanddatum=versanddatum, kanal=kanal,
        )
        bestehend = self.fuer_idempotenz_schluessel(idempotenz_schluessel)
        if bestehend is not None:
            return bestehend  # echter Retry - sicherer No-Op, KEIN zweiter Datensatz

        with self._session_factory() as session:
            try:
                row = ManuellerMahnversandTable(
                    vertrag_id=vertrag_id, stufe=stufe,
                    forderung_op_position_ids_json=json.dumps(sorted(forderung_op_position_ids)),
                    versanddatum=versanddatum, kanal=kanal, empfaenger_name=empfaenger_name,
                    empfaenger_adresse=empfaenger_adresse, beleg_referenz=beleg_referenz,
                    beleg_pdf_bytes=beleg_pdf_bytes, beleg_sha256=beleg_sha256,
                    idempotenz_schluessel=idempotenz_schluessel, erfasst_von=erfasst_von,
                )
                session.add(row)
                session.commit()
                session.refresh(row)
                return row
            except IntegrityError:
                # Gleichzeitiger zweiter Versuch mit IDENTISCHEM Inhalt
                # (Race-Sicherheitsnetz, analog `MahnbriefArchivRepository`).
                session.rollback()
                bestehend = self.fuer_idempotenz_schluessel(idempotenz_schluessel)
                if bestehend is None:
                    raise RuntimeError(
                        f"Manualimport für Vertrag {vertrag_id} Stufe {stufe} ist an einer IntegrityError "
                        "gescheitert, aber es existiert KEINE eigene Zeile - manuelle Klärung erforderlich."
                    ) from None
                return bestehend

    def setze_mahnkosten_buchung_id(self, *, id_: int, mahnkosten_buchung_id: int) -> None:
        with self._session_factory() as session:
            row = session.get(ManuellerMahnversandTable, id_, with_for_update=True)
            if row is None:
                raise ValueError(f"Unbekannte Manualimport-Zeile {id_}.")
            if row.mahnkosten_buchung_id is None:
                row.mahnkosten_buchung_id = mahnkosten_buchung_id
                session.commit()
