"""Separate, additive Netto-Mietanteil-Freigabe je Vertragskomponente,
AUSSCHLIESSLICH für die Nettomieterlös-Monatsübersicht (Auftrag 13.09.,
HV-20260913-DASHBOARD, unabhängiger Review: "Bestehende
Vertragskomponenten sind historisch teilweise BRUTTO gespeichert, auch
bei art=HMZ/KUECHE/PARKPLATZ; ust_satz_promille ist vorhanden, beweist
allein aber keine Betragsbasis").

Ändert NIE `VertragsKomponenteTable`/`OPPositionTable` - Altbeträge/OP
bleiben unverändert. `quelle_hash` bindet die Freigabe an den Stand der
referenzierten Komponente zum Freigabezeitpunkt; jede spätere Änderung
entwertet sie automatisch (`ist_noch_gueltig`, analog
`RechtsprofilService`). Eine ungeprüfte Bestandskomponente OHNE aktive
Freigabe ist in der Monatsübersicht IMMER eine Datenlücke, nie ein
angenommener Nettobetrag."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.auth.service import AuthContext, require_gesellschaft_access, require_schreibrecht
from mietinkasso.infrastructure.db.tables import KomponentenNettoMietFreigabeTable, VertragsKomponenteTable
from mietinkasso.op.service import compute_content_hash
from mietinkasso.stammdaten.repository import StammdatenRepository


class KomponentenNettoMietFreigabeRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def anlegen(self, row: KomponentenNettoMietFreigabeTable) -> KomponentenNettoMietFreigabeTable:
        with self._session_factory() as session:
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def get(self, id: int) -> KomponentenNettoMietFreigabeTable | None:
        with self._session_factory() as session:
            return session.get(KomponentenNettoMietFreigabeTable, id)

    def invalidieren(self, id: int) -> None:
        with self._session_factory() as session:
            row = session.get(KomponentenNettoMietFreigabeTable, id)
            if row is not None and row.status == "FREIGEGEBEN":
                row.status = "INVALIDIERT"
                session.commit()

    def liste_fuer_komponente(self, komponente_id: str) -> list[KomponentenNettoMietFreigabeTable]:
        with self._session_factory() as session:
            statement = (
                select(KomponentenNettoMietFreigabeTable)
                .where(KomponentenNettoMietFreigabeTable.komponente_id == komponente_id)
                .order_by(KomponentenNettoMietFreigabeTable.id.desc())
            )
            return list(session.execute(statement).scalars().all())

    def freigegebene_fuer_komponente(self, komponente_id: str) -> list[KomponentenNettoMietFreigabeTable]:
        with self._session_factory() as session:
            statement = (
                select(KomponentenNettoMietFreigabeTable)
                .where(KomponentenNettoMietFreigabeTable.komponente_id == komponente_id)
                .where(KomponentenNettoMietFreigabeTable.status == "FREIGEGEBEN")
            )
            return list(session.execute(statement).scalars().all())

    def liste_alle(self) -> list[KomponentenNettoMietFreigabeTable]:
        with self._session_factory() as session:
            statement = select(KomponentenNettoMietFreigabeTable).order_by(KomponentenNettoMietFreigabeTable.id.desc())
            return list(session.execute(statement).scalars().all())


def _komponenten_snapshot(komponente: VertragsKomponenteTable) -> dict:
    return {
        "vertrag_id": komponente.vertrag_id,
        "art": komponente.art,
        "bezeichnung": komponente.bezeichnung,
        "betrag_cent": komponente.betrag_cent,
        "ust_satz_promille": komponente.ust_satz_promille,
        "gueltig_von": str(komponente.gueltig_von),
        "gueltig_bis": str(komponente.gueltig_bis) if komponente.gueltig_bis else None,
    }


class KomponentenNettoMietFreigabeService:
    def __init__(self, repository: KomponentenNettoMietFreigabeRepository, stammdaten_repository: StammdatenRepository):
        self._repository = repository
        self._stammdaten_repository = stammdaten_repository

    def freigeben(
        self,
        *,
        ctx: AuthContext,
        komponente_id: str,
        bestaetigter_netto_betrag_cent: int,
        quellenbeleg_referenz: str,
        gueltig_von: date,
        gueltig_bis: date | None,
        freigegeben_von: str,
    ) -> KomponentenNettoMietFreigabeTable:
        komponente = self._stammdaten_repository.get_komponente(komponente_id)
        if komponente is None:
            raise ValueError(f"Unbekannte Vertragskomponente {komponente_id}")
        vertrag = self._stammdaten_repository.get_vertrag(komponente.vertrag_id)
        if vertrag is None:
            raise ValueError(f"Unbekannter Vertrag {komponente.vertrag_id}")
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)
        self._stammdaten_repository.pruefe_vertrag_nicht_ausgeschlossen(vertrag.id)

        if bestaetigter_netto_betrag_cent < 0:
            raise ValueError(f"bestaetigter_netto_betrag_cent darf nicht negativ sein (erhalten: {bestaetigter_netto_betrag_cent}).")
        if not (quellenbeleg_referenz or "").strip():
            raise ValueError("Eine Netto-Mietanteil-Freigabe ohne Quellenbeleg wird abgelehnt.")
        if gueltig_bis is not None and gueltig_bis < gueltig_von:
            raise ValueError(f"gueltig_bis ({gueltig_bis}) liegt vor gueltig_von ({gueltig_von}).")

        # Frühere aktive Freigaben derselben Komponente werden entwertet -
        # es gibt bewusst nur EINE aktuell gültige Freigabe je Komponente,
        # keine Überlappung mehrerer FREIGEGEBEN-Zeilen mit potenziell
        # widersprüchlichen Beträgen für denselben Zeitraum.
        for alte in self._repository.freigegebene_fuer_komponente(komponente_id):
            self._repository.invalidieren(alte.id)

        row = KomponentenNettoMietFreigabeTable(
            komponente_id=komponente_id,
            bestaetigter_netto_betrag_cent=bestaetigter_netto_betrag_cent,
            quellenbeleg_referenz=quellenbeleg_referenz,
            gueltig_von=gueltig_von,
            gueltig_bis=gueltig_bis,
            quelle_hash=compute_content_hash(_komponenten_snapshot(komponente)),
            status="FREIGEGEBEN",
            freigegeben_von=freigegeben_von,
        )
        return self._repository.anlegen(row)

    def ist_noch_gueltig(self, freigabe: KomponentenNettoMietFreigabeTable) -> bool:
        if freigabe.status != "FREIGEGEBEN":
            return False
        komponente = self._stammdaten_repository.get_komponente(freigabe.komponente_id)
        if komponente is None:
            return False
        return compute_content_hash(_komponenten_snapshot(komponente)) == freigabe.quelle_hash

    def aktive_freigabe_fuer_monat(
        self, komponente_id: str, *, monatsanfang: date, monatsende: date
    ) -> KomponentenNettoMietFreigabeTable | None:
        """Liefert die FREIGEGEBEN-Zeile NUR, wenn sie inhaltlich noch
        gültig ist (`ist_noch_gueltig`) UND ihre eigene Gültigkeit den
        GESAMTEN gewählten Monat abdeckt - eine nur teilweise für den
        Monat gültige Freigabe zählt NICHT als bestätigte Basis für
        diesen vollen Monat (keine erfundene anteilige Berechnung)."""

        for freigabe in self._repository.freigegebene_fuer_komponente(komponente_id):
            if freigabe.gueltig_von > monatsanfang:
                continue
            if freigabe.gueltig_bis is not None and freigabe.gueltig_bis < monatsende:
                continue
            if self.ist_noch_gueltig(freigabe):
                return freigabe
        return None

    def liste_fuer_komponente(self, komponente_id: str) -> list[KomponentenNettoMietFreigabeTable]:
        return self._repository.liste_fuer_komponente(komponente_id)

    def liste_alle(self) -> list[KomponentenNettoMietFreigabeTable]:
        return self._repository.liste_alle()
