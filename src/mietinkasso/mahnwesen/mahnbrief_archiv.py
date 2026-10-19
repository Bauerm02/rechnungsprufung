"""Auftrag HV-20261019-MAHNKOSTEN/-VERSAND-KOSTEN-ABSCHLUSS: Archivierung
des tatsächlich versendeten PDF EINES Mahnschreibens (Kanal BRIEF) -
siehe `infrastructure/db/tables.py::MahnbriefArchivTable`-Docstring. Der
bestehende `/vertrag/{id}/mahnbrief.pdf`-Endpunkt (`backoffice/routes/
mahnwesen.py`) ist eine reine On-Demand-VORSCHAU und schreibt nichts weg;
EMAIL-Versandbelege sind bereits generisch über `AuditEventTable`
(`indexautomatik/mailnachweis.py::versand_belegen`) archiviert.

Diese Funktion ist bewusst EIGENSTÄNDIG und nimmt bereits gerenderte
PDF-Bytes entgegen (KEINE eigene PDF-Erzeugung, KEIN eigener Zugriff auf
Absender-/Settings-/Stammdaten - das bleibt `brief_pdf.py`/den
jeweiligen Aufrufern vorbehalten).

`archivieren` bindet PDF-Inhalt UND Empfänger dauerhaft an den
eingefrorenen Mahnlauf (`vertrag_id`+`stufe`+`mahnlauf_schluessel`):
ein echter Retry mit IDENTISCHEM PDF-Hash und IDENTISCHEM Empfänger
bleibt ein sicherer No-Op (liefert die bestehende Zeile zurück), ein
Versuch mit ABWEICHENDEM PDF-Inhalt ODER Empfänger ist dagegen ein
Konflikt (`VorgangIdKonfliktError`) - NIE ein stiller Erfolg mit dem
ALTEN Inhalt (Codex-Rückmeldung 19.10.2026, vorher wurde ein
abweichender Wiederholungsversuch stillschweigend mit der ersten Zeile
beantwortet). Der Forderungssnapshot selbst ist bereits über
`MahnLaufTable.mahnkosten_snapshot_json` durabel eingefroren (siehe
`mahnwesen/service.py::versende_mahnlauf`) - kein zweiter, redundanter
Hash dafür hier.

`status` bildet NUR den einen maschinell ehrlich erreichbaren Zustand
ab (`VORBEREITET` -> `UEBERMITTELT` = erfolgreich an den Post-SFTP-
Server übertragen, siehe `brief_sftp.py`) - es gibt KEINE maschinelle
Status-API für Postaufgabe/Zustellung. `importiere_anbieterbeleg`
erfasst separat den MENSCHLICH/prozessual aus der Post-Portal-
Auftragsliste entnommenen, tatsächlichen Anbieterbeleg - Kosten-/
Mahnstatusfolgen (Codex, außerhalb dieses Repos) dürfen sich
AUSSCHLIESSLICH auf dieses Feld stützen, NIE auf den bloßen
UEBERMITTELT-Uploadstatus."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mietinkasso.domain.exceptions import VorgangIdKonfliktError
from mietinkasso.infrastructure.db.tables import MahnbriefArchivTable

_STATUS_REIHENFOLGE = ("VORBEREITET", "UEBERMITTELT")


def _empfaenger_hash(empfaenger_name: str, empfaenger_adresse: str) -> str:
    normalisiert = f"{empfaenger_name.strip()}\n{empfaenger_adresse.strip()}"
    return hashlib.sha256(normalisiert.encode()).hexdigest()


def _pruefe_identisch_oder_konflikt(
    bestehend: MahnbriefArchivTable, *, pdf_sha256: str, empfaenger_hash: str,
) -> MahnbriefArchivTable:
    if bestehend.pdf_sha256 != pdf_sha256 or bestehend.empfaenger_hash != empfaenger_hash:
        raise VorgangIdKonfliktError(
            f"Mahnbrief-Archiv für Vertrag {bestehend.vertrag_id} Stufe {bestehend.stufe} "
            f"(Mahnlauf-Schlüssel {bestehend.mahnlauf_schluessel!r}) existiert bereits mit abweichendem "
            "PDF-Inhalt und/oder Empfänger - kein stiller Erfolg bei einem abweichenden Versuch."
        )
    return bestehend


class MahnbriefArchivRepository:
    def __init__(self, session_factory):
        self._session_factory = session_factory

    def fuer_mahnlauf(
        self, *, vertrag_id: str, stufe: int, mahnlauf_schluessel: str, session: Session | None = None,
    ) -> MahnbriefArchivTable | None:
        def _lesen(active_session: Session) -> MahnbriefArchivTable | None:
            return active_session.execute(
                select(MahnbriefArchivTable).where(
                    MahnbriefArchivTable.vertrag_id == vertrag_id,
                    MahnbriefArchivTable.stufe == stufe,
                    MahnbriefArchivTable.mahnlauf_schluessel == mahnlauf_schluessel,
                )
            ).scalar_one_or_none()

        if session is not None:
            return _lesen(session)
        with self._session_factory() as owned_session:
            return _lesen(owned_session)

    def archivieren(
        self, *, vertrag_id: str, stufe: int, mahnlauf_schluessel: str, pdf_bytes: bytes,
        versandnachweis_referenz: str, empfaenger_name: str, empfaenger_adresse: str, erstellt_von: str,
    ) -> MahnbriefArchivTable:
        """Idempotent je (vertrag_id, stufe, mahnlauf_schluessel) - siehe
        `MahnbriefArchivTable.uq_mahnbriefarchiv_lauf`-Docstring UND
        Moduldoc für die Konfliktprüfung gegen PDF-Inhalt/Empfänger."""

        pdf_sha256 = hashlib.sha256(pdf_bytes).hexdigest()
        empfaenger_hash = _empfaenger_hash(empfaenger_name, empfaenger_adresse)

        bestehend = self.fuer_mahnlauf(vertrag_id=vertrag_id, stufe=stufe, mahnlauf_schluessel=mahnlauf_schluessel)
        if bestehend is not None:
            return _pruefe_identisch_oder_konflikt(bestehend, pdf_sha256=pdf_sha256, empfaenger_hash=empfaenger_hash)

        with self._session_factory() as session:
            try:
                row = MahnbriefArchivTable(
                    vertrag_id=vertrag_id, stufe=stufe, mahnlauf_schluessel=mahnlauf_schluessel,
                    pdf_bytes=pdf_bytes, pdf_sha256=pdf_sha256, empfaenger_hash=empfaenger_hash,
                    versandnachweis_referenz=versandnachweis_referenz, erstellt_von=erstellt_von,
                    status="VORBEREITET",
                )
                session.add(row)
                session.commit()
                session.refresh(row)
                return row
            except IntegrityError:
                # Ein gleichzeitiger zweiter Versuch für DENSELBEN
                # Mahnlauf (Race-Sicherheitsnetz, analog
                # `MahnkostenBuchungTable`) - idempotent/Konflikt wie oben,
                # KEIN automatischer stiller Erfolg bei Abweichung.
                session.rollback()
                bestehend = self.fuer_mahnlauf(vertrag_id=vertrag_id, stufe=stufe, mahnlauf_schluessel=mahnlauf_schluessel)
                if bestehend is None:
                    raise RuntimeError(
                        f"Mahnbrief-Archivierung für Vertrag {vertrag_id} Stufe {stufe} "
                        f"(Mahnlauf-Schlüssel {mahnlauf_schluessel}) ist an einer IntegrityError gescheitert, "
                        "aber es existiert KEINE eigene Archivzeile - manuelle Klärung erforderlich."
                    ) from None
                return _pruefe_identisch_oder_konflikt(bestehend, pdf_sha256=pdf_sha256, empfaenger_hash=empfaenger_hash)

    def aktualisiere_status(
        self, *, archiv_id: int, neuer_status: str, beleg_referenz: str, erfasst_von: str,
        session: Session | None = None,
    ) -> MahnbriefArchivTable:
        """Einziger maschinell ehrlicher Statusübergang
        (`VORBEREITET` -> `UEBERMITTELT`, siehe Moduldoc) - IMMER mit
        expliziter `beleg_referenz` (hier: der dauerhaft reservierte
        SFTP-Dateiname, `brief_sftp.BriefSftpErgebnis.externe_referenz`).
        Ein echter Retry mit IDENTISCHER `beleg_referenz` ist ein
        No-Op; eine ABWEICHENDE Referenz für denselben bereits erreichten
        Status ist ein Konflikt; ein Rückschritt ist NIE erlaubt."""

        if neuer_status not in _STATUS_REIHENFOLGE:
            raise ValueError(f"Unbekannter Mahnbrief-Archivstatus {neuer_status!r}.")
        if not beleg_referenz or not beleg_referenz.strip():
            raise ValueError("Ein Statuswechsel verlangt eine explizite Belegreferenz.")

        def _aktualisieren(active_session: Session) -> MahnbriefArchivTable:
            row = active_session.get(MahnbriefArchivTable, archiv_id, with_for_update=True)
            if row is None:
                raise ValueError(f"Unbekannte Mahnbrief-Archivzeile {archiv_id}.")
            aktueller_index = _STATUS_REIHENFOLGE.index(row.status)
            neuer_index = _STATUS_REIHENFOLGE.index(neuer_status)
            if neuer_index == aktueller_index:
                if row.status_beleg_referenz != beleg_referenz:
                    raise VorgangIdKonfliktError(
                        f"Archivzeile {archiv_id} hat bereits Status {row.status!r} mit abweichender "
                        f"Belegreferenz ({row.status_beleg_referenz!r} != {beleg_referenz!r})."
                    )
                return row  # echter Retry, No-Op
            if neuer_index < aktueller_index:
                raise ValueError(
                    f"Archivzeile {archiv_id}: Statuswechsel von {row.status!r} zurück auf {neuer_status!r} "
                    "ist nicht erlaubt (nur vorwärts)."
                )
            row.status = neuer_status
            row.status_beleg_referenz = beleg_referenz
            row.status_erfasst_von = erfasst_von
            row.status_aktualisiert_am = datetime.now(timezone.utc)
            active_session.flush()
            return row

        if session is not None:
            return _aktualisieren(session)
        with self._session_factory() as owned_session:
            ergebnis = _aktualisieren(owned_session)
            owned_session.commit()
            owned_session.refresh(ergebnis)
            return ergebnis

    def importiere_anbieterbeleg(
        self, *, archiv_id: int, anbieterbeleg_referenz: str, importiert_von: str, session: Session | None = None,
    ) -> MahnbriefArchivTable:
        """Menschlich/prozessual erfasster NACHWEIS aus der echten
        Portal-Auftragsliste des Post-Anbieters (KEINE maschinelle
        Status-API) - bindet sich an eine einmal importierte Referenz:
        ein echter Retry mit identischer Referenz bleibt ein No-Op, ein
        zweiter Import mit ABWEICHENDER Referenz ist ein Konflikt.
        Kosten-/Mahnstatusfolgen dürfen sich NUR auf dieses Feld
        stützen, nie auf den bloßen `status=="UEBERMITTELT"`."""

        if not anbieterbeleg_referenz or not anbieterbeleg_referenz.strip():
            raise ValueError("Ein Anbieterbeleg-Import verlangt eine explizite, nicht-leere Referenz.")

        def _importieren(active_session: Session) -> MahnbriefArchivTable:
            row = active_session.get(MahnbriefArchivTable, archiv_id, with_for_update=True)
            if row is None:
                raise ValueError(f"Unbekannte Mahnbrief-Archivzeile {archiv_id}.")
            if row.anbieterbeleg_referenz is not None:
                if row.anbieterbeleg_referenz != anbieterbeleg_referenz:
                    raise VorgangIdKonfliktError(
                        f"Archivzeile {archiv_id} hat bereits einen Anbieterbeleg "
                        f"({row.anbieterbeleg_referenz!r}) - abweichender Import ({anbieterbeleg_referenz!r}) "
                        "wird abgelehnt statt den bestehenden Nachweis stillschweigend zu ersetzen."
                    )
                return row  # echter Retry, No-Op
            row.anbieterbeleg_referenz = anbieterbeleg_referenz
            row.anbieterbeleg_importiert_von = importiert_von
            row.anbieterbeleg_importiert_am = datetime.now(timezone.utc)
            active_session.flush()
            return row

        if session is not None:
            return _importieren(session)
        with self._session_factory() as owned_session:
            ergebnis = _importieren(owned_session)
            owned_session.commit()
            owned_session.refresh(ergebnis)
            return ergebnis
