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
test_gebuchte_kosten_genau_einmal_beide_ruecklastschriften_separat`/
`test_lade_kontoblatt_gegen_echte_repositories` (prüfen explizit
`hauptforderung_cent + kosten_cent + zahlungen_saldo_cent == saldo_cent`).

**Status statt bloßem `rest_cent` (Codex-Abnahme 20.10.2026, P3):**
`rest_cent is None` konnte bisher sowohl "vollständig beglichen" als auch
"keine Forderung" (z. B. eine GUTSCHRIFT) bedeuten - `KontoblattZeile.
status` löst das auf (`STATUS_OFFEN`/`STATUS_TEILBEZAHLT`/
`STATUS_BEZAHLT`, `None` nur für Zeilen ohne eigenen Forderungscharakter).
Zusätzlich führt `Kontoblatt.kosten_historie` bereits STORNIERTE (ggf.
durch eine Korrektur ERSETZTE) Kostenzeilen separat von `kosten_zeilen`/
`kosten_cent` weiter - eine stornierte Kostenbuchung verschwindet dadurch
nicht spurlos, verändert aber weder die aktuelle Summe noch den
Gesamtsaldo. `hauptforderung_cent`/`kosten_cent` sind dabei IMMER die
gebuchte Sollsumme VOR Zahlungen - der tatsächliche, nach Zahlungen noch
offene Rückstand steht separat in `hauptforderung_offener_rest_cent`/
`kosten_offener_rest_cent`, damit eine bereits (teil-)bezahlte Forderung
nicht als voller Rückstand missverstanden wird.

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
betrifft nur die Anzeige-Kategorie, nicht den Gesamtsaldo.

**Offener Rest einer RUECKLASTSCHRIFT-Zeile (unabhängiger Befund nach
Commit 239e33c):** eine OP-Zeile vom Typ RUECKLASTSCHRIFT reinstalliert
eine ECHTE Forderung (sie zählt zu `_POSITIVE_TYPEN`, nicht zu einer
bloßen Zahlung) und kann selbst noch offen sein. Zur Anzeige landet sie
trotzdem in `zahlungen_zeilen` (`_ZAHLUNGS_TYPEN`, zusammen mit der
ZAHLUNG, die sie storniert) - das ist weiterhin so gewollt, damit
"Zahlungen/Stornostand" eine Zahlung und ihre eigene Rücklastschrift als
EIN zusammengehöriges Paar zeigt. Ihr `rest_cent` fehlte dadurch bisher
komplett in `hauptforderung_offener_rest_cent`/`kosten_offener_rest_cent`
(die nur über `hauptforderung_zeilen`/`kosten_zeilen` summierten) - eine
Forderung, deren EINZIGE offene Position eine wieder aufgelebte
Rücklastschrift ist, zeigte fälschlich 0,00 EUR offenen Rest. Der
tatsächlich offene Rest einer solchen Zeile wird deshalb zusätzlich,
nach derselben Kategorie-Logik wie `hauptforderung_zeilen`/
`kosten_zeilen`, in die jeweilige `..._offener_rest_cent`-Summe
eingerechnet - `zahlungen_zeilen`/`zahlungen_saldo_cent` (die Anzeige
"Zahlungen/Stornostand") bleiben dabei UNVERÄNDERT, siehe
`test_offene_ruecklastschrift_ohne_weitere_forderung_zaehlt_als_
hauptforderung_offener_rest`.

**Zusatz Zinszuordnung/Mahnbeleg (Folgeschritt zu Commit 239e33c):**
`KontoblattZeile.ledger` macht für eine VERZUGSZINSEN-/MAHNSPESEN-/
MAHNPAUSCHALE-Zeile die bereits bestehenden `MahnkostenBuchungTable`-
Ledgerdetails sichtbar (`versandnachweis_referenz`, `forderung_op_
position_ids`) - rein lesend, KEINE neue Buchung/Architektur. Für
Verzugszinsen zusätzlich `MahnkostenLedgerEintrag.zins_zuordnung`: je
zugrunde liegender Forderung (`op_position_id`) GETRENNT das
vollständige, KUMULATIVE Segment-Bild (`zins_segmente_json` - rein
deskriptiv/Audit, über alle Halbjahres-/Basiszinssatz-Teilperioden bis
zum Buchungstag) UND das an DIESEM Buchungstag tatsächlich NEU gebuchte
Delta (`zinsen_delta_je_op_json`). Diese beiden dürfen NIE verwechselt
werden (siehe `MahnkostenBuchungTable`-Moduldoc: das kumulative Segment
als "neu gebucht" auszugeben würde bei einer späteren Stufe zu doppelt
gezählten Zinsen in der ANZEIGE führen, auch wenn die tatsächliche
Buchung selbst korrekt bleibt) - `delta_cent is None` bedeutet ehrlich
"für diese Forderung an diesem Tag kein Delta gebucht", nie erfundene
0,00 EUR. Fehlt zu einer Kostenzeile ausnahmsweise die zugehörige
Buchung (z. B. eine fremd/manuell gebuchte Kostenzeile ohne
`MahnkostenBuchungTable`-Eintrag), bleibt `ledger` `None` - Anzeige
dann ausdrücklich "fehlend", nie ein erfundener Beleg."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from mietinkasso.domain.enums import OPTyp
from mietinkasso.infrastructure.db.tables import (
    ManuellerMahnversandTable,
    MahnkostenBuchungTable,
    MahnLaufTable,
    OPPositionTable,
    RuecklastschriftkostenTable,
)
#: Dieselbe bereits bestehende Profil-Auflösung wie `backoffice/routes/
#: mahnwesen.py::mahnpauschale_freigabe_formular` (reine Funktion über
#: einer bereits gelesenen Historie, kein eigener DB-Zugriff) - siehe
#: `lade_kontoblatt`/`AusstehenderManuellerMahnversand`.
from mietinkasso.mahnwesen.kosten import _profil_wirksam_am, zins_bis_einschliesslich

KATEGORIE_HAUPTFORDERUNG = "HAUPTFORDERUNG"
KATEGORIE_VERZUGSZINSEN = "VERZUGSZINSEN"
KATEGORIE_MAHNSPESEN = "MAHNSPESEN"
KATEGORIE_MAHNPAUSCHALE = "MAHNPAUSCHALE"
KATEGORIE_RUECKLASTSCHRIFTKOSTEN = "RUECKLASTSCHRIFTKOSTEN"

#: Status EINER Zeile (Codex-Abnahme 20.10.2026, P3) - löst die bisherige
#: Mehrdeutigkeit auf, bei der `rest_cent is None` sowohl "vollständig
#: bezahlt" als auch "keine Forderung" bedeuten konnte.
STATUS_OFFEN = "OFFEN"
STATUS_TEILBEZAHLT = "TEILBEZAHLT"
STATUS_BEZAHLT = "BEZAHLT"
#: Nur in `Kontoblatt.kosten_historie` (bereits STORNIERTE Zeilen):
#: STORNIERT = endgültig storniert, KEIN Ersatz; ERSETZT = storniert UND
#: durch eine Korrekturzeile (`OPService.storniere_und_korrigiere`)
#: ersetzt - die Ersatzzeile selbst führt mit ihrem EIGENEN Status
#: (offen/teilbezahlt/bezahlt) in `kosten_zeilen` fort.
STATUS_STORNIERT = "STORNIERT"
STATUS_ERSETZT = "ERSETZT"

STATUS_LABEL = {
    STATUS_OFFEN: "offen",
    STATUS_TEILBEZAHLT: "teilbezahlt",
    STATUS_BEZAHLT: "bezahlt",
    STATUS_STORNIERT: "storniert",
    STATUS_ERSETZT: "ersetzt (siehe Korrekturzeile)",
}

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
class ZinsZuordnungZeile:
    """EIN zugrunde liegender Forderungs-`op_position_id` innerhalb einer
    `MahnkostenBuchungTable`-Zeile - trennt scharf zwischen dem
    vollständigen, KUMULATIVEN Segment-Bild (`segmente`, rein deskriptiv/
    Audit, Rohdaten aus `zins_segmente_json`: je Segment `von`/`bis`/
    `satz_prozent`/`quelle`/`rest_cent`/`zinsen_cent`) und dem an DIESEM
    Buchungstag TATSÄCHLICH neu gebuchten Delta (`delta_cent`, aus
    `zinsen_delta_je_op_json`). `delta_cent is None` heißt ehrlich "für
    diese Forderung an diesem Tag kein Delta gebucht" - NIE aus
    `segmente` abgeleitet oder mit 0 verwechselt (siehe
    `MahnkostenBuchungTable`-Moduldoc)."""

    op_position_id: int
    segmente: list[dict]
    delta_cent: int | None


@dataclass(frozen=True)
class MahnkostenLedgerEintrag:
    """Die bereits bestehenden `MahnkostenBuchungTable`-Ledgerdetails zu
    EINER Kostenzeile (Verzugszinsen/Mahnspesen/Mahnpauschale) - rein
    lesend durchgereicht, keine neue Buchung/Berechnung. `zins_zuordnung`
    ist nur für die VERZUGSZINSEN-Zeile befüllt (leer bei Mahnspesen/
    -pauschale, die kein Zinssegment-Bild haben). `zins_nachweis_
    unvollstaendig` ist `True`, wenn `zins_segmente_json`/`zinsen_delta_
    je_op_json` zwar gültiges JSON, aber NICHT die erwartete Struktur
    enthalten (z. B. kein Array, ein Segment ohne `op_position_id`, ein
    nicht-numerischer Delta-Schlüssel) - Anzeige-Pflicht: als
    "unvollständiger Nachweis" kennzeichnen, NIE stillschweigend
    ignorieren, abstürzen oder als 0/bezahlt ausgeben (Codex-Abnahme,
    unabhängige Prüfung Commit 8ae0406)."""

    buchung: MahnkostenBuchungTable
    versandnachweis_referenz: str
    forderung_op_position_ids: list[int]
    zins_zuordnung: list[ZinsZuordnungZeile] = field(default_factory=list)
    zins_nachweis_unvollstaendig: bool = False


@dataclass(frozen=True)
class KontoblattZeile:
    """Eine einzelne OP-Zeile mit ihrer Kontoblatt-Kategorie - reine
    Anzeige-Gruppierung, kein eigener Geldwert abseits von `position.
    betrag_cent`/`position.typ`. `rest_cent` ist `None`, wenn die Zeile
    nicht forderungsseitig ist oder bereits vollständig beglichen wurde
    (siehe `OPService.offene_forderungen`) - `status` löst diese
    Mehrdeutigkeit ausdrücklich auf (Codex-Abnahme 20.10.2026, P3):
    `STATUS_BEZAHLT` bei vollständiger Begleichung, `None` bei einer
    Zeile ohne eigenen Forderungscharakter (z. B. GUTSCHRIFT/ZAHLUNG/
    KORREKTUR). In `Kontoblatt.kosten_zeilen` ist `status` IMMER eine der
    drei aktiven Werte (offen/teilbezahlt/bezahlt); in `Kontoblatt.
    kosten_historie` IMMER `STORNIERT`/`ERSETZT`. `ledger` ist nur für
    Verzugszinsen/Mahnspesen/Mahnpauschale gesetzt (sonst `None`) - siehe
    `MahnkostenLedgerEintrag`."""

    position: OPPositionTable
    kategorie: str
    rest_cent: int | None
    status: str | None = None
    ledger: MahnkostenLedgerEintrag | None = None


