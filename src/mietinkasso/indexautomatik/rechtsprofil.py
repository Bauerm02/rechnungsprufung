"""Versionierte Eigentümer-Freigabe des Rechtsprofils je Vertrag
(Auftrag 13.09., HV-20260913-INDEXAUTOMATIK) - liefert der monatlichen
Indexautomatik alle Eingaben für den jeweils passenden Rechner
(`mieweg_vorschau_service.vorschau_erstellen` für den MieWeG-
Wohnungsrechner-Pfad, `index/service.py`/`IndexKlauselTable` für den
Geschäftsraum-/generischen-Klausel-Pfad), OHNE dass jeden Monat erneut
manuell bestätigt werden muss ("Eigentümer-Rechtsprofilfreigabe einmal
versioniert, danach keine unnötige monatliche Einzelgenehmigung
unveränderter Standardfälle").

`quelle_hash` bindet eine Freigabe an den Stand von Vertrag, jeder
referenzierten Komponente UND einer ggf. referenzierten
`IndexKlauselTable` zum Freigabezeitpunkt - ändert sich einer dieser
Werte danach, erkennt `ist_noch_gueltig` das bei der nächsten Prüfung
und die Freigabe gilt automatisch als entwertet ("Änderungen an Quelle/
Basis/Vertrag/Profil entwerten alte Freigabe") - ohne dass irgendjemand
das manuell nachpflegen müsste. Eine `mietzinsobergrenze_gueltig_bis`,
die bereits VERSTRICHEN ist, macht eine Freigabe ebenfalls ungültig
(zeitbasierte Prüfung, unabhängig vom Hash)."""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from mietinkasso.auth.service import AuthContext, require_gesellschaft_access, require_schreibrecht
from mietinkasso.domain.enums import Rechtsordnung
from mietinkasso.domain.exceptions import QuellenbelegFehltError
from mietinkasso.index.repository import IndexRepository
from mietinkasso.indexautomatik.repository import RechtsprofilRepository
from mietinkasso.infrastructure.db.tables import RechtsprofilTable, VertragTable
from mietinkasso.mieweg_vorschau.service import _NIE_INDEXIERBARE_ARTEN as _MIEWEG_NIE_INDEXIERBARE_ARTEN
from mietinkasso.op.service import compute_content_hash
from mietinkasso.stammdaten.repository import StammdatenRepository

#: Deckt sich bewusst mit `mieweg_vorschau/service.py::
#: _WOHNUNGSRECHNER_RECHTSORDNUNGEN` (eigene, lokale Kopie - siehe
#: dortige Begründung zur Modultrennung, AGENTS.md).
_WOHNUNGSRECHNER_RECHTSORDNUNGEN = {
    Rechtsordnung.OESTERREICH_MRG_VOLL.value,
    Rechtsordnung.OESTERREICH_MRG_TEIL.value,
}


