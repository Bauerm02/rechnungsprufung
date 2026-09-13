"""Reines CRUD für die Indexautomatik-Tabellen - keine Auth-/Fachregel-
Prüfung (die lebt in den `*_service.py`-Dateien, siehe bestehende
Konvention aus `mieweg_vorschau/`, `mahnwesen/`)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.infrastructure.db.tables import (
    ErhoehungsschreibenTable,
    IndexautomatikLaufTable,
    RechtsprofilTable,
    VertragsendeErinnerungTable,
    VpiJahreswertTable,
    VpiMonatswertTable,
)


class VpiRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    # -- Jahresdurchschnitt-Override (manuell, mit Beleg) --------------------
    def jahreswert_erfassen(
        self,
        *,
        reihe: str,
        jahr: int,
        wert: Decimal,
        quelle: str,
        quelle_datum: date,
        erfasst_von: str,
        finalitaet: str = "ENDGUELTIG",
    ) -> VpiJahreswertTable:
        with self._session_factory() as session:
            statement = select(VpiJahreswertTable).where(
                VpiJahreswertTable.reihe == reihe, VpiJahreswertTable.jahr == jahr
            )
            row = session.execute(statement).scalars().first()
            if row is None:
                row = VpiJahreswertTable(
                    reihe=reihe, jahr=jahr, wert=wert, finalitaet=finalitaet, quelle=quelle,
                    quelle_datum=quelle_datum, erfasst_von=erfasst_von,
                )
                session.add(row)
            else:
                row.wert = wert
                row.finalitaet = finalitaet
                row.quelle = quelle
                row.quelle_datum = quelle_datum
                row.erfasst_von = erfasst_von
            session.commit()
            session.refresh(row)
            return row

    def jahreswert_liste(self, reihe: str | None = None) -> list[VpiJahreswertTable]:
        with self._session_factory() as session:
            statement = select(VpiJahreswertTable).order_by(VpiJahreswertTable.reihe, VpiJahreswertTable.jahr)
            if reihe is not None:
                statement = statement.where(VpiJahreswertTable.reihe == reihe)
            return list(session.execute(statement).scalars().all())

    # -- Monatswerte (amtlicher Import) ---------------------------------------
    def monatswert_erfassen(
        self,
        *,
        reihe: str,
        jahr: int,
        monat: int,
        wert: Decimal,
        finalitaet: str,
        quelle_datei: str,
        quelle_zeile: int | None,
        quelle_hash: str,
        abgerufen_am: datetime,
        importiert_von: str,
    ) -> VpiMonatswertTable:
        with self._session_factory() as session:
            statement = select(VpiMonatswertTable).where(
                VpiMonatswertTable.reihe == reihe, VpiMonatswertTable.jahr == jahr, VpiMonatswertTable.monat == monat
            )
            row = session.execute(statement).scalars().first()
            if row is None:
                row = VpiMonatswertTable(
                    reihe=reihe,
                    jahr=jahr,
                    monat=monat,
                    wert=wert,
                    finalitaet=finalitaet,
                    quelle_datei=quelle_datei,
                    quelle_zeile=quelle_zeile,
                    quelle_hash=quelle_hash,
                    abgerufen_am=abgerufen_am,
                    importiert_von=importiert_von,
                )
                session.add(row)
            else:
                row.wert = wert
                row.finalitaet = finalitaet
                row.quelle_datei = quelle_datei
                row.quelle_zeile = quelle_zeile
                row.quelle_hash = quelle_hash
                row.abgerufen_am = abgerufen_am
                row.importiert_von = importiert_von
            session.commit()
            session.refresh(row)
            return row

    def batch_erfassen(
        self, *, monatszeilen: list[dict], jahreszeilen: list[dict]
    ) -> None:
        """Ein kompletter OGD-Import (`vpi_import.py::importiere_ogd_csv`)
        in EINER Transaktion - Zwischenreview (34c6fdd): "die aktuelle
        Schleife commit je Zeile ist trotz Docstring nicht atomar bei
        DB-Fehler/Absturz". Ein Fehler bei irgendeiner Zeile lässt die
        GESAMTE Transaktion zurückrollen, kein Teilimport."""

        with self._session_factory() as session:
            for zeile in monatszeilen:
                statement = select(VpiMonatswertTable).where(
                    VpiMonatswertTable.reihe == zeile["reihe"],
                    VpiMonatswertTable.jahr == zeile["jahr"],
                    VpiMonatswertTable.monat == zeile["monat"],
                )
                row = session.execute(statement).scalars().first()
                if row is None:
                    session.add(VpiMonatswertTable(**zeile))
                else:
                    for feld, wert in zeile.items():
                        setattr(row, feld, wert)
            for zeile in jahreszeilen:
                statement = select(VpiJahreswertTable).where(
                    VpiJahreswertTable.reihe == zeile["reihe"], VpiJahreswertTable.jahr == zeile["jahr"]
                )
                row = session.execute(statement).scalars().first()
                if row is None:
                    session.add(VpiJahreswertTable(**zeile))
                else:
                    for feld, wert in zeile.items():
                        setattr(row, feld, wert)
            session.commit()

    def monatswerte_liste(self, reihe: str, jahr: int) -> list[VpiMonatswertTable]:
        with self._session_factory() as session:
            statement = (
                select(VpiMonatswertTable)
                .where(VpiMonatswertTable.reihe == reihe, VpiMonatswertTable.jahr == jahr)
                .order_by(VpiMonatswertTable.monat)
            )
            return list(session.execute(statement).scalars().all())

    def jahresdurchschnitt(self, reihe: str, jahr: int) -> Decimal | None:
        """Liefert den Jahresdurchschnitt für (Reihe, Jahr) - bevorzugt
        einen manuell erfassten `VpiJahreswertTable`-Override; sonst nur,
        wenn ALLE 12 Monate vorliegen UND ausnahmslos ENDGUELTIG sind
        (kein teilweise erfundener/vorläufiger Durchschnitt)."""

        with self._session_factory() as session:
            override_statement = select(VpiJahreswertTable).where(
                VpiJahreswertTable.reihe == reihe, VpiJahreswertTable.jahr == jahr
            )
            override = session.execute(override_statement).scalars().first()
            if override is not None and override.finalitaet == "ENDGUELTIG":
                return override.wert
            monate = self.monatswerte_liste(reihe, jahr)
            if len(monate) != 12:
                return None
            if any(m.finalitaet != "ENDGUELTIG" for m in monate):
                return None
            return sum((m.wert for m in monate), Decimal("0")) / 12

    def neuester_endgueltiger_monatswert(self, reihe: str, bis: date) -> Decimal | None:
        """Für die vertragliche Klausel-Spur (`vertragsspur.py`): der
        jüngste ENDGUELTIGe Monatswert auf oder vor `bis` - ein noch
        VORLAEUFIGer letzter Monat (Statistik-Austria-Praxis: der
        jeweils letzte veröffentlichte Monat ist vorläufig, wird erst
        mit dem Folgemonat final) wird bewusst NICHT verwendet."""

        with self._session_factory() as session:
            statement = (
                select(VpiMonatswertTable)
                .where(VpiMonatswertTable.reihe == reihe)
                .where(VpiMonatswertTable.finalitaet == "ENDGUELTIG")
                .where(
                    (VpiMonatswertTable.jahr < bis.year)
                    | ((VpiMonatswertTable.jahr == bis.year) & (VpiMonatswertTable.monat <= bis.month))
                )
                .order_by(VpiMonatswertTable.jahr.desc(), VpiMonatswertTable.monat.desc())
                .limit(1)
            )
            row = session.execute(statement).scalars().first()
            return row.wert if row else None

    def jahresdurchschnitte_fuer(self, reihe: str, jahre: set[int]) -> dict[int, Decimal]:
        ergebnis: dict[int, Decimal] = {}
        for jahr in jahre:
            wert = self.jahresdurchschnitt(reihe, jahr)
            if wert is not None:
                ergebnis[jahr] = wert
        return ergebnis


class RechtsprofilRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def naechste_version(self, vertrag_id: str) -> int:
        with self._session_factory() as session:
            bisher = session.execute(
                select(func.max(RechtsprofilTable.version)).where(RechtsprofilTable.vertrag_id == vertrag_id)
            ).scalar_one_or_none()
            return (bisher or 0) + 1

    def anlegen(self, row: RechtsprofilTable) -> RechtsprofilTable:
        with self._session_factory() as session:
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def get(self, id: int) -> RechtsprofilTable | None:
        with self._session_factory() as session:
            return session.get(RechtsprofilTable, id)

    def liste_fuer_vertrag(self, vertrag_id: str) -> list[RechtsprofilTable]:
        with self._session_factory() as session:
            statement = (
                select(RechtsprofilTable)
                .where(RechtsprofilTable.vertrag_id == vertrag_id)
                .order_by(RechtsprofilTable.version.desc())
            )
            return list(session.execute(statement).scalars().all())

    def freigegebenes_profil(self, vertrag_id: str) -> RechtsprofilTable | None:
        with self._session_factory() as session:
            statement = (
                select(RechtsprofilTable)
                .where(RechtsprofilTable.vertrag_id == vertrag_id)
                .where(RechtsprofilTable.status == "FREIGEGEBEN")
                .order_by(RechtsprofilTable.version.desc())
                .limit(1)
            )
            return session.execute(statement).scalars().first()

    def freigeben(self, id: int, *, freigegeben_von: str, quelle_hash: str) -> RechtsprofilTable:
        with self._session_factory() as session:
            row = session.get(RechtsprofilTable, id)
            if row is None:
                raise ValueError(f"Unbekanntes Rechtsprofil {id}")
            # Genau ein FREIGEGEBENES Profil je Vertrag: eine neue Freigabe
            # invalidiert automatisch jede vorher freigegebene Version
            # desselben Vertrags (kein doppelt "aktives" Profil möglich).
            session.execute(
                update(RechtsprofilTable)
                .where(RechtsprofilTable.vertrag_id == row.vertrag_id)
                .where(RechtsprofilTable.status == "FREIGEGEBEN")
                .values(status="INVALIDIERT")
            )
            row.status = "FREIGEGEBEN"
            row.freigegeben_von = freigegeben_von
            row.freigegeben_am = datetime.now(timezone.utc)
            row.quelle_hash = quelle_hash
            session.commit()
            session.refresh(row)
            return row

    def invalidieren(self, id: int) -> None:
        with self._session_factory() as session:
            row = session.get(RechtsprofilTable, id)
            if row is None:
                raise ValueError(f"Unbekanntes Rechtsprofil {id}")
            row.status = "INVALIDIERT"
            session.commit()


#: Ein Lauf in einem dieser Zustände darf erneut versucht werden (z. B.
#: nachdem eine fehlende VPI-Publikation nachträglich erfasst wurde),
#: OHNE ein bereits erzeugtes Erhöhungsschreiben zu verdoppeln -
#: Modellreview 13.09.: "Begründet blockierte Fälle müssen nach
#: Quellen-/Profiländerung sicher erneut prüfbar sein, ohne fertige
#: Schreiben zu verdoppeln". Ein TERMINALER Status (ERHOEHUNG_ERZEUGT/
#: KEIN_ERHOEHUNGSBEDARF/BEREITS_ERFASST) wird NIE erneut versucht.
_RETRYABLE_LAUF_STATUS = {"LAEUFT", "BLOCKIERT", "TERMIN_NICHT_ERREICHT"}


class IndexautomatikLaufRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def get_by_periode(self, vertrag_id: str, periode: str) -> IndexautomatikLaufTable | None:
        with self._session_factory() as session:
            statement = select(IndexautomatikLaufTable).where(
                IndexautomatikLaufTable.vertrag_id == vertrag_id, IndexautomatikLaufTable.periode == periode
            )
            return session.execute(statement).scalars().first()

    def claim_periode(self, vertrag_id: str, periode: str) -> IndexautomatikLaufTable | None:
        """Atomarer Erzeugungsclaim VOR jeder seiteneffektbehafteten
        Berechnung (Modellreview 13.09.: "Pro Vertrag+Monat atomarer
        Erzeugungsclaim (Geschäftsraum-Duplikate derzeit möglich)") -
        ohne diesen Claim könnten zwei parallele Worker beide den
        "existiert noch nicht"-Zustand sehen und beide
        `IndexService.berechne_vorschlag`/ein Erhöhungsschreiben
        erzeugen, bevor die abschließende `IndexautomatikLaufTable`-
        Unique-Zeile das verhindert. Liefert die geclaimte Zeile
        (Status LAEUFT) zurück - `None`, wenn bereits ein TERMINALER
        Lauf existiert oder ein anderer Worker gerade selbst claimt."""

        with self._session_factory() as session:
            neu = IndexautomatikLaufTable(vertrag_id=vertrag_id, periode=periode, status="LAEUFT", blockiert_gruende=[])
            session.add(neu)
            try:
                session.commit()
                session.refresh(neu)
                return neu
            except IntegrityError:
                session.rollback()

            result = session.execute(
                update(IndexautomatikLaufTable)
                .where(IndexautomatikLaufTable.vertrag_id == vertrag_id, IndexautomatikLaufTable.periode == periode)
                .where(IndexautomatikLaufTable.status.in_(_RETRYABLE_LAUF_STATUS))
                .values(status="LAEUFT")
            )
            session.commit()
            if result.rowcount == 0:
                return None
            return self.get_by_periode(vertrag_id, periode)

    def abschliessen(
        self,
        id: int,
        *,
        status: str,
        blockiert_gruende: list[str] | None = None,
        rechtsprofil_id: int | None = None,
        rechtsprofil_version: int | None = None,
        mieweg_vorschau_id: int | None = None,
        erhoehungsschreiben_id: int | None = None,
    ) -> IndexautomatikLaufTable:
        with self._session_factory() as session:
            row = session.get(IndexautomatikLaufTable, id)
            if row is None:
                raise ValueError(f"Unbekannter IndexautomatikLauf {id}")
            row.status = status
            row.blockiert_gruende = list(blockiert_gruende or [])
            row.rechtsprofil_id = rechtsprofil_id
            row.rechtsprofil_version = rechtsprofil_version
            row.mieweg_vorschau_id = mieweg_vorschau_id
            row.erhoehungsschreiben_id = erhoehungsschreiben_id
            session.commit()
            session.refresh(row)
            return row

    def liste_fuer_vertrag(self, vertrag_id: str) -> list[IndexautomatikLaufTable]:
        with self._session_factory() as session:
            statement = (
                select(IndexautomatikLaufTable)
                .where(IndexautomatikLaufTable.vertrag_id == vertrag_id)
                .order_by(IndexautomatikLaufTable.periode.desc())
            )
            return list(session.execute(statement).scalars().all())


class ErhoehungsschreibenRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def get(self, id: int) -> ErhoehungsschreibenTable | None:
        with self._session_factory() as session:
            return session.get(ErhoehungsschreibenTable, id)

    def get_by_ziel(self, vertrag_id: str, ziel_bewertungsjahr: int) -> ErhoehungsschreibenTable | None:
        with self._session_factory() as session:
            statement = select(ErhoehungsschreibenTable).where(
                ErhoehungsschreibenTable.vertrag_id == vertrag_id,
                ErhoehungsschreibenTable.ziel_bewertungsjahr == ziel_bewertungsjahr,
            )
            return session.execute(statement).scalars().first()

    def get_by_index_anpassung(self, index_anpassung_id: int) -> ErhoehungsschreibenTable | None:
        with self._session_factory() as session:
            statement = select(ErhoehungsschreibenTable).where(
                ErhoehungsschreibenTable.index_anpassung_id == index_anpassung_id
            )
            return session.execute(statement).scalars().first()

    def anlegen(self, row: ErhoehungsschreibenTable) -> ErhoehungsschreibenTable:
        """Idempotent - für den MieWeG-Pfad (`ziel_bewertungsjahr`
        gesetzt) über den partiellen (vertrag_id, ziel_bewertungsjahr)-
        Unique-Index; für den Geschäftsraum-/Klausel-Pfad
        (`index_anpassung_id` gesetzt, `ziel_bewertungsjahr` NULL) ist
        die Idempotenz bereits vorgelagert über
        `IndexautomatikLaufRepository` (höchstens ein Versuch pro
        Kalendermonat) sichergestellt - ein `IntegrityError` sollte für
        diesen Pfad nicht auftreten, wird hier aber trotzdem defensiv
        über `index_anpassung_id` aufgelöst statt weiterzuwerfen."""

        with self._session_factory() as session:
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                if row.ziel_bewertungsjahr is not None:
                    bestehend = self.get_by_ziel(row.vertrag_id, row.ziel_bewertungsjahr)
                elif row.index_anpassung_id is not None:
                    bestehend = self.get_by_index_anpassung(row.index_anpassung_id)
                else:
                    bestehend = None
                if bestehend is None:
                    raise
                return bestehend
            session.refresh(row)
            return row

    def claim_fuer_versand(self, id: int, *, jetzt: datetime | None = None) -> bool:
        """Atomarer Compare-and-Swap BEREIT->IN_VERSAND (analog
        `MahnFallRepository.claim_fuer_versand`): nur der Worker, dessen
        UPDATE tatsächlich eine Zeile trifft, darf den Transportadapter
        aufrufen. Unabhängiger Review (0d65e2b): die vorherige Fassung
        setzte NUR `versand_beansprucht_am`, ließ `status` aber auf
        BEREIT stehen - das WHERE-Kriterium eines zweiten, praktisch
        gleichzeitigen Aufrufs traf dadurch weiterhin zu und beide
        Worker erhielten `True` (Doppelversand). Der Status wechselt
        jetzt selbst TEIL des atomaren UPDATE-Prädikats zu IN_VERSAND,
        exakt wie beim bestehenden Mahnwesen-Muster."""

        with self._session_factory() as session:
            result = session.execute(
                update(ErhoehungsschreibenTable)
                .where(ErhoehungsschreibenTable.id == id)
                .where(ErhoehungsschreibenTable.status == "BEREIT")
                .values(status="IN_VERSAND", versand_beansprucht_am=jetzt or datetime.now(timezone.utc))
            )
            session.commit()
            return result.rowcount > 0

    def verwaiste_in_versand(self, *, aelter_als: datetime) -> list[ErhoehungsschreibenTable]:
        """Recovery für einen Absturz zwischen `claim_fuer_versand` und
        dem Auflösen des Ergebnisses (analog
        `mahnwesen/service.py::markiere_verwaiste_als_unsicher`)."""

        with self._session_factory() as session:
            statement = select(ErhoehungsschreibenTable).where(
                ErhoehungsschreibenTable.status == "IN_VERSAND",
                ErhoehungsschreibenTable.versand_beansprucht_am < aelter_als,
            )
            return list(session.execute(statement).scalars().all())

    def aktualisieren(self, id: int, **felder) -> ErhoehungsschreibenTable:
        """Überschreibt ein bestehendes ENTWURF/BLOCKIERT-Schreiben mit
        neu berechneten Feldern (Retry nach behobener Quelle) - NIEMALS
        für ein bereits GESENDET/ZUGANG_BESTAETIGT/AUSGEFUEHRT-Schreiben
        aufrufen (das bleibt unveränderlich, siehe Aufrufer in
        `service.py`)."""

        with self._session_factory() as session:
            row = session.get(ErhoehungsschreibenTable, id)
            if row is None:
                raise ValueError(f"Unbekanntes Erhoehungsschreiben {id}")
            for feld, wert in felder.items():
                setattr(row, feld, wert)
            session.commit()
            session.refresh(row)
            return row

    def set_status(self, id: int, status: str, **felder) -> ErhoehungsschreibenTable:
        with self._session_factory() as session:
            row = session.get(ErhoehungsschreibenTable, id)
            if row is None:
                raise ValueError(f"Unbekanntes Erhoehungsschreiben {id}")
            row.status = status
            for feld, wert in felder.items():
                setattr(row, feld, wert)
            session.commit()
            session.refresh(row)
            return row

    def liste_fuer_vertrag(self, vertrag_id: str) -> list[ErhoehungsschreibenTable]:
        with self._session_factory() as session:
            statement = (
                select(ErhoehungsschreibenTable)
                .where(ErhoehungsschreibenTable.vertrag_id == vertrag_id)
                .order_by(ErhoehungsschreibenTable.ziel_bewertungsjahr.desc())
            )
            return list(session.execute(statement).scalars().all())

    def liste_nach_status(self, status: str) -> list[ErhoehungsschreibenTable]:
        with self._session_factory() as session:
            statement = select(ErhoehungsschreibenTable).where(ErhoehungsschreibenTable.status == status)
            return list(session.execute(statement).scalars().all())

    def liste_alle(self) -> list[ErhoehungsschreibenTable]:
        with self._session_factory() as session:
            statement = select(ErhoehungsschreibenTable).order_by(ErhoehungsschreibenTable.id.desc())
            return list(session.execute(statement).scalars().all())


class VertragsendeErinnerungRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def get(self, id: int) -> VertragsendeErinnerungTable | None:
        with self._session_factory() as session:
            return session.get(VertragsendeErinnerungTable, id)

    def get_by_enddatum(self, vertrag_id: str, end_datum: date) -> VertragsendeErinnerungTable | None:
        with self._session_factory() as session:
            statement = select(VertragsendeErinnerungTable).where(
                VertragsendeErinnerungTable.vertrag_id == vertrag_id,
                VertragsendeErinnerungTable.end_datum == end_datum,
            )
            return session.execute(statement).scalars().first()

    def anlegen(self, row: VertragsendeErinnerungTable) -> VertragsendeErinnerungTable:
        with self._session_factory() as session:
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                bestehend = self.get_by_enddatum(row.vertrag_id, row.end_datum)
                if bestehend is None:
                    raise
                return bestehend
            session.refresh(row)
            return row

    def invalidiere_veraltete(
        self, vertrag_id: str, aktuelles_end_datum: date | None
    ) -> list[VertragsendeErinnerungTable]:
        """Fachregel: ändert sich `VertragTable.gueltig_bis` (Verlängerung/
        Verkürzung ODER Wechsel auf unbefristet), werden alle noch nicht
        ungültigen Erinnerungen für ein ANDERES Enddatum desselben
        Vertrags ungültig - eine neue Zeile für ein neues, weiterhin
        befristetes Enddatum wird separat (idempotent über den
        Unique-Constraint) angelegt.

        Unabhängiger Review (0d65e2b): zwei bisherige Lücken behoben -
        (1) `aktuelles_end_datum=None` (Vertrag wurde unbefristet)
        invalidiert jetzt ALLE noch nicht ungültigen Erinnerungen
        dieses Vertrags, unabhängig vom jeweiligen `end_datum` (ein
        SQL-`!=`-Vergleich gegen NULL wäre sonst nie wahr gewesen und
        hätte stattdessen fälschlich GAR NICHTS invalidiert). (2) auch
        eine bereits `ENTSCHIEDEN`e, aber laut Fachregel NIE automatisch
        versendete Zeile (der Mieterentwurf bleibt bis zur manuellen
        Freigabe durch einen Menschen ungesendet) wird bei einer
        Enddatum-Änderung mit invalidiert, nicht nur OFFEN/
        BENACHRICHTIGT."""

        with self._session_factory() as session:
            statement = (
                select(VertragsendeErinnerungTable)
                .where(VertragsendeErinnerungTable.vertrag_id == vertrag_id)
                .where(VertragsendeErinnerungTable.status.in_(["OFFEN", "BENACHRICHTIGT", "ENTSCHIEDEN"]))
            )
            if aktuelles_end_datum is not None:
                statement = statement.where(VertragsendeErinnerungTable.end_datum != aktuelles_end_datum)
            betroffene = list(session.execute(statement).scalars().all())
            for row in betroffene:
                row.status = "UNGUELTIG"
            session.commit()
            for row in betroffene:
                session.refresh(row)
            return betroffene

    def set_status(self, id: int, status: str, **felder) -> VertragsendeErinnerungTable:
        with self._session_factory() as session:
            row = session.get(VertragsendeErinnerungTable, id)
            if row is None:
                raise ValueError(f"Unbekannte VertragsendeErinnerung {id}")
            row.status = status
            for feld, wert in felder.items():
                setattr(row, feld, wert)
            session.commit()
            session.refresh(row)
            return row

    def claim_fuer_versand(self, id: int, *, jetzt: datetime | None = None) -> bool:
        """Atomarer CAS OFFEN->IN_VERSAND (analog
        `ErhoehungsschreibenRepository.claim_fuer_versand`) - verhindert,
        dass zwei parallele Läufe dieselbe fällige Erinnerung doppelt
        an den Eigentümer versenden."""

        with self._session_factory() as session:
            result = session.execute(
                update(VertragsendeErinnerungTable)
                .where(VertragsendeErinnerungTable.id == id)
                .where(VertragsendeErinnerungTable.status == "OFFEN")
                .values(status="IN_VERSAND", versand_beansprucht_am=jetzt or datetime.now(timezone.utc))
            )
            session.commit()
            return result.rowcount > 0

    def verwaiste_in_versand(self, *, aelter_als: datetime) -> list[VertragsendeErinnerungTable]:
        with self._session_factory() as session:
            statement = select(VertragsendeErinnerungTable).where(
                VertragsendeErinnerungTable.status == "IN_VERSAND",
                VertragsendeErinnerungTable.versand_beansprucht_am < aelter_als,
            )
            return list(session.execute(statement).scalars().all())

    def liste_faellig(self, *, heute: date) -> list[VertragsendeErinnerungTable]:
        with self._session_factory() as session:
            statement = (
                select(VertragsendeErinnerungTable)
                .where(VertragsendeErinnerungTable.status == "OFFEN")
                .where(VertragsendeErinnerungTable.faellig_am <= heute)
            )
            return list(session.execute(statement).scalars().all())

    def liste_alle(self) -> list[VertragsendeErinnerungTable]:
        with self._session_factory() as session:
            statement = select(VertragsendeErinnerungTable).order_by(VertragsendeErinnerungTable.faellig_am)
            return list(session.execute(statement).scalars().all())