@dataclass(frozen=True)
class DokumentierterBankaufwand:
    """Ein erfasstes, aber (noch) NICHT als Forderung gebuchtes
    Rücklastschrift-Bankentgelt - KEINE OP-Zeile, fließt daher nicht in
    den Saldo ein. `status_text` ist wörtlich der vom Auftraggeber
    vorgegebene Text.

    `rueckgabe_datum` ist der TATSÄCHLICHE Bankereignistag (Codex-Abnahme
    20.10.2026, P2) - das `belegdatum` der gebundenen Rücklastschrift-
    OP-Zeile (`row.ruecklastschrift_op_position_id`), NICHT `row.
    gebucht_am` (das ist nur das spätere ERFASSUNGSdatum dieser Kosten-
    Dokumentationszeile selbst). `None`, wenn die referenzierte OP-Zeile
    nicht auffindbar ist ("Datum unbekannt" statt eines erfundenen
    Datums) - `row.gebucht_am` bleibt daneben als eigenes, korrekt
    benanntes Erfassungsdatum erhalten."""

    row: RuecklastschriftkostenTable
    rueckgabe_datum: date | None
    status_text: str = "dokumentiert – nicht belastet / rechtlich in Prüfung"


@dataclass(frozen=True)
class AusstehenderManuellerMahnversand:
    """EIN noch nicht kostenfreigegebener Manualimport-Beleg (`row.
    pauschale_abgegolten=False`) mitsamt dem AKTUELL geltenden, rein
    unverbindlichen Mahnpauschale-Prüfwert (Codex-Rückprüfung 22.10.2026,
    echter Anzeige-Mangel - vorherige Fassung zeigte Referenz/Datum, aber
    KEINEN Betrag, obwohl ausdrücklich "ungeprüfte Ansätze sichtbar"
    gefordert war).

    `vorschlag_cent` ist GENAU derselbe unverbindliche Prüfwert, den auch
    `backoffice/routes/mahnwesen.py::mahnpauschale_freigabe_formular`
    anzeigt (`ZinsprofilTable.mahnpauschale_kostenbasis_cent`, am echten
    Tagesdatum wirksames Profil) - KEINE zweite, eigene Berechnung/kein
    zweiter Ledger, nur derselbe bereits bestehende Konfigurationswert an
    einer zweiten Stelle angezeigt. `None`, wenn kein Profilwert
    konfiguriert ist - NIE eine erfundene Zahl. Dieser Wert wird NIE
    gebucht/versandt, er ist ausschließlich eine "in Prüfung, NICHT im
    Saldo"-Anzeige; die tatsächliche Buchung bleibt ausschließlich der
    expliziten Freigabe (`mahnpauschale-freigabe?fuer_manualimport_
    row_id=...`) und Nachbuchung vorbehalten."""

    row: ManuellerMahnversandTable
    vorschlag_cent: int | None


@dataclass(frozen=True)
class Kontoblatt:
    konto_id: str
    saldo_cent: int
    faelliger_unstrittiger_rest_cent: int
    hauptforderung_zeilen: list[KontoblattZeile]
    #: Gebuchte Sollsumme VOR Zahlungen (Codex-Abnahme 20.10.2026, P3 -
    #: NICHT mit dem aktuellen offenen Rest verwechseln, siehe
    #: `hauptforderung_offener_rest_cent`).
    hauptforderung_cent: int
    #: Summe der tatsächlich NOCH offenen Beträge (`rest_cent`) über alle
    #: `hauptforderung_zeilen` - das ist der tatsächliche Rückstand,
    #: NICHT `hauptforderung_cent` (das bliebe nach einer Zahlung
    #: unverändert hoch und würde als Rückstand missverstanden).
    hauptforderung_offener_rest_cent: int
    kosten_zeilen: list[KontoblattZeile]
    #: Gebuchte Sollsumme VOR Zahlungen (siehe oben, analog Hauptforderung).
    kosten_cent: int
    #: Summe der tatsächlich noch offenen Kostenbeträge (siehe oben).
    kosten_offener_rest_cent: int
    #: Bereits STORNIERTE (ggf. durch eine Korrektur ERSETZTE) Kosten-
    #: zeilen - Codex-Abnahme 20.10.2026, P3: separat von `kosten_zeilen`/
    #: `kosten_cent` geführt, verändert weder Summe noch Saldo, macht
    #: eine stornierte Kostenbuchung aber nachvollziehbar statt sie
    #: stillschweigend verschwinden zu lassen.
    kosten_historie: list[KontoblattZeile]
    zahlungen_zeilen: list[KontoblattZeile]
    zahlungen_saldo_cent: int
    dokumentierte_bankaufwaende: list[DokumentierterBankaufwand]
    ausstehende_mahnlaeufe: list[MahnLaufTable]
    #: Bereits STORNIERTE (ggf. ERSETZTE) Hauptforderungszeilen - additiv
    #: ergänzt (Auftrag HV-20261022-EIN-KONTOBLATT), symmetrisch zu
    #: `kosten_historie`, aber für die Hauptforderungs-Kategorie. Default
    #: leer, damit bestehende direkte `Kontoblatt(...)`-Konstruktionen in
    #: Tests ohne dieses Feld unverändert weiterlaufen. Wird benötigt,
    #: damit das EINE chronologische Journal (`erstelle_journal`) eine
    #: stornierte/ersetzte Miet-/Zahlungszeile weiterhin nachvollziehbar
    #: zeigt, nachdem die bisherige, redundante rohe OP-Tabelle
    #: (`konten.py::op_tabelle`) entfällt.
    hauptforderung_historie: list[KontoblattZeile] = field(default_factory=list)
    #: Additiv ergänzt (unabhängige Rückprüfung Codex 22.10.2026, Fix6,
    #: echter Bug): ein Manualimport OHNE Kosten-Freigabe (`pauschale_
    #: abgegolten=False`, also bislang 0 EUR gebucht) erschien bisher
    #: NIRGENDS auf dem gemeinsamen Kontoblatt - weder HTML noch PDF noch
    #: Mieterakte luden überhaupt `ManuellerMahnversandTable`-Zeilen.
    #: Zeigt hier dieselben, bereits dokumentierten Belege (Stufe/Datum/
    #: Kanal/Belegreferenz) als "in Prüfung, noch nicht gebucht" - KEINE
    #: erfundene 0,00-EUR-Summe, nur die reale Existenz des noch
    #: ungebuchten Vorgangs. Seit der zweiten Rückprüfung (22.10.2026,
    #: echter Anzeige-Mangel) trägt jeder Eintrag zusätzlich den
    #: unverbindlichen Mahnpauschale-Prüfwert aus der AKTUELL geltenden
    #: Konfiguration (`AusstehenderManuellerMahnversand.vorschlag_cent`,
    #: siehe dort - "ungeprüfte Ansätze sichtbar", NIE gebucht). Default
    #: leer, damit bestehende direkte `Kontoblatt(...)`-Konstruktionen in
    #: Tests unverändert weiterlaufen.
    ausstehende_manuelle_mahnversand: list[AusstehenderManuellerMahnversand] = field(default_factory=list)


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


def _kategorie_und_quelle_id_fuer_position(
    position: OPPositionTable, *, buchungs_id_kategorie: dict[int, str],
    ruecklastschriftkosten_ids: frozenset[int], vorgaenger_index: dict[int, OPPositionTable],
) -> tuple[str, int | None]:
    """Wie die bisherige `_kategorie_fuer_position`, gibt zusätzlich die
    tatsächlich TREFFENDE (Mahnkosten-/Rücklastschriftkosten-)Id zurück -
    bei einer korrigierten Zeile die der URSPRÜNGLICHEN (ggf. bereits
    stornierten) Position, NICHT `position.id` selbst. Diese Id ist der
    Schlüssel, unter dem `MahnkostenBuchungTable` weiterhin referenziert
    (siehe Moduldoc "Korrigierte Kostenzeilen bleiben erkennbar") - und
    damit auch der Schlüssel, um die zugehörigen Ledgerdetails
    (`MahnkostenLedgerEintrag`) wiederzufinden. `None`, wenn keine
    Mahnkosten-/Rücklastschriftkosten-Zuordnung existiert (Hauptforderung
    ohne Kostenbezug)."""

    aktuelle = position
    for _ in range(10):
        if aktuelle.id in buchungs_id_kategorie:
            return buchungs_id_kategorie[aktuelle.id], aktuelle.id
        if aktuelle.id in ruecklastschriftkosten_ids:
            return KATEGORIE_RUECKLASTSCHRIFTKOSTEN, aktuelle.id
        if aktuelle.quelle_system != "korrektur":
            return KATEGORIE_HAUPTFORDERUNG, None
        vorgaenger = vorgaenger_index.get(aktuelle.id)
        if vorgaenger is None:
            return KATEGORIE_HAUPTFORDERUNG, None
        aktuelle = vorgaenger
    return KATEGORIE_HAUPTFORDERUNG, None


def _buchungs_id_zu_buchung(buchungen: list[MahnkostenBuchungTable]) -> dict[int, MahnkostenBuchungTable]:
    """Wie `_buchungs_id_kategorie`, aber auf die vollständige Buchungs-
    zeile statt nur die Kategorie gemappt - Grundlage für `_ledger_fuer`."""

    mapping: dict[int, MahnkostenBuchungTable] = {}
    for b in buchungen:
        if b.zinsen_op_position_id is not None:
            mapping[b.zinsen_op_position_id] = b
        if b.gebuehr_op_position_id is not None:
            mapping[b.gebuehr_op_position_id] = b
        if b.mahnpauschale_op_position_id is not None:
            mapping[b.mahnpauschale_op_position_id] = b
    return mapping


