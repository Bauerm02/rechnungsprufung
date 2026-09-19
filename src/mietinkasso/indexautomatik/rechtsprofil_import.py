"""Generischer, validierbarer Quellenimport für Rechtsprofil-Entwürfe UND
Index-Quellenfakten (Auftrag HV-20260919-INDEX-MONATSBERICHT, Umfang C).
Dry-run (`plan`) ist Standard, `apply` verlangt den exakten Plan-Hash aus
einem vorherigen `plan`-Lauf - siehe
`docs/hausverwaltung/IMPORT_RECHTSPROFIL_QUELLENFAKTEN.md` für das
vollständige JSON-Schema und ein Beispiel.

Schreibt AUSSCHLIESSLICH:
- `RechtsprofilTable`-Zeilen mit `status=ENTWURF` (über das bereits
  bestehende `RechtsprofilService.entwurf_anlegen` - KEINE Freigabe,
  KEINE Fachentscheidung durch diesen Import).
- `IndexQuellenFaktenTable`-Zeilen (rein informativ, siehe dortiger
  Docstring - NIE Eingabe für eine echte Berechnung).

Nichts an Soll/Bank/OP, keine Buchung, kein Versand. Beide Zeilenarten
sind inhaltsversioniert: ein Re-Import mit UNVERÄNDERTEM Inhalt erzeugt
KEINE neue Version (Codex-Korrektur: "bereits 34 Drafts vorhanden ...
keine unnötigen dritten identischen Drafts")."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from mietinkasso.auth.service import AuthContext
from mietinkasso.domain.exceptions import MietinkassoError
from mietinkasso.indexautomatik.rechtsprofil import RechtsprofilService
from mietinkasso.indexautomatik.repository import IndexQuellenFaktenRepository, RechtsprofilRepository
from mietinkasso.infrastructure.db.tables import IndexQuellenFaktenTable
from mietinkasso.op.service import compute_content_hash
from mietinkasso.stammdaten.repository import StammdatenRepository


class RechtsprofilImportFehlerError(MietinkassoError):
    """Der Import wird VOR jedem Schreibzugriff abgelehnt - Formatfehler,
    unbekannte `vertrag_id`/Komponenten-IDs, oder ein Plan-Hash, der
    nicht mehr zum aktuellen `--datei`-Inhalt passt (Datei wurde
    zwischenzeitlich geändert)."""


_RECHTSPROFIL_PFLICHTFELDER = (
    "vertrag_id", "rechtsordnung", "ist_wohnungsnutzung", "mrg_zinsbeschraenkung", "ist_altvertrag",
    "ist_hauptmiete", "foerderbindung", "mietzinsobergrenze_cent", "mietzinsobergrenze_quellenbeleg",
    "mietzinsobergrenze_gueltig_bis", "bezugsjahr", "bezugsmonat", "letzte_basis_war_jahresdurchschnitt",
    "basis_komponenten_ids", "vertrag_beleg_referenz", "klausel_referenz",
)
_RECHTSPROFIL_OPTIONALFELDER = {
    "mrg_zinsbeschraenkung_geprueft": False, "foerderbindung_geprueft": False, "historische_basis_belege": {},
    "vpi_reihe": "VPI20C18", "vertraglich_zulaessiger_betrag_cent": None, "vertraglicher_quellenbeleg": None,
    "vertraglicher_fruehestmoeglicher_termin": None, "vertragsklausel_id": None,
    "frist_tage_zugang_bis_wirksamkeit": None, "frist_quellenbeleg": None,
}
_RECHTSPROFIL_DATUMSFELDER = ("mietzinsobergrenze_gueltig_bis", "vertraglicher_fruehestmoeglicher_termin")

_QUELLEN_FAKTEN_OPTIONALFELDER = {
    # Codex-Korrektur: `ist_wohnungsnutzung` fehlte im Import - fail-closed
    # tri-state (`None` = ungeklärt, sperrt genau wie `True` die
    # Gewerbe-Rechenvorschau) wie bei `RechtsprofilTable.ist_hauptmiete`.
    "ist_wohnungsnutzung": None,
    "urspruengliche_klauselbasis": None, "urspruenglicher_indexbetrag_cent": None,
    # Codex-Korrektur (Schlussreview 2d45e27): separater, HEUTE
    # tatsächlich verrechneter Indexanteil - Pflicht für jeden
    # Gesamtvorschlag der Gewerbe-Rechenvorschau (siehe
    # `monatsbericht_service.py::_gewerbe_rechenvorschlag`), NIE
    # identisch mit `urspruenglicher_indexbetrag_cent`, falls
    # zwischenzeitlich bereits (Teil-)Erhöhungen stattfanden.
    "aktueller_indexbetrag_cent": None,
    "betrag_basisbindung_belegt": False, "schwelle_prozent": None, "schwelle_inklusive": None,
    "daempfung_prozent": None, "vertragliche_grenze_prozent": None, "klauselregel_text": None,
    "bestaetigte_gesamtmiete_cent": None, "bestaetigte_gesamtmiete_quelle": None,
    "bestaetigte_gesamtmiete_stichtag": None, "letzte_tatsaechliche_basis_jahr": None,
    "letzte_tatsaechliche_basis_monat": None, "letzte_tatsaechliche_basis_hinweis": None,
    "bereits_enthaltene_erhoehungen_hinweis": None, "pruefhinweis": None, "bedingter_naechster_monat": None,
    "bedingter_fruehester_termin_hinweis": None, "quellenreferenzen": [],
}
_QUELLEN_FAKTEN_DATUMSFELDER = ("bestaetigte_gesamtmiete_stichtag",)


def _parse_datum(wert, feld: str, vertrag_id: str) -> date | None:
    if wert is None:
        return None
    try:
        return date.fromisoformat(wert)
    except (TypeError, ValueError) as exc:
        raise RechtsprofilImportFehlerError(f"{vertrag_id}: Feld '{feld}' ist kein gültiges ISO-Datum ({wert!r}).") from exc


def _pruefe_bool_oder_none(wert, feld: str, vertrag_id: str) -> bool | None:
    """Codex-Korrektur: "Wohnungsflag fehlt fail-closed, string false
    niemals bool(true)" - ein JSON-String `"false"` ist in Python truthy
    (`bool("false") == True`); nur ein tatsächlicher JSON-Bool oder `null`
    wird akzeptiert, alles andere (String, 0/1, ...) wird hart abgelehnt
    statt stillschweigend (falsch) nach bool umgewandelt zu werden."""

    if wert is None or isinstance(wert, bool):
        return wert
    raise RechtsprofilImportFehlerError(
        f"{vertrag_id}: Feld '{feld}' muss ein JSON-Bool (true/false) oder null sein, kein "
        f"{type(wert).__name__} ({wert!r})."
    )


def _pruefe_bool(wert, feld: str, vertrag_id: str) -> bool:
    ergebnis = _pruefe_bool_oder_none(wert, feld, vertrag_id)
    if ergebnis is None:
        raise RechtsprofilImportFehlerError(f"{vertrag_id}: Feld '{feld}' darf nicht null sein (true/false erforderlich).")
    return ergebnis


def _pruefe_int_oder_none(wert, feld: str, vertrag_id: str) -> int | None:
    if wert is None:
        return None
    if isinstance(wert, bool) or not isinstance(wert, int):
        raise RechtsprofilImportFehlerError(
            f"{vertrag_id}: Feld '{feld}' muss eine ganze Zahl (int, Cent) oder null sein, kein "
            f"{type(wert).__name__} ({wert!r})."
        )
    return wert


def _pruefe_decimal_oder_none(wert, feld: str, vertrag_id: str, *, nur_positiv: bool = False) -> Decimal | None:
    """Codex-Korrektur: "alle Typen/Decimal-endlich/Basis>0 ... explizit
    validieren" - lehnt Bool (Python-`bool` ist eine `int`-Unterklasse),
    NaN/Infinity und (bei `nur_positiv`) einen Basiswert <= 0 explizit ab,
    statt eine schwer nachvollziehbare spätere Falschrechnung zuzulassen."""

    if wert is None:
        return None
    if isinstance(wert, bool) or not isinstance(wert, (int, float, str, Decimal)):
        raise RechtsprofilImportFehlerError(
            f"{vertrag_id}: Feld '{feld}' hat einen unzulässigen Zahlentyp ({type(wert).__name__}: {wert!r})."
        )
    try:
        dezimal = Decimal(str(wert))
    except (InvalidOperation, ValueError) as exc:
        raise RechtsprofilImportFehlerError(f"{vertrag_id}: Feld '{feld}' ist keine gültige Zahl ({wert!r}).") from exc
    if not dezimal.is_finite():
        raise RechtsprofilImportFehlerError(f"{vertrag_id}: Feld '{feld}' muss endlich sein (NaN/Infinity abgelehnt).")
    if nur_positiv and dezimal <= 0:
        raise RechtsprofilImportFehlerError(f"{vertrag_id}: Feld '{feld}' muss > 0 sein (erhalten: {dezimal}).")
    return dezimal


@dataclass(frozen=True)
class ImportPaket:
    quelle: str
    rechtsprofile: list[dict] = field(default_factory=list)
    quellen_fakten: list[dict] = field(default_factory=list)


def parse_json_paket(text: str) -> ImportPaket:
    try:
        rohdaten = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RechtsprofilImportFehlerError(f"Datei ist kein gültiges JSON: {exc}") from exc
    if not isinstance(rohdaten, dict) or not (rohdaten.get("quelle") or "").strip():
        raise RechtsprofilImportFehlerError("Paket ohne nicht-leeres 'quelle'-Feld wird abgelehnt (Provenienzpflicht).")
    rechtsprofile = rohdaten.get("rechtsprofile", [])
    quellen_fakten = rohdaten.get("quellen_fakten", [])
    if not isinstance(rechtsprofile, list) or not isinstance(quellen_fakten, list):
        raise RechtsprofilImportFehlerError("'rechtsprofile'/'quellen_fakten' müssen Listen sein.")
    if not rechtsprofile and not quellen_fakten:
        raise RechtsprofilImportFehlerError("Paket ohne jede Zeile ('rechtsprofile'/'quellen_fakten' beide leer).")
    return ImportPaket(quelle=rohdaten["quelle"], rechtsprofile=rechtsprofile, quellen_fakten=quellen_fakten)


@dataclass(frozen=True)
class GeplanteRechtsprofilZeile:
    vertrag_id: str
    felder: dict
    inhalt_hash: str
    aktion: str  # "ANLEGEN" | "UNVERAENDERT_UEBERSPRUNGEN"


@dataclass(frozen=True)
class GeplanteQuellenFaktenZeile:
    vertrag_id: str
    felder: dict
    inhalt_hash: str
    aktion: str  # "ANLEGEN" | "UNVERAENDERT_UEBERSPRUNGEN"


@dataclass(frozen=True)
class ImportPlan:
    paket_hash: str
    quelle: str
    rechtsprofil_zeilen: tuple[GeplanteRechtsprofilZeile, ...]
    quellen_fakten_zeilen: tuple[GeplanteQuellenFaktenZeile, ...]

    @property
    def anzahl_anzulegen(self) -> int:
        return sum(1 for z in self.rechtsprofil_zeilen if z.aktion == "ANLEGEN") + sum(
            1 for z in self.quellen_fakten_zeilen if z.aktion == "ANLEGEN"
        )


def _rechtsprofil_felder_aus_zeile(zeile: dict, index: int) -> dict:
    vertrag_id = zeile.get("vertrag_id")
    if not isinstance(vertrag_id, str) or not vertrag_id.strip():
        raise RechtsprofilImportFehlerError(f"rechtsprofile[{index}]: 'vertrag_id' fehlt oder ist leer.")
    for pflichtfeld in _RECHTSPROFIL_PFLICHTFELDER:
        if pflichtfeld not in zeile:
            raise RechtsprofilImportFehlerError(f"{vertrag_id}: Pflichtfeld '{pflichtfeld}' fehlt in rechtsprofile[{index}].")
    felder = {k: zeile[k] for k in _RECHTSPROFIL_PFLICHTFELDER}
    for optionalfeld, default in _RECHTSPROFIL_OPTIONALFELDER.items():
        felder[optionalfeld] = zeile.get(optionalfeld, default)
    for datumsfeld in _RECHTSPROFIL_DATUMSFELDER:
        felder[datumsfeld] = _parse_datum(felder[datumsfeld], datumsfeld, vertrag_id)
    if not isinstance(felder["basis_komponenten_ids"], list):
        raise RechtsprofilImportFehlerError(f"{vertrag_id}: 'basis_komponenten_ids' muss eine Liste sein.")
    return felder


def _quellen_fakten_felder_aus_zeile(zeile: dict, index: int) -> dict:
    """Codex-Korrektur: "Import bitte alle Typen/Decimal-endlich/Basis>0/
    Cent-int/bool explizit validieren. Wohnungsflag fehlt fail-closed,
    string false niemals bool(true)." Alle sicherheitsrelevanten Felder
    werden HIER strikt typgeprüft (nicht erst beim Schreiben) - `felder`
    enthält danach bereits geparste `Decimal`/`bool`/`int`-Werte, keine
    rohen JSON-Primitive mehr."""

    vertrag_id = zeile.get("vertrag_id")
    if not isinstance(vertrag_id, str) or not vertrag_id.strip():
        raise RechtsprofilImportFehlerError(f"quellen_fakten[{index}]: 'vertrag_id' fehlt oder ist leer.")
    felder = {"vertrag_id": vertrag_id}
    for optionalfeld, default in _QUELLEN_FAKTEN_OPTIONALFELDER.items():
        felder[optionalfeld] = zeile.get(optionalfeld, default)
    for datumsfeld in _QUELLEN_FAKTEN_DATUMSFELDER:
        felder[datumsfeld] = _parse_datum(felder[datumsfeld], datumsfeld, vertrag_id)

    felder["ist_wohnungsnutzung"] = _pruefe_bool_oder_none(felder["ist_wohnungsnutzung"], "ist_wohnungsnutzung", vertrag_id)
    felder["betrag_basisbindung_belegt"] = _pruefe_bool(
        felder["betrag_basisbindung_belegt"], "betrag_basisbindung_belegt", vertrag_id
    )
    felder["schwelle_inklusive"] = _pruefe_bool_oder_none(felder["schwelle_inklusive"], "schwelle_inklusive", vertrag_id)

    felder["urspruenglicher_indexbetrag_cent"] = _pruefe_int_oder_none(
        felder["urspruenglicher_indexbetrag_cent"], "urspruenglicher_indexbetrag_cent", vertrag_id
    )
    if felder["urspruenglicher_indexbetrag_cent"] is not None and felder["urspruenglicher_indexbetrag_cent"] < 0:
        raise RechtsprofilImportFehlerError(f"{vertrag_id}: 'urspruenglicher_indexbetrag_cent' ist negativ.")
    felder["aktueller_indexbetrag_cent"] = _pruefe_int_oder_none(
        felder["aktueller_indexbetrag_cent"], "aktueller_indexbetrag_cent", vertrag_id
    )
    if felder["aktueller_indexbetrag_cent"] is not None and felder["aktueller_indexbetrag_cent"] < 0:
        raise RechtsprofilImportFehlerError(f"{vertrag_id}: 'aktueller_indexbetrag_cent' ist negativ.")
    felder["bestaetigte_gesamtmiete_cent"] = _pruefe_int_oder_none(
        felder["bestaetigte_gesamtmiete_cent"], "bestaetigte_gesamtmiete_cent", vertrag_id
    )
    if felder["bestaetigte_gesamtmiete_cent"] is not None and felder["bestaetigte_gesamtmiete_cent"] < 0:
        raise RechtsprofilImportFehlerError(f"{vertrag_id}: 'bestaetigte_gesamtmiete_cent' ist negativ.")

    felder["schwelle_prozent"] = _pruefe_decimal_oder_none(felder["schwelle_prozent"], "schwelle_prozent", vertrag_id)
    felder["daempfung_prozent"] = _pruefe_decimal_oder_none(felder["daempfung_prozent"], "daempfung_prozent", vertrag_id)
    felder["vertragliche_grenze_prozent"] = _pruefe_decimal_oder_none(
        felder["vertragliche_grenze_prozent"], "vertragliche_grenze_prozent", vertrag_id
    )

    klauselbasis = felder["urspruengliche_klauselbasis"]
    if klauselbasis is not None:
        if not isinstance(klauselbasis, dict):
            raise RechtsprofilImportFehlerError(
                f"{vertrag_id}: 'urspruengliche_klauselbasis' muss ein Objekt {{reihe,monat,wert}} sein."
            )
        # "Basis>0": eine ursprüngliche Klauselbasis von 0 oder negativ
        # wäre eine Division-durch-0/eine sinnlose Referenzbasis in jeder
        # nachgelagerten Veränderungsberechnung (`_gewerbe_rechenvorschlag`).
        klauselbasis = dict(klauselbasis)
        klauselbasis["wert"] = _pruefe_decimal_oder_none(
            klauselbasis.get("wert"), "urspruengliche_klauselbasis.wert", vertrag_id, nur_positiv=True
        )
        felder["urspruengliche_klauselbasis"] = klauselbasis

    if felder["bedingter_naechster_monat"] is not None and not (1 <= felder["bedingter_naechster_monat"] <= 12):
        raise RechtsprofilImportFehlerError(f"{vertrag_id}: 'bedingter_naechster_monat' muss zwischen 1 und 12 liegen.")
    if not isinstance(felder["quellenreferenzen"], list):
        raise RechtsprofilImportFehlerError(f"{vertrag_id}: 'quellenreferenzen' muss eine Liste sein.")
    return felder


def erstelle_plan(
    paket: ImportPaket, *, stammdaten_repository: StammdatenRepository,
    rechtsprofil_repository: RechtsprofilRepository, quellen_fakten_repository: IndexQuellenFaktenRepository,
) -> ImportPlan:
    """Rein lesend - prüft Referenzen (Vertrag existiert, referenzierte
    Basis-Komponenten existieren und gehören zum Vertrag) UND vergleicht
    den Inhalts-Hash jeder Zeile gegen ALLE bereits vorhandenen Versionen
    (Codex-Korrektur: kein Duplikat bei unverändertem Re-Import). Schreibt
    NICHTS - der zurückgegebene `paket_hash` bindet `apply()` an exakt
    diesen geprüften Stand."""

    rechtsprofil_geplant: list[GeplanteRechtsprofilZeile] = []
    gesehene_vertrag_ids: set[str] = set()
    for index, roh_zeile in enumerate(paket.rechtsprofile):
        felder = _rechtsprofil_felder_aus_zeile(roh_zeile, index)
        vertrag_id = felder["vertrag_id"]
        if vertrag_id in gesehene_vertrag_ids:
            raise RechtsprofilImportFehlerError(f"{vertrag_id}: mehrfach in 'rechtsprofile' desselben Pakets - nicht eindeutig.")
        gesehene_vertrag_ids.add(vertrag_id)
        vertrag = stammdaten_repository.get_vertrag(vertrag_id)
        if vertrag is None:
            raise RechtsprofilImportFehlerError(f"{vertrag_id}: unbekannter Vertrag.")
        for komponente_id in felder["basis_komponenten_ids"]:
            komponente = stammdaten_repository.get_komponente(komponente_id)
            if komponente is None or komponente.vertrag_id != vertrag_id:
                raise RechtsprofilImportFehlerError(
                    f"{vertrag_id}: Basis-Komponente '{komponente_id}' existiert nicht oder gehört nicht zu diesem Vertrag."
                )
        if felder["vertragsklausel_id"] is not None and felder["vertraglich_zulaessiger_betrag_cent"] is not None:
            raise RechtsprofilImportFehlerError(
                f"{vertrag_id}: 'vertragsklausel_id' und 'vertraglich_zulaessiger_betrag_cent' schließen sich aus."
            )
        inhalt_hash = compute_content_hash(felder)
        aktion = (
            "UNVERAENDERT_UEBERSPRUNGEN"
            if rechtsprofil_repository.existiert_import_inhalt_bereits(vertrag_id, inhalt_hash)
            else "ANLEGEN"
        )
        rechtsprofil_geplant.append(
            GeplanteRechtsprofilZeile(vertrag_id=vertrag_id, felder=felder, inhalt_hash=inhalt_hash, aktion=aktion)
        )

    quellen_fakten_geplant: list[GeplanteQuellenFaktenZeile] = []
    gesehene_vertrag_ids_qf: set[str] = set()
    for index, roh_zeile in enumerate(paket.quellen_fakten):
        felder = _quellen_fakten_felder_aus_zeile(roh_zeile, index)
        vertrag_id = felder["vertrag_id"]
        if vertrag_id in gesehene_vertrag_ids_qf:
            raise RechtsprofilImportFehlerError(f"{vertrag_id}: mehrfach in 'quellen_fakten' desselben Pakets - nicht eindeutig.")
        gesehene_vertrag_ids_qf.add(vertrag_id)
        if stammdaten_repository.get_vertrag(vertrag_id) is None:
            raise RechtsprofilImportFehlerError(f"{vertrag_id}: unbekannter Vertrag.")
        inhalt_hash = compute_content_hash(felder)
        aktion = (
            "UNVERAENDERT_UEBERSPRUNGEN"
            if quellen_fakten_repository.existiert_inhalt_bereits(vertrag_id, inhalt_hash)
            else "ANLEGEN"
        )
        quellen_fakten_geplant.append(
            GeplanteQuellenFaktenZeile(vertrag_id=vertrag_id, felder=felder, inhalt_hash=inhalt_hash, aktion=aktion)
        )

    paket_hash = compute_content_hash(
        {
            "quelle": paket.quelle,
            "rechtsprofile": [(z.vertrag_id, z.inhalt_hash) for z in rechtsprofil_geplant],
            "quellen_fakten": [(z.vertrag_id, z.inhalt_hash) for z in quellen_fakten_geplant],
        }
    )
    return ImportPlan(
        paket_hash=paket_hash, quelle=paket.quelle,
        rechtsprofil_zeilen=tuple(rechtsprofil_geplant), quellen_fakten_zeilen=tuple(quellen_fakten_geplant),
    )


def wende_an(
    plan: ImportPlan, *, bestaetige_hash: str, ctx: AuthContext, akteur: str,
    rechtsprofil_service: RechtsprofilService, rechtsprofil_repository: RechtsprofilRepository,
    quellen_fakten_repository: IndexQuellenFaktenRepository,
) -> dict:
    """Verlangt den exakten `paket_hash` aus einem vorherigen `plan()`-
    Lauf - ein geänderter `--datei`-Inhalt zwischen `plan` und `apply`
    erzeugt einen anderen Hash und wird abgelehnt, statt auf einem
    veralteten Stand zu schreiben. Legt NUR Zeilen mit Aktion "ANLEGEN"
    an; "UNVERAENDERT_UEBERSPRUNGEN" bleibt ein reiner Zähler.

    Die `quellen_fakten`-Zeilen werden ALLE zusammen erst in einem
    einzigen `anlegen_batch`-Aufruf geschrieben (Codex-Korrektur: "bei
    reinem quellen_fakten-Batch wenigstens atomar schreiben; keine halben
    17 Datensätze") - ein Fehler beim Bauen einer Zeile lässt keine
    einzige davon in der Datenbank landen. Für die `rechtsprofile`-Zeilen
    bleibt dagegen bewusst KEIN Vollrollback über den gesamten Batch
    hinweg bestehen (dokumentierte Grenze - siehe OFFENE_PUNKTE.md): jede
    über `RechtsprofilService.entwurf_anlegen` angelegte ENTWURF-Zeile
    committet für sich, da dieser bestehende Service selbst je Aufruf
    committet. Das ist unschädlich, da beide Zeilenarten inert sind
    (kein Effekt auf Soll/Bank/OP), bis ein Mensch eine Freigabe erteilt."""

    if bestaetige_hash != plan.paket_hash:
        raise RechtsprofilImportFehlerError(
            f"--bestaetige-hash ({bestaetige_hash}) stimmt nicht mit dem aktuellen Plan-Hash ({plan.paket_hash}) "
            "überein - Datei wurde vermutlich seit dem letzten 'plan'-Lauf geändert. Bitte erneut planen."
        )

    rechtsprofile_angelegt = 0
    rechtsprofile_uebersprungen = 0
    for zeile in plan.rechtsprofil_zeilen:
        if zeile.aktion == "UNVERAENDERT_UEBERSPRUNGEN":
            rechtsprofile_uebersprungen += 1
            continue
        angelegt = rechtsprofil_service.entwurf_anlegen(ctx=ctx, erstellt_von=akteur, **zeile.felder)
        rechtsprofil_repository.setze_import_provenienz(angelegt.id, quelle=plan.quelle, inhalt_hash=zeile.inhalt_hash)
        rechtsprofile_angelegt += 1

    # Codex-Korrektur: "Bei reinem quellen_fakten-Batch wenigstens atomar
    # schreiben; keine halben 17 Datensätze" - ALLE Zeilen dieses Laufs
    # werden erst vollständig im Speicher gebaut und dann in EINEM Aufruf
    # (`anlegen_batch`, eine Transaktion) geschrieben, statt je Zeile
    # einzeln zu committen.
    quellen_fakten_uebersprungen = 0
    neue_quellen_fakten_zeilen: list[IndexQuellenFaktenTable] = []
    for zeile in plan.quellen_fakten_zeilen:
        if zeile.aktion == "UNVERAENDERT_UEBERSPRUNGEN":
            quellen_fakten_uebersprungen += 1
            continue
        felder = zeile.felder
        klauselbasis = felder["urspruengliche_klauselbasis"] or {}
        version = quellen_fakten_repository.naechste_version(zeile.vertrag_id)
        neue_quellen_fakten_zeilen.append(
            IndexQuellenFaktenTable(
                vertrag_id=zeile.vertrag_id, version=version, quelle=plan.quelle, inhalt_hash=zeile.inhalt_hash,
                ist_wohnungsnutzung=felder["ist_wohnungsnutzung"],
                urspruengliche_klauselbasis_reihe=klauselbasis.get("reihe"),
                urspruengliche_klauselbasis_monat=klauselbasis.get("monat"),
                urspruengliche_klauselbasis_wert=(
                    str(klauselbasis["wert"]) if klauselbasis.get("wert") is not None else None
                ),
                urspruenglicher_indexbetrag_cent=felder["urspruenglicher_indexbetrag_cent"],
                aktueller_indexbetrag_cent=felder["aktueller_indexbetrag_cent"],
                betrag_basisbindung_belegt=felder["betrag_basisbindung_belegt"],
                schwelle_prozent=(str(felder["schwelle_prozent"]) if felder["schwelle_prozent"] is not None else None),
                schwelle_inklusive=felder["schwelle_inklusive"],
                daempfung_prozent=(str(felder["daempfung_prozent"]) if felder["daempfung_prozent"] is not None else None),
                vertragliche_grenze_prozent=(
                    str(felder["vertragliche_grenze_prozent"]) if felder["vertragliche_grenze_prozent"] is not None else None
                ),
                klauselregel_text=felder["klauselregel_text"],
                bestaetigte_gesamtmiete_cent=felder["bestaetigte_gesamtmiete_cent"],
                bestaetigte_gesamtmiete_quelle=felder["bestaetigte_gesamtmiete_quelle"],
                bestaetigte_gesamtmiete_stichtag=felder["bestaetigte_gesamtmiete_stichtag"],
                letzte_tatsaechliche_basis_jahr=felder["letzte_tatsaechliche_basis_jahr"],
                letzte_tatsaechliche_basis_monat=felder["letzte_tatsaechliche_basis_monat"],
                letzte_tatsaechliche_basis_hinweis=felder["letzte_tatsaechliche_basis_hinweis"],
                bereits_enthaltene_erhoehungen_hinweis=felder["bereits_enthaltene_erhoehungen_hinweis"],
                pruefhinweis=felder["pruefhinweis"],
                bedingter_naechster_monat=felder["bedingter_naechster_monat"],
                bedingter_fruehester_termin_hinweis=felder["bedingter_fruehester_termin_hinweis"],
                quellenreferenzen=list(felder["quellenreferenzen"]),
                erstellt_von=akteur,
            )
        )
    quellen_fakten_repository.anlegen_batch(neue_quellen_fakten_zeilen)
    quellen_fakten_angelegt = len(neue_quellen_fakten_zeilen)

    return {
        "rechtsprofile_angelegt": rechtsprofile_angelegt,
        "rechtsprofile_unveraendert_uebersprungen": rechtsprofile_uebersprungen,
        "quellen_fakten_angelegt": quellen_fakten_angelegt,
        "quellen_fakten_unveraendert_uebersprungen": quellen_fakten_uebersprungen,
    }
