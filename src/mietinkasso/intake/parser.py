"""Parst ein Intake-Paket aus JSON oder einem CSV-Bündel in ein
`IntakePaket` (siehe `docs/hausverwaltung/IMPORT_VERTRAG.md` für den
verbindlichen Feldvertrag). Reine Struktur-/Typprüfung - fachliche
Prüfungen (Referenzen, Objekt 107, Konflikte) passieren in `planner.py`.

Beide Formate erzeugen identisch strukturierte `IntakePaket`-Objekte."""

from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from mietinkasso.domain.exceptions import MietinkassoError
from mietinkasso.intake.schema import (
    DebitorZeile,
    EinheitZeile,
    EroeffnungskorrekturZeile,
    EroeffnungZeile,
    GesellschaftZeile,
    IntakePaket,
    KautionZeile,
    KomponenteZeile,
    MietvertragsprofilZeile,
    NachbuchungZeile,
    ObjektZeile,
    SperreZeile,
    VertragZeile,
)

#: Erwartete Dateinamen eines CSV-Bündels (siehe IMPORT_VERTRAG.md).
CSV_BUENDEL_DATEINAMEN = (
    "gesellschaften",
    "objekte",
    "einheiten",
    "debitoren",
    "vertraege",
    "eroeffnungen",
    "nachbuchungen",
    "eroeffnungskorrekturen",
    "sperren",
    "komponenten",
    "kautionen",
    "mietvertragsprofile",
)


class IntakeFormatFehlerError(MietinkassoError):
    """Die Intake-Datei(en) entsprechen strukturell nicht dem
    verbindlichen Feldvertrag (fehlendes Pflichtfeld, falscher Typ,
    ungültiges JSON/CSV) - wird abgelehnt statt spekulativ mit Lücken
    weiterverarbeitet."""


def _pflicht_str(zeile: dict, feld: str, kontext: str) -> str:
    wert = zeile.get(feld)
    if wert is None or (isinstance(wert, str) and not wert.strip()):
        raise IntakeFormatFehlerError(f"{kontext}: Pflichtfeld '{feld}' fehlt oder ist leer.")
    return str(wert).strip()


def _optional_str(zeile: dict, feld: str) -> str | None:
    wert = zeile.get(feld)
    if wert is None:
        return None
    text = str(wert).strip()
    return text or None


def _pflicht_bool(zeile: dict, feld: str, kontext: str) -> bool:
    wert = zeile.get(feld)
    if isinstance(wert, bool):
        return wert
    if isinstance(wert, str):
        normalisiert = wert.strip().lower()
        if normalisiert in ("true", "1", "ja"):
            return True
        if normalisiert in ("false", "0", "nein", ""):
            return False
    if wert is None:
        return False
    raise IntakeFormatFehlerError(f"{kontext}: Feld '{feld}' ist kein gültiger Wahrheitswert ('{wert}').")


def _optional_bool(zeile: dict, feld: str, *, default: bool) -> bool:
    if feld not in zeile or zeile.get(feld) in (None, ""):
        return default
    return _pflicht_bool(zeile, feld, feld)


def _optional_bool_oder_none(zeile: dict, feld: str) -> bool | None:
    """Tri-state: None = nicht bestimmt/unbekannt, sonst echter Wahrheitswert.
    Für Felder wie `index_schwelle_inklusive`, bei denen ein fehlender Fund
    NICHT mit einem konkreten True/False verwechselt werden darf."""
    if feld not in zeile or zeile.get(feld) in (None, ""):
        return None
    return _pflicht_bool(zeile, feld, feld)


def _pflicht_int(zeile: dict, feld: str, kontext: str) -> int:
    wert = zeile.get(feld)
    try:
        return int(str(wert).strip())
    except (TypeError, ValueError) as exc:
        raise IntakeFormatFehlerError(f"{kontext}: Feld '{feld}' ist keine gültige Ganzzahl ('{wert}').") from exc