def _parse_zins_zuordnung(buchung: MahnkostenBuchungTable) -> tuple[list[ZinsZuordnungZeile], bool]:
    """Zusatz Zinszuordnung (Folgeschritt zu Commit 239e33c): parst
    `zins_segmente_json` (vollständiges, kumulatives Segment-Bild) und
    `zinsen_delta_je_op_json` (das an DIESEM Tag tatsächlich NEU gebuchte
    Delta) GETRENNT je `op_position_id` - niemals das eine aus dem
    anderen ableiten (siehe `MahnkostenBuchungTable`-Moduldoc).

    Unabhängige Prüfung Commit 8ae0406, echter Bug: bisher wurde nur die
    JSON-SYNTAX geprüft - gültiges JSON mit der FALSCHEN Struktur (kein
    Array, `[null]`, ein Segment ohne `op_position_id`, ein nicht-
    numerischer Delta-Schlüssel) führte zu einer rohen Exception (500)
    statt einer ehrlichen Anzeige. Jetzt wird jede Struktur-Erwartung
    EINZELN geprüft, ein fehlerhaftes Element wird übersprungen (NIE als
    0/bezahlt/gebucht fehlinterpretiert) und der zweite Rückgabewert
    (`unvollstaendig`) zeigt an, dass mindestens ein Element nicht der
    erwarteten Struktur entsprach - die Anzeige muss das als
    "unvollständiger Nachweis" kennzeichnen, nicht stillschweigend
    ignorieren."""

    unvollstaendig = False

    try:
        segmente_roh = json.loads(buchung.zins_segmente_json)
    except (TypeError, ValueError):
        segmente_roh = None
    if segmente_roh is None:
        segmente_roh = []
    elif not isinstance(segmente_roh, list):
        unvollstaendig = True
        segmente_roh = []

    try:
        delta_roh = json.loads(buchung.zinsen_delta_je_op_json)
    except (TypeError, ValueError):
        delta_roh = None
    if delta_roh is None:
        delta_roh = {}
    elif not isinstance(delta_roh, dict):
        unvollstaendig = True
        delta_roh = {}

    segmente_je_op: dict[int, list[dict]] = {}
    for segment in segmente_roh:
        op_id = segment.get("op_position_id") if isinstance(segment, dict) else None
        if not isinstance(op_id, int):
            unvollstaendig = True
            continue
        segmente_je_op.setdefault(op_id, []).append(segment)

    delta_je_op: dict[int, int] = {}
    for roh_schluessel, roh_wert in delta_roh.items():
        try:
            op_id = int(roh_schluessel)
        except (TypeError, ValueError):
            unvollstaendig = True
            continue
        if not isinstance(roh_wert, int):
            unvollstaendig = True
            continue
        delta_je_op[op_id] = roh_wert

    op_ids = sorted(set(segmente_je_op) | set(delta_je_op))
    zeilen = [
        ZinsZuordnungZeile(op_position_id=op_id, segmente=segmente_je_op.get(op_id, []), delta_cent=delta_je_op.get(op_id))
        for op_id in op_ids
    ]
    return zeilen, unvollstaendig


def _ledger_fuer(kategorie: str, buchung: MahnkostenBuchungTable | None) -> MahnkostenLedgerEintrag | None:
    """`None`, wenn keine Buchung gefunden wurde - Anzeige dann
    ausdrücklich "fehlend", NIE ein erfundener Beleg (siehe Moduldoc)."""

    if buchung is None:
        return None
    try:
        forderung_ids_roh = json.loads(buchung.forderung_op_position_ids)
        forderung_ids = sorted(i for i in forderung_ids_roh if isinstance(i, int)) if isinstance(forderung_ids_roh, list) else []
    except (TypeError, ValueError):
        forderung_ids = []
    zins_zuordnung: list[ZinsZuordnungZeile] = []
    zins_nachweis_unvollstaendig = False
    if kategorie == KATEGORIE_VERZUGSZINSEN:
        zins_zuordnung, zins_nachweis_unvollstaendig = _parse_zins_zuordnung(buchung)
    return MahnkostenLedgerEintrag(
        buchung=buchung, versandnachweis_referenz=buchung.versandnachweis_referenz,
        forderung_op_position_ids=forderung_ids, zins_zuordnung=zins_zuordnung,
        zins_nachweis_unvollstaendig=zins_nachweis_unvollstaendig,
    )


def _status_fuer_aktive_zeile(position: OPPositionTable, rest_cent: int | None) -> str | None:
    """`None` für eine Zeile OHNE eigenen Forderungscharakter (GUTSCHRIFT/
    ZAHLUNG/KORREKTUR - die mindern eine ANDERE Forderung, haben aber
    keinen eigenen offenen Rest). Für eine echte Forderung (EROEFFNUNG/
    SOLL/RUECKLASTSCHRIFT mit positivem Betrag) ist `rest_cent is None`
    laut `OPService.offene_forderungen` eindeutig "vollständig beglichen"
    (die Funktion gibt eine Forderung mit `rest_cent <= 0` gar nicht erst
    zurück) - NIE mehr mit "keine Forderung" verwechselbar."""

    if position.typ not in _POSITIVE_TYPEN or position.betrag_cent <= 0:
        return None
    if rest_cent is None:
        return STATUS_BEZAHLT
    if rest_cent >= position.betrag_cent:
        return STATUS_OFFEN
    if rest_cent > 0:
        return STATUS_TEILBEZAHLT
    return STATUS_BEZAHLT


def erstelle_kontoblatt(
    *, konto_id: str, saldo_cent: int, faelliger_unstrittiger_rest_cent: int,
    positionen_aktiv: list[OPPositionTable], positionen_alle: list[OPPositionTable],
    offene_forderungen_rest_je_id: dict[int, int], mahnkosten_buchungen: list[MahnkostenBuchungTable],
    ruecklastschriftkosten_zeilen: list[RuecklastschriftkostenTable], mahnlaeufe: list[MahnLaufTable],
    manuelle_mahnversand_zeilen: list[ManuellerMahnversandTable] = (),
    manuelle_mahnversand_vorschlag_cent: int | None = None,
) -> Kontoblatt:
    """Reine Funktion - bekommt alle Daten bereits geladen übergeben
    (siehe `lade_kontoblatt` unten für den I/O-Teil), keine eigene
    DB-/Zeitabfrage, damit sie ohne DB direkt getestet werden kann.

    `manuelle_mahnversand_vorschlag_cent` ist der bereits AUSSERHALB
    (von `lade_kontoblatt`, am echten Tagesdatum) ermittelte,
    unverbindliche Mahnpauschale-Prüfwert aus der aktuell geltenden
    Zinsprofil-Konfiguration - diese Funktion selbst liest dafür KEINE
    eigene Historie/Uhrzeit, sondern reicht den einen bereits fertigen
    Wert nur an jeden ausstehenden Manualimport-Beleg durch (siehe
    `AusstehenderManuellerMahnversand`-Docstring)."""

    buchungs_kategorie = _buchungs_id_kategorie(mahnkosten_buchungen)
    buchungen_by_id = _buchungs_id_zu_buchung(mahnkosten_buchungen)
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
    # Unabhängiger Befund nach Commit 239e33c (siehe Moduldoc "Offener
    # Rest einer RUECKLASTSCHRIFT-Zeile"): zusätzlich zur Summe über
    # `hauptforderung_zeilen`/`kosten_zeilen` unten.
    hauptforderung_offener_rest_zusatz_cent = 0
    kosten_offener_rest_zusatz_cent = 0

    for p in positionen_aktiv:
        rest = offene_forderungen_rest_je_id.get(p.id)
        status = _status_fuer_aktive_zeile(p, rest)
        if p.typ in _ZAHLUNGS_TYPEN:
            zahlungen_zeilen.append(KontoblattZeile(position=p, kategorie=KATEGORIE_HAUPTFORDERUNG, rest_cent=rest, status=status))
            zahlungen_saldo_cent += _effect_cent(p)
            # Eine RUECKLASTSCHRIFT-Zeile reinstalliert eine ECHTE
            # Forderung und kann selbst noch offen sein - dieser Rest
            # fehlte bisher komplett in den `..._offener_rest_cent`-
            # Summen, weil die Zeile zur ANZEIGE in `zahlungen_zeilen`
            # steht (das bleibt unverändert so). Nur der tatsächlich
            # offene Rest wird hier zusätzlich derselben Kategorie
            # zugeschlagen, die `hauptforderung_zeilen`/`kosten_zeilen`
            # auch für eine gewöhnliche Forderung verwenden würden.
            if rest:
                zahlungstyp_kategorie, _ = _kategorie_und_quelle_id_fuer_position(
                    p, buchungs_id_kategorie=buchungs_kategorie, ruecklastschriftkosten_ids=ruecklastschriftkosten_ids,
                    vorgaenger_index=vorgaenger_index,
                )
                if zahlungstyp_kategorie == KATEGORIE_HAUPTFORDERUNG:
                    hauptforderung_offener_rest_zusatz_cent += rest
                else:
                    kosten_offener_rest_zusatz_cent += rest
            continue
        kategorie, quelle_id = _kategorie_und_quelle_id_fuer_position(
            p, buchungs_id_kategorie=buchungs_kategorie, ruecklastschriftkosten_ids=ruecklastschriftkosten_ids,
            vorgaenger_index=vorgaenger_index,
        )
        ledger = _ledger_fuer(kategorie, buchungen_by_id.get(quelle_id)) if quelle_id is not None else None
        zeile = KontoblattZeile(position=p, kategorie=kategorie, rest_cent=rest, status=status, ledger=ledger)
        if kategorie == KATEGORIE_HAUPTFORDERUNG:
            hauptforderung_zeilen.append(zeile)
            hauptforderung_cent += _effect_cent(p)
        else:
            kosten_zeilen.append(zeile)
            kosten_cent += _effect_cent(p)

    # Codex-Abnahme 20.10.2026, P3: "gebuchte Sollsumme vor Zahlungen"
    # (`hauptforderung_cent`/`kosten_cent`, oben) ist NICHT derselbe Wert
    # wie der tatsächlich noch offene Rückstand - der kommt separat aus
    # der Summe der je Zeile bereits korrekt verteilten `rest_cent`-Werte
    # (`OPService.offene_forderungen`), NIE aus einer zweiten eigenen
    # Zahlungsverteilung. Zusätzlich fließt der oben ermittelte Rest
    # offener RUECKLASTSCHRIFT-Zeilen ein (unabhängiger Befund nach
    # Commit 239e33c).
    hauptforderung_offener_rest_cent = sum(z.rest_cent or 0 for z in hauptforderung_zeilen) + hauptforderung_offener_rest_zusatz_cent
    kosten_offener_rest_cent = sum(z.rest_cent or 0 for z in kosten_zeilen) + kosten_offener_rest_zusatz_cent

    # Codex-Abnahme 20.10.2026, P3: bereits STORNIERTE Kostenzeilen bleiben
    # als eigene Historie sichtbar (NIE Teil von `kosten_zeilen`/
    # `kosten_cent` - das würde den Gesamtsaldo nicht ändern, aber eine
    # stornierte Buchung fälschlich wie eine noch gültige behandeln).
    # `_kategorie_und_quelle_id_fuer_position` erkennt eine stornierte
    # Kostenzeile auch dann noch korrekt, wenn `MahnkostenBuchungTable`/
    # `RuecklastschriftkostenTable` weiterhin auf ihre (jetzt stornierte)
    # ID verweisen - diese Fremdreferenzen werden bei einer Korrektur NIE
    # nachträglich umgeschrieben.
    kosten_historie: list[KontoblattZeile] = []
    # Additiv ergänzt (Auftrag HV-20261022-EIN-KONTOBLATT): bis hierher
    # wurde eine stornierte/ersetzte Hauptforderungszeile (z. B. eine
    # korrigierte Miet-/Zahlungsbuchung) bewusst übersprungen - sie blieb
    # ausschließlich über die separate rohe OP-Tabelle sichtbar
    # (`konten.py::op_tabelle`). Mit dem EINEN chronologischen Journal
    # entfällt diese zweite Tabelle, deshalb wird sie jetzt ins eigene,
    # symmetrische `hauptforderung_historie`-Feld aufgenommen statt
    # stillschweigend zu verschwinden.
    hauptforderung_historie: list[KontoblattZeile] = []
    for p in positionen_alle:
        if p.status != "STORNIERT":
            continue
        kategorie, quelle_id = _kategorie_und_quelle_id_fuer_position(
            p, buchungs_id_kategorie=buchungs_kategorie, ruecklastschriftkosten_ids=ruecklastschriftkosten_ids,
            vorgaenger_index=vorgaenger_index,
        )
        ledger = _ledger_fuer(kategorie, buchungen_by_id.get(quelle_id)) if quelle_id is not None else None
        status = STATUS_ERSETZT if p.storniert_durch_id is not None else STATUS_STORNIERT
        zeile = KontoblattZeile(position=p, kategorie=kategorie, rest_cent=None, status=status, ledger=ledger)
        if kategorie == KATEGORIE_HAUPTFORDERUNG:
            hauptforderung_historie.append(zeile)
        else:
            kosten_historie.append(zeile)

    # Codex-Abnahme 20.10.2026, P2: das tatsächliche Rückgabedatum kommt
    # aus der gebundenen Rücklastschrift-OP-Zeile (`belegdatum`), NICHT
    # aus `row.gebucht_am` (das ist nur der spätere Erfassungszeitpunkt
    # DIESER Kosten-Dokumentationszeile). `positionen_alle` statt nur
    # `positionen_aktiv`, damit das Datum auch dann noch auffindbar ist,
    # wenn die Rücklastschrift-OP-Zeile selbst zwischenzeitlich storniert
    # wurde - fehlt der Bezug ganz, wird NIE ein Datum erfunden.
    positionen_alle_by_id = {p.id: p for p in positionen_alle}
    dokumentierte_bankaufwaende = [
        DokumentierterBankaufwand(
            row=r,
            rueckgabe_datum=(
                positionen_alle_by_id[r.ruecklastschrift_op_position_id].belegdatum
                if r.ruecklastschrift_op_position_id in positionen_alle_by_id else None
            ),
        )
        for r in ruecklastschriftkosten_zeilen if r.kosten_op_position_id is None
    ]
    ausstehende_mahnlaeufe = [
        m for m in mahnlaeufe
        if m.mahnkosten_verarbeitet_am is None and m.status not in _MAHNLAUF_STATUS_ENDGUELTIG_OHNE_KOSTEN
    ]
    # Fix6 (unabhängige Rückprüfung Codex 22.10.2026): ein Manualimport,
    # dessen Kostenfreigabe noch aussteht, bleibt `pauschale_abgegolten=
    # False` - genau das ist "noch nicht gebucht, in Prüfung" (siehe
    # `ManuellerMahnversandTable`-Moduldoc). Eine bereits abgegoltene
    # Zeile ist bereits über ihre eigene OP-/Ledgerzeile in `kosten_
    # zeilen` sichtbar und erscheint hier bewusst NICHT noch einmal.
    ausstehende_manuelle_mahnversand = [
        AusstehenderManuellerMahnversand(row=z, vorschlag_cent=manuelle_mahnversand_vorschlag_cent)
        for z in manuelle_mahnversand_zeilen if not z.pauschale_abgegolten
    ]

    return Kontoblatt(
        konto_id=konto_id, saldo_cent=saldo_cent, faelliger_unstrittiger_rest_cent=faelliger_unstrittiger_rest_cent,
        hauptforderung_zeilen=hauptforderung_zeilen, hauptforderung_cent=hauptforderung_cent,
        hauptforderung_offener_rest_cent=hauptforderung_offener_rest_cent,
        kosten_zeilen=kosten_zeilen, kosten_cent=kosten_cent, kosten_offener_rest_cent=kosten_offener_rest_cent,
        kosten_historie=kosten_historie,
        zahlungen_zeilen=zahlungen_zeilen, zahlungen_saldo_cent=zahlungen_saldo_cent,
        dokumentierte_bankaufwaende=dokumentierte_bankaufwaende, ausstehende_mahnlaeufe=ausstehende_mahnlaeufe,
        hauptforderung_historie=hauptforderung_historie,
        ausstehende_manuelle_mahnversand=ausstehende_manuelle_mahnversand,
    )