class RechtsprofilService:
    def __init__(
        self,
        repository: RechtsprofilRepository,
        stammdaten_repository: StammdatenRepository,
        index_repository: IndexRepository,
    ):
        self._repository = repository
        self._stammdaten_repository = stammdaten_repository
        self._index_repository = index_repository

    def _vertrag_oder_fehler(self, vertrag_id: str) -> VertragTable:
        vertrag = self._stammdaten_repository.get_vertrag(vertrag_id)
        if vertrag is None:
            raise ValueError(f"Unbekannter Vertrag {vertrag_id}")
        return vertrag

    def entwurf_anlegen(
        self,
        *,
        ctx: AuthContext,
        vertrag_id: str,
        rechtsordnung: str,
        ist_wohnungsnutzung: bool | None,
        mrg_zinsbeschraenkung: bool,
        ist_altvertrag: bool,
        ist_hauptmiete: bool | None,
        foerderbindung: bool,
        mietzinsobergrenze_cent: int | None,
        mrg_zinsbeschraenkung_geprueft: bool = False,
        foerderbindung_geprueft: bool = False,
        mietzinsobergrenze_quellenbeleg: str | None,
        mietzinsobergrenze_gueltig_bis: date | None,
        bezugsjahr: int | None,
        bezugsmonat: int | None,
        letzte_basis_war_jahresdurchschnitt: bool,
        basis_komponenten_ids: list[str],
        vpi_reihe: str = "VPI20C18",
        vertraglich_zulaessiger_betrag_cent: int | None = None,
        vertraglicher_quellenbeleg: str | None = None,
        vertraglicher_fruehestmoeglicher_termin: date | None = None,
        vertragsklausel_id: int | None = None,
        vertrag_beleg_referenz: str,
        klausel_referenz: str | None,
        erstellt_von: str,
        frist_tage_zugang_bis_wirksamkeit: int | None = None,
        frist_quellenbeleg: str | None = None,
    ) -> RechtsprofilTable:
        vertrag = self._vertrag_oder_fehler(vertrag_id)
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)
        self._stammdaten_repository.pruefe_vertrag_nicht_ausgeschlossen(vertrag_id)

        # UI-Endprüfung (6317f96): "MieWeG-Gesetzesspur muss VPI20C18
        # verwenden (UI bietet derzeit andere Reihen, service nimmt
        # profil.vpi_reihe); Vertragsklausel darf getrennt ihre eigene
        # Reihe haben." `vpi_reihe` wird AUSSCHLIESSLICH vom
        # MieWeG-Wohnungsrechner-Pfad gelesen (siehe
        # `indexautomatik/service.py::_monatslauf_mieweg`) - der
        # Klausel-Pfad hat mit `IndexKlauselTable.basis_reihe` eine
        # eigene, unabhängige Reihe. VPI20C18 ist die aktuell amtlich
        # verlautbarte Reihe für die gesetzliche MieWeG-Berechnung.
        if vpi_reihe != "VPI20C18":
            raise ValueError(
                f"vpi_reihe '{vpi_reihe}' ist für die gesetzliche MieWeG-Spur nicht zulässig - nur "
                "'VPI20C18' (aktuell amtlich verlautbarte Reihe). Eine abweichende vertragliche Reihe "
                "gehört als eigenständige, versionierte IndexKlausel (vertragsklausel_id) erfasst."
            )
        if not (vertrag_beleg_referenz or "").strip():
            raise QuellenbelegFehltError(
                "Ein Rechtsprofil ohne Vertragsbeleg-Referenz wird abgelehnt - keine beleglose "
                "Klassifizierung (wie jede andere Rechtsklassifizierung in diesem Repository)."
            )
        if foerderbindung and (
            mietzinsobergrenze_cent is None or not (mietzinsobergrenze_quellenbeleg or "").strip()
        ):
            raise QuellenbelegFehltError(
                "Förderbindung ist gesetzt, aber Mietzinsobergrenze/Quellenbeleg fehlen - eine "
                "gesetzliche Obergrenze ist eine harte Bedingung: fehlend blockiert, wird nie "
                "fiktiv als unbegrenzt behandelt."
            )
        if mietzinsobergrenze_cent is not None and mietzinsobergrenze_cent < 0:
            raise ValueError(f"mietzinsobergrenze_cent ({mietzinsobergrenze_cent}) ist negativ.")
        if frist_tage_zugang_bis_wirksamkeit is not None and not (frist_quellenbeleg or "").strip():
            raise QuellenbelegFehltError(
                "Ein konfiguriertes Fristenprofil (frist_tage_zugang_bis_wirksamkeit) ohne "
                "frist_quellenbeleg wird abgelehnt - keine unbelegte Frist wird an einen Mieter "
                "kommuniziert."
            )
        if frist_tage_zugang_bis_wirksamkeit is not None and frist_tage_zugang_bis_wirksamkeit < 0:
            raise ValueError(f"frist_tage_zugang_bis_wirksamkeit ({frist_tage_zugang_bis_wirksamkeit}) ist negativ.")
        if vertraglich_zulaessiger_betrag_cent is not None and vertragsklausel_id is not None:
            raise ValueError(
                "vertraglich_zulaessiger_betrag_cent (statischer Zielbetrag) und vertragsklausel_id "
                "(dynamisch aus einer Klausel berechnete Vertragsspur) schließen sich aus - ein fixer "
                "manuell eingetragener Betrag ist keine dauerhafte Vertragsformel, wenn es tatsächlich "
                "eine wiederkehrende Klausel gibt; pro Rechtsprofil gilt GENAU eine der beiden Spuren."
            )
        if vertragsklausel_id is not None:
            klausel = self._index_repository.get_klausel(vertragsklausel_id)
            if klausel is None or klausel.vertrag_id != vertrag_id:
                raise ValueError(f"IndexKlausel {vertragsklausel_id} gehört nicht zu Vertrag {vertrag_id}.")

        version = self._repository.naechste_version(vertrag_id)
        row = RechtsprofilTable(
            vertrag_id=vertrag_id,
            version=version,
            rechtsordnung=rechtsordnung,
            ist_wohnungsnutzung=ist_wohnungsnutzung,
            mrg_zinsbeschraenkung=mrg_zinsbeschraenkung,
            mrg_zinsbeschraenkung_geprueft=mrg_zinsbeschraenkung_geprueft,
            ist_altvertrag=ist_altvertrag,
            ist_hauptmiete=ist_hauptmiete,
            foerderbindung=foerderbindung,
            foerderbindung_geprueft=foerderbindung_geprueft,
            mietzinsobergrenze_cent=mietzinsobergrenze_cent,
            mietzinsobergrenze_quellenbeleg=mietzinsobergrenze_quellenbeleg,
            mietzinsobergrenze_gueltig_bis=mietzinsobergrenze_gueltig_bis,
            bezugsjahr=bezugsjahr,
            bezugsmonat=bezugsmonat,
            letzte_basis_war_jahresdurchschnitt=letzte_basis_war_jahresdurchschnitt,
            basis_komponenten_ids=list(basis_komponenten_ids),
            vpi_reihe=vpi_reihe,
            vertraglich_zulaessiger_betrag_cent=vertraglich_zulaessiger_betrag_cent,
            vertraglicher_quellenbeleg=vertraglicher_quellenbeleg,
            vertraglicher_fruehestmoeglicher_termin=vertraglicher_fruehestmoeglicher_termin,
            vertragsklausel_id=vertragsklausel_id,
            vertrag_beleg_referenz=vertrag_beleg_referenz,
            klausel_referenz=klausel_referenz,
            frist_tage_zugang_bis_wirksamkeit=frist_tage_zugang_bis_wirksamkeit,
            frist_quellenbeleg=frist_quellenbeleg,
            status="ENTWURF",
            erstellt_von=erstellt_von,
        )
        return self._repository.anlegen(row)

    def _validiere_vollstaendigkeit_fuer_freigabe(self, profil: RechtsprofilTable, vertrag: VertragTable) -> None:
        """Nur eine INHALTLICH für die Automatik ausführbare Kombination
        darf freigegeben werden - ein `ENTWURF` darf dagegen weiterhin
        unvollständig bleiben (reine Prüfliste, siehe
        `indexautomatik/service.py`). Fehlende/vertragsfremde
        Basis-Komponenten werden HIER, spätestens bei der Freigabe,
        abgelehnt statt erst Monate später im automatischen Lauf
        aufzufallen (unabhängiger Review 0d65e2b)."""

        # UI-Endprüfung (6317f96): "Fehlende belegte Mietzinsobergrenze
        # bei MRG-Voll darf nicht als unbeschränkt gelten, mindestens
        # für bestätigte Zinsbeschränkung Pflicht bei Freigabe" - eine
        # bestätigte MRG-Zinsbeschränkung OHNE erfasste Obergrenze würde
        # `_pruefe_und_kappe_mietzinsobergrenze` (service.py) fiktiv als
        # unbegrenzt behandeln, weil die Kappung dort nur greift, wenn
        # `mietzinsobergrenze_cent` überhaupt gesetzt ist.
        if (
            profil.rechtsordnung == Rechtsordnung.OESTERREICH_MRG_VOLL.value
            and profil.mrg_zinsbeschraenkung
            and profil.mietzinsobergrenze_cent is None
        ):
            raise ValueError(
                "MRG-Zinsbeschränkung ist bestätigt, aber keine Mietzinsobergrenze erfasst - eine "
                "bestätigte gesetzliche Zinsbeschränkung wird nie fiktiv als unbegrenzt behandelt. "
                "Bitte Mietzinsobergrenze/Quellenbeleg vor Freigabe erfassen."
            )
        # Auftrag HV-20260913-VERSAND-SOLL, Punkt "Tri-State": `unbekannt`
        # (Default `False` auf dem additiven `*_geprueft`-Flag) darf eine
        # Freigabe NIE stillschweigend passieren lassen - eine Freigabe
        # verlangt eine EXPLIZIT geklärte Angabe (ja ODER nein), kein
        # gerateter Boolean. Ein `ENTWURF` bleibt davon unberührt.
        if not profil.mrg_zinsbeschraenkung_geprueft:
            raise ValueError(
                "MRG-Zinsbeschränkung ist nicht geprüft (unbekannt) - eine Freigabe verlangt eine "
                "geklärte Angabe (ja oder nein), keine automatische Annahme."
            )
        if not profil.foerderbindung_geprueft:
            raise ValueError(
                "Förderbindung ist nicht geprüft (unbekannt) - eine Freigabe verlangt eine geklärte "
                "Angabe (ja oder nein), keine automatische Annahme."
            )
        if not profil.basis_komponenten_ids:
            raise ValueError("Mindestens eine referenzierte Basis-Komponente ist für eine Freigabe Pflicht.")
        if profil.bezugsjahr is None or profil.bezugsmonat is None:
            raise ValueError("Bezugsjahr/-monat (letzte tatsächlich verwendete Indexbasis) sind für eine Freigabe Pflicht.")
        if profil.vertraglich_zulaessiger_betrag_cent is None and profil.vertragsklausel_id is None:
            raise ValueError(
                "Weder ein statischer vertraglich zulässiger Betrag noch eine Vertragsklausel ist erfasst - "
                "ohne eine der beiden Spuren bleibt der massgebliche Höchstbetrag dauerhaft offen."
            )
        referenzdatum = date(profil.bezugsjahr, profil.bezugsmonat, 1)
        for komponente_id in profil.basis_komponenten_ids:
            komponente = self._stammdaten_repository.get_komponente(komponente_id)
            if komponente is None or komponente.vertrag_id != vertrag.id:
                raise ValueError(f"Komponente {komponente_id} gehört nicht zu Vertrag {vertrag.id} (oder existiert nicht).")
            if not komponente.indexierbar:
                raise ValueError(f"Komponente {komponente_id} ist nicht als indexierbar markiert.")
            if komponente.art in _MIEWEG_NIE_INDEXIERBARE_ARTEN:
                raise ValueError(
                    f"Komponente {komponente_id} hat die Art '{komponente.art}' - Betriebs-/Heizkosten("
                    "-Vorauszahlungen) und vergleichbare Aliasarten werden NIE automatisch indexiert."
                )
            # Historisierungs-Kette statt reiner Zeilenprüfung (Codex-Rückmeldung
            # zu 8f499c9): eine Umsetzung schließt die alte Komponentenzeile und
            # legt eine NEUE mit neuem `gueltig_von` (dem Anspruchsmonat) an -
            # dieselbe, ununterbrochen fortbestehende Verpflichtung. Ein reiner
            # `komponente.gueltig_von`-Vergleich würde die bewusst fortgeschriebene
            # MieWeG-Bezugsbasis (siehe `umsetzung_service.py`, "Leerschritt")
            # fälschlich als Rückdatierung ablehnen. Siehe
            # `StammdatenRepository.ursprungs_gueltig_von`-Docstring.
            if self._stammdaten_repository.ursprungs_gueltig_von(komponente) > referenzdatum:
                raise ValueError(
                    f"Komponente {komponente_id} ist erst ab {komponente.gueltig_von.isoformat()} gültig - "
                    f"zum Bezugszeitpunkt {referenzdatum.isoformat()} hat sie noch nicht bestanden."
                )
            if komponente.gueltig_bis is not None and komponente.gueltig_bis < referenzdatum:
                raise ValueError(
                    f"Komponente {komponente_id} war bereits bis {komponente.gueltig_bis.isoformat()} "
                    f"befristet - zum Bezugszeitpunkt {referenzdatum.isoformat()} nicht mehr gültig."
                )

    def _quelle_snapshot(
        self, profil: RechtsprofilTable, vertrag: VertragTable, *, session: Session | None = None
    ) -> dict:
        """`session`: siehe `stammdaten/repository.py::get_komponente` -
        Pflicht für einen Aufrufer, der eine referenzierte Komponente
        INNERHALB derselben, noch nicht committeten Transaktion neu
        angelegt hat (`indexautomatik/umsetzung_service.py::umsetzen`) -
        ohne das würde eine separat geöffnete Session die neue Zeile
        noch nicht sehen und einen abweichenden, für spätere (nach dem
        Commit ganz normal berechnete) Prüfungen falschen Hash
        erzeugen."""

        komponenten_snapshot = []
        for komponente_id in sorted(profil.basis_komponenten_ids):
            komponente = self._stammdaten_repository.get_komponente(komponente_id, session=session)
            komponenten_snapshot.append(
                {
                    "id": komponente_id,
                    "vorhanden": komponente is not None,
                    "betrag_cent": komponente.betrag_cent if komponente else None,
                    "indexierbar": komponente.indexierbar if komponente else None,
                    "art": komponente.art if komponente else None,
                    "ust_satz_promille": komponente.ust_satz_promille if komponente else None,
                    "gueltig_von": komponente.gueltig_von.isoformat() if komponente else None,
                    "gueltig_bis": (
                        komponente.gueltig_bis.isoformat() if komponente and komponente.gueltig_bis else None
                    ),
                }
            )
        klausel_snapshot = None
        if profil.vertragsklausel_id is not None:
            # `session=session` mit derselben Begründung wie oben bei
            # `get_komponente` - Pflicht, wenn `profil.vertragsklausel_id`
            # innerhalb DIESER noch nicht committeten Transaktion neu
            # angelegt wurde (`umsetzung_service.py::umsetzen`, Klausel-
            # Basisfortschreibung) - siehe auch `IndexRepository.
            # get_klausel`-Docstring zum beobachteten StaleDataError bei
            # einer separat geöffneten zweiten Session auf derselben
            # SQLite-In-Memory-Connection.
            klausel = self._index_repository.get_klausel(profil.vertragsklausel_id, session=session)
            if klausel is not None:
                klausel_snapshot = {
                    "id": klausel.id,
                    "status": klausel.status,
                    "basis_reihe": klausel.basis_reihe,
                    "basis_wert": str(klausel.basis_wert),
                    "basis_monat": klausel.basis_monat,
                    "schwelle_prozent": str(klausel.schwelle_prozent),
                    "schwelle_inklusive": klausel.schwelle_inklusive,
                    "daempfung_prozent": str(klausel.daempfung_prozent) if klausel.daempfung_prozent is not None else None,
                    "vertragliche_grenze_prozent": (
                        str(klausel.vertragliche_grenze_prozent) if klausel.vertragliche_grenze_prozent is not None else None
                    ),
                    "indexierbare_komponenten": sorted(klausel.indexierbare_komponenten or []),
                }
        return {
            "vertrag_rechtsordnung": vertrag.rechtsordnung,
            "vertrag_gueltig_von": vertrag.gueltig_von.isoformat(),
            "vertrag_gueltig_bis": vertrag.gueltig_bis.isoformat() if vertrag.gueltig_bis else None,
            "vertrag_faelligkeit_tag": vertrag.faelligkeit_tag,
            "profil_felder": {
                "rechtsordnung": profil.rechtsordnung,
                "ist_wohnungsnutzung": profil.ist_wohnungsnutzung,
                "mrg_zinsbeschraenkung": profil.mrg_zinsbeschraenkung,
                "mrg_zinsbeschraenkung_geprueft": profil.mrg_zinsbeschraenkung_geprueft,
                "ist_altvertrag": profil.ist_altvertrag,
                "ist_hauptmiete": profil.ist_hauptmiete,
                "foerderbindung": profil.foerderbindung,
                "foerderbindung_geprueft": profil.foerderbindung_geprueft,
                "mietzinsobergrenze_cent": profil.mietzinsobergrenze_cent,
                "mietzinsobergrenze_quellenbeleg": profil.mietzinsobergrenze_quellenbeleg,
                "mietzinsobergrenze_gueltig_bis": (
                    profil.mietzinsobergrenze_gueltig_bis.isoformat() if profil.mietzinsobergrenze_gueltig_bis else None
                ),
                "bezugsjahr": profil.bezugsjahr,
                "bezugsmonat": profil.bezugsmonat,
                "letzte_basis_war_jahresdurchschnitt": profil.letzte_basis_war_jahresdurchschnitt,
                "basis_komponenten_ids": sorted(profil.basis_komponenten_ids),
                "vpi_reihe": profil.vpi_reihe,
                "vertraglich_zulaessiger_betrag_cent": profil.vertraglich_zulaessiger_betrag_cent,
                "vertraglicher_quellenbeleg": profil.vertraglicher_quellenbeleg,
                "vertraglicher_fruehestmoeglicher_termin": (
                    profil.vertraglicher_fruehestmoeglicher_termin.isoformat()
                    if profil.vertraglicher_fruehestmoeglicher_termin
                    else None
                ),
                "vertragsklausel_id": profil.vertragsklausel_id,
                "vertrag_beleg_referenz": profil.vertrag_beleg_referenz,
                "klausel_referenz": profil.klausel_referenz,
                "frist_tage_zugang_bis_wirksamkeit": profil.frist_tage_zugang_bis_wirksamkeit,
                "frist_quellenbeleg": profil.frist_quellenbeleg,
            },
            "komponenten": komponenten_snapshot,
            "vertragsklausel": klausel_snapshot,
        }

    def freigeben(self, rechtsprofil_id: int, *, ctx: AuthContext, freigegeben_von: str) -> RechtsprofilTable:
        profil = self._repository.get(rechtsprofil_id)
        if profil is None:
            raise ValueError(f"Unbekanntes Rechtsprofil {rechtsprofil_id}")
        vertrag = self._vertrag_oder_fehler(profil.vertrag_id)
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)
        self._validiere_vollstaendigkeit_fuer_freigabe(profil, vertrag)
        quelle_hash = compute_content_hash(self._quelle_snapshot(profil, vertrag))
        return self._repository.freigeben(rechtsprofil_id, freigegeben_von=freigegeben_von, quelle_hash=quelle_hash)

    def ist_noch_gueltig(self, profil: RechtsprofilTable, *, heute: date | None = None) -> bool:
        """Prüft, ob der bei `freigeben()` festgehaltene `quelle_hash`
        noch zum AKTUELLEN Stand von Vertrag/referenzierten Komponenten/
        einer ggf. referenzierten Klausel passt UND ob eine erfasste
        `mietzinsobergrenze_gueltig_bis` noch nicht verstrichen ist -
        unabhängig vom gespeicherten `status`-Feld (das erst beim
        NÄCHSTEN Lauf nachgezogen wird, siehe `indexautomatik/service.py`)."""

        if profil.status != "FREIGEGEBEN" or not profil.quelle_hash:
            return False
        if (
            profil.mietzinsobergrenze_gueltig_bis is not None
            and heute is not None
            and heute > profil.mietzinsobergrenze_gueltig_bis
        ):
            return False
        vertrag = self._stammdaten_repository.get_vertrag(profil.vertrag_id)
        if vertrag is None:
            return False
        aktueller_hash = compute_content_hash(self._quelle_snapshot(profil, vertrag))
        return aktueller_hash == profil.quelle_hash

    def aktives_gueltiges_profil(self, vertrag_id: str, *, heute: date | None = None) -> RechtsprofilTable | None:
        """Liefert das freigegebene Profil NUR, wenn es auch inhaltlich
        noch gültig ist - eine bereits entwertete, aber noch nicht
        `INVALIDIERT` markierte Zeile wird hier automatisch mit
        `invalidieren()` nachgezogen (kein stiller Weiterbetrieb auf
        veraltetem Stand)."""

        profil = self._repository.freigegebenes_profil(vertrag_id)
        if profil is None:
            return None
        if self.ist_noch_gueltig(profil, heute=heute):
            return profil
        self._repository.invalidieren(profil.id)
        return None

    def liste_fuer_vertrag(self, vertrag_id: str) -> list[RechtsprofilTable]:
        return self._repository.liste_fuer_vertrag(vertrag_id)
