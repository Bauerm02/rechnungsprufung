"""Repository für Zinsprofile, OeNB-Basiszinssätze und die
Mahnkosten-Buchungsledger (Auftrag Markus 13.09.2026, siehe
`mahnwesen/kosten.py` für die fachliche Begründung)."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.infrastructure.db.tables import (
    MahnkostenBuchungTable,
    MahnkostenGebuehrTable,
    OenbBasiszinssatzTable,
    ZinsprofilTable,
)


class ZinsledgerInkonsistentError(Exception):
    """Eine `MahnkostenBuchungTable`-Zeile hat tatsächlich gebuchte
    Zinsen (`zinsen_cent > 0`), aber ihr `zinsen_delta_je_op_json`
    (das JE `op_position_id` tatsächlich neu gebuchte Delta) fehlt oder
    summiert sich nicht auf denselben Betrag - z. B. eine Buchung aus
    der Zeit vor Einführung dieser Spalte, oder eine manuell/fehlerhaft
    veränderte Zeile. Unabhängige Rückprüfung Codex 14.09.2026: eine
    solche Zeile NIE stillschweigend als "0 bereits gebucht" werten
    (das würde bei einer künftigen Stufe zu doppelt gebuchten Zinsen
    führen) - stattdessen wird die weitere automatische Berechnung für
    die betroffenen Forderungen explizit blockiert, bis die Historie
    nachvollziehbar migriert/belegt ist."""

    def __init__(self, *, vertrag_id: str, betroffene_op_ids: list[int], zinsen_cent: int, summe_delta_cent: int):
        self.vertrag_id = vertrag_id
        self.betroffene_op_ids = betroffene_op_ids
        super().__init__(
            f"Vertrag {vertrag_id}: Buchung mit zinsen_cent={zinsen_cent} hat ein fehlendes/inkonsistentes "
            f"zinsen_delta_je_op_json (Summe={summe_delta_cent}) - betroffene Forderungen: {betroffene_op_ids}."
        )


class MahnkostenRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    # -- Zinsprofil (versioniert, wie RechtsprofilTable) ---------------------
    def neuestes_zinsprofil(self, vertrag_id: str) -> ZinsprofilTable | None:
        with self._session_factory() as session:
            return session.execute(
                select(ZinsprofilTable)
                .where(ZinsprofilTable.vertrag_id == vertrag_id)
                .order_by(ZinsprofilTable.version.desc())
                .limit(1)
            ).scalar_one_or_none()

    def geprueftes_zinsprofil(self, vertrag_id: str) -> ZinsprofilTable | None:
        """Das AKTUELL wirksame Profil - nur eines im Status GEPRUEFT
        wird von `kosten.py::bestimme_zinssatz` als Grundlage akzeptiert;
        ein neuerer ENTWURF entwertet die alte Freigabe NICHT (analog
        RechtsprofilTable - eine neue Version braucht eine NEUE, explizite
        Freigabe)."""

        with self._session_factory() as session:
            return session.execute(
                select(ZinsprofilTable)
                .where(ZinsprofilTable.vertrag_id == vertrag_id)
                .where(ZinsprofilTable.status == "GEPRUEFT")
                .order_by(ZinsprofilTable.version.desc())
                .limit(1)
            ).scalar_one_or_none()

    def liste_fuer_vertrag(self, vertrag_id: str) -> list[ZinsprofilTable]:
        with self._session_factory() as session:
            return list(session.execute(
                select(ZinsprofilTable).where(ZinsprofilTable.vertrag_id == vertrag_id)
                .order_by(ZinsprofilTable.version.desc())
            ).scalars().all())

    def historie_geprueft(self, vertrag_id: str) -> list[ZinsprofilTable]:
        """ALLE jemals GEPRUEFTEN Versionen (aufsteigend nach Version) -
        Grundlage für die periodengerechte Auflösung bei einem
        Vertragszinswechsel (siehe `kosten.py::_zinsprofil_segmente`).
        Ein zwischenzeitlicher ENTWURF, der nie freigegeben wurde, taucht
        hier NICHT auf (er entfaltet ohnehin keine Wirkung)."""

        with self._session_factory() as session:
            return list(session.execute(
                select(ZinsprofilTable).where(ZinsprofilTable.vertrag_id == vertrag_id)
                .where(ZinsprofilTable.status == "GEPRUEFT")
                .order_by(ZinsprofilTable.version.asc())
            ).scalars().all())

    def zinsprofil_anlegen(
        self, *, vertrag_id: str, ist_b2b: bool, vertragsdatum: date | None,
        vereinbarter_zinssatz_prozent=None, vereinbarung_geprueft: bool = False, vereinbarung_beleg: str | None = None,
        mahngebuehr_kostenbasis_cent: int | None = None, mahngebuehr_kostenbasis_beleg: str | None = None,
        gueltig_ab: date | None = None, verzugsverantwortung_geprueft: bool = False, erstellt_von: str,
    ) -> ZinsprofilTable:
        with self._session_factory() as session:
            bisherige_version = session.execute(
                select(ZinsprofilTable.version).where(ZinsprofilTable.vertrag_id == vertrag_id)
                .order_by(ZinsprofilTable.version.desc()).limit(1)
            ).scalar_one_or_none()
            row = ZinsprofilTable(
                vertrag_id=vertrag_id, version=(bisherige_version or 0) + 1, status="ENTWURF",
                ist_b2b=ist_b2b, vertragsdatum=vertragsdatum, gueltig_ab=gueltig_ab,
                verzugsverantwortung_geprueft=verzugsverantwortung_geprueft,
                vereinbarter_zinssatz_prozent=vereinbarter_zinssatz_prozent,
                vereinbarung_geprueft=vereinbarung_geprueft, vereinbarung_beleg=vereinbarung_beleg,
                mahngebuehr_kostenbasis_cent=mahngebuehr_kostenbasis_cent,
                mahngebuehr_kostenbasis_beleg=mahngebuehr_kostenbasis_beleg, erstellt_von=erstellt_von,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def zinsprofil_freigeben(self, zinsprofil_id: int, *, freigegeben_von: str) -> ZinsprofilTable:
        with self._session_factory() as session:
            row = session.get(ZinsprofilTable, zinsprofil_id)
            if row is None:
                raise ValueError(f"Unbekanntes Zinsprofil {zinsprofil_id}")
            if row.status != "ENTWURF":
                raise ValueError(f"Zinsprofil {zinsprofil_id} ist bereits {row.status}, keine erneute Freigabe.")
            row.status = "GEPRUEFT"
            row.geprueft_von = freigegeben_von
            row.geprueft_am = datetime.now(timezone.utc)
            session.commit()
            session.refresh(row)
            return row

    # -- OeNB-Basiszinssatz ---------------------------------------------------
    def basiszinssatz_fuer_datum(self, datum: date) -> OenbBasiszinssatzTable | None:
        """`.limit(1)` ist bewusst gesetzt: `basiszinssatz_erfassen` lehnt
        neue, sich überlappende Zeiträume bereits beim Erfassen ab, aber
        ein `.scalar_one_or_none()` ohne Limit würde bei doch vorhandenen
        Altdaten mit überlappenden Zeiträumen mit `MultipleResultsFound`
        abstürzen statt die Zinsberechnung mit einem klaren Hinweis
        weiterlaufen zu lassen - siehe `basiszinssatz_erfassen`."""

        with self._session_factory() as session:
            return session.execute(
                select(OenbBasiszinssatzTable)
                .where(OenbBasiszinssatzTable.gueltig_von <= datum)
                .where(OenbBasiszinssatzTable.gueltig_bis >= datum)
                .order_by(OenbBasiszinssatzTable.gueltig_von)
                .limit(1)
            ).scalar_one_or_none()

    def basiszinssatz_erfassen(
        self, *, id: str, gueltig_von: date, gueltig_bis: date, basiszinssatz_prozent, erfasst_von: str, quelle_referenz: str,
    ) -> OenbBasiszinssatzTable:
        if gueltig_bis < gueltig_von:
            raise ValueError(f"Basiszinssatz '{id}': gueltig_bis ({gueltig_bis}) liegt vor gueltig_von ({gueltig_von}).")
        with self._session_factory() as session:
            bestehend = session.get(OenbBasiszinssatzTable, id)
            if bestehend is not None:
                raise ValueError(f"Basiszinssatz '{id}' bereits erfasst - Halbjahreswerte sind unveränderlich, ggf. neuen Zeitraum verwenden.")
            # Zwei verschiedene IDs mit überlappenden Gültigkeitszeiträumen
            # würden `basiszinssatz_fuer_datum` für Tage im Überlappungs-
            # bereich mehrdeutig machen - das wird HIER beim Erfassen
            # abgelehnt, nicht erst bei der Zinsberechnung entdeckt.
            ueberlappung = session.execute(
                select(OenbBasiszinssatzTable)
                .where(OenbBasiszinssatzTable.gueltig_von <= gueltig_bis)
                .where(OenbBasiszinssatzTable.gueltig_bis >= gueltig_von)
                .limit(1)
            ).scalar_one_or_none()
            if ueberlappung is not None:
                raise ValueError(
                    f"Basiszinssatz '{id}' ({gueltig_von}–{gueltig_bis}) überschneidet sich mit bereits "
                    f"erfasstem Zeitraum '{ueberlappung.id}' ({ueberlappung.gueltig_von}–{ueberlappung.gueltig_bis})."
                )
            row = OenbBasiszinssatzTable(
                id=id, gueltig_von=gueltig_von, gueltig_bis=gueltig_bis,
                basiszinssatz_prozent=basiszinssatz_prozent, erfasst_von=erfasst_von, quelle_referenz=quelle_referenz,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def liste_basiszinssaetze(self) -> list[OenbBasiszinssatzTable]:
        with self._session_factory() as session:
            return list(session.execute(select(OenbBasiszinssatzTable).order_by(OenbBasiszinssatzTable.gueltig_von)).scalars().all())

    def naechster_basiszinssatz_ab(self, datum: date) -> OenbBasiszinssatzTable | None:
        """Der FRÜHESTE erfasste Basiszinssatz, dessen Gültigkeit erst
        NACH `datum` beginnt - für eine präzise Lückenabgrenzung in
        `kosten.py::_segmentiere_periode_ugb`, falls ein Zwischenhalbjahr
        fehlt, ein späteres aber schon erfasst ist (statt die Lücke bis
        zum Periodenende pauschal als ungeklärt zu markieren)."""

        with self._session_factory() as session:
            return session.execute(
                select(OenbBasiszinssatzTable)
                .where(OenbBasiszinssatzTable.gueltig_von > datum)
                .order_by(OenbBasiszinssatzTable.gueltig_von)
                .limit(1)
            ).scalar_one_or_none()

    # -- Mahnkosten-Buchungsledger --------------------------------------------
    def bereits_gebuchte_zinsen_cent(self, *, vertrag_id: str) -> int:
        """Summe der für DIESEN Vertrag bereits über frühere Mahnläufe
        gebuchten Zinsen, ÜBER ALLE MAHNSTUFEN HINWEG (bewusst NICHT je
        Stufe gefiltert) - Grundlage dafür, dass Stufe 2 dieselben, bei
        Stufe 1 bereits fakturierten Zinstage NIE erneut ansetzt (siehe
        `kosten.py::MahnkostenVorschau.zusaetzlicher_betrag_cent`)."""

        with self._session_factory() as session:
            zeilen = session.execute(
                select(MahnkostenBuchungTable.zinsen_cent).where(MahnkostenBuchungTable.vertrag_id == vertrag_id)
            ).all()
            return sum(z for (z,) in zeilen)

    def bereits_gebuchte_zinsen_je_op_position(self, *, vertrag_id: str) -> dict[int, int]:
        """Wie `bereits_gebuchte_zinsen_cent`, aber JE zugrunde liegender
        `op_position_id` aufgeschlüsselt - unabhängige Rückprüfung Codex
        14.09.2026: eine vertragsweite BLANKO-Summe würde die Verzinsung
        einer genuin NEUEN, seither entstandenen Forderung fälschlich
        reduzieren, sobald eine ANDERE, mittlerweile abgelöste/
        geschlossene Forderung früher bereits verzinst wurde. Der Abzug
        in `kosten.py::berechne_mahnkosten_vorschau` erfolgt deshalb JE
        `op_position_id`, nicht vertragsweit gesamt.

        Liest AUSSCHLIESSLICH `zinsen_delta_je_op_json` (das an JEDEM
        einzelnen Buchungstag tatsächlich NEU gebuchte Delta) - NIEMALS
        `zins_segmente_json` (zweiter, unabhängig gemeldeter Bug der
        Rückprüfung Codex 14.09.2026: `zins_segmente_json` bildet die
        VOLLE, ab der Fälligkeit neu berechnete Periode ab, nicht nur das
        an diesem Tag zusätzlich gebuchte Delta - eine Summe über mehrere
        Buchungen hinweg würde denselben Zeitraum mehrfach zählen, siehe
        `MahnkostenBuchungTable.zinsen_delta_je_op_json`-Docstring).

        Hat eine Buchung tatsächlich gebuchte Zinsen (`zinsen_cent > 0`),
        aber ihr `zinsen_delta_je_op_json` fehlt oder summiert sich NICHT
        auf denselben Betrag (z. B. eine Alt-Buchung von vor Einführung
        dieser Spalte), wird das NIEMALS still als "0 bereits gebucht"
        gewertet (dritter unabhängig gemeldeter Bug derselben Rückprüfung
        - das würde bei einer künftigen Stufe zu doppelt gebuchten Zinsen
        führen) - stattdessen wird `ZinsledgerInkonsistentError`
        ausgelöst, die `kosten_service.py::vorschau()` abfängt und in
        eine explizit "unberechenbar"e Vorschau für den betroffenen
        Vertrag umwandelt."""

        with self._session_factory() as session:
            zeilen = session.execute(
                select(
                    MahnkostenBuchungTable.zinsen_delta_je_op_json, MahnkostenBuchungTable.zinsen_cent,
                    MahnkostenBuchungTable.forderung_op_position_ids,
                ).where(MahnkostenBuchungTable.vertrag_id == vertrag_id)
            ).all()
        ergebnis: dict[int, int] = {}
        for roh, zinsen_cent, op_ids_json in zeilen:
            try:
                eintraege = json.loads(roh) if roh else {}
            except (TypeError, ValueError):
                eintraege = {}
            summe_delta = 0
            for op_id_str, delta in eintraege.items():
                try:
                    op_id = int(op_id_str)
                    delta_cent = int(delta)
                except (TypeError, ValueError):
                    continue
                ergebnis[op_id] = ergebnis.get(op_id, 0) + delta_cent
                summe_delta += delta_cent
            if zinsen_cent and zinsen_cent > 0 and summe_delta != zinsen_cent:
                try:
                    betroffene_ops = json.loads(op_ids_json) if op_ids_json else []
                except (TypeError, ValueError):
                    betroffene_ops = []
                raise ZinsledgerInkonsistentError(
                    vertrag_id=vertrag_id, betroffene_op_ids=betroffene_ops,
                    zinsen_cent=zinsen_cent, summe_delta_cent=summe_delta,
                )
        return ergebnis

    def bereits_erhobene_gebuehr_schluessel(self, *, vertrag_id: str) -> frozenset[str]:
        """Alle `entgeltforderung_schluessel`, für die für DIESEN Vertrag
        bereits PERMANENT eine §458-UGB-Pauschale erhoben wurde - über
        ALLE Mahnläufe/Stufen hinweg (siehe
        `kosten.py::MahnkostenGebuehrTable`-Moduldoc). Eine hier
        enthaltene Entgeltforderung löst NIE wieder eine zweite Pauschale
        aus; eine GENUIN andere (nicht enthaltene) Entgeltforderung kann
        weiterhin ihre eigene, separate Pauschale auslösen."""

        with self._session_factory() as session:
            zeilen = session.execute(
                select(MahnkostenGebuehrTable.entgeltforderung_schluessel)
                .where(MahnkostenGebuehrTable.vertrag_id == vertrag_id)
            ).all()
            return frozenset(s for (s,) in zeilen)

    def gebuehr_erheben(
        self, *, vertrag_id: str, entgeltforderung_schluessel: str, betrag_cent: int, rechtsgrundlage: str,
        mahnkosten_buchung_id: int | None, gebuehr_op_position_id: int | None, erstellt_von: str,
        session: Session | None = None,
    ) -> MahnkostenGebuehrTable:
        """Schreibt die PERMANENTE Erhebung fest - die Unique-Constraint
        `uq_mahnkosten_gebuehr_forderung` ist hier das FACHLICHE Gate
        selbst (siehe Tabellen-Docstring), kein bloßes Race-Netz: ein
        zweiter Versuch für dieselbe Entgeltforderung MUSS mit
        `IntegrityError` scheitern."""

        def _schreiben(active_session: Session) -> MahnkostenGebuehrTable:
            row = MahnkostenGebuehrTable(
                vertrag_id=vertrag_id, entgeltforderung_schluessel=entgeltforderung_schluessel,
                betrag_cent=betrag_cent, rechtsgrundlage=rechtsgrundlage,
                mahnkosten_buchung_id=mahnkosten_buchung_id, gebuehr_op_position_id=gebuehr_op_position_id,
                erstellt_von=erstellt_von,
            )
            active_session.add(row)
            return row

        if session is not None:
            row = _schreiben(session)
            session.flush()
            return row
        with self._session_factory() as owned_session:
            row = _schreiben(owned_session)
            owned_session.commit()
            owned_session.refresh(row)
            return row

    def buchung_fuer_stichtag(self, *, vertrag_id: str, stufe: int, zins_bis: date) -> MahnkostenBuchungTable | None:
        with self._session_factory() as session:
            return session.execute(
                select(MahnkostenBuchungTable)
                .where(MahnkostenBuchungTable.vertrag_id == vertrag_id)
                .where(MahnkostenBuchungTable.stufe == stufe)
                .where(MahnkostenBuchungTable.zins_bis == zins_bis)
            ).scalar_one_or_none()

    def buchung_anlegen(
        self, *, vertrag_id: str, stufe: int, mahnlauf_schluessel: str, forderung_op_position_ids: list[int],
        hauptforderung_cent: int, zinsbasis: str, zinssatz_prozent, zins_von: date, zins_bis: date,
        zinsen_cent: int, gebuehr_cent: int | None, rechtsgrundlage_gebuehr: str | None,
        versandnachweis_referenz: str, zinsen_op_position_id: int | None, gebuehr_op_position_id: int | None,
        erstellt_von: str, zins_segmente_json: str = "[]", zinsen_delta_je_op_json: str = "{}",
        session: Session | None = None,
    ) -> MahnkostenBuchungTable:
        """`session`: siehe `stammdaten/repository.py::upsert_gesellschaft` -
        übergeben, um diese Buchung Teil derselben atomaren Transaktion
        wie die tatsächlichen OP-Buchungen zu machen (Alles-oder-nichts,
        siehe `kosten_service.py::MahnkostenService.buche_bei_versand`).
        Die `uq_mahnkosten_lauf`-Unique-Constraint (`vertrag_id, stufe,
        zins_bis`) lässt einen gleichzeitigen zweiten Versuch für
        denselben Mahnlauf mit einem `IntegrityError` scheitern statt eine
        zweite Zeile anzulegen - reines Race-Condition-Sicherheitsnetz,
        die primäre Idempotenz ist das vertragsweite Delta in
        `kosten_service.py::MahnkostenService.buche_bei_versand`."""

        def _schreiben(active_session: Session) -> MahnkostenBuchungTable:
            row = MahnkostenBuchungTable(
                vertrag_id=vertrag_id, stufe=stufe, mahnlauf_schluessel=mahnlauf_schluessel,
                forderung_op_position_ids=json.dumps(sorted(forderung_op_position_ids)),
                hauptforderung_cent=hauptforderung_cent, zinsbasis=zinsbasis, zinssatz_prozent=zinssatz_prozent,
                zins_von=zins_von, zins_bis=zins_bis, zinsen_cent=zinsen_cent, gebuehr_cent=gebuehr_cent,
                rechtsgrundlage_gebuehr=rechtsgrundlage_gebuehr, versandnachweis_referenz=versandnachweis_referenz,
                zinsen_op_position_id=zinsen_op_position_id, gebuehr_op_position_id=gebuehr_op_position_id,
                zins_segmente_json=zins_segmente_json, zinsen_delta_je_op_json=zinsen_delta_je_op_json,
                erstellt_von=erstellt_von,
            )
            active_session.add(row)
            return row

        if session is not None:
            row = _schreiben(session)
            session.flush()
            return row
        with self._session_factory() as owned_session:
            row = _schreiben(owned_session)
            owned_session.commit()
            owned_session.refresh(row)
            return row
