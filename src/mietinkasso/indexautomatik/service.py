"""Monatliche Indexautomatik (Auftrag 13.09., HV-20260913-INDEXAUTOMATIK)
- idempotent je (Vertrag, Kalendermonat), reine Orchestrierung über
bereits abgenommene Rechner: `mieweg_vorschau_service.vorschau_erstellen`
für den geprüften MieWeG-Wohnungsrechner-Fall (MRG-Voll-/Teilanwendung
UND bestätigte Wohnungsnutzung), `index/service.py::IndexService.
berechne_vorschlag` (über eine referenzierte, versionierte
`IndexKlauselTable`) für JEDEN anderen Fall mit einer geprüften
Vertragsklausel (insbesondere Geschäftsraum) - KEIN paralleler
Rohrechner, KEINE pauschale Rechtsklassifikation.

Ungeklärte/unvollständige Daten blockieren NUR den einzelnen Fall
(sichtbare `IndexautomatikLaufTable`-Zeile mit Gründen), nie den
gesamten Monatslauf."""

from __future__ import annotations

import json
from datetime import date

from mietinkasso.auth.service import AuthContext, require_gesellschaft_access, require_schreibrecht
from mietinkasso.domain.enums import Rechtsordnung
from mietinkasso.domain.exceptions import (
    IndexKlauselFehltError,
    ObjektAusgeschlossenError,
    QuellenbelegFehltError,
    RechtsordnungUngeklaertError,
    RechtsprofilNichtImplementiertError,
)
from mietinkasso.index.repository import IndexRepository
from mietinkasso.index.service import IndexService
from mietinkasso.indexautomatik.outbox_service import ErhoehungsschreibenOutboxService
from mietinkasso.indexautomatik.rechtsprofil import RechtsprofilService
from mietinkasso.indexautomatik.repository import ErhoehungsschreibenRepository, IndexautomatikLaufRepository, VpiRepository
from mietinkasso.indexautomatik.vertragsspur import berechne_vertragliche_spur_cent
from mietinkasso.infrastructure.db.tables import IndexautomatikLaufTable, RechtsprofilTable, VertragTable
from mietinkasso.mieweg_vorschau.service import MieWegVorschauService, VpiWert
from mietinkasso.stammdaten.repository import StammdatenRepository

_WOHNUNGSRECHNER_RECHTSORDNUNGEN = {
    Rechtsordnung.OESTERREICH_MRG_VOLL.value,
    Rechtsordnung.OESTERREICH_MRG_TEIL.value,
}

_BEHANDELBARE_FEHLER = (
    ValueError,
    QuellenbelegFehltError,
    IndexKlauselFehltError,
    RechtsordnungUngeklaertError,
    RechtsprofilNichtImplementiertError,
)


