from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.domain.enums import MahnStatus
from mietinkasso.infrastructure.db.tables import (
    BriefAnbieterProfilTable, MahnEmpfaengerZugangTable, MahnFallTable, MahnKanalregelTable, MahnLaufTable,
    MahnPolicyTable, VertragMitmieterTable,
)


class MahnKanalregelRepository:
    """Versionierte Kanalregel (ENTWURF -> FREIGEGEBEN), fast wörtliches
    Pendant zu `MahnPolicyRepository` - siehe `MahnKanalregelTable`-
    Docstring. `MahnwesenService._resolve_kanal` verwendet AUSSCHLIESSLICH
    die aktuell freigegebene Regel; fehlt jede, bleibt der bisherige
    Code-Default (EMAIL für beide Stufen, siehe dortige Docstring)
    unverändert wirksam - eine neue Kanalzuordnung braucht KEINEN Deploy,
    nur eine neue freigegebene Version, entfaltet aber auch erst DANN
    tatsächlich Wirkung."""

    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def naechste_version(self) -> int:
        with self._session_factory() as session:
            versionen = [v for (v,) in session.execute(select(MahnKanalregelTable.version)).all()]
            return (max(versionen) + 1) if versionen else 1

    def anlegen(self, **kwargs) -> MahnKanalregelTable:
        with self._session_factory() as session:
            row = MahnKanalregelTable(version=self.naechste_version(), **kwargs)
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def freigeben(self, regel_id: int, *, freigegeben_von: str) -> MahnKanalregelTable:
        with self._session_factory() as session:
            row = session.get(MahnKanalregelTable, regel_id)
            if row is None:
                raise ValueError(f"Unbekannte MahnKanalregel {regel_id}")
            if row.status != "ENTWURF":
                raise ValueError(f"MahnKanalregel {regel_id} ist bereits {row.status}, keine erneute Freigabe.")
            row.status = "FREIGEGEBEN"
            row.geprueft_von = freigegeben_von
            row.geprueft_am = datetime.now(timezone.utc)
            session.commit()
            session.refresh(row)
            return row

    def aktuelle_freigegebene(self) -> MahnKanalregelTable | None:
        with self._session_factory() as session:
            return session.execute(
                select(MahnKanalregelTable).where(MahnKanalregelTable.status == "FREIGEGEBEN")
                .order_by(MahnKanalregelTable.version.desc()).limit(1)
            ).scalar_one_or_none()

    def alle(self) -> list[MahnKanalregelTable]:
        with self._session_factory() as session:
            return list(session.execute(select(MahnKanalregelTable).order_by(MahnKanalregelTable.version.desc())).scalars())


class BriefAnbieterProfilRepository:
    """Versioniertes, geprüftes Preis-/Tarifprofil je Briefart (ENTWURF ->
    FREIGEGEBEN), fast wörtliches Pendant zu `MahnPolicyRepository` -
    siehe `BriefAnbieterProfilTable`-Docstring. Fehlt jede freigegebene
    Version für die gewünschte `briefart`, bleibt der Briefkanal explizit
    blockiert (`mahnwesen/service.py::_pruefe_frisch_versandbereit`)."""

    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def naechste_version(self, *, briefart: str) -> int:
        with self._session_factory() as session:
            versionen = [v for (v,) in session.execute(
                select(BriefAnbieterProfilTable.version).where(BriefAnbieterProfilTable.briefart == briefart)
            ).all()]
            return (max(versionen) + 1) if versionen else 1

    def anlegen(self, *, briefart: str = "STANDARD", **kwargs) -> BriefAnbieterProfilTable:
        with self._session_factory() as session:
            row = BriefAnbieterProfilTable(briefart=briefart, version=self.naechste_version(briefart=briefart), **kwargs)
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def freigeben(self, profil_id: int, *, freigegeben_von: str) -> BriefAnbieterProfilTable:
        with self._session_factory() as session:
            row = session.get(BriefAnbieterProfilTable, profil_id)
            if row is None:
                raise ValueError(f"Unbekanntes BriefAnbieterProfil {profil_id}")
            if row.status != "ENTWURF":
                raise ValueError(f"BriefAnbieterProfil {profil_id} ist bereits {row.status}, keine erneute Freigabe.")
            row.status = "FREIGEGEBEN"
            row.geprueft_von = freigegeben_von
            row.geprueft_am = datetime.now(timezone.utc)
            session.commit()
            session.refresh(row)
            return row

    def aktuelle_freigegebene(self, *, briefart: str = "STANDARD") -> BriefAnbieterProfilTable | None:
        with self._session_factory() as session:
            return session.execute(
                select(BriefAnbieterProfilTable)
                .where(BriefAnbieterProfilTable.briefart == briefart, BriefAnbieterProfilTable.status == "FREIGEGEBEN")
                .order_by(BriefAnbieterProfilTable.version.desc()).limit(1)
            ).scalar_one_or_none()

    def alle(self, *, briefart: str | None = None) -> list[BriefAnbieterProfilTable]:
        with self._session_factory() as session:
            statement = select(BriefAnbieterProfilTable).order_by(BriefAnbieterProfilTable.version.desc())
            if briefart is not None:
                statement = statement.where(BriefAnbieterProfilTable.briefart == briefart)
            return list(session.execute(statement).scalars())


class MahnPolicyRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def naechste_version(self) -> int:
        with self._session_factory() as session:
            versionen = [v for (v,) in session.execute(select(MahnPolicyTable.version)).all()]
            return (max(versionen) + 1) if versionen else 1

    def anlegen(self, **kwargs) -> MahnPolicyTable:
        with self._session_factory() as session:
            row = MahnPolicyTable(version=self.naechste_version(), **kwargs)
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def freigeben(self, policy_id: int) -> MahnPolicyTable:
        with self._session_factory() as session:
            row = session.get(MahnPolicyTable, policy_id)
            if row is None:
                raise ValueError(f"Unbekannte MahnPolicy {policy_id}")
            row.status = "FREIGEGEBEN"
            row.freigegeben_am = datetime.now(timezone.utc)
            session.commit()
            session.refresh(row)
            return row

    def aktuelle_freigegebene(self) -> MahnPolicyTable | None:
        with self._session_factory() as session:
            statement = (
                select(MahnPolicyTable)
                .where(MahnPolicyTable.status == "FREIGEGEBEN")
                .order_by(MahnPolicyTable.version.desc())
                .limit(1)
            )
            return session.execute(statement).scalar_one_or_none()

    def alle(self) -> list[MahnPolicyTable]:
        with self._session_factory() as session:
            statement = select(MahnPolicyTable).order_by(MahnPolicyTable.version.desc())
            return list(session.execute(statement).scalars().all())


class MahnFallRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def get_or_create(self, *, outbox_key: str, **kwargs) -> MahnFallTable:
        """Race-safe wie bei der Vorschreibung: zwei Worker, die gleichzeitig
        denselben Fall planen, landen wegen der Unique-Constraint auf
        outbox_key garantiert bei genau einer Zeile."""

        with self._session_factory() as session:
            existing = session.execute(
                select(MahnFallTable).where(MahnFallTable.outbox_key == outbox_key)
            ).scalar_one_or_none()
            if existing is not None:
                return existing
            row = MahnFallTable(outbox_key=outbox_key, **kwargs)
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return session.execute(
                    select(MahnFallTable).where(MahnFallTable.outbox_key == outbox_key)
                ).scalar_one()
            session.refresh(row)
            return row

    def get(self, mahnfall_id: int) -> MahnFallTable | None:
        with self._session_factory() as session:
            return session.get(MahnFallTable, mahnfall_id)

    def get_by_outbox_key(self, outbox_key: str) -> MahnFallTable | None:
        """Reiner Lesezugriff (KEIN `get_or_create`) - für eine Vorschau,
        die einen ggf. bereits geplanten Fall anzeigen darf, ohne selbst
        einen anzulegen (Auftrag Markus 14.09.2026: GET darf keine
        Mahnfälle schreiben, siehe `MahnwesenService.vorschau_
        forderung`)."""

        with self._session_factory() as session:
            return session.execute(
                select(MahnFallTable).where(MahnFallTable.outbox_key == outbox_key)
            ).scalar_one_or_none()

    def list_fuer_vertrag(self, vertrag_id: str) -> list[MahnFallTable]:
        with self._session_factory() as session:
            statement = (
                select(MahnFallTable)
                .where(MahnFallTable.vertrag_id == vertrag_id)
                .order_by(MahnFallTable.id.desc())
            )
            return list(session.execute(statement).scalars().all())

    def letzter_mahnfall_fuer_forderung(self, forderung_op_position_id: int) -> MahnFallTable | None:
        """Jede Forderung (identifiziert über die sie erzeugende OP-Zeile)
        hat ihren EIGENEN Stufe1->Stufe2-Zyklus, unabhängig davon, wie weit
        andere Forderungen desselben Vertrags schon gediehen sind."""

        with self._session_factory() as session:
            statement = (
                select(MahnFallTable)
                .where(MahnFallTable.forderung_op_position_id == forderung_op_position_id)
                .order_by(MahnFallTable.id.desc())
                .limit(1)
            )
            return session.execute(statement).scalar_one_or_none()

    def letzter_mahnfall_je_stufe_fuer_forderung(self, forderung_op_position_id: int, stufe: int) -> MahnFallTable | None:
        with self._session_factory() as session:
            statement = (
                select(MahnFallTable)
                .where(MahnFallTable.forderung_op_position_id == forderung_op_position_id)
                .where(MahnFallTable.stufe == stufe)
                .order_by(MahnFallTable.id.desc())
                .limit(1)
            )
            return session.execute(statement).scalar_one_or_none()

    def claim_fuer_versand(self, mahnfall_id: int, *, jetzt: datetime | None = None) -> bool:
        """Atomarer Compare-and-Swap GEPLANT -> IN_VERSAND: nur der Worker,
        dessen UPDATE eine Zeile trifft, darf tatsächlich den
        Versand-Provider aufrufen. Ein zweiter Worker (oder ein Neustart,
        der denselben Fall erneut anstößt) sieht rowcount==0 und bricht ab,
        statt doppelt zu versenden. Der Status bleibt danach so lange
        IN_VERSAND, bis `versenden()` ihn auf GESENDET/UNSICHER auflöst -
        ein Absturz mittendrin macht den Fall NICHT wieder als GEPLANT
        greifbar (siehe `verwaiste_in_versand` für die Recovery)."""

        with self._session_factory() as session:
            result = session.execute(
                update(MahnFallTable)
                .where(MahnFallTable.id == mahnfall_id)
                .where(MahnFallTable.status == MahnStatus.GEPLANT.value)
                .values(status=MahnStatus.IN_VERSAND.value, versand_beansprucht_am=jetzt or datetime.now(timezone.utc))
            )
            session.commit()
            return result.rowcount > 0

    def verwaiste_in_versand(self, *, aelter_als: datetime) -> list[MahnFallTable]:
        """Fälle, die seit `aelter_als` in IN_VERSAND feststecken - z. B.
        weil der Worker zwischen `claim_fuer_versand` und dem Auflösen des
        Ergebnisses abgestürzt ist. Werden NICHT automatisch erneut
        versucht (kein blinder Retry), sondern für die manuelle Klärung als
        Kandidaten zurückgegeben."""

        with self._session_factory() as session:
            statement = (
                select(MahnFallTable)
                .where(MahnFallTable.status == MahnStatus.IN_VERSAND.value)
                .where(MahnFallTable.versand_beansprucht_am < aelter_als)
            )
            return list(session.execute(statement).scalars().all())

    def set_status(self, mahnfall_id: int, status: str, **zusatz) -> MahnFallTable:
        with self._session_factory() as session:
            row = session.get(MahnFallTable, mahnfall_id)
            if row is None:
                raise ValueError(f"Unbekannter MahnFall {mahnfall_id}")
            row.status = status
            for key, value in zusatz.items():
                setattr(row, key, value)
            session.commit()
            session.refresh(row)
            return row


