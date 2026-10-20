"""Rein lesendes Kontoblatt-Kostenmodell (Auftrag HV-20261020-KONTOBLATT-
KOSTEN): fasst bereits bestehende `OPPositionTable`-Zeilen,
`MahnkostenBuchungTable` und `RuecklastschriftkostenTable` zu EINER
gemeinsamen Darstellung zusammen, die der Kontoauszug
(`backoffice/routes/konten.py::kontoauszug`), der PDF-Kontoauszug
(`backoffice/routes/konten.py::kontoauszug_pdf`) und der Verweis aus der
Mieterakte (`vertragsanlage_form.py`) GLEICHERMASSEN verwenden - keine
zweite, abweichende Berechnung an einer dieser Stellen. Kein neuer
Buchungsvorgang, keine neue Tabelle/Migration - ausschließlich Lesen und
Gruppieren bereits vorhandener Daten.

**Kein Doppelzählen (zentrale Fachregel, Auftrag Markus 20.10.2026):**
Mahngebühren/-pauschale, Verzugszinsen UND Rücklastschriftkosten sind,
sobald sie tatsächlich erhoben wurden, bereits als eigene OP-SOLL-Zeile
gebucht (`OPPositionTable.quelle_system in {"mahnkosten",
"ruecklastschriftkosten"}`, siehe `mahnwesen/kosten_service.py::
MahnkostenService.buche_vorschau` und `bank/ruecklastschriftkosten_
service.py::RuecklastschriftkostenService._buchen_in_session`). Der
Gesamtsaldo dieses Moduls ist deshalb IMMER exakt `OPService.
berechne_saldo(...).saldo_cent` - derselbe, bereits etablierte
Rechenweg, NIE eine zweite Summe aus Kosten-Nebentabellen PLUS der
OP-Summe (das würde jede bereits gebuchte Kostenzeile doppelt zählen).
Die Kategorie-Zuordnung unten (`Kontoblatt.kosten_zeilen` vs.
`hauptforderung_zeilen`) dient AUSSCHLIESSLICH der Anzeige/Gruppierung,
niemals der Saldoberechnung selbst - siehe `test_kontoblatt.py::
test_summe_aller_kategorien_ergibt_exakt_den_echten_saldo`.

**Dokumentiert, aber nicht belastet:** ein erfasstes Rücklastschrift-
Bankentgelt OHNE belegte Zurechenbarkeit zum Zahlungspflichtigen
(`RuecklastschriftkostenTable.kosten_op_position_id IS NULL`) hat KEINE
OP-Zeile und fließt deshalb korrekt NICHT in den Saldo ein - erscheint
hier separat in `Kontoblatt.dokumentierte_bankaufwaende` mit dem Status
"dokumentiert – nicht belastet / rechtlich in Prüfung" (Auftrag Markus
20.10.2026, wörtlich).

**Nicht gebucht, nicht "alles bezahlt":** eine Mahnlaufgruppe, deren
Kostenverarbeitung noch nicht abgeschlossen ist
(`MahnLaufTable.mahnkosten_verarbeitet_am IS NULL`, Status weder
BLOCKIERT noch UEBERSPRUNGEN - diese beiden sind ein endgültiges "wird
nie gebucht", kein "noch ausstehend") erscheint in `Kontoblatt.
ausstehende_mahnlaeufe` - das bloße FEHLEN einer gebuchten Kostenzeile
bedeutet hier ausdrücklich NICHT, dass nichts offen ist.

**Korrigierte Kostenzeilen bleiben erkennbar:** `OPService.
storniere_und_korrigiere` legt die Ersatzzeile pauschal mit
`quelle_system="korrektur"` an, unabhängig von der Kategorie der
stornierten Originalzeile. `_kategorie_fuer_position` folgt deshalb der
`storniert_durch_id`-Kette bis zu 10 Schritte rückwärts, bevor eine Zeile
als "Hauptforderung" statt als (korrigierte) Kostenzeile gilt.

**Bekannte Grenze (siehe `docs/hausverwaltung/OFFENE_PUNKTE.md`):** eine
manuell über die generische Nachbuchung (`backoffice/routes/konten.py::
nachbuchung_absenden`, `quelle_system="backoffice"`) gebuchte GUTSCHRIFT
gegen eine Kostenzeile hat KEINE strukturelle Bindung an die
Originalzeile (anders als eine `storniere_und_korrigiere`-Korrektur) und
erscheint deshalb als Hauptforderung, nicht als Kostenkorrektur - das
betrifft nur die Anzeige-Kategorie, nicht den Gesamtsaldo."""

