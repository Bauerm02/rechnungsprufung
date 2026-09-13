"""Deterministische, nachvollziehbare Mahngebühren-/Verzugszinsenvorschau
je Mahnlauf (Auftrag Markus, 13.09.2026: "pro Mahnlauf Mahngebühren, und
die Zinsen dazu, soviel wie gesetzlich erlaubt ist").

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
  bezogenem Geschäft (`ist_b2b`), einem Vertragsdatum ab 16.03.2013 UND
  einem zu vertretenden Zahlungsverzug. Ohne erfassten Basiszinssatz für
  das benötigte Halbjahr (`OenbBasiszinssatzTable`) bleibt die B2B-
  Berechnung explizit "Basis ungeklärt" blockiert statt einen alten Wert
  stillschweigend fortzuschreiben.
- §458 UGB: die Mahnspesen-Pauschale (hier: die geprüfte Kostenbasis aus
  `ZinsprofilTable`) wird NUR EINMAL je zugrunde liegendem Mahnlauf
  (Vertrag+Stufe) angesetzt, NIE je einzelner OP-Zeile/Mietkomponente -
  siehe `MahnkostenBuchungTable`.

Diese Vorschau bucht NICHTS - siehe `MahnkostenService.buche_bei_versand`
in `service.py` für die einzige, an einen bestätigten Versandnachweis
gekoppelte Buchungsstelle."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from mietinkasso.infrastructure.db.tables import OPPositionTable, OenbBasiszinssatzTable, ZinsprofilTable
from mietinkasso.op.service import OffeneForderung

_STICHTAG_UGB_456 = date(2013, 3, 16)
_GESETZLICHER_ZINSSATZ_PROZENT = Decimal("4.000")
_UGB_AUFSCHLAG_PROZENTPUNKTE = Decimal("9.200")
_TAGE_IM_JAHR = Decimal(365)


@dataclass(frozen=True)
class Zinsentscheidung:
    satz_prozent: Decimal | None  # None nur bei status != "OK"
    basis: str  # "GESETZLICH_ABGB" | "VEREINBART_GEPRUEFT" | "UGB_B2B_BASISZINSSATZ"
    status: str  # "OK" | "UNBEKANNTE_BASIS"
    hinweis: str


def bestimme_zinssatz(
    *, zinsprofil: ZinsprofilTable | None, heute: date, basiszinssatz_lookup,
) -> Zinsentscheidung:
    """`basiszinssatz_lookup(heute) -> OenbBasiszinssatzTable | None` -
    liefert den für `heute` geltenden erfassten Basiszinssatz, oder
    `None`, wenn keiner erfasst ist (siehe Moduldoc: nie den letzten
    bekannten Wert stillschweigend fortschreiben)."""

    if zinsprofil is not None and zinsprofil.status == "GEPRUEFT" and zinsprofil.vereinbarung_geprueft and zinsprofil.vereinbarter_zinssatz_prozent is not None:
        return Zinsentscheidung(
            satz_prozent=zinsprofil.vereinbarter_zinssatz_prozent, basis="VEREINBART_GEPRUEFT", status="OK",
            hinweis=f"Vereinbarter, geprüfter Zinssatz laut Profil ({zinsprofil.vereinbarung_beleg or 'ohne Belegangabe'}).",
        )

    if (
        zinsprofil is not None and zinsprofil.status == "GEPRUEFT" and zinsprofil.ist_b2b
        and zinsprofil.vertragsdatum is not None and zinsprofil.vertragsdatum >= _STICHTAG_UGB_456
    ):
        basiszins = basiszinssatz_lookup(heute)
        if basiszins is None:
            return Zinsentscheidung(
                satz_prozent=None, basis="UGB_B2B_BASISZINSSATZ", status="UNBEKANNTE_BASIS",
                hinweis=f"Kein OeNB-Basiszinssatz für {heute.isoformat()} erfasst - §456-UGB-Zinsen bleiben blockiert, bis nachgetragen.",
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


def berechne_verzugszinsen_cent(perioden: list[BalancePeriode], satz_prozent: Decimal) -> int:
    """Einfache (NIE zusammengesetzte) Zinsen: je Periode
    `rest_cent * satz_prozent/100 * tage/365`, taggenau aufsummiert und
    erst am Ende auf ganze Cent gerundet - keine Zinseszinsen, da jede
    Periode ausschließlich auf dem tatsächlichen HAUPTFORDERUNGS-Rest
    rechnet, nie auf bereits berechneten Zinsen."""

    gesamt = Decimal(0)
    satz = satz_prozent / Decimal(100)
    for periode in perioden:
        tage = (periode.bis - periode.von).days
        if tage <= 0:
            continue
        gesamt += Decimal(periode.rest_cent) * satz * Decimal(tage) / _TAGE_IM_JAHR
    return int(gesamt.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class ForderungsKosten:
    op_position_id: int
    zinsen_cent: int
    perioden: tuple[BalancePeriode, ...]


@dataclass(frozen=True)
class MahnkostenVorschau:
    vertrag_id: str
    stufe: int
    hauptforderung_cent: int
    zinsbasis: str
    zinssatz_prozent: Decimal | None
    zins_von: date | None
    zins_bis: date | None
    neue_zinsen_cent: int
    bereits_gebuchte_zinsen_cent: int
    gebuehr_cent: int | None
    gebuehr_rechtsgrundlage: str | None
    forderung_op_position_ids: tuple[int, ...]
    ausgeschlossene_forderungen_hinweis: tuple[str, ...] = field(default_factory=tuple)
    hinweise: tuple[str, ...] = field(default_factory=tuple)

    @property
    def zusaetzlicher_betrag_cent(self) -> int:
        """Der NEU anzusetzende Betrag (Zinsen abzüglich bereits für
        denselben Zeitraum gebuchter Zinsen, plus eine neue Gebühr, falls
        noch keine für diesen Mahnlauf gebucht wurde) - siehe Moduldoc:
        bereits berechnete Zinstage werden nie doppelt angesetzt."""

        zusatz = max(self.neue_zinsen_cent - self.bereits_gebuchte_zinsen_cent, 0)
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
    zinsprofil: ZinsprofilTable | None,
    basiszinssatz_lookup,
    bereits_gebuchte_zinsen_cent: int,
    gebuehr_bereits_gebucht: bool,
) -> MahnkostenVorschau:
    """Aggregiert ALLE offenen Forderungen dieses Vertrags zu EINER
    Mahnlauf-Kostenvorschau (Hauptforderung, Zinsen, Gebühr) - NIE eine
    Gebühr/einen Zinsbetrag je einzelner OP-Zeile/Mietkomponente (siehe
    Moduldoc). Forderungen ohne bekannte Fälligkeit (z. B. eine
    ungegliederte GESAMTSALDO-Eröffnung) fließen in die Hauptforderung
    ein, werden aber NICHT verzinst - das wird als Hinweis ausgewiesen,
    blockiert aber nicht die Hauptforderung selbst."""

    hauptforderung_cent = sum(f.rest_cent for f in forderungen)
    entscheidung = bestimme_zinssatz(zinsprofil=zinsprofil, heute=heute, basiszinssatz_lookup=basiszinssatz_lookup)

    hinweise: list[str] = [entscheidung.hinweis]
    ausgeschlossen: list[str] = []
    kosten_je_forderung: list[ForderungsKosten] = []

    if entscheidung.status == "OK" and entscheidung.satz_prozent is not None:
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
            zinsen = berechne_verzugszinsen_cent(perioden, entscheidung.satz_prozent)
            kosten_je_forderung.append(ForderungsKosten(forderung.op_position_id, zinsen, tuple(perioden)))

    neue_zinsen_gesamt = sum(k.zinsen_cent for k in kosten_je_forderung)
    alle_perioden = [p for k in kosten_je_forderung for p in k.perioden]
    zins_von = min((p.von for p in alle_perioden), default=None)
    zins_bis = max((p.bis for p in alle_perioden), default=None)

    gebuehr_cent = None
    rechtsgrundlage = None
    if not gebuehr_bereits_gebucht and zinsprofil is not None and zinsprofil.status == "GEPRUEFT" and zinsprofil.mahngebuehr_kostenbasis_cent is not None:
        gebuehr_cent = zinsprofil.mahngebuehr_kostenbasis_cent
        rechtsgrundlage = f"§458 UGB - geprüfte Kostenbasis ({zinsprofil.mahngebuehr_kostenbasis_beleg or 'ohne Belegangabe'})"
        hinweise.append("Mahngebühr wird nur einmal für diesen Mahnlauf angesetzt (§458 UGB, einmal je Forderung/Mahnlauf).")
    elif not gebuehr_bereits_gebucht and (zinsprofil is None or zinsprofil.status != "GEPRUEFT"):
        hinweise.append("Mahngebühr: Klärung erforderlich (kein geprüftes Zinsprofil mit belegter Kostenbasis hinterlegt).")

    return MahnkostenVorschau(
        vertrag_id=vertrag_id, stufe=stufe, hauptforderung_cent=hauptforderung_cent,
        zinsbasis=entscheidung.basis, zinssatz_prozent=entscheidung.satz_prozent,
        zins_von=zins_von, zins_bis=zins_bis, neue_zinsen_cent=neue_zinsen_gesamt,
        bereits_gebuchte_zinsen_cent=bereits_gebuchte_zinsen_cent,
        gebuehr_cent=gebuehr_cent, gebuehr_rechtsgrundlage=rechtsgrundlage,
        forderung_op_position_ids=tuple(f.op_position_id for f in forderungen),
        ausgeschlossene_forderungen_hinweis=tuple(ausgeschlossen), hinweise=tuple(hinweise),
    )
