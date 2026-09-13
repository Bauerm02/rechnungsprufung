"""Rein lesende Prüfung/Planung eines `IntakePaket` (Auftrag
HV-20260912-ECHTBETRIEB). Erzeugt einen lesbaren Plan mit Quelle/Hash -
KEINE Datenbankschreibung. `apply.py` ruft `pruefe_paket` unmittelbar vor
dem Schreiben ERNEUT auf (mit derselben Logik, aber einer schreibenden
Session) - ein Plan wird nie blind wiederverwendet, siehe
`docs/hausverwaltung/IMPORT_VERTRAG.md`."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.domain.enums import (
    MietvertragsprofilQuelle,
    Nutzungsart,
    Nutzungsstatus,
    OPTyp,
    Rechtsordnung,
    Sperrgrund,
    rechtsordnung_geklaert,
)
from mietinkasso.infrastructure.db.tables import (
    DebitorTable,
    EinheitTable,
    GesellschaftTable,
    KautionTable,
    KontoTable,
    MietvertragsprofilTable,
    ObjektTable,
    OPPositionTable,
    SperreTable,
    VertragsKomponenteTable,
    VertragTable,
)
from mietinkasso.intake.schema import (
    EroeffnungskorrekturZeile,
    EroeffnungZeile,
    IntakePaket,
    KautionZeile,
    KomponenteZeile,
    MietvertragsprofilZeile,
    NachbuchungZeile,
    SperreZeile,
    paket_hash,
)

_GUELTIGE_NUTZUNGSARTEN = {e.value for e in Nutzungsart}
_GUELTIGE_MIETVERTRAGSPROFIL_QUELLEN = {e.value for e in MietvertragsprofilQuelle}

_GUELTIGE_NUTZUNGSSTATUS = {e.value for e in Nutzungsstatus}
_GUELTIGE_RECHTSORDNUNGEN = {e.value for e in Rechtsordnung}
_GUELTIGE_NACHBUCHUNGS_TYPEN = {OPTyp.SOLL.value, OPTyp.GUTSCHRIFT.value, OPTyp.ZAHLUNG.value, OPTyp.RUECKLASTSCHRIFT.value}
_GUELTIGE_SPERRGRUENDE = {e.value for e in Sperrgrund}

_STANDARD_AUSGESCHLOSSENE_OBJEKTE = frozenset({"107"})


@dataclass(frozen=True)
class PruefBefund:
    """Ein Prüfergebnis je Zeile. `status`:
    - "NEU": existiert noch nicht, würde neu angelegt/gebucht.
    - "UNVERAENDERT": existiert bereits mit identischem Inhalt - Apply
      wäre für diese Zeile ein wirkungsloser No-Op (Wiederholimport).
    - "KONFLIKT": existiert bereits mit ABWEICHENDEM Inhalt, ODER
      widerspricht einer anderen Zeile desselben Pakets.
    - "GESPERRT": Objekt 107/ausgeschlossen, oder ein Anfangssaldo ohne
      `quelle_bestaetigt`.
    - "AKTUALISIERUNG": NUR für `Mietvertragsprofil` (Auftrag
      HV-20260913-VERTRAGSANLAGE) - existiert bereits, aber mit
      abweichendem Inhalt gegenüber der zuletzt gespeicherten Version.
      Anders als "KONFLIKT" blockiert das NICHT den Lauf: laufende
      Datenpflege an rein deskriptiven Verwaltungsfeldern ist gewollt
      (siehe `MietvertragsprofilTable`-Docstring) und erzeugt beim Apply
      eine neue, sichtbare Version statt den gesamten Lauf abzulehnen.
    "NEU"/"UNVERAENDERT"/"AKTUALISIERUNG" sind unproblematisch; ein Paket
    mit irgendeinem KONFLIKT/GESPERRT ist NICHT anwendbar (siehe
    `IntakePlan.anwendbar`)."""

    entitaet: str
    id: str
    status: str
    grund: str | None = None


@dataclass(frozen=True)
class IntakePlan:
    paket_hash: str
    befunde: tuple[PruefBefund, ...]
    hinweise: tuple[str, ...]

    @property
    def neu(self) -> list[PruefBefund]:
        return [b for b in self.befunde if b.status == "NEU"]

    @property
    def unveraendert(self) -> list[PruefBefund]:
        return [b for b in self.befunde if b.status == "UNVERAENDERT"]

    @property
    def konflikte(self) -> list[PruefBefund]:
        return [b for b in self.befunde if b.status == "KONFLIKT"]

    @property
    def gesperrt(self) -> list[PruefBefund]:
        return [b for b in self.befunde if b.status == "GESPERRT"]

    @property
    def aktualisierungen(self) -> list[PruefBefund]:
        return [b for b in self.befunde if b.status == "AKTUALISIERUNG"]

    @property
    def anwendbar(self) -> bool:
        return not self.konflikte and not self.gesperrt


def _mehrfache_werte(werte: list[str]) -> set[str]:
    """Liefert die Werte, die 2+ Mal in `werte` vorkommen - Grundlage für
    die paketinterne Dublettenprüfung (siehe `pruefe_paket`): zwei
    verschiedene Zeilen mit derselben Primär-/Quell-ID im SELBEN Paket
    dürfen NIE beide als NEU durchgehen, sonst würde eine die andere
    beim Schreiben still überschreiben/verdrängen, je nach Reihenfolge
    und Flush-Zeitpunkt - nicht deterministisch und nicht erkennbar am
    Plan."""

    gesehen: set[str] = set()
    mehrfach: set[str] = set()
    for wert in werte:
        if wert in gesehen:
            mehrfach.add(wert)
        gesehen.add(wert)
    return mehrfach


#: Feste Nachkommastellen-Skalierung, EXAKT wie die Spalten in
#: `MietvertragsprofilTable` (`Numeric(12, 4)`/`Numeric(6, 3)`) - siehe
#: `_dezimal_kanonisch`.
_SKALIERUNG_INDEX_BASISWERT = Decimal("0.0001")
_SKALIERUNG_INDEX_SCHWELLE_PROZENT = Decimal("0.001")


def _dezimal_kanonisch(wert: Decimal | None, skalierung: Decimal) -> str | None:
    """Kanonisiert einen `Decimal`-Wert auf die FESTE Spaltenskalierung,
    BEVOR er zu Text wird - `str(Decimal(...))` behält die Anzahl der
    ursprünglich eingegebenen Nachkommastellen bei (`Decimal("2.00")` !=
    textuell `Decimal("2.000")`), obwohl beide denselben Wert darstellen
    und die Datenbank (`Numeric(6, 3)`) ihn beim Rücklesen ohnehin auf
    exakt diese Skalierung normalisiert. Ohne diese Kanonisierung würde
    JEDER identische Wiederholimport mit einer anderen Nachkommastellen-
    Schreibweise fälschlich als `AKTUALISIERUNG` statt `UNVERAENDERT`
    erkannt - reine Formatierungsunterschiede sind KEINE inhaltliche
    Änderung. `NaN`/`Infinity` werden explizit abgelehnt (ungültige
    Eingabe, nie stillschweigend weiterverarbeitet)."""

    if wert is None:
        return None
    if not wert.is_finite():
        raise ValueError(f"Dezimalwert '{wert}' ist nicht endlich (NaN/Infinity) - ungültige Eingabe.")
    return format(wert.quantize(skalierung), "f")


def _felder_hash(felder: dict) -> str:
    kanonisch = json.dumps(felder, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(kanonisch.encode("utf-8")).hexdigest()


def _gesellschaft_felder(name: str) -> dict:
    return {"name": name}


def _objekt_felder(gesellschaft_id: str, bezeichnung: str, adresse: str | None, ausgeschlossen: bool) -> dict:
    return {"gesellschaft_id": gesellschaft_id, "bezeichnung": bezeichnung, "adresse": adresse, "ausgeschlossen": ausgeschlossen}


def _einheit_felder(objekt_id: str, bezeichnung: str, nutzungsstatus: str, flaeche_qm, miteigentumsanteile) -> dict:
    return {
        "objekt_id": objekt_id,
        "bezeichnung": bezeichnung,
        "nutzungsstatus": nutzungsstatus,
        "flaeche_qm": str(flaeche_qm) if flaeche_qm is not None else None,
        "miteigentumsanteile": str(miteigentumsanteile) if miteigentumsanteile is not None else None,
    }


def _debitor_felder(name: str, email: str | None, adresse: str | None) -> dict:
    return {"name": name, "email": email, "adresse": adresse}


def _komponente_felder(
    vertrag_id: str, art: str, bezeichnung: str, betrag_cent: int, ust_satz_promille: int,
    indexierbar: bool, gueltig_von: date, gueltig_bis: date | None,
) -> dict:
    return {
        "vertrag_id": vertrag_id, "art": art, "bezeichnung": bezeichnung, "betrag_cent": betrag_cent,
        "ust_satz_promille": ust_satz_promille, "indexierbar": indexierbar,
        "gueltig_von": str(gueltig_von), "gueltig_bis": str(gueltig_bis) if gueltig_bis else None,
    }


def _kaution_felder(betrag_cent: int, stichtag: date, referenz: str | None) -> dict:
    return {"betrag_cent": betrag_cent, "stichtag": str(stichtag), "referenz": referenz}


def _mietvertragsprofil_felder(
    *, nutzungsart: str, urspruenglicher_mietbeginn: date | None, verwaltungsuebernahme_am: date | None,
    verwaltung_bezeichnung: str | None, vertragliche_kaution_cent: int | None,
    vertragliche_kaution_quellenbeleg: str | None, mahngebuehr_cent: int | None,
    mahngebuehr_quellenbeleg: str | None, index_reihe: str | None,
    index_urspruenglicher_basismonat: str | None, index_urspruenglicher_basiswert,
    index_schwelle_prozent, index_schwelle_inklusive: bool | None, index_anpassungsmonat: int | None,
    index_mindestintervall_monate: int | None, index_klauseltext_auszug: str | None,
    index_klauseltext_seite: int | None, quelle_typ: str, quelle_referenz: str | None,
) -> dict:
    """Feldabbild für den Versionsvergleich (siehe `_pruefe_mietvertragsprofil`)
    - bewusst OHNE `erstellt_von`/`erstellt_am`/`version`: eine neue Version mit
    identischem fachlichem Inhalt aber anderem Erfasser/Zeitstempel ist immer
    noch UNVERAENDERT, kein Grund für eine neue Version."""
    return {
        "nutzungsart": nutzungsart,
        "urspruenglicher_mietbeginn": str(urspruenglicher_mietbeginn) if urspruenglicher_mietbeginn else None,
        "verwaltungsuebernahme_am": str(verwaltungsuebernahme_am) if verwaltungsuebernahme_am else None,
        "verwaltung_bezeichnung": verwaltung_bezeichnung,
        "vertragliche_kaution_cent": vertragliche_kaution_cent,
        "vertragliche_kaution_quellenbeleg": vertragliche_kaution_quellenbeleg,
        "mahngebuehr_cent": mahngebuehr_cent,
        "mahngebuehr_quellenbeleg": mahngebuehr_quellenbeleg,
        "index_reihe": index_reihe,
        "index_urspruenglicher_basismonat": index_urspruenglicher_basismonat,
        "index_urspruenglicher_basiswert": _dezimal_kanonisch(index_urspruenglicher_basiswert, _SKALIERUNG_INDEX_BASISWERT),
        "index_schwelle_prozent": _dezimal_kanonisch(index_schwelle_prozent, _SKALIERUNG_INDEX_SCHWELLE_PROZENT),
        "index_schwelle_inklusive": index_schwelle_inklusive,
        "index_anpassungsmonat": index_anpassungsmonat,
        "index_mindestintervall_monate": index_mindestintervall_monate,
        "index_klauseltext_auszug": index_klauseltext_auszug,
        "index_klauseltext_seite": index_klauseltext_seite,
        "quelle_typ": quelle_typ,
        "quelle_referenz": quelle_referenz,
    }


def _mietvertragsprofil_felder_aus_zeile(z: MietvertragsprofilZeile) -> dict:
    return _mietvertragsprofil_felder(
        nutzungsart=z.nutzungsart, urspruenglicher_mietbeginn=z.urspruenglicher_mietbeginn,
        verwaltungsuebernahme_am=z.verwaltungsuebernahme_am, verwaltung_bezeichnung=z.verwaltung_bezeichnung,
        vertragliche_kaution_cent=z.vertragliche_kaution_cent,
        vertragliche_kaution_quellenbeleg=z.vertragliche_kaution_quellenbeleg,
        mahngebuehr_cent=z.mahngebuehr_cent, mahngebuehr_quellenbeleg=z.mahngebuehr_quellenbeleg,
        index_reihe=z.index_reihe, index_urspruenglicher_basismonat=z.index_urspruenglicher_basismonat,
        index_urspruenglicher_basiswert=z.index_urspruenglicher_basiswert,
        index_schwelle_prozent=z.index_schwelle_prozent, index_schwelle_inklusive=z.index_schwelle_inklusive,
        index_anpassungsmonat=z.index_anpassungsmonat, index_mindestintervall_monate=z.index_mindestintervall_monate,
        index_klauseltext_auszug=z.index_klauseltext_auszug, index_klauseltext_seite=z.index_klauseltext_seite,
        quelle_typ=z.quelle_typ, quelle_referenz=z.quelle_referenz,
    )


def _mietvertragsprofil_felder_aus_tabelle(t: MietvertragsprofilTable) -> dict:
    return _mietvertragsprofil_felder(
        nutzungsart=t.nutzungsart, urspruenglicher_mietbeginn=t.urspruenglicher_mietbeginn,
        verwaltungsuebernahme_am=t.verwaltungsuebernahme_am, verwaltung_bezeichnung=t.verwaltung_bezeichnung,
        vertragliche_kaution_cent=t.vertragliche_kaution_cent,
        vertragliche_kaution_quellenbeleg=t.vertragliche_kaution_quellenbeleg,
        mahngebuehr_cent=t.mahngebuehr_cent, mahngebuehr_quellenbeleg=t.mahngebuehr_quellenbeleg,
        index_reihe=t.index_reihe, index_urspruenglicher_basismonat=t.index_urspruenglicher_basismonat,
        index_urspruenglicher_basiswert=t.index_urspruenglicher_basiswert,
        index_schwelle_prozent=t.index_schwelle_prozent, index_schwelle_inklusive=t.index_schwelle_inklusive,
        index_anpassungsmonat=t.index_anpassungsmonat, index_mindestintervall_monate=t.index_mindestintervall_monate,
        index_klauseltext_auszug=t.index_klauseltext_auszug, index_klauseltext_seite=t.index_klauseltext_seite,
        quelle_typ=t.quelle_typ, quelle_referenz=t.quelle_referenz,
    )


def _vertrag_felder(
    einheit_id: str, debitor_id: str, gesellschaft_id: str, rechtsordnung: str,
    gueltig_von: date, gueltig_bis: date | None, faelligkeit_tag: int, zahlungsfrist_tage: int,
) -> dict:
    return {
        "einheit_id": einheit_id, "debitor_id": debitor_id, "gesellschaft_id": gesellschaft_id,
        "rechtsordnung": rechtsordnung, "gueltig_von": str(gueltig_von),
        "gueltig_bis": str(gueltig_bis) if gueltig_bis else None,
        "faelligkeit_tag": faelligkeit_tag, "zahlungsfrist_tage": zahlungsfrist_tage,
    }


class _Kontext:
    """Sammelt während der Prüfung, welche IDs im PAKET SELBST bereits
    deklariert wurden (für Referenzen zwischen Zeilen DERSELBEN Datei,
    z. B. ein Vertrag, der eine im selben Lauf neu angelegte Einheit
    referenziert) - getrennt von dem, was schon in der DB steht."""

    def __init__(self) -> None:
        self.gesellschaft_ids: set[str] = set()
        self.objekt_ausgeschlossen: dict[str, bool] = {}
        self.einheit_objekt: dict[str, str] = {}
        self.debitor_ids: set[str] = set()
        self.vertrag_gesellschaft: dict[str, str] = {}
        self.vertrag_einheit: dict[str, str] = {}
        # vertrag_id -> (stichtag, betrag_cent) einer im PAKET deklarierten GESAMTSALDO-Eröffnung
        self.gesamtsaldo_im_paket: dict[str, tuple[date, int]] = {}
        # (vertrag_id, grund, kommentar) bereits im PAKET deklarierter Sperren - Dedupe für
        # mehrfach identische Zeilen INNERHALB derselben Datei (nicht nur gegen die DB).
        self.sperren_im_paket: set[tuple[str, str, str | None]] = set()


def pruefe_paket(
    paket: IntakePaket,
    *,
    session: Session,
    ausgeschlossene_objekte: frozenset[str] = _STANDARD_AUSGESCHLOSSENE_OBJEKTE,
) -> IntakePlan:
    """Rein lesende Prüfung GEGEN die per `session` erreichbare Datenbank.
    Wird identisch von `plan()` (Lesesession) und `apply()` (unmittelbar
    vor dem Schreiben, auf derselben Session wie die Schreibung) benutzt -
    kein separater Prüfpfad, der von der tatsächlichen Schreiblogik
    abweichen könnte."""

    befunde: list[PruefBefund] = []
    hinweise: list[str] = []
    ctx = _Kontext()

    gesellschaft_mehrfach = _mehrfache_werte([z.id for z in paket.gesellschaften])
    for z in paket.gesellschaften:
        ctx.gesellschaft_ids.add(z.id)
        if z.id in gesellschaft_mehrfach:
            befunde.append(PruefBefund("Gesellschaft", z.id, "KONFLIKT", f"Gesellschaft-ID '{z.id}' ist mehrfach im selben Paket vertreten - eindeutige IDs sind Pflicht."))
            continue
        felder = _gesellschaft_felder(z.name)
        bestehend = session.get(GesellschaftTable, z.id)
        if bestehend is None:
            befunde.append(PruefBefund("Gesellschaft", z.id, "NEU"))
        elif _felder_hash(_gesellschaft_felder(bestehend.name)) == _felder_hash(felder):
            befunde.append(PruefBefund("Gesellschaft", z.id, "UNVERAENDERT"))
        else:
            befunde.append(PruefBefund("Gesellschaft", z.id, "KONFLIKT", f"Gesellschaft '{z.id}' existiert bereits mit abweichendem Inhalt."))

    objekt_mehrfach = _mehrfache_werte([z.id for z in paket.objekte])
    for z in paket.objekte:
        gesperrt_grund = None
        if z.id in objekt_mehrfach:
            befunde.append(PruefBefund("Objekt", z.id, "KONFLIKT", f"Objekt-ID '{z.id}' ist mehrfach im selben Paket vertreten - eindeutige IDs sind Pflicht."))
            ctx.objekt_ausgeschlossen[z.id] = z.id in ausgeschlossene_objekte or z.ausgeschlossen
            continue
        if z.id in ausgeschlossene_objekte or z.ausgeschlossen:
            gesperrt_grund = f"Objekt '{z.id}' ist ausgeschlossen (Pilot-Fachregel 1) und darf nicht eingespielt werden."
        if z.gesellschaft_id not in ctx.gesellschaft_ids and session.get(GesellschaftTable, z.gesellschaft_id) is None:
            befunde.append(PruefBefund("Objekt", z.id, "KONFLIKT", f"Objekt '{z.id}' referenziert unbekannte Gesellschaft '{z.gesellschaft_id}'."))
        elif gesperrt_grund is not None:
            befunde.append(PruefBefund("Objekt", z.id, "GESPERRT", gesperrt_grund))
        else:
            felder = _objekt_felder(z.gesellschaft_id, z.bezeichnung, z.adresse, z.ausgeschlossen)
            bestehend = session.get(ObjektTable, z.id)
            if bestehend is None:
                befunde.append(PruefBefund("Objekt", z.id, "NEU"))
            elif _felder_hash(_objekt_felder(bestehend.gesellschaft_id, bestehend.bezeichnung, bestehend.adresse, bestehend.ausgeschlossen)) == _felder_hash(felder):
                befunde.append(PruefBefund("Objekt", z.id, "UNVERAENDERT"))
            else:
                befunde.append(PruefBefund("Objekt", z.id, "KONFLIKT", f"Objekt '{z.id}' existiert bereits mit abweichendem Inhalt."))
        ctx.objekt_ausgeschlossen[z.id] = bool(gesperrt_grund)

    def _objekt_ausgeschlossen(objekt_id: str) -> bool:
        if objekt_id in ctx.objekt_ausgeschlossen:
            return ctx.objekt_ausgeschlossen[objekt_id]
        if objekt_id in ausgeschlossene_objekte:
            return True
        bestehend = session.get(ObjektTable, objekt_id)
        return bestehend is not None and bestehend.ausgeschlossen

    einheit_mehrfach = _mehrfache_werte([z.id for z in paket.einheiten])
    for z in paket.einheiten:
        ctx.einheit_objekt[z.id] = z.objekt_id
        if z.id in einheit_mehrfach:
            befunde.append(PruefBefund("Einheit", z.id, "KONFLIKT", f"Einheit-ID '{z.id}' ist mehrfach im selben Paket vertreten - eindeutige IDs sind Pflicht."))
            continue
        objekt_bekannt = z.objekt_id in ctx.objekt_ausgeschlossen or session.get(ObjektTable, z.objekt_id) is not None
        if z.nutzungsstatus not in _GUELTIGE_NUTZUNGSSTATUS:
            befunde.append(PruefBefund("Einheit", z.id, "KONFLIKT", f"Einheit '{z.id}': ungültiger Nutzungsstatus '{z.nutzungsstatus}'."))
        elif not objekt_bekannt:
            befunde.append(PruefBefund("Einheit", z.id, "KONFLIKT", f"Einheit '{z.id}' referenziert unbekanntes Objekt '{z.objekt_id}'."))
        elif _objekt_ausgeschlossen(z.objekt_id):
            befunde.append(PruefBefund("Einheit", z.id, "GESPERRT", f"Einheit '{z.id}' gehört zu einem ausgeschlossenen Objekt ('{z.objekt_id}')."))
        else:
            felder = _einheit_felder(z.objekt_id, z.bezeichnung, z.nutzungsstatus, z.flaeche_qm, z.miteigentumsanteile)
            bestehend = session.get(EinheitTable, z.id)
            if bestehend is None:
                befunde.append(PruefBefund("Einheit", z.id, "NEU"))
            elif _felder_hash(_einheit_felder(bestehend.objekt_id, bestehend.bezeichnung, bestehend.nutzungsstatus, bestehend.flaeche_qm, bestehend.miteigentumsanteile)) == _felder_hash(felder):
                befunde.append(PruefBefund("Einheit", z.id, "UNVERAENDERT"))
            else:
                befunde.append(PruefBefund("Einheit", z.id, "KONFLIKT", f"Einheit '{z.id}' existiert bereits mit abweichendem Inhalt."))

    fehlende_email_anzahl = 0
    debitor_mehrfach = _mehrfache_werte([z.id for z in paket.debitoren])
    for z in paket.debitoren:
        ctx.debitor_ids.add(z.id)
        if not (z.email or "").strip():
            fehlende_email_anzahl += 1
        if z.id in debitor_mehrfach:
            befunde.append(PruefBefund("Debitor", z.id, "KONFLIKT", f"Debitor-ID '{z.id}' ist mehrfach im selben Paket vertreten - eindeutige IDs sind Pflicht."))
            continue
        felder = _debitor_felder(z.name, z.email, z.adresse)
        bestehend = session.get(DebitorTable, z.id)
        if bestehend is None:
            befunde.append(PruefBefund("Debitor", z.id, "NEU"))
        elif _felder_hash(_debitor_felder(bestehend.name, bestehend.email, bestehend.adresse)) == _felder_hash(felder):
            befunde.append(PruefBefund("Debitor", z.id, "UNVERAENDERT"))
        else:
            befunde.append(PruefBefund("Debitor", z.id, "KONFLIKT", f"Debitor '{z.id}' existiert bereits mit abweichendem Inhalt."))

    def _einheit_objekt_id(einheit_id: str) -> str | None:
        if einheit_id in ctx.einheit_objekt:
            return ctx.einheit_objekt[einheit_id]
        bestehend = session.get(EinheitTable, einheit_id)
        return bestehend.objekt_id if bestehend is not None else None

    fehlende_faelligkeit_anzahl = 0
    ungeklaerte_rechtsordnung_anzahl = 0
    vertrag_mehrfach = _mehrfache_werte([z.id for z in paket.vertraege])
    for z in paket.vertraege:
        ctx.vertrag_gesellschaft[z.id] = z.gesellschaft_id
        ctx.vertrag_einheit[z.id] = z.einheit_id
        if not rechtsordnung_geklaert(z.rechtsordnung):
            ungeklaerte_rechtsordnung_anzahl += 1
        if z.id in vertrag_mehrfach:
            befunde.append(PruefBefund("Vertrag", z.id, "KONFLIKT", f"Vertrag-ID '{z.id}' ist mehrfach im selben Paket vertreten - eindeutige IDs sind Pflicht."))
            continue
        einheit_bekannt = z.einheit_id in ctx.einheit_objekt or session.get(EinheitTable, z.einheit_id) is not None
        debitor_bekannt = z.debitor_id in ctx.debitor_ids or session.get(DebitorTable, z.debitor_id) is not None
        gesellschaft_bekannt = z.gesellschaft_id in ctx.gesellschaft_ids or session.get(GesellschaftTable, z.gesellschaft_id) is not None
        if z.rechtsordnung not in _GUELTIGE_RECHTSORDNUNGEN:
            befunde.append(PruefBefund("Vertrag", z.id, "KONFLIKT", f"Vertrag '{z.id}': ungültige Rechtsordnung '{z.rechtsordnung}'."))
        elif not (einheit_bekannt and debitor_bekannt and gesellschaft_bekannt):
            fehlend = [
                name for name, ok in (("einheit_id", einheit_bekannt), ("debitor_id", debitor_bekannt), ("gesellschaft_id", gesellschaft_bekannt)) if not ok
            ]
            befunde.append(PruefBefund("Vertrag", z.id, "KONFLIKT", f"Vertrag '{z.id}': unbekannte Referenz(en) {fehlend}."))
        else:
            objekt_id = _einheit_objekt_id(z.einheit_id)
            if objekt_id is not None and _objekt_ausgeschlossen(objekt_id):
                befunde.append(PruefBefund("Vertrag", z.id, "GESPERRT", f"Vertrag '{z.id}' gehört zu einem ausgeschlossenen Objekt ('{objekt_id}')."))
            else:
                felder = _vertrag_felder(
                    z.einheit_id, z.debitor_id, z.gesellschaft_id, z.rechtsordnung,
                    z.gueltig_von, z.gueltig_bis, z.faelligkeit_tag, z.zahlungsfrist_tage,
                )
                bestehend = session.get(VertragTable, z.id)
                if bestehend is None:
                    befunde.append(PruefBefund("Vertrag", z.id, "NEU"))
                elif _felder_hash(_vertrag_felder(
                    bestehend.einheit_id, bestehend.debitor_id, bestehend.gesellschaft_id, bestehend.rechtsordnung,
                    bestehend.gueltig_von, bestehend.gueltig_bis, bestehend.faelligkeit_tag, bestehend.zahlungsfrist_tage,
                )) == _felder_hash(felder):
                    befunde.append(PruefBefund("Vertrag", z.id, "UNVERAENDERT"))
                else:
                    befunde.append(PruefBefund("Vertrag", z.id, "KONFLIKT", f"Vertrag '{z.id}' existiert bereits mit abweichendem Inhalt."))

    def _vertrag_bekannt(vertrag_id: str) -> bool:
        return vertrag_id in ctx.vertrag_einheit or session.get(VertragTable, vertrag_id) is not None

    def _konto_id(vertrag_id: str) -> str:
        return f"KTO-{vertrag_id}"

    def _bestehende_eroeffnung(konto_id: str) -> OPPositionTable | None:
        return session.execute(
            select(OPPositionTable)
            .where(OPPositionTable.konto_id == konto_id)
            .where(OPPositionTable.typ == OPTyp.EROEFFNUNG.value)
            .where(OPPositionTable.status == "AKTIV")
        ).scalar_one_or_none()

    def _bestehender_eroeffnungsmodus(vertrag_id: str) -> str | None:
        konto = session.execute(select(KontoTable).where(KontoTable.vertrag_id == vertrag_id)).scalar_one_or_none()
        return konto.eroeffnung_modus if konto is not None else None

    # Eröffnungen/Nachbuchungen/Eröffnungskorrekturen teilen sich EINEN
    # ID-Raum (alle schreiben letztlich in `OPPositionTable.import_id`,
    # ein einziger DB-weiter Unique-Index `uq_op_import_id`) - eine
    # Dublette MUSS deshalb über alle drei Listen hinweg geprüft werden,
    # nicht nur innerhalb einer einzelnen Liste.
    op_import_id_mehrfach = _mehrfache_werte([
        z.import_id for z in (*paket.eroeffnungen, *paket.nachbuchungen, *paket.eroeffnungskorrekturen)
    ])

    for z in paket.eroeffnungen:
        if z.import_id in op_import_id_mehrfach:
            befunde.append(PruefBefund("Eröffnung", z.import_id, "KONFLIKT", f"import_id '{z.import_id}' ist mehrfach im selben Paket vertreten (Eröffnungen/Nachbuchungen/Eröffnungskorrekturen teilen sich einen ID-Raum) - eindeutige IDs sind Pflicht."))
            continue
        _pruefe_eroeffnung(z, ctx=ctx, session=session, befunde=befunde, vertrag_bekannt=_vertrag_bekannt(z.vertrag_id),
                            konto_id=_konto_id(z.vertrag_id), bestehende_eroeffnung=_bestehende_eroeffnung, bestehender_modus=_bestehender_eroeffnungsmodus)
        if not z.faelligkeit and z.modus == "EINZEL_OP":
            fehlende_faelligkeit_anzahl += 1

    for z in paket.nachbuchungen:
        if z.import_id in op_import_id_mehrfach:
            befunde.append(PruefBefund("Nachbuchung", z.import_id, "KONFLIKT", f"import_id '{z.import_id}' ist mehrfach im selben Paket vertreten (Eröffnungen/Nachbuchungen/Eröffnungskorrekturen teilen sich einen ID-Raum) - eindeutige IDs sind Pflicht."))
            continue
        _pruefe_nachbuchung(z, ctx=ctx, session=session, befunde=befunde, vertrag_bekannt=_vertrag_bekannt(z.vertrag_id),
                             konto_id=_konto_id(z.vertrag_id), bestehender_modus=_bestehender_eroeffnungsmodus)
        if not z.faelligkeit:
            fehlende_faelligkeit_anzahl += 1

    for z in paket.eroeffnungskorrekturen:
        if z.import_id in op_import_id_mehrfach:
            befunde.append(PruefBefund("Eröffnungskorrektur", z.import_id, "KONFLIKT", f"import_id '{z.import_id}' ist mehrfach im selben Paket vertreten (Eröffnungen/Nachbuchungen/Eröffnungskorrekturen teilen sich einen ID-Raum) - eindeutige IDs sind Pflicht."))
            continue
        _pruefe_eroeffnungskorrektur(
            z, ctx=ctx, session=session, befunde=befunde, vertrag_bekannt=_vertrag_bekannt(z.vertrag_id),
            bestehender_modus=_bestehender_eroeffnungsmodus,
        )

    for z in paket.sperren:
        _pruefe_sperre(z, ctx=ctx, session=session, befunde=befunde, vertrag_bekannt=_vertrag_bekannt(z.vertrag_id))

    def _einheit_id_fuer_vertrag(vertrag_id: str) -> str | None:
        if vertrag_id in ctx.vertrag_einheit:
            return ctx.vertrag_einheit[vertrag_id]
        bestehender_vertrag = session.get(VertragTable, vertrag_id)
        return bestehender_vertrag.einheit_id if bestehender_vertrag is not None else None

    komponente_mehrfach = _mehrfache_werte([z.id for z in paket.komponenten])
    for z in paket.komponenten:
        if z.id in komponente_mehrfach:
            befunde.append(PruefBefund("Komponente", z.id, "KONFLIKT", f"Komponente-ID '{z.id}' ist mehrfach im selben Paket vertreten - eindeutige IDs sind Pflicht."))
            continue
        einheit_id = _einheit_id_fuer_vertrag(z.vertrag_id)
        objekt_id = _einheit_objekt_id(einheit_id) if einheit_id is not None else None
        _pruefe_komponente(
            z, session=session, befunde=befunde, vertrag_bekannt=_vertrag_bekannt(z.vertrag_id),
            objekt_ausgeschlossen=objekt_id is not None and _objekt_ausgeschlossen(objekt_id),
        )

    kaution_mehrfach = _mehrfache_werte([z.vertrag_id for z in paket.kautionen])
    for z in paket.kautionen:
        if z.vertrag_id in kaution_mehrfach:
            befunde.append(PruefBefund("Kaution", z.vertrag_id, "KONFLIKT", f"Kaution für Vertrag '{z.vertrag_id}' ist mehrfach im selben Paket vertreten - eindeutige Zeile je Vertrag ist Pflicht."))
            continue
        _pruefe_kaution(z, session=session, befunde=befunde, vertrag_bekannt=_vertrag_bekannt(z.vertrag_id))

    mietvertragsprofil_mehrfach = _mehrfache_werte([z.vertrag_id for z in paket.mietvertragsprofile])
    for z in paket.mietvertragsprofile:
        if z.vertrag_id in mietvertragsprofil_mehrfach:
            befunde.append(PruefBefund("Mietvertragsprofil", z.vertrag_id, "KONFLIKT", f"Mietvertragsprofil für Vertrag '{z.vertrag_id}' ist mehrfach im selben Paket vertreten - eindeutige Zeile je Vertrag ist Pflicht."))
            continue
        _pruefe_mietvertragsprofil(z, session=session, befunde=befunde, vertrag_bekannt=_vertrag_bekannt(z.vertrag_id))

    if fehlende_email_anzahl:
        hinweise.append(f"{fehlende_email_anzahl} Debitor(en) ohne E-Mail — bleiben anlegbar, sind aber nie automatisch mahnfähig.")
    if fehlende_faelligkeit_anzahl:
        hinweise.append(f"{fehlende_faelligkeit_anzahl} Eröffnungs-/Nachbuchungszeile(n) ohne Fälligkeit — bleiben sichtbar, werden nie automatisch gemahnt.")
    if ungeklaerte_rechtsordnung_anzahl:
        hinweise.append(
            f"{ungeklaerte_rechtsordnung_anzahl} Vertrag/Verträge mit Rechtsordnung UNGEKLAERT — bleiben anlegbar, "
            "sind aber technisch von Mahnung, Index-Anpassung und Sollstellung gesperrt."
        )

    return IntakePlan(paket_hash=paket_hash(paket), befunde=tuple(befunde), hinweise=tuple(hinweise))


def _pruefe_eroeffnung(
    z: EroeffnungZeile, *, ctx: _Kontext, session: Session, befunde: list[PruefBefund],
    vertrag_bekannt: bool, konto_id: str, bestehende_eroeffnung, bestehender_modus,
) -> None:
    entitaet = "Eröffnung"
    if not vertrag_bekannt:
        befunde.append(PruefBefund(entitaet, z.import_id, "KONFLIKT", f"Eröffnung '{z.import_id}' referenziert unbekannten Vertrag '{z.vertrag_id}'."))
        return
    if not z.quelle_bestaetigt:
        befunde.append(PruefBefund(entitaet, z.import_id, "GESPERRT", f"Eröffnung '{z.import_id}': quelle_bestaetigt fehlt/false — ungeprüfter Anfangssaldo wird nicht eingespielt."))
        return
    if z.modus not in ("GESAMTSALDO", "EINZEL_OP"):
        befunde.append(PruefBefund(entitaet, z.import_id, "KONFLIKT", f"Eröffnung '{z.import_id}': ungültiger Modus '{z.modus}'."))
        return
    vorhandener_modus = ctx.gesamtsaldo_im_paket.get(z.vertrag_id) is not None and "GESAMTSALDO" or bestehender_modus(z.vertrag_id)
    if z.modus == "EINZEL_OP" and not z.typ:
        befunde.append(PruefBefund(entitaet, z.import_id, "KONFLIKT", f"Eröffnung '{z.import_id}': modus EINZEL_OP verlangt 'typ'."))
        return
    if z.modus == "EINZEL_OP" and z.typ not in {t.value for t in OPTyp}:
        befunde.append(PruefBefund(entitaet, z.import_id, "KONFLIKT", f"Eröffnung '{z.import_id}': ungültiger typ '{z.typ}'."))
        return
    if z.modus == "EINZEL_OP" and z.betrag_cent <= 0:
        # EINZEL_OP-Zeilen leiten ihr Vorzeichen aus `typ` ab (siehe
        # op/service.py::_effect_cent); ein negativer Eingabewert würde
        # dort ein zweites Mal negiert und z. B. eine GUTSCHRIFT/ZAHLUNG
        # versehentlich zu einer Schulderhöhung machen statt zu mindern.
        # NUR die GESAMTSALDO-Eröffnung (Nettosumme, kein typ-Vorzeichen)
        # darf negativ (ein Guthaben) sein.
        befunde.append(PruefBefund(entitaet, z.import_id, "KONFLIKT", f"Eröffnung '{z.import_id}': betrag_cent muss bei EINZEL_OP positiv sein ({z.betrag_cent})."))
        return

    if z.modus == "GESAMTSALDO":
        bereits_im_paket = ctx.gesamtsaldo_im_paket.get(z.vertrag_id)
        if bereits_im_paket is not None and bereits_im_paket != (z.stichtag, z.betrag_cent):
            befunde.append(PruefBefund(entitaet, z.import_id, "KONFLIKT", f"Vertrag '{z.vertrag_id}': mehrere widersprüchliche GESAMTSALDO-Eröffnungen im selben Paket."))
            return
        ctx.gesamtsaldo_im_paket[z.vertrag_id] = (z.stichtag, z.betrag_cent)
        if bestehender_modus(z.vertrag_id) not in (None, "GESAMTSALDO"):
            befunde.append(PruefBefund(entitaet, z.import_id, "KONFLIKT", f"Vertrag '{z.vertrag_id}' wurde bereits mit anderem Eröffnungsmodus eröffnet."))
            return
        bestehend = bestehende_eroeffnung(konto_id)
        if bestehend is None:
            befunde.append(PruefBefund(entitaet, z.import_id, "NEU"))
        elif bestehend.betrag_cent == z.betrag_cent and bestehend.belegdatum == z.stichtag:
            befunde.append(PruefBefund(entitaet, z.import_id, "UNVERAENDERT"))
        else:
            befunde.append(PruefBefund(entitaet, z.import_id, "KONFLIKT", f"Vertrag '{z.vertrag_id}' hat bereits einen abweichenden Gesamtsaldo."))
        return

    # EINZEL_OP
    if bestehender_modus(z.vertrag_id) not in (None, "EINZEL_OP") and vorhandener_modus != "GESAMTSALDO":
        befunde.append(PruefBefund(entitaet, z.import_id, "KONFLIKT", f"Vertrag '{z.vertrag_id}' wurde bereits mit anderem Eröffnungsmodus eröffnet."))
        return
    bestehende_zeile = session.execute(select(OPPositionTable).where(OPPositionTable.import_id == z.import_id)).scalar_one_or_none()
    if bestehende_zeile is None:
        befunde.append(PruefBefund(entitaet, z.import_id, "NEU"))
    elif bestehende_zeile.betrag_cent == z.betrag_cent and bestehende_zeile.typ == z.typ and bestehende_zeile.belegdatum == (z.belegdatum or z.stichtag):
        befunde.append(PruefBefund(entitaet, z.import_id, "UNVERAENDERT"))
    else:
        befunde.append(PruefBefund(entitaet, z.import_id, "KONFLIKT", f"import_id '{z.import_id}' existiert bereits mit abweichendem Inhalt."))


def _pruefe_nachbuchung(
    z: NachbuchungZeile, *, ctx: _Kontext, session: Session, befunde: list[PruefBefund],
    vertrag_bekannt: bool, konto_id: str, bestehender_modus,
) -> None:
    entitaet = "Nachbuchung"
    if not vertrag_bekannt:
        befunde.append(PruefBefund(entitaet, z.import_id, "KONFLIKT", f"Nachbuchung '{z.import_id}' referenziert unbekannten Vertrag '{z.vertrag_id}'."))
        return
    if z.typ not in _GUELTIGE_NACHBUCHUNGS_TYPEN:
        befunde.append(PruefBefund(entitaet, z.import_id, "KONFLIKT", f"Nachbuchung '{z.import_id}': ungültiger typ '{z.typ}'."))
        return
    if z.betrag_cent <= 0:
        # SOLL/GUTSCHRIFT/ZAHLUNG/RUECKLASTSCHRIFT leiten ihr Vorzeichen
        # aus `typ` ab (op/service.py::_effect_cent) - `betrag_cent` ist
        # IMMER positiv einzugeben, auch bei GUTSCHRIFT/ZAHLUNG (die den
        # Saldo MINDERN, aber als positiver Betrag gebucht werden). Ein
        # negativer Wert würde sonst ein zweites Mal negiert und z. B.
        # eine GUTSCHRIFT versehentlich zur Schulderhöhung machen.
        befunde.append(PruefBefund(entitaet, z.import_id, "KONFLIKT", f"Nachbuchung '{z.import_id}': betrag_cent muss positiv sein ({z.betrag_cent})."))
        return

    if z.typ in (OPTyp.SOLL.value, OPTyp.GUTSCHRIFT.value):
        gesamtsaldo_stichtag = None
        im_paket = ctx.gesamtsaldo_im_paket.get(z.vertrag_id)
        if im_paket is not None:
            gesamtsaldo_stichtag = im_paket[0]
        elif bestehender_modus(z.vertrag_id) == "GESAMTSALDO":
            konto = session.execute(select(KontoTable).where(KontoTable.vertrag_id == z.vertrag_id)).scalar_one_or_none()
            gesamtsaldo_stichtag = konto.eroeffnung_stichtag if konto is not None else None
        if gesamtsaldo_stichtag is not None and z.belegdatum <= gesamtsaldo_stichtag:
            befunde.append(PruefBefund(
                entitaet, z.import_id, "KONFLIKT",
                f"Nachbuchung '{z.import_id}': Belegdatum {z.belegdatum} liegt vor/auf dem Gesamtsaldo-Stichtag "
                f"{gesamtsaldo_stichtag} und wäre im Saldo bereits enthalten (Journal nicht doppelt buchen).",
            ))
            return

    bestehende_zeile = session.execute(select(OPPositionTable).where(OPPositionTable.import_id == z.import_id)).scalar_one_or_none()
    if bestehende_zeile is None:
        befunde.append(PruefBefund(entitaet, z.import_id, "NEU"))
    elif (
        bestehende_zeile.betrag_cent == z.betrag_cent
        and bestehende_zeile.typ == z.typ
        and bestehende_zeile.belegdatum == z.belegdatum
        and (bestehende_zeile.leistungsperiode or None) == z.leistungsperiode
        and (bestehende_zeile.beleg_referenz or "") == z.beleg_referenz
    ):
        befunde.append(PruefBefund(entitaet, z.import_id, "UNVERAENDERT"))
    else:
        befunde.append(PruefBefund(entitaet, z.import_id, "KONFLIKT", f"import_id '{z.import_id}' existiert bereits mit abweichendem Inhalt."))


def _pruefe_eroeffnungskorrektur(
    z: EroeffnungskorrekturZeile, *, ctx: _Kontext, session: Session, befunde: list[PruefBefund],
    vertrag_bekannt: bool, bestehender_modus,
) -> None:
    entitaet = "Eröffnungskorrektur"
    if not vertrag_bekannt:
        befunde.append(PruefBefund(entitaet, z.import_id, "KONFLIKT", f"Eröffnungskorrektur '{z.import_id}' referenziert unbekannten Vertrag '{z.vertrag_id}'."))
        return
    if z.typ not in _GUELTIGE_NACHBUCHUNGS_TYPEN:
        befunde.append(PruefBefund(entitaet, z.import_id, "KONFLIKT", f"Eröffnungskorrektur '{z.import_id}': ungültiger typ '{z.typ}'."))
        return
    if z.betrag_cent <= 0:
        # Dieselbe typ-abgeleitete Vorzeichenlogik wie bei Nachbuchungen
        # (siehe dort) - betrag_cent ist IMMER positiv einzugeben.
        befunde.append(PruefBefund(entitaet, z.import_id, "KONFLIKT", f"Eröffnungskorrektur '{z.import_id}': betrag_cent muss positiv sein ({z.betrag_cent})."))
        return
    modus = ctx.gesamtsaldo_im_paket.get(z.vertrag_id) is not None and "GESAMTSALDO" or bestehender_modus(z.vertrag_id)
    if modus != "GESAMTSALDO":
        befunde.append(PruefBefund(
            entitaet, z.import_id, "KONFLIKT",
            f"Eröffnungskorrektur '{z.import_id}': Vertrag '{z.vertrag_id}' hat (noch) keine bestätigte "
            "GESAMTSALDO-Eröffnung - eine Korrektur setzt genau das voraus.",
        ))
        return
    bestehende_zeile = session.execute(select(OPPositionTable).where(OPPositionTable.import_id == z.import_id)).scalar_one_or_none()
    if bestehende_zeile is None:
        befunde.append(PruefBefund(entitaet, z.import_id, "NEU"))
    elif (
        bestehende_zeile.betrag_cent == z.betrag_cent
        and bestehende_zeile.typ == z.typ
        and bestehende_zeile.belegdatum == z.original_belegdatum
        and (bestehende_zeile.aenderungsgrund or "") == z.grund
    ):
        befunde.append(PruefBefund(entitaet, z.import_id, "UNVERAENDERT"))
    else:
        befunde.append(PruefBefund(entitaet, z.import_id, "KONFLIKT", f"import_id '{z.import_id}' existiert bereits mit abweichendem Inhalt."))


def sperre_ist_bereits_aktiv(session: Session, *, vertrag_id: str, grund: str, kommentar: str | None) -> bool:
    """Von `planner.py` UND `apply.py` benutzt (siehe `pruefe_paket`s
    Docstring: kein separater Prüfpfad, der von der Schreiblogik abweichen
    könnte) - eine bereits aktive, inhaltsgleiche Sperre macht einen
    erneuten Import zu einem sicheren No-Op statt zu einer zweiten, sonst
    ununterscheidbaren Sperr-Zeile."""

    statement = (
        select(SperreTable)
        .where(SperreTable.vertrag_id == vertrag_id)
        .where(SperreTable.grund == grund)
        .where(SperreTable.aufgehoben_am.is_(None))
    )
    if kommentar is None:
        statement = statement.where(SperreTable.kommentar.is_(None))
    else:
        statement = statement.where(SperreTable.kommentar == kommentar)
    return session.execute(statement).first() is not None


def _pruefe_sperre(
    z: SperreZeile, *, ctx: _Kontext, session: Session, befunde: list[PruefBefund], vertrag_bekannt: bool,
) -> None:
    entitaet = "Sperre"
    anzeige_id = f"{z.vertrag_id}:{z.grund}"
    if not vertrag_bekannt:
        befunde.append(PruefBefund(entitaet, anzeige_id, "KONFLIKT", f"Sperre referenziert unbekannten Vertrag '{z.vertrag_id}'."))
        return
    if z.grund not in _GUELTIGE_SPERRGRUENDE:
        befunde.append(PruefBefund(entitaet, anzeige_id, "KONFLIKT", f"Sperre '{anzeige_id}': ungültiger Grund '{z.grund}'."))
        return
    schluessel = (z.vertrag_id, z.grund, z.kommentar)
    if schluessel in ctx.sperren_im_paket:
        befunde.append(PruefBefund(entitaet, anzeige_id, "UNVERAENDERT"))
        return
    ctx.sperren_im_paket.add(schluessel)
    if sperre_ist_bereits_aktiv(session, vertrag_id=z.vertrag_id, grund=z.grund, kommentar=z.kommentar):
        befunde.append(PruefBefund(entitaet, anzeige_id, "UNVERAENDERT"))
    else:
        befunde.append(PruefBefund(entitaet, anzeige_id, "NEU"))


def _pruefe_komponente(
    z: KomponenteZeile, *, session: Session, befunde: list[PruefBefund], vertrag_bekannt: bool,
    objekt_ausgeschlossen: bool,
) -> None:
    entitaet = "Komponente"
    if not vertrag_bekannt:
        befunde.append(PruefBefund(entitaet, z.id, "KONFLIKT", f"Komponente '{z.id}' referenziert unbekannten Vertrag '{z.vertrag_id}'."))
        return
    if objekt_ausgeschlossen:
        befunde.append(PruefBefund(entitaet, z.id, "GESPERRT", f"Komponente '{z.id}' gehört zu einem ausgeschlossenen Objekt."))
        return
    felder = _komponente_felder(
        z.vertrag_id, z.art, z.bezeichnung, z.betrag_cent, z.ust_satz_promille, z.indexierbar, z.gueltig_von, z.gueltig_bis,
    )
    bestehend = session.get(VertragsKomponenteTable, z.id)
    if bestehend is None:
        befunde.append(PruefBefund(entitaet, z.id, "NEU"))
    elif _felder_hash(_komponente_felder(
        bestehend.vertrag_id, bestehend.art, bestehend.bezeichnung, bestehend.betrag_cent,
        bestehend.ust_satz_promille, bestehend.indexierbar, bestehend.gueltig_von, bestehend.gueltig_bis,
    )) == _felder_hash(felder):
        befunde.append(PruefBefund(entitaet, z.id, "UNVERAENDERT"))
    else:
        befunde.append(PruefBefund(entitaet, z.id, "KONFLIKT", f"Komponente '{z.id}' existiert bereits mit abweichendem Inhalt."))


def _pruefe_kaution(
    z: KautionZeile, *, session: Session, befunde: list[PruefBefund], vertrag_bekannt: bool,
) -> None:
    """STRIKTE Idempotenz wie die übrigen Stammdaten (siehe `KautionZeile`-
    Docstring): eine Kaution ist ein bestätigter Zahlungsfakt, keine laufend
    fortzuschreibende Angabe wie `MietvertragsprofilZeile` - abweichender
    Inhalt ist immer ein KONFLIKT, nie eine stille Aktualisierung."""

    entitaet = "Kaution"
    if not vertrag_bekannt:
        befunde.append(PruefBefund(entitaet, z.vertrag_id, "KONFLIKT", f"Kaution referenziert unbekannten Vertrag '{z.vertrag_id}'."))
        return
    if z.betrag_cent <= 0:
        befunde.append(PruefBefund(entitaet, z.vertrag_id, "KONFLIKT", f"Kaution für Vertrag '{z.vertrag_id}': betrag_cent muss positiv sein ({z.betrag_cent})."))
        return
    felder = _kaution_felder(z.betrag_cent, z.stichtag, z.referenz)
    bestehend = session.execute(select(KautionTable).where(KautionTable.vertrag_id == z.vertrag_id)).scalar_one_or_none()
    if bestehend is None:
        befunde.append(PruefBefund(entitaet, z.vertrag_id, "NEU"))
    elif _felder_hash(_kaution_felder(bestehend.betrag_cent, bestehend.stichtag, bestehend.referenz)) == _felder_hash(felder):
        befunde.append(PruefBefund(entitaet, z.vertrag_id, "UNVERAENDERT"))
    else:
        befunde.append(PruefBefund(entitaet, z.vertrag_id, "KONFLIKT", f"Kaution für Vertrag '{z.vertrag_id}' existiert bereits mit abweichendem Inhalt."))


def _neuestes_mietvertragsprofil(session: Session, vertrag_id: str) -> MietvertragsprofilTable | None:
    return session.execute(
        select(MietvertragsprofilTable)
        .where(MietvertragsprofilTable.vertrag_id == vertrag_id)
        .order_by(MietvertragsprofilTable.version.desc())
        .limit(1)
    ).scalar_one_or_none()


def _pruefe_mietvertragsprofil(
    z: MietvertragsprofilZeile, *, session: Session, befunde: list[PruefBefund], vertrag_bekannt: bool,
) -> None:
    """Anders als alle übrigen Stammdaten-Zeilen: ein abweichender Inhalt
    ist HIER kein KONFLIKT, sondern eine neue Version ("AKTUALISIERUNG") -
    siehe `MietvertragsprofilTable`-Docstring und `PruefBefund`-Docstring."""

    entitaet = "Mietvertragsprofil"
    if not vertrag_bekannt:
        befunde.append(PruefBefund(entitaet, z.vertrag_id, "KONFLIKT", f"Mietvertragsprofil referenziert unbekannten Vertrag '{z.vertrag_id}'."))
        return
    if z.nutzungsart not in _GUELTIGE_NUTZUNGSARTEN:
        befunde.append(PruefBefund(entitaet, z.vertrag_id, "KONFLIKT", f"Mietvertragsprofil '{z.vertrag_id}': ungültige Nutzungsart '{z.nutzungsart}'."))
        return
    if z.quelle_typ not in _GUELTIGE_MIETVERTRAGSPROFIL_QUELLEN:
        befunde.append(PruefBefund(entitaet, z.vertrag_id, "KONFLIKT", f"Mietvertragsprofil '{z.vertrag_id}': ungültiger quelle_typ '{z.quelle_typ}'."))
        return
    bestehend = _neuestes_mietvertragsprofil(session, z.vertrag_id)
    neue_felder = _mietvertragsprofil_felder_aus_zeile(z)
    if bestehend is None:
        befunde.append(PruefBefund(entitaet, z.vertrag_id, "NEU"))
    elif _felder_hash(_mietvertragsprofil_felder_aus_tabelle(bestehend)) == _felder_hash(neue_felder):
        befunde.append(PruefBefund(entitaet, z.vertrag_id, "UNVERAENDERT"))
    else:
        befunde.append(PruefBefund(
            entitaet, z.vertrag_id, "AKTUALISIERUNG",
            f"Mietvertragsprofil '{z.vertrag_id}' erhält eine neue Version (Inhalt weicht von der zuletzt "
            "gespeicherten Version ab) - bestehende Konten/OP/Sperren/Komponenten bleiben unangetastet.",
        ))


def erstelle_plan(
    paket: IntakePaket,
    *,
    session_factory: sessionmaker[Session],
    ausgeschlossene_objekte: frozenset[str] = _STANDARD_AUSGESCHLOSSENE_OBJEKTE,
) -> IntakePlan:
    """Öffentlicher Dry-run-Einstieg: öffnet eine reine Lesesession (es
    wird nie committet/geschrieben) und ruft `pruefe_paket`."""

    with session_factory() as session:
        return pruefe_paket(paket, session=session, ausgeschlossene_objekte=ausgeschlossene_objekte)