#: Anzeige-Bezeichnung je Buchungsart im Journal (Auftrag HV-20261022-
#: EIN-KONTOBLATT) - rein deskriptiv, KEINE neue Typisierung: beruht
#: ausschließlich auf dem bereits vorhandenen `OPTyp` + derselben
#: Kategorie-Zuordnung, die `kosten_zeilen`/`hauptforderung_zeilen` schon
#: verwenden.
_BUCHUNGSART_KOSTEN_LABEL = {
    KATEGORIE_VERZUGSZINSEN: "Zinsen",
    KATEGORIE_MAHNSPESEN: "Mahnspesen (§458 UGB)",
    KATEGORIE_MAHNPAUSCHALE: "Mahngebühr (§1333 Abs 2 ABGB)",
    KATEGORIE_RUECKLASTSCHRIFTKOSTEN: "Bankkosten (Rücklastschrift-Entgelt)",
}


def _buchungsart_label(position: OPPositionTable, kategorie: str) -> str:
    typ = position.typ
    if typ == OPTyp.EROEFFNUNG.value:
        return "Eröffnung"
    if typ == OPTyp.ZAHLUNG.value:
        return "Zahlung"
    if typ == OPTyp.GUTSCHRIFT.value:
        return "Gutschrift"
    if typ == OPTyp.RUECKLASTSCHRIFT.value:
        return "Retourkapital (Rücklastschrift)"
    if typ == OPTyp.KORREKTUR.value:
        return "Korrektur"
    if typ == OPTyp.SOLL.value:
        return _BUCHUNGSART_KOSTEN_LABEL.get(kategorie, "Miete/Vorschreibung")
    return typ


def _forderungsbezug(zeile: KontoblattZeile) -> str:
    """Beleg-/Forderungszuordnung EINER Journalzeile - für eine Kosten-
    zeile mit Ledger die zugrunde liegenden Forderungs-`op_position_id`s
    (identisch zu `_kontoblatt_ledger_html`), sonst die eigene OP-Id."""

    if zeile.ledger is not None and zeile.ledger.forderung_op_position_ids:
        return ", ".join(f"#{i}" for i in zeile.ledger.forderung_op_position_ids)
    return f"OP #{zeile.position.id}"


@dataclass(frozen=True)
class JournalZeile:
    """EINE Zeile des gemeinsamen, chronologischen Kontoblatt-Journals
    (Auftrag HV-20261022-EIN-KONTOBLATT) - reine Anzeige-Umformung der
    bereits in `Kontoblatt` zusammengeführten/saldierten Zeilen zu EINER
    nach Datum sortierten Tabelle mit laufendem Saldo. KEINE neue
    Buchungslogik, KEINE zweite Berechnung: `laufender_saldo_cent` endet
    nach der letzten `beeinflusst_saldo=True`-Zeile exakt bei `Kontoblatt.
    saldo_cent` (siehe `test_kontoblatt.py`). Eine Zeile aus `kosten_
    historie` (storniert/ersetzt) erscheint chronologisch eingeordnet,
    aber mit `beeinflusst_saldo=False` und unverändertem `laufender_
    saldo_cent` - sie bleibt sichtbar (Nachvollziehbarkeit), verändert
    aber nie den fortlaufenden Saldo."""

    datum: date
    buchungsart: str
    soll_cent: int
    haben_cent: int
    laufender_saldo_cent: int
    beleg_referenz: str
    forderungsbezug: str
    kategorie: str
    status: str | None
    beeinflusst_saldo: bool
    zeile: KontoblattZeile


def erstelle_journal(kontoblatt: Kontoblatt) -> list[JournalZeile]:
    """Fasst Hauptforderung/Kosten/Zahlungen EINES bereits geladenen
    `Kontoblatt` zu EINER gemeinsamen, chronologischen Tabelle mit
    laufendem Saldo zusammen (Auftrag HV-20261022-EIN-KONTOBLATT: "Miete,
    Zahlung, Gutschrift, Retourkapital, Mahngebühren, Bankkosten und
    Zinsen in EINER Tabelle"). Reine Funktion ohne eigenen I/O/Rechenweg -
    verwendet ausschließlich bereits in `Kontoblatt` vorhandene Werte
    (`_effect_cent`, dieselbe Vorzeichenregel wie `op/service.py`), damit
    niemals eine zweite, vom etablierten Saldo abweichende Engine
    entsteht."""

    aktiv = [
        (z, False) for z in (
            list(kontoblatt.hauptforderung_zeilen) + list(kontoblatt.kosten_zeilen) + list(kontoblatt.zahlungen_zeilen)
        )
    ]
    historie = [(z, True) for z in kontoblatt.kosten_historie] + [(z, True) for z in kontoblatt.hauptforderung_historie]
    alle = sorted(aktiv + historie, key=lambda t: (t[0].position.belegdatum, t[0].position.id, t[1]))

    ergebnis: list[JournalZeile] = []
    laufend = 0
    for zeile, ist_historie in alle:
        p = zeile.position
        if ist_historie:
            # Der ursprüngliche Betrag bleibt SICHTBAR (Nachvollziehbarkeit,
            # "Altdaten/Stornos erhalten") - nur `laufend` wird NICHT
            # fortgeschrieben, damit eine stornierte/ersetzte Zeile den
            # laufenden Saldo nie verändert.
            historie_effekt = _effect_cent(p)
            buchungsart = _buchungsart_label(p, zeile.kategorie) + " (" + STATUS_LABEL.get(zeile.status, zeile.status or "") + ")"
            ergebnis.append(JournalZeile(
                datum=p.belegdatum, buchungsart=buchungsart,
                soll_cent=historie_effekt if historie_effekt > 0 else 0,
                haben_cent=-historie_effekt if historie_effekt < 0 else 0,
                laufender_saldo_cent=laufend, beleg_referenz=p.beleg_referenz or p.aenderungsgrund or "",
                forderungsbezug=_forderungsbezug(zeile), kategorie=zeile.kategorie, status=zeile.status,
                beeinflusst_saldo=False, zeile=zeile,
            ))
            continue
        effekt = _effect_cent(p)
        laufend += effekt
        ergebnis.append(JournalZeile(
            datum=p.belegdatum, buchungsart=_buchungsart_label(p, zeile.kategorie),
            soll_cent=effekt if effekt > 0 else 0, haben_cent=-effekt if effekt < 0 else 0,
            laufender_saldo_cent=laufend, beleg_referenz=p.beleg_referenz or p.aenderungsgrund or "",
            forderungsbezug=_forderungsbezug(zeile), kategorie=zeile.kategorie, status=zeile.status,
            beeinflusst_saldo=True, zeile=zeile,
        ))
    return ergebnis