class MahnLaufRepository:
    """Persistente, atomare Gruppensperre für einen gebündelten
    Mahnversand (siehe `MahnLaufTable`-Docstring in `tables.py`) - ein
    fast wörtliches Pendant zu `MahnFallRepository`, aber auf der
    GRUPPENEBENE statt je einzelnem Fall. `outbox_key` enthält
    ABSICHTLICH KEINEN Kanal: dieselbe eingefrorene Mitgliedermenge
    desselben (Vertrag, Stufe) MUSS über Kanäle hinweg (E-Mail, künftig
    Brief) auf DIESELBE Zeile treffen, damit ein zweiter Kanal niemals
    unabhängig um dieselben Forderungen konkurrieren kann (Rückprüfung
    14.09.2026, Risiko 1: kein bloßer Leader-CAS)."""

    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def get_or_create(self, *, outbox_key: str, **kwargs) -> MahnLaufTable:
        with self._session_factory() as session:
            existing = session.execute(
                select(MahnLaufTable).where(MahnLaufTable.outbox_key == outbox_key)
            ).scalar_one_or_none()
            if existing is not None:
                return existing
            row = MahnLaufTable(outbox_key=outbox_key, **kwargs)
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return session.execute(
                    select(MahnLaufTable).where(MahnLaufTable.outbox_key == outbox_key)
                ).scalar_one()
            session.refresh(row)
            return row

    def claim_mitglieder_und_erstelle_gruppe(
        self, *, mahnfall_ids: list[int], outbox_key: str, vertrag_id: str, gesellschaft_id: str,
        stufe: int, kanal: str = "EMAIL",
    ) -> MahnLaufTable | None:
        """Bindet den Mitglieder-Claim (GEPLANT -> GEBUENDELT auf
        `MahnFallTable`) UND die Anlage der `MahnLaufTable`-Zeile in
        GENAU EINER Datenbanktransaktion zusammen (unabhängige
        Rückprüfung Codex 14.09.2026, echter Bug: die vorherige Version
        führte beides in ZWEI separaten, jeweils für sich committeten
        Schritten aus - ein Absturz/Prozesskill genau dazwischen ließ
        die Mitglieder für immer GEBUENDELT OHNE zugehörige Gruppenzeile
        zurück: weder durch eine künftige Planung erreichbar (GEBUENDELT
        ist kein Planungskandidat mehr) noch durch die MahnLauf-Recovery
        auflösbar (es existiert ja gar keine Zeile). Ein reines
        try/except auf Python-Ebene hätte das NICHT verhindert - die
        Sicherheit kommt hier ausschließlich aus der EINEN gemeinsamen
        Transaktionsgrenze: entweder committen BEIDE Änderungen
        zusammen, oder KEINE von beiden.

        Gibt `None` zurück, wenn der Mitglieder-Claim fehlschlägt (ein
        anderer, gleichzeitiger Planungsversuch war schneller) - dann
        wird auch KEINE Gruppenzeile angelegt. Bereits existierende
        Zeile für denselben `outbox_key` wird grundsätzlich unverändert
        zurückgegeben (Idempotenz wie `get_or_create`), OHNE die
        Mitglieder erneut zu claimen (sie sind es unter diesem
        `outbox_key` bereits) - AUSSER sie ist eine tote, VOR jedem
        Providerkontakt blockierte Zeile (`status == "BLOCKIERT"` UND
        `versand_beansprucht_am is None`): unabhängige Rückprüfung Codex
        14.09.2026, echter Bug - deren Mitglieder wurden beim Blockieren
        bereits wieder auf GEPLANT freigegeben
        (`MahnwesenService._freigebe_gebuendelte_mitglieder`), der
        deterministische `outbox_key` (Hash der exakten Mitgliedermenge)
        darf diese Menge dann NICHT dauerhaft unbenutzbar machen, falls
        exakt dieselbe Menge später wieder versandbereit wird - eine
        solche Zeile wird für den neuen Claim WIEDERVERWENDET statt eine
        zweite Zeile mit identischem, unique-constraint-geschütztem
        Schlüssel anzulegen. Jeder andere Zustand (insbesondere
        IN_VERSAND/UNSICHER/GESENDET - tatsächlicher oder unklarer
        Providerkontakt) bleibt für IMMER geschützt und wird nie
        wiederverwendet."""

        if not mahnfall_ids:
            return None
        with self._session_factory() as session:
            existing = session.execute(
                select(MahnLaufTable).where(MahnLaufTable.outbox_key == outbox_key)
            ).scalar_one_or_none()
            if existing is not None and not (existing.status == "BLOCKIERT" and existing.versand_beansprucht_am is None):
                # Entweder eine echte GEPLANT-Race (ein gleichzeitiger
                # anderer Planungsversuch hat diese Zeile soeben
                # angelegt) oder ein Zustand mit tatsächlichem/unklarem
                # Providerkontakt (IN_VERSAND/UNSICHER/GESENDET) - in
                # KEINEM dieser Fälle darf der Schlüssel erneut
                # beansprucht werden.
                return existing
            if existing is not None:
                # Unabhängige Rückprüfung Codex 14.09.2026, echter Bug:
                # eine Gruppe, die VOR jedem Providerkontakt blockiert
                # wurde (`versand_beansprucht_am is None`), hat ihre
                # Mitglieder bereits über `MahnwesenService.
                # _freigebe_gebuendelte_mitglieder` wieder auf GEPLANT
                # freigegeben - der `outbox_key` (deterministisch aus der
                # exakten Mitgliedermenge) darf diese Menge dann NICHT
                # dauerhaft unbenutzbar machen, falls exakt dieselbe
                # Menge später wieder versandbereit wird. Diese tote
                # Zeile wird für den neuen, frischen Claim
                # WIEDERVERWENDET statt eine zweite Zeile mit
                # identischem, unique-constraint-geschütztem Schlüssel zu
                # versuchen.
                # Auftrag HV-20261020-VIER-MAHNSTUFEN: `mahnlauf_id`
                # (lose, FK-freie Referenz, siehe `MahnFallTable`-
                # Docstring) wird hier gleich mitgesetzt - `existing.id`
                # ist bereits bekannt (wiederverwendete Zeile).
                ergebnis = session.execute(
                    update(MahnFallTable)
                    .where(MahnFallTable.id.in_(mahnfall_ids))
                    .where(MahnFallTable.status == MahnStatus.GEPLANT.value)
                    .values(status=MahnStatus.GEBUENDELT.value, mahnlauf_id=existing.id)
                )
                if ergebnis.rowcount != len(mahnfall_ids):
                    session.rollback()
                    return None
                existing.status = "GEPLANT"
                existing.fehlergrund = None
                existing.mitglieder_mahnfall_ids = json.dumps(sorted(mahnfall_ids))
                session.commit()
                session.refresh(existing)
                return existing
            result = session.execute(
                update(MahnFallTable)
                .where(MahnFallTable.id.in_(mahnfall_ids))
                .where(MahnFallTable.status == MahnStatus.GEPLANT.value)
                .values(status=MahnStatus.GEBUENDELT.value)
            )
            if result.rowcount != len(mahnfall_ids):
                session.rollback()
                return None
            row = MahnLaufTable(
                outbox_key=outbox_key, vertrag_id=vertrag_id, gesellschaft_id=gesellschaft_id,
                stufe=stufe, kanal=kanal, mitglieder_mahnfall_ids=json.dumps(sorted(mahnfall_ids)),
            )
            session.add(row)
            # Auftrag HV-20261020-VIER-MAHNSTUFEN: `flush()` statt
            # `commit()` hier - weist `row.id` bereits zu, OHNE die
            # Transaktion abzuschließen, damit die nachfolgende
            # `mahnlauf_id`-Rückverknüpfung auf den Mitgliedern UND die
            # Gruppenzeile selbst atomar GEMEINSAM committet (oder bei
            # einem gleichzeitigen outbox_key-Konflikt GEMEINSAM
            # zurückgerollt) werden - dieselbe "alles oder nichts"-
            # Transaktionsgrenze wie beim ursprünglichen Mitglieder-Claim
            # oben (Rückprüfung Codex 14.09.2026).
            session.flush()
            session.execute(
                update(MahnFallTable).where(MahnFallTable.id.in_(mahnfall_ids)).values(mahnlauf_id=row.id)
            )
            try:
                session.commit()
            except IntegrityError:
                # Ein gleichzeitiger anderer Versuch hat exakt diesen
                # outbox_key zwischenzeitlich bereits angelegt - die
                # MITGLIEDER-Claim-Änderung dieser Transaktion wird
                # dabei ATOMAR mit zurückgerollt (keine verwaisten
                # GEBUENDELT-Mitglieder ohne Gruppe).
                session.rollback()
                return session.execute(
                    select(MahnLaufTable).where(MahnLaufTable.outbox_key == outbox_key)
                ).scalar_one()
            session.refresh(row)
            return row

    def get(self, mahnlauf_id: int) -> MahnLaufTable | None:
        with self._session_factory() as session:
            return session.get(MahnLaufTable, mahnlauf_id)

    def list_fuer_vertrag(self, vertrag_id: str) -> list[MahnLaufTable]:
        with self._session_factory() as session:
            statement = (
                select(MahnLaufTable)
                .where(MahnLaufTable.vertrag_id == vertrag_id)
                .order_by(MahnLaufTable.id.desc())
            )
            return list(session.execute(statement).scalars().all())

    def claim_fuer_versand(self, mahnlauf_id: int, *, jetzt: datetime | None = None, zusatz: dict | None = None) -> bool:
        """Atomarer Compare-and-Swap GEPLANT -> IN_VERSAND auf der
        GRUPPENZEILE selbst - das ist der eigentliche Unterschied zur
        alten "kleinste-Id"-Heuristik: die Exklusivität hängt an keinem
        einzelnen Mitglied mehr, sondern an dieser einen, für die exakte
        Mitgliedermenge eindeutigen Zeile.

        `zusatz`: optionale zusätzliche Spaltenwerte, ATOMAR in DERSELBEN
        UPDATE-Anweisung wie der Claim selbst geschrieben (Auftrag Markus
        14.09.2026, unabhängige Rückprüfung: der eingefrorene Kosten-/
        Inhaltssnapshot MUSS bereits VOR jedem tatsächlichen
        Providerkontakt persistiert sein - sonst geht er verloren, falls
        der Provider das Schreiben annimmt, der lokale Prozess aber VOR
        der Bestätigung des Ergebnisses abstürzt; eine spätere Status-
        Abfrage für eine dann UNSICHERE Gruppe hätte sonst keine
        verlässliche Grundlage mehr für Mitgliedsnachweis/Kostenbuchung,
        außer einer potenziell abweichenden Neuberechnung)."""

        with self._session_factory() as session:
            values = {"status": "IN_VERSAND", "versand_beansprucht_am": jetzt or datetime.now(timezone.utc)}
            if zusatz:
                values.update(zusatz)
            result = session.execute(
                update(MahnLaufTable)
                .where(MahnLaufTable.id == mahnlauf_id)
                .where(MahnLaufTable.status == "GEPLANT")
                .values(**values)
            )
            session.commit()
            return result.rowcount > 0

    def verwaiste_in_versand(self, *, aelter_als: datetime) -> list[MahnLaufTable]:
        with self._session_factory() as session:
            statement = (
                select(MahnLaufTable)
                .where(MahnLaufTable.status == "IN_VERSAND")
                .where(MahnLaufTable.versand_beansprucht_am < aelter_als)
            )
            return list(session.execute(statement).scalars().all())

    def gesendet_ohne_kostenabschluss(self) -> list[MahnLaufTable]:
        """Genau die Recovery-Lücke (Auftrag Markus 14.09.2026): der
        Versand ist bestätigt (`GESENDET`) und ein eingefrorener Kosten-
        /Inhaltssnapshot wurde atomar damit persistiert
        (`mahnkosten_snapshot_json IS NOT NULL`), aber die eigentliche
        Kostenbuchung wurde noch nicht als abgeschlossen markiert - ein
        Absturz zwischen bestätigtem Versand und Buchung, oder zwischen
        Buchung und dem Markieren als abgeschlossen. Siehe
        `MahnwesenService.vervollstaendige_gesendete_mahnlaeufe`."""

        with self._session_factory() as session:
            statement = (
                select(MahnLaufTable)
                .where(MahnLaufTable.status == "GESENDET")
                .where(MahnLaufTable.mahnkosten_snapshot_json.is_not(None))
                .where(MahnLaufTable.mahnkosten_verarbeitet_am.is_(None))
            )
            return list(session.execute(statement).scalars().all())

    def list_fuer_status(self, status: str) -> list[MahnLaufTable]:
        with self._session_factory() as session:
            return list(session.execute(select(MahnLaufTable).where(MahnLaufTable.status == status)).scalars().all())

    def list_fuer_vertrag(self, vertrag_id: str) -> list[MahnLaufTable]:
        """Reiner Read, jüngste Zeile zuerst - für die Mieterakte
        (HV-20260914-AUFGABEN-MIETERAKTE, Codex-Rückprüfung): tatsächlich
        geplante/gesendete Mahnläufe als Schriftverkehrsnachweis, getrennt
        nach `versand_beansprucht_am` (Transport angenommen) und
        `gesendet_am` (bestätigter Abschluss) - keine neue Berechnung."""

        with self._session_factory() as session:
            statement = (
                select(MahnLaufTable)
                .where(MahnLaufTable.vertrag_id == vertrag_id)
                .order_by(MahnLaufTable.geplant_am.desc())
            )
            return list(session.execute(statement).scalars().all())

    def list_gesendet_mit_offenen_mitgliedern(self, *, mitglieder_repo) -> list[MahnLaufTable]:
        """GESENDETE Mahnläufe, bei denen mindestens ein eingefrorenes
        Mitglied NOCH NICHT auf GESENDET nachgezogen wurde - unabhängige
        Rückprüfung Codex 14.09.2026, echter Bug: ein Absturz MITTEN in
        der Mitgliederschleife (nach dem bestätigten Gruppenübergang,
        vor/während einzelner Mitgliedsnachweise) durfte die Gruppe
        NICHT mit einem für immer GEBUENDELT bleibenden Mitglied
        zurücklassen (das blockiert insbesondere Stufe 2/den
        Zugangsbeleg für dieses Mitglied). Siehe
        `MahnwesenService.vervollstaendige_gesendete_mahnlaeufe`."""

        offene: list[MahnLaufTable] = []
        for lauf in self.list_fuer_status("GESENDET"):
            mitglieder_ids = self.mitglieder_ids(lauf)
            if any(
                (mitglied := mitglieder_repo.get(mitglied_id)) is None or mitglied.status != MahnStatus.GESENDET.value
                for mitglied_id in mitglieder_ids
            ):
                offene.append(lauf)
        return offene

    def markiere_mahnkosten_verarbeitet(self, mahnlauf_id: int, *, zeitpunkt: datetime | None = None) -> None:
        with self._session_factory() as session:
            session.execute(
                update(MahnLaufTable).where(MahnLaufTable.id == mahnlauf_id)
                .values(mahnkosten_verarbeitet_am=zeitpunkt or datetime.now(timezone.utc))
            )
            session.commit()

    def aktualisiere_kosten_snapshot(self, mahnlauf_id: int, kosten_snapshot_json: str) -> None:
        """Schreibt einen bereits VOR dem Providerkontakt eingefrorenen
        Snapshot NACHTRÄGLICH um (z. B. weil `MahnkostenService.
        reserviere_und_kuerze_vorschau` ein Gebührensegment entfernen
        musste, das eine andere, schnellere Gruppe soeben reserviert
        hat) - IMMER NOCH VOR dem eigentlichen Providerkontakt
        aufgerufen, siehe `MahnwesenService.versende_mahnlauf`."""

        with self._session_factory() as session:
            session.execute(
                update(MahnLaufTable).where(MahnLaufTable.id == mahnlauf_id)
                .values(mahnkosten_snapshot_json=kosten_snapshot_json)
            )
            session.commit()

    def set_status(self, mahnlauf_id: int, status: str, **zusatz) -> MahnLaufTable:
        with self._session_factory() as session:
            row = session.get(MahnLaufTable, mahnlauf_id)
            if row is None:
                raise ValueError(f"Unbekannter MahnLauf {mahnlauf_id}")
            row.status = status
            for key, value in zusatz.items():
                setattr(row, key, value)
            session.commit()
            session.refresh(row)
            return row

    @staticmethod
    def mitglieder_ids(mahnlauf: MahnLaufTable) -> list[int]:
        return list(json.loads(mahnlauf.mitglieder_mahnfall_ids))

    def setze_zugang(
        self, mahnlauf_id: int, *, zugangsform: str, zugang_bestaetigt_am, zugang_beleg: str, zugang_bestaetigt_von: str,
    ) -> MahnLaufTable:
        """Auftrag HV-20261020-VIER-MAHNSTUFEN: schreibt den Zugangsnachweis
        DIESES gebündelten Schreibens - reiner Persistenzzugriff, ALLE
        fachlichen Prüfungen (Status GESENDET, Reihenfolge, Zugangsform
        ausreichend, Plausibilität der Daten) laufen VORHER in
        `MahnwesenService.zugang_bestaetigen` (siehe dort, mirrors
        `indexautomatik/outbox_service.py::zugang_bestaetigen`)."""

        with self._session_factory() as session:
            row = session.get(MahnLaufTable, mahnlauf_id)
            if row is None:
                raise ValueError(f"Unbekannter MahnLauf {mahnlauf_id}")
            row.zugangsform = zugangsform
            row.zugang_bestaetigt_am = zugang_bestaetigt_am
            row.zugang_beleg = zugang_beleg
            row.zugang_bestaetigt_von = zugang_bestaetigt_von
            session.commit()
            session.refresh(row)
            return row


