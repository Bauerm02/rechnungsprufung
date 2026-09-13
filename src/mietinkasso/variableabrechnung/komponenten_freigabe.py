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
angenommener Nettobetrag.

Zweite unabhängige Gegenprobe: eine neue Freigabe darf NUR die
ZEITLICH ÜBERLAPPENDEN früheren Freigaben derselben Komponente
entwerten (`freigeben`), nicht pauschal ALLE - sonst würde z. B. eine
neu angelegte September-Freigabe eine völlig unabhängige, bereits
bestätigte August-Freigabe rückwirkend stillschweigend zerstören."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.auth.service import AuthContext, require_gesellschaft_access, require_schreibrecht
from mietinkasso.domain.exceptions import OptimistischerLockKonfliktError
from mietinkasso.infrastructure.db.tables import KomponentenNettoMietFreigabeTable, VertragsKomponenteTable
from mietinkasso.op.service import compute_content_hash
from mietinkasso.stammdaten.repository import StammdatenRepository


def _zeitraeume_ueberlappen(a_von: date, a_bis: date | None, b_von: date, b_bis: date | None) -> bool:
    if a_bis is not None and a_bis < b_von:
        return False
    if b_bis is not None and b_bis < a_von:
        return False
    return True


class KomponentenNettoMietFreigabeRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def get(self, id: int) -> KomponentenNettoMietFreigabeTable | None:
        with self._session_factory() as session:
            return session.get(KomponentenNettoMietFreigabeTable, id)

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

    def anlegen_atomar(
        self, neue_row: KomponentenNettoMietFreigabeTable, *, invalidiere_ids: list[int]
    ) -> KomponentenNettoMietFreigabeTable:
        """Entwertet ATOMAR (in DERSELBEN Transaktion wie das Anlegen der
        neuen Zeile) NUR die übergebenen `invalidiere_ids` - per
        bedingter UPDATE-Anweisung (`WHERE id=X AND status='FREIGEGEBEN'`,
        `rowcount`-Prüfung), NIE pauschal alle Freigaben der Komponente.
        Trifft die Bedingung für eine der IDs nicht mehr zu (z. B. weil
        zwischenzeitlich bereits eine andere Änderung stattfand), wird
        GAR NICHTS geschrieben und ein `OptimistischerLockKonfliktError`
        geworfen - analog zu
        `VariableAbrechnungRepository.neue_version_anlegen`."""

        with self._session_factory() as session:
            for id_ in invalidiere_ids:
                ergebnis = session.execute(
                    update(KomponentenNettoMietFreigabeTable)
                    .where(KomponentenNettoMietFreigabeTable.id == id_)
                    .where(KomponentenNettoMietFreigabeTable.status == "FREIGEGEBEN")
                    .values(status="INVALIDIERT")
                )
                if ergebnis.rowcount != 1:
                    session.rollback()
                    raise OptimistischerLockKonfliktError(
                        f"Freigabe #{id_} wurde zwischenzeitlich bereits anderweitig geändert - bitte den "
                        "aktuellen Stand neu laden und erneut freigeben. Es wurde NICHTS geschrieben."
                    )
            session.add(neue_row)
            session.commit()
            session.refresh(neue_row)
            return neue_row


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

    def _komponente_und_vertrag(self, komponente_id: str):
        komponente = self._stammdaten_repository.get_komponente(komponente_id)
        if komponente is None:
            raise ValueError(f"Unbekannte Vertragskomponente {komponente_id}")
        vertrag = self._stammdaten_repository.get_vertrag(komponente.vertrag_id)
        if vertrag is None:
            raise ValueError(f"Unbekannter Vertrag {komponente.vertrag_id}")
        return komponente, vertrag

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
        aenderungsgrund: str | None = None,
    ) -> KomponentenNettoMietFreigabeTable:
        komponente, vertrag = self._komponente_und_vertrag(komponente_id)
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)
        self._stammdaten_repository.pruefe_vertrag_nicht_ausgeschlossen(vertrag.id)

        if bestaetigter_netto_betrag_cent < 0:
            raise ValueError(f"bestaetigter_netto_betrag_cent darf nicht negativ sein (erhalten: {bestaetigter_netto_betrag_cent}).")
        if not (quellenbeleg_referenz or "").strip():
            raise ValueError("Eine Netto-Mietanteil-Freigabe ohne Quellenbeleg wird abgelehnt.")
        if gueltig_bis is not None and gueltig_bis < gueltig_von:
            raise ValueError(f"gueltig_bis ({gueltig_bis}) liegt vor gueltig_von ({gueltig_von}).")

        # NUR zeitlich ÜBERLAPPENDE bestehende Freigaben derselben
        # Komponente werden ermittelt/entwertet - unabhängiger Review,
        # reproduziert: eine pauschale "entwerte ALLE" hätte eine völlig
        # unabhängige, andere Periode betreffende Freigabe (z. B. August)
        # durch eine neue, nicht überlappende Freigabe (z. B. September)
        # stillschweigend mit zerstört.
        bestehende = self._repository.freigegebene_fuer_komponente(komponente_id)
        ueberlappende = [
            f for f in bestehende if _zeitraeume_ueberlappen(f.gueltig_von, f.gueltig_bis, gueltig_von, gueltig_bis)
        ]
        if ueberlappende and not (aenderungsgrund or "").strip():
            betroffene_ids = ", ".join(f"#{f.id}" for f in ueberlappende)
            raise ValueError(
                f"Diese Freigabe überschneidet sich zeitlich mit bestehenden Freigaben ({betroffene_ids}) - "
                "das erfordert einen Änderungsgrund (begründete Korrektur). Eine NICHT überlappende "
                "historische Freigabe (z. B. ein anderer Monat) ist davon nicht betroffen und wird nie "
                "entwertet."
            )

        row = KomponentenNettoMietFreigabeTable(
            komponente_id=komponente_id,
            bestaetigter_netto_betrag_cent=bestaetigter_netto_betrag_cent,
            quellenbeleg_referenz=quellenbeleg_referenz,
            gueltig_von=gueltig_von,
            gueltig_bis=gueltig_bis,
            quelle_hash=compute_content_hash(_komponenten_snapshot(komponente)),
            status="FREIGEGEBEN",
            aenderungsgrund=aenderungsgrund,
            freigegeben_von=freigegeben_von,
        )
        return self._repository.anlegen_atomar(row, invalidiere_ids=[f.id for f in ueberlappende])

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

    def liste_fuer_komponente(self, *, ctx: AuthContext, komponente_id: str) -> list[KomponentenNettoMietFreigabeTable]:
        _, vertrag = self._komponente_und_vertrag(komponente_id)
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        return self._repository.liste_fuer_komponente(komponente_id)

    def liste_alle(self, *, ctx: AuthContext) -> list[KomponentenNettoMietFreigabeTable]:
        ergebnis = []
        for freigabe in self._repository.liste_alle():
            try:
                _, vertrag = self._komponente_und_vertrag(freigabe.komponente_id)
            except ValueError:
                continue
            if ctx.has_zugriff(vertrag.gesellschaft_id):
                ergebnis.append(freigabe)
        return ergebnis