from __future__ import annotations

from dataclasses import dataclass

from mietinkasso.domain.enums import OPTyp
from mietinkasso.infrastructure.db.tables import (
    MahnkostenBuchungTable,
    MahnLaufTable,
    OPPositionTable,
    RuecklastschriftkostenTable,
)

KATEGORIE_HAUPTFORDERUNG = "HAUPTFORDERUNG"
KATEGORIE_VERZUGSZINSEN = "VERZUGSZINSEN"
KATEGORIE_MAHNSPESEN = "MAHNSPESEN"
KATEGORIE_MAHNPAUSCHALE = "MAHNPAUSCHALE"
KATEGORIE_RUECKLASTSCHRIFTKOSTEN = "RUECKLASTSCHRIFTKOSTEN"

#: Anzeige-Beschriftung je Kostenkategorie (NICHT für HAUPTFORDERUNG -
#: die zeigt stattdessen den OP-Typ/`beleg_referenz` wie bisher).
KATEGORIE_LABEL = {
    KATEGORIE_VERZUGSZINSEN: "Verzugszinsen",
    KATEGORIE_MAHNSPESEN: "Mahnspesen (§458 UGB)",
    KATEGORIE_MAHNPAUSCHALE: "Mahnkostenpauschale (§1333 Abs 2 ABGB)",
    KATEGORIE_RUECKLASTSCHRIFTKOSTEN: "Rücklastschriftkosten (Bankentgelt)",
}

#: Dieselbe Vorzeichenregel wie `op/service.py::_effect_cent` - bewusst
#: hier DUPLIZIERT statt importiert, da jene Funktion privat und Teil der
#: tatsächlichen Saldo-BERECHNUNG ist; diese Kopie dient AUSSCHLIESSLICH
#: der Kategorie-Summenanzeige, nie dem tatsächlichen Gesamtsaldo (der
#: kommt unverändert aus `OPService.berechne_saldo(...).saldo_cent`).
_POSITIVE_TYPEN = {OPTyp.EROEFFNUNG.value, OPTyp.SOLL.value, OPTyp.RUECKLASTSCHRIFT.value}
_NEGATIVE_TYPEN = {OPTyp.GUTSCHRIFT.value, OPTyp.ZAHLUNG.value}
_ZAHLUNGS_TYPEN = {OPTyp.ZAHLUNG.value, OPTyp.RUECKLASTSCHRIFT.value}
#: Diese beiden MahnLauf-Status sind ein ENDGÜLTIGES "wird nie gebucht"
#: (Reservierung bereits freigegeben, siehe `MahnkostenGebuehrTable`-
#: Moduldoc) - KEIN "noch ausstehend".
_MAHNLAUF_STATUS_ENDGUELTIG_OHNE_KOSTEN = {"BLOCKIERT", "UEBERSPRUNGEN"}


def _effect_cent(position: OPPositionTable) -> int:
    if position.typ in _POSITIVE_TYPEN:
        return position.betrag_cent
    if position.typ in _NEGATIVE_TYPEN:
        return -position.betrag_cent
    if position.typ == OPTyp.KORREKTUR.value:
        return position.betrag_cent
    raise ValueError(f"Unbekannter OP-Typ {position.typ!r}")


@dataclass(frozen=True)
class KontoblattZeile:
    """Eine einzelne, bereits auf dem Konto AKTIVE OP-Zeile mit ihrer
    Kontoblatt-Kategorie - reine Anzeige-Gruppierung, kein eigener
    Geldwert abseits von `position.betrag_cent`/`position.typ`.
    `rest_cent` ist `None`, wenn die Zeile nicht forderungsseitig ist
    oder bereits vollständig beglichen wurde (siehe `OPService.
    offene_forderungen`)."""

    position: OPPositionTable
    kategorie: str
    rest_cent: int | None


