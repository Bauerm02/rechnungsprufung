"""Deterministische, nachvollziehbare Mahngebühren-/Verzugszinsenvorschau
je Mahnlauf (Auftrag Markus, 13.09.2026: "pro Mahnlauf Mahngebühren, und
die Zinsen dazu, soviel wie gesetzlich erlaubt ist"; ergänzt durch die
Rückprüfung vom 14.09.2026 zu Halbjahres-Segmentierung und §458 UGB).

FACHLICHE LEITPLANKEN (von Codex nach Primärquellenprüfung, siehe
`docs/hausverwaltung/RAHMENPROGRAMM.md`):

- §1000/§1333 ABGB: gesetzliche Verzugszinsen 4 % p.a., wenn keine
  geprüfte abweichende Vereinbarung vorliegt. §1333 Abs 2 ABGB erlaubt
  NUR notwendige, zweckmäßige, schuldhaft verursachte tatsächliche
  Betreibungskosten in angemessenem Verhältnis zur Hauptforderung - KEINE
  Pauschale wird hier erfunden, die Kostenbasis muss über ein GEPRÜFTES
  `ZinsprofilTable`-Profil belegt sein.
- KSchG §6 Abs 1 Z 13 / OGH 7Ob111/25m: eine vereinbarte Verbraucher-
  Verzugszinsklausel unter der Fünf-Prozentpunkte-Grenze ist NICHT
  automatisch wirksam - eine gelesene Klausel ist noch keine
  Wirksamkeitsfreigabe. Ein vereinbarter Satz wird deshalb NUR verwendet,
  wenn `ZinsprofilTable.vereinbarung_geprueft=True` (menschliche
  Prüfung), sonst gilt die gesetzliche Basis.
- §456 UGB (ab 16.03.2013): 9,2 Prozentpunkte über dem für das jeweilige
  Halbjahr geltenden Basiszinssatz - NUR bei beiderseits unternehmens-
  bezogenem Geschäft (`ist_b2b`) und einem Vertragsdatum ab 16.03.2013.
  Eine Verzinsungsperiode, die einen Halbjahreswechsel überspannt, wird
  in TEILPERIODEN je Halbjahr zerlegt (`_segmentiere_periode_ugb`) -
  JEDES Teilsegment verwendet den für SEINEN Zeitraum tatsächlich
  belegten Basiszinssatz, niemals einen fortgeschriebenen alten Wert.
  Ein Teilsegment ohne erfassten Basiszinssatz bleibt für SICH GENOMMEN
  "Basis ungeklärt" (satz_prozent=None, keine Zinsen für dieses Segment),
  blockiert aber NICHT die übrigen, belegten Segmente derselben Periode
  und NICHT die Hauptforderung.
- §458 UGB: die Mahnspesen-Pauschale (die geprüfte Kostenbasis aus
  `ZinsprofilTable`) ist laut Gesetzesmaterialien VERSCHULDENSUNABHÄNGIG,
  gilt aber wie §456 NUR für eine beiderseits unternehmensbezogene
  Unternehmerforderung mit Vertragsdatum ab 16.03.2013 (`ugb_anwendbar`).
  Sie wird NIE je Mahnlauf/Brief und NIE je einzelner
  OP-Zeile/Mietkomponente angesetzt, sondern GENAU EINMAL je zugrunde
  liegender, fälliger Entgeltforderung (`_entgeltforderung_schluessel` -
  alle OP-Zeilen derselben `leistungsperiode`, z. B. HMZ+BK+HK desselben
  Monats, bilden EINE Entgeltforderung). Eine einmal erhobene Pauschale
  für eine bestimmte Entgeltforderung wird DAUERHAFT erkannt
  (`MahnkostenGebuehrTable`, vertragsweit über alle Mahnläufe/Stufen
  hinweg) und nie ein zweites Mal für dieselbe Forderung angesetzt - eine
  GENUIN neue, andere Entgeltforderung (z. B. ein späterer Monat) kann
  hingegen ihre EIGENE, separate Pauschale auslösen.
- Keine automatische Maximalauswahl zwischen vereinbartem und
  gesetzlichem/UGB-Satz: eine geprüfte konkrete Vereinbarung hat
  IMMER Vorrang, unabhängig davon, ob sie höher oder niedriger als die
  gesetzliche/UGB-Basis ausfällt (siehe `bestimme_zinssatz`-Rangfolge).

Diese Vorschau bucht NICHTS - siehe `MahnkostenService.buche_vorschau`
in `kosten_service.py` für die einzige, an einen bestätigten
Versandnachweis gekoppelte Buchungsstelle, die IMMER exakt die hier
berechneten Werte übernimmt (nie eine zweite, potenziell abweichende
Neuberechnung)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from mietinkasso.infrastructure.db.tables import BriefAnbieterProfilTable, OPPositionTable, OenbBasiszinssatzTable, ZinsprofilTable
from mietinkasso.op.service import OffeneForderung

_STICHTAG_UGB_456 = date(2013, 3, 16)
_GESETZLICHER_ZINSSATZ_PROZENT = Decimal("4.000")
_UGB_AUFSCHLAG_PROZENTPUNKTE = Decimal("9.200")
_TAGE_IM_JAHR = Decimal(365)
# §458 UGB (https://www.ris.bka.gv.at/eli/drgbl/1897/219/P458/NOR40148646):
# gesetzlicher HÖCHSTBETRAG der Mahnspesen-Pauschale 40 EUR - unabhängige
# Rückprüfung Codex 14.09.2026, echter Bug: das Formular/Backend nahm
# jeden Wert an (u. a. 100 EUR), obwohl §458 UGB diesen Betrag DECKELT.
# Öffentlich (kein führender Unterstrich), damit `kosten_repository.py`
# beim Anlegen eines Zinsprofils dieselbe Konstante prüft, statt eine
# zweite, potenziell abweichende Kopie zu pflegen. Ein bereits belegter,
# REDUZIERTER Altwert (0 < Wert <= 4000) bleibt uneingeschränkt gültig -
# NUR Werte AUSSERHALB [0, 4000] sind unzulässig.
S458_UGB_HOECHSTBETRAG_CENT = 4000


def ugb_anwendbar(zinsprofil: ZinsprofilTable | None) -> bool:
    """Gemeinsames Anwendungstor für die §458-UGB-Mahnspesen-Pauschale:
    beiderseits unternehmensbezogenes Geschäft, geprüftes Profil,
    Vertragsdatum ab 16.03.2013. Laut Gesetzesmaterialien ist §458
    VERSCHULDENSUNABHÄNGIG - dieses Tor prüft daher bewusst NICHT die
    Verzugsverantwortung. Für den ERHÖHTEN §456-Zinssatz gilt das
    ZUSÄTZLICHE, engere Tor `_ugb_zinssatz_anwendbar` (unabhängige
    Rückprüfung Codex 14.09.2026: §456 braucht neben B2B/Datum eine
    BELEGTE Verzugsverantwortung, sonst gesetzliche 4 % ABGB)."""

    return (
        zinsprofil is not None and zinsprofil.status == "GEPRUEFT" and zinsprofil.ist_b2b
        and zinsprofil.vertragsdatum is not None and zinsprofil.vertragsdatum >= _STICHTAG_UGB_456
    )


def _ugb_zinssatz_anwendbar(zinsprofil: ZinsprofilTable | None) -> bool:
    """Enger als `ugb_anwendbar`: gilt NUR für die Wahl des ERHÖHTEN
    §456-Zinssatzes, NIE für die §458-Pauschale (siehe dortiger
    Docstring). Ein bloß unterstellter, nicht belegt geprüfter
    Zahlungsverzug (`verzugsverantwortung_geprueft=False`, der additive
    Spalten-Default) reicht nicht - die Verzinsung fällt dann auf die
    gesetzlichen 4 % ABGB zurück, statt automatisch den höheren
    UGB-Satz zu unterstellen ("ungeklärt führt nicht automatisch zum
    Höchstsatz")."""

    return ugb_anwendbar(zinsprofil) and zinsprofil.verzugsverantwortung_geprueft


@dataclass(frozen=True)
class Zinsentscheidung:
    satz_prozent: Decimal | None  # None nur bei status != "OK"; bei UGB nur ein REPRÄSENTATIVER Punktwert für `heute`
    basis: str  # "GESETZLICH_ABGB" | "VEREINBART_GEPRUEFT" | "UGB_B2B_BASISZINSSATZ"
    status: str  # "OK" | "UNBEKANNTE_BASIS"
    hinweis: str


def bestimme_zinssatz(
    *, zinsprofil: ZinsprofilTable | None, heute: date, basiszinssatz_lookup,
) -> Zinsentscheidung:
    """Punktuelle Zinssatz-Auskunft FÜR EXAKT `heute` (z. B. für eine
    einfache "welcher Satz gilt heute"-Anzeige). `basiszinssatz_lookup(datum)
    -> OenbBasiszinssatzTable | None`. Für die TATSÄCHLICHE Verzinsung
    einer über mehrere Halbjahre laufenden Periode verwendet
    `berechne_mahnkosten_vorschau` NICHT diesen punktuellen Wert, sondern
    die volle Segmentierung (`_segmentiere_periode_ugb`) - eine einzelne
    `heute`-Momentaufnahme würde einen Halbjahreswechsel innerhalb der
    Periode sonst verdecken."""

    if zinsprofil is not None and zinsprofil.status == "GEPRUEFT" and zinsprofil.vereinbarung_geprueft and zinsprofil.vereinbarter_zinssatz_prozent is not None:
        return Zinsentscheidung(
            satz_prozent=zinsprofil.vereinbarter_zinssatz_prozent, basis="VEREINBART_GEPRUEFT", status="OK",
            hinweis=f"Vereinbarter, geprüfter Zinssatz laut Profil ({zinsprofil.vereinbarung_beleg or 'ohne Belegangabe'}).",
        )

    if _ugb_zinssatz_anwendbar(zinsprofil):
        basiszins = basiszinssatz_lookup(heute)
        if basiszins is None:
            return Zinsentscheidung(
                satz_prozent=None, basis="UGB_B2B_BASISZINSSATZ", status="UNBEKANNTE_BASIS",
                hinweis=f"Kein OeNB-Basiszinssatz für {heute.isoformat()} erfasst - §456-UGB-Zinsen bleiben für diesen Zeitpunkt blockiert, bis nachgetragen.",
            )
        satz = basiszins.basiszinssatz_prozent + _UGB_AUFSCHLAG_PROZENTPUNKTE
        return Zinsentscheidung(
            satz_prozent=satz, basis="UGB_B2B_BASISZINSSATZ", status="OK",
            hinweis=f"§456 UGB: {_UGB_AUFSCHLAG_PROZENTPUNKTE} Prozentpunkte über Basiszinssatz {basiszins.basiszinssatz_prozent} % ({basiszins.id}).",
        )

    return Zinsentscheidung(
        satz_prozent=_GESETZLICHER_ZINSSATZ_PROZENT, basis="GESETZLICH_ABGB", status="OK",
        hinweis="Gesetzliche Verzugszinsen §1000 ABGB (keine geprüfte abweichende Vereinbarung/kein geprüftes B2B-Profil).",
    )


@dataclass(frozen=True)
class BalancePeriode:
    """Ein Zeitabschnitt [von, bis) mit konstantem Reststand einer
    EINZELNEN Forderung, ab deren FÄLLIGKEIT (nie davor - vor Fälligkeit
    besteht kein Verzug)."""

    von: date
    bis: date  # exklusiv
    rest_cent: int


def balance_zeitreihe_fuer_forderung(
    *, ziel_op_position_id: int, alle_positionen: list[OPPositionTable], heute: date,
) -> list[BalancePeriode] | None:
    """Reproduziert EXAKT dieselbe Zuordnungsregel wie
    `OPService.offene_forderungen` (eine explizit gebundene Zahlung/
    Gutschrift - `bezieht_sich_auf_id` - deckt IMMER zuerst ihre
    Zielforderung, nur ein tatsächlicher Überschuss fließt in die
    generische FIFO-Verteilung nach Fälligkeit/Belegdatum über die
    übrigen Forderungen), aber mit dem tatsächlichen BUCHUNGSDATUM jeder
    Reduktion, um den Reststand der Zielforderung ÜBER DIE ZEIT (nicht
    nur den Endstand) zu rekonstruieren - Grundlage für eine taggenaue
    Verzinsung, die eine datierte Teilzahlung korrekt ab ihrem
    tatsächlichen Datum berücksichtigt statt den Ausgangsbetrag über die
    gesamte Periode zu verzinsen.

    Liefert `None`, wenn die Zielforderung unbekannte/keine Fälligkeit
    hat (z. B. eine ungegliederte GESAMTSALDO-Eröffnung) - eine solche
    Forderung wird NIE fiktiv ab einem erfundenen Datum verzinst."""

    from mietinkasso.domain.enums import OPTyp
    from mietinkasso.op.service import resolve_zahlungsziel

    positive_typen = {OPTyp.EROEFFNUNG.value, OPTyp.SOLL.value, OPTyp.RUECKLASTSCHRIFT.value}
    negative_typen = {OPTyp.GUTSCHRIFT.value, OPTyp.ZAHLUNG.value}

    forderungs_rows = [p for p in alle_positionen if p.typ in positive_typen and p.betrag_cent > 0]
    forderungs_rows.sort(key=lambda p: (p.faelligkeit or p.belegdatum, p.belegdatum, p.id))
    forderung_by_id = {p.id: p for p in forderungs_rows}

    ziel = forderung_by_id.get(ziel_op_position_id)
    if ziel is None or not ziel.faelligkeit_bekannt or ziel.faelligkeit is None:
        return None

    # Reduktionsereignisse mit Datum (Zahlung/Gutschrift, negative
    # KORREKTUR, ein Guthaben in einer eigentlich forderungsseitigen
    # Zeile) - exakt dieselben Quellen wie im Minderungs-Pool von
    # `offene_forderungen`, hier aber mit `buchungsdatum` statt nur als
    # aufsummierter Gesamtpool. `ziel_id` trägt die explizite
    # Zahlungszweckbindung (falls gesetzt) für die vorrangige Zuordnung
    # unten - `None` bedeutet "ungebunden, generisch verteilen".
    ereignisse: list[tuple[date, int, int, int | None]] = []  # (datum, betrag, id, ziel_id)
    for p in alle_positionen:
        if p.typ in negative_typen and p.betrag_cent > 0:
            ereignisse.append((p.buchungsdatum, p.betrag_cent, p.id, p.bezieht_sich_auf_id))
        elif p.typ in positive_typen and p.betrag_cent < 0:
            ereignisse.append((p.buchungsdatum, -p.betrag_cent, p.id, None))
        elif p.typ == OPTyp.KORREKTUR.value and p.betrag_cent < 0:
            ereignisse.append((p.buchungsdatum, -p.betrag_cent, p.id, None))
    ereignisse.sort(key=lambda e: (e[0], e[2]))

    verbleibend = {p.id: p.betrag_cent for p in forderungs_rows}
    aenderungen: list[tuple[date, int]] = []  # (datum, neuer_rest der Zielforderung)

    for ereignis_datum, betrag, _eid, gebundene_ziel_id in ereignisse:
        rest_zu_verteilen = betrag
        if gebundene_ziel_id is not None:
            # Dieselbe Bindungsauflösung/-prüfung wie `offene_forderungen`
            # (gleiche Kontozugehörigkeit, konsistente Leistungsperiode) -
            # eine unauflösbare/widersprüchliche Bindung wird auch hier
            # NICHT stillschweigend generisch verteilt, sondern abgelehnt.
            zahlung = next(p for p in alle_positionen if p.id == _eid)
            ziel_row = resolve_zahlungsziel(zahlung, forderung_by_id)
            aktuell = verbleibend[ziel_row.id]
            abzug = min(aktuell, rest_zu_verteilen)
            verbleibend[ziel_row.id] = aktuell - abzug
            rest_zu_verteilen -= abzug
            if ziel_row.id == ziel_op_position_id:
                aenderungen.append((ereignis_datum, verbleibend[ziel_row.id]))
        for p in forderungs_rows:
            if rest_zu_verteilen <= 0:
                break
            if p.id == gebundene_ziel_id:
                continue  # bereits oben direkt/vorrangig bedient
            aktuell = verbleibend[p.id]
            if aktuell <= 0:
                continue
            abzug = min(aktuell, rest_zu_verteilen)
            verbleibend[p.id] = aktuell - abzug
            rest_zu_verteilen -= abzug
            if p.id == ziel_op_position_id:
                aenderungen.append((ereignis_datum, verbleibend[p.id]))

    perioden: list[BalancePeriode] = []
    aktueller_rest = ziel.betrag_cent
    # Verzugszinsen beginnen erst mit dem ERSTEN TAG NACH Fälligkeit, nie
    # am Fälligkeitstag selbst (unabhängige Rückprüfung Codex 14.09.2026,
    # echter Bug, reproduziert an einer eigenen ASGI-Vorschau: Fälligkeit
    # 30.06., Stichtag 02.07. erzeugte fälschlich ein Segment 30.06.–02.07.
    # inkl. eines Juni-Verzugstags/-Halbjahressatzes. Amtlich bestätigt:
    # https://finanznavi.gv.at/glossar/verzugszinsen,
    # https://www.wko.at/vertragsrecht/zahlungsverzug-des-geschaeftspartners
    # - "ab dem Tag NACH Fälligkeit". Ändert NUR künftig neu berechnete
    # Vorschauen/Buchungen; bereits gebuchte Ledger-Einträge werden NIE
    # rückwirkend korrigiert (siehe `MahnkostenService.buche_vorschau`-
    # Moduldoc: Delta wird je Forderung gegen bereits Gebuchtes gekappt,
    # nie negativ/rückwirkend storniert).
    aktuelles_datum = ziel.faelligkeit + timedelta(days=1)
    for aenderung_datum, neuer_rest in aenderungen:
        if aenderung_datum <= aktuelles_datum:
            # Reduktion vor/auf Fälligkeit - mindert nur den Ausgangsbetrag,
            # erzeugt aber KEINE eigene Verzinsungsperiode (vor Fälligkeit
            # besteht kein Verzug).
            aktueller_rest = neuer_rest
            continue
        if aenderung_datum > heute:
            break
        if aktueller_rest > 0:
            perioden.append(BalancePeriode(von=aktuelles_datum, bis=aenderung_datum, rest_cent=aktueller_rest))
        aktueller_rest = neuer_rest
        aktuelles_datum = aenderung_datum

    if aktueller_rest > 0 and aktuelles_datum < heute:
        perioden.append(BalancePeriode(von=aktuelles_datum, bis=heute, rest_cent=aktueller_rest))

    return perioden


def zins_bis_einschliesslich(bis: date) -> date:
    """`ZinsSegment.bis`/`MahnkostenVorschau.zins_bis` sind EXKLUSIV
    (siehe `ZinsSegment`-Docstring) - für jede Anzeige gegenüber Mieter/
    Portal/Brief wird stattdessen der tatsächlich letzte verzinste Tag
    gebraucht (`bis - 1 Tag`), NIE das exklusive Enddatum selbst.
    Ändert NICHTS an der Zinsberechnung, nur an deren Anzeige."""

    return bis - timedelta(days=1)


@dataclass(frozen=True)
class ZinsSegment:
    """Ein taggenau abgegrenztes Teilstück EINER `BalancePeriode` mit
    EINEM belegten Zinssatz - bei `basis != UGB_B2B_BASISZINSSATZ` deckt
    genau ein Segment die ganze Periode ab; bei UGB_B2B wird die Periode
    an jedem Halbjahreswechsel (bzw. jeder Lücke in den erfassten
    Basiszinssätzen) in mehrere Segmente zerlegt. `satz_prozent=None`
    markiert ein Segment, dessen Basis (noch) nicht belegt ist - es
    trägt `zinsen_cent=0` und wird separat als Hinweis ausgewiesen,
    blockiert aber nicht die übrigen Segmente."""

    op_position_id: int
    von: date
    bis: date  # exklusiv
    rest_cent: int
    satz_prozent: Decimal | None
    quelle: str
    zinsen_cent: int


def _cent_ROUND_HALF_UP(betrag: Decimal) -> int:
    return int(betrag.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _zinsen_fuer_segment_cent(rest_cent: int, satz_prozent: Decimal, tage: int) -> int:
    if tage <= 0:
        return 0
    betrag = Decimal(rest_cent) * (satz_prozent / Decimal(100)) * Decimal(tage) / _TAGE_IM_JAHR
    return _cent_ROUND_HALF_UP(betrag)


def _segmentiere_periode_ugb(
    periode: BalancePeriode, *, basiszinssatz_lookup, naechster_basiszinssatz_lookup,
) -> list[tuple[date, date, Decimal | None, str]]:
    """Zerlegt EINE Periode an jedem Halbjahreswechsel/jeder Erfassungs-
    lücke in Teilsegmente. `naechster_basiszinssatz_lookup(datum) ->
    OenbBasiszinssatzTable | None` liefert den FRÜHESTEN erfassten
    Basiszinssatz mit `gueltig_von > datum` (für eine präzise
    Lückenabgrenzung, falls ein Zwischenhalbjahr fehlt, ein späteres aber
    schon erfasst ist)."""

    segmente: list[tuple[date, date, Decimal | None, str]] = []
    cursor = periode.von
    sicherung = 0
    while cursor < periode.bis:
        sicherung += 1
        if sicherung > 1000:  # Endlosschleifen-Schutz bei unerwarteten Daten
            raise ValueError(f"Zinssegmentierung bricht nicht ab (Periode {periode.von}..{periode.bis}).")
        row = basiszinssatz_lookup(cursor)
        if row is not None:
            ende = min(row.gueltig_bis + timedelta(days=1), periode.bis)
            satz = row.basiszinssatz_prozent + _UGB_AUFSCHLAG_PROZENTPUNKTE
            quelle = f"§456 UGB: {_UGB_AUFSCHLAG_PROZENTPUNKTE} Prozentpunkte über Basiszinssatz {row.basiszinssatz_prozent} % ({row.id})"
            segmente.append((cursor, ende, satz, quelle))
            cursor = ende
        else:
            naechster = naechster_basiszinssatz_lookup(cursor)
            ende = min(naechster.gueltig_von, periode.bis) if naechster is not None else periode.bis
            quelle = f"Kein OeNB-Basiszinssatz für {cursor.isoformat()} erfasst - §456-UGB-Zinsen bleiben für diesen Zeitraum blockiert, bis nachgetragen."
            segmente.append((cursor, ende, None, quelle))
            cursor = ende
    return segmente


def _basis_fuer_profil(profil: ZinsprofilTable | None) -> tuple[str, Decimal | None]:
    """Bestimmt den Basistyp für EIN konkretes, zu einem Zeitsegment
    gehörendes Zinsprofil (oder `None` - kein Profil wirksam, gesetzliche
    Basis). `konstanter_satz` ist nur bei `VEREINBART_GEPRUEFT`/
    `GESETZLICH_ABGB` gesetzt - bei `UGB_B2B_BASISZINSSATZ` wird der Satz
    weiterhin separat je Halbjahr aufgelöst (`_segmentiere_periode_ugb`)."""

    if profil is not None and profil.status == "GEPRUEFT" and profil.vereinbarung_geprueft and profil.vereinbarter_zinssatz_prozent is not None:
        return "VEREINBART_GEPRUEFT", profil.vereinbarter_zinssatz_prozent
    if _ugb_zinssatz_anwendbar(profil):
        return "UGB_B2B_BASISZINSSATZ", None
    return "GESETZLICH_ABGB", _GESETZLICHER_ZINSSATZ_PROZENT


def _zinsprofil_segmente(
    historie: list[ZinsprofilTable], von: date, bis: date,
) -> list[tuple[date, date, ZinsprofilTable | None, bool]]:
    """Zerlegt [von, bis) periodengerecht nach Zinsprofil-Versionswechseln
    (Rückprüfung 14.09.2026, Risiko 3 - "niemals rückwirkend den aktuellen
    Satz verwenden"). Rückgabe je Segment: (seg_von, seg_bis, profil,
    unberechenbar).

    - 0 oder 1 jemals GEPRÜFTE Version: unverändertes Verhalten, sie gilt
      (falls vorhanden) für die GESAMTE Periode - keine Mehrdeutigkeit
      möglich.
    - Mehrere GEPRÜFTE Versionen, ALLE mit belegtem `gueltig_ab`:
      periodengerechte Zerlegung an den `gueltig_ab`-Wechseln; VOR dem
      frühesten `gueltig_ab` gilt mangels bekannter Vereinbarung die
      gesetzliche Basis (kein Profil, NICHT "unberechenbar" - das ist der
      sichere gesetzliche Normalfall).
    - Mehrere GEPRÜFTE Versionen, aber MINDESTENS EINE ohne `gueltig_ab`:
      die GESAMTE Periode bleibt explizit "unberechenbar" (`profil=None,
      unberechenbar=True`) - wir wissen, dass sich die Vereinbarung
      geändert hat, aber NICHT wann, und raten das nicht anhand der
      zuletzt geprüften Version."""

    if len(historie) <= 1:
        return [(von, bis, historie[0] if historie else None, False)]
    if any(p.gueltig_ab is None for p in historie):
        return [(von, bis, None, True)]

    sortiert = sorted(historie, key=lambda p: p.gueltig_ab)
    segmente: list[tuple[date, date, ZinsprofilTable | None, bool]] = []
    cursor = von
    erste_gueltig_ab = sortiert[0].gueltig_ab
    if cursor < min(erste_gueltig_ab, bis):
        segmente.append((cursor, min(erste_gueltig_ab, bis), None, False))
        cursor = min(erste_gueltig_ab, bis)
    for index, profil in enumerate(sortiert):
        if cursor >= bis:
            break
        naechster_wechsel = sortiert[index + 1].gueltig_ab if index + 1 < len(sortiert) else None
        seg_von = max(cursor, profil.gueltig_ab)
        seg_bis = min(naechster_wechsel, bis) if naechster_wechsel is not None else bis
        if seg_von < seg_bis:
            segmente.append((seg_von, seg_bis, profil, False))
            cursor = seg_bis
    return segmente


def _profil_wirksam_am(historie: list[ZinsprofilTable], datum: date) -> tuple[ZinsprofilTable | None, bool]:
    """Das für GENAU `datum` (typischerweise `heute`) wirksame Profil -
    für die §458-Gebührenprüfung (Präsens: "sind wir GERADE JETZT B2B mit
    belegter Kostenbasis"), NICHT rückwirkend für die Zinsperiode
    verwendet (siehe `_zinsprofil_segmente`). `unberechenbar=True`
    bedeutet: mehrere Versionen ohne durchgängiges `gueltig_ab`, auch die
    AKTUELLE Einordnung ist damit nicht sicher bestimmbar."""

    segmente = _zinsprofil_segmente(historie, datum, datum + timedelta(days=1))
    _seg_von, _seg_bis, profil, unberechenbar = segmente[-1]
    return profil, unberechenbar


def _zinssegmente_fuer_periode(
    *, op_position_id: int, periode: BalancePeriode, zinsprofil_historie: list[ZinsprofilTable],
    basiszinssatz_lookup, naechster_basiszinssatz_lookup,
) -> list[ZinsSegment]:
    ergebnis: list[ZinsSegment] = []
    for seg_von, seg_bis, profil, unberechenbar in _zinsprofil_segmente(zinsprofil_historie, periode.von, periode.bis):
        if unberechenbar:
            ergebnis.append(ZinsSegment(
                op_position_id, seg_von, seg_bis, periode.rest_cent, None,
                "Vertragszinswechsel ohne durchgängig belegtes Wirksamkeitsdatum (gueltig_ab) - Zinsen für "
                "diesen Zeitraum sind unberechenbar, bis die Historie geklärt/belegt ist.", 0,
            ))
            continue
        basis, konstanter_satz = _basis_fuer_profil(profil)
        if basis == "UGB_B2B_BASISZINSSATZ":
            rohsegmente = _segmentiere_periode_ugb(
                BalancePeriode(seg_von, seg_bis, periode.rest_cent),
                basiszinssatz_lookup=basiszinssatz_lookup, naechster_basiszinssatz_lookup=naechster_basiszinssatz_lookup,
            )
        else:
            quelle = "Vereinbarter, geprüfter Zinssatz" if basis == "VEREINBART_GEPRUEFT" else "Gesetzliche Verzugszinsen §1000 ABGB"
            rohsegmente = [(seg_von, seg_bis, konstanter_satz, quelle)]
        for von, bis, satz, quelle in rohsegmente:
            tage = (bis - von).days
            zinsen = _zinsen_fuer_segment_cent(periode.rest_cent, satz, tage) if satz is not None else 0
            ergebnis.append(ZinsSegment(op_position_id, von, bis, periode.rest_cent, satz, quelle, zinsen))
    return ergebnis


@dataclass(frozen=True)
class GebuehrSegment:
    """Eine EINZELNE, neu anzusetzende §458-UGB-Pauschale für GENAU EINE
    zugrunde liegende Entgeltforderung (siehe Moduldoc). Mehrere
    Segmente in einem Lauf bedeuten mehrere GENUIN unterschiedliche,
    bisher noch nicht bepauschalte Entgeltforderungen (z. B. zwei
    verschiedene säumige Monate), NICHT mehrere Komponenten derselben
    Forderung."""

    entgeltforderung_schluessel: str
    betrag_cent: int
    rechtsgrundlage: str


def _entgeltforderung_schluessel(forderung: OffeneForderung) -> str:
    """Gruppierungsschlüssel für §458 UGB: alle OP-Zeilen derselben
    Vorschreibungsperiode (z. B. HMZ+BK+HK/Küche/Parkplatz desselben
    Monats) teilen dieselbe `leistungsperiode` und bilden damit EINE
    Entgeltforderung. Eine Forderung ohne Periodenangabe (z. B. eine
    einmalige Nachbuchung) ist ihre eigene, alleinstehende
    Entgeltforderung."""

    if forderung.leistungsperiode:
        return f"PERIODE:{forderung.leistungsperiode}"
    return f"OP:{forderung.op_position_id}"


def _faellige_entgeltforderungs_schluessel(forderungen: list[OffeneForderung], heute: date) -> list[str]:
    gesehen: list[str] = []
    for forderung in forderungen:
        if not forderung.faelligkeit_bekannt or forderung.faelligkeit is None or forderung.faelligkeit > heute:
            continue
        schluessel = _entgeltforderung_schluessel(forderung)
        if schluessel not in gesehen:
            gesehen.append(schluessel)
    return gesehen


@dataclass(frozen=True)
class MahnkostenVorschau:
    vertrag_id: str
    stufe: int
    hauptforderung_cent: int
    zinsbasis: str
    zinssatz_prozent: Decimal | None  # nur gesetzt, wenn ALLE aufgelösten Segmente denselben Satz teilen
    zins_von: date | None
    zins_bis: date | None
    neue_zinsen_cent: int
    bereits_gebuchte_zinsen_cent: int
    neue_zinsen_delta_cent: int
    # JE `op_position_id` (nicht nur die Summe `neue_zinsen_delta_cent`) -
    # wird 1:1 in `MahnkostenBuchungTable.zinsen_delta_je_op_json`
    # persistiert (unabhängige Rückprüfung Codex 14.09.2026: NUR dieses
    # Delta darf später als "bereits gebucht" gegengerechnet werden,
    # NIEMALS die volle, ab der Fälligkeit neu berechnete
    # `zins_segmente`-Periode - siehe `MahnkostenBuchungTable`-Docstring).
    neue_zinsen_delta_je_op_position: dict[int, int]
    zins_segmente: tuple[ZinsSegment, ...]
    zins_teilweise_ungeklaert: bool  # mind. ein Segment hat satz_prozent=None
    gebuehr_segmente: tuple[GebuehrSegment, ...]
    gebuehr_cent: int | None  # Summe von gebuehr_segmente, None wenn leer
    gebuehr_rechtsgrundlage: str | None  # Rechtsgrundlage des ERSTEN Segments (informativ)
    forderung_op_position_ids: tuple[int, ...]
    ausgeschlossene_forderungen_hinweis: tuple[str, ...] = field(default_factory=tuple)
    hinweise: tuple[str, ...] = field(default_factory=tuple)
    # Reiner Transparenzwert (Auftrag Markus 14.09.2026, Brief-Kostenpaket):
    # der TATSÄCHLICHE Anbieteraufwand (Druck+Kuvert+Porto+Nachweis) laut
    # freigegebenem `BriefAnbieterProfilTable`, NUR gesetzt wenn Kanal
    # BRIEF und ein freigegebenes Profil vorliegt - unabhängig davon, ob
    # (und in welcher Höhe) davon überhaupt etwas als `gebuehr_segmente`
    # ersatzfähig angesetzt wird (siehe dortige §1333/§458-Weiche). Zeigt
    # dem Portal ehrlich den Unterschied zwischen "was der Versand
    # tatsächlich kostet" und "was davon dem Mieter verrechnet wird".
    versandkosten_anbieteraufwand_cent: int | None = None
    # Rückprüfung 14.09.2026, echter Bug: `hauptforderung_cent` enthält
    # NIE mehr die vom Mahnwesen selbst gebuchten, noch offenen Zinsen-/
    # Gebühr-SOLL-Zeilen (`OPPositionTable.quelle_system == "mahnkosten"`,
    # siehe `kosten_service.py::buche_vorschau`) - diese würden sonst bei
    # einer SPÄTEREN Vorschau nochmals aus `offene_forderungen()`
    # hereinkommen und die Hauptforderung künstlich aufblähen (konkreter
    # Repro: 830 EUR Hauptforderung + bereits gebuchte 40-EUR-Pauschale
    # ergäbe sonst fälschlich 870 EUR "Hauptforderung"). Dieses Feld zeigt
    # stattdessen TRANSPARENT, wie viel aus FRÜHEREN Mahnläufen bereits
    # angesetzte Zinsen/Gebühren noch offen (unbezahlt) sind - separat von
    # `hauptforderung_cent` (echte Miet-/BK-Forderungen) UND von
    # `bereits_gebuchte_zinsen_cent` (das reine Ledger-Delta für die
    # laufende Neuberechnung), damit nichts doppelt gezählt wird.
    bereits_offene_mahnkosten_cent: int = 0

    @property
    def zusaetzlicher_betrag_cent(self) -> int:
        """Der NEU anzusetzende Betrag (`neue_zinsen_delta_cent`, siehe
        dortiger Docstring - bereits JE FORDERUNG um früher gebuchte
        Zinsen bereinigt, plus alle neuen, noch nicht erhobenen
        §458-Pauschalen)."""

        zusatz = self.neue_zinsen_delta_cent
        if self.gebuehr_cent is not None:
            zusatz += self.gebuehr_cent
        return zusatz


def _vorschau_zu_dict(vorschau: MahnkostenVorschau) -> dict:
    """Vollständige, verlustfreie Serialisierung EINER `MahnkostenVorschau`
    - Grundlage des eingefrorenen Kosten-/Inhaltssnapshots (Auftrag
    Markus 14.09.2026: "Recovery nach bestätigtem Versand mit
    eingefrorenem Kosten-/Inhaltssnapshot"). Muss exakt zu
    `_vorschau_aus_dict` passen (Round-Trip-Eigenschaft, siehe Tests)."""

    return {
        "vertrag_id": vorschau.vertrag_id, "stufe": vorschau.stufe,
        "hauptforderung_cent": vorschau.hauptforderung_cent, "zinsbasis": vorschau.zinsbasis,
        "zinssatz_prozent": str(vorschau.zinssatz_prozent) if vorschau.zinssatz_prozent is not None else None,
        "zins_von": vorschau.zins_von.isoformat() if vorschau.zins_von else None,
        "zins_bis": vorschau.zins_bis.isoformat() if vorschau.zins_bis else None,
        "neue_zinsen_cent": vorschau.neue_zinsen_cent,
        "bereits_gebuchte_zinsen_cent": vorschau.bereits_gebuchte_zinsen_cent,
        "neue_zinsen_delta_cent": vorschau.neue_zinsen_delta_cent,
        "neue_zinsen_delta_je_op_position": {str(k): v for k, v in vorschau.neue_zinsen_delta_je_op_position.items()},
        "zins_segmente": [
            {"op_position_id": s.op_position_id, "von": s.von.isoformat(), "bis": s.bis.isoformat(),
             "rest_cent": s.rest_cent, "satz_prozent": str(s.satz_prozent) if s.satz_prozent is not None else None,
             "quelle": s.quelle, "zinsen_cent": s.zinsen_cent}
            for s in vorschau.zins_segmente
        ],
        "zins_teilweise_ungeklaert": vorschau.zins_teilweise_ungeklaert,
        "gebuehr_segmente": [
            {"entgeltforderung_schluessel": g.entgeltforderung_schluessel, "betrag_cent": g.betrag_cent,
             "rechtsgrundlage": g.rechtsgrundlage}
            for g in vorschau.gebuehr_segmente
        ],
        "gebuehr_cent": vorschau.gebuehr_cent, "gebuehr_rechtsgrundlage": vorschau.gebuehr_rechtsgrundlage,
        "forderung_op_position_ids": list(vorschau.forderung_op_position_ids),
        "ausgeschlossene_forderungen_hinweis": list(vorschau.ausgeschlossene_forderungen_hinweis),
        "hinweise": list(vorschau.hinweise),
        "versandkosten_anbieteraufwand_cent": vorschau.versandkosten_anbieteraufwand_cent,
        "bereits_offene_mahnkosten_cent": vorschau.bereits_offene_mahnkosten_cent,
    }


def _vorschau_aus_dict(data: dict) -> MahnkostenVorschau:
    return MahnkostenVorschau(
        vertrag_id=data["vertrag_id"], stufe=data["stufe"], hauptforderung_cent=data["hauptforderung_cent"],
        zinsbasis=data["zinsbasis"],
        zinssatz_prozent=Decimal(data["zinssatz_prozent"]) if data["zinssatz_prozent"] is not None else None,
        zins_von=date.fromisoformat(data["zins_von"]) if data["zins_von"] else None,
        zins_bis=date.fromisoformat(data["zins_bis"]) if data["zins_bis"] else None,
        neue_zinsen_cent=data["neue_zinsen_cent"], bereits_gebuchte_zinsen_cent=data["bereits_gebuchte_zinsen_cent"],
        neue_zinsen_delta_cent=data["neue_zinsen_delta_cent"],
        neue_zinsen_delta_je_op_position={int(k): v for k, v in data["neue_zinsen_delta_je_op_position"].items()},
        zins_segmente=tuple(
            ZinsSegment(
                s["op_position_id"], date.fromisoformat(s["von"]), date.fromisoformat(s["bis"]), s["rest_cent"],
                Decimal(s["satz_prozent"]) if s["satz_prozent"] is not None else None, s["quelle"], s["zinsen_cent"],
            )
            for s in data["zins_segmente"]
        ),
        zins_teilweise_ungeklaert=data["zins_teilweise_ungeklaert"],
        gebuehr_segmente=tuple(
            GebuehrSegment(g["entgeltforderung_schluessel"], g["betrag_cent"], g["rechtsgrundlage"])
            for g in data["gebuehr_segmente"]
        ),
        gebuehr_cent=data["gebuehr_cent"], gebuehr_rechtsgrundlage=data["gebuehr_rechtsgrundlage"],
        forderung_op_position_ids=tuple(data["forderung_op_position_ids"]),
        ausgeschlossene_forderungen_hinweis=tuple(data["ausgeschlossene_forderungen_hinweis"]),
        hinweise=tuple(data["hinweise"]),
        # `.get(...)`: ein VOR diesem Auftrag persistierter Snapshot kennt
        # dieses Feld noch nicht - additiv-sicher statt eines KeyError.
        versandkosten_anbieteraufwand_cent=data.get("versandkosten_anbieteraufwand_cent"),
        bereits_offene_mahnkosten_cent=data.get("bereits_offene_mahnkosten_cent", 0),
    )


def snapshot_zu_json(vorschau: MahnkostenVorschau | None) -> str:
    """Friert `vorschau` (oder explizit `None` - "kein Kostenservice
    konfiguriert"/"nichts zu berechnen") als JSON-Text ein - wird ATOMAR
    mit dem GESENDET-Übergang persistiert (`MahnLaufTable.
    mahnkosten_snapshot_json`, siehe dortiger Docstring). NIE aus einer
    späteren Neuberechnung ableiten."""

    return json.dumps(_vorschau_zu_dict(vorschau) if vorschau is not None else None)


def snapshot_aus_json(payload: str | None) -> MahnkostenVorschau | None:
    """Kehrseite zu `snapshot_zu_json` - liefert `None`, wenn die Spalte
    selbst `NULL` ist (noch nie bis zum Versand gekommen) ODER das JSON
    explizit `null` ist (kein Kostenservice/nichts zu buchen)."""

    if payload is None:
        return None
    data = json.loads(payload)
    return _vorschau_aus_dict(data) if data is not None else None


def vorschau_bei_ledger_inkonsistenz(
    *, vertrag_id: str, stufe: int, forderungen: list[OffeneForderung], grund: str,
) -> MahnkostenVorschau:
    """Sicherer Rückfall für `kosten_service.py::vorschau()`, wenn
    `MahnkostenRepository.bereits_gebuchte_zinsen_je_op_position`
    `ZinsledgerInkonsistentError` auslöst (unabhängige Rückprüfung
    Codex 14.09.2026) - dieselbe "niemals raten"-Haltung wie bei einer
    unberechenbaren Zinsprofil-Historie (siehe `_zinsprofil_segmente`):
    KEINE Zinsen/Gebühr für den GESAMTEN Vertrag, solange der Ledger
    selbst widersprüchlich ist, aber die Hauptforderung bleibt
    unblockiert."""

    # Wie in `berechne_mahnkosten_vorschau`: bereits vom Mahnwesen selbst
    # gebuchte, noch offene Zinsen-/Gebühr-Zeilen zählen NIE als
    # Hauptforderung (Rückprüfung 14.09.2026, echter Bug).
    forderungen_kern = [f for f in forderungen if f.quelle_system != "mahnkosten"]
    forderungen_mahnkosten = [f for f in forderungen if f.quelle_system == "mahnkosten"]
    hauptforderung_cent = sum(f.rest_cent for f in forderungen_kern)
    bereits_offene_mahnkosten_cent = sum(f.rest_cent for f in forderungen_mahnkosten)
    return MahnkostenVorschau(
        vertrag_id=vertrag_id, stufe=stufe, hauptforderung_cent=hauptforderung_cent,
        zinsbasis="UNBERECHENBAR", zinssatz_prozent=None, zins_von=None, zins_bis=None,
        neue_zinsen_cent=0, bereits_gebuchte_zinsen_cent=0, neue_zinsen_delta_cent=0,
        neue_zinsen_delta_je_op_position={}, zins_segmente=(), zins_teilweise_ungeklaert=True,
        gebuehr_segmente=(), gebuehr_cent=None, gebuehr_rechtsgrundlage=None,
        forderung_op_position_ids=tuple(f.op_position_id for f in forderungen_kern),
        hinweise=(f"Zinsledger widersprüchlich, Zinsen/Gebühr bleiben blockiert: {grund}",),
        bereits_offene_mahnkosten_cent=bereits_offene_mahnkosten_cent,
    )


def berechne_mahnkosten_vorschau(
    *,
    vertrag_id: str,
    stufe: int,
    forderungen: list[OffeneForderung],
    alle_positionen: list[OPPositionTable],
    heute: date,
    zinsprofil_historie: list[ZinsprofilTable],
    basiszinssatz_lookup,
    naechster_basiszinssatz_lookup,
    bereits_gebuchte_zinsen_je_op_position: dict[int, int],
    bereits_erhobene_gebuehr_schluessel: frozenset[str],
    kanal: str = "EMAIL",
    brief_anbieterprofil: BriefAnbieterProfilTable | None = None,
) -> MahnkostenVorschau:
    """Aggregiert ALLE offenen Forderungen dieses Vertrags zu EINER
    Mahnlauf-Kostenvorschau (Hauptforderung, Zinsen, Gebühr) - NIE eine
    Gebühr/einen Zinsbetrag je einzelner OP-Zeile/Mietkomponente (siehe
    Moduldoc). Forderungen ohne bekannte Fälligkeit (z. B. eine
    ungegliederte GESAMTSALDO-Eröffnung) fließen in die Hauptforderung
    ein, werden aber NICHT verzinst - das wird als Hinweis ausgewiesen,
    blockiert aber nicht die Hauptforderung selbst.

    `zinsprofil_historie`: ALLE jemals GEPRÜFTEN Versionen (aufsteigend),
    siehe `MahnkostenRepository.historie_geprueft` - die Zinsen werden
    PERIODENGERECHT je nach zum jeweiligen Zeitpunkt wirksamer Version
    berechnet (`_zinsprofil_segmente`), NIE rückwirkend mit der zuletzt
    geprüften Version (Rückprüfung 14.09.2026, Risiko 3).
    `naechster_basiszinssatz_lookup(datum) -> OenbBasiszinssatzTable |
    None` - siehe `_segmentiere_periode_ugb`.
    `bereits_erhobene_gebuehr_schluessel` - siehe
    `MahnkostenRepository.bereits_erhobene_gebuehr_schluessel`; PERMANENT
    über alle Mahnläufe/Stufen dieses Vertrags hinweg, nicht nur die
    aktuelle Stufe.

    `bereits_gebuchte_zinsen_je_op_position`: JE `op_position_id`
    aufgeschlüsselt (siehe `MahnkostenRepository.
    bereits_gebuchte_zinsen_je_op_position`) - unabhängige Rückprüfung
    Codex 14.09.2026: das Delta wird JE FORDERUNG gebildet und bei 0
    gekappt, BEVOR es summiert wird (`neue_zinsen_delta_cent`). Eine
    vertragsweite Blanko-Subtraktion (die alte, fehlerhafte
    Berechnungsweise) würde die Verzinsung einer genuin NEUEN Forderung
    fälschlich schlucken, sobald für eine ANDERE, mittlerweile
    abgelöste/geschlossene Forderung früher bereits Zinsen gebucht
    wurden.

    `kanal`/`brief_anbieterprofil` (Auftrag Markus 14.09.2026, Brief-
    Kostenpaket): §458 UGB (B2B, `ugb_scope`) und die §1333-Abs-2-ABGB-
    Versandkosten-Ersatzfähigkeit (Nicht-B2B/Verbraucher, NUR Kanal
    BRIEF) sind EINANDER AUSSCHLIESSEND für dieselbe Entgeltforderung -
    ist B2B einschlägig, gilt AUSSCHLIESSLICH §458 (verschuldensunabhängig,
    unabhängig vom tatsächlichen Porto), NIE zusätzlich die tatsächlichen
    Versandkosten (keine doppelte Entschädigung). Beide teilen sich
    DIESELBE `MahnkostenGebuehrTable`-Ledger-Exklusivität je
    `entgeltforderung_schluessel` (siehe `GebuehrSegment`) - welche der
    beiden Rechtsgrundlagen für eine Forderung zuerst reserviert wird,
    besetzt sie dauerhaft; es gibt NIE einen nachträglichen Aufschlag auf
    eine bereits einmal (ggf. reduziert) erhobene Position."""

    # Rückprüfung 14.09.2026, echter Bug: eine vom Mahnwesen SELBST bereits
    # gebuchte, noch offene Zinsen-/Gebühr-SOLL-Zeile
    # (`OPPositionTable.quelle_system == "mahnkosten"`, siehe
    # `kosten_service.py::buche_vorschau`) kommt über `offene_forderungen()`
    # bei einer SPÄTEREN Vorschau erneut herein - sie ist aber keine echte
    # Miet-/BK-Forderung und darf NIE nochmals in die Hauptforderung
    # einfließen (das würde bereits gebuchte, noch unbezahlte Mahnkosten
    # doppelt zählen: einmal hier, einmal implizit über die künftige
    # Zahlung). Aus demselben Grund wird eine solche Zeile auch NIE selbst
    # als neu zu bepauschalende "Entgeltforderung" behandelt (kein §458/
    # §1333-Segment auf eine bereits gebuchte Mahnspesen-Zeile) und NIE ein
    # zweites Mal verzinst.
    forderungen_kern = [f for f in forderungen if f.quelle_system != "mahnkosten"]
    forderungen_mahnkosten = [f for f in forderungen if f.quelle_system == "mahnkosten"]
    hauptforderung_cent = sum(f.rest_cent for f in forderungen_kern)
    bereits_offene_mahnkosten_cent = sum(f.rest_cent for f in forderungen_mahnkosten)

    aktuelles_profil, aktuell_unberechenbar = _profil_wirksam_am(zinsprofil_historie, heute)
    ugb_scope = ugb_anwendbar(aktuelles_profil)
    aktuelle_basis, _ = _basis_fuer_profil(aktuelles_profil) if not aktuell_unberechenbar else (None, None)

    hinweise: list[str] = []
    if aktuell_unberechenbar:
        hinweise.append(
            "Zinsprofil: mehrere geprüfte Versionen ohne durchgängig belegtes Wirksamkeitsdatum (gueltig_ab) - "
            "die aktuell gültige Einordnung ist unberechenbar, bis die Historie geklärt ist."
        )
    elif aktuelle_basis == "VEREINBART_GEPRUEFT":
        hinweise.append(f"Vereinbarter, geprüfter Zinssatz laut Profil ({aktuelles_profil.vereinbarung_beleg or 'ohne Belegangabe'}).")
    elif aktuelle_basis == "UGB_B2B_BASISZINSSATZ":
        hinweise.append(
            f"§456 UGB: {_UGB_AUFSCHLAG_PROZENTPUNKTE} Prozentpunkte über dem für jedes betroffene Halbjahr "
            "belegten Basiszinssatz - eine Periode über einen Halbjahreswechsel wird in Teilsegmente zerlegt."
        )
    else:
        hinweise.append("Gesetzliche Verzugszinsen §1000 ABGB (keine geprüfte abweichende Vereinbarung/kein geprüftes B2B-Profil).")

    # Rückprüfung 14.09.2026, echter Bug (nachgelaufene Zinsen bei
    # vollständiger Zahlung): eine Forderung, die zwischen der letzten
    # Zinsberechnung und heute VOLLSTÄNDIG bezahlt wird, verschwindet aus
    # `offene_forderungen()` (rest_cent == 0) und damit aus
    # `forderungen_kern` - die zwischen der letzten Buchung und ihrer
    # tatsächlichen Zahlung TATSÄCHLICH ANGEFALLENEN, noch nicht
    # gebuchten Zinsen dürfen dadurch nicht ersatzlos verschwinden (§1000
    # ABGB läuft bis zur tatsächlichen Zahlung, nicht bis zum letzten
    # Mahnlauf). Nur eine Forderung, für die BEREITS EINMAL Zinsen
    # gebucht wurden (`bereits_gebuchte_zinsen_je_op_position`), wird
    # dafür aus den Rohdaten (`alle_positionen`) reaktiviert -
    # `balance_zeitreihe_fuer_forderung` berücksichtigt die Zahlung
    # ohnehin taggenau und liefert dann automatisch nur die Periode BIS
    # zur Zahlung (rest_cent erreicht danach 0, keine erfundene
    # Nachverzinsung). Eine Forderung, die VOR jeder Berechnung bereits
    # vollständig bezahlt war (nie Teil eines Mahnlaufs), wird NIE
    # rückwirkend neu entdeckt. Nebenforderungen (`quelle_system ==
    # "mahnkosten"`) werden hier nie reaktiviert - sie tauchen aus
    # demselben Grund gar nicht erst in diesem Dictionary auf.
    from mietinkasso.domain.enums import OPTyp as _OPTyp

    _bekannte_op_ids = {f.op_position_id for f in forderungen_kern}
    _positive_typen_werte = {_OPTyp.EROEFFNUNG.value, _OPTyp.SOLL.value, _OPTyp.RUECKLASTSCHRIFT.value}
    forderungen_getilgt_reaktiviert = [
        OffeneForderung(
            op_position_id=p.id, art=p.typ, betrag_cent=p.betrag_cent, rest_cent=0,
            belegdatum=p.belegdatum, faelligkeit=p.faelligkeit, faelligkeit_bekannt=p.faelligkeit_bekannt,
            leistungsperiode=p.leistungsperiode, quelle_system=p.quelle_system,
        )
        for p in alle_positionen
        if p.id in bereits_gebuchte_zinsen_je_op_position and p.id not in _bekannte_op_ids
        and p.typ in _positive_typen_werte and p.betrag_cent > 0 and p.quelle_system != "mahnkosten"
    ]
    forderungen_fuer_verzinsung = forderungen_kern + forderungen_getilgt_reaktiviert

    ausgeschlossen: list[str] = []
    alle_segmente: list[ZinsSegment] = []

    for forderung in forderungen_fuer_verzinsung:
        perioden = balance_zeitreihe_fuer_forderung(
            ziel_op_position_id=forderung.op_position_id, alle_positionen=alle_positionen, heute=heute,
        )
        if perioden is None:
            ausgeschlossen.append(
                f"Forderung #{forderung.op_position_id} ({forderung.art}): Fälligkeit unbekannt/ungegliedert - "
                "wird NICHT verzinst (kein fiktives Startdatum)."
            )
            continue
        for periode in perioden:
            alle_segmente.extend(_zinssegmente_fuer_periode(
                op_position_id=forderung.op_position_id, periode=periode, zinsprofil_historie=zinsprofil_historie,
                basiszinssatz_lookup=basiszinssatz_lookup, naechster_basiszinssatz_lookup=naechster_basiszinssatz_lookup,
            ))

    neue_zinsen_gesamt = sum(s.zinsen_cent for s in alle_segmente)

    # Delta JE FORDERUNG (op_position_id), NICHT vertragsweit gesamt -
    # siehe Funktions-Docstring/Rückprüfung Codex 14.09.2026.
    betroffene_ops = {s.op_position_id for s in alle_segmente}
    neue_zinsen_delta_gesamt = 0
    neue_zinsen_delta_je_op: dict[int, int] = {}
    bereits_gebuchte_relevant_cent = 0
    for op_id in betroffene_ops:
        zinsen_dieser_op = sum(s.zinsen_cent for s in alle_segmente if s.op_position_id == op_id)
        bereits_op = bereits_gebuchte_zinsen_je_op_position.get(op_id, 0)
        bereits_gebuchte_relevant_cent += min(zinsen_dieser_op, bereits_op)
        delta = max(zinsen_dieser_op - bereits_op, 0)
        neue_zinsen_delta_gesamt += delta
        if delta > 0:
            neue_zinsen_delta_je_op[op_id] = delta

    zins_teilweise_ungeklaert = any(s.satz_prozent is None for s in alle_segmente)
    if zins_teilweise_ungeklaert:
        unresolved_count = sum(1 for s in alle_segmente if s.satz_prozent is None)
        hinweise.append(
            f"Verzugszinsen: {unresolved_count} Zeitsegment(e) ohne belegte Zins-/Basiszinssatzgrundlage - "
            "dieser Anteil wird NICHT mitverzinst und muss nachgetragen werden, sobald die Grundlage geklärt ist."
        )
    aufgeloeste_saetze = {s.satz_prozent for s in alle_segmente if s.satz_prozent is not None}
    zinssatz_prozent = next(iter(aufgeloeste_saetze)) if len(aufgeloeste_saetze) == 1 else None

    zins_von = min((s.von for s in alle_segmente), default=None)
    zins_bis = max((s.bis for s in alle_segmente), default=None)

    gebuehr_segmente: list[GebuehrSegment] = []
    versandkosten_anbieteraufwand_cent: int | None = None
    if aktuell_unberechenbar:
        hinweise.append("§458 UGB: keine Mahngebühr, solange die Zinsprofil-Historie unberechenbar ist.")
    elif ugb_scope:
        # §458 UGB (B2B) gilt EXKLUSIV für diese Entgeltforderung - selbst
        # im Briefkanal NIE zusätzlich die tatsächlichen Versandkosten
        # (keine doppelte Entschädigung, siehe Funktions-Docstring).
        if aktuelles_profil is None or aktuelles_profil.status != "GEPRUEFT" or aktuelles_profil.mahngebuehr_kostenbasis_cent is None:
            hinweise.append("Mahngebühr: Klärung erforderlich (kein geprüftes Zinsprofil mit belegter §458-Kostenbasis hinterlegt).")
        elif not (0 <= aktuelles_profil.mahngebuehr_kostenbasis_cent <= S458_UGB_HOECHSTBETRAG_CENT):
            # Defensiv (unabhängige Rückprüfung Codex 14.09.2026, echter
            # Bug): `zinsprofil_anlegen` weist einen solchen Wert seit
            # diesem Fix bereits beim Anlegen ab - ein BEREITS
            # bestehendes, davor angelegtes ungültiges Profil (z. B. aus
            # der Zeit vor der Formularumstellung von Cent auf EUR) wird
            # hier trotzdem NIE stillschweigend verwendet, sondern die
            # Pauschale bleibt sichtbar blockiert, bis das Profil
            # korrigiert ist - niemals automatisch auf 40 EUR gekappt.
            hinweise.append(
                f"Mahngebühr: geprüfte Kostenbasis ({aktuelles_profil.mahngebuehr_kostenbasis_cent / 100:.2f} EUR) "
                f"überschreitet den gesetzlichen §458-UGB-Höchstbetrag von 40,00 EUR - Pauschale bleibt blockiert, "
                "bis das Zinsprofil korrigiert ist."
            )
        else:
            rechtsgrundlage = f"§458 UGB - geprüfte Kostenbasis ({aktuelles_profil.mahngebuehr_kostenbasis_beleg or 'ohne Belegangabe'})"
            fuer_gebuehr_qualifiziert = [
                schluessel for schluessel in _faellige_entgeltforderungs_schluessel(forderungen_kern, heute)
                if schluessel not in bereits_erhobene_gebuehr_schluessel
            ]
            for schluessel in fuer_gebuehr_qualifiziert:
                gebuehr_segmente.append(GebuehrSegment(schluessel, aktuelles_profil.mahngebuehr_kostenbasis_cent, rechtsgrundlage))
            if gebuehr_segmente:
                hinweise.append(
                    f"§458 UGB: {len(gebuehr_segmente)} neue Pauschale(n) für bislang noch nicht bepauschalte "
                    "Entgeltforderung(en) - eine bereits erhobene Pauschale wird nie wiederholt."
                )
            else:
                hinweise.append("§458 UGB: keine neue Pauschale - alle fälligen Entgeltforderungen wurden bereits einmalig bepauschalt.")
    elif kanal == "BRIEF":
        # §1333 Abs 2 ABGB - Versandkosten-Ersatz gilt NUR für den
        # Briefkanal bei einem NICHT-B2B-Vertrag (Verbraucher-Mieter
        # eingeschlossen, anders als §458), NUR mit gebündelt geprüfter
        # Ersatzfähigkeit UND einem tatsächlich freigegebenen Brief-
        # Anbieterprofil - ein bloß vorhandenes Profil reicht NICHT
        # (Auftrag Markus 14.09.2026).
        if aktuelles_profil is None or aktuelles_profil.status != "GEPRUEFT" or not aktuelles_profil.versandkosten_ersatzfaehig_geprueft:
            hinweise.append(
                "§1333 Abs 2 ABGB: Versandkosten-Ersatzfähigkeit für diesen Vertrag nicht geprüft - "
                "keine Versandkosten-Position angesetzt."
            )
        elif brief_anbieterprofil is None:
            hinweise.append("§1333 Abs 2 ABGB: kein freigegebenes Brief-Anbieterprofil hinterlegt - keine Versandkosten-Position angesetzt.")
        else:
            versandkosten_anbieteraufwand_cent = (
                brief_anbieterprofil.preis_druck_cent + brief_anbieterprofil.preis_kuvert_cent
                + brief_anbieterprofil.preis_porto_cent + (brief_anbieterprofil.preis_nachweis_cent or 0)
            )
            ersatzfaehiger_betrag_cent = versandkosten_anbieteraufwand_cent
            if brief_anbieterprofil.ersatzfaehiger_hoechstbetrag_cent is not None:
                ersatzfaehiger_betrag_cent = min(ersatzfaehiger_betrag_cent, brief_anbieterprofil.ersatzfaehiger_hoechstbetrag_cent)
            rechtsgrundlage = "§1333 Abs 2 ABGB - ersatzfähige Versandkosten (geprüft)"
            fuer_gebuehr_qualifiziert = [
                schluessel for schluessel in _faellige_entgeltforderungs_schluessel(forderungen_kern, heute)
                if schluessel not in bereits_erhobene_gebuehr_schluessel
            ]
            # Rückprüfung 14.09.2026, echter Bug: §1333 Abs 2 ABGB ersetzt
            # die TATSÄCHLICHEN Kosten DIESES EINEN Briefs (Druck+Kuvert+
            # Porto+Nachweis) - anders als die je-Entgeltforderung
            # gedachte §458-UGB-Pauschale entsteht dieser Aufwand GENAU
            # EINMAL je Versand, unabhängig davon, wie viele fällige
            # Monatsforderungen in diesem einen Brief gebündelt sind. Bei
            # mehreren qualifizierten Entgeltforderungen in DERSELBEN
            # Vorschau (ein einziger Brief) wird der volle, gedeckelte
            # Ersatzbetrag deshalb NUR EINMAL angesetzt - auf die (nach
            # `_faellige_entgeltforderungs_schluessel`, also FIFO-)
            # ERSTE qualifizierte Entgeltforderung als Trägerin der
            # dauerhaften Ledger-Sperre; die übrigen Entgeltforderungen
            # dieses Briefs bleiben für §1333 unberührt (bekommen KEIN
            # eigenes Segment) und können bei einem SPÄTEREN, separaten
            # Brief noch ihre eigene tatsächliche Versandkosten-Position
            # auslösen. Nie derselbe Portoaufwand mehrfach angesetzt.
            if fuer_gebuehr_qualifiziert:
                gebuehr_segmente.append(
                    GebuehrSegment(fuer_gebuehr_qualifiziert[0], ersatzfaehiger_betrag_cent, rechtsgrundlage)
                )
            if gebuehr_segmente:
                hinweise.append(
                    f"§1333 Abs 2 ABGB: 1 neue ersatzfähige Versandkosten-Position ({ersatzfaehiger_betrag_cent / 100:.2f} EUR) "
                    "für diesen einen Brief - unabhängig von der Anzahl gebündelter Entgeltforderungen."
                )
            else:
                hinweise.append(
                    "§1333 Abs 2 ABGB: keine neue Versandkosten-Position - alle fälligen Entgeltforderungen "
                    "wurden bereits einmalig belastet."
                )
    else:
        hinweise.append("§458 UGB gilt nur bei beiderseits unternehmensbezogenem Geschäft mit Vertragsdatum ab 16.03.2013 - keine Mahngebühr angesetzt.")

    gebuehr_cent = sum(s.betrag_cent for s in gebuehr_segmente) or None
    gebuehr_rechtsgrundlage = gebuehr_segmente[0].rechtsgrundlage if gebuehr_segmente else None

    zinsbasis_label = "UNBERECHENBAR" if aktuell_unberechenbar else aktuelle_basis

    return MahnkostenVorschau(
        vertrag_id=vertrag_id, stufe=stufe, hauptforderung_cent=hauptforderung_cent,
        zinsbasis=zinsbasis_label, zinssatz_prozent=zinssatz_prozent,
        zins_von=zins_von, zins_bis=zins_bis, neue_zinsen_cent=neue_zinsen_gesamt,
        bereits_gebuchte_zinsen_cent=bereits_gebuchte_relevant_cent,
        neue_zinsen_delta_cent=neue_zinsen_delta_gesamt,
        neue_zinsen_delta_je_op_position=neue_zinsen_delta_je_op,
        zins_segmente=tuple(alle_segmente), zins_teilweise_ungeklaert=zins_teilweise_ungeklaert,
        gebuehr_segmente=tuple(gebuehr_segmente), gebuehr_cent=gebuehr_cent, gebuehr_rechtsgrundlage=gebuehr_rechtsgrundlage,
        # NUR die echten Miet-/BK-Forderungen (siehe Funktions-Docstring/
        # `forderungen_kern` oben) - eine bereits gebuchte, noch offene
        # Mahnkosten-Zeile ist keine vom Mahnlauf verfolgte "Forderung"
        # und würde den daraus abgeleiteten `mahnlauf_schluessel`
        # (`kosten_service.py::_mahnlauf_schluessel`) instabil machen,
        # sobald diese Zeile später bezahlt wird und aus
        # `offene_forderungen()` verschwindet.
        forderung_op_position_ids=tuple(f.op_position_id for f in forderungen_kern),
        ausgeschlossene_forderungen_hinweis=tuple(ausgeschlossen), hinweise=tuple(hinweise),
        versandkosten_anbieteraufwand_cent=versandkosten_anbieteraufwand_cent,
        bereits_offene_mahnkosten_cent=bereits_offene_mahnkosten_cent,
    )