#: Österreichische Monatsnamen ("Jänner", nicht "Januar") für die kurze,
#: menschlich verständliche Empfänger-Tabelle (Auftrag HV-20261023-
#: MAHNUNG-EINFACH).
_MONATSNAMEN = {
    "01": "Jänner", "02": "Februar", "03": "März", "04": "April", "05": "Mai", "06": "Juni",
    "07": "Juli", "08": "August", "09": "September", "10": "Oktober", "11": "November", "12": "Dezember",
}


def _monat_jahr_text(leistungsperiode: str) -> str:
    """`leistungsperiode` ist das strukturierte `OPPositionTable`-Feld
    (z. B. "2026-01", NICHT geraten/geparst aus `beleg_referenz`) -
    liefert "Jänner 2026". Bei unerwartetem Format wird der Rohwert
    unverändert zurückgegeben (NIE ein erfundenes Datum)."""

    try:
        jahr, monat_nr = leistungsperiode.split("-")
        return f"{_MONATSNAMEN.get(monat_nr, monat_nr)} {jahr}"
    except (ValueError, AttributeError):
        return leistungsperiode


# Nachtrag Markus 23.10.2026 (Abnahme-Hinweis zu Commit fdda658, danach
# ZWEIMAL KORRIGIERT nach Quellabnahme 329b1fc/0c5343c): bisher nannte
# `_einfache_buchung_text` JEDE nicht als Kosten klassifizierte
# SOLL-Zeile pauschal "Mietvorschreibung", unabhängig von ihrer
# tatsächlichen fachlichen Komponente - eine Heizkostenabrechnung 2025
# erschien dadurch irreführend als "Mietvorschreibung September 2026".
#
# **Korrektur nach Quellabnahme 0c5343c (echter struktureller
# Fachfehler, zweite Runde):** die Vorfassung erlaubte innerhalb des
# erkannten HV-VS-Referenzfelds beliebigen Resttext (`referenz: str`
# ungeprüft) - dadurch wurde z. B. "J.123; Abrechnung2025" komplett als
# EIN Referenzfeld verschluckt und das Abrechnungsjahr ignoriert. Und
# das eigenständige Abrechnungsfeld erlaubte eine beliebige `.*?`-
# Vorsilbe - dadurch akzeptierte es weiterhin JEDE fremde Zeichenkette,
# solange sie zufällig mit "Abrechnung<Jahr>" endete (z. B. die völlig
# unabhängige Familie-Wasser-Referenz). Jetzt eine einfache, EXAKTE
# Grammatik OHNE neue `.*`-Vorsilben:
#
# (A) Nach Entfernen eines bekannten Korrektur-Präfix wird der Text an
#     ";" in getrimmte Felder zerlegt. Das HV-VS-Schema ist NUR bei
#     GENAU 3 oder 4 Feldern zulässig: Feld 0 muss vollständig "HV VS
#     <Monat>/<Jahr>" sein (Monat 1-12), Feld 1 ist das Fachbezeichnungs-
#     feld, Feld 2 muss vollständig "J." plus Ziffern sein, ein
#     optionales Feld 3 muss vollständig "Abrechnung <vierstelliges
#     Jahr>" sein. NUR Feld 1 liefert die Komponente (per exaktem
#     Whitelist-Vergleich, NIE Wortsuche), NUR Feld 3 liefert das
#     Abrechnungsjahr. Erfüllt irgendein Pflichtfeld sein eigenes Muster
#     NICHT, ist die GESAMTE Zeichenkette unbekannt (`None`).
# (B) Enthält der Text KEIN Semikolon, ist NUR ein vollständiger
#     Literal-Treffer auf eine der bekannten Formen "<Komponente>
#     abrechnung <Jahr>" bzw. "Abrechnung <Jahr>" zulässig
#     (`_EINZELFELD_ABRECHNUNG_MUSTER`, `fullmatch`) - kein beliebiger
#     Vorsilbentext, keine Wortsuche.
# (C) Das Fachbezeichnungsfeld (Feld 1) wird dafür zuerst um einen
#     bekannten Mwst-/Prozent-Zusatz gekürzt (`_MWST_SUFFIX_MUSTER`,
#     z. B. "10 % Mwst"/"20%") und danach EXAKT (nicht als Teilstring)
#     gegen die bekannte Bezeichnungs-Whitelist verglichen
#     (`_BEZEICHNUNG_WHITELIST`) - keine Suche nach einzelnen Wörtern
#     oder Namen irgendwo im Feld.
_MWST_SUFFIX_MUSTER = re.compile(r"(?i)\s*\d{1,3}(?:[.,]\d+)?\s*%\s*(?:Mwst|USt)?\.?\s*$")
#: Bekannte, EXAKTE Bezeichnungsfeld-Werte (nach Entfernen des Mwst-/
#: Prozent-Zusatzes) -> Kurzbezeichnung für den Kundentext. Schlüssel
#: liegen normalisiert vor (klein geschrieben, Mehrfach-Leerzeichen auf
#: eines reduziert) - der Vergleich selbst ist IMMER ein exakter
#: Dict-Lookup, NIE ein Teiltreffer/eine Wortsuche.
_BEZEICHNUNG_WHITELIST: dict[str, str] = {
    "hauptmietzins": "Hauptmiete", "hauptmiete": "Hauptmiete", "hmz": "Hauptmiete",
    "betriebskosten": "Betriebskosten", "bk": "Betriebskosten",
    "bk-einnahmen": "Betriebskosten", "bk einnahmen": "Betriebskosten",
    "bk garage": "BK Garage", "bk-garage": "BK Garage",
    "betriebskosten garage": "BK Garage", "betriebskosten-garage": "BK Garage",
    "heizkosten": "Heizkosten", "hk": "Heizkosten",
    "hk-einnahmen": "Heizkosten", "hk einnahmen": "Heizkosten",
    "wasser": "Wasser", "wasser-einnahmen": "Wasser", "wasser einnahmen": "Wasser",
    "küche": "Küche", "kueche": "Küche",
    "küchen-einnahmen": "Küche", "kuechen-einnahmen": "Küche",
    "küchen einnahmen": "Küche", "kuechen einnahmen": "Küche",
    "stellplatz": "Stellplatz", "abstellplatz": "Stellplatz", "parkplatz": "Stellplatz",
}
_KORREKTUR_PRAEFIX_MUSTER = re.compile(r"^Korrektur zu #\d+:\s*")
#: Feld 0 des HV-VS-Schemas: "HV VS <Monat 1-12>/<Jahr>" - vollständig,
#: kein Teiltreffer.
_HV_VS_FELD0_MUSTER = re.compile(r"(?i)^HV\s*VS\s+(?:0?[1-9]|1[0-2])/\d{4}$")
#: Feld 2 des HV-VS-Schemas: reines Referenzfeld "J." plus Ziffern.
_HV_VS_FELD2_MUSTER = re.compile(r"(?i)^J\.\d+$")
#: Optionales Feld 3 des HV-VS-Schemas: vollständig "Abrechnung
#: <vierstelliges Jahr>".
_HV_VS_FELD3_MUSTER = re.compile(r"(?i)^Abrechnung\s*(\d{4})$")
#: Einzelfeldformat (B) - KEIN Semikolon im Text, vollständiger Treffer
#: auf eine der bekannten Literalformen; die Komponenten-Vorsilbe ist
#: eine feste Alternative aus genau den sechs bekannten Grundbegriffen,
#: KEIN beliebiger `.*`-Platzhalter.
_EINZELFELD_ABRECHNUNG_MUSTER = re.compile(
    r"(?i)^(?P<komponente>Hauptmiete|Betriebskosten|Heizkosten|Wasser|K(?:ü|ue)che|Stellplatz)?"
    r"Abrechnung\s*(?P<jahr>\d{4})$"
)


def _komponente_aus_bezeichnungsfeld(feld: str) -> str | None:
    """Exakter (NICHT durchsuchender) Whitelist-Vergleich des
    Fachbezeichnungsfelds - entfernt zuerst einen bekannten Mwst-/
    Prozent-Zusatz, normalisiert dann Groß-/Kleinschreibung und
    Mehrfach-Leerzeichen, und vergleicht das Ergebnis EXAKT gegen
    `_BEZEICHNUNG_WHITELIST`. Liefert `None` bei jedem Nicht-Treffer -
    es wird NIE nach einem einzelnen bekannten Wort irgendwo im Feld
    gesucht (z. B. ein zufällig enthaltener Familienname)."""

    ohne_suffix = _MWST_SUFFIX_MUSTER.sub("", feld).strip()
    schluessel = " ".join(ohne_suffix.split()).lower()
    return _BEZEICHNUNG_WHITELIST.get(schluessel)