class VertragMitmieterRepository:
    """Auftrag HV-20261020-VIER-MAHNSTUFEN: rein additive Verwaltung der
    NICHT-finanziellen Zusatz-Empfänger eines Vertrags (siehe
    `VertragMitmieterTable`-Docstring) - bewusst NICHT Teil von
    `stammdaten/repository.py::StammdatenRepository` (das bleibt die
    finanzielle Stammdatenwahrheit: Debitor/Konto/OP bleiben davon
    unberührt), sondern hier beim Mahnwesen, das diese Daten AUSSCHLIESSLICH
    für Briefadressierung/Zugangsnachweis verwendet."""

    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def anlegen(self, *, vertrag_id: str, name: str, email: str | None, adresse: str | None, erstellt_von: str) -> VertragMitmieterTable:
        with self._session_factory() as session:
            row = VertragMitmieterTable(vertrag_id=vertrag_id, name=name, email=email, adresse=adresse, erstellt_von=erstellt_von)
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def get(self, mitmieter_id: int) -> VertragMitmieterTable | None:
        with self._session_factory() as session:
            return session.get(VertragMitmieterTable, mitmieter_id)

    def list_aktiv(self, vertrag_id: str) -> list[VertragMitmieterTable]:
        with self._session_factory() as session:
            statement = (
                select(VertragMitmieterTable)
                .where(VertragMitmieterTable.vertrag_id == vertrag_id, VertragMitmieterTable.aktiv.is_(True))
                .order_by(VertragMitmieterTable.id)
            )
            return list(session.execute(statement).scalars().all())

    def list_alle(self, vertrag_id: str) -> list[VertragMitmieterTable]:
        with self._session_factory() as session:
            statement = select(VertragMitmieterTable).where(VertragMitmieterTable.vertrag_id == vertrag_id).order_by(VertragMitmieterTable.id)
            return list(session.execute(statement).scalars().all())

    def setze_postadresse_geprueft(self, mitmieter_id: int, *, geprueft: bool) -> VertragMitmieterTable:
        with self._session_factory() as session:
            row = session.get(VertragMitmieterTable, mitmieter_id)
            if row is None:
                raise ValueError(f"Unbekannter Mitmieter {mitmieter_id}")
            row.postadresse_geprueft = geprueft
            session.commit()
            session.refresh(row)
            return row

    def deaktivieren(self, mitmieter_id: int) -> VertragMitmieterTable:
        with self._session_factory() as session:
            row = session.get(VertragMitmieterTable, mitmieter_id)
            if row is None:
                raise ValueError(f"Unbekannter Mitmieter {mitmieter_id}")
            row.aktiv = False
            session.commit()
            session.refresh(row)
            return row


