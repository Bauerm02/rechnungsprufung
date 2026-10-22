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

from mietinkasso.domain.exceptions import VorgangIdKonfliktError
from mietinkasso.infrastructure.db.tables import ManuellerMahnversandTable


def berechne_idempotenz_schluessel(
    *, vertrag_id: str, forderung_op_position_ids: list[int], beleg_sha256: str,
    versanddatum: date, kanal: str,
) -> str:
    """Belegidentität (`beleg_sha256`) + Forderungsbindung (sortierte
    `forderung_op_position_ids`) + Betragshistorie (`versanddatum`,
    `kanal`) - unabhängige Rückprüfung Codex 22.10.2026: bewusst NICHT
    nur `stufe`+`zins_bis` oder `stufe`+Forderungsgruppe, damit eine am
    selben Tag zusätzlich gebuchte, aber tatsächlich ANDERE Zinsabrechnung
    oder Mahngebühr diesen Datensatz hier NIE verdrängt oder fälschlich
    als identisch erkennt.

    **Korrektur nach unabhängiger Rückprüfung Codex 22.10.2026, echter
    Bug:** `stufe` ist bewusst NICHT mehr Teil dieses Schlüssels - ein
    byteidentisches Originaldokument (identischer Beleg-Hash, identische
    Forderungsbindung/Versanddatum/Kanal) bleibt DASSELBE physische
    Schreiben, auch wenn jemand nachträglich die Stufen-Klassifizierung
    korrigiert; `stufe` wäre sonst Teil der Identität gewesen und hätte
    bei einer bloßen Korrektur eine ZWEITE, unabhängige Gebührenbasis für
    DASSELBE Schreiben erzeugt. `ManuellerMahnversandRepository.
    importieren` prüft `stufe` stattdessen EXPLIZIT gegen eine bereits
    bestehende Zeile mit identischem Schlüssel und lehnt eine Abweichung
    als Konflikt ab (siehe dort)."""

    rohdaten = {
        "vertrag_id": vertrag_id,
        "forderung_op_position_ids": sorted(forderung_op_position_ids),
        "beleg_sha256": beleg_sha256, "versanddatum": versanddatum.isoformat(), "kanal": kanal,
    }
    return hashlib.sha256(json.dumps(rohdaten, sort_keys=True).encode()).hexdigest()