def _hauptforderung_komponente_text(p: OPPositionTable) -> str | None:
    """Liefert die fachliche Kurzbezeichnung einer SOLL-Hauptforderung
    (z. B. "Heizkosten", "Hauptmiete"), WENN `beleg_referenz` eines der
    GENAU ZWEI bekannten Schemata strukturell erkennen lässt - sonst
    `None` (der Aufrufer fällt dann unverändert auf "Mietvorschreibung
    ..." zurück, NIE eine erfundene Komponente ohne Grundlage).

    (A) Der bekannte HV-VS-Altimport: nach ";" in GENAU 3 oder 4
        getrimmte Felder zerlegt, jedes Pflichtfeld muss sein eigenes
        strenges Muster VOLLSTÄNDIG erfüllen (siehe Moduldoc oben) -
        andernfalls `None` für die GESAMTE Zeichenkette. NUR Feld 1
        liefert die Komponente, NUR ein vorhandenes Feld 3 liefert das
        Abrechnungsjahr.
    (B) Enthält der Text KEIN Semikolon: nur ein vollständiger
        Literal-Treffer auf "<Komponente>abrechnung <Jahr>" bzw.
        "Abrechnung <Jahr>" (`_EINZELFELD_ABRECHNUNG_MUSTER`).

    Ein bekannter, bereits vorhandener "Korrektur zu #<id>: ..."-Präfix
    (`op/service.py::storniere_und_korrigiere`) wird davor sicher
    entfernt - er selbst wird NIE ausgegeben. Jede andere, nicht als
    eines dieser beiden Schemata erkannte Zeichenkette (z. B. eine
    völlig unabhängige Referenz) bleibt BEWUSST neutral - keine freie
    Stichwortsuche/kein beliebiger Vorsilbentext über Freitext."""

    text = p.beleg_referenz or ""
    while True:
        ohne_praefix = _KORREKTUR_PRAEFIX_MUSTER.sub("", text)
        if ohne_praefix == text:
            break
        text = ohne_praefix
    text = text.strip()
    if not text:
        return None

    if ";" in text:
        felder = [f.strip() for f in text.split(";")]
        if len(felder) not in (3, 4):
            return None
        if not _HV_VS_FELD0_MUSTER.fullmatch(felder[0]):
            return None
        if not _HV_VS_FELD2_MUSTER.fullmatch(felder[2]):
            return None
        komponente = _komponente_aus_bezeichnungsfeld(felder[1])
        if len(felder) == 4:
            feld3_treffer = _HV_VS_FELD3_MUSTER.fullmatch(felder[3])
            if feld3_treffer is None:
                return None
            jahr = feld3_treffer.group(1)
            return f"{komponente}abrechnung {jahr}" if komponente else f"Abrechnung {jahr}"
        # Genau 3 Felder, kein Abrechnungsjahr - ohne erkannte Komponente
        # ist die Zeichenkette als Ganzes unbekannt.
        if komponente is None:
            return None
        return f"{komponente} {_monat_jahr_text(p.leistungsperiode)}" if p.leistungsperiode else komponente

    einzelfeld_treffer = _EINZELFELD_ABRECHNUNG_MUSTER.fullmatch(text)
    if einzelfeld_treffer is None:
        return None
    jahr = einzelfeld_treffer.group("jahr")
    komponente_roh = einzelfeld_treffer.group("komponente")
    if not komponente_roh:
        return f"Abrechnung {jahr}"
    komponente = _komponente_aus_bezeichnungsfeld(komponente_roh)
    return f"{komponente}abrechnung {jahr}"


def _eur_basis_text(cent: int) -> str:
    return f"{cent / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _satz_text(satz_prozent) -> str:
    """Menschlich lesbare Prozent-Darstellung (Nachtrag Markus
    23.10.2026, Gegenlesen der tatsächlichen Mock-Mail zu Commit
    3c6c175): `satz_prozent` ist ein `Decimal` mit fester DB-
    Nachkommastellen-Skala (z. B. `Decimal('4.000')`) - unverändert
    interpoliert ergab das fälschlich "4.000%", was als "viertausend
    Prozent" missverstanden werden könnte. Reine Darstellungskorrektur
    (feste Nachkommanotation statt Exponentialschreibweise, unnötige
    Nachkommanullen entfernt, Dezimalkomma statt Punkt) - der
    ZAHLENWERT selbst bleibt unverändert, es wird NICHTS neu
    berechnet."""

    text = f"{Decimal(satz_prozent):f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text.replace(".", ",")


def _zinsklausel_aus_segmenten(segmente, delta_cent: int, basis_label: str) -> str | None:
    """Gemeinsamer Kern für die kurze Zinsklausel - genutzt SOWOHL für
    eine bereits GEBUCHTE Ledgerzeile (`_verzugszinsen_basis_klauseln`,
    Segmente als rohe Dicts aus `zins_segmente_json`) ALS AUCH für einen
    noch NICHT gebuchten Vorschau-Posten (`erweitere_um_vorschau`,
    Segmente als `kosten.py::ZinsSegment`-Instanzen) - `segment.get(...)`
    wird dafür für BEIDE Formen unterstützt (ein Dict hat `.get`
    nativ; bei einer `ZinsSegment`-Instanz wird stattdessen per
    `getattr` zugegriffen). `von`/`bis` dürfen entweder ISO-Strings
    (Ledger-JSON) oder bereits echte `date`-Objekte (`ZinsSegment`)
    sein. KEINE neue Berechnung - reine Aggregation/Subtraktion
    bereits vorhandener Werte (Summe der Segment-Beträge minus dem
    bereits bekannten `delta_cent`)."""

    def _feld(segment, name):
        return segment.get(name) if isinstance(segment, dict) else getattr(segment, name, None)

    basis_teile: list[str] = []
    kumulativ_cent = 0
    for segment in segmente:
        zinsen_cent = _feld(segment, "zinsen_cent")
        rest_cent = _feld(segment, "rest_cent")
        satz = _feld(segment, "satz_prozent")
        if not (isinstance(zinsen_cent, int) and zinsen_cent > 0 and isinstance(rest_cent, int) and satz is not None):
            continue
        von_roh, bis_roh = _feld(segment, "von"), _feld(segment, "bis")
        try:
            von = von_roh if isinstance(von_roh, date) else date.fromisoformat(von_roh)
            bis = bis_roh if isinstance(bis_roh, date) else date.fromisoformat(bis_roh)
        except (TypeError, ValueError):
            continue
        kumulativ_cent += zinsen_cent
        bis_einschliesslich = zins_bis_einschliesslich(bis)
        basis_teile.append(
            f"{_satz_text(satz)}% aus {_eur_basis_text(rest_cent)} EUR{basis_label}, "
            f"{von.strftime('%d.%m.%Y')}–{bis_einschliesslich.strftime('%d.%m.%Y')}"
        )
    if not basis_teile:
        return None
    basis_text = "; ".join(basis_teile)
    bereits_gebucht_cent = kumulativ_cent - delta_cent
    if bereits_gebucht_cent > 0:
        return (
            f"Zinsnachtrag: insgesamt {_eur_basis_text(kumulativ_cent)} EUR, davon "
            f"{_eur_basis_text(bereits_gebucht_cent)} EUR bereits gebucht; neu "
            f"{_eur_basis_text(delta_cent)} EUR ({basis_text})"
        )
    return basis_text


def _verzugszinsen_basis_klauseln(
    ledger: "MahnkostenLedgerEintrag", positionen_by_id: dict[int, OPPositionTable],
) -> list[str]:
    """Nachtrag Markus 23.10.2026 (ergänzender fachlicher Abnahmehinweis):
    die kurze Zinszeile muss neben Zeitraum/Satz auch die tatsächliche
    ZINSBASIS erkennen lassen (z. B. "4% aus 1.000,00 EUR Miete
    September 2026, 04.–07.09.2026") - AUSSCHLIESSLICH aus bereits
    vorhandenen, strukturierten Daten (`ledger.zins_zuordnung`, selbst
    wiederum reine Umformung von `zins_segmente_json`/`zinsen_delta_
    je_op_json`, siehe `_parse_zins_zuordnung`): KEINE neue Berechnung,
    KEINE erfundene Zuordnung, KEINE OP-Id im Text (nur zum internen
    Nachschlagen des Periodenlabels verwendet).

    EIN zugrunde liegender Forderungs-`op_position_id` gilt nur, wenn er
    an DIESER Buchung tatsächlich ein Delta (`delta_cent`) beigetragen
    hat - sonst wäre er nur historischer Kontext einer früheren Buchung,
    keine Grundlage für DIESE Zeile. Pro solchem Zuordnungseintrag wird
    JEDES seiner Segmente mit tatsächlich positivem `zinsen_cent`
    GETRENNT ausgewiesen ("bei mehreren unterschiedlichen Grundlagen
    kurz getrennt je Basis/Zeitraum ausweisen") - keine Zusammenfassung/
    Mittelwertbildung über mehrere Sätze/Zeiträume/Basisbeträge. Ein
    Segment mit fehlendem/unklarem Satz, Zeitraum oder Restbetrag wird
    STILL übersprungen (nie ein unbekannter Wert erfunden) - die Summe
    im Betrag der Zeile bleibt davon unberührt, nur die Textbeschreibung
    kann dadurch unvollständig bleiben (der vollständige Nachweis bleibt
    intern über `MahnkostenLedgerEintrag` abrufbar).

    Das Periodenlabel ("Miete {Monat Jahr}") wird NUR gezeigt, wenn die
    referenzierte Position tatsächlich eine `leistungsperiode` trägt
    (typisch für eine Mietvorschreibung) - sonst bleibt es bei einer
    neutralen Datumsangabe ("Forderung vom ..."), NIE eine erfundene
    Bezeichnung "Miete" für eine andere Forderungsart (z. B. eine
    Mahngebühr, die selbst Zinsen trägt).

    **Korrektur nach unabhängiger Rückprüfung (Codex, 23.10.2026,
    konkreter Zinsanzeigefehler):** `zuordnung.segmente` ist das
    VOLLSTÄNDIGE, KUMULATIVE Segment-Bild (siehe `ZinsZuordnungZeile`-
    Docstring) - bei einer ZWEITEN (oder späteren) Zinsbuchung für
    dieselbe Forderung deckt ein Segment bereits den GESAMTEN Zeitraum
    seit Beginn ab (z. B. "04.–08.09., 0,55 EUR kumulativ"), während
    `delta_cent` nur den an DIESER Buchung NEU hinzugekommenen Anteil
    nennt (hier 0,11 EUR, weil 0,44 EUR bereits bei einer früheren
    Buchung gebucht wurden). Die vorherige Fassung beschrieb die Zeile
    schlicht als "4% aus 1.000 EUR, 04.–08." mit dem Betrag 0,11 EUR -
    das behauptet fälschlich, der GESAMTE kumulative Zeitraum/Betrag
    gehöre zu DIESER (viel kleineren) Buchung. Jetzt: ist die Summe der
    tatsächlich einbezogenen Segmente (`kumulativ_cent`) GRÖSSER als
    `delta_cent`, wird die Zeile als "Zinsnachtrag: insgesamt X EUR,
    davon Y EUR bereits gebucht; neu Z EUR (<Basis/Satz/Zeitraum>)"
    ausgewiesen - `Y = kumulativ_cent - delta_cent`, `Z = delta_cent`,
    beides reine Subtraktion bereits vorhandener Werte, KEINE neue
    Engine. Bei der ERSTEN Buchung ohne Vorbetrag (`kumulativ_cent ==
    delta_cent`) bleibt die bisherige kurze Basiszeile unverändert."""

    klauseln: list[str] = []
    for zuordnung in ledger.zins_zuordnung:
        if not zuordnung.delta_cent:
            continue
        basis_position = positionen_by_id.get(zuordnung.op_position_id)
        if basis_position is not None and basis_position.leistungsperiode:
            basis_label = f" Miete {_monat_jahr_text(basis_position.leistungsperiode)}"
        elif basis_position is not None:
            basis_label = f" Forderung vom {basis_position.belegdatum.strftime('%d.%m.%Y')}"
        else:
            basis_label = ""
        klausel = _zinsklausel_aus_segmenten(zuordnung.segmente, zuordnung.delta_cent, basis_label)
        if klausel:
            klauseln.append(klausel)
    return klauseln


