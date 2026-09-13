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
eine Seite verwendet.

Alle Cent-Beträge in diesem Modul (`basis_betrag_cent`,
`vertraglich_zulaessiger_betrag_cent`, `aktuell_verrechneter_betrag_cent`)
sind BRUTTO - verbindliche Konvention dieses Repositories, siehe
`domain/money.py::zerlege_brutto_cent`: "`VertragsKomponenteTable.
betrag_cent`/`VorschreibungPositionTable.betrag_cent` sind BRUTTO - das
ist der Betrag, der tatsächlich als SOLL gebucht wird". Referenzierte
Komponenten mit UNTERSCHIEDLICHEN `ust_satz_promille`-Sätzen dürfen zu
EINER Brutto-Summe addiert werden (Brutto + Brutto = Brutto,
unabhängig vom jeweiligen Steuersatz), aber diese Summe darf an KEINER
Stelle so behandelt werden, als wäre sie über einen einzigen,
einheitlichen (Netto-)Steuersatz herleitbar - eine solche Rückrechnung
fände hier ohnehin nicht statt (`berechnung.py` multipliziert
ausschließlich mit VPI-Verhältniszahlen, nie mit einem Steuersatz),
wird hier aber ausdrücklich als Grenze dokumentiert, da eine künftige
Erweiterung (z. B. ein Netto-Ausweis je Komponente) das sonst
stillschweigend falsch mischen könnte."""

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

# Defense-in-depth (Folgeauftrag Markus) - unabhängig vom `indexierbar`-
# Flag auf `VertragsKomponenteTable` NIEMALS automatisch indexierbar.
# Deckt sich bewusst mit `index/service.py::_NIE_INDEXIERBARE_ARTEN`
# (BK_VORAUSZAHLUNG/HEIZ_WW_VORAUSZAHLUNG) und ergänzt die dort
# genannten Aliasarten (BK_VZ/HK_VZ/BK_PARKPLATZ) - bewusst eine eigene,
# lokale Liste statt eines Imports aus `index/service.py`, um dieses
# Modul (Paket C) unabhängig vom bestehenden Index-MVP1-Modul zu halten
# (siehe AGENTS.md-Modultrennung); Codex sollte beide Listen bei
# künftigen Änderungen synchron halten.
_NIE_INDEXIERBARE_ARTEN = frozenset({
    "BK_VORAUSZAHLUNG",
    "HEIZ_WW_VORAUSZAHLUNG",
    "BK_VZ",
    "HK_VZ",
    "BK_PARKPLATZ",
    "WASSER",
    "STROM",
})


def belegte_historische_basis_gueltig(beleg: dict | None, *, erwarteter_betrag_cent: int) -> bool:
    """Codex-Rückprüfung: "Historisierungskette löst noch nicht initial
    importierte Bestandskomponenten" - ein Mietvertrag/eine belegte
    Indexbasis kann zeitlich VOR der erst später importierten
    `VertragsKomponenteTable`-Zeile liegen (z. B. Vertragsbeginn 1.4.,
    belegte Indexbasis Februar, eine unveränderte Pauschale aber erst ab
    August importiert). `StammdatenRepository.ursprungs_gueltig_von`
    kann eine SPÄTER importierte Zeile nicht von einer tatsächlich
    fehlenden Historie unterscheiden - eine reine Zeitprüfung würde
    einen tatsächlich bestehenden, nur spät importierten Altbestand
    fälschlich blockieren.

    Diese Funktion prüft einen EXPLIZITEN, vom Operator erfassten
    Ausnahmenachweis (`RechtsprofilTable.historische_basis_belege[
    komponente_id]`, Form `{"betrag_cent": int, "datum": "YYYY-MM-DD",
    "quellenbeleg": str}`). Kein Rateversuch: ein fehlender/
    unvollständiger Beleg liefert `False`, die Sperre bleibt bestehen.
    Ändert NIEMALS die technische Komponentenzeile selbst - rein
    dokumentarischer Nachweis, fließt in KEINE Berechnung ein.

    Zweite unabhängige Abnahme (Codex-Rückprüfung zu 28ca323): der
    belegte Betrag MUSS exakt dem tatsächlich für die Berechnung
    verwendeten aktuellen Komponentenbetrag entsprechen
    (`erwarteter_betrag_cent`) - ohne diesen Abgleich hätte ein
    beliebiger, insbesondere ein trivial niedriger Belegbetrag (z. B.
    1 Cent) die Existenzsperre für eine völlig unabhängig davon
    verrechnete, viel höhere Komponente aufgehoben.

    `datum` ist AUSSCHLIESSLICH eine dokumentarische Angabe (wann der
    Beleg selbst ausgestellt/unterfertigt wurde) - es wird NICHT gegen
    den vertraglichen Bezugsmonat (`RechtsprofilTable.bezugsjahr/
    -monat`) geprüft; beide bleiben ausdrücklich getrennte Konzepte
    (Codex-Rückprüfung: Vertragsunterzeichnung im März kann eine
    vertraglich vereinbarte VPI-Basis Februar dokumentieren - das
    Beleg-/Unterschriftsdatum liegt dabei bewusst NACH dem belegten
    Bezugsmonat; ein früheres Belegdatum darf dafür nicht erfunden
    werden)."""

    if not beleg:
        return False
    betrag_cent = beleg.get("betrag_cent")
    quellenbeleg = beleg.get("quellenbeleg")
    datum_str = beleg.get("datum")
    if not isinstance(betrag_cent, int) or betrag_cent != erwarteter_betrag_cent:
        return False
    if not isinstance(quellenbeleg, str) or not quellenbeleg.strip():
        return False
    if not isinstance(datum_str, str):
        return False
    try:
        date.fromisoformat(datum_str)
    except ValueError:
        return False
    return True


def _naechster_gueltiger_april(datum: date) -> date:
    """MieWeG-Anpassungen sind ausschließlich zu einem 1. April wirksam
    (§ 1 Abs 4) - rundet ein beliebiges Datum auf den nächsten gültigen
    1. April auf. Ein bereits gültiger 1. April bleibt unverändert."""

    kandidat = date(datum.year, 4, 1)
    return kandidat if datum <= kandidat else date(datum.year + 1, 4, 1)


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
        ist_wohnungsnutzung: bool | None,
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
        aktuell_verrechneter_betrag_cent: int | None,
        aktuell_verrechnet_quellenbeleg: str | None,
        aktuell_verrechnet_stichtag: date | None,
        zustellnachweis_referenz: str | None,
        kommentar: str | None,
        akteur: str,
        historische_basis_belege: dict[str, dict] | None = None,
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
        # Zweite unabhängige Abnahme (Folgeauftrag Markus): ein negativer
        # "Ist"-Betrag (aktuell verrechnet) oder ein negativer
        # vertraglicher Zielbetrag wurde bisher anstandslos akzeptiert
        # und konnte eine fiktive, riesige "Erhöhung" erzeugen (ein
        # negativer aktuell verrechneter Betrag hätte in `max(0,
        # massgeblich - aktuell_verrechnet)` den Subtrahenden künstlich
        # vergrößert). Null bleibt bewusst zulässig (kein Ersatz für
        # "nicht erfasst" - das ist weiterhin `None`, geprüft über
        # `is not None`, nicht über Wahrheitswert).
        if aktuell_verrechneter_betrag_cent is not None and aktuell_verrechneter_betrag_cent < 0:
            raise ValueError(
                f"aktuell_verrechneter_betrag_cent ({aktuell_verrechneter_betrag_cent}) ist negativ - "
                "kein plausibler tatsächlich verrechneter Betrag."
            )
        if vertraglich_zulaessiger_betrag_cent is not None and vertraglich_zulaessiger_betrag_cent < 0:
            raise ValueError(
                f"vertraglich_zulaessiger_betrag_cent ({vertraglich_zulaessiger_betrag_cent}) ist negativ - "
                "kein plausibler vertraglicher Zielbetrag."
            )

        # Nur explizit vertragsindexierte Komponenten (Fachregel: "z.B.
        # Küche kann ja sein, Garage nicht automatisch"; BK/HK/Versorger
        # nie automatisch) - jede referenzierte Komponente muss zu DIESEM
        # Vertrag gehören und explizit als indexierbar markiert sein.
        #
        # Zusätzlich (Folgeauftrag Markus, Schutz gegen bereits enthaltene
        # Erhöhungen): "Komponenten statt Gesamtbrutto" - wer Komponenten
        # referenziert, behauptet damit, die Basis bestehe aus GENAU diesen
        # Beträgen. Ein davon unabhängiger basis_betrag_cent (zu hoch oder
        # zu niedrig) würde einen bereits in den Komponenten enthaltenen
        # Erhöhungsschritt unbemerkt verfälschen - deshalb muss die Summe
        # exakt übereinstimmen. Ebenso muss jede referenzierte Komponente
        # zum angegebenen Bezugszeitpunkt (Bezugsjahr/-monat) laut
        # Stammdaten bereits bestanden haben ("unterschiedliche bestehende
        # HMZ/Küche/Parkplatz-Stände"): eine erst später vereinbarte
        # Komponente darf nicht rückwirkend über den gesamten
        # Kumulierungszeitraum mit hochgerechnet werden.
        # Leere/doppelte IDs sind IMMER ein fehlerhafter Aufruf, nie eine
        # legitime Eingabe - eine doppelte ID würde denselben Betrag ein
        # zweites Mal in `summe_komponenten_cent` einrechnen (eigene Form
        # einer Doppelzählung).
        if any(not (kid or "").strip() for kid in basis_komponenten_ids):
            raise ValueError("basis_komponenten_ids enthält eine leere/blanke ID.")
        if len(basis_komponenten_ids) != len(set(basis_komponenten_ids)):
            raise ValueError("basis_komponenten_ids enthält doppelte Einträge.")

        referenzdatum = date(bezugsjahr, bezugsmonat, 1) if bezugsjahr is not None and bezugsmonat is not None else None
        summe_komponenten_cent = 0
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
            if komponente.art in _NIE_INDEXIERBARE_ARTEN:
                raise ValueError(
                    f"Komponente {komponente_id} hat die Art '{komponente.art}' - Betriebs-/Heizkosten(-"
                    "Vorauszahlungen) und vergleichbare Aliasarten werden NIE automatisch indexiert, "
                    f"unabhängig vom indexierbar-Flag (verteidigt gegen eine fehlerhaft gesetzte "
                    f"indexierbar=True-Markierung; ausgeschlossene Arten: {sorted(_NIE_INDEXIERBARE_ARTEN)})."
                )
            if (
                referenzdatum is not None
                and self._stammdaten_repository.ursprungs_gueltig_von(komponente) > referenzdatum
                and not belegte_historische_basis_gueltig(
                    (historische_basis_belege or {}).get(komponente_id), erwarteter_betrag_cent=komponente.betrag_cent
                )
            ):
                raise ValueError(
                    f"Komponente {komponente_id} ist erst ab {komponente.gueltig_von.isoformat()} gültig und "
                    f"hat zum angegebenen Bezugszeitpunkt {referenzdatum.isoformat()} noch nicht bestanden - "
                    "sie darf nicht rückwirkend in die Basis einfließen. Ein expliziter, vollständiger "
                    "Ausnahmenachweis (Betrag/Datum/Quellenbeleg, RechtsprofilTable."
                    "historische_basis_belege) kann diese Sperre für GENAU diese Komponente aufheben."
                )
            if referenzdatum is not None and komponente.gueltig_bis is not None and komponente.gueltig_bis < referenzdatum:
                raise ValueError(
                    f"Komponente {komponente_id} war bereits bis {komponente.gueltig_bis.isoformat()} befristet "
                    f"und zum angegebenen Bezugszeitpunkt {referenzdatum.isoformat()} nicht mehr gültig."
                )
            summe_komponenten_cent += komponente.betrag_cent

        if basis_komponenten_ids and basis_betrag_cent is not None and summe_komponenten_cent != basis_betrag_cent:
            raise ValueError(
                f"basis_betrag_cent ({basis_betrag_cent}) entspricht nicht der Summe der referenzierten "
                f"Komponenten ({summe_komponenten_cent}) - keine von den Komponenten losgelöste Basis, "
                "sonst könnte ein bereits enthaltener Erhöhungsschritt unbemerkt verfälscht werden."
            )

        # Folgeauftrag Markus: MieWeG §1 Abs1 gilt AUSDRÜCKLICH nur für
        # WOHNUNGEN - eine MRG-Vollanwendung/-Teilanwendung allein sagt
        # das NICHT aus (auch ein Geschäftsraum kann unter MRG_VOLL/
        # MRG_TEIL fallen). Da es dafür kein bestehendes Stammdatenfeld
        # gibt, muss die Wohnungsnutzung explizit bestätigt werden;
        # `None` (nicht geprüft) oder `False` sperrt den Wohnungsrechner
        # GENAUSO wie ein falsches Rechtsprofil - kein Rateversuch, kein
        # Sonderweg über eine bloß angenommene MRG-Kategorie.
        ist_wohnungsrechner_fall = rechtsordnung in _WOHNUNGSRECHNER_RECHTSORDNUNGEN and ist_wohnungsnutzung is True
        offene_nachweise: list[str] = []
        blockiert_grund: str | None = None
        gesetzliches_ergebnis: GesetzlicheHoechstgrenzeErgebnis | None = None

        if rechtsordnung == Rechtsordnung.UNGEKLAERT.value:
            blockiert_grund = (
                "Rechtsprofil UNGEKLAERT - kein Rateversuch, keine ausführbare Anpassung; nur als "
                "Entwurf mit fehlenden Eingaben gespeichert."
            )
        elif rechtsordnung in _WOHNUNGSRECHNER_RECHTSORDNUNGEN and ist_wohnungsnutzung is not True:
            blockiert_grund = (
                f"Rechtsordnung {rechtsordnung} allein belegt keine Wohnungsnutzung - MieWeG §1 Abs1 gilt "
                "ausdrücklich nur für Wohnungen, nicht automatisch für jede MRG-Geschäftsmiete. Ohne "
                "explizit bestätigte Wohnungsnutzung (ist_wohnungsnutzung=True) kein Wohnungsrechner-Fall, "
                "keine Berechnung durchgeführt."
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
            # Zweite unabhängige Abnahme (Folgeauftrag Markus): `any([])`
            # und `len([]) != len(set([]))` sind beide `False` - eine
            # LEERE `basis_komponenten_ids`-Liste rutschte bislang durch
            # ALLE Komponentenprüfungen und ließ `basis_betrag_cent` als
            # völlig freien, nicht rückführbaren Wert stehen, der dennoch
            # zu `vollstaendig=True` führen konnte. Für eine tatsächlich
            # durchgeführte numerische Berechnung wird deshalb eine ECHTE,
            # nichtleere Komponentenliste verlangt - ein unvollständiger
            # Entwurf (z. B. fehlendes Bezugsjahr, siehe `blockiert_grund`
            # oben) bleibt davon unberührt und darf weiterhin offen sein.
            if not basis_komponenten_ids:
                offene_nachweise.append(
                    "Keine Komponenten referenziert (basis_komponenten_ids ist leer) - eine numerische "
                    "Berechnung ohne strukturierte, nachvollziehbare Komponentenbindung bleibt Prüfbedarf."
                )
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

            offene_nachweise.extend(self._ueberlappungshinweise(vertrag.id, bezugsjahr))

            # Folgeauftrag Markus: "letzter maßgeblicher Bezugsmonat bei
            # Altverträgen, nicht blind ursprünglicher Mietbeginn" (§4
            # Abs2). Diese Klasse kann nicht verlässlich prüfen, OB
            # bezugsjahr/-monat tatsächlich der zuletzt verwendete
            # Indexwert sind (dafür gibt es keine verbindliche
            # Datenquelle in diesem Modul) - aber ein exaktes
            # Zusammentreffen mit dem URSPRÜNGLICHEN Vertragsbeginn ist
            # ein konkretes Verdachtsmoment auf eine blind übernommene
            # Abschlussdatum-statt-Bezugsmonat-Verwechslung und wird
            # deshalb als offener Nachweis erzwungen (kein Rateversuch:
            # der Fall kann legitim sein, wenn der Vertrag tatsächlich
            # noch nie indexiert wurde - deshalb Prüfbedarf, kein
            # Hartstopp).
            if (
                ist_altvertrag
                and vertrag.gueltig_von is not None
                and bezugsjahr == vertrag.gueltig_von.year
                and bezugsmonat == vertrag.gueltig_von.month
            ):
                offene_nachweise.append(
                    f"Altvertrag mit Bezugsjahr/-monat {bezugsjahr}-{bezugsmonat:02d} identisch zum "
                    f"ursprünglichen Vertragsbeginn ({vertrag.gueltig_von.isoformat()}) - bitte bestätigen, "
                    "dass dies tatsächlich der zuletzt verwendete Indexwert ist und nicht blind das "
                    "ursprüngliche Abschlussdatum übernommen wurde (§4 Abs2)."
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

        # -- Aktuell verrechneter Betrag: GETRENNT vom historischen
        # `basis_betrag_cent` (Folgeauftrag Markus, konkreter
        # Doppelzählungs-Fund): der historische Basisbetrag und der
        # HEUTE tatsächlich verrechnete Betrag sind unterschiedliche
        # Zustände - zwischen dem historischen Bezugszeitpunkt und heute
        # können bereits (teilweise) Erhöhungen umgesetzt worden sein,
        # die dieses Modul nicht kennt. `massgeblicher_hoechstbetrag_cent`
        # ist NIE unmittelbar "der neue Zielbetrag", sondern nur die
        # gesetzliche/vertragliche OBERGRENZE seit dem historischen
        # Bezugspunkt - ohne einen dokumentierten, aktuellen
        # Vergleichswert (mit Datum/Beleg) darf daraus KEINE ausführbare
        # Erhöhung abgeleitet werden (sonst würde ein bereits verrechneter
        # Teil der Erhöhung ein zweites Mal aufgeschlagen). Pflicht-
        # Quellenbeleg wie bei jeder anderen Klassifizierung in diesem
        # Modul.
        if aktuell_verrechneter_betrag_cent is not None and not (aktuell_verrechnet_quellenbeleg or "").strip():
            raise QuellenbelegFehltError(
                "Ein aktuell verrechneter Betrag ohne Quellenbeleg-Referenz wird abgelehnt - keine "
                "beleglose Vergleichsbasis."
            )
        if aktuell_verrechneter_betrag_cent is None:
            offene_nachweise.append(
                "Aktuell verrechneter Betrag (mit Datum/Beleg) noch nicht erfasst - ohne diese Historie "
                "ist keine ausführbare Erhöhung ausweisbar, nur die gesetzliche/vertragliche Obergrenze "
                "seit dem historischen Bezugszeitpunkt."
            )
        elif aktuell_verrechnet_stichtag is None:
            offene_nachweise.append(
                "Kein Stichtag für den aktuell verrechneten Betrag erfasst - der Vergleichswert bleibt "
                "ohne Datum nicht nachvollziehbar."
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
            #
            # KORRIGIERT (Folgeauftrag Markus): das reine Maximum zweier
            # Daten kann einen NICHT-April-Termin liefern (z. B. ein
            # vertraglicher September-Termin), obwohl § 1 Abs 4 MieWeG
            # ausschließlich den 1. April als Anpassungsstichtag zulässt.
            # Der kombinierte Termin wird deshalb auf den nächsten
            # gültigen 1. April aufgerundet; landet dieser dadurch in
            # einem ANDEREN Jahr als dem hier berechneten
            # `ziel_bewertungsjahr`, wird das NICHT stillschweigend
            # übernommen (dafür wären zusätzliche, hier nicht angefragte
            # VPI-Jahreswerte nötig, siehe `berechne_gesetzliche_
            # hoechstgrenze`) - stattdessen wird kein Termin ausgegeben,
            # sondern ein offener Nachweis erzwungen ("berücksichtigen
            # oder sperren" - hier: sperren, bis eine eigene Berechnung
            # für das richtige Ziel-Bewertungsjahr vorliegt).
            vertrags_termin = vertraglicher_fruehestmoeglicher_termin or gesetzlicher_termin
            kombiniert_roh = max(gesetzlicher_termin, vertrags_termin)
            kombiniert_april = _naechster_gueltiger_april(kombiniert_roh)
            if kombiniert_april.year != ziel_bewertungsjahr:
                offene_nachweise.append(
                    f"Der vertragliche Termin ({vertrags_termin.isoformat()}) verschiebt die früheste "
                    f"zulässige Anpassung auf den nächsten gültigen 1. April ({kombiniert_april.isoformat()}) "
                    f"- ein anderes Jahr als das hier berechnete Ziel-Bewertungsjahr {ziel_bewertungsjahr}. "
                    "Nur der 1. April ist als Anpassungstermin zulässig; für dieses spätere Jahr ist eine "
                    "eigene Berechnung mit den dafür erforderlichen VPI-Werten nötig, kein automatischer "
                    "Sprung auf einen nicht berechneten Zeitraum."
                )
            else:
                fruehester_termin_gesamt = kombiniert_april
        else:
            offene_nachweise.append(
                "Maßgeblicher Höchstbetrag noch offen - gesetzliche und/oder vertragliche Spur "
                "unvollständig, keine Vermischung einer einzelnen Seite als Ergebnis."
            )

        # max(0, ...): niemals eine Senkung "vorschlagen", falls der
        # aktuell verrechnete Betrag den Höchstbetrag bereits erreicht/
        # übersteigt - das bedeutet lediglich, dass dieser Erhöhungs-
        # schritt bereits ausgeschöpft ist, nicht dass rückwirkend
        # gesenkt werden müsste (das wäre eine eigene, hier nicht
        # beauftragte Fachfrage).
        #
        # KORRIGIERT (zweite unabhängige Abnahme, Folgeauftrag Markus):
        # `ausfuehrbare_erhoehung_cent` wurde bisher allein aus den beiden
        # Beträgen berechnet, OHNE zu prüfen, ob das Gesamtergebnis
        # überhaupt vollständig ist - ein fehlender Stichtag, ein
        # fehlender Zustellnachweis, ein in ein anderes Jahr
        # verschobener Termin oder eine leere Komponentenauswahl hatten
        # alle bereits einen `offene_nachweise`-Eintrag erzeugt
        # (`vollstaendig=False`), aber die Zahl wurde trotzdem als
        # scheinbar belastbares Ergebnis ausgegeben. Jetzt getrennt:
        # `rechnerische_differenz_cent` ist eine rein informative
        # Vorschauzahl (sobald beide Beträge numerisch vorliegen), OHNE
        # Aussage über Vollständigkeit; `ausfuehrbare_erhoehung_cent`
        # wird NUR gesetzt, wenn ZU DIESEM ZEITPUNKT keinerlei offener
        # Nachweis mehr aussteht (also alle Vorbedingungen dieser
        # Vorschau erfüllt sind) - keine Freigabe, keine Behauptung von
        # Ausführbarkeit ohne vollständige Nachweise.
        rechnerische_differenz_cent: int | None = None
        if massgeblicher_hoechstbetrag_cent is not None and aktuell_verrechneter_betrag_cent is not None:
            rechnerische_differenz_cent = max(0, massgeblicher_hoechstbetrag_cent - aktuell_verrechneter_betrag_cent)
        ausfuehrbare_erhoehung_cent: int | None = (
            rechnerische_differenz_cent if not offene_nachweise else None
        )

        eingaben = {
            "rechtsordnung": rechtsordnung,
            "ist_wohnungsnutzung": ist_wohnungsnutzung,
            "mrg_zinsbeschraenkung": mrg_zinsbeschraenkung,
            "ist_altvertrag": ist_altvertrag,
            "bezugsjahr": bezugsjahr,
            "bezugsmonat": bezugsmonat,
            "letzte_basis_war_jahresdurchschnitt": letzte_basis_war_jahresdurchschnitt,
            "ziel_bewertungsjahr": ziel_bewertungsjahr,
            "basis_betrag_cent": basis_betrag_cent,
            "basis_komponenten_ids": list(basis_komponenten_ids),
            "historische_basis_belege": historische_basis_belege or {},
            "vpi_jahresdurchschnitte": {str(jahr): asdict(eintrag) for jahr, eintrag in vpi_jahresdurchschnitte.items()},
            "vertraglich_zulaessiger_betrag_cent": vertraglich_zulaessiger_betrag_cent,
            "vertraglicher_quellenbeleg": vertraglicher_quellenbeleg,
            "vertraglicher_fruehestmoeglicher_termin": (
                vertraglicher_fruehestmoeglicher_termin.isoformat() if vertraglicher_fruehestmoeglicher_termin else None
            ),
            "aktuell_verrechneter_betrag_cent": aktuell_verrechneter_betrag_cent,
            "aktuell_verrechnet_quellenbeleg": aktuell_verrechnet_quellenbeleg,
            "aktuell_verrechnet_stichtag": (
                aktuell_verrechnet_stichtag.isoformat() if aktuell_verrechnet_stichtag else None
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
            # Dupliziert aus `eingaben` (wie `vertraglich_zulaessiger_
            # betrag_cent` oben) - ausschließlich für bequemen Lesezugriff
            # auf das Ergebnis (z. B. `backoffice/app.py::
            # _mieweg_vorschau_zeile_html`), OHNE `eingaben_json`
            # zusätzlich parsen zu müssen. Bugfund (zweite unabhängige
            # Abnahme): dieses Feld fehlte hier ursprünglich, obwohl die
            # Backoffice-Anzeige es bereits aus `ergebnis_json` gelesen
            # hat - die Spalte "Aktuell verrechnet" zeigte deshalb immer
            # einen Strich.
            "aktuell_verrechneter_betrag_cent": aktuell_verrechneter_betrag_cent,
            "rechnerische_differenz_cent": rechnerische_differenz_cent,
            "ausfuehrbare_erhoehung_cent": ausfuehrbare_erhoehung_cent,
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

    def _ueberlappungshinweise(self, vertrag_id: str, bezugsjahr: int) -> list[str]:
        """Folgeauftrag Markus (Schutz gegen bereits enthaltene
        Erhöhungen): eine FRÜHERE Vorschau für DENSELBEN Vertrag, deren
        Ziel-Bewertungsjahr auf oder nach dem hier verwendeten Bezugsjahr
        liegt und die bereits eine gesetzliche Höchstgrenze berechnet
        hatte, deckt den Kumulierungsschritt bis zu ihrem eigenen
        Ziel-Bewertungsjahr schon ab. Eine Folgevorschau, die WIEDER vom
        selben (oder einem noch früheren) Bezugsjahr aus rechnet, statt ab
        dem Ziel-Bewertungsjahr der Vorversion fortzusetzen, würde diesen
        Schritt ein zweites Mal einrechnen ("identischer Wiederholungslauf"/
        "nachträgliche Profiländerung"). Das wird NICHT automatisch
        blockiert (reine Vorschau, keine Fachentscheidung durch Code),
        aber als offener Nachweis erzwungen - `vollstaendig` wird dadurch
        automatisch False (siehe Verwendung von `offene_nachweise` in
        `anlegen()`)."""

        hinweise: list[str] = []
        for vorherige in self._vorschau_repository.liste_fuer_vertrag(vertrag_id):
            if vorherige.ziel_bewertungsjahr is None or vorherige.ziel_bewertungsjahr < bezugsjahr:
                continue
            vorheriges_ergebnis = json.loads(vorherige.ergebnis_json)
            if vorheriges_ergebnis.get("gesetzliche_hoechstgrenze_cent") is None:
                continue
            hinweise.append(
                f"Überlappung mit Version {vorherige.version} (Ziel-Bewertungsjahr "
                f"{vorherige.ziel_bewertungsjahr}): diese Vorversion hat bereits eine gesetzliche "
                f"Höchstgrenze bis {vorherige.ziel_bewertungsjahr} berechnet, die aktuelle Vorschau "
                f"rechnet aber erneut ab Bezugsjahr {bezugsjahr} - vor Verwendung prüfen, ob eine "
                "Erhöhung aus der Vorversion bereits umgesetzt wurde, um keinen Schritt doppelt zu "
                "zählen."
            )
        return hinweise

    def liste_fuer_vertrag(self, vertrag_id: str) -> list[MieWegVorschauTable]:
        return self._vorschau_repository.liste_fuer_vertrag(vertrag_id)

    def aktuelle(self, vertrag_id: str) -> MieWegVorschauTable | None:
        return self._vorschau_repository.aktuelle(vertrag_id)
