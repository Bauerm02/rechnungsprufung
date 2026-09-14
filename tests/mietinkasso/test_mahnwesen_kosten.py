"""Tests für die Mahnkosten-Vorschau/-Buchung (Auftrag Markus 13.09.2026:
"pro Mahnlauf Mahngebühren, und die Zinsen dazu, soviel wie gesetzlich
erlaubt ist") - siehe `mahnwesen/kosten.py` für die fachlichen
Leitplanken (ABGB §§1000/1333, KSchG §6, §§456/458 UGB).

Deckt genau die vom Auftraggeber/Codex explizit geforderten Grenzfälle
ab: zwei Komponenten derselben Miete (eine kombinierte Gebühr/Zins,
nicht je Komponente), zwei Läufe am selben Tag (Idempotenz),
Teilzahlung, Halbjahreswechsel ohne erfassten Basiszinssatz,
Privat/B2B, Altvertrag vor dem UGB-Stichtag, unbekannte Basis,
Retry/Stufe 2 (keine doppelte Verzinsung bereits fakturierter Tage) und
eine unbekannte/ungegliederte Eröffnungsstruktur (nie fiktiv
verzinst, aber Teil der Hauptforderung)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from mietinkasso.auth.service import AuthContext, require_gesellschaft_access
from mietinkasso.domain.enums import OPTyp, Rolle
from mietinkasso.domain.exceptions import CrossTenantError
from mietinkasso.infrastructure.db.tables import MahnkostenGebuehrTable
from mietinkasso.mahnwesen.kosten import bestimme_zinssatz, snapshot_aus_json, snapshot_zu_json
from mietinkasso.mahnwesen.kosten_repository import MahnkostenRepository
from mietinkasso.mahnwesen.kosten_service import MahnkostenService


@pytest.fixture
def kosten_repo(session_factory) -> MahnkostenRepository:
    return MahnkostenRepository(session_factory)


@pytest.fixture
def kosten_service(kosten_repo, op_service, stammdaten_repo) -> MahnkostenService:
    return MahnkostenService(kosten_repo, op_service, stammdaten_repo)


def _profil_geprueft(kosten_repo: MahnkostenRepository, *, vertrag_id: str, **kwargs) -> None:
    profil = kosten_repo.zinsprofil_anlegen(vertrag_id=vertrag_id, erstellt_von="test", **kwargs)
    kosten_repo.zinsprofil_freigeben(profil.id, freigegeben_von="test")


def _segment_zinsen_cent(rest_cent: int, satz_prozent: Decimal, tage: int) -> int:
    """Dupliziert bewusst NUR die Rundungsformel aus
    `kosten.py::_zinsen_fuer_segment_cent` (statt sie als privates Detail
    zu importieren), damit Tests unabhängig von internen Funktionsnamen
    bleiben: je Segment einfache (nicht zusammengesetzte) Zinsen,
    kaufmännisch auf den Cent gerundet."""

    from decimal import ROUND_HALF_UP
    betrag = Decimal(rest_cent) * (satz_prozent / Decimal(100)) * Decimal(tage) / Decimal(365)
    return int(betrag.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# -- Zwei Komponenten derselben Miete: EINE kombinierte Vorschau ------------


def test_zwei_komponenten_derselben_miete_ergeben_eine_kombinierte_vorschau(
    op_service, kosten_service, admin_ctx, basis_vertrag,
):
    vertrag, konto = basis_vertrag
    # Zwei getrennte OP-Zeilen derselben Monatsvorschreibung (HMZ + BK),
    # exakt der vom Reviewpoint benannte Fall - darf NICHT zu zwei
    # Gebühren/zwei Zinsbeträgen führen.
    op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=60_000,
        belegdatum=date(2026, 1, 1), buchungsdatum=date(2026, 1, 1),
        faelligkeit=date(2026, 1, 5), beleg_referenz="HMZ Jänner",
    )
    op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=15_000,
        belegdatum=date(2026, 1, 1), buchungsdatum=date(2026, 1, 1),
        faelligkeit=date(2026, 1, 5), beleg_referenz="Betriebskosten Jänner",
    )

    vorschau = kosten_service.vorschau(vertrag_id=vertrag.id, stufe=1, heute=date(2026, 3, 1))

    assert vorschau is not None
    assert vorschau.hauptforderung_cent == 75_000
    # Eine einzige Zinssumme über beide Komponenten - nicht separat je Zeile.
    assert vorschau.neue_zinsen_cent > 0
    assert len(vorschau.forderung_op_position_ids) == 2

    gebucht = kosten_service.buche_bei_versand(
        ctx=admin_ctx, vertrag_id=vertrag.id, stufe=1, heute=date(2026, 3, 1),
        versandnachweis_referenz="mahnung:test-1", akteur="test",
    )
    assert gebucht is not None
    # Genau EINE Zinsen-OP-Position für den gesamten Mahnlauf, keine
    # Gebühr ohne geprüftes Zinsprofil.
    assert gebucht.zinsen_op_position_id is not None
    assert gebucht.gebuehr_op_position_id is None
    saldo_positionen = op_service.berechne_saldo(konto.id, stichtag=date(2026, 3, 1)).positionen
    zinsen_zeilen = [p for p in saldo_positionen if p.aenderungsgrund == "Mahnkosten - Verzugszinsen"]
    assert len(zinsen_zeilen) == 1


# -- Zwei Läufe am selben Tag: idempotent, keine Doppelbuchung -------------


def test_zwei_laeufe_am_selben_tag_buchen_nicht_doppelt(op_service, kosten_service, admin_ctx, basis_vertrag):
    vertrag, konto = basis_vertrag
    op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=50_000,
        belegdatum=date(2026, 1, 1), buchungsdatum=date(2026, 1, 1),
        faelligkeit=date(2026, 1, 5), beleg_referenz="HMZ Jänner",
    )

    erster = kosten_service.buche_bei_versand(
        ctx=admin_ctx, vertrag_id=vertrag.id, stufe=1, heute=date(2026, 3, 1),
        versandnachweis_referenz="mahnung:lauf-1", akteur="test",
    )
    assert erster is not None
    zinsen_erster_lauf = erster.zinsen_cent
    assert zinsen_erster_lauf > 0

    zweiter = kosten_service.buche_bei_versand(
        ctx=admin_ctx, vertrag_id=vertrag.id, stufe=1, heute=date(2026, 3, 1),
        versandnachweis_referenz="mahnung:lauf-2-selber-tag", akteur="test",
    )
    assert zweiter is None  # nichts Neues zu buchen - Delta ist 0

    saldo_positionen = op_service.berechne_saldo(konto.id, stichtag=date(2026, 3, 1)).positionen
    zinsen_zeilen = [p for p in saldo_positionen if p.aenderungsgrund == "Mahnkosten - Verzugszinsen"]
    assert len(zinsen_zeilen) == 1
    assert sum(p.betrag_cent for p in zinsen_zeilen) == zinsen_erster_lauf


# -- Retry/Stufe 2: nie bereits fakturierte Zinstage erneut ansetzen -------


def test_stufe_zwei_rechnet_nur_das_delta_seit_stufe_eins_ab(op_service, kosten_service, admin_ctx, basis_vertrag):
    vertrag, konto = basis_vertrag
    op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=50_000,
        belegdatum=date(2026, 1, 1), buchungsdatum=date(2026, 1, 1),
        faelligkeit=date(2026, 1, 5), beleg_referenz="HMZ Jänner",
    )

    stufe1 = kosten_service.buche_bei_versand(
        ctx=admin_ctx, vertrag_id=vertrag.id, stufe=1, heute=date(2026, 2, 1),
        versandnachweis_referenz="mahnung:stufe1", akteur="test",
    )
    assert stufe1 is not None
    zinsen_stufe1 = stufe1.zinsen_cent
    assert zinsen_stufe1 > 0

    # Stufe 2 einen Monat später - darf NUR die zusätzlichen Tage seit
    # Stufe 1 abrechnen, nicht die gesamte Periode seit Fälligkeit erneut.
    stufe2 = kosten_service.buche_bei_versand(
        ctx=admin_ctx, vertrag_id=vertrag.id, stufe=2, heute=date(2026, 3, 3),
        versandnachweis_referenz="mahnung:stufe2", akteur="test",
    )
    assert stufe2 is not None
    assert stufe2.zinsen_cent > 0

    vollzins_seit_faelligkeit = op_service  # nur zur Lesbarkeit, keine Nutzung
    vorschau_gesamt_stufe2_zeitpunkt = kosten_service.vorschau(vertrag_id=vertrag.id, stufe=2, heute=date(2026, 3, 3))
    assert vorschau_gesamt_stufe2_zeitpunkt is not None
    # Der bei Stufe 2 NEU gebuchte Betrag ist kleiner als die theoretische
    # Gesamtzinssumme seit Fälligkeit - sonst wären die Stufe-1-Tage
    # doppelt verrechnet worden.
    assert stufe2.zinsen_cent < vorschau_gesamt_stufe2_zeitpunkt.neue_zinsen_cent
    assert zinsen_stufe1 + stufe2.zinsen_cent == vorschau_gesamt_stufe2_zeitpunkt.neue_zinsen_cent

    saldo_positionen = op_service.berechne_saldo(konto.id, stichtag=date(2026, 3, 3)).positionen
    zinsen_zeilen = [p for p in saldo_positionen if p.aenderungsgrund == "Mahnkosten - Verzugszinsen"]
    assert len(zinsen_zeilen) == 2


def test_bereits_gebuchte_zinsen_je_op_position_zaehlt_stufe1_delta_nicht_doppelt(
    op_service, kosten_repo, kosten_service, admin_ctx, basis_vertrag,
):
    """Unabhängige Rückprüfung Codex 14.09.2026, echter Bug: das an
    Stufe 1 gebuchte Zinsdelta wurde bei Stufe 2 ein ZWEITES Mal in
    `bereits_gebuchte_zinsen_je_op_position` gezählt, weil diese Methode
    ursprünglich `zins_segmente_json` (die VOLLE, ab der Fälligkeit neu
    berechnete Periode je Buchung) statt des tatsächlich an jedem Tag
    NEU gebuchten Deltas summierte. Exakter, vom unabhängigen Prüfer
    gemeldeter Ablauf: 100.000 Cent Hauptforderung, fällig 01.01.2026,
    Stufe 1 am 20.01. (19 Tage, 4 % gesetzlich -> 208 Cent), Stufe 2 am
    10.02. (40 Tage seit Fälligkeit -> 438 Cent gesamt, davon 230 Cent
    neu). Insgesamt tatsächlich gebucht: 208 + 230 = 438 Cent - NICHT
    646 (208 fälschlich doppelt gezählt)."""

    vertrag, konto = basis_vertrag
    op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=100_000,
        belegdatum=date(2026, 1, 1), buchungsdatum=date(2026, 1, 1),
        faelligkeit=date(2026, 1, 1), beleg_referenz="HMZ Jänner",
    )

    stufe1 = kosten_service.buche_bei_versand(
        ctx=admin_ctx, vertrag_id=vertrag.id, stufe=1, heute=date(2026, 1, 20),
        versandnachweis_referenz="mahnung:stufe1", akteur="test",
    )
    assert stufe1 is not None
    assert stufe1.zinsen_cent == 208

    stufe2 = kosten_service.buche_bei_versand(
        ctx=admin_ctx, vertrag_id=vertrag.id, stufe=2, heute=date(2026, 2, 10),
        versandnachweis_referenz="mahnung:stufe2", akteur="test",
    )
    assert stufe2 is not None
    assert stufe2.zinsen_cent == 230

    op_ids = [f.op_position_id for f in op_service.offene_forderungen(konto.id, heute=date(2026, 2, 10))]
    hmz_op_id = min(op_ids)  # die Hauptforderung selbst (kleinste Id, vor den beiden Zinsbuchungen)

    bereits_je_op = kosten_repo.bereits_gebuchte_zinsen_je_op_position(vertrag_id=vertrag.id)
    assert bereits_je_op[hmz_op_id] == 438  # NICHT 646 (208 nicht doppelt gezählt)

    tatsaechlich_gebucht_gesamt = stufe1.zinsen_cent + stufe2.zinsen_cent
    assert bereits_je_op[hmz_op_id] == tatsaechlich_gebucht_gesamt


def test_inkonsistenter_zinsledger_wird_nicht_als_null_bereits_gebucht_gewertet(
    op_service, kosten_repo, kosten_service, admin_ctx, basis_vertrag,
):
    """Unabhängige Rückprüfung Codex 14.09.2026: eine (z. B. aus der Zeit
    vor Einführung von `zinsen_delta_je_op_json` stammende) Buchung mit
    tatsächlich gebuchten Zinsen, aber fehlendem/inkonsistentem Delta-
    JSON, darf NIEMALS still als "0 bereits gebucht" behandelt werden -
    das würde bei einer künftigen Stufe zu doppelt gebuchten Zinsen
    führen. Stattdessen bleibt die Vorschau für den GESAMTEN Vertrag
    explizit "unberechenbar" (keine Zinsen/Gebühr), die Hauptforderung
    selbst bleibt unblockiert."""

    vertrag, konto = basis_vertrag
    op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=50_000,
        belegdatum=date(2026, 1, 1), buchungsdatum=date(2026, 1, 1),
        faelligkeit=date(2026, 1, 5), beleg_referenz="HMZ Jänner",
    )
    forderung = op_service.offene_forderungen(konto.id, heute=date(2026, 2, 1))[0]

    # Simuliert eine Alt-Buchung: zinsen_cent > 0, aber KEIN (bzw. leeres)
    # zinsen_delta_je_op_json - genau der vom unabhängigen Prüfer
    # beschriebene inkonsistente Zustand.
    kosten_repo.buchung_anlegen(
        vertrag_id=vertrag.id, stufe=1, mahnlauf_schluessel="ALT-BUCHUNG",
        forderung_op_position_ids=[forderung.op_position_id], hauptforderung_cent=50_000,
        zinsbasis="GESETZLICH_ABGB", zinssatz_prozent=Decimal("4.000"),
        zins_von=date(2026, 1, 5), zins_bis=date(2026, 1, 20), zinsen_cent=208,
        gebuehr_cent=None, rechtsgrundlage_gebuehr=None, versandnachweis_referenz="mahnung:alt",
        zinsen_op_position_id=None, gebuehr_op_position_id=None, erstellt_von="test",
        # zinsen_delta_je_op_json bewusst NICHT gesetzt -> Default "{}".
    )

    vorschau = kosten_service.vorschau(vertrag_id=vertrag.id, stufe=2, heute=date(2026, 3, 1))

    assert vorschau is not None
    assert vorschau.zinsbasis == "UNBERECHENBAR"
    assert vorschau.neue_zinsen_cent == 0
    assert vorschau.gebuehr_cent is None
    assert vorschau.hauptforderung_cent == 50_000  # Hauptforderung bleibt unblockiert
    assert any("Zinsledger widersprüchlich" in h for h in vorschau.hinweise)

    # Direkt am Repository nachgewiesen: die Methode wirft, statt 0 zu
    # unterstellen.
    from mietinkasso.mahnwesen.kosten_repository import ZinsledgerInkonsistentError
    with pytest.raises(ZinsledgerInkonsistentError):
        kosten_repo.bereits_gebuchte_zinsen_je_op_position(vertrag_id=vertrag.id)


def test_zinsdelta_einer_neuen_forderung_wird_nicht_durch_eine_alte_abgeloeste_geschluckt(
    op_service, kosten_service, admin_ctx, basis_vertrag,
):
    """Unabhängige Rückprüfung Codex 14.09.2026: das Delta darf NICHT
    vertragsweit als eine einzige Blanko-Summe gebildet werden. Eine
    ALTE Forderung (hier: HMZ Jänner) wird vollständig abgelöst, NACHDEM
    für sie bereits Zinsen gebucht wurden; danach entsteht eine GENUIN
    NEUE, andere Forderung (HMZ Februar). Die neuen Zinsen dieser neuen
    Forderung dürfen NICHT durch die (deutlich höhere) historische
    Zinssumme der längst abgelösten alten Forderung aufgezehrt werden -
    das wäre mit einer vertragsweiten Blanko-Subtraktion der Fall
    gewesen (`max(neue_zinsen_cent - bereits_gebuchte_zinsen_cent, 0)`
    hätte 0 ergeben, obwohl die neue Forderung echte, noch nie gebuchte
    Zinsen trägt)."""

    vertrag, konto = basis_vertrag
    op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=50_000,
        belegdatum=date(2026, 1, 1), buchungsdatum=date(2026, 1, 1),
        faelligkeit=date(2026, 1, 5), beleg_referenz="HMZ Jänner",
    )
    alte_forderung = kosten_service.buche_bei_versand(
        ctx=admin_ctx, vertrag_id=vertrag.id, stufe=1, heute=date(2026, 2, 1),
        versandnachweis_referenz="mahnung:alte-forderung", akteur="test",
    )
    assert alte_forderung is not None
    zinsen_alte_forderung = alte_forderung.zinsen_cent
    assert zinsen_alte_forderung > 0

    # Die alte Forderung wird VOLLSTÄNDIG abgelöst - sie taucht ab jetzt
    # in keiner `offene_forderungen`-Auswertung mehr auf.
    op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.ZAHLUNG, betrag_cent=50_000,
        belegdatum=date(2026, 2, 10), buchungsdatum=date(2026, 2, 10),
        faelligkeit=None, beleg_referenz="Vollzahlung HMZ Jänner",
    )

    # Eine GENUIN NEUE, deutlich kleinere Forderung entsteht - ihre
    # eigenen, bisher nie gebuchten Zinsen sind viel kleiner als die
    # historische Zinssumme der alten Forderung.
    op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=3_000,
        belegdatum=date(2026, 2, 1), buchungsdatum=date(2026, 2, 1),
        faelligkeit=date(2026, 2, 15), beleg_referenz="HMZ Februar",
    )

    zinsen_neue_forderung = _segment_zinsen_cent(3_000, Decimal("4.000"), (date(2026, 3, 1) - date(2026, 2, 15)).days)
    assert zinsen_neue_forderung > 0
    assert zinsen_alte_forderung > zinsen_neue_forderung  # das eigentliche Bug-Szenario

    vorschau = kosten_service.vorschau(vertrag_id=vertrag.id, stufe=1, heute=date(2026, 3, 1))
    assert vorschau is not None
    # Die alte HAUPTforderung selbst ist getilgt; die für sie schon
    # gebuchte, noch offene Zinsposition (`faelligkeit=None`, daher NIE
    # selbst mitverzinst) bleibt bis zu ihrer eigenen Zahlung ein
    # separater offener Posten.
    assert vorschau.hauptforderung_cent == 3_000 + zinsen_alte_forderung
    assert vorschau.neue_zinsen_cent == zinsen_neue_forderung
    # Der Kern des Fixes: das Delta ist NICHT 0, obwohl vertragsweit
    # bereits mehr Zinsen gebucht wurden, als die neue Forderung selbst
    # an Zinsen trägt.
    assert vorschau.neue_zinsen_delta_cent == zinsen_neue_forderung

    gebucht = kosten_service.buche_bei_versand(
        ctx=admin_ctx, vertrag_id=vertrag.id, stufe=1, heute=date(2026, 3, 1),
        versandnachweis_referenz="mahnung:neue-forderung", akteur="test",
    )
    assert gebucht is not None
    assert gebucht.zinsen_cent == zinsen_neue_forderung


def test_zwei_disjunkte_gruppen_selbe_stufe_selber_stichtag_buchen_getrennte_ledger(
    op_service, kosten_service, admin_ctx, basis_vertrag,
):
    """Unabhängige Abnahme auf Commit 1328f2d, neuer echter Bug:
    `uq_mahnkosten_lauf` war (vertrag_id, stufe, zins_bis) - zu grob.
    Zwei DISJUNKTE, an unterschiedliche Mahnläufe gebundene Forderungen
    desselben Vertrags/derselben Stufe können denselben `zins_bis`-
    Stichtag (`heute`) treffen. Mit der alten Eindeutigkeit wurde der
    zweite `buche_vorschau`-Aufruf als Doppelversuch für die ERSTE
    Gruppe abgelehnt (`IntegrityError`) und lieferte deren FREMDEN
    Ledger zurück - die zweite Forderung bekam nie eigene Zinsen
    gebucht, obwohl `buche_vorschau` GESENDET zurückmeldete."""

    vertrag, konto = basis_vertrag
    op_a = op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=50_000,
        belegdatum=date(2026, 1, 5), buchungsdatum=date(2026, 1, 5),
        faelligkeit=date(2026, 1, 5), beleg_referenz="HMZ Jänner",
    )
    op_b = op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=50_000,
        belegdatum=date(2026, 2, 5), buchungsdatum=date(2026, 2, 5),
        faelligkeit=date(2026, 2, 5), beleg_referenz="HMZ Februar",
    )
    heute = date(2026, 3, 1)

    vorschau_a = kosten_service.vorschau(
        vertrag_id=vertrag.id, stufe=1, heute=heute, nur_op_position_ids=frozenset({op_a.id}),
    )
    vorschau_b = kosten_service.vorschau(
        vertrag_id=vertrag.id, stufe=1, heute=heute, nur_op_position_ids=frozenset({op_b.id}),
    )
    assert vorschau_a is not None and vorschau_b is not None
    assert vorschau_a.neue_zinsen_cent > 0
    assert vorschau_b.neue_zinsen_cent > 0
    # Derselbe Stichtag für beide Gruppen - genau die Konstellation, die
    # die alte, zu grobe Eindeutigkeit fälschlich als "derselbe Vorgang"
    # behandelt hätte.
    assert vorschau_a.zins_bis == vorschau_b.zins_bis == heute

    gebucht_a = kosten_service.buche_vorschau(
        ctx=admin_ctx, vorschau=vorschau_a, heute=heute,
        versandnachweis_referenz="mahnungslauf:gruppe-a", akteur="test",
    )
    gebucht_b = kosten_service.buche_vorschau(
        ctx=admin_ctx, vorschau=vorschau_b, heute=heute,
        versandnachweis_referenz="mahnungslauf:gruppe-b", akteur="test",
    )

    assert gebucht_a is not None
    assert gebucht_b is not None
    assert gebucht_a.id != gebucht_b.id  # NIE derselbe fremde Ledger
    assert gebucht_a.zinsen_cent == vorschau_a.neue_zinsen_cent
    assert gebucht_b.zinsen_cent == vorschau_b.neue_zinsen_cent  # tatsächlich für B gebucht, nicht 0/fremd
    assert json.loads(gebucht_a.forderung_op_position_ids) == [op_a.id]
    assert json.loads(gebucht_b.forderung_op_position_ids) == [op_b.id]

    saldo_positionen = op_service.berechne_saldo(konto.id, stichtag=heute).positionen
    zinsen_zeilen = [p for p in saldo_positionen if p.aenderungsgrund == "Mahnkosten - Verzugszinsen"]
    assert len(zinsen_zeilen) == 2  # je Gruppe EINE eigene Zinsposition, nicht nur eine


def test_wiederholung_derselben_gruppe_bucht_weiterhin_nicht_doppelt(
    op_service, kosten_service, admin_ctx, basis_vertrag,
):
    """Gegenprobe zum vorigen Test: die neue, feinere Eindeutigkeit auf
    `mahnlauf_schluessel` darf eine ECHTE Wiederholung DERSELBEN Gruppe
    (identische Mitgliedermenge, DIESELBE bereits verwendete Vorschau -
    z. B. ein Retry nach einem unklaren Versandergebnis) weiterhin nicht
    doppelt buchen: der zweite Versuch kollidiert mit der Unique-
    Constraint und liefert exakt denselben, bereits gebuchten Ledger
    zurück, NIEMALS eine zweite Zeile."""

    vertrag, konto = basis_vertrag
    op_a = op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=50_000,
        belegdatum=date(2026, 1, 5), buchungsdatum=date(2026, 1, 5),
        faelligkeit=date(2026, 1, 5), beleg_referenz="HMZ Jänner",
    )
    heute = date(2026, 3, 1)
    vorschau = kosten_service.vorschau(
        vertrag_id=vertrag.id, stufe=1, heute=heute, nur_op_position_ids=frozenset({op_a.id}),
    )
    assert vorschau is not None and vorschau.neue_zinsen_cent > 0

    erster = kosten_service.buche_vorschau(
        ctx=admin_ctx, vorschau=vorschau, heute=heute,
        versandnachweis_referenz="mahnungslauf:erster-versuch", akteur="test",
    )
    assert erster is not None

    zweiter = kosten_service.buche_vorschau(
        ctx=admin_ctx, vorschau=vorschau, heute=heute,
        versandnachweis_referenz="mahnungslauf:zweiter-versuch-selbe-gruppe", akteur="test",
    )
    assert zweiter is not None
    assert zweiter.id == erster.id  # derselbe Ledger, kein zweiter/fremder

    saldo_positionen = op_service.berechne_saldo(konto.id, stichtag=heute).positionen
    zinsen_zeilen = [p for p in saldo_positionen if p.aenderungsgrund == "Mahnkosten - Verzugszinsen"]
    assert len(zinsen_zeilen) == 1  # KEINE zweite OP-Position durch den Retry

    # Eine frisch berechnete Vorschau (der normale Weg für einen
    # echten zweiten Lauf, siehe `test_zwei_laeufe_am_selben_tag_
    # buchen_nicht_doppelt`) sieht das Delta korrekt bereits als 0.
    frische_vorschau = kosten_service.vorschau(
        vertrag_id=vertrag.id, stufe=1, heute=heute, nur_op_position_ids=frozenset({op_a.id}),
    )
    assert frische_vorschau.neue_zinsen_delta_cent <= 0


def test_snapshot_json_rundtrip_erhaelt_alle_felder_verlustfrei(
    op_service, kosten_repo, kosten_service, admin_ctx, basis_vertrag,
):
    """Grundlage der Recovery (Auftrag Markus 14.09.2026, "eingefrorener
    Kosten-/Inhaltssnapshot"): eine über `snapshot_zu_json` eingefrorene
    Vorschau muss über `snapshot_aus_json` VERLUSTFREI (inkl. Segmenten,
    Gebühren, Hinweisen, Decimal-Präzision) zurückgewonnen werden - eine
    per Recovery nachgeholte Buchung darf sich in KEINEM Feld von einer
    direkt gebuchten unterscheiden."""

    vertrag, konto = basis_vertrag
    _profil_geprueft(
        kosten_repo, vertrag_id=vertrag.id, ist_b2b=True, vertragsdatum=date(2020, 1, 1),
        mahngebuehr_kostenbasis_cent=1500, mahngebuehr_kostenbasis_beleg="Portokosten-Nachweis",
    )
    kosten_repo.basiszinssatz_erfassen(
        id="2026-1", gueltig_von=date(2026, 1, 1), gueltig_bis=date(2026, 6, 30),
        basiszinssatz_prozent=Decimal("1.530"), erfasst_von="test", quelle_referenz="OeNB 01.01.2026",
    )
    op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=50_000,
        belegdatum=date(2026, 1, 1), buchungsdatum=date(2026, 1, 1),
        faelligkeit=date(2026, 1, 5), leistungsperiode="2026-01", beleg_referenz="HMZ Jänner",
    )

    vorschau = kosten_service.vorschau(vertrag_id=vertrag.id, stufe=1, heute=date(2026, 3, 1))
    assert vorschau is not None
    assert vorschau.zins_segmente and vorschau.gebuehr_segmente

    payload = snapshot_zu_json(vorschau)
    zurueckgewonnen = snapshot_aus_json(payload)

    assert zurueckgewonnen == vorschau

    # Der None-Fall (kein Kostenservice konfiguriert/nichts zu berechnen)
    # muss ebenfalls verlustfrei rundlaufen - und bleibt unterscheidbar
    # von "Spalte war NULL" (siehe `snapshot_aus_json`-Docstring).
    assert snapshot_aus_json(snapshot_zu_json(None)) is None
    assert snapshot_aus_json(None) is None


# -- Teilzahlung reduziert die Zinsbasis ab ihrem tatsächlichen Datum ------


def test_teilzahlung_reduziert_zinsbasis_ab_zahlungsdatum(op_service, kosten_service, admin_ctx, basis_vertrag):
    vertrag, konto = basis_vertrag
    op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=100_000,
        belegdatum=date(2026, 1, 1), buchungsdatum=date(2026, 1, 1),
        faelligkeit=date(2026, 1, 5), beleg_referenz="HMZ Jänner",
    )
    op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.ZAHLUNG, betrag_cent=40_000,
        belegdatum=date(2026, 1, 20), buchungsdatum=date(2026, 1, 20),
        faelligkeit=None, beleg_referenz="Teilzahlung Mieter",
    )

    vorschau_mit_teilzahlung = kosten_service.vorschau(vertrag_id=vertrag.id, stufe=1, heute=date(2026, 3, 1))
    assert vorschau_mit_teilzahlung is not None
    assert vorschau_mit_teilzahlung.hauptforderung_cent == 60_000

    # Erwartete Zinsen taggenau nachgerechnet (gesetzliche 4 % p.a., kein
    # Zinsprofil hinterlegt): 100.000 Cent vom 5.1. (Fälligkeit) bis 20.1.
    # (Zahlungsdatum, 15 Tage), danach 60.000 Cent vom 20.1. bis 1.3.
    # (40 Tage) - exakt die beiden Perioden, die
    # `balance_zeitreihe_fuer_forderung` liefern muss. Jedes Segment wird
    # EINZELN kaufmännisch gerundet und dann summiert (siehe
    # `kosten.py::_zinsen_fuer_segment_cent`).
    erwartete_zinsen = _segment_zinsen_cent(100_000, Decimal("4.000"), 15) + _segment_zinsen_cent(60_000, Decimal("4.000"), 40)
    assert vorschau_mit_teilzahlung.neue_zinsen_cent == erwartete_zinsen

    # Gegenprobe: würde die Teilzahlung NICHT berücksichtigt, wäre die
    # volle Basis (100.000 Cent) über die gesamte Periode verzinst worden -
    # das muss strikt mehr sein als der tatsächliche, korrekt reduzierte Wert.
    ohne_teilzahlung = _segment_zinsen_cent(100_000, Decimal("4.000"), 55)
    assert vorschau_mit_teilzahlung.neue_zinsen_cent < ohne_teilzahlung


# -- Halbjahreswechsel ohne erfassten Basiszinssatz: B2B blockiert ---------


def test_b2b_ohne_erfassten_basiszinssatz_fuer_das_halbjahr_bleibt_blockiert(
    op_service, kosten_repo, kosten_service, admin_ctx, basis_vertrag,
):
    vertrag, konto = basis_vertrag
    _profil_geprueft(
        kosten_repo, vertrag_id=vertrag.id, ist_b2b=True, vertragsdatum=date(2020, 1, 1),
        verzugsverantwortung_geprueft=True,
    )
    op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=50_000,
        belegdatum=date(2026, 1, 1), buchungsdatum=date(2026, 1, 1),
        faelligkeit=date(2026, 1, 5), beleg_referenz="HMZ Jänner",
    )
    # Kein OeNB-Basiszinssatz für das benötigte Halbjahr erfasst.

    vorschau = kosten_service.vorschau(vertrag_id=vertrag.id, stufe=1, heute=date(2026, 3, 1))

    assert vorschau is not None
    assert vorschau.zinsbasis == "UGB_B2B_BASISZINSSATZ"
    assert vorschau.zinssatz_prozent is None
    assert vorschau.neue_zinsen_cent == 0
    # Die Hauptforderung selbst bleibt unblockiert - nur der Zinsanteil ist
    # als klärungsbedürftig ausgewiesen (nie stillschweigend einen alten
    # Basiszinssatz fortschreiben).
    assert vorschau.hauptforderung_cent == 50_000
    assert any("Basiszinssatz" in h for h in vorschau.hinweise)

    gebucht = kosten_service.buche_bei_versand(
        ctx=admin_ctx, vertrag_id=vertrag.id, stufe=1, heute=date(2026, 3, 1),
        versandnachweis_referenz="mahnung:b2b-ohne-basis", akteur="test",
    )
    assert gebucht is None  # nichts zu buchen, solange die Basis ungeklärt ist


def test_b2b_mit_erfasstem_basiszinssatz_verwendet_ugb456(
    op_service, kosten_repo, kosten_service, admin_ctx, basis_vertrag,
):
    vertrag, konto = basis_vertrag
    _profil_geprueft(
        kosten_repo, vertrag_id=vertrag.id, ist_b2b=True, vertragsdatum=date(2020, 1, 1),
        verzugsverantwortung_geprueft=True,
    )
    kosten_repo.basiszinssatz_erfassen(
        id="2026-1", gueltig_von=date(2026, 1, 1), gueltig_bis=date(2026, 6, 30),
        basiszinssatz_prozent=Decimal("1.530"), erfasst_von="test", quelle_referenz="OeNB 01.01.2026",
    )
    op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=50_000,
        belegdatum=date(2026, 1, 1), buchungsdatum=date(2026, 1, 1),
        faelligkeit=date(2026, 1, 5), beleg_referenz="HMZ Jänner",
    )

    vorschau = kosten_service.vorschau(vertrag_id=vertrag.id, stufe=1, heute=date(2026, 3, 1))

    assert vorschau is not None
    assert vorschau.zinsbasis == "UGB_B2B_BASISZINSSATZ"
    assert vorschau.zinssatz_prozent == Decimal("10.730")  # 1,53 % + 9,2 Prozentpunkte
    assert vorschau.neue_zinsen_cent > 0


def test_b2b_ohne_belegte_verzugsverantwortung_faellt_auf_gesetzliche_zinsen_zurueck(
    op_service, kosten_repo, kosten_service, admin_ctx, basis_vertrag,
):
    """Unabhängige Rückprüfung Codex 14.09.2026: B2B + Vertragsdatum ab
    16.03.2013 + erfasster Basiszinssatz allein reichen NICHT für den
    erhöhten §456-Zinssatz - die Verantwortlichkeit für den Verzug muss
    ZUSÄTZLICH belegt geprüft sein. Ist sie das (noch) nicht (der
    Default), gelten die gesetzlichen 4 % ABGB - NICHT automatisch der
    UGB-Höchstsatz."""

    vertrag, konto = basis_vertrag
    _profil_geprueft(kosten_repo, vertrag_id=vertrag.id, ist_b2b=True, vertragsdatum=date(2020, 1, 1))
    kosten_repo.basiszinssatz_erfassen(
        id="2026-1", gueltig_von=date(2026, 1, 1), gueltig_bis=date(2026, 6, 30),
        basiszinssatz_prozent=Decimal("1.530"), erfasst_von="test", quelle_referenz="OeNB 01.01.2026",
    )
    op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=50_000,
        belegdatum=date(2026, 1, 1), buchungsdatum=date(2026, 1, 1),
        faelligkeit=date(2026, 1, 5), beleg_referenz="HMZ Jänner",
    )

    vorschau = kosten_service.vorschau(vertrag_id=vertrag.id, stufe=1, heute=date(2026, 3, 1))

    assert vorschau is not None
    assert vorschau.zinsbasis == "GESETZLICH_ABGB"
    assert vorschau.zinssatz_prozent == Decimal("4.000")
    # §458-Pauschale bleibt DAVON UNBERÜHRT (verschuldensunabhängig) -
    # eine geklärte Kostenbasis würde weiterhin eine Pauschale auslösen,
    # nur die VERZINSUNG fällt auf das gesetzliche Niveau zurück.
    assert any("gesetzliche" in h.lower() or "abgb" in h.lower() for h in vorschau.hinweise)


def test_zukuenftiges_halbjahr_verwendet_nicht_stillschweigend_alten_basiszinssatz(
    op_service, kosten_repo, kosten_service, admin_ctx, basis_vertrag,
):
    vertrag, konto = basis_vertrag
    _profil_geprueft(
        kosten_repo, vertrag_id=vertrag.id, ist_b2b=True, vertragsdatum=date(2020, 1, 1),
        verzugsverantwortung_geprueft=True,
    )
    kosten_repo.basiszinssatz_erfassen(
        id="2026-1", gueltig_von=date(2026, 1, 1), gueltig_bis=date(2026, 6, 30),
        basiszinssatz_prozent=Decimal("1.530"), erfasst_von="test", quelle_referenz="OeNB 01.01.2026",
    )
    op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=50_000,
        belegdatum=date(2026, 1, 1), buchungsdatum=date(2026, 1, 1),
        faelligkeit=date(2026, 1, 5), beleg_referenz="HMZ Jänner",
    )

    # Zweites Halbjahr 2026 wurde NICHT erfasst - darf keinesfalls den
    # Wert des ersten Halbjahrs stillschweigend fortschreiben. Die
    # Periode läuft aber teilweise noch durchs BELEGTE erste Halbjahr
    # (5.1.-1.7.) - dieser Teil wird jetzt korrekt segmentiert verzinst,
    # nur der Rest (1.7.-15.7., zweites Halbjahr) bleibt ungeklärt.
    vorschau = kosten_service.vorschau(vertrag_id=vertrag.id, stufe=1, heute=date(2026, 7, 15))

    assert vorschau is not None
    assert vorschau.zins_teilweise_ungeklaert is True
    assert vorschau.zinssatz_prozent == Decimal("10.730")  # das einzige AUFGELÖSTE Segment
    assert vorschau.neue_zinsen_cent > 0
    erwartete_zinsen_h1_anteil = _segment_zinsen_cent(50_000, Decimal("10.730"), (date(2026, 7, 1) - date(2026, 1, 5)).days)
    assert vorschau.neue_zinsen_cent == erwartete_zinsen_h1_anteil
    assert any("ohne belegte Zins-/Basiszinssatzgrundlage" in h for h in vorschau.hinweise)


def test_halbjahreswechsel_mit_beiden_erfassten_halbjahren_rechnet_in_teilperioden(
    op_service, kosten_repo, kosten_service, admin_ctx, basis_vertrag,
):
    """Kernfall der Rückprüfung 14.09.2026: eine Verzinsungsperiode, die
    einen Halbjahreswechsel überspannt, MUSS in zwei Teilperioden mit je
    EIGENEM belegtem Basiszinssatz gerechnet werden - niemals mit einem
    einzigen, für die ganze Periode geltenden Satz."""

    vertrag, konto = basis_vertrag
    _profil_geprueft(
        kosten_repo, vertrag_id=vertrag.id, ist_b2b=True, vertragsdatum=date(2020, 1, 1),
        verzugsverantwortung_geprueft=True,
    )
    kosten_repo.basiszinssatz_erfassen(
        id="2026-1", gueltig_von=date(2026, 1, 1), gueltig_bis=date(2026, 6, 30),
        basiszinssatz_prozent=Decimal("1.530"), erfasst_von="test", quelle_referenz="OeNB 01.01.2026",
    )
    kosten_repo.basiszinssatz_erfassen(
        id="2026-2", gueltig_von=date(2026, 7, 1), gueltig_bis=date(2026, 12, 31),
        basiszinssatz_prozent=Decimal("2.000"), erfasst_von="test", quelle_referenz="OeNB 01.07.2026",
    )
    op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=50_000,
        belegdatum=date(2026, 6, 1), buchungsdatum=date(2026, 6, 1),
        faelligkeit=date(2026, 6, 5), beleg_referenz="HMZ Juni",
    )

    vorschau = kosten_service.vorschau(vertrag_id=vertrag.id, stufe=1, heute=date(2026, 7, 20))

    assert vorschau is not None
    assert vorschau.zins_teilweise_ungeklaert is False
    # Zwei Sätze im Spiel (10,73 % bis 30.6., 11,20 % ab 1.7.) - keine
    # einzelne Zahl kann das korrekt zusammenfassen.
    assert vorschau.zinssatz_prozent is None
    assert len(vorschau.zins_segmente) == 2
    segment_h1 = next(s for s in vorschau.zins_segmente if s.satz_prozent == Decimal("10.730"))
    segment_h2 = next(s for s in vorschau.zins_segmente if s.satz_prozent == Decimal("11.200"))
    assert segment_h1.von == date(2026, 6, 5) and segment_h1.bis == date(2026, 7, 1)
    assert segment_h2.von == date(2026, 7, 1) and segment_h2.bis == date(2026, 7, 20)
    erwartet = (
        _segment_zinsen_cent(50_000, Decimal("10.730"), (date(2026, 7, 1) - date(2026, 6, 5)).days)
        + _segment_zinsen_cent(50_000, Decimal("11.200"), (date(2026, 7, 20) - date(2026, 7, 1)).days)
    )
    assert vorschau.neue_zinsen_cent == erwartet


# -- Vertragszinswechsel: periodengerecht ODER explizit unberechenbar ------


def test_zinsprofil_wechsel_mit_belegtem_gueltig_ab_wird_periodengerecht_segmentiert(
    op_service, kosten_repo, kosten_service, admin_ctx, basis_vertrag,
):
    """Rückprüfung 14.09.2026, Risiko 3: ZWEI geprüfte Zinsprofil-
    Versionen mit je EIGENEM, belegtem `gueltig_ab` müssen eine Periode,
    die den Wechsel überspannt, in Teilperioden mit je EIGENEM Satz
    zerlegen - niemals rückwirkend den aktuell/zuletzt geprüften Satz
    für die GESAMTE Periode verwenden."""

    vertrag, konto = basis_vertrag
    _profil_geprueft(
        kosten_repo, vertrag_id=vertrag.id, ist_b2b=False, vertragsdatum=date(2020, 1, 1),
        vereinbarter_zinssatz_prozent=Decimal("5.000"), vereinbarung_geprueft=True,
        vereinbarung_beleg="Mietvertrag 2020", gueltig_ab=date(2026, 1, 1),
    )
    _profil_geprueft(
        kosten_repo, vertrag_id=vertrag.id, ist_b2b=False, vertragsdatum=date(2020, 1, 1),
        vereinbarter_zinssatz_prozent=Decimal("6.000"), vereinbarung_geprueft=True,
        vereinbarung_beleg="Nachtrag 2026", gueltig_ab=date(2026, 6, 1),
    )
    op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=50_000,
        belegdatum=date(2026, 3, 1), buchungsdatum=date(2026, 3, 1),
        faelligkeit=date(2026, 3, 1), beleg_referenz="HMZ März",
    )

    vorschau = kosten_service.vorschau(vertrag_id=vertrag.id, stufe=1, heute=date(2026, 8, 1))

    assert vorschau is not None
    assert vorschau.zins_teilweise_ungeklaert is False
    assert len(vorschau.zins_segmente) == 2
    segment_v1 = next(s for s in vorschau.zins_segmente if s.satz_prozent == Decimal("5.000"))
    segment_v2 = next(s for s in vorschau.zins_segmente if s.satz_prozent == Decimal("6.000"))
    assert segment_v1.von == date(2026, 3, 1) and segment_v1.bis == date(2026, 6, 1)
    assert segment_v2.von == date(2026, 6, 1) and segment_v2.bis == date(2026, 8, 1)
    erwartet = (
        _segment_zinsen_cent(50_000, Decimal("5.000"), (date(2026, 6, 1) - date(2026, 3, 1)).days)
        + _segment_zinsen_cent(50_000, Decimal("6.000"), (date(2026, 8, 1) - date(2026, 6, 1)).days)
    )
    assert vorschau.neue_zinsen_cent == erwartet


def test_zinsprofil_wechsel_ohne_durchgaengiges_gueltig_ab_bleibt_unberechenbar(
    op_service, kosten_repo, kosten_service, admin_ctx, basis_vertrag,
):
    """Rückprüfung 14.09.2026, Risiko 3, Kehrseite: fehlt bei
    MINDESTENS einer von mehreren geprüften Versionen das `gueltig_ab`,
    wissen wir zwar, dass sich die Vereinbarung geändert hat, aber NICHT
    wann - der GESAMTE Zeitraum bleibt dann explizit unberechenbar
    (satz_prozent=None, keine Zinsen), statt zu raten, welche Version
    wann galt. Die Hauptforderung selbst bleibt davon unberührt."""

    vertrag, konto = basis_vertrag
    _profil_geprueft(
        kosten_repo, vertrag_id=vertrag.id, ist_b2b=False, vertragsdatum=date(2020, 1, 1),
        vereinbarter_zinssatz_prozent=Decimal("5.000"), vereinbarung_geprueft=True,
        vereinbarung_beleg="Mietvertrag 2020", gueltig_ab=date(2026, 1, 1),
    )
    _profil_geprueft(
        kosten_repo, vertrag_id=vertrag.id, ist_b2b=False, vertragsdatum=date(2020, 1, 1),
        vereinbarter_zinssatz_prozent=Decimal("6.000"), vereinbarung_geprueft=True,
        vereinbarung_beleg="Nachtrag 2026 (Datum nicht belegt)",  # bewusst OHNE gueltig_ab
    )
    op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=50_000,
        belegdatum=date(2026, 3, 1), buchungsdatum=date(2026, 3, 1),
        faelligkeit=date(2026, 3, 1), beleg_referenz="HMZ März",
    )

    vorschau = kosten_service.vorschau(vertrag_id=vertrag.id, stufe=1, heute=date(2026, 8, 1))

    assert vorschau is not None
    assert vorschau.zinsbasis == "UNBERECHENBAR"
    assert vorschau.zins_teilweise_ungeklaert is True
    assert vorschau.neue_zinsen_cent == 0
    assert all(s.satz_prozent is None for s in vorschau.zins_segmente)
    assert vorschau.hauptforderung_cent == 50_000  # Hauptforderung bleibt unblockiert
    assert any("unberechenbar" in h.lower() for h in vorschau.hinweise)
    assert any("§458" in h and "unberechenbar" in h.lower() for h in vorschau.hinweise)

    gebucht = kosten_service.buche_bei_versand(
        ctx=admin_ctx, vertrag_id=vertrag.id, stufe=1, heute=date(2026, 8, 1),
        versandnachweis_referenz="mahnung:unberechenbar", akteur="test",
    )
    assert gebucht is None  # nichts zu buchen, solange die Historie ungeklärt ist


# -- Privat/B2B-Unterscheidung ----------------------------------------------


def test_privatvertrag_erhaelt_gesetzliche_vier_prozent_trotz_datum_nach_ugb_stichtag(
    op_service, kosten_repo, kosten_service, admin_ctx, basis_vertrag,
):
    vertrag, konto = basis_vertrag
    _profil_geprueft(kosten_repo, vertrag_id=vertrag.id, ist_b2b=False, vertragsdatum=date(2024, 1, 1))
    op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=50_000,
        belegdatum=date(2026, 1, 1), buchungsdatum=date(2026, 1, 1),
        faelligkeit=date(2026, 1, 5), beleg_referenz="HMZ Jänner",
    )

    entscheidung = bestimme_zinssatz(
        zinsprofil=kosten_repo.geprueftes_zinsprofil(vertrag.id), heute=date(2026, 3, 1),
        basiszinssatz_lookup=kosten_repo.basiszinssatz_fuer_datum,
    )
    assert entscheidung.basis == "GESETZLICH_ABGB"
    assert entscheidung.satz_prozent == Decimal("4.000")


# -- Altvertrag vor dem UGB-Stichtag 16.03.2013 -----------------------------


def test_b2b_altvertrag_vor_ugb_stichtag_faellt_auf_gesetzliche_vier_prozent_zurueck(
    kosten_repo, basis_vertrag,
):
    vertrag, _konto = basis_vertrag
    _profil_geprueft(kosten_repo, vertrag_id=vertrag.id, ist_b2b=True, vertragsdatum=date(2010, 5, 1))
    kosten_repo.basiszinssatz_erfassen(
        id="2026-1", gueltig_von=date(2026, 1, 1), gueltig_bis=date(2026, 6, 30),
        basiszinssatz_prozent=Decimal("1.530"), erfasst_von="test", quelle_referenz="OeNB 01.01.2026",
    )

    entscheidung = bestimme_zinssatz(
        zinsprofil=kosten_repo.geprueftes_zinsprofil(vertrag.id), heute=date(2026, 3, 1),
        basiszinssatz_lookup=kosten_repo.basiszinssatz_fuer_datum,
    )
    # Vertragsdatum vor dem UGB-Stichtag - NIEMALS fälschlich 9,2 Prozentpunkte
    # aufschlagen, auch wenn ein Basiszinssatz erfasst ist.
    assert entscheidung.basis == "GESETZLICH_ABGB"
    assert entscheidung.satz_prozent == Decimal("4.000")


# -- Ungeklärte Mahngebühren-Kostenbasis blockiert nicht die Hauptforderung -


def test_fehlendes_zinsprofil_blockiert_hauptforderung_nicht_nur_gebuehr_klaerungsbeduerftig(
    op_service, kosten_service, admin_ctx, basis_vertrag,
):
    vertrag, konto = basis_vertrag
    op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=50_000,
        belegdatum=date(2026, 1, 1), buchungsdatum=date(2026, 1, 1),
        faelligkeit=date(2026, 1, 5), beleg_referenz="HMZ Jänner",
    )

    vorschau = kosten_service.vorschau(vertrag_id=vertrag.id, stufe=1, heute=date(2026, 3, 1))

    assert vorschau is not None
    assert vorschau.hauptforderung_cent == 50_000  # nicht blockiert
    assert vorschau.gebuehr_cent is None
    assert vorschau.zinssatz_prozent == Decimal("4.000")  # gesetzliche Basis ohne Profil
    assert vorschau.neue_zinsen_cent > 0
    # Ohne jedes Zinsprofil ist §458 UGB (Unternehmerforderung) von
    # vornherein nicht anwendbar - die Gebühr bleibt klar begründet aus,
    # ohne die (unabhängig davon weiterhin verzinste) Hauptforderung zu berühren.
    assert any("beiderseits unternehmensbezogenem" in h for h in vorschau.hinweise)


# -- Mahngebühr (§458 UGB) - nur bei B2B, permanent je Entgeltforderung ---


def test_mahngebuehr_erfordert_b2b_und_wird_bei_privatvertrag_nie_angesetzt(
    op_service, kosten_repo, kosten_service, admin_ctx, basis_vertrag,
):
    """Rückprüfung 14.09.2026: §458 UGB betrifft ausschließlich eine
    Unternehmerforderung - eine belegte Kostenbasis allein reicht NICHT,
    ohne beiderseits unternehmensbezogenes Geschäft (B2B) wird NIE eine
    Pauschale angesetzt, unabhängig vom Ansatz früherer Runden."""

    vertrag, konto = basis_vertrag
    _profil_geprueft(
        kosten_repo, vertrag_id=vertrag.id, ist_b2b=False, vertragsdatum=date(2020, 1, 1),
        mahngebuehr_kostenbasis_cent=1500, mahngebuehr_kostenbasis_beleg="Portokosten-Nachweis",
    )
    op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=50_000,
        belegdatum=date(2026, 1, 1), buchungsdatum=date(2026, 1, 1),
        faelligkeit=date(2026, 1, 5), beleg_referenz="HMZ Jänner",
    )

    vorschau = kosten_service.vorschau(vertrag_id=vertrag.id, stufe=1, heute=date(2026, 2, 1))

    assert vorschau is not None
    assert vorschau.gebuehr_cent is None
    assert not vorschau.gebuehr_segmente
    assert any("beiderseits unternehmensbezogenem" in h for h in vorschau.hinweise)


def test_mahngebuehr_wird_nur_einmal_ueber_beide_stufen_fuer_dieselbe_forderung_angesetzt(
    op_service, kosten_repo, kosten_service, admin_ctx, basis_vertrag,
):
    vertrag, konto = basis_vertrag
    _profil_geprueft(
        kosten_repo, vertrag_id=vertrag.id, ist_b2b=True, vertragsdatum=date(2020, 1, 1),
        mahngebuehr_kostenbasis_cent=1500, mahngebuehr_kostenbasis_beleg="Portokosten-Nachweis",
    )
    kosten_repo.basiszinssatz_erfassen(
        id="2026-1", gueltig_von=date(2026, 1, 1), gueltig_bis=date(2026, 6, 30),
        basiszinssatz_prozent=Decimal("1.530"), erfasst_von="test", quelle_referenz="OeNB 01.01.2026",
    )
    op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=50_000,
        belegdatum=date(2026, 1, 1), buchungsdatum=date(2026, 1, 1),
        faelligkeit=date(2026, 1, 5), leistungsperiode="2026-01", beleg_referenz="HMZ Jänner",
    )

    stufe1 = kosten_service.buche_bei_versand(
        ctx=admin_ctx, vertrag_id=vertrag.id, stufe=1, heute=date(2026, 2, 1),
        versandnachweis_referenz="mahnung:stufe1", akteur="test",
    )
    assert stufe1 is not None
    assert stufe1.gebuehr_cent == 1500

    stufe2 = kosten_service.buche_bei_versand(
        ctx=admin_ctx, vertrag_id=vertrag.id, stufe=2, heute=date(2026, 3, 3),
        versandnachweis_referenz="mahnung:stufe2", akteur="test",
    )
    assert stufe2 is not None  # weiterhin neue Zinsen seit Stufe 1 (Basiszinssatz belegt)
    assert stufe2.gebuehr_cent is None  # §458 UGB: nicht ein zweites Mal für DIESELBE Entgeltforderung

    saldo_positionen = op_service.berechne_saldo(konto.id, stichtag=date(2026, 3, 3)).positionen
    gebuehr_zeilen = [p for p in saldo_positionen if p.aenderungsgrund == "Mahnkosten - Mahnspesen"]
    assert len(gebuehr_zeilen) == 1


def test_mahngebuehr_zwei_komponenten_derselben_periode_ergeben_nur_eine_pauschale(
    op_service, kosten_repo, kosten_service, admin_ctx, basis_vertrag,
):
    """Nicht je Mietkomponente: HMZ und BK derselben Vorschreibungsperiode
    teilen dieselbe `leistungsperiode` und bilden EINE Entgeltforderung -
    also GENAU EINE Pauschale, nicht zwei."""

    vertrag, konto = basis_vertrag
    _profil_geprueft(
        kosten_repo, vertrag_id=vertrag.id, ist_b2b=True, vertragsdatum=date(2020, 1, 1),
        mahngebuehr_kostenbasis_cent=1500, mahngebuehr_kostenbasis_beleg="Portokosten-Nachweis",
    )
    op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=50_000,
        belegdatum=date(2026, 1, 1), buchungsdatum=date(2026, 1, 1),
        faelligkeit=date(2026, 1, 5), leistungsperiode="2026-01", beleg_referenz="HMZ Jänner",
    )
    op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=15_000,
        belegdatum=date(2026, 1, 1), buchungsdatum=date(2026, 1, 1),
        faelligkeit=date(2026, 1, 5), leistungsperiode="2026-01", beleg_referenz="BK Jänner",
    )

    vorschau = kosten_service.vorschau(vertrag_id=vertrag.id, stufe=1, heute=date(2026, 2, 1))

    assert vorschau is not None
    assert len(vorschau.gebuehr_segmente) == 1
    assert vorschau.gebuehr_cent == 1500


def test_mahngebuehr_zwei_genuin_unterschiedliche_monate_ergeben_zwei_pauschalen(
    op_service, kosten_repo, kosten_service, admin_ctx, basis_vertrag,
):
    """Eine bereits erhobene Pauschale wird für eine GENUIN andere
    Entgeltforderung (ein anderer Monat) NICHT blockiert - die
    Permanenz gilt je Forderung, nicht pauschal je Vertrag."""

    vertrag, konto = basis_vertrag
    _profil_geprueft(
        kosten_repo, vertrag_id=vertrag.id, ist_b2b=True, vertragsdatum=date(2020, 1, 1),
        mahngebuehr_kostenbasis_cent=1500, mahngebuehr_kostenbasis_beleg="Portokosten-Nachweis",
    )
    op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=50_000,
        belegdatum=date(2026, 1, 1), buchungsdatum=date(2026, 1, 1),
        faelligkeit=date(2026, 1, 5), leistungsperiode="2026-01", beleg_referenz="HMZ Jänner",
    )
    op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=50_000,
        belegdatum=date(2026, 2, 1), buchungsdatum=date(2026, 2, 1),
        faelligkeit=date(2026, 2, 5), leistungsperiode="2026-02", beleg_referenz="HMZ Februar",
    )

    stufe1 = kosten_service.buche_bei_versand(
        ctx=admin_ctx, vertrag_id=vertrag.id, stufe=1, heute=date(2026, 3, 1),
        versandnachweis_referenz="mahnung:beide-monate", akteur="test",
    )
    assert stufe1 is not None
    assert stufe1.gebuehr_cent == 3000  # zwei genuin unterschiedliche Entgeltforderungen, je 1500

    # Ein dritter, wieder neuer Monat später löst noch eine EIGENE Pauschale
    # aus - die beiden bereits erhobenen bleiben dauerhaft erkannt.
    op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=50_000,
        belegdatum=date(2026, 3, 1), buchungsdatum=date(2026, 3, 1),
        faelligkeit=date(2026, 3, 5), leistungsperiode="2026-03", beleg_referenz="HMZ März",
    )
    stufe2 = kosten_service.buche_bei_versand(
        ctx=admin_ctx, vertrag_id=vertrag.id, stufe=2, heute=date(2026, 4, 1),
        versandnachweis_referenz="mahnung:dritter-monat", akteur="test",
    )
    assert stufe2 is not None
    assert stufe2.gebuehr_cent == 1500  # nur der DRITTE, bisher unbepauschalte Monat


def test_reserviere_und_kuerze_vorschau_entfernt_von_anderer_gruppe_bereits_reserviertes_segment(
    op_service, kosten_repo, kosten_service, admin_ctx, basis_vertrag,
):
    """Unabhängige Abnahme eb7b8a7, echter Bug (reproduziert ohne
    Threads): zwei disjunkte Mahnlauf-Gruppen für zwei GENUIN
    unterschiedliche OP-Komponenten DERSELBEN Vorschreibungsperiode
    können ihre jeweilige Kostenvorschau BEIDE berechnen, BEVOR eine von
    beiden tatsächlich reserviert - beide sehen (noch) dieselbe offene
    §458-Pauschale. `reserviere_und_kuerze_vorschau` MUSS sicherstellen,
    dass NUR die zuerst reservierende Gruppe die Pauschale in ihrer
    (danach eingefrorenen und versendeten) Vorschau behält - die zweite
    Gruppe darf sie NIEMALS ebenfalls ankündigen."""

    vertrag, konto = basis_vertrag
    _profil_geprueft(
        kosten_repo, vertrag_id=vertrag.id, ist_b2b=True, vertragsdatum=date(2020, 1, 1),
        mahngebuehr_kostenbasis_cent=4000, mahngebuehr_kostenbasis_beleg="Portokosten-Nachweis",
    )
    op_a = op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=60_000,
        belegdatum=date(2026, 4, 1), buchungsdatum=date(2026, 4, 1),
        faelligkeit=date(2026, 4, 5), leistungsperiode="2026-04", beleg_referenz="Komponente A",
    )
    op_b = op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=15_000,
        belegdatum=date(2026, 4, 1), buchungsdatum=date(2026, 4, 1),
        faelligkeit=date(2026, 4, 6), leistungsperiode="2026-04", beleg_referenz="Komponente B",
    )
    heute = date(2026, 4, 20)
    # BEIDE Gruppen berechnen ihre eigene, an ihre Mitgliedermenge
    # gebundene Vorschau, BEVOR auch nur eine von beiden reserviert -
    # genau das Zeitfenster einer echten Nebenläufigkeit, hier ohne
    # Threads durch getrennte, vorab berechnete Vorschauen simuliert.
    vorschau_a = kosten_service.vorschau(vertrag_id=vertrag.id, stufe=1, heute=heute, nur_op_position_ids=frozenset({op_a.id}))
    vorschau_b = kosten_service.vorschau(vertrag_id=vertrag.id, stufe=1, heute=heute, nur_op_position_ids=frozenset({op_b.id}))
    assert vorschau_a.gebuehr_cent == 4000
    assert vorschau_b.gebuehr_cent == 4000  # BEIDE sehen (noch) dieselbe offene Pauschale

    gekuerzte_a = kosten_service.reserviere_und_kuerze_vorschau(vorschau=vorschau_a, mahnlauf_id=101, akteur="test")
    assert gekuerzte_a is vorschau_a  # nichts zu kürzen - A war zuerst
    assert gekuerzte_a.gebuehr_cent == 4000

    gekuerzte_b = kosten_service.reserviere_und_kuerze_vorschau(vorschau=vorschau_b, mahnlauf_id=102, akteur="test")
    assert gekuerzte_b is not vorschau_b
    assert gekuerzte_b.gebuehr_cent is None  # B darf die Pauschale NICHT nochmal ankündigen
    assert gekuerzte_b.gebuehr_segmente == ()
    assert gekuerzte_b.hauptforderung_cent == vorschau_b.hauptforderung_cent  # Hauptforderung unberührt

    with kosten_repo._session_factory() as db:
        gebuehren = list(db.execute(select(MahnkostenGebuehrTable).where(
            MahnkostenGebuehrTable.vertrag_id == vertrag.id)).scalars())
    assert len(gebuehren) == 1  # GENAU eine Zeile für "PERIODE:2026-04"
    assert gebuehren[0].reserviert_fuer_mahnlauf_id == 101
    assert gebuehren[0].status == "RESERVIERT"


def test_gib_reservierungen_frei_erlaubt_spaeteren_versuch_einer_anderen_gruppe(
    op_service, kosten_repo, kosten_service, admin_ctx, basis_vertrag,
):
    """Eine VOR jedem Providerkontakt blockierte Gruppe gibt ihre
    Reservierung wieder frei - eine GENUIN andere (oder dieselbe, neu
    geplante) Gruppe kann die Entgeltforderung danach erneut
    versuchen."""

    vertrag, konto = basis_vertrag
    _profil_geprueft(
        kosten_repo, vertrag_id=vertrag.id, ist_b2b=True, vertragsdatum=date(2020, 1, 1),
        mahngebuehr_kostenbasis_cent=4000, mahngebuehr_kostenbasis_beleg="Portokosten-Nachweis",
    )
    op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=50_000,
        belegdatum=date(2026, 4, 1), buchungsdatum=date(2026, 4, 1),
        faelligkeit=date(2026, 4, 5), leistungsperiode="2026-04", beleg_referenz="HMZ April",
    )
    heute = date(2026, 4, 20)
    vorschau = kosten_service.vorschau(vertrag_id=vertrag.id, stufe=1, heute=heute)
    assert vorschau.gebuehr_cent == 4000

    gekuerzt = kosten_service.reserviere_und_kuerze_vorschau(vorschau=vorschau, mahnlauf_id=201, akteur="test")
    assert gekuerzt is vorschau  # erfolgreich reserviert

    kosten_service.gib_reservierungen_frei(vorschau=vorschau, mahnlauf_id=201)
    with kosten_repo._session_factory() as db:
        gebuehren = list(db.execute(select(MahnkostenGebuehrTable).where(
            MahnkostenGebuehrTable.vertrag_id == vertrag.id)).scalars())
    assert gebuehren == []  # Reservierung tatsächlich entfernt

    frische_vorschau = kosten_service.vorschau(vertrag_id=vertrag.id, stufe=1, heute=heute)
    assert frische_vorschau.gebuehr_cent == 4000  # wieder offen für einen neuen Versuch
    gekuerzt_neu = kosten_service.reserviere_und_kuerze_vorschau(vorschau=frische_vorschau, mahnlauf_id=202, akteur="test")
    assert gekuerzt_neu is frische_vorschau
    assert gekuerzt_neu.gebuehr_cent == 4000


# -- Unbekannte/ungegliederte Eröffnungsstruktur: nie fiktiv verzinst ------


def test_ungegliederte_eroeffnung_wird_nicht_verzinst_aber_zaehlt_zur_hauptforderung(
    op_service, kosten_service, admin_ctx, basis_vertrag,
):
    vertrag, konto = basis_vertrag
    op_service.eroeffnen_gesamtsaldo(
        ctx=admin_ctx, konto=konto, betrag_cent=30_000, stichtag=date(2026, 1, 1),
        import_id="ERO-1", akteur="test",
    )

    vorschau = kosten_service.vorschau(vertrag_id=vertrag.id, stufe=1, heute=date(2026, 3, 1))

    assert vorschau is not None
    assert vorschau.hauptforderung_cent == 30_000  # zählt zur Hauptforderung
    assert vorschau.neue_zinsen_cent == 0  # aber nie fiktiv seit Monatsanfang verzinst
    assert len(vorschau.ausgeschlossene_forderungen_hinweis) == 1


# -- Cross-Tenant / Sicherheitsgrenze auf buche_bei_versand ----------------


def test_buche_bei_versand_verweigert_zugriff_ohne_gesellschaftsscope(
    op_service, kosten_service, ctx_factory, basis_vertrag,
):
    vertrag, konto = basis_vertrag
    op_service.buchen(
        ctx=AuthContext(user_id="admin", rolle=Rolle.ADMIN, gesellschaft_ids=None),
        konto=konto, typ=OPTyp.SOLL, betrag_cent=50_000,
        belegdatum=date(2026, 1, 1), buchungsdatum=date(2026, 1, 1),
        faelligkeit=date(2026, 1, 5), beleg_referenz="HMZ Jänner",
    )
    fremder_ctx = ctx_factory("ANDERE-GESELLSCHAFT")

    with pytest.raises(CrossTenantError):
        kosten_service.buche_bei_versand(
            ctx=fremder_ctx, vertrag_id=vertrag.id, stufe=1, heute=date(2026, 3, 1),
            versandnachweis_referenz="mahnung:fremd", akteur="fremd",
        )
