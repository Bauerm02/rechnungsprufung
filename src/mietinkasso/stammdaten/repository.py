from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.domain.exceptions import ObjektAusgeschlossenError
from mietinkasso.infrastructure.db.tables import (
    DebitorTable,
    EinheitTable,
    GesellschaftTable,
    KautionTable,
    KontoTable,
    ObjektTable,
    SperreTable,
    VertragTable,
    VertragsKomponenteTable,
)


class StammdatenRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    # -- Gesellschaft ---------------------------------------------------
    def upsert_gesellschaft(self, *, id: str, name: str) -> None:
        with self._session_factory() as session:
            row = session.get(GesellschaftTable, id)
            if row is None:
                session.add(GesellschaftTable(id=id, name=name))
            else:
                row.name = name
            session.commit()

    def get_gesellschaft(self, id: str) -> GesellschaftTable | None:
        with self._session_factory() as session:
            return session.get(GesellschaftTable, id)

    # -- Objekt -----------------------------------------------------------
    def upsert_objekt(
        self, *, id: str, gesellschaft_id: str, bezeichnung: str, adresse: str | None = None, ausgeschlossen: bool = False
    ) -> None:
        with self._session_factory() as session:
            row = session.get(ObjektTable, id)
            if row is None:
                session.add(
                    ObjektTable(
                        id=id,
                        gesellschaft_id=gesellschaft_id,
                        bezeichnung=bezeichnung,
                        adresse=adresse,
                        ausgeschlossen=ausgeschlossen,
                    )
                )
            else:
                row.gesellschaft_id = gesellschaft_id
                row.bezeichnung = bezeichnung
                row.adresse = adresse
                row.ausgeschlossen = ausgeschlossen
            session.commit()

    def get_objekt(self, id: str) -> ObjektTable | None:
        with self._session_factory() as session:
            return session.get(ObjektTable, id)

    # -- Einheit ----------------------------------------------------------
    def upsert_einheit(
        self,
        *,
        id: str,
        objekt_id: str,
        bezeichnung: str,
        nutzungsstatus: str,
        flaeche_qm: Decimal | None = None,
        miteigentumsanteile: Decimal | None = None,
    ) -> None:
        with self._session_factory() as session:
            row = session.get(EinheitTable, id)
            if row is None:
                session.add(
                    EinheitTable(
                        id=id,
                        objekt_id=objekt_id,
                        bezeichnung=bezeichnung,
                        nutzungsstatus=nutzungsstatus,
                        flaeche_qm=flaeche_qm,
                        miteigentumsanteile=miteigentumsanteile,
                    )
                )
            else:
                row.objekt_id = objekt_id
                row.bezeichnung = bezeichnung
                row.nutzungsstatus = nutzungsstatus
                row.flaeche_qm = flaeche_qm
                row.miteigentumsanteile = miteigentumsanteile
            session.commit()

    def get_einheit(self, id: str) -> EinheitTable | None:
        with self._session_factory() as session:
            return session.get(EinheitTable, id)

    def set_nutzungsstatus(self, *, einheit_id: str, nutzungsstatus: str) -> None:
        """Explicit, human-triggered status change. Never called by ledger
        code just because Soll==0 for a period (Leerstand must not be
        inferred automatically)."""

        with self._session_factory() as session:
            row = session.get(EinheitTable, einheit_id)
            if row is None:
                raise ValueError(f"Unbekannte Einheit {einheit_id}")
            row.nutzungsstatus = nutzungsstatus
            session.commit()

    # -- Debitor ------------------------------------------------------------
    def upsert_debitor(self, *, id: str, name: str, email: str | None = None, adresse: str | None = None) -> None:
        with self._session_factory() as session:
            row = session.get(DebitorTable, id)
            if row is None:
                session.add(DebitorTable(id=id, name=name, email=email, adresse=adresse))
            else:
                row.name = name
                row.email = email
                row.adresse = adresse
            session.commit()

    def get_debitor(self, id: str) -> DebitorTable | None:
        with self._session_factory() as session:
            return session.get(DebitorTable, id)

    # -- Vertrag ------------------------------------------------------------
    def upsert_vertrag(
        self,
        *,
        id: str,
        einheit_id: str,
        debitor_id: str,
        gesellschaft_id: str,
        rechtsordnung: str,
        gueltig_von: date,
        gueltig_bis: date | None = None,
        faelligkeit_tag: int = 5,
        zahlungsfrist_tage: int = 14,
    ) -> None:
        with self._session_factory() as session:
            row = session.get(VertragTable, id)
            if row is None:
                session.add(
                    VertragTable(
                        id=id,
                        einheit_id=einheit_id,
                        debitor_id=debitor_id,
                        gesellschaft_id=gesellschaft_id,
                        rechtsordnung=rechtsordnung,
                        gueltig_von=gueltig_von,
                        gueltig_bis=gueltig_bis,
                        faelligkeit_tag=faelligkeit_tag,
                        zahlungsfrist_tage=zahlungsfrist_tage,
                    )
                )
            else:
                row.einheit_id = einheit_id
                row.debitor_id = debitor_id
                row.gesellschaft_id = gesellschaft_id
                row.rechtsordnung = rechtsordnung
                row.gueltig_von = gueltig_von
                row.gueltig_bis = gueltig_bis
                row.faelligkeit_tag = faelligkeit_tag
                row.zahlungsfrist_tage = zahlungsfrist_tage
            session.commit()

    def get_vertrag(self, id: str) -> VertragTable | None:
        with self._session_factory() as session:
            return session.get(VertragTable, id)

    def objekt_fuer_vertrag(self, vertrag_id: str) -> ObjektTable:
        with self._session_factory() as session:
            vertrag = session.get(VertragTable, vertrag_id)
            if vertrag is None:
                raise ValueError(f"Unbekannter Vertrag {vertrag_id}")
            einheit = session.get(EinheitTable, vertrag.einheit_id)
            if einheit is None:
                raise ValueError(f"Unbekannte Einheit {vertrag.einheit_id}")
            objekt = session.get(ObjektTable, einheit.objekt_id)
            if objekt is None:
                raise ValueError(f"Unbekanntes Objekt {einheit.objekt_id}")
            return objekt

    def pruefe_vertrag_nicht_ausgeschlossen(self, vertrag_id: str) -> None:
        """Zentrale, unumgängliche Durchsetzung von Fachregel 1 (Objekt 107
        ausgeschlossen): löst Konto/Vertrag -> Einheit -> Objekt frisch aus
        der DB auf und blockiert JEDEN schreibenden Finanzpfad, unabhängig
        von Rolle (auch ADMIN) - nicht nur eine isolierte Hilfsfunktion, die
        ein Aufrufer vergessen könnte zu rufen."""

        objekt = self.objekt_fuer_vertrag(vertrag_id)
        if objekt.ausgeschlossen:
            raise ObjektAusgeschlossenError(
                f"Vertrag {vertrag_id} gehört zu Objekt {objekt.id}, das von der Pilotphase "
                "ausgeschlossen ist (z. B. 107 Sieben Dörfer)."
            )

    def pruefe_konto_nicht_ausgeschlossen(self, konto: KontoTable) -> None:
        self.pruefe_vertrag_nicht_ausgeschlossen(konto.vertrag_id)

    def add_komponente(
        self,
        *,
        id: str,
        vertrag_id: str,
        art: str,
        bezeichnung: str,
        betrag_cent: int,
        ust_satz_promille: int = 10000,
        indexierbar: bool = False,
        gueltig_von: date,
        gueltig_bis: date | None = None,
    ) -> None:
        with self._session_factory() as session:
            session.add(
                VertragsKomponenteTable(
                    id=id,
                    vertrag_id=vertrag_id,
                    art=art,
                    bezeichnung=bezeichnung,
                    betrag_cent=betrag_cent,
                    ust_satz_promille=ust_satz_promille,
                    indexierbar=indexierbar,
                    gueltig_von=gueltig_von,
                    gueltig_bis=gueltig_bis,
                )
            )
            session.commit()

    def get_komponente(self, id: str) -> VertragsKomponenteTable | None:
        with self._session_factory() as session:
            return session.get(VertragsKomponenteTable, id)

    def list_aktive_komponenten(self, vertrag_id: str, stichtag: date) -> list[VertragsKomponenteTable]:
        with self._session_factory() as session:
            statement = (
                select(VertragsKomponenteTable)
                .where(VertragsKomponenteTable.vertrag_id == vertrag_id)
                .where(VertragsKomponenteTable.gueltig_von <= stichtag)
                .where(
                    (VertragsKomponenteTable.gueltig_bis.is_(None))
                    | (VertragsKomponenteTable.gueltig_bis >= stichtag)
                )
            )
            return list(session.execute(statement).scalars().all())

    # -- Kaution (strictly separate from OP) ---------------------------------
    def set_kaution(self, *, id: str, vertrag_id: str, betrag_cent: int, stichtag: date, referenz: str | None = None) -> None:
        with self._session_factory() as session:
            existing = session.execute(
                select(KautionTable).where(KautionTable.vertrag_id == vertrag_id)
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    KautionTable(
                        id=id, vertrag_id=vertrag_id, betrag_cent=betrag_cent, stichtag=stichtag, referenz=referenz
                    )
                )
            else:
                existing.betrag_cent = betrag_cent
                existing.stichtag = stichtag
                existing.referenz = referenz
            session.commit()

    def get_kaution(self, vertrag_id: str) -> KautionTable | None:
        with self._session_factory() as session:
            return session.execute(select(KautionTable).where(KautionTable.vertrag_id == vertrag_id)).scalar_one_or_none()

    # -- Sperren --------------------------------------------------------------
    def sperre_setzen(self, *, vertrag_id: str, grund: str, kommentar: str | None = None) -> int:
        with self._session_factory() as session:
            row = SperreTable(vertrag_id=vertrag_id, grund=grund, kommentar=kommentar)
            session.add(row)
            session.commit()
            session.refresh(row)
            return row.id

    def sperre_aufheben(self, sperre_id: int) -> None:
        from datetime import datetime, timezone

        with self._session_factory() as session:
            row = session.get(SperreTable, sperre_id)
            if row is None:
                raise ValueError(f"Unbekannte Sperre {sperre_id}")
            row.aufgehoben_am = datetime.now(timezone.utc)
            session.commit()

    def aktive_sperren(self, vertrag_id: str) -> list[SperreTable]:
        with self._session_factory() as session:
            statement = (
                select(SperreTable)
                .where(SperreTable.vertrag_id == vertrag_id)
                .where(SperreTable.aufgehoben_am.is_(None))
            )
            return list(session.execute(statement).scalars().all())

    # -- Konto ------------------------------------------------------------
    def get_or_create_konto(self, *, vertrag: VertragTable) -> KontoTable:
        with self._session_factory() as session:
            existing = session.execute(
                select(KontoTable).where(KontoTable.vertrag_id == vertrag.id)
            ).scalar_one_or_none()
            if existing is not None:
                return existing
            konto = KontoTable(
                id=f"KTO-{vertrag.id}",
                vertrag_id=vertrag.id,
                debitor_id=vertrag.debitor_id,
                gesellschaft_id=vertrag.gesellschaft_id,
            )
            session.add(konto)
            session.commit()
            session.refresh(konto)
            return konto

    def get_konto(self, konto_id: str) -> KontoTable | None:
        with self._session_factory() as session:
            return session.get(KontoTable, konto_id)

    def get_konto_by_vertrag(self, vertrag_id: str) -> KontoTable | None:
        with self._session_factory() as session:
            return session.execute(select(KontoTable).where(KontoTable.vertrag_id == vertrag_id)).scalar_one_or_none()

    def set_eroeffnung_modus(self, *, konto_id: str, modus: str, stichtag: date) -> None:
        with self._session_factory() as session:
            row = session.get(KontoTable, konto_id)
            if row is None:
                raise ValueError(f"Unbekanntes Konto {konto_id}")
            row.eroeffnung_modus = modus
            row.eroeffnung_stichtag = stichtag
            session.commit()