@dataclass(frozen=True)
class DokumentierterBankaufwand:
    """Ein erfasstes, aber (noch) NICHT als Forderung gebuchtes
    Rücklastschrift-Bankentgelt - KEINE OP-Zeile, fließt daher nicht in
    den Saldo ein. `status_text` ist wörtlich der vom Auftraggeber
    vorgegebene Text."""

    row: RuecklastschriftkostenTable
    status_text: str = "dokumentiert – nicht belastet / rechtlich in Prüfung"


@dataclass(frozen=True)
class Kontoblatt:
    konto_id: str
    saldo_cent: int
    faelliger_unstrittiger_rest_cent: int
    hauptforderung_zeilen: list[KontoblattZeile]
    hauptforderung_cent: int
    kosten_zeilen: list[KontoblattZeile]
    kosten_cent: int
    zahlungen_zeilen: list[KontoblattZeile]
    zahlungen_saldo_cent: int
    dokumentierte_bankaufwaende: list[DokumentierterBankaufwand]
    ausstehende_mahnlaeufe: list[MahnLaufTable]


def _buchungs_id_kategorie(buchungen: list[MahnkostenBuchungTable]) -> dict[int, str]:
    mapping: dict[int, str] = {}
    for b in buchungen:
        if b.zinsen_op_position_id is not None:
            mapping[b.zinsen_op_position_id] = KATEGORIE_VERZUGSZINSEN
        if b.gebuehr_op_position_id is not None:
            mapping[b.gebuehr_op_position_id] = KATEGORIE_MAHNSPESEN
        if b.mahnpauschale_op_position_id is not None:
            mapping[b.mahnpauschale_op_position_id] = KATEGORIE_MAHNPAUSCHALE
    return mapping


def _storno_vorgaenger_index(positionen_alle: list[OPPositionTable]) -> dict[int, OPPositionTable]:
    """`storniert_durch_id` zeigt auf der STORNIERTEN Zeile VORWÄRTS auf
    ihren Ersatz - diese Umkehrung erlaubt es, von der aktiven
    Ersatzzeile aus RÜCKWÄRTS zur stornierten Originalzeile zu finden."""

    return {p.storniert_durch_id: p for p in positionen_alle if p.storniert_durch_id is not None}


def _kategorie_fuer_position(
    position: OPPositionTable, *, buchungs_id_kategorie: dict[int, str],
    ruecklastschriftkosten_ids: frozenset[int], vorgaenger_index: dict[int, OPPositionTable],
) -> str:
    aktuelle = position
    for _ in range(10):
        if aktuelle.id in buchungs_id_kategorie:
            return buchungs_id_kategorie[aktuelle.id]
        if aktuelle.id in ruecklastschriftkosten_ids:
            return KATEGORIE_RUECKLASTSCHRIFTKOSTEN
        if aktuelle.quelle_system != "korrektur":
            return KATEGORIE_HAUPTFORDERUNG
        vorgaenger = vorgaenger_index.get(aktuelle.id)
        if vorgaenger is None:
            return KATEGORIE_HAUPTFORDERUNG
        aktuelle = vorgaenger
    return KATEGORIE_HAUPTFORDERUNG