def _einfache_buchung_text(jz: "JournalZeile", p: OPPositionTable, positionen_by_id: dict[int, OPPositionTable]) -> str:
    """Kurze, für einen Laien verständliche Beschreibung EINER
    Journalzeile (Auftrag HV-20261023-MAHNUNG-EINFACH) - AUSSCHLIESSLICH
    aus bereits vorhandenen, strukturierten Feldern (`leistungsperiode`,
    `belegdatum`, `faelligkeit`, die direkten Skalarfelder des Zins-
    Ledgers `zins_von`/`zins_bis`/`zinssatz_prozent`, `bezieht_sich_auf_id`)
    - NIE eine interne OP-Id, ein Import-/Beleg-Schlüssel, ein Hash oder
    eine rohe `versandnachweis_referenz` (die bleiben ausschließlich im
    internen Backoffice-Kontoblatt sichtbar, siehe `KontoblattZeile.
    ledger`/`_forderungsbezug`).

    **Korrektur nach unabhängiger Rückprüfung (Codex, 23.10.2026,
    Gegenlesen von Commit d52948f):**
    1. `zins_bis`/`buchung.zins_bis` sind EXKLUSIV (siehe `kosten.py::
       zins_bis_einschliesslich`-Docstring) - vorher wurde das exklusive
       Enddatum unverändert angezeigt (ein am 08. endender Zeitraum
       zeigte fälschlich "...–08.", tatsächlich verzinst ist nur bis
       07.). Jetzt wie überall sonst im Kundentext über
       `zins_bis_einschliesslich` in den tatsächlich letzten verzinsten
       Tag umgerechnet - NUR die Anzeige, NIE der Betrag selbst.
    2. Eine RUECKLASTSCHRIFT-Zeile nannte bisher ihr EIGENES Belegdatum
       als "Rückbuchung dieser Zahlung ({datum})" - das ist das Datum
       DER RÜCKBUCHUNG SELBST, nicht das der ursprünglich zurückgebuchten
       Zahlung, und damit ein falscher Bezug. Jetzt wird über die
       bereits bestehende `bezieht_sich_auf_id`-Bindung (siehe `bank/
       service.py`, RUECKLASTSCHRIFT -> ursprüngliche ZAHLUNG) das
       tatsächliche Zahlungsdatum aufgelöst und benannt ("Rückbuchung
       der Zahlung vom {datum der Zahlung}"); die DATUMS-SPALTE der
       Tabellenzeile bleibt UNVERÄNDERT das Rückbuchungsdatum selbst
       (`jz.datum`/`p.belegdatum`, siehe `erstelle_einfache_tabelle`) -
       nur der Beschreibungstext bekommt den korrekten Bezug. Ist die
       Bindung nicht auflösbar (fehlt/Ziel nicht gefunden), bleibt der
       Text bewusst NEUTRAL ("Rückbuchung einer Zahlung") statt ein
       falsches Datum zu erfinden.
    3. Eine gebuchte Rücklastschriftkosten-Zeile nannte bisher IHR
       EIGENES Buchungsdatum (wann das Bankentgelt selbst erfasst wurde)
       fälschlich ALS Rückbuchungsdatum ("Bankspesen Rückbuchung
       ({datum})") - dieses Datum ist aber laut `bank/
       ruecklastschriftkosten_service.py` NICHT an die Rücklastschrift
       gebunden (`heute` beim Erfassen, siehe dort: "KEINE
       bezieht_sich_auf_id auf die Rücklastschrift"). Der Text behauptet
       das Rückbuchungsdatum deshalb jetzt NICHT mehr - die Datums-Spalte
       der Zeile zeigt weiterhin korrekt das (eigene) Buchungsdatum
       dieser Kostenzeile."""

    datum_text = p.belegdatum.strftime("%d.%m.%Y")
    if jz.kategorie == KATEGORIE_VERZUGSZINSEN:
        ledger = jz.zeile.ledger
        klauseln = _verzugszinsen_basis_klauseln(ledger, positionen_by_id) if ledger is not None else []
        if klauseln:
            return "Verzugszinsen " + "; ".join(klauseln)
        buchung = ledger.buchung if ledger is not None else None
        if buchung is not None and buchung.zins_von and buchung.zins_bis and buchung.zinssatz_prozent is not None:
            bis_einschliesslich = zins_bis_einschliesslich(buchung.zins_bis)
            return (
                f"Verzugszinsen {buchung.zins_von.strftime('%d.%m.%Y')}–{bis_einschliesslich.strftime('%d.%m.%Y')}, "
                f"{_satz_text(buchung.zinssatz_prozent)}%"
            )
        return f"Verzugszinsen ({datum_text})"
    if jz.kategorie in (KATEGORIE_MAHNSPESEN, KATEGORIE_MAHNPAUSCHALE):
        return f"Mahngebühr ({datum_text})"
    if jz.kategorie == KATEGORIE_RUECKLASTSCHRIFTKOSTEN:
        return "Bankspesen Rücklastschrift"
    # KATEGORIE_HAUPTFORDERUNG - je tatsächlichem OP-Typ.
    if p.typ == OPTyp.EROEFFNUNG.value:
        return "Ausgangssaldo"
    if p.typ == OPTyp.ZAHLUNG.value:
        return "Zahlung"
    if p.typ == OPTyp.GUTSCHRIFT.value:
        return "Gutschrift"
    if p.typ == OPTyp.RUECKLASTSCHRIFT.value:
        zahlung = positionen_by_id.get(p.bezieht_sich_auf_id) if p.bezieht_sich_auf_id is not None else None
        if zahlung is not None:
            return f"Rückbuchung der Zahlung vom {zahlung.belegdatum.strftime('%d.%m.%Y')}"
        return "Rückbuchung einer Zahlung"
    if p.typ == OPTyp.KORREKTUR.value:
        return "Korrektur"
    # SOLL - die tatsächliche Mietvorschreibung. `_hauptforderung_
    # komponente_text` liefert, WENN erkennbar, die fachliche Komponente
    # (Hauptmiete/Betriebskosten/BK Garage/Heizkosten/Wasser/Küche/
    # Stellplatz bzw. ein explizites Abrechnungsjahr) statt der
    # pauschalen "Mietvorschreibung" - `None` fällt unverändert auf das
    # bisherige Verhalten zurück.
    komponente = _hauptforderung_komponente_text(p)
    basis = komponente if komponente is not None else (
        f"Mietvorschreibung {_monat_jahr_text(p.leistungsperiode)}" if p.leistungsperiode else "Mietvorschreibung"
    )
    if p.faelligkeit_bekannt and p.faelligkeit is not None:
        basis += f", fällig {p.faelligkeit.strftime('%d.%m.%Y')}"
    return basis


@dataclass(frozen=True)
class EinfacheZeile:
    """EINE Zeile der kurzen, menschlich verständlichen Empfänger-Tabelle
    (Auftrag HV-20261023-MAHNUNG-EINFACH, verbindlicher Darstellungs-
    auftrag) - abgeleitet AUSSCHLIESSLICH aus `erstelle_journal` (keine
    zweite Berechnung/Engine), aber auf das für einen Laien verständliche
    Minimum reduziert: keine OP-IDs, keine Beleg-/Import-/Hash-Ketten,
    keine Ledger-/Gate-Interna. `betrag_cent` ist VORZEICHENBEHAFTET (+
    erhöht den offenen Betrag, - vermindert ihn - identisch zur
    Erklärung im Anschreiben); `offen_cent` ist der laufende Saldo NACH
    dieser Zeile, IDENTISCH zu `JournalZeile.laufender_saldo_cent` (keine
    zweite Saldoberechnung). `ist_storno` markiert eine Historie-Zeile
    (storniert/ersetzt) - bleibt sichtbar ("keine verlorene Historie"),
    ändert `offen_cent` aber NICHT gegenüber der Vorzeile. Fälligkeit
    einer eingemahnten Hauptposition steht - wo vorhanden - bereits
    direkt in `buchung` (z. B. "Mietvorschreibung Jänner 2026, fällig
    05.01.2026"), keine eigene Spalte nötig."""

    datum: date
    buchung: str
    betrag_cent: int
    offen_cent: int
    ist_storno: bool = False
    #: Nachtrag Markus 23.10.2026 (Abnahme-Hinweis): eine über
    #: `erweitere_um_vorschau` angehängte Zeile für einen mit DIESEM
    #: Versand tatsächlich zulässigen, aber ZUM RENDERZEITPUNKT noch
    #: NICHT gebuchten Posten (Zinsdelta/Mahngebühr) - Buchung erfolgt
    #: weiterhin ausschließlich NACH dem tatsächlichen Versand. Erlaubt
    #: einem Renderer eine optische Kennzeichnung ("Vorschauwerte als
    #: Vorschau kennzeichnen"), OHNE dass der kurze Empfängertext dafür
    #: interne Fachbegriffe nennen müsste.
    ist_vorschau: bool = False


def erstelle_einfache_tabelle(kontoblatt: Kontoblatt) -> list[EinfacheZeile]:
    """Die kurze, chronologische Empfänger-Tabelle (Datum | Buchung |
    Betrag | Offen, ältestes Ereignis oben) - Grundlage für den
    tatsächlichen Mahnbrief-/E-Mail-Anhang (`mahnwesen/
    kontoauszug_anhang.py::journal_als_text`, `op/kontoauszug_pdf.py::
    erzeuge_kontoauszug_pdf(..., empfaenger_ansicht=True)`). Reine Um-
    formung von `erstelle_journal` - KEINE zweite Berechnung/Engine, KEIN
    abweichender Saldo. `None`/leere Liste nur, wenn es wirklich NOCH
    KEINE Buchung gibt.

    **Ausgangssaldo (wörtliche Vorgabe):** "Ausgangssaldo stets als erste
    Zeile korrekt aus dem Datenmodell, ggf. 0,00" - existiert bereits
    eine echte `EROEFFNUNG`-Buchung als erste Journalzeile, IST sie
    bereits dieser Ausgangssaldo (keine doppelte Zeile). Fehlt eine
    solche Buchung (z. B. ein Konto, das direkt mit der ersten
    Vorschreibung beginnt), wird eine synthetische "Ausgangssaldo"-Zeile
    MIT 0,00 EUR vorangestellt - NIE eine verlorene/weggelassene
    Ausgangslage."""

    journal = erstelle_journal(kontoblatt)
    if not journal:
        return []
    # Nachtrag Markus 23.10.2026 (Gegenlesen von d52948f): Lookup ALLER
    # im Kontoblatt vorkommenden Positionen über ihre Id - ausschließlich
    # dafür, dass eine RUECKLASTSCHRIFT-Zeile die über `bezieht_sich_
    # auf_id` gebundene ursprüngliche ZAHLUNG korrekt benennen kann
    # (siehe `_einfache_buchung_text`), KEINE zweite Berechnung/Query.
    positionen_by_id = {
        z.position.id: z.position for z in (
            list(kontoblatt.hauptforderung_zeilen) + list(kontoblatt.kosten_zeilen) + list(kontoblatt.zahlungen_zeilen)
            + list(kontoblatt.hauptforderung_historie) + list(kontoblatt.kosten_historie)
        )
    }
    zeilen: list[EinfacheZeile] = []
    erste = journal[0]
    if not (erste.beeinflusst_saldo and erste.zeile.position.typ == OPTyp.EROEFFNUNG.value):
        zeilen.append(EinfacheZeile(datum=erste.datum, buchung="Ausgangssaldo", betrag_cent=0, offen_cent=0))
    for jz in journal:
        p = jz.zeile.position
        buchung = _einfache_buchung_text(jz, p, positionen_by_id)
        if not jz.beeinflusst_saldo:
            buchung += " (storniert)"
        zeilen.append(EinfacheZeile(
            datum=jz.datum, buchung=buchung, betrag_cent=jz.soll_cent - jz.haben_cent,
            offen_cent=jz.laufender_saldo_cent, ist_storno=not jz.beeinflusst_saldo,
        ))
    return zeilen