def _pruefe_stufe_identisch_oder_konflikt(bestehend: ManuellerMahnversandTable, *, stufe: int) -> ManuellerMahnversandTable:
    """Unabhängige Rückprüfung Codex 22.10.2026, echter Bug: ein
    BYTEIDENTISCHER Originalnachweis (identischer `idempotenz_schluessel`
    - also identischer Beleg-Hash/Forderungsbindung/Versanddatum/Kanal)
    ist DASSELBE physische Schreiben. Eine Wiederholung mit abweichender
    `stufe` ist KEIN zweites tatsächliches Schreiben, sondern entweder ein
    Tippfehler oder eine nachträgliche Korrektur der Klassifizierung -
    beides wird als Konflikt abgelehnt, NIE als stiller zweiter
    Datensatz/zweite Gebührenbasis für dasselbe Dokument akzeptiert."""

    if bestehend.stufe != stufe:
        raise VorgangIdKonfliktError(
            f"Identischer Originalnachweis (Beleg-Hash {bestehend.beleg_sha256[:12]}…, dieselbe "
            f"Forderungsbindung/Versanddatum/Kanal) ist bereits als Stufe {bestehend.stufe} erfasst - eine "
            f"abweichende Stufe {stufe} für DASSELBE Schreiben wird abgelehnt, statt eine zweite, unabhängige "
            "Gebührenbasis für dasselbe Dokument zu erzeugen."
        )
    return bestehend  # echter Retry - sicherer No-Op, KEIN zweiter Datensatz


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
            vertrag_id=vertrag_id, forderung_op_position_ids=forderung_op_position_ids,
            beleg_sha256=beleg_sha256, versanddatum=versanddatum, kanal=kanal,
        )
        bestehend = self.fuer_idempotenz_schluessel(idempotenz_schluessel)
        if bestehend is not None:
            return _pruefe_stufe_identisch_oder_konflikt(bestehend, stufe=stufe)

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
                # Gleichzeitiger zweiter Versuch mit IDENTISCHEM Beleg
                # (Race-Sicherheitsnetz, analog `MahnbriefArchivRepository`)
                # ODER derselbe Originalnachweis mit abweichender Stufe.
                session.rollback()
                bestehend = self.fuer_idempotenz_schluessel(idempotenz_schluessel)
                if bestehend is None:
                    raise RuntimeError(
                        f"Manualimport für Vertrag {vertrag_id} Stufe {stufe} ist an einer IntegrityError "
                        "gescheitert, aber es existiert KEINE eigene Zeile - manuelle Klärung erforderlich."
                    ) from None
                return _pruefe_stufe_identisch_oder_konflikt(bestehend, stufe=stufe)

    def setze_mahnkosten_buchung_id(
        self, *, id_: int, mahnkosten_buchung_id: int, pauschale_abgegolten: bool = False,
    ) -> None:
        """`mahnkosten_buchung_id` wird auf die ZULETZT tatsächlich
        gebuchte Ledger-Zeile aktualisiert (erste Buchung bei Import/
        Bestätigung, später ggf. die Nachbuchungs-Zeile) - rein
        informativ für die Anzeige, trägt KEINE Idempotenz-Semantik
        selbst (die liegt bei `pauschale_abgegolten`/den jeweiligen
        Aufrufer-Guards in `manueller_mahnversand_service.py`).
        `pauschale_abgegolten=True` wird NUR gesetzt, nie wieder auf
        `False` zurückgenommen (unabhängige Rückprüfung Codex 22.10.2026) -
        sobald GENAU EINE Gebühr/Pauschale für diesen Beleg tatsächlich
        gebucht wurde, bleibt das dauerhaft so, unabhängig davon, ob eine
        spätere Buchung für denselben Beleg rein zinsbasiert ist."""

        with self._session_factory() as session:
            row = session.get(ManuellerMahnversandTable, id_, with_for_update=True)
            if row is None:
                raise ValueError(f"Unbekannte Manualimport-Zeile {id_}.")
            aenderung = False
            if row.mahnkosten_buchung_id != mahnkosten_buchung_id:
                row.mahnkosten_buchung_id = mahnkosten_buchung_id
                aenderung = True
            if pauschale_abgegolten and not row.pauschale_abgegolten:
                row.pauschale_abgegolten = True
                aenderung = True
            if aenderung:
                session.commit()

    def forderung_ids_bereits_manuell_versendet(self, *, vertrag_id: str, stufe: int) -> set[int]:
        """Für das Doppelversand-Gate in `MahnwesenService.plane_mahnlauf`
        (unabhängige Rückprüfung Codex 22.10.2026, echter Bug: die
        vorherige `vorhanden_fuer_stufe`-Prüfung blockierte PAUSCHAL die
        GESAMTE Stufe für immer, sobald IRGENDEIN Manualimport für
        (vertrag_id, stufe) existierte - das hätte auch eine völlig neue,
        später erst fällige Forderung derselben Stufe für immer von der
        automatischen Bündelung ausgeschlossen). Liefert stattdessen NUR
        die konkreten `forderung_op_position_id`s, die TATSÄCHLICH durch
        einen Manualimport abgedeckt sind - jede ANDERE Forderung
        derselben Stufe bleibt normal automatisch mahnbar."""

        ids: set[int] = set()
        for row in self.list_fuer_vertrag(vertrag_id):
            if row.stufe != stufe:
                continue
            ids.update(json.loads(row.forderung_op_position_ids_json))
        return ids
