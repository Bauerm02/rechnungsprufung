"""Repository für Zinsprofile, OeNB-Basiszinssätze und die
Mahnkosten-Buchungsledger (Auftrag Markus 13.09.2026, siehe
`mahnwesen/kosten.py` für die fachliche Begründung)."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.infrastructure.db.tables import MahnkostenBuchungTable, OenbBasiszinssatzTable, ZinsprofilTable


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

    def zinsprofil_anlegen(
        self, *, vertrag_id: str, ist_b2b: bool, vertragsdatum: date | None,
        vereinbarter_zinssatz_prozent=None, vereinbarung_geprueft: bool = False, vereinbarung_beleg: str | None = None,
        mahngebuehr_kostenbasis_cent: int | None = None, mahngebuehr_kostenbasis_beleg: str | None = None,
        erstellt_von: str,
    ) -> ZinsprofilTable:
        with self._session_factory() as session:
            bisherige_version = session.execute(
                select(ZinsprofilTable.version).where(ZinsprofilTable.vertrag_id == vertrag_id)
                .order_by(ZinsprofilTable.version.desc()).limit(1)
            ).scalar_one_or_none()
            row = ZinsprofilTable(
                vertrag_id=vertrag_id, version=(bisherige_version or 0) + 1, status="ENTWURF",
                ist_b2b=ist_b2b, vertragsdatum=vertragsdatum,
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
        with self._session_factory() as session:
            return session.execute(
                select(OenbBasiszinssatzTable)
                .where(OenbBasiszinssatzTable.gueltig_von <= datum)
                .where(OenbBasiszinssatzTable.gueltig_bis >= datum)
            ).scalar_one_or_none()

    def basiszinssatz_erfassen(
        self, *, id: str, gueltig_von: date, gueltig_bis: date, basiszinssatz_prozent, erfasst_von: str, quelle_referenz: str,
    ) -> OenbBasiszinssatzTable:
        with self._session_factory() as session:
            bestehend = session.get(OenbBasiszinssatzTable, id)
            if bestehend is not None:
                raise ValueError(f"Basiszinssatz '{id}' bereits erfasst - Halbjahreswerte sind unveränderlich, ggf. neuen Zeitraum verwenden.")
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

    def gebuehr_bereits_gebucht(self, *, vertrag_id: str) -> bool:
        """§458 UGB: einmal je Forderung/Mahnlauf - hier vertragsweit
        geprüft (nicht nur je Stufe), damit Stufe 2 keine ZWEITE Gebühr
        für dieselbe zugrunde liegende Entgeltforderung ansetzt."""

        with self._session_factory() as session:
            treffer = session.execute(
                select(MahnkostenBuchungTable.id)
                .where(MahnkostenBuchungTable.vertrag_id == vertrag_id)
                .where(MahnkostenBuchungTable.gebuehr_cent.is_not(None))
                .limit(1)
            ).first()
            return treffer is not None

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
        erstellt_von: str, session: Session | None = None,
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