def _optional_int(zeile: dict, feld: str, *, default: int) -> int:
    if feld not in zeile or zeile.get(feld) in (None, ""):
        return default
    return _pflicht_int(zeile, feld, feld)


def _pflicht_datum(zeile: dict, feld: str, kontext: str) -> date:
    wert = zeile.get(feld)
    try:
        return datetime.strptime(str(wert).strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise IntakeFormatFehlerError(
            f"{kontext}: Feld '{feld}' ist kein gültiges Datum im Format JJJJ-MM-TT ('{wert}')."
        ) from exc


def _optional_datum(zeile: dict, feld: str) -> date | None:
    wert = zeile.get(feld)
    if wert is None or (isinstance(wert, str) and not wert.strip()):
        return None
    return _pflicht_datum(zeile, feld, feld)


def _optional_decimal(zeile: dict, feld: str, kontext: str) -> Decimal | None:
    wert = zeile.get(feld)
    if wert is None or (isinstance(wert, str) and not wert.strip()):
        return None
    try:
        return Decimal(str(wert).strip())
    except InvalidOperation as exc:
        raise IntakeFormatFehlerError(f"{kontext}: Feld '{feld}' ist keine gültige Dezimalzahl ('{wert}').") from exc


def _gesellschaft_aus_dict(zeile: dict) -> GesellschaftZeile:
    return GesellschaftZeile(id=_pflicht_str(zeile, "id", "Gesellschaft"), name=_pflicht_str(zeile, "name", "Gesellschaft"))


def _objekt_aus_dict(zeile: dict) -> ObjektZeile:
    kontext = f"Objekt '{zeile.get('id')}'"
    return ObjektZeile(
        id=_pflicht_str(zeile, "id", "Objekt"),
        gesellschaft_id=_pflicht_str(zeile, "gesellschaft_id", kontext),
        bezeichnung=_pflicht_str(zeile, "bezeichnung", kontext),
        adresse=_optional_str(zeile, "adresse"),
        ausgeschlossen=_optional_bool(zeile, "ausgeschlossen", default=False),
    )


def _einheit_aus_dict(zeile: dict) -> EinheitZeile:
    kontext = f"Einheit '{zeile.get('id')}'"
    return EinheitZeile(
        id=_pflicht_str(zeile, "id", "Einheit"),
        objekt_id=_pflicht_str(zeile, "objekt_id", kontext),
        bezeichnung=_pflicht_str(zeile, "bezeichnung", kontext),
        nutzungsstatus=_pflicht_str(zeile, "nutzungsstatus", kontext),
        flaeche_qm=_optional_decimal(zeile, "flaeche_qm", kontext),
        miteigentumsanteile=_optional_decimal(zeile, "miteigentumsanteile", kontext),
    )


def _debitor_aus_dict(zeile: dict) -> DebitorZeile:
    return DebitorZeile(
        id=_pflicht_str(zeile, "id", "Debitor"),
        name=_pflicht_str(zeile, "name", f"Debitor '{zeile.get('id')}'"),
        email=_optional_str(zeile, "email"),
        adresse=_optional_str(zeile, "adresse"),
    )


def _vertrag_aus_dict(zeile: dict) -> VertragZeile:
    kontext = f"Vertrag '{zeile.get('id')}'"
    return VertragZeile(
        id=_pflicht_str(zeile, "id", "Vertrag"),
        einheit_id=_pflicht_str(zeile, "einheit_id", kontext),
        debitor_id=_pflicht_str(zeile, "debitor_id", kontext),
        gesellschaft_id=_pflicht_str(zeile, "gesellschaft_id", kontext),
        rechtsordnung=_pflicht_str(zeile, "rechtsordnung", kontext),
        gueltig_von=_pflicht_datum(zeile, "gueltig_von", kontext),
        gueltig_bis=_optional_datum(zeile, "gueltig_bis"),
        faelligkeit_tag=_optional_int(zeile, "faelligkeit_tag", default=5),
        zahlungsfrist_tage=_optional_int(zeile, "zahlungsfrist_tage", default=14),
    )


def _eroeffnung_aus_dict(zeile: dict) -> EroeffnungZeile:
    kontext = f"Eröffnung '{zeile.get('import_id')}'"
    return EroeffnungZeile(
        import_id=_pflicht_str(zeile, "import_id", "Eröffnung"),
        vertrag_id=_pflicht_str(zeile, "vertrag_id", kontext),
        modus=_pflicht_str(zeile, "modus", kontext).upper(),
        betrag_cent=_pflicht_int(zeile, "betrag_cent", kontext),
        stichtag=_pflicht_datum(zeile, "stichtag", kontext),
        quelle_bestaetigt=_pflicht_bool(zeile, "quelle_bestaetigt", kontext),
        typ=_optional_str(zeile, "typ"),
        belegdatum=_optional_datum(zeile, "belegdatum"),
        faelligkeit=_optional_datum(zeile, "faelligkeit"),
        beleg_referenz=_optional_str(zeile, "beleg_referenz") or "Eröffnungsimport",
    )


def _nachbuchung_aus_dict(zeile: dict) -> NachbuchungZeile:
    kontext = f"Nachbuchung '{zeile.get('import_id')}'"
    return NachbuchungZeile(
        import_id=_pflicht_str(zeile, "import_id", "Nachbuchung"),
        vertrag_id=_pflicht_str(zeile, "vertrag_id", kontext),
        typ=_pflicht_str(zeile, "typ", kontext).upper(),
        betrag_cent=_pflicht_int(zeile, "betrag_cent", kontext),
        belegdatum=_pflicht_datum(zeile, "belegdatum", kontext),
        buchungsdatum=_pflicht_datum(zeile, "buchungsdatum", kontext),
        faelligkeit=_optional_datum(zeile, "faelligkeit"),
        beleg_referenz=_optional_str(zeile, "beleg_referenz") or "",
        aenderungsgrund=_optional_str(zeile, "aenderungsgrund"),
        leistungsperiode=_optional_str(zeile, "leistungsperiode"),
    )


def _eroeffnungskorrektur_aus_dict(zeile: dict) -> EroeffnungskorrekturZeile:
    kontext = f"Eröffnungskorrektur '{zeile.get('import_id')}'"
    return EroeffnungskorrekturZeile(
        import_id=_pflicht_str(zeile, "import_id", "Eröffnungskorrektur"),
        vertrag_id=_pflicht_str(zeile, "vertrag_id", kontext),
        typ=_pflicht_str(zeile, "typ", kontext).upper(),
        betrag_cent=_pflicht_int(zeile, "betrag_cent", kontext),
        original_belegdatum=_pflicht_datum(zeile, "original_belegdatum", kontext),
        grund=_pflicht_str(zeile, "grund", kontext),
        quelle_referenz=_pflicht_str(zeile, "quelle_referenz", kontext),
        beleg_referenz=_optional_str(zeile, "beleg_referenz") or "Eröffnungskorrektur",
    )


def _sperre_aus_dict(zeile: dict) -> SperreZeile:
    kontext = f"Sperre für Vertrag '{zeile.get('vertrag_id')}'"
    return SperreZeile(
        vertrag_id=_pflicht_str(zeile, "vertrag_id", "Sperre"),
        grund=_pflicht_str(zeile, "grund", kontext).upper(),
        kommentar=_optional_str(zeile, "kommentar"),
    )


def _komponente_aus_dict(zeile: dict) -> KomponenteZeile:
    kontext = f"Komponente '{zeile.get('id')}'"
    return KomponenteZeile(
        id=_pflicht_str(zeile, "id", "Komponente"),
        vertrag_id=_pflicht_str(zeile, "vertrag_id", kontext),
        art=_pflicht_str(zeile, "art", kontext),
        bezeichnung=_pflicht_str(zeile, "bezeichnung", kontext),
        betrag_cent=_pflicht_int(zeile, "betrag_cent", kontext),
        gueltig_von=_pflicht_datum(zeile, "gueltig_von", kontext),
        ust_satz_promille=_optional_int(zeile, "ust_satz_promille", default=10000),
        indexierbar=_optional_bool(zeile, "indexierbar", default=False),
        gueltig_bis=_optional_datum(zeile, "gueltig_bis"),
    )


def _kaution_aus_dict(zeile: dict) -> KautionZeile:
    kontext = f"Kaution für Vertrag '{zeile.get('vertrag_id')}'"
    return KautionZeile(
        vertrag_id=_pflicht_str(zeile, "vertrag_id", "Kaution"),
        betrag_cent=_pflicht_int(zeile, "betrag_cent", kontext),
        stichtag=_pflicht_datum(zeile, "stichtag", kontext),
        referenz=_optional_str(zeile, "referenz"),
    )


def _mietvertragsprofil_aus_dict(zeile: dict) -> MietvertragsprofilZeile:
    kontext = f"Mietvertragsprofil für Vertrag '{zeile.get('vertrag_id')}'"
    return MietvertragsprofilZeile(
        vertrag_id=_pflicht_str(zeile, "vertrag_id", "Mietvertragsprofil"),
        nutzungsart=(_optional_str(zeile, "nutzungsart") or "UNGEKLAERT").upper(),
        urspruenglicher_mietbeginn=_optional_datum(zeile, "urspruenglicher_mietbeginn"),
        verwaltungsuebernahme_am=_optional_datum(zeile, "verwaltungsuebernahme_am"),
        verwaltung_bezeichnung=_optional_str(zeile, "verwaltung_bezeichnung"),
        vertragliche_kaution_cent=_optional_int_oder_none(zeile, "vertragliche_kaution_cent", kontext),
        vertragliche_kaution_quellenbeleg=_optional_str(zeile, "vertragliche_kaution_quellenbeleg"),
        # None = unbekannt/kein Fund; 0 = ausdrücklich belegte "keine Gebühr".
        mahngebuehr_cent=_optional_int_oder_none(zeile, "mahngebuehr_cent", kontext),
        mahngebuehr_quellenbeleg=_optional_str(zeile, "mahngebuehr_quellenbeleg"),
        index_reihe=_optional_str(zeile, "index_reihe"),
        index_urspruenglicher_basismonat=_optional_str(zeile, "index_urspruenglicher_basismonat"),
        index_urspruenglicher_basiswert=_optional_decimal(zeile, "index_urspruenglicher_basiswert", kontext),
        index_schwelle_prozent=_optional_decimal(zeile, "index_schwelle_prozent", kontext),
        index_schwelle_inklusive=_optional_bool_oder_none(zeile, "index_schwelle_inklusive"),
        index_anpassungsmonat=_optional_int_oder_none(zeile, "index_anpassungsmonat", kontext),
        index_mindestintervall_monate=_optional_int_oder_none(zeile, "index_mindestintervall_monate", kontext),
        index_klauseltext_auszug=_optional_str(zeile, "index_klauseltext_auszug"),
        index_klauseltext_seite=_optional_int_oder_none(zeile, "index_klauseltext_seite", kontext),
        quelle_typ=(_optional_str(zeile, "quelle_typ") or "IMPORT_SCHEMA").upper(),
        quelle_referenz=_optional_str(zeile, "quelle_referenz"),
    )


def _optional_int_oder_none(zeile: dict, feld: str, kontext: str) -> int | None:
    if feld not in zeile or zeile.get(feld) in (None, ""):
        return None
    return _pflicht_int(zeile, feld, kontext)


def parse_json_paket(text: str) -> IntakePaket:
    try:
        rohdaten = json.loads(text)
    except json.JSONDecodeError as exc:
        raise IntakeFormatFehlerError(f"Ungültiges JSON: {exc}") from exc
    if not isinstance(rohdaten, dict):
        raise IntakeFormatFehlerError("Die JSON-Datei muss ein Objekt auf oberster Ebene sein.")

    return IntakePaket(
        quelle=_optional_str(rohdaten, "quelle") or "unbenannt",
        gesellschaften=tuple(_gesellschaft_aus_dict(z) for z in rohdaten.get("gesellschaften", [])),
        objekte=tuple(_objekt_aus_dict(z) for z in rohdaten.get("objekte", [])),
        einheiten=tuple(_einheit_aus_dict(z) for z in rohdaten.get("einheiten", [])),
        debitoren=tuple(_debitor_aus_dict(z) for z in rohdaten.get("debitoren", [])),
        vertraege=tuple(_vertrag_aus_dict(z) for z in rohdaten.get("vertraege", [])),
        eroeffnungen=tuple(_eroeffnung_aus_dict(z) for z in rohdaten.get("eroeffnungen", [])),
        nachbuchungen=tuple(_nachbuchung_aus_dict(z) for z in rohdaten.get("nachbuchungen", [])),
        eroeffnungskorrekturen=tuple(
            _eroeffnungskorrektur_aus_dict(z) for z in rohdaten.get("eroeffnungskorrekturen", [])
        ),
        sperren=tuple(_sperre_aus_dict(z) for z in rohdaten.get("sperren", [])),
        komponenten=tuple(_komponente_aus_dict(z) for z in rohdaten.get("komponenten", [])),
        kautionen=tuple(_kaution_aus_dict(z) for z in rohdaten.get("kautionen", [])),
        mietvertragsprofile=tuple(
            _mietvertragsprofil_aus_dict(z) for z in rohdaten.get("mietvertragsprofile", [])
        ),
    )


def _lese_csv_zeilen(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    return [dict(zeile) for zeile in reader]


def parse_csv_buendel(*, quelle: str, dateien: dict[str, str]) -> IntakePaket:
    """`dateien`: Dateiname ohne Endung (siehe `CSV_BUENDEL_DATEINAMEN`) ->
    CSV-Text. Fehlende Dateien = leere Liste für diesen Typ."""

    unbekannt = set(dateien) - set(CSV_BUENDEL_DATEINAMEN)
    if unbekannt:
        raise IntakeFormatFehlerError(f"Unbekannte Datei(en) im CSV-Bündel: {sorted(unbekannt)}.")

    def _zeilen(name: str) -> list[dict]:
        text = dateien.get(name)
        return _lese_csv_zeilen(text) if text is not None else []

    return IntakePaket(
        quelle=quelle,
        gesellschaften=tuple(_gesellschaft_aus_dict(z) for z in _zeilen("gesellschaften")),
        objekte=tuple(_objekt_aus_dict(z) for z in _zeilen("objekte")),
        einheiten=tuple(_einheit_aus_dict(z) for z in _zeilen("einheiten")),
        debitoren=tuple(_debitor_aus_dict(z) for z in _zeilen("debitoren")),
        vertraege=tuple(_vertrag_aus_dict(z) for z in _zeilen("vertraege")),
        eroeffnungen=tuple(_eroeffnung_aus_dict(z) for z in _zeilen("eroeffnungen")),
        nachbuchungen=tuple(_nachbuchung_aus_dict(z) for z in _zeilen("nachbuchungen")),
        eroeffnungskorrekturen=tuple(
            _eroeffnungskorrektur_aus_dict(z) for z in _zeilen("eroeffnungskorrekturen")
        ),
        sperren=tuple(_sperre_aus_dict(z) for z in _zeilen("sperren")),
        komponenten=tuple(_komponente_aus_dict(z) for z in _zeilen("komponenten")),
        kautionen=tuple(_kaution_aus_dict(z) for z in _zeilen("kautionen")),
        mietvertragsprofile=tuple(_mietvertragsprofil_aus_dict(z) for z in _zeilen("mietvertragsprofile")),
    )
