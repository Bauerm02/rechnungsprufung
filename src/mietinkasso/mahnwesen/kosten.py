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

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from mietinkasso.infrastructure.db.tables import OPPositionTable, OenbBasiszinssatzTable, ZinsprofilTable
from mietinkasso.op.service import OffeneForderung

_STICHTAG_UGB_456 = date(2013, 3, 16)
_GESETZLICHER_ZINSSATZ_PROZENT = Decimal("4.000")
_UGB_AUFSCHLAG_PROZENTPUNKTE = Decimal("9.200")
_TAGE_IM_JAHR = Decimal(365)


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
    """Reproduziert EXAKT dieselbe FIFO-Zuordnung wie
    `OPService.offene_forderungen` (ältere Forderungen werden zuerst
    reduziert), aber mit dem tatsächlichen BUCHUNGSDATUM jeder
    Reduktion, um den Reststand der Zielforderung ÜBER DIE ZEIT (nicht
    nur den Endstand) zu rekonstruieren - Grundlage für eine taggenaue
    Verzinsung, die eine datierte Teilzahlung korrekt ab ihrem
    tatsächlichen Datum berücksichtigt statt den Ausgangsbetrag über die
    gesamte Periode zu verzinsen.

    Liefert `None`, wenn die Zielforderung unbekannte/keine Fälligkeit
    hat (z. B. eine ungegliederte GESAMTSALDO-Eröffnung) - eine solche
    Forderung wird NIE fiktiv ab einem erfundenen Datum verzinst."""

    from mietinkasso.domain.enums import OPTyp

    positive_typen = {OPTyp.EROEFFNUNG.value, OPTyp.SOLL.value, OPTyp.RUECKLASTSCHRIFT.value}
    negative_typen = {OPTyp.GUTSCHRIFT.value, OPTyp.ZAHLUNG.value}

    forderungs_rows = [p for p in alle_positionen if p.typ in positive_typen and p.betrag_cent > 0]
    forderungs_rows.sort(key=lambda p: (p.faelligkeit or p.belegdatum, p.belegdatum, p.id))

    ziel = next((p for p in forderungs_rows if p.id == ziel_op_position_id), None)
    if ziel is None or not ziel.faelligkeit_bekannt or ziel.faelligkeit is None:
        return None

    # Reduktionsereignisse mit Datum (Zahlung/Gutschrift, negative
    # KORREKTUR, ein Guthaben in einer eigentlich forderungsseitigen
    # Zeile) - exakt dieselben Quellen wie im Minderungs-Pool von
    # `offene_forderungen`, hier aber mit `buchungsdatum` statt nur als
    # aufsummierter Gesamtpool.
    ereignisse: list[tuple[date, int, int]] = []  # (datum, betrag, id) - id für deterministische Sortierung
    for p in alle_positionen:
        if p.typ in negative_typen and p.betrag_cent > 0:
            ereignisse.append((p.buchungsdatum, p.betrag_cent, p.id))
        elif p.typ in positive_typen and p.betrag_cent < 0:
            ereignisse.append((p.buchungsdatum, -p.betrag_cent, p.id))
        elif p.typ == OPTyp.KORREKTUR.value and p.betrag_cent < 0:
            ereignisse.append((p.buchungsdatum, -p.betrag_cent, p.id))
    ereignisse.sort(key=lambda e: (e[0], e[2]))

    verbleibend = {p.id: p.betrag_cent for p in forderungs_rows}
    aenderungen: list[tuple[date, int]] = []  # (datum, neuer_rest der Zielforderung)

    for ereignis_datum, betrag, _eid in ereignisse:
        rest_zu_verteilen = betrag
        for p in forderungs_rows:
            if rest_zu_verteilen <= 0:
                break
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
    aktuelles_datum = ziel.faelligkeit
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
    zins_segmente: tuple[ZinsSegment, ...]
    zins_teilweise_ungeklaert: bool  # mind. ein Segment hat satz_prozent=None
    gebuehr_segmente: tuple[GebuehrSegment, ...]
    gebuehr_cent: int | None  # Summe von gebuehr_segmente, None wenn leer
    gebuehr_rechtsgrundlage: str | None  # Rechtsgrundlage des ERSTEN Segments (informativ)
    forderung_op_position_ids: tuple[int, ...]
    ausgeschlossene_forderungen_hinweis: tuple[str, ...] = field(default_factory=tuple)
    hinweise: tuple[str, ...] = field(default_factory=tuple)

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
    wurden."""

    hauptforderung_cent = sum(f.rest_cent for f in forderungen)

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

    ausgeschlossen: list[str] = []
    alle_segmente: list[ZinsSegment] = []

    for forderung in forderungen:
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
    bereits_gebuchte_relevant_cent = 0
    for op_id in betroffene_ops:
        zinsen_dieser_op = sum(s.zinsen_cent for s in alle_segmente if s.op_position_id == op_id)
        bereits_op = bereits_gebuchte_zinsen_je_op_position.get(op_id, 0)
        bereits_gebuchte_relevant_cent += min(zinsen_dieser_op, bereits_op)
        neue_zinsen_delta_gesamt += max(zinsen_dieser_op - bereits_op, 0)

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
    if aktuell_unberechenbar:
        hinweise.append("§458 UGB: keine Mahngebühr, solange die Zinsprofil-Historie unberechenbar ist.")
    elif not ugb_scope:
        hinweise.append("§458 UGB gilt nur bei beiderseits unternehmensbezogenem Geschäft mit Vertragsdatum ab 16.03.2013 - keine Mahngebühr angesetzt.")
    elif aktuelles_profil is None or aktuelles_profil.status != "GEPRUEFT" or aktuelles_profil.mahngebuehr_kostenbasis_cent is None:
        hinweise.append("Mahngebühr: Klärung erforderlich (kein geprüftes Zinsprofil mit belegter §458-Kostenbasis hinterlegt).")
    else:
        rechtsgrundlage = f"§458 UGB - geprüfte Kostenbasis ({aktuelles_profil.mahngebuehr_kostenbasis_beleg or 'ohne Belegangabe'})"
        fuer_gebuehr_qualifiziert = [
            schluessel for schluessel in _faellige_entgeltforderungs_schluessel(forderungen, heute)
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

    gebuehr_cent = sum(s.betrag_cent for s in gebuehr_segmente) or None
    gebuehr_rechtsgrundlage = gebuehr_segmente[0].rechtsgrundlage if gebuehr_segmente else None

    zinsbasis_label = "UNBERECHENBAR" if aktuell_unberechenbar else aktuelle_basis

    return MahnkostenVorschau(
        vertrag_id=vertrag_id, stufe=stufe, hauptforderung_cent=hauptforderung_cent,
        zinsbasis=zinsbasis_label, zinssatz_prozent=zinssatz_prozent,
        zins_von=zins_von, zins_bis=zins_bis, neue_zinsen_cent=neue_zinsen_gesamt,
        bereits_gebuchte_zinsen_cent=bereits_gebuchte_relevant_cent,
        neue_zinsen_delta_cent=neue_zinsen_delta_gesamt,
        zins_segmente=tuple(alle_segmente), zins_teilweise_ungeklaert=zins_teilweise_ungeklaert,
        gebuehr_segmente=tuple(gebuehr_segmente), gebuehr_cent=gebuehr_cent, gebuehr_rechtsgrundlage=gebuehr_rechtsgrundlage,
        forderung_op_position_ids=tuple(f.op_position_id for f in forderungen),
        ausgeschlossene_forderungen_hinweis=tuple(ausgeschlossen), hinweise=tuple(hinweise),
    )
