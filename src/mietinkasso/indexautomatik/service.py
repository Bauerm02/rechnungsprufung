"""Monatliche Indexautomatik (Auftrag 13.09., HV-20260913-INDEXAUTOMATIK)
- idempotent je (Vertrag, Kalendermonat), reine Orchestrierung über
bereits abgenommene Rechner: `mieweg_vorschau_service.vorschau_erstellen`
für den geprüften MieWeG-Wohnungsrechner-Fall (MRG-Voll-/Teilanwendung
UND bestätigte Wohnungsnutzung - Haupt- ODER geprüfte Untermiete, siehe
unten), `index/service.py::IndexService.berechne_vorschlag` (über eine
referenzierte, versionierte `IndexKlauselTable`) für JEDEN anderen Fall
mit einer geprüften Vertragsklausel (insbesondere Geschäftsraum) - KEIN
paralleler Rohrechner, KEINE pauschale Rechtsklassifikation.

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

    def _unveraenderte_komponenten(self, vertrag_id: str, heute: date, referenzierte_ids: set[str]) -> list:
        return [
            k
            for k in self._stammdaten_repository.list_aktive_komponenten(vertrag_id, heute)
            if k.id not in referenzierte_ids
        ]

    def monatslauf_fuer_vertrag(
        self, *, ctx: AuthContext, vertrag: VertragTable, heute: date, akteur: str
    ) -> IndexautomatikLaufTable:
        # Unabhängiger Review (994e786-Folgereview): Auth MUSS vor jedem
        # Rückgabepfad geprüft werden, auch dem "es gibt schon eine
        # Zeile"-Kurzschluss - sonst könnte ein Aufrufer ohne Zugriff auf
        # diese Gesellschaft trotzdem eine bereits existierende
        # IndexautomatikLaufTable-Zeile für einen fremden Vertrag lesen.
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)
        self._stammdaten_repository.pruefe_vertrag_nicht_ausgeschlossen(vertrag.id)

        periode = f"{heute.year:04d}-{heute.month:02d}"
        claim = self._lauf_repository.claim_periode(vertrag.id, periode)
        if claim is None:
            bestehender = self._lauf_repository.get_by_periode(vertrag.id, periode)
            if bestehender is not None:
                return bestehender
            raise RuntimeError(
                f"Indexautomatik-Lauf für Vertrag {vertrag.id}, Periode {periode} wird gerade von einem "
                "anderen Worker verarbeitet."
            )

        try:
            return self._verarbeiten(ctx, vertrag, heute, periode, akteur, claim.id)
        except _BEHANDELBARE_FEHLER as exc:
            return self._lauf_repository.abschliessen(claim.id, status="BLOCKIERT", blockiert_gruende=[str(exc)])

    def _verarbeiten(
        self, ctx: AuthContext, vertrag: VertragTable, heute: date, periode: str, akteur: str, claim_id: int
    ) -> IndexautomatikLaufTable:
        profil = self._rechtsprofil_service.aktives_gueltiges_profil(vertrag.id, heute=heute)
        if profil is None:
            return self._lauf_repository.abschliessen(
                claim_id, status="BLOCKIERT",
                blockiert_gruende=["Kein gültiges, freigegebenes Rechtsprofil (fehlt, nie freigegeben, oder seit Freigabe entwertet)."],
            )

        # Dreiwertig (siehe RechtsprofilTable-Docstring): NUR `None`
        # (ungeklärt) sperrt - `False` ist eine GEPRÜFTE, bestätigte
        # Untermiete, die MieWeG ausdrücklich mit abdeckt (Modellreview
        # 13.09.: "ist_hauptmiete is not True sperrt pauschal UNTERMIETEN").
        if profil.ist_hauptmiete is None:
            return self._lauf_repository.abschliessen(
                claim_id, status="BLOCKIERT", rechtsprofil_id=profil.id, rechtsprofil_version=profil.version,
                blockiert_gruende=["Haupt-/Untermiete nicht geprüft (ungeklärt) - keine automatische Rechtsannahme."],
            )
        if profil.rechtsordnung == Rechtsordnung.UNGEKLAERT.value:
            return self._lauf_repository.abschliessen(
                claim_id, status="BLOCKIERT", rechtsprofil_id=profil.id, rechtsprofil_version=profil.version,
                blockiert_gruende=["Rechtsordnung UNGEKLAERT."],
            )

        ist_wohnungsrechner = profil.rechtsordnung in _WOHNUNGSRECHNER_RECHTSORDNUNGEN and profil.ist_wohnungsnutzung is True

        if ist_wohnungsrechner:
            return self._monatslauf_mieweg(ctx, vertrag, profil, heute, periode, akteur, claim_id)
        if profil.vertragsklausel_id is not None:
            return self._monatslauf_klausel(ctx, vertrag, profil, heute, periode, akteur, claim_id)
        return self._lauf_repository.abschliessen(
            claim_id, status="BLOCKIERT", rechtsprofil_id=profil.id, rechtsprofil_version=profil.version,
            blockiert_gruende=[
                "Kein MieWeG-Wohnungsrechner-Fall (Geschäftsraum/WGG/Gewerbe/unklares Profil) und keine "
                "geprüfte Vertragsklausel hinterlegt - nur manuelle Prüfung, keine automatische Berechnung."
            ],
        )

    def _pruefe_und_kappe_mietzinsobergrenze(
        self, profil: RechtsprofilTable, massgeblich_cent: int, heute: date
    ) -> tuple[int | None, str | None]:
        """Die Mietzinsobergrenze wirkt IMMER, wenn sie erfasst ist -
        unabhängig davon, ob `foerderbindung` gesetzt ist (unabhängiger
        Review, fd8c2b2-Folgereview: "Die harte Mietzinsobergrenze gilt
        bei MRG-Voll unabhängig davon, ob foerderbindung gesetzt ist;
        nicht nur als Förderobergrenze behandeln"). Liefert
        `(gekappter_betrag, blockierender_grund)` - genau einer der
        beiden ist `None`."""

        if profil.mietzinsobergrenze_cent is None:
            return massgeblich_cent, None
        if profil.mietzinsobergrenze_gueltig_bis is not None and heute > profil.mietzinsobergrenze_gueltig_bis:
            return None, (
                f"Mietzinsobergrenze-Beleg ist seit {profil.mietzinsobergrenze_gueltig_bis.isoformat()} "
                "abgelaufen - keine fiktiv unbegrenzte Weitergeltung."
            )
        return min(massgeblich_cent, profil.mietzinsobergrenze_cent), None

    def _monatslauf_mieweg(
        self, ctx: AuthContext, vertrag: VertragTable, profil: RechtsprofilTable, heute: date, periode: str,
        akteur: str, claim_id: int,
    ) -> IndexautomatikLaufTable:
        def _abschliessen(status: str, gruende: list[str] | None = None, **felder) -> IndexautomatikLaufTable:
            return self._lauf_repository.abschliessen(
                claim_id, status=status, blockiert_gruende=gruende or [], rechtsprofil_id=profil.id,
                rechtsprofil_version=profil.version, **felder,
            )

        ziel_jahr = heute.year if heute >= date(heute.year, 4, 1) else heute.year - 1
        if profil.bezugsjahr is None or ziel_jahr <= profil.bezugsjahr:
            return _abschliessen("TERMIN_NICHT_ERREICHT")

        bestehendes = self._outbox_repository.get_by_ziel(vertrag.id, ziel_jahr)
        bestehende_id: int | None = None
        if bestehendes is not None:
            if bestehendes.status != "BLOCKIERT":
                # Bereits GESENDET/ZUGANG_BESTAETIGT/AUSGEFUEHRT/UNKLAR/
                # BEREIT (in Versand) - unveränderlich, kein Retry.
                return _abschliessen(
                    "BEREITS_ERFASST", mieweg_vorschau_id=bestehendes.mieweg_vorschau_id,
                    erhoehungsschreiben_id=bestehendes.id,
                )
            # Ein noch NICHT versendetes, blockiertes Schreiben darf nach
            # einer behobenen Quelle erneut versucht werden - der ganze
            # April-Zyklus wird sonst dauerhaft "verschluckt" (unabhängiger
            # Review, fd8c2b2-Folgereview).
            bestehende_id = bestehendes.id

        aktive_komponenten = {k.id: k for k in self._stammdaten_repository.list_aktive_komponenten(vertrag.id, heute)}
        fehlend = [kid for kid in profil.basis_komponenten_ids if kid not in aktive_komponenten]
        if fehlend:
            return _abschliessen(
                "BLOCKIERT",
                [f"Referenzierte Komponente(n) {fehlend} zum heutigen Stichtag {heute.isoformat()} nicht mehr aktiv/gültig."],
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
                return _abschliessen("BLOCKIERT", ["Referenzierte Vertragsklausel ist nicht (mehr) freigegeben."])
            aktueller_vpi = self._vpi_repository.neuester_endgueltiger_monatswert(klausel.basis_reihe, heute)
            if aktueller_vpi is None:
                return _abschliessen(
                    "BLOCKIERT", [f"Kein amtlicher, endgültiger VPI-Monatswert für Reihe {klausel.basis_reihe} verfügbar."]
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
            historische_basis_belege=profil.historische_basis_belege,
        )
        ergebnis = json.loads(vorschau.ergebnis_json)
        if ergebnis["blockiert_grund"] is not None:
            return _abschliessen("BLOCKIERT", [ergebnis["blockiert_grund"]], mieweg_vorschau_id=vorschau.id)
        if ergebnis["massgeblicher_hoechstbetrag_cent"] is None or ergebnis["fruehester_termin_gesamt"] is None:
            return _abschliessen(
                "BLOCKIERT", list(ergebnis["offene_nachweise"]) or ["Berechnung unvollständig."],
                mieweg_vorschau_id=vorschau.id,
            )

        massgeblich_cent, obergrenze_grund = self._pruefe_und_kappe_mietzinsobergrenze(
            profil, ergebnis["massgeblicher_hoechstbetrag_cent"], heute
        )
        if obergrenze_grund is not None:
            return _abschliessen("BLOCKIERT", [obergrenze_grund], mieweg_vorschau_id=vorschau.id)

        differenz_cent = max(0, massgeblich_cent - aktuell_cent)
        if not differenz_cent:
            return _abschliessen("KEIN_ERHOEHUNGSBEDARF", mieweg_vorschau_id=vorschau.id)

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
            bestehende_id=bestehende_id,
        )
        return _abschliessen("ERHOEHUNG_ERZEUGT", mieweg_vorschau_id=vorschau.id, erhoehungsschreiben_id=schreiben.id)

    def _monatslauf_klausel(
        self, ctx: AuthContext, vertrag: VertragTable, profil: RechtsprofilTable, heute: date, periode: str,
        akteur: str, claim_id: int,
    ) -> IndexautomatikLaufTable:
        def _abschliessen(status: str, gruende: list[str] | None = None, **felder) -> IndexautomatikLaufTable:
            return self._lauf_repository.abschliessen(
                claim_id, status=status, blockiert_gruende=gruende or [], rechtsprofil_id=profil.id,
                rechtsprofil_version=profil.version, **felder,
            )

        klausel = self._index_repository.get_klausel(profil.vertragsklausel_id)
        if klausel is None or klausel.status != "FREIGEGEBEN":
            return _abschliessen("BLOCKIERT", ["Referenzierte Vertragsklausel ist nicht (mehr) freigegeben."])
        aktueller_vpi = self._vpi_repository.neuester_endgueltiger_monatswert(klausel.basis_reihe, heute)
        if aktueller_vpi is None:
            return _abschliessen(
                "BLOCKIERT", [f"Kein amtlicher, endgültiger VPI-Monatswert für Reihe {klausel.basis_reihe} verfügbar."]
            )

        # Idempotenz an die FACHLICHE Anpassung (identischer Vergleich:
        # gleiche Klausel-Basis, gleicher aktueller VPI-Wert) binden,
        # nicht allein an eine neue technische index_anpassung_id -
        # solange sich weder Basis noch amtlicher Wert geändert haben,
        # erzeugt ein Folgemonat KEINEN zweiten, inhaltlich identischen
        # Vorschlag (unabhängiger Review, fd8c2b2-Folgereview).
        letzte_anpassung = self._index_repository.letzte_anpassung(vertrag.id)
        if (
            letzte_anpassung is not None
            and letzte_anpassung.index_klausel_id == klausel.id
            and letzte_anpassung.alter_wert == klausel.basis_wert
            and letzte_anpassung.neuer_wert == aktueller_vpi
            and letzte_anpassung.status != "VERWORFEN"
        ):
            return _abschliessen(
                "BLOCKIERT",
                [
                    f"Bereits erfasster Vorschlag (IndexAnpassung {letzte_anpassung.id}) für denselben "
                    f"Vergleich (Basis {klausel.basis_wert}, aktueller VPI-Wert {aktueller_vpi}) - kein "
                    "zweiter, inhaltlich identischer Vorschlag."
                ],
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
            return _abschliessen("KEIN_ERHOEHUNGSBEDARF")

        # Kein unabhängig verifizierter automatischer Wirksamkeitstermin
        # für diesen Pfad (Geschäftsraum/generische Klausel) - "kein
        # heutiges Datum als frei erfundenen Erhöhungstermin" (Modellreview
        # 13.09.). Der rechnerische Vorschlag liegt geprüft vor
        # (IndexAnpassungTable, bereits abgenommener Rechner), aber KEIN
        # automatisches Erhöhungsschreiben/keine Outbox-Zeile wird daraus
        # erzeugt - siehe OFFENE_PUNKTE.md.
        return _abschliessen(
            "BLOCKIERT",
            [
                f"Rechnerischer Vorschlag liegt vor (IndexAnpassung {vorschlag.id}, Erhöhung "
                f"{vorschlag.erhoehung_cent} Cent) - ein automatischer Wirksamkeitstermin für den "
                "Geschäftsraum-/Klausel-Pfad wird noch nicht unterstützt. Bitte Termin manuell prüfen "
                "und bestätigen (kein erfundenes Datum)."
            ],
        )

    def monatslauf_alle(self, *, ctx: AuthContext, heute: date, akteur: str) -> list[IndexautomatikLaufTable]:
        """Unabhängiger Review (b31-Folgereview, synthetisch mit "1
        fremde Rückgabe + fremde Laufzeile geschrieben" reproduziert):
        VOR jeder Verarbeitung wird der Gesellschaftsscope des
        Aufrufers geprüft - ein Vertrag außerhalb von
        `ctx.gesellschaft_ids` wird komplett übersprungen (keine Zeile
        gelesen, keine geschrieben, kein Rückgabewert). Zusätzlich fängt
        der Batch nur noch die in `_BEHANDELBARE_FEHLER` gelisteten,
        fachlich erwarteten Fehler ab - ein `CrossTenantError` (z. B.
        durch eine Race zwischen Scope-Filter und Verarbeitung) darf NIE
        in einer Fachlauf-Zeile landen, sondern muss den Aufrufer
        erreichen."""

        ergebnisse: list[IndexautomatikLaufTable] = []
        periode = f"{heute.year:04d}-{heute.month:02d}"
        for vertrag in self._stammdaten_repository.list_alle_vertraege():
            if not ctx.has_zugriff(vertrag.gesellschaft_id):
                continue
            try:
                self._stammdaten_repository.pruefe_vertrag_nicht_ausgeschlossen(vertrag.id)
            except ObjektAusgeschlossenError:
                continue
            if vertrag.gueltig_von > heute:
                continue  # Vertrag hat noch nicht begonnen
            if vertrag.gueltig_bis is not None and vertrag.gueltig_bis < heute:
                # Abgelaufener Vertrag: keine künftige Erhöhung mehr sinnvoll,
                # kein Indexautomatik-Fall wird angelegt.
                continue
            try:
                ergebnisse.append(self.monatslauf_fuer_vertrag(ctx=ctx, vertrag=vertrag, heute=heute, akteur=akteur))
            except _BEHANDELBARE_FEHLER as exc:  # ein fachlich erwarteter Fehler darf den Batch nicht abbrechen
                bestehender = self._lauf_repository.get_by_periode(vertrag.id, periode)
                if bestehender is None:
                    claim = self._lauf_repository.claim_periode(vertrag.id, periode)
                    if claim is not None:
                        bestehender = self._lauf_repository.abschliessen(
                            claim.id, status="BLOCKIERT", blockiert_gruende=[f"Unerwarteter Fehler im Automatiklauf: {exc}"]
                        )
                if bestehender is not None:
                    ergebnisse.append(bestehender)
        return ergebnisse
