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

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.auth.service import AuthContext, require_gesellschaft_access, require_schreibrecht
from mietinkasso.domain.exceptions import ObjektAusgeschlossenError
from mietinkasso.indexautomatik.rechtsprofil import RechtsprofilService
from mietinkasso.indexautomatik.repository import ErhoehungsschreibenRepository
from mietinkasso.infrastructure.db.tables import (
    ErhoehungsschreibenTable,
    IndexAnpassungTable,
    IndexKlauselTable,
    IndexSollUmsetzungTable,
    RechtsprofilTable,
    VertragsKomponenteTable,
    VertragTable,
    VorschreibungPositionTable,
    VorschreibungTable,
)
from mietinkasso.mieweg_vorschau.service import _NIE_INDEXIERBARE_ARTEN
from mietinkasso.op.service import compute_content_hash
from mietinkasso.stammdaten.repository import StammdatenRepository

def _anspruchsmonat_start(datum: date) -> date:
    """Trennt den ANSPRUCHSMONAT (der Kalendermonat, in den die
    Zahlungspflicht fällt) von der konkreten FÄLLIGKEIT (Tag im Monat,
    z. B. der 5./15.) - unabhängige Rückprüfung: `VorschreibungService.
    entwurf_erstellen` liest aktive Komponenten IMMER zum Monatsersten
    (`faelligkeitsdatum(monat, 1)`), nicht zum Fälligkeitstag. Eine
    Komponentenwirksamkeit mitten im Monat (z. B. `gueltig_von` = der
    15.) wäre am Monatsersten desselben Monats noch NICHT aktiv, und die
    Monatsvorschreibung würde den alten Betrag für den GESAMTEN
    Anspruchsmonat verwenden. Die TECHNISCHE Komponentenwirksamkeit wird
    deshalb bewusst auf den 1. des Anspruchsmonats gelegt (keine
    taggenaue Proration - dieses Repository unterstützt keine anteiligen
    Monatsbeträge); die tatsächliche Fälligkeit bleibt davon unberührt
    auf `ErhoehungsschreibenTable.zahlungspflicht_ab` sichtbar."""

    return date(datum.year, datum.month, 1)


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
    # Bequemlichkeitsfeld: NUR im Ein-Komponenten-Fall gesetzt (der
    # weiterhin häufigste Fall) - bei Mehrkomponentenverteilung `None`,
    # siehe `neue_komponenten_ids` für die generische, immer vollständige
    # Liste (Ein- UND Mehrkomponenten-Fall).
    neue_komponente_id: str | None = None
    neue_komponenten_ids: list[str] = field(default_factory=list)
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

        # Codex-Rückprüfung (499c36f, Fund a): ohne einen TATSÄCHLICHEN,
        # vom Transport bestätigten Versand (versendet_am +
        # externe_versandreferenz) darf niemals umgesetzt werden - ein
        # (z. B. synthetisch fehlerhaft) direkt auf SOLL_UMSETZUNG_OFFEN
        # gesetzter Fall OHNE durchlaufenen Versand ist kein zugegangenes
        # Schreiben, egal was `zugang_bestaetigt_am` sagt.
        if schreiben.versendet_am is None or not (schreiben.externe_versandreferenz or "").strip():
            gruende.append(
                "Kein tatsächlicher Versandbeleg (versendet_am/externe_versandreferenz) vorhanden - "
                "Voraussetzung für eine Soll-Umsetzung fehlt."
            )
        if (
            schreiben.zugang_bestaetigt_am is None
            or not (schreiben.zugang_beleg or "").strip()
            or schreiben.zahlungspflicht_ab is None
        ):
            gruende.append(
                "Kein bestätigter Zugang MIT Belegreferenz bzw. keine daraus berechnete Zahlungspflicht "
                "vorhanden - Voraussetzung für eine Soll-Umsetzung fehlt."
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

        # Mehrkomponentenverteilung (Codex-Rückprüfung 499c36f/8f499c9):
        # `komponenten_verteilung` trägt eine LISTE von Einträgen
        # (`eintraege`), auch im weiterhin häufigsten Ein-Komponenten-Fall -
        # `outbox_service._verteile_erhoehung_centgenau` verteilt die
        # Gesamterhöhung centgenau proportional auf jede referenzierte
        # Komponente. JEDER Eintrag wird hier einzeln gegen den AKTUELLEN
        # Stand re-validiert (analog dem bisherigen Ein-Komponenten-Fall);
        # ein einziger fehlerhafter Eintrag blockiert die GESAMTE Umsetzung
        # (keine Teilumsetzung einer Mehrkomponenten-Erhöhung).
        verteilung = schreiben.komponenten_verteilung or {}
        eintraege = verteilung.get("eintraege") or []
        alte_komponenten: list[tuple[VertragsKomponenteTable, int]] | None = None
        if not eintraege:
            gruende.append(
                "Keine (oder leere) komponenten_verteilung-Einträge vorhanden - keine automatische "
                "Umsetzung ohne centgenaue Verteilung auf mindestens eine Komponente."
            )
        else:
            kandidaten: list[tuple[VertragsKomponenteTable, int]] = []
            ids_gesehen: set[str] = set()
            fehlerhaft = False
            for eintrag in eintraege:
                komponente_id = eintrag.get("komponente_id")
                if not komponente_id or komponente_id in ids_gesehen:
                    gruende.append(
                        f"Komponenten_verteilung enthält eine fehlende oder doppelte Komponenten-ID "
                        f"({komponente_id!r}) - keine Umsetzung auf einem unstimmigen Datensatz."
                    )
                    fehlerhaft = True
                    break
                ids_gesehen.add(komponente_id)
                komponente = session.get(VertragsKomponenteTable, komponente_id)
                if komponente is None or komponente.vertrag_id != vertrag.id:
                    gruende.append(
                        f"Komponente {komponente_id} ist nicht mehr auffindbar oder gehört nicht mehr zu "
                        "diesem Vertrag - Stale-Snapshot, neue Prüfung erforderlich."
                    )
                    fehlerhaft = True
                    break
                if komponente.betrag_cent != eintrag.get("alter_betrag_cent"):
                    gruende.append(
                        f"Komponente {komponente.id} wurde seit Schreibenserstellung anderweitig auf "
                        f"{komponente.betrag_cent} Cent geändert (Schreiben ging von "
                        f"{eintrag.get('alter_betrag_cent')} Cent aus) - Stale-Snapshot."
                    )
                    fehlerhaft = True
                    break
                if komponente.art in _NIE_INDEXIERBARE_ARTEN:
                    gruende.append(
                        f"Komponente {komponente.id} hat die Art '{komponente.art}' - BK/HK/USt-"
                        "Vorauszahlungen und vergleichbare Aliasarten werden nie mitindexiert/umgesetzt."
                    )
                    fehlerhaft = True
                    break
                if (
                    komponente.gueltig_bis is not None
                    and schreiben.zahlungspflicht_ab is not None
                    and komponente.gueltig_bis < schreiben.zahlungspflicht_ab
                ):
                    gruende.append(
                        f"Komponente {komponente.id} ist bereits ab {komponente.gueltig_bis.isoformat()} "
                        "befristet ausgelaufen - vor dem Wirksamkeitsdatum, keine Umsetzung auf eine "
                        "beendete Position."
                    )
                    fehlerhaft = True
                    break
                kandidaten.append((komponente, eintrag.get("neuer_betrag_cent")))
            if not fehlerhaft:
                # Codex-Rückprüfung (499c36f, Fund d), jetzt über die
                # GESAMTE Verteilung statt nur eine einzelne Komponente: die
                # Summe der gespeicherten Delta-Beträge muss EXAKT der
                # versendeten Gesamterhöhung entsprechen - eine
                # manipulierte/inkonsistente Einzelverteilung, die in Summe
                # trotzdem nicht passt, wird NIE blind gebucht.
                alte_summe = sum((eintrag.get("alter_betrag_cent") or 0) for eintrag in eintraege)
                neue_summe = sum((eintrag.get("neuer_betrag_cent") or 0) for eintrag in eintraege)
                if neue_summe - alte_summe != schreiben.erhoehung_cent:
                    gruende.append(
                        f"Gespeicherte Verteilung ist inkonsistent mit dem versendeten Schreiben (Summe der "
                        f"Delta-Beträge {neue_summe - alte_summe} Cent entspricht nicht der Gesamterhöhung "
                        f"{schreiben.erhoehung_cent} Cent) - keine Umsetzung auf einem unstimmigen Datensatz."
                    )
                else:
                    alte_komponenten = kandidaten

        profil = session.get(RechtsprofilTable, schreiben.rechtsprofil_id)
        if profil is None or profil.version != schreiben.rechtsprofil_version:
            gruende.append("Zugrunde liegendes Rechtsprofil nicht mehr auffindbar oder Version weicht ab.")
            profil = None
        elif profil.status != "FREIGEGEBEN":
            gruende.append(f"Rechtsprofil hat Status '{profil.status}', ist nicht mehr FREIGEGEBEN.")
            profil = None
        elif not self._rechtsprofil_service.ist_noch_gueltig(profil, heute=heute):
            # Codex-Rückprüfung (499c36f, Fund b): ein reiner Hash-
            # Vergleich (wie zuvor hier) übersieht eine ABGELAUFENE
            # `mietzinsobergrenze_gueltig_bis` - `ist_noch_gueltig` prüft
            # BEIDES (Quelle UND zeitliche Gültigkeit des Obergrenze-
            # Belegs), exakt wie jede andere reguläre Prüfung dieses
            # Rechtsprofils (`indexautomatik/service.py`, `outbox_service.
            # versenden`).
            gruende.append(
                "Rechtsprofil ist nicht mehr gültig (Quelle seit Freigabe geändert oder Mietzinsobergrenze-"
                "Beleg zum heutigen Stichtag abgelaufen) - Umsetzung wird gesperrt, keine Umsetzung auf "
                "veraltetem/abgelaufenem Stand."
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

        if gruende or alte_komponenten is None or profil is None:
            return gruende, None

        # Codex-Rückprüfung (499c36f): `VorschreibungService.
        # entwurf_erstellen` liest aktive Komponenten IMMER zum
        # Monatsersten - die technische Komponentenwirksamkeit wird
        # deshalb bewusst auf den Anspruchsmonat gelegt, GETRENNT von der
        # tatsächlichen (taggenauen) Fälligkeit (siehe
        # `_anspruchsmonat_start`-Docstring).
        anspruchsmonat = _anspruchsmonat_start(schreiben.zahlungspflicht_ab)
        plan = {
            # list[(VertragsKomponenteTable, neuer_betrag_cent)] - eine oder
            # mehrere betroffene Komponenten (Mehrkomponentenverteilung).
            "alte_komponenten": alte_komponenten,
            "wirksam_ab": anspruchsmonat,
            "zahlungspflicht_ab": schreiben.zahlungspflicht_ab,
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
        neue_komponenten_ids: list[str] | None = None,
        beendete_komponenten_ids: list[str] | None = None,
        neues_rechtsprofil_id: int | None = None,
        wirksam_ab: date | None = None,
    ) -> IndexSollUmsetzungTable:
        """GENAU EINE Zeile je Erhöhungsschreiben (UNIQUE) - ein erneuter
        Anlauf (nach behobener Blockierursache) AKTUALISIERT diese eine
        Zeile, statt eine zweite anzulegen (kein Unique-Konflikt, voller
        Verlauf bleibt trotzdem über `blockiert_gruende`/`status` je
        aktuellem Stand nachvollziehbar). `neue_komponente_id`/
        `beendete_komponente_id` bleiben die Bequemlichkeitsfelder für den
        (weiterhin häufigsten) Ein-Komponenten-Fall; `neue_komponenten_ids`/
        `beendete_komponenten_ids` sind die generische, bei Mehrkomponenten-
        verteilung vollständige Quelle."""

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
        if neue_komponenten_ids is not None:
            nachweis.neue_komponenten_ids = neue_komponenten_ids
        if beendete_komponenten_ids is not None:
            nachweis.beendete_komponenten_ids = beendete_komponenten_ids
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
                    "eintraege": [
                        {
                            "komponente_id": alte_komponente.id,
                            "alter_betrag_cent": alte_komponente.betrag_cent,
                            "neuer_betrag_cent": neuer_betrag_cent,
                        }
                        for alte_komponente, neuer_betrag_cent in plan["alte_komponenten"]
                    ],
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

            alte_komponenten: list[tuple[VertragsKomponenteTable, int]] = plan["alte_komponenten"]
            wirksam_ab: date = plan["wirksam_ab"]  # Anspruchsmonat-Start (Monatserster)
            profil: RechtsprofilTable = plan["profil"]

            # Historisierung (append-only, wie überall in diesem
            # Repository): JEDE betroffene Komponentenzeile wird per
            # gueltig_bis geschlossen, NIE `betrag_cent` in-place geändert -
            # historische OP/Vorschreibungspositionen referenzieren
            # Betragsschnappschüsse, kein Live-Betrag. Bei Mehrkomponenten-
            # verteilung (z. B. HMZ+Küche) betrifft das mehrere Zeilen in
            # dieser einen Transaktion, nie eine gemeinsame Zeile.
            neue_komponenten_ids: list[str] = []
            alt_zu_neu_id: dict[str, str] = {}
            for alte_komponente, neuer_betrag_cent in alte_komponenten:
                # Ein ursprünglich geplantes Enddatum der alten Komponente
                # (z. B. eine befristete Klausel) darf durch die Umsetzung
                # NICHT verloren gehen (Codex-Rückprüfung, Fund c) - es wird
                # unverändert auf die NEUE Komponente übertragen.
                urspruengliches_gueltig_bis = alte_komponente.gueltig_bis
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
                    gueltig_bis=urspruengliches_gueltig_bis,
                    # Explizite Historisierungs-Kette (KEINE ID-String-
                    # Heuristik): `mieweg_vorschau/service.py`s Existenz-
                    # prüfung verfolgt diese Referenz bis zur ursprünglichen
                    # Zeile zurück - die neue Zeile ist rechtlich dieselbe,
                    # ununterbrochen fortbestehende Verpflichtung wie
                    # `alte_komponente`, nur eine neue DB-Zeile (append-only
                    # Historisierung), keine neu vereinbarte Komponente.
                    historisiert_von_id=alte_komponente.id,
                    session=session,
                )
                neue_komponenten_ids.append(neue_komponente_id)
                alt_zu_neu_id[alte_komponente.id] = neue_komponente_id

            # Neue Rechtsprofil-VERSION (nie in-place) - die für DIESES
            # Schreiben tatsächlich verwendete alte Version bleibt
            # unverändert nachvollziehbar. basis_komponenten_ids zeigt
            # jetzt auf die NEUE Komponente. Beim MieWeG-Pfad werden
            # bezugsjahr/-monat/letzte_basis_war_jahresdurchschnitt
            # gemeinsam auf die neue Jahresbasis fortgeschrieben (Codex-
            # Rückprüfung 499c36f/fb34ecb).
            #
            # `berechne_gesetzliche_hoechstgrenze` (mieweg_vorschau/
            # berechnung.py) behandelt die ERSTE Loop-Iteration
            # (jahr=erster_bezug_jahr) IMMER als u. U. unterjährig
            # (`anteil=(12-erster_bezug_monat)/12`), jede FOLGENDE
            # Iteration voll (`anteil=1`). Mit `erster_bezug_monat=12`
            # (effektiver_monat bei `letzte_basis_war_jahresdurchschnitt`)
            # wird die ERSTE Iteration bewusst zu `anteil=0` - ein
            # gezielter "Leerschritt", der GENAU DAS bereits verarbeitete
            # Jahr (das schon Teil der vorherigen Erhöhung war) neutral
            # überspringt, damit die ECHTE neue Vergleichsperiode erst in
            # der ZWEITEN (voll gewichteten) Iteration ankommt. Deshalb
            # ist die neue `bezugsjahr` NICHT das verarbeitete
            # `ziel_bewertungsjahr` selbst, sondern EIN JAHR DAVOR - sonst
            # hätte ein direkt folgender Jahreszyklus (ziel_bewertungsjahr
            # der Vorperiode + 1) nur die eine (geleerte) Iteration und
            # ergäbe fälschlich GAR KEINE Erhöhung (unabhängige Rückprüfung
            # fb34ecb: "ergibt für das Folgejahr im ersten Schritt 0").
            # Der Geschäftsraum-/Klausel-Pfad hat kein `ziel_bewertungsjahr`
            # und damit kein analoges Gate - alle drei Felder bleiben dort
            # unverändert.
            ist_mieweg_pfad = frisches_schreiben.ziel_bewertungsjahr is not None
            neues_bezugsjahr = (
                frisches_schreiben.ziel_bewertungsjahr - 1 if ist_mieweg_pfad else profil.bezugsjahr
            )
            neuer_bezugsmonat = 12 if ist_mieweg_pfad else profil.bezugsmonat
            neue_letzte_basis_war_jahresdurchschnitt = (
                True if ist_mieweg_pfad else profil.letzte_basis_war_jahresdurchschnitt
            )
            neue_basis_ids = sorted({alt_zu_neu_id.get(kid, kid) for kid in profil.basis_komponenten_ids})
            # Ein belegter Ausnahmenachweis (siehe `RechtsprofilTable.
            # historische_basis_belege`-Docstring) bleibt an die
            # KOMPONENTEN-ID gebunden - nach der Historisierung muss er
            # deshalb auf die NEUE ID mitwandern, sonst würde er nach
            # genau einem Zyklus stillschweigend wirkungslos.
            neue_historische_basis_belege = {
                alt_zu_neu_id.get(kid, kid): beleg for kid, beleg in (profil.historische_basis_belege or {}).items()
            }

            # Klausel-Basisfortschreibung (Geschäftsraum-/generischer-
            # Klausel-Pfad, Codex-Rückprüfung: "basis_wert/basis_monat/
            # letzte_anpassung werden NICHT fortgeschrieben -> wiederholte
            # Indexierung auf schon erhöhten Betrag droht"). Rein
            # mechanische Bestandskorrektur (KEINE neue Rechtsentscheidung
            # zu Fristen/Terminen - dafür bleibt `_monatslauf_klausel`
            # bewusst gesperrt, siehe dort): eine bereits umgesetzte
            # Erhöhung darf beim NÄCHSTEN Vergleich nicht erneut gegen den
            # ALTEN `basis_wert` gerechnet werden, sonst würde derselbe
            # VPI-Sprung ein zweites Mal als Erhöhung ausgewiesen. Wie bei
            # `VertragsKomponenteTable`/`RechtsprofilTable` append-only:
            # eine NEUE `IndexKlauselTable`-Version ersetzt die alte
            # (`GESPERRT`+`ersetzt_id`), NIE ein In-Place-Update von
            # `basis_wert` auf der bestehenden Zeile.
            neue_vertragsklausel_id = profil.vertragsklausel_id
            if profil.vertragsklausel_id is not None and frisches_schreiben.index_anpassung_id is not None:
                alte_klausel = session.get(IndexKlauselTable, profil.vertragsklausel_id)
                anpassung = session.get(IndexAnpassungTable, frisches_schreiben.index_anpassung_id)
                if alte_klausel is not None and anpassung is not None:
                    anspruchsmonat_str = f"{wirksam_ab.year:04d}-{wirksam_ab.month:02d}"
                    naechste_klausel_version = session.execute(
                        select(func.max(IndexKlauselTable.version)).where(
                            IndexKlauselTable.vertrag_id == alte_klausel.vertrag_id
                        )
                    ).scalar_one()
                    neue_klausel = IndexKlauselTable(
                        vertrag_id=alte_klausel.vertrag_id,
                        version=(naechste_klausel_version or 0) + 1,
                        rechtsordnung=alte_klausel.rechtsordnung,
                        berechnungsprofil=alte_klausel.berechnungsprofil,
                        klausel_text=alte_klausel.klausel_text,
                        abschlussdatum=alte_klausel.abschlussdatum,
                        basis_reihe=alte_klausel.basis_reihe,
                        # Fortgeschrieben auf den tatsächlich für DIESE
                        # Anpassung verwendeten Wert - der Kern der
                        # Korrektur, verhindert die wiederholte Indexierung.
                        basis_wert=anpassung.neuer_wert,
                        # Codex-Rückprüfung (zu c01ceb2): `basis_monat` muss
                        # der TATSÄCHLICHE VPI-Quellmonat von `neuer_wert`
                        # sein, NICHT der Anspruchsmonat der neuen Miete
                        # (zwei unterschiedliche Monate, z. B. bei einer
                        # vertraglichen Wartefrist). `IndexAnpassungTable.
                        # vpi_jahr`/`vpi_monat` tragen diesen Bezug seit
                        # `IndexService.berechne_vorschlag` nachvollziehbar
                        # mit; eine ältere, davor erzeugte Anpassung ohne
                        # diese Angabe (`None`) fällt auf den Anspruchsmonat
                        # zurück (degradiert, aber niemals ein erfundener
                        # früherer Monat).
                        basis_monat=(
                            f"{anpassung.vpi_jahr:04d}-{anpassung.vpi_monat:02d}"
                            if anpassung.vpi_jahr is not None and anpassung.vpi_monat is not None
                            else anspruchsmonat_str
                        ),
                        letzte_anpassung_monat=anspruchsmonat_str,
                        schwelle_prozent=alte_klausel.schwelle_prozent,
                        schwelle_inklusive=alte_klausel.schwelle_inklusive,
                        daempfung_prozent=alte_klausel.daempfung_prozent,
                        vertragliche_grenze_prozent=alte_klausel.vertragliche_grenze_prozent,
                        indexierbare_komponenten=list(alte_klausel.indexierbare_komponenten or []),
                        # Belegtes Kalender-/Intervall-/Rundungs-/
                        # Wartefrist-Regelprofil bleibt bei der mechanischen
                        # Fortschreibung UNVERÄNDERT erhalten (Auftrag
                        # Markus) - reine Bestandskorrektur der Basis, keine
                        # neue Fachentscheidung zur Vertragsregel selbst.
                        anpassungsmonat=alte_klausel.anpassungsmonat,
                        mindestintervall_monate=alte_klausel.mindestintervall_monate,
                        indexwert_rundung_dezimalstellen=alte_klausel.indexwert_rundung_dezimalstellen,
                        wartefrist_monate_nach_indexereignis=alte_klausel.wartefrist_monate_nach_indexereignis,
                        wartefrist_bezug=alte_klausel.wartefrist_bezug,
                        status="FREIGEGEBEN",
                        freigegeben_am=datetime.now(timezone.utc),
                        freigegeben_von=akteur,
                    )
                    session.add(neue_klausel)
                    session.flush()
                    alte_klausel.status = "GESPERRT"
                    alte_klausel.ersetzt_id = neue_klausel.id
                    neue_vertragsklausel_id = neue_klausel.id

            naechste_version = session.execute(
                select(func.max(RechtsprofilTable.version)).where(RechtsprofilTable.vertrag_id == vertrag.id)
            ).scalar_one()
            neues_profil = RechtsprofilTable(
                vertrag_id=vertrag.id,
                version=(naechste_version or 0) + 1,
                rechtsordnung=profil.rechtsordnung,
                ist_wohnungsnutzung=profil.ist_wohnungsnutzung,
                mrg_zinsbeschraenkung=profil.mrg_zinsbeschraenkung,
                mrg_zinsbeschraenkung_geprueft=profil.mrg_zinsbeschraenkung_geprueft,
                ist_altvertrag=profil.ist_altvertrag,
                ist_hauptmiete=profil.ist_hauptmiete,
                foerderbindung=profil.foerderbindung,
                foerderbindung_geprueft=profil.foerderbindung_geprueft,
                mietzinsobergrenze_cent=profil.mietzinsobergrenze_cent,
                mietzinsobergrenze_quellenbeleg=profil.mietzinsobergrenze_quellenbeleg,
                mietzinsobergrenze_gueltig_bis=profil.mietzinsobergrenze_gueltig_bis,
                bezugsjahr=neues_bezugsjahr,
                bezugsmonat=neuer_bezugsmonat,
                letzte_basis_war_jahresdurchschnitt=neue_letzte_basis_war_jahresdurchschnitt,
                basis_komponenten_ids=neue_basis_ids,
                historische_basis_belege=neue_historische_basis_belege,
                vpi_reihe=profil.vpi_reihe,
                vertraglich_zulaessiger_betrag_cent=profil.vertraglich_zulaessiger_betrag_cent,
                vertraglicher_quellenbeleg=profil.vertraglicher_quellenbeleg,
                vertraglicher_fruehestmoeglicher_termin=profil.vertraglicher_fruehestmoeglicher_termin,
                vertragsklausel_id=neue_vertragsklausel_id,
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

            # Codex-Rückprüfung: `VorschreibungService.entwurf_erstellen`
            # befüllt eine Monatsvorschreibung NUR beim allerersten Aufruf
            # (`if not bestehende_positionen`) - eine bereits VORHER
            # angelegte, noch nicht gebuchte ENTWURF-Vorschreibung für den
            # Wirksamkeitsmonat (oder einen späteren, ebenfalls noch
            # offenen Monat) bliebe sonst dauerhaft auf dem ALTEN
            # Komponentenstand stehen, obwohl die Umsetzung soeben neue
            # Beträge historisiert hat. `_pruefen()` blockiert die
            # Umsetzung bereits vollständig, wenn eine BEREITS GEBUCHTE
            # Periode betroffen wäre (status != ENTWURF) - hier geht es
            # NUR um noch offene Entwürfe, die in DERSELBEN Transaktion aus
            # dem jetzt aktuellen (gerade historisierten) Komponentenstand
            # neu aufgebaut werden, exakt wie ein frischer
            # `entwurf_erstellen`-Aufruf es täte (inklusive unveränderter
            # BK/HK-Positionen).
            offene_entwuerfe = list(
                session.execute(
                    select(VorschreibungTable)
                    .where(VorschreibungTable.vertrag_id == vertrag.id)
                    .where(VorschreibungTable.monat >= f"{wirksam_ab.year:04d}-{wirksam_ab.month:02d}")
                    .where(VorschreibungTable.status == "ENTWURF")
                ).scalars()
            )
            for entwurf_zeile in offene_entwuerfe:
                stichtag = date(int(entwurf_zeile.monat[:4]), int(entwurf_zeile.monat[5:7]), 1)
                aktive_komponenten = self._stammdaten_repository.list_aktive_komponenten(
                    vertrag.id, stichtag, session=session
                )
                session.execute(
                    delete(VorschreibungPositionTable).where(
                        VorschreibungPositionTable.vorschreibung_id == entwurf_zeile.id
                    )
                )
                for komponente in aktive_komponenten:
                    session.add(
                        VorschreibungPositionTable(
                            vorschreibung_id=entwurf_zeile.id,
                            art=komponente.art,
                            bezeichnung=komponente.bezeichnung,
                            betrag_cent=komponente.betrag_cent,
                            ust_satz_promille=komponente.ust_satz_promille,
                        )
                    )

            frisches_schreiben.status = "SOLL_UMGESETZT"
            frisches_schreiben.blockiert_gruende = []

            beendete_komponenten_ids = [alte_komponente.id for alte_komponente, _ in alte_komponenten]
            self._ausfuehrungsnachweis_aktualisieren(
                session,
                schreiben_id=frisches_schreiben.id,
                vertrag_id=vertrag.id,
                status="UMGESETZT",
                gruende=[],
                akteur=akteur,
                quelle_hash=neuer_hash,
                neue_komponente_id=neue_komponenten_ids[0] if len(neue_komponenten_ids) == 1 else None,
                beendete_komponente_id=beendete_komponenten_ids[0] if len(beendete_komponenten_ids) == 1 else None,
                neue_komponenten_ids=neue_komponenten_ids,
                beendete_komponenten_ids=beendete_komponenten_ids,
                neues_rechtsprofil_id=neues_profil.id,
                wirksam_ab=wirksam_ab,
            )
            session.commit()
            return UmsetzungsErgebnis(
                status="UMGESETZT",
                neue_komponente_id=neue_komponenten_ids[0] if len(neue_komponenten_ids) == 1 else None,
                neue_komponenten_ids=neue_komponenten_ids,
                neues_rechtsprofil_id=neues_profil.id,
            )
