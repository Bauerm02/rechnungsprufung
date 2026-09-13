"""Zentrale, geschäftsübergreifende Rückstandsübersicht für das
Backoffice-Dashboard (Auftrag 13.09.2026, HV-20260913-RUECKSTAENDE).

REIN LESEND - bucht/plant/versendet nichts. Kombiniert ausschließlich
bereits bestehende, bepreisungsstabile Services:

- `StammdatenRepository` für Gesellschaften/Objekte/Einheiten/Verträge/
  Debitoren und aktive Mahnsperren (`aktive_sperren`).
- `OPService.berechne_saldo`/`OPService.offene_forderungen` für
  Kontosalden bzw. je Forderung getrennte offene Positionen - KEINE
  eigene Saldo-/Verrechnungslogik, nur Aggregation der bestehenden
  Ergebnisse.
- `MahnFallRepository.list_fuer_vertrag` (reiner Read) für ALLE
  bereits gespeicherten Mahnfälle - NIEMALS
  `MahnwesenService.plane_forderung`/`plane_alle_offenen_forderungen`,
  die neue `MahnFallTable`-Zeilen anlegen würden. Diese Übersicht ist
  ein GET-Seiteneffektfreier Lesepfad; Mahnfallbeträge fließen an
  KEINER Stelle in die OP-Kennzahlen ein (reine Anzeige nebeneinander).

Fachliche Leitplanken (siehe auch AGENTS.md):

- Sollsalden/Habensalden werden je Konto getrennt geführt; ein Guthaben
  eines Mieters wird NIE gegen den Rückstand eines anderen verrechnet
  ("Guthaben separat ohne Verrechnung zwischen Mietern").
- Eine bekannte Fälligkeit ist NIE eine Mahnfreigabe; eine unbekannte
  Fälligkeit ist NICHT automatisch strittig - beide werden als eigene,
  klar benannte Kennzahl geführt statt vermischt.
- `OPSaldo.faelliger_unstrittiger_rest_cent` (bestehende Kontosaldo-
  Rechnung) und die Summe der `OffeneForderung.rest_cent` je
  Einzelposition sind ZWEI VERSCHIEDENE, absichtlich getrennt geführte
  Ergebnisse desselben `OPService` - JEDE Mietkonto-Zeile trägt darum
  BEIDE Zahlen getrennt (`faelliger_unstrittiger_rest_cent` und
  `positionen_faelliger_rest_cent`) sowie eine explizite
  `abweichung_saldo_zu_positionen_cent` (Kontostand minus Summe aller
  offenen Einzelpositionen) - nie stillschweigend gleichgesetzt oder
  glattgerechnet. Ein Beispiel, das eine echte Abweichung erzeugt: eine
  positive KORREKTUR-Buchung erhöht den Kontostand, erzeugt aber KEINE
  eigene offene Einzelposition (KORREKTUR ist keine Forderungsart in
  `OPService.offene_forderungen`).
- Einheiten ohne (aktives) Mietkonto - Leerstand, Kurzzeitvermietung,
  Selfstorage, Eigennutzung - sind Bestand, kein erfundener
  Nullsaldo/Rückstand; sie erscheinen in einer eigenen Liste, NIE als
  Mietkonto-Zeile mit Saldo 0.
- Ausgeschlossene Objekte (Pilotausschluss) fließen NIE in die
  Kennzahlen/„Alle Objekte“-Summen und stehen NICHT als Filteroption
  zur Verfügung; ein explizit angefordertes ausgeschlossenes, fremdes
  oder unbekanntes `objekt_id` wird einheitlich (kein Erkenntnisgewinn
  für den Aufrufer, welcher der drei Fälle vorliegt) abgelehnt statt
  stillschweigend auf „Alle Objekte“ umzuschalten.
- Ein Vertrag/Konto, dessen EIGENES `gesellschaft_id`-Feld nicht zum
  `ctx`-Zugriff passt, wird übersprungen, SELBST WENN das zugehörige
  Objekt/die Einheit zu einer erlaubten Gesellschaft gehört - eine
  inkonsistente Stammdatenzeile (falsches `gesellschaft_id` an
  Vertrag/Konto) darf keine Finanzdaten durchlassen, nur weil ihr
  Objekt zufällig erlaubt ist."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from mietinkasso.auth.service import AuthContext
from mietinkasso.mahnwesen.repository import MahnFallRepository
from mietinkasso.op.service import OPService
from mietinkasso.stammdaten.repository import StammdatenRepository


class UnbekanntesObjektFilterError(ValueError):
    """`objekt_id` ist unbekannt, gehört zu einer Gesellschaft ohne
    `ctx`-Zugriff, oder ist ausgeschlossen - bewusst EIN einheitlicher
    Fehlertyp/eine einheitliche Meldung für alle drei Fälle, damit ein
    Aufrufer nicht ausprobieren kann, welcher der drei Fälle vorliegt."""


@dataclass(frozen=True)
class ObjektOption:
    id: str
    bezeichnung: str
    gesellschaft_id: str
    gesellschaft_name: str


@dataclass(frozen=True)
class MietkontoZeile:
    objekt_id: str
    objekt_bezeichnung: str
    vertrag_id: str
    einheit_id: str
    einheit_bezeichnung: str
    nutzungsstatus: str
    debitor_name: str
    konto_id: str | None
    saldo_cent: int | None
    faelliger_unstrittiger_rest_cent: int | None  # bestehende Kontosaldo-Rechnung (OPSaldo)
    positionen_faelliger_rest_cent: int | None  # Summe der Einzelpositionen mit bekannter, verstrichener Fälligkeit
    positionen_rest_gesamt_cent: int | None  # Summe ALLER offenen Einzelpositionen dieses Kontos
    abweichung_saldo_zu_positionen_cent: int | None  # saldo_cent - positionen_rest_gesamt_cent
    historisch: bool
    sperrgruende: tuple[str, ...]
    mahnfaelle_anzahl: int  # Anzahl vorhandener Mahnfälle - Details siehe RueckstandsUebersicht.mahnfaelle


@dataclass(frozen=True)
class OffenePositionZeile:
    objekt_id: str
    objekt_bezeichnung: str
    vertrag_id: str
    debitor_name: str
    konto_id: str
    op_position_id: int
    beleg_referenz: str
    art: str
    betrag_cent: int
    rest_cent: int
    belegdatum: date
    leistungsperiode: str | None
    faelligkeit: date | None
    faelligkeit_bekannt: bool
    faelligkeitsklasse: str  # "UEBERFAELLIG" | "NICHT_FAELLIG" | "UNBEKANNT"


@dataclass(frozen=True)
class MahnfallZeile:
    """EIN gespeicherter Mahnfall (reiner Read aus `MahnFallTable`) -
    JEDER vorhandene Fall je Forderung/Vertrag wird gezeigt, nicht nur
    der zuletzt angelegte, damit frühere Stufen/Forderungen nicht
    ausgeblendet werden. `betrag_cent` ist der ursprüngliche, zum
    Planungszeitpunkt festgehaltene Fallbetrag - fließt NIRGENDS in die
    OP-Kennzahlen dieser Übersicht ein."""

    objekt_id: str
    objekt_bezeichnung: str
    vertrag_id: str
    forderung_op_position_id: int
    stufe: int
    status: str
    betrag_cent: int
    geplant_am: datetime


@dataclass(frozen=True)
class EinheitOhneKontoZeile:
    objekt_id: str
    objekt_bezeichnung: str
    einheit_id: str
    einheit_bezeichnung: str
    nutzungsstatus: str


@dataclass(frozen=True)
class RueckstandsKennzahlen:
    """Bewusst FÜNF getrennte Zahlen statt einer Summe - siehe Modul-
    Docstring: positiver Kontostand und Guthaben werden nie gegeneinander
    verrechnet, und die drei Fälligkeitsklassen der EINZELPOSITIONEN
    (nicht des Kontosaldos) bleiben ebenfalls getrennt."""

    summe_positiver_kontostaende_cent: int
    summe_guthaben_cent: int
    ueberfaellig_cent: int
    nicht_faellig_cent: int
    faelligkeit_unbekannt_cent: int
    anzahl_konten: int


@dataclass(frozen=True)
class RueckstandsUebersicht:
    objekt_filter: str | None  # None == "Alle Objekte"
    objekt_optionen: tuple[ObjektOption, ...]
    kennzahlen: RueckstandsKennzahlen
    mietkonten: tuple[MietkontoZeile, ...]
    offene_positionen: tuple[OffenePositionZeile, ...]
    mahnfaelle: tuple[MahnfallZeile, ...]
    einheiten_ohne_konto: tuple[EinheitOhneKontoZeile, ...]


def _faelligkeitsklasse(*, faelligkeit_bekannt: bool, faelligkeit: date | None, heute: date) -> str:
    if not faelligkeit_bekannt or faelligkeit is None:
        return "UNBEKANNT"
    return "UEBERFAELLIG" if faelligkeit <= heute else "NICHT_FAELLIG"


def berechne_rueckstandsuebersicht(
    *,
    ctx: AuthContext,
    objekt_id: str | None,
    stammdaten_repository: StammdatenRepository,
    op_service: OPService,
    mahn_fall_repository: MahnFallRepository,
    heute: date | None = None,
) -> RueckstandsUebersicht:
    heute = heute or date.today()

    # Erlaubter Bestand: NUR Gesellschaften, auf die `ctx` Zugriff hat
    # (ADMIN mit `gesellschaft_ids=None` sieht alle), UND NUR nicht
    # ausgeschlossene Objekte - deckt sowohl die "Alle Objekte"-Summen
    # als auch die Filter-Auswahlliste ab, damit beide garantiert
    # denselben Bestand sehen.
    objekt_optionen: list[ObjektOption] = []
    objekt_lookup: dict[str, ObjektOption] = {}
    for gesellschaft in stammdaten_repository.list_gesellschaften():
        if not ctx.has_zugriff(gesellschaft.id):
            continue
        for objekt in stammdaten_repository.list_objekte(gesellschaft_id=gesellschaft.id):
            if objekt.ausgeschlossen:
                continue
            option = ObjektOption(
                id=objekt.id, bezeichnung=objekt.bezeichnung,
                gesellschaft_id=gesellschaft.id, gesellschaft_name=gesellschaft.name,
            )
            objekt_optionen.append(option)
            objekt_lookup[objekt.id] = option
    objekt_optionen.sort(key=lambda o: (o.gesellschaft_name, o.bezeichnung))

    if objekt_id:
        if objekt_id not in objekt_lookup:
            raise UnbekanntesObjektFilterError(
                f"Objekt {objekt_id} ist unbekannt, gehört zu keiner zugänglichen Gesellschaft, oder ist "
                "ausgeschlossen."
            )
        ziel_objekt_ids = [objekt_id]
    else:
        ziel_objekt_ids = [o.id for o in objekt_optionen]

    mietkonten: list[MietkontoZeile] = []
    offene_positionen: list[OffenePositionZeile] = []
    mahnfaelle: list[MahnfallZeile] = []
    einheiten_ohne_konto: list[EinheitOhneKontoZeile] = []
    summe_positiv = 0
    summe_guthaben = 0
    ueberfaellig = 0
    nicht_faellig = 0
    unbekannt = 0
    anzahl_konten = 0

    for oid in ziel_objekt_ids:
        objekt = stammdaten_repository.get_objekt(oid)
        if objekt is None:
            continue
        vertraege = stammdaten_repository.list_vertraege_fuer_objekt(oid)
        einheiten_mit_vertrag: set[str] = set()

        for vertrag in vertraege:
            # Die Einheit gilt ab hier als "belegt" - unabhängig davon,
            # ob der Vertrag gleich darunter wegen einer Dateninkonsistenz
            # übersprungen wird (sie soll dann NICHT fälschlich als
            # "ohne Mietkonto" auftauchen).
            einheiten_mit_vertrag.add(vertrag.einheit_id)

            # Verteidigung gegen inkonsistente Stammdaten: `list_vertraege_
            # fuer_objekt` filtert NUR über Einheit->Objekt, nie über das
            # eigene `gesellschaft_id`-Feld des Vertrags. Ein Vertrag (oder
            # ein davon abgeleitetes Konto), dessen EIGENES gesellschaft_id
            # nicht im ctx-Zugriff liegt, wird deshalb komplett
            # übersprungen - selbst wenn sein Objekt zufällig erlaubt ist.
            if not ctx.has_zugriff(vertrag.gesellschaft_id):
                continue

            einheit = stammdaten_repository.get_einheit(vertrag.einheit_id)
            debitor = stammdaten_repository.get_debitor(vertrag.debitor_id)
            konto = stammdaten_repository.get_konto_by_vertrag(vertrag.id)
            if konto is not None and not ctx.has_zugriff(konto.gesellschaft_id):
                continue
            historisch = vertrag.gueltig_bis is not None and vertrag.gueltig_bis < heute
            sperrgruende = tuple(s.grund for s in stammdaten_repository.aktive_sperren(vertrag.id))

            vertrag_mahnfaelle = mahn_fall_repository.list_fuer_vertrag(vertrag.id)
            for mahnfall in vertrag_mahnfaelle:
                mahnfaelle.append(
                    MahnfallZeile(
                        objekt_id=objekt.id, objekt_bezeichnung=objekt.bezeichnung, vertrag_id=vertrag.id,
                        forderung_op_position_id=mahnfall.forderung_op_position_id, stufe=mahnfall.stufe,
                        status=mahnfall.status, betrag_cent=mahnfall.betrag_cent, geplant_am=mahnfall.geplant_am,
                    )
                )

            saldo_cent: int | None = None
            faelliger_rest: int | None = None
            positionen_faelliger_rest: int | None = None
            positionen_rest_gesamt: int | None = None
            abweichung: int | None = None
            if konto is not None:
                anzahl_konten += 1
                saldo = op_service.berechne_saldo(konto.id, stichtag=heute)
                saldo_cent = saldo.saldo_cent
                faelliger_rest = saldo.faelliger_unstrittiger_rest_cent
                if saldo_cent > 0:
                    summe_positiv += saldo_cent
                elif saldo_cent < 0:
                    summe_guthaben += -saldo_cent

                positionen_faelliger_rest = 0
                positionen_rest_gesamt = 0
                for forderung in op_service.offene_forderungen(konto.id, heute=heute):
                    klasse = _faelligkeitsklasse(
                        faelligkeit_bekannt=forderung.faelligkeit_bekannt, faelligkeit=forderung.faelligkeit, heute=heute
                    )
                    positionen_rest_gesamt += forderung.rest_cent
                    if klasse == "UEBERFAELLIG":
                        ueberfaellig += forderung.rest_cent
                        positionen_faelliger_rest += forderung.rest_cent
                    elif klasse == "NICHT_FAELLIG":
                        nicht_faellig += forderung.rest_cent
                    else:
                        unbekannt += forderung.rest_cent
                    op_position = op_service.get_position(forderung.op_position_id)
                    offene_positionen.append(
                        OffenePositionZeile(
                            objekt_id=objekt.id, objekt_bezeichnung=objekt.bezeichnung, vertrag_id=vertrag.id,
                            debitor_name=debitor.name if debitor else "-", konto_id=konto.id,
                            op_position_id=forderung.op_position_id,
                            beleg_referenz=op_position.beleg_referenz if op_position else "",
                            art=forderung.art, betrag_cent=forderung.betrag_cent, rest_cent=forderung.rest_cent,
                            belegdatum=forderung.belegdatum, leistungsperiode=forderung.leistungsperiode,
                            faelligkeit=forderung.faelligkeit, faelligkeit_bekannt=forderung.faelligkeit_bekannt,
                            faelligkeitsklasse=klasse,
                        )
                    )
                abweichung = saldo_cent - positionen_rest_gesamt

            mietkonten.append(
                MietkontoZeile(
                    objekt_id=objekt.id, objekt_bezeichnung=objekt.bezeichnung, vertrag_id=vertrag.id,
                    einheit_id=vertrag.einheit_id, einheit_bezeichnung=einheit.bezeichnung if einheit else "-",
                    nutzungsstatus=einheit.nutzungsstatus if einheit else "-",
                    debitor_name=debitor.name if debitor else "-", konto_id=konto.id if konto else None,
                    saldo_cent=saldo_cent, faelliger_unstrittiger_rest_cent=faelliger_rest,
                    positionen_faelliger_rest_cent=positionen_faelliger_rest,
                    positionen_rest_gesamt_cent=positionen_rest_gesamt,
                    abweichung_saldo_zu_positionen_cent=abweichung,
                    historisch=historisch, sperrgruende=sperrgruende,
                    mahnfaelle_anzahl=len(vertrag_mahnfaelle),
                )
            )

        # Einheiten ohne (aktiven) Vertrag/Mietkonto sind BESTAND, kein
        # erfundener Nullsaldo - Leerstand/Kurzzeitvermietung/Selfstorage/
        # Eigennutzung erscheinen hier, nie als Mietkonto-Zeile.
        for einheit in stammdaten_repository.list_einheiten_fuer_objekt(oid):
            if einheit.id in einheiten_mit_vertrag:
                continue
            einheiten_ohne_konto.append(
                EinheitOhneKontoZeile(
                    objekt_id=objekt.id, objekt_bezeichnung=objekt.bezeichnung,
                    einheit_id=einheit.id, einheit_bezeichnung=einheit.bezeichnung,
                    nutzungsstatus=einheit.nutzungsstatus,
                )
            )

    mietkonten.sort(key=lambda z: (z.objekt_bezeichnung, z.einheit_bezeichnung, z.vertrag_id))
    offene_positionen.sort(key=lambda z: (z.faelligkeit or date.max, z.belegdatum, z.op_position_id))
    mahnfaelle.sort(key=lambda z: (z.vertrag_id, z.forderung_op_position_id, z.stufe))
    einheiten_ohne_konto.sort(key=lambda z: (z.objekt_bezeichnung, z.einheit_bezeichnung))

    return RueckstandsUebersicht(
        objekt_filter=objekt_id or None,
        objekt_optionen=tuple(objekt_optionen),
        kennzahlen=RueckstandsKennzahlen(
            summe_positiver_kontostaende_cent=summe_positiv,
            summe_guthaben_cent=summe_guthaben,
            ueberfaellig_cent=ueberfaellig,
            nicht_faellig_cent=nicht_faellig,
            faelligkeit_unbekannt_cent=unbekannt,
            anzahl_konten=anzahl_konten,
        ),
        mietkonten=tuple(mietkonten),
        offene_positionen=tuple(offene_positionen),
        mahnfaelle=tuple(mahnfaelle),
        einheiten_ohne_konto=tuple(einheiten_ohne_konto),
    )
