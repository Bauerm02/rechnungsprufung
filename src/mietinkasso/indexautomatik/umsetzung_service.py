"""Atomare, versionierte Umsetzung eines zugegangenen, wirksamen
Erhöhungsschreibens in Vertragskomponenten und Rechtsprofil-Basis
(Auftrag HV-20260913-VERSAND-SOLL, Punkt 1) - der bisher fehlende
letzte Schritt der Indexautomatik-Pipeline: `outbox_service.
taegliche_pflege()` setzt einen zugegangenen, wirksam gewordenen Fall
auf `SOLL_UMSETZUNG_OFFEN` und endet dort bewusst (siehe
RAHMENPROGRAMM.md/OFFENE_PUNKTE.md - ein dokumentierter, kein
vergessener Haltepunkt). `umsetzen()` ist der fehlende nächste Schritt.

Anders als `outbox_service.versenden()`/`zugang_bestaetigen()` (die
jeweils NUR ihre eigene, separate Session/Transaktion öffnen) läuft
`umsetzen()` KOMPLETT in EINER einzigen DB-Transaktion: Claim (CAS
SOLL_UMSETZUNG_OFFEN/-BLOCKIERT -> SOLL_UMSETZUNG_IN_PRUEFUNG), erneute
Validierung gegen den AKTUELLEN Stand (unabhängig von der UI), die
Komponentenhistorisierung, die neue Rechtsprofil-Version UND der
Ausführungsnachweis (`IndexSollUmsetzungTable`) werden zusammen
committet oder gemeinsam zurückgerollt. Das ist hier SICHERER als das
zweiphasige Versand-Muster (Claim -> externer Aufruf -> Ergebnis
nachtragen, mit `verwaiste_in_versand`-Recovery für einen Absturz
dazwischen), weil diese Operation KEINEN externen, die Transaktion
überdauernden Seiteneffekt hat: ein Absturz vor dem COMMIT lässt die
Zeile unverändert bei SOLL_UMSETZUNG_OFFEN/-BLOCKIERT stehen, ein
erneuter Versuch (auch parallel gestartet) ist gefahrlos - GENAU EINE
Änderung, nie mehr."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.auth.service import AuthContext, require_gesellschaft_access, require_schreibrecht
from mietinkasso.domain.exceptions import ObjektAusgeschlossenError
from mietinkasso.indexautomatik.rechtsprofil import RechtsprofilService
from mietinkasso.indexautomatik.repository import ErhoehungsschreibenRepository
from mietinkasso.infrastructure.db.tables import (
    ErhoehungsschreibenTable,
    IndexSollUmsetzungTable,
    RechtsprofilTable,
    VertragsKomponenteTable,
    VertragTable,
    VorschreibungTable,
)
from mietinkasso.mieweg_vorschau.service import _NIE_INDEXIERBARE_ARTEN
from mietinkasso.op.service import compute_content_hash
from mietinkasso.stammdaten.repository import StammdatenRepository

#: Aus diesen beiden Status darf `umsetzen()` geclaimt werden - BLOCKIERT
#: ist wie in `outbox_service.py` retryable (z. B. nach einer
#: nachträglich belegten differenziellen Korrektur einer zuvor
#: blockierenden Monatsvorschreibung), jeder andere Status (insbesondere
#: das terminale SOLL_UMGESETZT) ist es nicht.
_CLAIMBARE_STATUS = ("SOLL_UMSETZUNG_OFFEN", "SOLL_UMSETZUNG_BLOCKIERT")


@dataclass
class UmsetzungsErgebnis:
    status: str  # "UMGESETZT" | "BLOCKIERT" | "BEREITS_VERARBEITET"
    gruende: list[str] = field(default_factory=list)
    neue_komponente_id: str | None = None
    neues_rechtsprofil_id: int | None = None


class IndexSollUmsetzungService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        stammdaten_repository: StammdatenRepository,
        rechtsprofil_service: RechtsprofilService,
        erhoehungsschreiben_repository: ErhoehungsschreibenRepository,
    ):
        self._session_factory = session_factory
        self._stammdaten_repository = stammdaten_repository
        self._rechtsprofil_service = rechtsprofil_service
        self._erhoehungsschreiben_repository = erhoehungsschreiben_repository

    def _schreiben_und_vertrag(self, erhoehungsschreiben_id: int) -> tuple[ErhoehungsschreibenTable, VertragTable]:
        schreiben = self._erhoehungsschreiben_repository.get(erhoehungsschreiben_id)
        if schreiben is None:
            raise ValueError(f"Unbekanntes Erhoehungsschreiben {erhoehungsschreiben_id}")
        vertrag = self._stammdaten_repository.get_vertrag(schreiben.vertrag_id)
        if vertrag is None:
            raise ValueError(f"Unbekannter Vertrag {schreiben.vertrag_id}")
        return schreiben, vertrag

    def _pruefen(
        self, session: Session, *, schreiben: ErhoehungsschreibenTable, vertrag: VertragTable, heute: date
    ) -> tuple[list[str], dict | None]:
        """Vollständige Re-Validierung GEGEN DEN AKTUELLEN STAND, unabhängig
        von der UI/einem alten Entwurf - jede der hier geprüften Quellen
        kann sich seit `SOLL_UMSETZUNG_OFFEN` geändert haben."""

        gruende: list[str] = []

        try:
            self._stammdaten_repository.pruefe_vertrag_nicht_ausgeschlossen(vertrag.id, session=session)
        except ObjektAusgeschlossenError as exc:
            gruende.append(str(exc))

        if schreiben.zugang_bestaetigt_am is None or schreiben.zahlungspflicht_ab is None:
            gruende.append(
                "Kein bestätigter Zugang bzw. keine daraus berechnete Zahlungspflicht vorhanden - "
                "Voraussetzung für eine Soll-Umsetzung fehlt."
            )
        elif heute < schreiben.zahlungspflicht_ab:
            gruende.append(
                f"Zahlungspflicht beginnt erst am {schreiben.zahlungspflicht_ab.isoformat()} - noch nicht "
                "wirksam, keine verfrühte Umsetzung vor Wirksamkeit."
            )
        if (
            vertrag.gueltig_bis is not None
            and schreiben.zahlungspflicht_ab is not None
            and vertrag.gueltig_bis < schreiben.zahlungspflicht_ab
        ):
            gruende.append(
                f"Vertrag ist bereits ab {vertrag.gueltig_bis.isoformat()} beendet - keine Soll-Umsetzung "
                "für einen Zeitraum nach Vertragsende."
            )

        verteilung = schreiben.komponenten_verteilung or {}
        alte_komponente: VertragsKomponenteTable | None = None
        if not verteilung.get("komponente_id"):
            gruende.append(
                "Kein eindeutig zugeordneter komponenten_verteilung-Eintrag (Mehrkomponenten-Fall oder "
                "älteres Schreiben ohne diese Zuordnung) - keine automatische Umsetzung ohne centgenaue "
                "Verteilung auf genau eine Komponente."
            )
        else:
            alte_komponente = session.get(VertragsKomponenteTable, verteilung["komponente_id"])
            if alte_komponente is None or alte_komponente.vertrag_id != vertrag.id:
                gruende.append(
                    f"Komponente {verteilung['komponente_id']} ist nicht mehr auffindbar oder gehört nicht "
                    "mehr zu diesem Vertrag - Stale-Snapshot, neue Prüfung erforderlich."
                )
                alte_komponente = None
            elif alte_komponente.betrag_cent != verteilung.get("alter_betrag_cent"):
                gruende.append(
                    f"Komponente {alte_komponente.id} wurde seit Schreibenserstellung anderweitig auf "
                    f"{alte_komponente.betrag_cent} Cent geändert (Schreiben ging von "
                    f"{verteilung.get('alter_betrag_cent')} Cent aus) - Stale-Snapshot."
                )
                alte_komponente = None
            elif alte_komponente.art in _NIE_INDEXIERBARE_ARTEN:
                gruende.append(
                    f"Komponente {alte_komponente.id} hat die Art '{alte_komponente.art}' - BK/HK/USt-"
                    "Vorauszahlungen und vergleichbare Aliasarten werden nie mitindexiert/umgesetzt."
                )
                alte_komponente = None
            elif (
                alte_komponente.gueltig_bis is not None
                and schreiben.zahlungspflicht_ab is not None
                and alte_komponente.gueltig_bis < schreiben.zahlungspflicht_ab
            ):
                gruende.append(
                    f"Komponente {alte_komponente.id} ist bereits ab {alte_komponente.gueltig_bis.isoformat()} "
                    "befristet ausgelaufen - vor dem Wirksamkeitsdatum, keine Umsetzung auf eine beendete "
                    "Position."
                )
                alte_komponente = None

        profil = session.get(RechtsprofilTable, schreiben.rechtsprofil_id)
        if profil is None or profil.version != schreiben.rechtsprofil_version:
            gruende.append("Zugrunde liegendes Rechtsprofil nicht mehr auffindbar oder Version weicht ab.")
            profil = None
        elif profil.status != "FREIGEGEBEN":
            gruende.append(f"Rechtsprofil hat Status '{profil.status}', ist nicht mehr FREIGEGEBEN.")
            profil = None
        else:
            aktueller_hash = compute_content_hash(self._rechtsprofil_service._quelle_snapshot(profil, vertrag))
            if aktueller_hash != profil.quelle_hash:
                gruende.append(
                    "Vertrag/Komponenten/Klausel haben sich seit der Rechtsprofil-Freigabe geändert "
                    "(Stale-Snapshot) - Umsetzung wird gesperrt, keine Umsetzung auf veraltetem Stand."
                )
                profil = None

        if schreiben.zahlungspflicht_ab is not None:
            wirksam_monat = f"{schreiben.zahlungspflicht_ab.year:04d}-{schreiben.zahlungspflicht_ab.month:02d}"
            bereits_gebuchte = list(
                session.execute(
                    select(VorschreibungTable)
                    .where(VorschreibungTable.vertrag_id == vertrag.id)
                    .where(VorschreibungTable.monat >= wirksam_monat)
                    .where(VorschreibungTable.status != "ENTWURF")
                ).scalars()
            )
            if bereits_gebuchte:
                betroffene = ", ".join(sorted(f"{v.monat} (#{v.id})" for v in bereits_gebuchte))
                gruende.append(
                    f"Bereits gebuchte Monatsvorschreibung(en) ab dem Wirksamkeitsmonat {wirksam_monat} "
                    f"vorhanden ({betroffene}) - keine automatische Überschreibung/erneute Vollbuchung "
                    "bereits gebuchter Perioden. Eine differenzielle Korrektur ist nur belegt und manuell "
                    "vorzunehmen."
                )

        if gruende or alte_komponente is None or profil is None:
            return gruende, None

        plan = {
            "alte_komponente": alte_komponente,
            "neuer_betrag_cent": verteilung["neuer_betrag_cent"],
            "wirksam_ab": schreiben.zahlungspflicht_ab,
            "profil": profil,
        }
        return [], plan

    def _ausfuehrungsnachweis_aktualisieren(
        self,
        session: Session,
        *,
        schreiben_id: int,
        vertrag_id: str,
        status: str,
        gruende: list[str],
        akteur: str,
        quelle_hash: str | None = None,
        neue_komponente_id: str | None = None,
        beendete_komponente_id: str | None = None,
        neues_rechtsprofil_id: int | None = None,
        wirksam_ab: date | None = None,
    ) -> IndexSollUmsetzungTable:
        """GENAU EINE Zeile je Erhöhungsschreiben (UNIQUE) - ein erneuter
        Anlauf (nach behobener Blockierursache) AKTUALISIERT diese eine
        Zeile, statt eine zweite anzulegen (kein Unique-Konflikt, voller
        Verlauf bleibt trotzdem über `blockiert_gruende`/`status` je
        aktuellem Stand nachvollziehbar)."""

        nachweis = session.execute(
            select(IndexSollUmsetzungTable).where(IndexSollUmsetzungTable.erhoehungsschreiben_id == schreiben_id)
        ).scalar_one_or_none()
        if nachweis is None:
            nachweis = IndexSollUmsetzungTable(erhoehungsschreiben_id=schreiben_id, vertrag_id=vertrag_id)
            session.add(nachweis)
        nachweis.status = status
        nachweis.blockiert_gruende = gruende
        nachweis.akteur = akteur
        if quelle_hash is not None:
            nachweis.quelle_hash = quelle_hash
        if neue_komponente_id is not None:
            nachweis.neue_komponente_id = neue_komponente_id
        if beendete_komponente_id is not None:
            nachweis.beendete_komponente_id = beendete_komponente_id
        if neues_rechtsprofil_id is not None:
            nachweis.neues_rechtsprofil_id = neues_rechtsprofil_id
        if wirksam_ab is not None:
            nachweis.wirksam_ab = wirksam_ab
        return nachweis

    def vorschau(self, *, ctx: AuthContext, erhoehungsschreiben_id: int, heute: date) -> dict:
        """Rein lesend (GET, kein Claim/keine Statusänderung) - Vorschau
        Soll alt/neu ab Datum bzw. konkreter Blockiergrund für die
        Backoffice-Ansicht. Schreibt NIE einen Status/Blockiergrund fort
        (das bleibt ausschließlich `umsetzen()` vorbehalten)."""

        schreiben, vertrag = self._schreiben_und_vertrag(erhoehungsschreiben_id)
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)

        with self._session_factory() as session:
            gruende, plan = self._pruefen(session, schreiben=schreiben, vertrag=vertrag, heute=heute)

        ergebnis: dict = {
            "erhoehungsschreiben_id": schreiben.id,
            "vertrag_id": vertrag.id,
            "status": schreiben.status,
            "zahlungspflicht_ab": schreiben.zahlungspflicht_ab.isoformat() if schreiben.zahlungspflicht_ab else None,
            "blockiert": bool(gruende),
            "gruende": gruende,
        }
        if plan is not None:
            ergebnis.update(
                {
                    "komponente_id": plan["alte_komponente"].id,
                    "alter_betrag_cent": plan["alte_komponente"].betrag_cent,
                    "neuer_betrag_cent": plan["neuer_betrag_cent"],
                    "wirksam_ab": plan["wirksam_ab"].isoformat(),
                }
            )
        return ergebnis

    def umsetzen(
        self, *, ctx: AuthContext, erhoehungsschreiben_id: int, heute: date, akteur: str, soll_umsetzung_enabled: bool
    ) -> UmsetzungsErgebnis:
        """`soll_umsetzung_enabled`: wie `outbox_service.versenden()`s
        `send_enabled` als Aufrufparameter (nicht Konstruktorzustand) -
        DAMIT sowohl der tägliche Worker als auch ein manueller
        Backoffice-Trigger dieselbe Sperre respektieren, ohne dass ein
        Aufrufer sie vergessen kann. Bewusst VOR dem Claim geprüft (kein
        Statuswechsel, keine Spur, wenn die Funktion insgesamt noch
        deaktiviert ist)."""

        schreiben, vertrag = self._schreiben_und_vertrag(erhoehungsschreiben_id)
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)
        if not soll_umsetzung_enabled:
            return UmsetzungsErgebnis(
                status="BEREITS_VERARBEITET",
                gruende=["Soll-Umsetzung ist konfigurationsseitig deaktiviert (indexautomatik_soll_umsetzung_enabled=false)."],
            )

        with self._session_factory() as session:
            # Claim: EINZIGE WHERE-Bedingung inkl. Status ist das atomare
            # Compare-and-Swap (analog `ErhoehungsschreibenRepository.
            # claim_fuer_versand`) - schließt jeden parallelen zweiten
            # Versuch (Doppelklick, zweiter Worker, Retry nach Absturz)
            # sofort aus. rowcount==0 heißt: ein anderer Aufruf hat diesen
            # Fall bereits abschließend verarbeitet (oder er ist gar nicht
            # (mehr) im richtigen Ausgangsstatus).
            claim = session.execute(
                update(ErhoehungsschreibenTable)
                .where(ErhoehungsschreibenTable.id == schreiben.id)
                .where(ErhoehungsschreibenTable.status.in_(_CLAIMBARE_STATUS))
                .values(status="SOLL_UMSETZUNG_IN_PRUEFUNG")
            )
            if claim.rowcount == 0:
                aktuell = session.get(ErhoehungsschreibenTable, schreiben.id)
                session.rollback()
                return UmsetzungsErgebnis(
                    status="BEREITS_VERARBEITET",
                    gruende=[
                        f"Status ist bereits {aktuell.status if aktuell else 'unbekannt'} - keine "
                        "Doppelumsetzung/Parallelverarbeitung."
                    ],
                )

            frisches_schreiben = session.get(ErhoehungsschreibenTable, schreiben.id)
            frischer_vertrag = session.get(VertragTable, vertrag.id)
            assert frisches_schreiben is not None and frischer_vertrag is not None

            gruende, plan = self._pruefen(session, schreiben=frisches_schreiben, vertrag=frischer_vertrag, heute=heute)
            if gruende:
                frisches_schreiben.status = "SOLL_UMSETZUNG_BLOCKIERT"
                frisches_schreiben.blockiert_gruende = gruende
                self._ausfuehrungsnachweis_aktualisieren(
                    session,
                    schreiben_id=frisches_schreiben.id,
                    vertrag_id=vertrag.id,
                    status="BLOCKIERT",
                    gruende=gruende,
                    akteur=akteur,
                )
                session.commit()
                return UmsetzungsErgebnis(status="BLOCKIERT", gruende=gruende)

            alte_komponente: VertragsKomponenteTable = plan["alte_komponente"]
            neuer_betrag_cent: int = plan["neuer_betrag_cent"]
            wirksam_ab: date = plan["wirksam_ab"]
            profil: RechtsprofilTable = plan["profil"]

            # Historisierung (append-only, wie überall in diesem
            # Repository): die BESTEHENDE Komponentenzeile wird per
            # gueltig_bis geschlossen, NIE `betrag_cent` in-place
            # geändert - historische OP/Vorschreibungspositionen
            # referenzieren betragsschnappschüsse, kein Live-Betrag.
            alte_komponente.gueltig_bis = wirksam_ab - timedelta(days=1)
            neue_komponente_id = f"{alte_komponente.id}-IDX{frisches_schreiben.id}"
            self._stammdaten_repository.add_komponente(
                id=neue_komponente_id,
                vertrag_id=vertrag.id,
                art=alte_komponente.art,
                bezeichnung=alte_komponente.bezeichnung,
                betrag_cent=neuer_betrag_cent,
                ust_satz_promille=alte_komponente.ust_satz_promille,
                indexierbar=alte_komponente.indexierbar,
                gueltig_von=wirksam_ab,
                gueltig_bis=None,
                session=session,
            )

            # Neue Rechtsprofil-VERSION (nie in-place) - die für DIESES
            # Schreiben tatsächlich verwendete alte Version bleibt
            # unverändert nachvollziehbar. basis_komponenten_ids zeigt
            # jetzt auf die NEUE Komponente; bezugsjahr wird beim
            # MieWeG-Pfad auf das verarbeitete Bewertungsjahr
            # vorgezogen, damit `_monatslauf_mieweg` den nächsten Zyklus
            # nicht dauerhaft mit TERMIN_NICHT_ERREICHT blockiert (der
            # Geschäftsraum-/Klausel-Pfad hat kein `ziel_bewertungsjahr`
            # und damit auch kein analoges Gate - `bezugsjahr` bleibt
            # dort unverändert).
            neue_basis_ids = sorted(
                {
                    (neue_komponente_id if kid == alte_komponente.id else kid)
                    for kid in profil.basis_komponenten_ids
                }
            )
            naechste_version = session.execute(
                select(func.max(RechtsprofilTable.version)).where(RechtsprofilTable.vertrag_id == vertrag.id)
            ).scalar_one()
            neues_profil = RechtsprofilTable(
                vertrag_id=vertrag.id,
                version=(naechste_version or 0) + 1,
                rechtsordnung=profil.rechtsordnung,
                ist_wohnungsnutzung=profil.ist_wohnungsnutzung,
                mrg_zinsbeschraenkung=profil.mrg_zinsbeschraenkung,
                ist_altvertrag=profil.ist_altvertrag,
                ist_hauptmiete=profil.ist_hauptmiete,
                foerderbindung=profil.foerderbindung,
                mietzinsobergrenze_cent=profil.mietzinsobergrenze_cent,
                mietzinsobergrenze_quellenbeleg=profil.mietzinsobergrenze_quellenbeleg,
                mietzinsobergrenze_gueltig_bis=profil.mietzinsobergrenze_gueltig_bis,
                bezugsjahr=frisches_schreiben.ziel_bewertungsjahr or profil.bezugsjahr,
                bezugsmonat=profil.bezugsmonat,
                letzte_basis_war_jahresdurchschnitt=profil.letzte_basis_war_jahresdurchschnitt,
                basis_komponenten_ids=neue_basis_ids,
                vpi_reihe=profil.vpi_reihe,
                vertraglich_zulaessiger_betrag_cent=profil.vertraglich_zulaessiger_betrag_cent,
                vertraglicher_quellenbeleg=profil.vertraglicher_quellenbeleg,
                vertraglicher_fruehestmoeglicher_termin=profil.vertraglicher_fruehestmoeglicher_termin,
                vertragsklausel_id=profil.vertragsklausel_id,
                vertrag_beleg_referenz=profil.vertrag_beleg_referenz,
                klausel_referenz=profil.klausel_referenz,
                frist_tage_zugang_bis_wirksamkeit=profil.frist_tage_zugang_bis_wirksamkeit,
                frist_quellenbeleg=profil.frist_quellenbeleg,
                status="ENTWURF",
                erstellt_von=akteur,
            )
            session.add(neues_profil)
            session.flush()  # eigene id für Snapshot/Hash-Berechnung benötigt

            # `session=session` ist hier Pflicht (siehe rechtsprofil.py::
            # _quelle_snapshot-Docstring): die neue Komponente ist in
            # DIESER Transaktion noch nicht committet, eine separat
            # geöffnete Session sähe sie noch nicht und würde einen
            # abweichenden, für spätere Prüfungen falschen Hash liefern.
            neuer_hash = compute_content_hash(
                self._rechtsprofil_service._quelle_snapshot(neues_profil, frischer_vertrag, session=session)
            )
            # Genau EIN FREIGEGEBENES Profil je Vertrag (exakt wie
            # `RechtsprofilRepository.freigeben`, hier innerhalb DERSELBEN
            # Transaktion nachgebaut statt über die separate Session des
            # bestehenden Repositorys aufgerufen - Pflicht, weil Claim,
            # Komponenten- UND Profiländerung zusammen committet werden
            # müssen).
            session.execute(
                update(RechtsprofilTable)
                .where(RechtsprofilTable.vertrag_id == vertrag.id)
                .where(RechtsprofilTable.status == "FREIGEGEBEN")
                .values(status="INVALIDIERT")
            )
            neues_profil.status = "FREIGEGEBEN"
            neues_profil.freigegeben_von = akteur
            neues_profil.freigegeben_am = datetime.now(timezone.utc)
            neues_profil.quelle_hash = neuer_hash

            frisches_schreiben.status = "SOLL_UMGESETZT"
            frisches_schreiben.blockiert_gruende = []

            self._ausfuehrungsnachweis_aktualisieren(
                session,
                schreiben_id=frisches_schreiben.id,
                vertrag_id=vertrag.id,
                status="UMGESETZT",
                gruende=[],
                akteur=akteur,
                quelle_hash=neuer_hash,
                neue_komponente_id=neue_komponente_id,
                beendete_komponente_id=alte_komponente.id,
                neues_rechtsprofil_id=neues_profil.id,
                wirksam_ab=wirksam_ab,
            )
            session.commit()
            return UmsetzungsErgebnis(
                status="UMGESETZT",
                neue_komponente_id=neue_komponente_id,
                neues_rechtsprofil_id=neues_profil.id,
            )