class IndexautomatikService:
    def __init__(
        self,
        *,
        stammdaten_repository: StammdatenRepository,
        rechtsprofil_service: RechtsprofilService,
        lauf_repository: IndexautomatikLaufRepository,
        outbox_repository: ErhoehungsschreibenRepository,
        vpi_repository: VpiRepository,
        mieweg_service: MieWegVorschauService,
        index_repository: IndexRepository,
        index_service: IndexService,
        outbox_service: ErhoehungsschreibenOutboxService,
    ):
        self._stammdaten_repository = stammdaten_repository
        self._rechtsprofil_service = rechtsprofil_service
        self._lauf_repository = lauf_repository
        self._outbox_repository = outbox_repository
        self._vpi_repository = vpi_repository
        self._mieweg_service = mieweg_service
        self._index_repository = index_repository
        self._index_service = index_service
        self._outbox_service = outbox_service

    def _anlegen_lauf(
        self,
        vertrag_id: str,
        periode: str,
        *,
        status: str,
        gruende: list[str] | None = None,
        profil: RechtsprofilTable | None = None,
        mieweg_vorschau_id: int | None = None,
        erhoehungsschreiben_id: int | None = None,
    ) -> IndexautomatikLaufTable:
        return self._lauf_repository.anlegen(
            IndexautomatikLaufTable(
                vertrag_id=vertrag_id,
                periode=periode,
                status=status,
                blockiert_gruende=list(gruende or []),
                rechtsprofil_id=profil.id if profil else None,
                rechtsprofil_version=profil.version if profil else None,
                mieweg_vorschau_id=mieweg_vorschau_id,
                erhoehungsschreiben_id=erhoehungsschreiben_id,
            )
        )

    def _unveraenderte_komponenten(self, vertrag_id: str, heute: date, referenzierte_ids: set[str]) -> list:
        return [
            k
            for k in self._stammdaten_repository.list_aktive_komponenten(vertrag_id, heute)
            if k.id not in referenzierte_ids
        ]

    def monatslauf_fuer_vertrag(
        self, *, ctx: AuthContext, vertrag: VertragTable, heute: date, akteur: str
    ) -> IndexautomatikLaufTable:
        periode = f"{heute.year:04d}-{heute.month:02d}"
        vorhandener = self._lauf_repository.get_by_periode(vertrag.id, periode)
        if vorhandener is not None:
            return vorhandener

        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)
        self._stammdaten_repository.pruefe_vertrag_nicht_ausgeschlossen(vertrag.id)

        profil = self._rechtsprofil_service.aktives_gueltiges_profil(vertrag.id, heute=heute)
        if profil is None:
            return self._anlegen_lauf(
                vertrag.id, periode, status="BLOCKIERT",
                gruende=["Kein gültiges, freigegebenes Rechtsprofil (fehlt, nie freigegeben, oder seit Freigabe entwertet)."],
            )

        if profil.ist_hauptmiete is not True:
            return self._anlegen_lauf(
                vertrag.id, periode, status="BLOCKIERT", profil=profil,
                gruende=["Hauptmiete nicht bestätigt (Untermiete/ungeklärt) - keine automatische Rechtsannahme zur Anwendbarkeit."],
            )
        if profil.rechtsordnung == Rechtsordnung.UNGEKLAERT.value:
            return self._anlegen_lauf(
                vertrag.id, periode, status="BLOCKIERT", profil=profil, gruende=["Rechtsordnung UNGEKLAERT."]
            )

        ist_wohnungsrechner = profil.rechtsordnung in _WOHNUNGSRECHNER_RECHTSORDNUNGEN and profil.ist_wohnungsnutzung is True

        try:
            if ist_wohnungsrechner:
                return self._monatslauf_mieweg(ctx, vertrag, profil, heute, periode, akteur)
            if profil.vertragsklausel_id is not None:
                return self._monatslauf_klausel(ctx, vertrag, profil, heute, periode, akteur)
            return self._anlegen_lauf(
                vertrag.id, periode, status="BLOCKIERT", profil=profil,
                gruende=[
                    "Kein MieWeG-Wohnungsrechner-Fall (Geschäftsraum/WGG/Gewerbe/unklares Profil) und keine "
                    "geprüfte Vertragsklausel hinterlegt - nur manuelle Prüfung, keine automatische Berechnung."
                ],
            )
        except _BEHANDELBARE_FEHLER as exc:
            return self._anlegen_lauf(vertrag.id, periode, status="BLOCKIERT", profil=profil, gruende=[str(exc)])

    def _monatslauf_mieweg(
        self, ctx: AuthContext, vertrag: VertragTable, profil: RechtsprofilTable, heute: date, periode: str, akteur: str
    ) -> IndexautomatikLaufTable:
        ziel_jahr = heute.year if heute >= date(heute.year, 4, 1) else heute.year - 1
        if profil.bezugsjahr is None or ziel_jahr <= profil.bezugsjahr:
            return self._anlegen_lauf(vertrag.id, periode, status="TERMIN_NICHT_ERREICHT", profil=profil)

        bestehendes = self._outbox_repository.get_by_ziel(vertrag.id, ziel_jahr)
        if bestehendes is not None:
            return self._anlegen_lauf(
                vertrag.id, periode, status="BEREITS_ERFASST", profil=profil,
                mieweg_vorschau_id=bestehendes.mieweg_vorschau_id, erhoehungsschreiben_id=bestehendes.id,
            )

        aktive_komponenten = {k.id: k for k in self._stammdaten_repository.list_aktive_komponenten(vertrag.id, heute)}
        fehlend = [kid for kid in profil.basis_komponenten_ids if kid not in aktive_komponenten]
        if fehlend:
            return self._anlegen_lauf(
                vertrag.id, periode, status="BLOCKIERT", profil=profil,
                gruende=[f"Referenzierte Komponente(n) {fehlend} zum heutigen Stichtag {heute.isoformat()} nicht mehr aktiv/gültig."],
            )
        referenzierte = [aktive_komponenten[kid] for kid in profil.basis_komponenten_ids]
        aktuell_cent = sum(k.betrag_cent for k in referenzierte)
        referenzierte_ids = set(profil.basis_komponenten_ids)
        unveraendert = self._unveraenderte_komponenten(vertrag.id, heute, referenzierte_ids)

        jahre_benoetigt = set(range(profil.bezugsjahr - 1, ziel_jahr))
        vpi_werte = self._vpi_repository.jahresdurchschnitte_fuer(profil.vpi_reihe, jahre_benoetigt)
        vpi_dict = {
            jahr: VpiWert(wert=str(wert), quelle=f"VpiRepository({profil.vpi_reihe})", datum=heute.isoformat())
            for jahr, wert in vpi_werte.items()
        }

        vertraglich_zulaessig_cent = profil.vertraglich_zulaessiger_betrag_cent
        vertraglicher_beleg = profil.vertraglicher_quellenbeleg
        if profil.vertragsklausel_id is not None:
            klausel = self._index_repository.get_klausel(profil.vertragsklausel_id)
            if klausel is None or klausel.status != "FREIGEGEBEN":
                return self._anlegen_lauf(
                    vertrag.id, periode, status="BLOCKIERT", profil=profil,
                    gruende=["Referenzierte Vertragsklausel ist nicht (mehr) freigegeben."],
                )
            aktueller_vpi = self._vpi_repository.neuester_endgueltiger_monatswert(klausel.basis_reihe, heute)
            if aktueller_vpi is None:
                return self._anlegen_lauf(
                    vertrag.id, periode, status="BLOCKIERT", profil=profil,
                    gruende=[f"Kein amtlicher, endgültiger VPI-Monatswert für Reihe {klausel.basis_reihe} verfügbar."],
                )
            vertraglich_zulaessig_cent = berechne_vertragliche_spur_cent(
                klausel=klausel, aktueller_vpi_wert=aktueller_vpi, basis_betrag_cent=aktuell_cent
            )
            vertraglicher_beleg = (
                f"Automatisch aus IndexKlausel {klausel.id} v{klausel.version} berechnet, "
                f"VPI {klausel.basis_reihe}={aktueller_vpi} zum {heute.isoformat()}"
            )

        vorschau = self._mieweg_service.vorschau_erstellen(
            ctx=ctx,
            vertrag=vertrag,
            rechtsordnung=profil.rechtsordnung,
            ist_wohnungsnutzung=profil.ist_wohnungsnutzung,
            mrg_zinsbeschraenkung=profil.mrg_zinsbeschraenkung,
            ist_altvertrag=profil.ist_altvertrag,
            bezugsjahr=profil.bezugsjahr,
            bezugsmonat=profil.bezugsmonat,
            letzte_basis_war_jahresdurchschnitt=profil.letzte_basis_war_jahresdurchschnitt,
            ziel_bewertungsjahr=ziel_jahr,
            basis_betrag_cent=aktuell_cent,
            basis_komponenten_ids=list(profil.basis_komponenten_ids),
            vpi_jahresdurchschnitte=vpi_dict,
            vertraglich_zulaessiger_betrag_cent=vertraglich_zulaessig_cent,
            vertraglicher_quellenbeleg=vertraglicher_beleg,
            vertraglicher_fruehestmoeglicher_termin=profil.vertraglicher_fruehestmoeglicher_termin,
            aktuell_verrechneter_betrag_cent=aktuell_cent,
            aktuell_verrechnet_quellenbeleg=(
                f"Automatischer Lauf: Stammdaten-Komponentenstand "
                f"({', '.join(sorted(profil.basis_komponenten_ids))}) zum {heute.isoformat()}"
            ),
            aktuell_verrechnet_stichtag=heute,
            zustellnachweis_referenz=None,
            kommentar=f"Automatischer Indexautomatik-Lauf, Periode {periode}.",
            akteur=akteur,
        )
        ergebnis = json.loads(vorschau.ergebnis_json)
        if ergebnis["blockiert_grund"] is not None:
            return self._anlegen_lauf(
                vertrag.id, periode, status="BLOCKIERT", profil=profil, mieweg_vorschau_id=vorschau.id,
                gruende=[ergebnis["blockiert_grund"]],
            )
        if ergebnis["massgeblicher_hoechstbetrag_cent"] is None or ergebnis["fruehester_termin_gesamt"] is None:
            return self._anlegen_lauf(
                vertrag.id, periode, status="BLOCKIERT", profil=profil, mieweg_vorschau_id=vorschau.id,
                gruende=list(ergebnis["offene_nachweise"]) or ["Berechnung unvollständig."],
            )

        differenz_cent = ergebnis["rechnerische_differenz_cent"]
        if not differenz_cent:
            return self._anlegen_lauf(
                vertrag.id, periode, status="KEIN_ERHOEHUNGSBEDARF", profil=profil, mieweg_vorschau_id=vorschau.id
            )

        schreiben = self._outbox_service.erstellen_aus_mieweg(
            ctx=ctx,
            vertrag=vertrag,
            profil=profil,
            vorschau=vorschau,
            ziel_bewertungsjahr=ziel_jahr,
            massgeblicher_termin=date.fromisoformat(ergebnis["fruehester_termin_gesamt"]),
            erhoehung_cent=differenz_cent,
            aktuell_verrechnet_cent=aktuell_cent,
            referenzierte_komponenten=referenzierte,
            unveraenderte_komponenten=unveraendert,
            akteur=akteur,
        )
        return self._anlegen_lauf(
            vertrag.id, periode, status="ERHOEHUNG_ERZEUGT", profil=profil, mieweg_vorschau_id=vorschau.id,
            erhoehungsschreiben_id=schreiben.id,
        )

    def _monatslauf_klausel(
        self, ctx: AuthContext, vertrag: VertragTable, profil: RechtsprofilTable, heute: date, periode: str, akteur: str
    ) -> IndexautomatikLaufTable:
        klausel = self._index_repository.get_klausel(profil.vertragsklausel_id)
        if klausel is None or klausel.status != "FREIGEGEBEN":
            return self._anlegen_lauf(
                vertrag.id, periode, status="BLOCKIERT", profil=profil,
                gruende=["Referenzierte Vertragsklausel ist nicht (mehr) freigegeben."],
            )
        aktueller_vpi = self._vpi_repository.neuester_endgueltiger_monatswert(klausel.basis_reihe, heute)
        if aktueller_vpi is None:
            return self._anlegen_lauf(
                vertrag.id, periode, status="BLOCKIERT", profil=profil,
                gruende=[f"Kein amtlicher, endgültiger VPI-Monatswert für Reihe {klausel.basis_reihe} verfügbar."],
            )

        vorschlag = self._index_service.berechne_vorschlag(
            ctx=ctx,
            vertrag_id=vertrag.id,
            stichtag=heute,
            neuer_wert=aktueller_vpi,
            quelle_referenz=(
                f"Automatischer Indexautomatik-Lauf, Periode {periode}, VPI {klausel.basis_reihe}={aktueller_vpi}"
            ),
        )
        if vorschlag.erhoehung_cent <= 0:
            return self._anlegen_lauf(vertrag.id, periode, status="KEIN_ERHOEHUNGSBEDARF", profil=profil)

        aktive_komponenten = {k.id: k for k in self._stammdaten_repository.list_aktive_komponenten(vertrag.id, heute)}
        referenzierte_ids = set(profil.basis_komponenten_ids) & set(aktive_komponenten)
        referenzierte = [aktive_komponenten[kid] for kid in referenzierte_ids]
        unveraendert = self._unveraenderte_komponenten(vertrag.id, heute, referenzierte_ids)

        schreiben = self._outbox_service.erstellen_aus_index_anpassung(
            ctx=ctx,
            vertrag=vertrag,
            profil=profil,
            index_anpassung_id=vorschlag.id,
            massgeblicher_termin=heute,
            erhoehung_cent=vorschlag.erhoehung_cent,
            referenzierte_komponenten=referenzierte,
            unveraenderte_komponenten=unveraendert,
            akteur=akteur,
        )
        return self._anlegen_lauf(vertrag.id, periode, status="ERHOEHUNG_ERZEUGT", profil=profil, erhoehungsschreiben_id=schreiben.id)

    def monatslauf_alle(self, *, ctx: AuthContext, heute: date, akteur: str) -> list[IndexautomatikLaufTable]:
        ergebnisse: list[IndexautomatikLaufTable] = []
        periode = f"{heute.year:04d}-{heute.month:02d}"
        for vertrag in self._stammdaten_repository.list_alle_vertraege():
            try:
                self._stammdaten_repository.pruefe_vertrag_nicht_ausgeschlossen(vertrag.id)
            except ObjektAusgeschlossenError:
                continue
            if vertrag.gueltig_bis is not None and vertrag.gueltig_bis < heute:
                # Abgelaufener Vertrag: keine künftige Erhöhung mehr sinnvoll,
                # kein Indexautomatik-Fall wird angelegt.
                continue
            try:
                ergebnisse.append(self.monatslauf_fuer_vertrag(ctx=ctx, vertrag=vertrag, heute=heute, akteur=akteur))
            except Exception as exc:  # ein fehlerhafter Vertrag darf den Batch nicht abbrechen
                ergebnisse.append(
                    self._anlegen_lauf(
                        vertrag.id, periode, status="BLOCKIERT",
                        gruende=[f"Unerwarteter Fehler im Automatiklauf: {exc}"],
                    )
                )
        return ergebnisse