def erweitere_um_vorschau(
    zeilen: list[EinfacheZeile], *, heute: date,
    neue_zinsen_delta_cent: int = 0, zins_von: date | None = None, zins_bis: date | None = None,
    zinssatz_prozent=None, gebuehr_cent: int | None = None, mahnpauschale_cent: int | None = None,
    zins_segmente=(), neue_zinsen_delta_je_op_position: dict | None = None,
) -> list[EinfacheZeile]:
    """Nachtrag Markus 23.10.2026 (Abnahme-Hinweis zu edb0417-Nachfolgern):
    "Brief-Tabelle und beigefügtes kanonisches Kontoblatt dürfen keine
    verschiedenen 'Gesamt offen'-Beträge zeigen" - der Briefkörper zeigt
    bereits HEUTE die mit DIESEM Versand tatsächlich zulässigen neuen
    Zinsen/Gebühren (`MahnkostenVorschau`), BEVOR sie tatsächlich gebucht
    werden (Buchung erfolgt weiterhin ausschließlich NACH dem Versand,
    `MahnwesenService.versende_mahnlauf`). Ohne diese Funktion zeigte das
    beigefügte Kontoblatt (gebaut aus dem tatsächlich bereits gebuchten
    Stand) deshalb einen NIEDRIGEREN Gesamtbetrag als der Briefkörper -
    zwei widersprüchliche Summen im selben Schreiben.

    Hängt deshalb GENAU dieselben, dem Briefkörper bereits bekannten
    Posten (dasselbe `MahnkostenVorschau`-Objekt, keine zweite
    Berechnung) als zusätzliche Zeilen an die bereits erzeugte
    `erstelle_einfache_tabelle`-Liste an, OHNE irgendetwas in die DB zu
    schreiben ("ohne vorherige DB-Buchung beim GET/Export") - rein
    darstellungsseitig, EIN eingefrorener Snapshot. Ein Posten mit
    Betrag 0/`None` wird NICHT angehängt (kein erfundener 0,00-Posten).
    Der laufende `offen_cent` setzt die letzte Zeile der übergebenen
    Liste fort (0, wenn die Liste leer ist) - danach stimmen Brief-
    Gesamtbetrag und das `offen_cent` der letzten angehängten Zeile
    GARANTIERT überein, weil beide aus derselben Vorschau stammen.

    Nur für einen Versand-Snapshot gedacht (Haupt-Mahnbrief/-Vorschau,
    `_sende_brief`) - der reine, mahnlaufunabhängige Kontoblatt-Download
    (`backoffice/routes/konten.py::kontoauszug_pdf`) ruft diese Funktion
    NICHT auf und bleibt beim tatsächlich gebuchten Stand.

    **Ergänzung Markus 23.10.2026 (Abnahmehinweis zu Commit 3c6c175):**
    `zins_segmente`/`neue_zinsen_delta_je_op_position` sind dieselben
    bereits vorhandenen strukturierten `MahnkostenVorschau`-Felder, die
    `_verzugszinsen_basis_klauseln` für eine bereits GEBUCHTE Zeile
    nutzt - hier werden sie GENAUSO ausgewertet (derselbe gemeinsame
    `_zinsklausel_aus_segmenten`-Kern, keine zweite Rechenlogik), damit
    eine noch nicht gebuchte Vorschau denselben Fall (kumulativ/bereits
    gebucht/neu, Basis/Satz/Zeitraum) wie die spätere gebuchte Zeile
    erklärt. Fehlen diese strukturierten Daten (älterer Aufrufer), greift
    unverändert der bisherige Skalar-Fallback (`zins_von`/`zins_bis`/
    `zinssatz_prozent`)."""

    laufend = zeilen[-1].offen_cent if zeilen else 0
    ergebnis = list(zeilen)
    if neue_zinsen_delta_cent:
        laufend += neue_zinsen_delta_cent
        klauseln: list[str] = []
        if neue_zinsen_delta_je_op_position:
            segmente_by_op: dict[int, list] = {}
            for segment in zins_segmente:
                segmente_by_op.setdefault(segment.op_position_id, []).append(segment)
            for op_position_id, delta in neue_zinsen_delta_je_op_position.items():
                if not delta:
                    continue
                klausel = _zinsklausel_aus_segmenten(segmente_by_op.get(op_position_id, ()), delta, "")
                if klausel:
                    klauseln.append(klausel)
        if klauseln:
            text = "Verzugszinsen " + "; ".join(klauseln) + " (mit diesem Schreiben)"
        elif zins_von is not None and zins_bis is not None and zinssatz_prozent is not None:
            # `zins_bis` ist EXKLUSIV (siehe Docstring/`_einfache_
            # buchung_text`-Korrektur oben) - angezeigt wird der
            # tatsächlich letzte verzinste Tag, NIE das exklusive Enddatum.
            bis_einschliesslich = zins_bis_einschliesslich(zins_bis)
            text = f"Verzugszinsen {zins_von.strftime('%d.%m.%Y')}–{bis_einschliesslich.strftime('%d.%m.%Y')}, {_satz_text(zinssatz_prozent)}% (mit diesem Schreiben)"
        else:
            text = f"Verzugszinsen (mit diesem Schreiben, {heute.strftime('%d.%m.%Y')})"
        ergebnis.append(EinfacheZeile(datum=heute, buchung=text, betrag_cent=neue_zinsen_delta_cent, offen_cent=laufend, ist_vorschau=True))
    if gebuehr_cent:
        laufend += gebuehr_cent
        ergebnis.append(EinfacheZeile(
            datum=heute, buchung=f"Mahngebühr (mit diesem Schreiben, {heute.strftime('%d.%m.%Y')})",
            betrag_cent=gebuehr_cent, offen_cent=laufend, ist_vorschau=True,
        ))
    if mahnpauschale_cent:
        laufend += mahnpauschale_cent
        ergebnis.append(EinfacheZeile(
            datum=heute, buchung=f"Mahngebühr (mit diesem Schreiben, {heute.strftime('%d.%m.%Y')})",
            betrag_cent=mahnpauschale_cent, offen_cent=laufend, ist_vorschau=True,
        ))
    return ergebnis


def lade_kontoblatt(
    *, konto_id: str, vertrag_id: str, op_service, mahnkosten_repo, ruecklastschriftkosten_repo, mahnlauf_repo,
    manueller_mahnversand_repo=None,
) -> Kontoblatt:
    """I/O-Teil: lädt exakt die bereits etablierten Repositories/Services
    (keine neue Query-Logik abseits der additiven `liste_fuer_vertrag`/
    `liste_buchungen_fuer_vertrag`-Lesemethoden) und ruft dann
    `erstelle_kontoblatt` auf. Gemeinsam verwendet von `backoffice/
    routes/konten.py::kontoauszug`/`kontoauszug_pdf`.

    `manueller_mahnversand_repo` ist bewusst OPTIONAL (Default `None`,
    Fix6) - nicht jeder bestehende Aufrufer hat dieses Repository bereits
    verdrahtet; ohne Repository bleibt `ausstehende_manuelle_mahnversand`
    einfach leer, statt mit einem Pflicht-Parameter alle Call-Sites auf
    einmal zu brechen."""

    saldo = op_service.berechne_saldo(konto_id)
    positionen_alle = op_service.list_alle_positionen(konto_id)
    offene_forderungen_rest_je_id = {f.op_position_id: f.rest_cent for f in op_service.offene_forderungen(konto_id)}
    mahnkosten_buchungen = mahnkosten_repo.liste_buchungen_fuer_vertrag(vertrag_id)
    ruecklastschriftkosten_zeilen = ruecklastschriftkosten_repo.liste_fuer_vertrag(vertrag_id)
    mahnlaeufe = mahnlauf_repo.list_fuer_vertrag(vertrag_id)
    manuelle_mahnversand_zeilen = (
        manueller_mahnversand_repo.list_fuer_vertrag(vertrag_id) if manueller_mahnversand_repo is not None else []
    )
    # Zweite Rückprüfung Codex 22.10.2026, echter Anzeige-Mangel: der
    # unverbindliche Mahnpauschale-Prüfwert für jeden noch nicht
    # freigegebenen Manualimport-Beleg kommt aus GENAU derselben, bereits
    # bestehenden Zinsprofil-Konfiguration wie die Mahnpauschale-Freigabe-
    # Maske (`mahnwesen.py::mahnpauschale_freigabe_formular`) - am echten
    # Tagesdatum wirksames Profil, KEINE zweite Berechnung/kein zweiter
    # Ledger. Nur abgefragt, wenn tatsächlich etwas ausstehend ist (keine
    # unnötige Zusatzabfrage bei jedem Kontoblatt-Laden).
    manuelle_mahnversand_vorschlag_cent = None
    if any(not z.pauschale_abgegolten for z in manuelle_mahnversand_zeilen):
        zinsprofil_historie = mahnkosten_repo.historie_geprueft(vertrag_id)
        aktuelles_profil, _unberechenbar = _profil_wirksam_am(zinsprofil_historie, date.today())
        manuelle_mahnversand_vorschlag_cent = (
            aktuelles_profil.mahnpauschale_kostenbasis_cent if aktuelles_profil is not None else None
        )

    return erstelle_kontoblatt(
        konto_id=konto_id, saldo_cent=saldo.saldo_cent, faelliger_unstrittiger_rest_cent=saldo.faelliger_unstrittiger_rest_cent,
        positionen_aktiv=saldo.positionen, positionen_alle=positionen_alle,
        offene_forderungen_rest_je_id=offene_forderungen_rest_je_id, mahnkosten_buchungen=mahnkosten_buchungen,
        ruecklastschriftkosten_zeilen=ruecklastschriftkosten_zeilen, mahnlaeufe=mahnlaeufe,
        manuelle_mahnversand_zeilen=manuelle_mahnversand_zeilen,
        manuelle_mahnversand_vorschlag_cent=manuelle_mahnversand_vorschlag_cent,
    )