def erstelle_kontoblatt(
    *, konto_id: str, saldo_cent: int, faelliger_unstrittiger_rest_cent: int,
    positionen_aktiv: list[OPPositionTable], positionen_alle: list[OPPositionTable],
    offene_forderungen_rest_je_id: dict[int, int], mahnkosten_buchungen: list[MahnkostenBuchungTable],
    ruecklastschriftkosten_zeilen: list[RuecklastschriftkostenTable], mahnlaeufe: list[MahnLaufTable],
) -> Kontoblatt:
    """Reine Funktion - bekommt alle Daten bereits geladen übergeben
    (siehe `lade_kontoblatt` unten für den I/O-Teil), keine eigene
    DB-/Zeitabfrage, damit sie ohne DB direkt getestet werden kann."""

    buchungs_kategorie = _buchungs_id_kategorie(mahnkosten_buchungen)
    ruecklastschriftkosten_ids = frozenset(
        r.kosten_op_position_id for r in ruecklastschriftkosten_zeilen if r.kosten_op_position_id is not None
    )
    vorgaenger_index = _storno_vorgaenger_index(positionen_alle)

    hauptforderung_zeilen: list[KontoblattZeile] = []
    kosten_zeilen: list[KontoblattZeile] = []
    zahlungen_zeilen: list[KontoblattZeile] = []
    hauptforderung_cent = 0
    kosten_cent = 0
    zahlungen_saldo_cent = 0

    for p in positionen_aktiv:
        rest = offene_forderungen_rest_je_id.get(p.id)
        if p.typ in _ZAHLUNGS_TYPEN:
            zahlungen_zeilen.append(KontoblattZeile(position=p, kategorie=KATEGORIE_HAUPTFORDERUNG, rest_cent=rest))
            zahlungen_saldo_cent += _effect_cent(p)
            continue
        kategorie = _kategorie_fuer_position(
            p, buchungs_id_kategorie=buchungs_kategorie, ruecklastschriftkosten_ids=ruecklastschriftkosten_ids,
            vorgaenger_index=vorgaenger_index,
        )
        zeile = KontoblattZeile(position=p, kategorie=kategorie, rest_cent=rest)
        if kategorie == KATEGORIE_HAUPTFORDERUNG:
            hauptforderung_zeilen.append(zeile)
            hauptforderung_cent += _effect_cent(p)
        else:
            kosten_zeilen.append(zeile)
            kosten_cent += _effect_cent(p)

    dokumentierte_bankaufwaende = [
        DokumentierterBankaufwand(row=r) for r in ruecklastschriftkosten_zeilen if r.kosten_op_position_id is None
    ]
    ausstehende_mahnlaeufe = [
        m for m in mahnlaeufe
        if m.mahnkosten_verarbeitet_am is None and m.status not in _MAHNLAUF_STATUS_ENDGUELTIG_OHNE_KOSTEN
    ]

    return Kontoblatt(
        konto_id=konto_id, saldo_cent=saldo_cent, faelliger_unstrittiger_rest_cent=faelliger_unstrittiger_rest_cent,
        hauptforderung_zeilen=hauptforderung_zeilen, hauptforderung_cent=hauptforderung_cent,
        kosten_zeilen=kosten_zeilen, kosten_cent=kosten_cent,
        zahlungen_zeilen=zahlungen_zeilen, zahlungen_saldo_cent=zahlungen_saldo_cent,
        dokumentierte_bankaufwaende=dokumentierte_bankaufwaende, ausstehende_mahnlaeufe=ausstehende_mahnlaeufe,
    )


def lade_kontoblatt(
    *, konto_id: str, vertrag_id: str, op_service, mahnkosten_repo, ruecklastschriftkosten_repo, mahnlauf_repo,
) -> Kontoblatt:
    """I/O-Teil: lädt exakt die bereits etablierten Repositories/Services
    (keine neue Query-Logik abseits der additiven `liste_fuer_vertrag`/
    `liste_buchungen_fuer_vertrag`-Lesemethoden) und ruft dann
    `erstelle_kontoblatt` auf. Gemeinsam verwendet von `backoffice/
    routes/konten.py::kontoauszug`/`kontoauszug_pdf`."""

    saldo = op_service.berechne_saldo(konto_id)
    positionen_alle = op_service.list_alle_positionen(konto_id)
    offene_forderungen_rest_je_id = {f.op_position_id: f.rest_cent for f in op_service.offene_forderungen(konto_id)}
    mahnkosten_buchungen = mahnkosten_repo.liste_buchungen_fuer_vertrag(vertrag_id)
    ruecklastschriftkosten_zeilen = ruecklastschriftkosten_repo.liste_fuer_vertrag(vertrag_id)
    mahnlaeufe = mahnlauf_repo.list_fuer_vertrag(vertrag_id)

    return erstelle_kontoblatt(
        konto_id=konto_id, saldo_cent=saldo.saldo_cent, faelliger_unstrittiger_rest_cent=saldo.faelliger_unstrittiger_rest_cent,
        positionen_aktiv=saldo.positionen, positionen_alle=positionen_alle,
        offene_forderungen_rest_je_id=offene_forderungen_rest_je_id, mahnkosten_buchungen=mahnkosten_buchungen,
        ruecklastschriftkosten_zeilen=ruecklastschriftkosten_zeilen, mahnlaeufe=mahnlaeufe,
    )