class MahnEmpfaengerZugangRepository:
    """Auftrag HV-20261020-VIER-MAHNSTUFEN: EIN Zugangsnachweis-Eintrag je
    tatsächlich erwartetem Empfänger EINES `MahnLaufTable`-Schreibens -
    siehe `MahnEmpfaengerZugangTable`-Docstring. `erstelle_erwartete_
    eintraege` legt beim BILDEN eines Mahnlaufs für jeden zu diesem
    Zeitpunkt aktiven erwarteten Empfänger (Hauptdebitor + aktive
    Mitmieter) einen NOCH UNBESTÄTIGTEN Platzhalter an (`zugangsform IS
    NULL`), damit das Backoffice von Anfang an zeigt, WER noch fehlt -
    idempotent über die Unique-Constraint `(mahnlauf_id, mitmieter_id)`."""

    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def erstelle_erwartete_eintraege(
        self, *, mahnlauf_id: int, empfaenger: list[tuple[int | None, str]], erstellt_von: str,
    ) -> list[MahnEmpfaengerZugangTable]:
        angelegt: list[MahnEmpfaengerZugangTable] = []
        with self._session_factory() as session:
            vorhandene = {
                (e.mitmieter_id)
                for e in session.execute(
                    select(MahnEmpfaengerZugangTable).where(MahnEmpfaengerZugangTable.mahnlauf_id == mahnlauf_id)
                ).scalars()
            }
            for mitmieter_id, name in empfaenger:
                if mitmieter_id in vorhandene:
                    continue
                row = MahnEmpfaengerZugangTable(
                    mahnlauf_id=mahnlauf_id, mitmieter_id=mitmieter_id, empfaenger_name_snapshot=name, erstellt_von=erstellt_von,
                )
                session.add(row)
                angelegt.append(row)
            if angelegt:
                session.commit()
                for row in angelegt:
                    session.refresh(row)
        return angelegt

    def fuer_mahnlauf(self, mahnlauf_id: int) -> list[MahnEmpfaengerZugangTable]:
        with self._session_factory() as session:
            statement = select(MahnEmpfaengerZugangTable).where(MahnEmpfaengerZugangTable.mahnlauf_id == mahnlauf_id)
            return list(session.execute(statement).scalars().all())

    def bestaetige_zugang(
        self, eintrag_id: int, *, zugangsform: str, zugang_bestaetigt_am, zugang_beleg: str,
    ) -> MahnEmpfaengerZugangTable:
        """Reiner Persistenzzugriff - die fachlichen Prüfungen (Status,
        Zugangsform ausreichend, Plausibilität) laufen in
        `MahnwesenService.zugang_bestaetigen`."""

        with self._session_factory() as session:
            row = session.get(MahnEmpfaengerZugangTable, eintrag_id)
            if row is None:
                raise ValueError(f"Unbekannter Empfänger-Zugangsnachweis {eintrag_id}")
            row.zugangsform = zugangsform
            row.zugang_bestaetigt_am = zugang_bestaetigt_am
            row.zugang_beleg = zugang_beleg
            session.commit()
            session.refresh(row)
            return row

    @staticmethod
    def alle_bestaetigt(eintraege: list[MahnEmpfaengerZugangTable], *, ausreichende_formen: frozenset[str]) -> bool:
        """`True` nur, wenn JEDER erwartete Empfänger einen BESTÄTIGTEN,
        FORMAL AUSREICHENDEN Zugang hat (siehe `domain/enums.py::
        ZUGANGSFORMEN_AUSREICHEND`) - ein leerer Erwartungssatz gilt NICHT
        automatisch als "alle bestätigt" (der Aufrufer muss mindestens
        den Hauptdebitor-Platzhalter angelegt haben)."""

        if not eintraege:
            return False
        return all(
            e.zugang_bestaetigt_am is not None and e.zugangsform in ausreichende_formen
            for e in eintraege
        )

    @staticmethod
    def spaetester_zugang(eintraege: list[MahnEmpfaengerZugangTable]) -> date | None:
        """Der ZULETZT bestätigte (späteste) Zugang über alle Einträge -
        konservative Grundlage für den Fristenlauf bei mehreren
        Empfängern (siehe `MahnLaufTable.zugang_bestaetigt_am`-
        Docstring). `None`, solange nicht JEDER Eintrag einen bestätigten
        Zugang hat."""

        daten = [e.zugang_bestaetigt_am for e in eintraege]
        if not daten or any(d is None for d in daten):
            return None
        return max(daten)
