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
eingefrorenen Mahnlauf (`vertrag_id`+`stufe`+`mahnlauf_schluessel`) UND
reserviert dabei (Codex-Abnahme 19.10.2026) einen PERMANENTEN, opaken
`sende_schluessel` (UUID) - VOR jedem Netzkontakt, nicht erst beim
tatsächlichen Versand: ein echter Retry mit IDENTISCHEM PDF-Hash und
IDENTISCHEM Empfänger bleibt ein sicherer No-Op (liefert dieselbe Zeile
samt demselben `sende_schluessel` zurück), ein Versuch mit ABWEICHENDEM
PDF-Inhalt ODER Empfänger ist dagegen ein Konflikt
(`VorgangIdKonfliktError`) - NIE ein stiller Erfolg mit dem ALTEN Inhalt.
Der Forderungssnapshot selbst ist bereits über `MahnLaufTable.
mahnkosten_snapshot_json` durabel eingefroren (siehe `mahnwesen/
service.py::versende_mahnlauf`) - kein zweiter, redundanter Hash dafür
hier.

`status` bildet NUR den einen maschinell ehrlich erreichbaren Zustand
ab (`VORBEREITET` -> `UEBERMITTELT` = erfolgreich an den Post-SFTP-
Server übertragen, siehe `brief_sftp.py`) - es gibt KEINE maschinelle
Status-API für Postaufgabe/Zustellung. `importiere_anbieterbeleg`
erfasst separat den MENSCHLICH/prozessual aus der Post-Portal-
Auftragsliste entnommenen, tatsächlichen Anbieterbeleg - ein bloßer
Referenztext ist dabei KEIN Beleg, es wird IMMER das tatsächlich
hochgeladene Belegdokument (PDF-Bytes + Hash) verlangt. Kosten-/
Mahnstatusfolgen (siehe `indexautomatik/mailversand_service.py::
HVMailversandService.vervollstaendige_mahnlauf_aus_anbieterbeleg`)
dürfen sich AUSSCHLIESSLICH auf dieses Feld stützen, NIE auf den
bloßen UEBERMITTELT-Uploadstatus."""

from __future__ import annotations

import hashlib
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mietinkasso.domain.exceptions import VorgangIdKonfliktError
from mietinkasso.infrastructure.db.sqlite_write_lock import schreibgesperrte_session
from mietinkasso.infrastructure.db.tables import MahnbriefArchivTable

_STATUS_REIHENFOLGE = ("VORBEREITET", "UEBERMITTELT")


def _empfaenger_hash(empfaenger_name: str, empfaenger_adresse: str) -> str:
    normalisiert = f"{empfaenger_name.strip()}\n{empfaenger_adresse.strip()}"
    return hashlib.sha256(normalisiert.encode()).hexdigest()


def _pruefe_identisch_oder_konflikt(
    bestehend: MahnbriefArchivTable, *, pdf_sha256: str, empfaenger_hash: str, versandnachweis_referenz: str,
) -> MahnbriefArchivTable:
    if (
        bestehend.pdf_sha256 != pdf_sha256
        or bestehend.empfaenger_hash != empfaenger_hash
        # Codex-Abnahme 19.10.2026, zweite Korrektur: `versandnachweis_
        # referenz` wurde bislang NICHT verglichen - ein Retry mit
        # abweichender Referenz (z. B. ein anderer `mahnlauf.outbox_key`-
        # Verweis) wäre bisher still mit der ALTEN Referenz akzeptiert
        # worden. Jetzt ebenso Teil der unveränderlichen Identität.
        or bestehend.versandnachweis_referenz != versandnachweis_referenz
    ):
        raise VorgangIdKonfliktError(
            f"Mahnbrief-Archiv für Vertrag {bestehend.vertrag_id} Stufe {bestehend.stufe} "
            f"(Mahnlauf-Schlüssel {bestehend.mahnlauf_schluessel!r}) existiert bereits mit abweichendem "
            "PDF-Inhalt, Empfänger und/oder Versandnachweis-Referenz - kein stiller Erfolg bei einem "
            "abweichenden Versuch."
        )
    return bestehend


class MahnbriefArchivRepository:
    def __init__(self, session_factory):
        self._session_factory = session_factory

    def get(self, archiv_id: int, *, session: Session | None = None) -> MahnbriefArchivTable | None:
        if session is not None:
            return session.get(MahnbriefArchivTable, archiv_id)
        with self._session_factory() as owned_session:
            return owned_session.get(MahnbriefArchivTable, archiv_id)

    def list_fuer_vertrag(self, vertrag_id: str) -> list[MahnbriefArchivTable]:
        with self._session_factory() as session:
            return list(session.execute(
                select(MahnbriefArchivTable)
                .where(MahnbriefArchivTable.vertrag_id == vertrag_id)
                .order_by(MahnbriefArchivTable.archiviert_am.desc())
            ).scalars())

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
        Moduldoc für die Konfliktprüfung gegen PDF-Inhalt/Empfänger.
        Vergibt bei NEUER Anlage einen permanenten `sende_schluessel`
        (UUID4-Hex) - VOR jedem Netzkontakt, eine unveränderte
        Wiederholung behält denselben Schlüssel."""

        pdf_sha256 = hashlib.sha256(pdf_bytes).hexdigest()
        empfaenger_hash = _empfaenger_hash(empfaenger_name, empfaenger_adresse)

        bestehend = self.fuer_mahnlauf(vertrag_id=vertrag_id, stufe=stufe, mahnlauf_schluessel=mahnlauf_schluessel)
        if bestehend is not None:
            return _pruefe_identisch_oder_konflikt(
                bestehend, pdf_sha256=pdf_sha256, empfaenger_hash=empfaenger_hash,
                versandnachweis_referenz=versandnachweis_referenz,
            )

        with self._session_factory() as session:
            try:
                row = MahnbriefArchivTable(
                    vertrag_id=vertrag_id, stufe=stufe, mahnlauf_schluessel=mahnlauf_schluessel,
                    pdf_bytes=pdf_bytes, pdf_sha256=pdf_sha256, empfaenger_hash=empfaenger_hash,
                    versandnachweis_referenz=versandnachweis_referenz, erstellt_von=erstellt_von,
                    status="VORBEREITET", sende_schluessel=uuid.uuid4().hex,
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
                return _pruefe_identisch_oder_konflikt(
                    bestehend, pdf_sha256=pdf_sha256, empfaenger_hash=empfaenger_hash,
                    versandnachweis_referenz=versandnachweis_referenz,
                )

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
        self, *, archiv_id: int, anbieterbeleg_pdf_bytes: bytes, anbieterbeleg_referenz: str,
        postaufgabe_datum: date, importiert_von: str, session: Session | None = None,
    ) -> MahnbriefArchivTable:
        """Menschlich/prozessual erfasster NACHWEIS aus der echten
        Portal-Auftragsliste des Post-Anbieters (KEINE maschinelle
        Status-API) - ein bloßer Referenztext ist KEIN Beleg, verlangt
        wird IMMER das tatsächlich hochgeladene Belegdokument (PDF-
        Bytes) samt dem TATSÄCHLICHEN Postaufgabedatum. Bindet sich an
        eine einmal importierte Kombination: ein echter Retry mit
        IDENTISCHEM Beleg-Hash/Referenz/Datum bleibt ein No-Op, ein
        zweiter Import mit ABWEICHENDEM Beleg für DIESELBE Archivzeile
        ist ein Konflikt. Eine Belegreferenz, die bereits einer ANDEREN
        Archivzeile zugeordnet ist, wird ebenfalls als Konflikt
        abgelehnt (fremde/mehrfach genutzte Belegreferenz) - jeder
        tatsächliche Postaufgabebeleg gehört zu GENAU EINER Sendung."""

        if not anbieterbeleg_referenz or not anbieterbeleg_referenz.strip():
            raise ValueError("Ein Anbieterbeleg-Import verlangt eine explizite, nicht-leere Referenz.")
        if not anbieterbeleg_pdf_bytes or anbieterbeleg_pdf_bytes[:5] != b"%PDF-":
            raise ValueError("Ein Anbieterbeleg-Import verlangt das tatsächliche Belegdokument (PDF) - ein Referenztext allein ist kein Beleg.")
        if postaufgabe_datum is None:
            raise ValueError("Ein Anbieterbeleg-Import verlangt das tatsächliche Postaufgabedatum.")

        beleg_sha256 = hashlib.sha256(anbieterbeleg_pdf_bytes).hexdigest()

        def _importieren(active_session: Session) -> MahnbriefArchivTable:
            row = active_session.get(MahnbriefArchivTable, archiv_id, with_for_update=True)
            if row is None:
                raise ValueError(f"Unbekannte Mahnbrief-Archivzeile {archiv_id}.")

            fremde_zeile = active_session.execute(
                select(MahnbriefArchivTable).where(
                    MahnbriefArchivTable.anbieterbeleg_referenz == anbieterbeleg_referenz,
                    MahnbriefArchivTable.id != archiv_id,
                )
            ).scalar_one_or_none()
            if fremde_zeile is not None:
                raise VorgangIdKonfliktError(
                    f"Belegreferenz {anbieterbeleg_referenz!r} ist bereits Archivzeile {fremde_zeile.id} "
                    f"zugeordnet - eine Belegreferenz gehört zu GENAU EINER Sendung, kein Mehrfachimport "
                    f"für Zeile {archiv_id}."
                )

            if row.anbieterbeleg_referenz is not None:
                identisch = (
                    row.anbieterbeleg_referenz == anbieterbeleg_referenz
                    and row.anbieterbeleg_pdf_sha256 == beleg_sha256
                    and row.anbieterbeleg_postaufgabe_datum == postaufgabe_datum
                )
                if not identisch:
                    raise VorgangIdKonfliktError(
                        f"Archivzeile {archiv_id} hat bereits einen Anbieterbeleg "
                        f"({row.anbieterbeleg_referenz!r}, {row.anbieterbeleg_postaufgabe_datum}) - ein "
                        "abweichender Import (anderes Dokument, andere Referenz oder anderes Postaufgabedatum) "
                        "wird abgelehnt statt den bestehenden Nachweis stillschweigend zu ersetzen."
                    )
                return row  # echter Retry, No-Op

            row.anbieterbeleg_referenz = anbieterbeleg_referenz
            row.anbieterbeleg_pdf_bytes = anbieterbeleg_pdf_bytes
            row.anbieterbeleg_pdf_sha256 = beleg_sha256
            row.anbieterbeleg_postaufgabe_datum = postaufgabe_datum
            row.anbieterbeleg_importiert_von = importiert_von
            row.anbieterbeleg_importiert_am = datetime.now(timezone.utc)
            active_session.flush()
            return row

        if session is not None:
            return _importieren(session)
        # Schreibgesperrt (Codex-Abnahme 19.10.2026, zweite Korrektur):
        # die Mehrfachverwendungsprüfung oben ist ein "lesen, dann
        # entscheiden, dann schreiben"-Ablauf - ohne echten Schreib-Lock
        # könnten zwei gleichzeitige Importe mit DERSELBEN Belegreferenz
        # für ZWEI verschiedene Archivzeilen beide die Prüfung passieren,
        # bevor einer von beiden committet (siehe `sqlite_write_lock.py`).
        with schreibgesperrte_session(self._session_factory) as owned_session:
            ergebnis = _importieren(owned_session)
            owned_session.commit()
            owned_session.refresh(ergebnis)
            return ergebnis
