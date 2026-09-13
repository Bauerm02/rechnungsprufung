"""Zeilentypen des generischen Echtbetrieb-Intakes (Auftrag
HV-20260912-ECHTBETRIEB). Siehe `docs/hausverwaltung/IMPORT_VERTRAG.md`
für den vollständigen, verbindlichen Vertrag (Feldschema, Beispiel,
CLI-Befehle) - dieses Modul implementiert exakt diesen Vertrag.

Diese Dataclasses sind reine, geparste Rohdaten (Typen/Pflichtfelder
geprüft) - fachliche Prüfungen (Referenzen lösen sich auf, Objekt 107,
Konflikte, `quelle_bestaetigt`) passieren erst in `planner.py`."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class GesellschaftZeile:
    id: str
    name: str


@dataclass(frozen=True)
class ObjektZeile:
    id: str
    gesellschaft_id: str
    bezeichnung: str
    adresse: str | None = None
    ausgeschlossen: bool = False


@dataclass(frozen=True)
class EinheitZeile:
    id: str
    objekt_id: str
    bezeichnung: str
    nutzungsstatus: str
    flaeche_qm: Decimal | None = None
    miteigentumsanteile: Decimal | None = None


@dataclass(frozen=True)
class DebitorZeile:
    id: str
    name: str
    email: str | None = None
    adresse: str | None = None


@dataclass(frozen=True)
class VertragZeile:
    id: str
    einheit_id: str
    debitor_id: str
    gesellschaft_id: str
    rechtsordnung: str
    gueltig_von: date
    gueltig_bis: date | None = None
    faelligkeit_tag: int = 5
    zahlungsfrist_tage: int = 14


@dataclass(frozen=True)
class EroeffnungZeile:
    import_id: str
    vertrag_id: str
    modus: str
    betrag_cent: int
    stichtag: date
    quelle_bestaetigt: bool
    typ: str | None = None
    belegdatum: date | None = None
    faelligkeit: date | None = None
    beleg_referenz: str = "Eröffnungsimport"


@dataclass(frozen=True)
class NachbuchungZeile:
    import_id: str
    vertrag_id: str
    typ: str
    betrag_cent: int
    belegdatum: date
    buchungsdatum: date
    faelligkeit: date | None = None
    beleg_referenz: str = ""
    aenderungsgrund: str | None = None
    leistungsperiode: str | None = None


@dataclass(frozen=True)
class EroeffnungskorrekturZeile:
    """Schmaler Sonderfall (siehe `op/service.py::eroeffnungskorrektur_buchen`):
    ein Posten, den der bestätigte Eröffnungs-Gesamtsaldo nachweislich NICHT
    enthält, weil sein tatsächliches Datum (`original_belegdatum`) vor/auf
    dem Eröffnungsstichtag liegt. `buchungsdatum` wird NICHT aus der Datei
    übernommen, sondern ist immer der Übernahmetag (Zeitpunkt des Applys) -
    siehe `intake/apply.py::wende_an`."""

    import_id: str
    vertrag_id: str
    typ: str
    betrag_cent: int
    original_belegdatum: date
    grund: str
    quelle_referenz: str
    beleg_referenz: str = "Eröffnungskorrektur"


@dataclass(frozen=True)
class SperreZeile:
    """Optionaler, dauerhaft gespeicherter Prüfhinweis/Sperrgrund je
    Vertrag (z. B. RECHTSANWALT/RATENPLAN/MANUELL) - nutzt dieselbe
    `SperreTable`, die `mahnwesen/service.py::plane_forderung`/`versenden`
    bereits als harte Mahnsperre auswertet; kein neuer Sperrmechanismus."""

    vertrag_id: str
    grund: str
    kommentar: str | None = None


@dataclass(frozen=True)
class KomponenteZeile:
    """Optionale, zeitlich begrenzte Vertragskomponente (HMZ/Küche/
    Parkplatz/BK-VZ/...) - nutzt die bestehende `VertragsKomponenteTable`/
    `StammdatenRepository.add_komponente`. `indexierbar` ist nur ein
    Eignungsflag für `index/service.py`; das Einspielen einer Komponente
    löst FÜR SICH GENOMMEN keine Indexklausel/-freigabe aus."""

    id: str
    vertrag_id: str
    art: str
    bezeichnung: str
    betrag_cent: int
    gueltig_von: date
    ust_satz_promille: int = 10000
    indexierbar: bool = False
    gueltig_bis: date | None = None


@dataclass(frozen=True)
class KautionZeile:
    """Bestätigter Kautionsbestand (Auftrag HV-20260913-VERTRAGSANLAGE) -
    nutzt die bestehende `KautionTable`/`StammdatenRepository.set_kaution`
    (ID-Konvention `KAU-{vertrag_id}`, wie im Seed-Skript). STRIKTE
    Idempotenz wie die übrigen Stammdaten (abweichender Inhalt =
    Konflikt, gesamter Lauf verweigert) - eine Kaution ist ein
    bestätigter Fakt, keine laufend fortzuschreibende Angabe wie
    `MietvertragsprofilZeile`. AUSDRÜCKLICH der TATSÄCHLICH eingegangene
    Betrag - NICHT der vertraglich vereinbarte (siehe dort)."""

    vertrag_id: str
    betrag_cent: int
    stichtag: date
    referenz: str | None = None


@dataclass(frozen=True)
class MietvertragsprofilZeile:
    """Zusätzliche, VERSIONIERTE Verwaltungs-/Anzeigefelder je Vertrag
    (Auftrag HV-20260913-VERTRAGSANLAGE) - siehe `MietvertragsprofilTable`-
    Docstring in `infrastructure/db/tables.py` für die vollständige
    Begründung (insbesondere `urspruenglicher_mietbeginn` vs.
    `VertragZeile.gueltig_von`, `vertragliche_kaution_cent` vs.
    `KautionZeile.betrag_cent`). Abweichend von der strikten Stammdaten-
    Konfliktregel erzeugt ein abweichender Inhalt hier KEINEN Konflikt,
    sondern eine neue, sichtbare Version (siehe `planner.py`)."""

    vertrag_id: str
    nutzungsart: str = "UNGEKLAERT"
    urspruenglicher_mietbeginn: date | None = None
    verwaltungsuebernahme_am: date | None = None
    verwaltung_bezeichnung: str | None = None
    vertragliche_kaution_cent: int | None = None
    vertragliche_kaution_quellenbeleg: str | None = None
    # None = unbekannt/kein Fund; 0 = ausdrücklich belegte "keine Gebühr".
    # NIEMALS aus einem fehlenden Treffer eine 0 erfinden (siehe Tabellen-Docstring).
    mahngebuehr_cent: int | None = None
    mahngebuehr_quellenbeleg: str | None = None
    # Ausdrücklich unverbindliche Index-QUELLFELDER (Gedächtnisstütze/Vorausfüllung
    # für `index/service.py::klausel_anlegen`) - erzeugen NIE automatisch eine
    # `IndexKlauselTable`-Zeile/Freigabe/Sollstellung. Immer getrennt von einer
    # tatsächlich wirksamen Klausel oder einem Rekonstruktionsmodell anzuzeigen.
    index_reihe: str | None = None
    index_urspruenglicher_basismonat: str | None = None
    index_urspruenglicher_basiswert: Decimal | None = None
    index_schwelle_prozent: Decimal | None = None
    index_schwelle_inklusive: bool | None = None
    index_anpassungsmonat: int | None = None
    index_mindestintervall_monate: int | None = None
    index_klauseltext_auszug: str | None = None
    index_klauseltext_seite: int | None = None
    quelle_typ: str = "IMPORT_SCHEMA"
    quelle_referenz: str | None = None


@dataclass(frozen=True)
class IntakePaket:
    """Vollständig geparstes, aber noch NICHT fachlich geprüftes Paket -
    egal ob aus einer JSON-Datei oder einem CSV-Bündel geparst, identisch
    strukturiert (siehe `parser.py`)."""

    quelle: str
    gesellschaften: tuple[GesellschaftZeile, ...] = field(default_factory=tuple)
    objekte: tuple[ObjektZeile, ...] = field(default_factory=tuple)
    einheiten: tuple[EinheitZeile, ...] = field(default_factory=tuple)
    debitoren: tuple[DebitorZeile, ...] = field(default_factory=tuple)
    vertraege: tuple[VertragZeile, ...] = field(default_factory=tuple)
    eroeffnungen: tuple[EroeffnungZeile, ...] = field(default_factory=tuple)
    nachbuchungen: tuple[NachbuchungZeile, ...] = field(default_factory=tuple)
    eroeffnungskorrekturen: tuple[EroeffnungskorrekturZeile, ...] = field(default_factory=tuple)
    sperren: tuple[SperreZeile, ...] = field(default_factory=tuple)
    komponenten: tuple[KomponenteZeile, ...] = field(default_factory=tuple)
    kautionen: tuple[KautionZeile, ...] = field(default_factory=tuple)
    mietvertragsprofile: tuple[MietvertragsprofilZeile, ...] = field(default_factory=tuple)


def paket_hash(paket: IntakePaket) -> str:
    """Deterministischer Hash über den GESAMTEN geparsten Inhalt (alle
    Zeilen, alle Felder, ursprüngliche Reihenfolge) - unabhängig davon,
    ob das Paket aus JSON oder einem CSV-Bündel geparst wurde. `apply()`
    verlangt genau diesen Hash aus dem vorherigen `plan()`-Lauf als
    Bestätigung ("bindet sich an identischen Inhalt"); ändert sich auch
    nur ein Feld einer einzigen Zeile, ändert sich dieser Hash."""

    rohdaten = dataclasses.asdict(paket)
    kanonisch = json.dumps(rohdaten, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(kanonisch.encode("utf-8")).hexdigest()
