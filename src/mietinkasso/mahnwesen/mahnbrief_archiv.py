"""Auftrag HV-20261019-MAHNKOSTEN: Archivierung des tatsächlich
versendeten PDF EINES Mahnschreibens (Kanal BRIEF) - siehe
`infrastructure/db/tables.py::MahnbriefArchivTable`-Docstring. Der
bestehende `/vertrag/{id}/mahnbrief.pdf`-Endpunkt (`backoffice/app.py`)
ist eine reine On-Demand-VORSCHAU und schreibt nichts weg; EMAIL-
Versandbelege sind bereits generisch über `AuditEventTable`
(`indexautomatik/mailnachweis.py::versand_belegen`) archiviert.

Diese Funktion ist bewusst EIGENSTÄNDIG und nimmt bereits gerenderte
PDF-Bytes entgegen (KEINE eigene PDF-Erzeugung, KEIN eigener
Zugriff auf Absender-/Settings-/Stammdaten - das bleibt
`backoffice/brief_pdf.py`/`backoffice/app.py` vorbehalten). Sie wird
erst wirksam eingebunden, sobald ein echter Brief-Versandweg existiert
(`brief_transport_verfuegbar`, aktuell `False` - siehe
`docs/hausverwaltung/OFFENE_PUNKTE.md`); bis dahin bleibt dies ein
eigenständiger, fertig getesteter Baustein ohne Aufrufer im Live-Pfad."""

from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mietinkasso.infrastructure.db.tables import MahnbriefArchivTable


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
        versandnachweis_referenz: str, erstellt_von: str,
    ) -> MahnbriefArchivTable:
        """Idempotent je (vertrag_id, stufe, mahnlauf_schluessel) - siehe
        `MahnbriefArchivTable.uq_mahnbriefarchiv_lauf`-Docstring. Ein
        wiederholter Aufruf für DENSELBEN eingefrorenen Mahnlauf liefert
        die bereits archivierte Zeile zurück, statt ein zweites Mal zu
        schreiben oder einen Fehler zu werfen."""

        bestehend = self.fuer_mahnlauf(vertrag_id=vertrag_id, stufe=stufe, mahnlauf_schluessel=mahnlauf_schluessel)
        if bestehend is not None:
            return bestehend

        pdf_sha256 = hashlib.sha256(pdf_bytes).hexdigest()
        with self._session_factory() as session:
            try:
                row = MahnbriefArchivTable(
                    vertrag_id=vertrag_id, stufe=stufe, mahnlauf_schluessel=mahnlauf_schluessel,
                    pdf_bytes=pdf_bytes, pdf_sha256=pdf_sha256,
                    versandnachweis_referenz=versandnachweis_referenz, erstellt_von=erstellt_von,
                )
                session.add(row)
                session.commit()
                session.refresh(row)
                return row
            except IntegrityError:
                # Ein gleichzeitiger zweiter Versuch für DENSELBEN
                # Mahnlauf (Race-Sicherheitsnetz, analog
                # `MahnkostenBuchungTable`) - kein Fehler, sondern der
                # Idempotenz-Fall.
                session.rollback()
                bestehend = self.fuer_mahnlauf(vertrag_id=vertrag_id, stufe=stufe, mahnlauf_schluessel=mahnlauf_schluessel)
                if bestehend is None:
                    raise RuntimeError(
                        f"Mahnbrief-Archivierung für Vertrag {vertrag_id} Stufe {stufe} "
                        f"(Mahnlauf-Schlüssel {mahnlauf_schluessel}) ist an einer IntegrityError gescheitert, "
                        "aber es existiert KEINE eigene Archivzeile - manuelle Klärung erforderlich."
                    ) from None
                return bestehend
