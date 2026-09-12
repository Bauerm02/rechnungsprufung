"""Orchestrierung der MieWeG-2026-Berechnungsvorschau (Auftrag 12.09.,
Paket C) - validiert Eingaben, ruft den deterministischen
`berechnung.py`-Kern auf und persistiert eine versionierte,
unveränderliche `MieWegVorschauTable`-Zeile. AUSDRÜCKLICH keine
Freigabe, keine Vorschreibung, kein Versand, kein KI-Aufruf.

Zwei getrennte Rechenspuren (Nutzerauftrag): die GESETZLICHE
Höchstgrenze wird hier vollständig nach der in `berechnung.py`
implementierten MieWeG-Formel berechnet. Die VERTRAGLICH zulässige
Änderung wird bewusst NICHT aus den VPI-Daten hergeleitet (Vertrags-
klauseln variieren zu stark, um sie generisch nachzubilden, ohne die
individuelle Klausel zu "erraten") - sie ist ein vom Operator explizit
erfasster, mit Quellenbeleg belegter Betrag (die eigentliche
Klauselprüfung bleibt Fachaufgabe, wie schon in
`vertragspruefung/service.py` für die Rechtsordnung selbst). Der
maßgebliche Höchstbetrag ist das Minimum beider Spuren; fehlt eine der
beiden Spuren, bleibt das Ergebnis offen (Prüfbedarf) - es wird nie nur
eine Seite verwendet."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal

from mietinkasso.auth.service import AuthContext, require_gesellschaft_access, require_schreibrecht
from mietinkasso.domain.enums import Rechtsordnung
from mietinkasso.domain.exceptions import QuellenbelegFehltError
from mietinkasso.infrastructure.db.tables import MieWegVorschauTable, VertragTable
from mietinkasso.mieweg_vorschau.berechnung import GesetzlicheHoechstgrenzeErgebnis, berechne_gesetzliche_hoechstgrenze
from mietinkasso.mieweg_vorschau.repository import MieWegVorschauRepository
from mietinkasso.stammdaten.repository import StammdatenRepository

_WOHNUNGSRECHNER_RECHTSORDNUNGEN = {
    Rechtsordnung.OESTERREICH_MRG_VOLL.value,
    Rechtsordnung.OESTERREICH_MRG_TEIL.value,
}
_GUELTIGE_RECHTSORDNUNGEN = {e.value for e in Rechtsordnung}
_ALTVERTRAG_ERSTE_MODELLBEWERTUNG_JAHR = 2026


@dataclass(frozen=True)
class VpiWert:
    wert: str  # Decimal als String (JSON-stabil, exakt)
    quelle: str
    datum: str  # ISO-Datum als String


def _decimal_zu_str(wert: Decimal) -> str:
    return format(wert, "f")


def _jahresschritt_zu_dict(schritt) -> dict:
    return {k: (_decimal_zu_str(v) if isinstance(v, Decimal) else v) for k, v in asdict(schritt).items()}


class MieWegVorschauService:
    def __init__(
        self,
        vorschau_repository: MieWegVorschauRepository,
        stammdaten_repository: StammdatenRepository,
    ):
        self._vorschau_repository = vorschau_repository
        self._stammdaten_repository = stammdaten_repository
        self._session_factory = vorschau_repository.session_factory

    def vorschau_erstellen(
        self,
        *,
        ctx: AuthContext,
        vertrag: VertragTable,
        rechtsordnung: str,
        mrg_zinsbeschraenkung: bool,
        ist_altvertrag: bool,
        bezugsjahr: int | None,
        bezugsmonat: int | None,
        letzte_basis_war_jahresdurchschnitt: bool,
        ziel_bewertungsjahr: int | None,
        basis_betrag_cent: int | None,
        basis_komponenten_ids: list[str],
        vpi_jahresdurchschnitte: dict[int, VpiWert],
        vertraglich_zulaessiger_betrag_cent: int | None,
        vertraglicher_quellenbeleg: str | None,
        vertraglicher_fruehestmoeglicher_termin: date | None,
        zustellnachweis_referenz: str | None,
        kommentar: str | None,
        akteur: str,
    ) -> MieWegVorschauTable:
        require_gesellschaft_access(ctx, vertrag.gesellschaft_id)
        require_schreibrecht(ctx)
        self._stammdaten_repository.pruefe_vertrag_nicht_ausgeschlossen(vertrag.id)

        if rechtsordnung not in _GUELTIGE_RECHTSORDNUNGEN:
            raise ValueError(f"Ungültige Rechtsordnung '{rechtsordnung}'.")
        if mrg_zinsbeschraenkung and rechtsordnung != Rechtsordnung.OESTERREICH_MRG_VOLL.value:
            raise ValueError(
                "Der MRG-Vollanwendungs-Übergangsdeckel (mrg_zinsbeschraenkung) gilt nur für "
                "OESTERREICH_MRG_VOLL, nicht für eine andere Rechtsordnung."
            )
        if ist_altvertrag and ziel_bewertungsjahr is not None and ziel_bewertungsjahr < _ALTVERTRAG_ERSTE_MODELLBEWERTUNG_JAHR:
            raise ValueError(
                f"Für Altverträge ist die erste Modellbewertung frühestens April "
                f"{_ALTVERTRAG_ERSTE_MODELLBEWERTUNG_JAHR} möglich, angefragt wurde {ziel_bewertungsjahr}."
            )

        # Nur explizit vertragsindexierte Komponenten (Fachregel: "z.B.
        # Küche kann ja sein, Garage nicht automatisch"; BK/HK/Versorger
        # nie automatisch) - jede referenzierte Komponente muss zu DIESEM
        # Vertrag gehören und explizit als indexierbar markiert sein.
        for komponente_id in basis_komponenten_ids:
            komponente = self._stammdaten_repository.get_komponente(komponente_id)
            if komponente is None or komponente.vertrag_id != vertrag.id:
                raise ValueError(f"Komponente {komponente_id} gehört nicht zu Vertrag {vertrag.id}.")
            if not komponente.indexierbar:
                raise ValueError(
                    f"Komponente {komponente_id} ist nicht als indexierbar markiert - nur explizit "
                    "vertragsindexierte Komponenten fließen in die Basis ein (BK/HK/Versorger/nicht "
                    "vereinbarte Komponenten werden nie automatisch indexiert)."
                )

        ist_wohnungsrechner_fall = rechtsordnung in _WOHNUNGSRECHNER_RECHTSORDNUNGEN
        offene_nachweise: list[str] = []
        blockiert_grund: str | None = None
        gesetzliches_ergebnis: GesetzlicheHoechstgrenzeErgebnis | None = None

        if rechtsordnung == Rechtsordnung.UNGEKLAERT.value:
            blockiert_grund = (
                "Rechtsprofil UNGEKLAERT - kein Rateversuch, keine ausführbare Anpassung; nur als "
                "Entwurf mit fehlenden Eingaben gespeichert."
            )
        elif not ist_wohnungsrechner_fall:
            blockiert_grund = (
                f"Rechtsordnung {rechtsordnung} ist kein Wohnungsrechner-Fall (nur MRG-Vollanwendung/"
                "-Teilanwendung sind hier abgedeckt) - Gewerbe/WGG/freie Miete/Deutschland werden nicht "
                "hineingeraten, keine Berechnung durchgeführt."
            )
        elif bezugsjahr is None or bezugsmonat is None or ziel_bewertungsjahr is None or basis_betrag_cent is None:
            blockiert_grund = (
                "Eingaben für die gesetzliche Berechnung unvollständig (Bezugsjahr/-monat, "
                "Ziel-Bewertungsjahr und/oder Basisbetrag fehlen) - nur als Entwurf gespeichert."
            )
        else:
            effektiver_monat = 12 if letzte_basis_war_jahresdurchschnitt else bezugsmonat
            vpi_decimal = {jahr: Decimal(eintrag.wert) for jahr, eintrag in vpi_jahresdurchschnitte.items()}
            gesetzliches_ergebnis = berechne_gesetzliche_hoechstgrenze(
                mrg_zinsbeschraenkung=mrg_zinsbeschraenkung,
                erster_bezug_jahr=bezugsjahr,
                erster_bezug_monat=effektiver_monat,
                ziel_bewertungsjahr=ziel_bewertungsjahr,
                vpi_jahresdurchschnitte=vpi_decimal,
                basis_betrag_cent=basis_betrag_cent,
            )
            if not gesetzliches_ergebnis.vollstaendig:
                offene_nachweise.append(
                    f"VPI-Jahresdurchschnitt fehlt für Jahr {gesetzliches_ergebnis.fehlende_jahre[0]} - "
                    "kein erfundener Wert, Berechnung bricht an dieser Stelle ab."
                )

        # -- Vertragsspur: manuell erfasst, Pflicht-Quellenbeleg (keine
        # beleglose Angabe, wie bei jeder anderen Klassifizierung in
        # diesem Modul).
        if vertraglich_zulaessiger_betrag_cent is not None and not (vertraglicher_quellenbeleg or "").strip():
            raise QuellenbelegFehltError(
                "Ein vertraglich zulässiger Betrag ohne Quellenbeleg-Referenz wird abgelehnt - keine "
                "beleglose Klassifizierung der Vertragsspur."
            )
        if vertraglich_zulaessiger_betrag_cent is None:
            offene_nachweise.append("Vertraglich zulässiger Betrag (Vertragsspur) noch nicht erfasst.")
        elif vertraglicher_fruehestmoeglicher_termin is None:
            offene_nachweise.append(
                "Kein vertraglicher frühestmöglicher Termin erfasst - der gesetzliche Termin wird als "
                "Ersatz herangezogen, ohne dass damit eine vertragliche Prüfung ersetzt wäre."
            )
        if not (zustellnachweis_referenz or "").strip():
            offene_nachweise.append(
                "Zustellnachweis fehlt - das Ergebnis bleibt eine reine Vorschau; ohne nachgewiesene, "
                "fristgerechte Mitteilung (mindestens 14 Tage vor dem Zinstermin, § 16 Abs 9 MRG) ist "
                "keine tatsächliche Fälligkeit ausführbar."
            )

        gesetzliche_hoechstgrenze_cent = (
            gesetzliches_ergebnis.hoechstbetrag_cent
            if gesetzliches_ergebnis is not None and gesetzliches_ergebnis.vollstaendig
            else None
        )
        gesetzlicher_termin = gesetzliches_ergebnis.fruehester_termin if gesetzliches_ergebnis is not None else None

        massgeblicher_hoechstbetrag_cent: int | None = None
        fruehester_termin_gesamt: date | None = None
        if gesetzliche_hoechstgrenze_cent is not None and vertraglich_zulaessiger_betrag_cent is not None:
            massgeblicher_hoechstbetrag_cent = min(gesetzliche_hoechstgrenze_cent, vertraglich_zulaessiger_betrag_cent)
            # "Vertraglich später ausgelöste Schwelle erst zum nächsten
            # gesetzlichen April, nie vor dem Vertragstermin": der
            # tatsächlich früheste Termin ist das Maximum aus beiden
            # Spuren, niemals nur einer davon.
            vertrags_termin = vertraglicher_fruehestmoeglicher_termin or gesetzlicher_termin
            fruehester_termin_gesamt = max(gesetzlicher_termin, vertrags_termin)
        else:
            offene_nachweise.append(
                "Maßgeblicher Höchstbetrag noch offen - gesetzliche und/oder vertragliche Spur "
                "unvollständig, keine Vermischung einer einzelnen Seite als Ergebnis."
            )

        eingaben = {
            "rechtsordnung": rechtsordnung,
            "mrg_zinsbeschraenkung": mrg_zinsbeschraenkung,
            "ist_altvertrag": ist_altvertrag,
            "bezugsjahr": bezugsjahr,
            "bezugsmonat": bezugsmonat,
            "letzte_basis_war_jahresdurchschnitt": letzte_basis_war_jahresdurchschnitt,
            "ziel_bewertungsjahr": ziel_bewertungsjahr,
            "basis_betrag_cent": basis_betrag_cent,
            "basis_komponenten_ids": list(basis_komponenten_ids),
            "vpi_jahresdurchschnitte": {str(jahr): asdict(eintrag) for jahr, eintrag in vpi_jahresdurchschnitte.items()},
            "vertraglich_zulaessiger_betrag_cent": vertraglich_zulaessiger_betrag_cent,
            "vertraglicher_quellenbeleg": vertraglicher_quellenbeleg,
            "vertraglicher_fruehestmoeglicher_termin": (
                vertraglicher_fruehestmoeglicher_termin.isoformat() if vertraglicher_fruehestmoeglicher_termin else None
            ),
            "zustellnachweis_referenz": zustellnachweis_referenz,
            "kommentar": kommentar,
        }
        ergebnis = {
            "blockiert_grund": blockiert_grund,
            "gesetzliche_hoechstgrenze_cent": gesetzliche_hoechstgrenze_cent,
            "gesetzlicher_termin": gesetzlicher_termin.isoformat() if gesetzlicher_termin else None,
            "vertraglich_zulaessiger_betrag_cent": vertraglich_zulaessiger_betrag_cent,
            "massgeblicher_hoechstbetrag_cent": massgeblicher_hoechstbetrag_cent,
            "fruehester_termin_gesamt": fruehester_termin_gesamt.isoformat() if fruehester_termin_gesamt else None,
            "fehlende_jahre": gesetzliches_ergebnis.fehlende_jahre if gesetzliches_ergebnis is not None else [],
            "jahresschritte": (
                [_jahresschritt_zu_dict(schritt) for schritt in gesetzliches_ergebnis.jahresschritte]
                if gesetzliches_ergebnis is not None
                else []
            ),
            "offene_nachweise": offene_nachweise,
        }

        version = self._vorschau_repository.naechste_version(vertrag.id)
        return self._vorschau_repository.anlegen(
            vertrag_id=vertrag.id,
            version=version,
            rechtsordnung=rechtsordnung,
            ist_wohnungsrechner_fall=ist_wohnungsrechner_fall,
            ziel_bewertungsjahr=ziel_bewertungsjahr,
            vollstaendig=not offene_nachweise and massgeblicher_hoechstbetrag_cent is not None,
            massgeblicher_hoechstbetrag_cent=massgeblicher_hoechstbetrag_cent,
            fruehester_termin=fruehester_termin_gesamt,
            eingaben_json=json.dumps(eingaben, ensure_ascii=False, sort_keys=True),
            ergebnis_json=json.dumps(ergebnis, ensure_ascii=False, sort_keys=True),
            erstellt_von=akteur,
        )

    def liste_fuer_vertrag(self, vertrag_id: str) -> list[MieWegVorschauTable]:
        return self._vorschau_repository.liste_fuer_vertrag(vertrag_id)

    def aktuelle(self, vertrag_id: str) -> MieWegVorschauTable | None:
        return self._vorschau_repository.aktuelle(vertrag_id)
