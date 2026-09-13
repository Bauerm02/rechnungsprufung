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

from datetime import date
from decimal import Decimal

import pytest

from mietinkasso.auth.service import AuthContext, require_gesellschaft_access
from mietinkasso.domain.enums import OPTyp, Rolle
from mietinkasso.domain.exceptions import CrossTenantError
from mietinkasso.mahnwesen.kosten import BalancePeriode, berechne_verzugszinsen_cent, bestimme_zinssatz
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
    # (Zahlungsdatum), danach 60.000 Cent vom 20.1. bis 1.3. - exakt die
    # beiden Perioden, die `balance_zeitreihe_fuer_forderung` liefern muss.
    erwartete_perioden = [
        BalancePeriode(von=date(2026, 1, 5), bis=date(2026, 1, 20), rest_cent=100_000),
        BalancePeriode(von=date(2026, 1, 20), bis=date(2026, 3, 1), rest_cent=60_000),
    ]
    erwartete_zinsen = berechne_verzugszinsen_cent(erwartete_perioden, Decimal("4.000"))
    assert vorschau_mit_teilzahlung.neue_zinsen_cent == erwartete_zinsen

    # Gegenprobe: würde die Teilzahlung NICHT berücksichtigt, wäre die
    # volle Basis (100.000 Cent) über die gesamte Periode verzinst worden -
    # das muss strikt mehr sein als der tatsächliche, korrekt reduzierte Wert.
    ohne_teilzahlung = berechne_verzugszinsen_cent(
        [BalancePeriode(von=date(2026, 1, 5), bis=date(2026, 3, 1), rest_cent=100_000)], Decimal("4.000"),
    )
    assert vorschau_mit_teilzahlung.neue_zinsen_cent < ohne_teilzahlung


# -- Halbjahreswechsel ohne erfassten Basiszinssatz: B2B blockiert ---------


def test_b2b_ohne_erfassten_basiszinssatz_fuer_das_halbjahr_bleibt_blockiert(
    op_service, kosten_repo, kosten_service, admin_ctx, basis_vertrag,
):
    vertrag, konto = basis_vertrag
    _profil_geprueft(kosten_repo, vertrag_id=vertrag.id, ist_b2b=True, vertragsdatum=date(2020, 1, 1))
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
    assert vorschau.zinsbasis == "UGB_B2B_BASISZINSSATZ"
    assert vorschau.zinssatz_prozent == Decimal("10.730")  # 1,53 % + 9,2 Prozentpunkte
    assert vorschau.neue_zinsen_cent > 0


def test_zukuenftiges_halbjahr_verwendet_nicht_stillschweigend_alten_basiszinssatz(
    op_service, kosten_repo, kosten_service, admin_ctx, basis_vertrag,
):
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

    # Zweites Halbjahr 2026 wurde NICHT erfasst - darf keinesfalls den
    # Wert des ersten Halbjahrs stillschweigend fortschreiben.
    vorschau = kosten_service.vorschau(vertrag_id=vertrag.id, stufe=1, heute=date(2026, 7, 15))

    assert vorschau is not None
    assert vorschau.zinssatz_prozent is None
    assert vorschau.neue_zinsen_cent == 0


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
    assert any("Klärung erforderlich" in h for h in vorschau.hinweise)


# -- Mahngebühr wird nur einmal je Vertrag/Mahnlauf angesetzt --------------


def test_mahngebuehr_wird_nur_einmal_ueber_beide_stufen_angesetzt(
    op_service, kosten_repo, kosten_service, admin_ctx, basis_vertrag,
):
    vertrag, konto = basis_vertrag
    _profil_geprueft(
        kosten_repo, vertrag_id=vertrag.id, ist_b2b=False, vertragsdatum=None,
        mahngebuehr_kostenbasis_cent=1500, mahngebuehr_kostenbasis_beleg="Portokosten-Nachweis",
    )
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
    assert stufe1.gebuehr_cent == 1500

    stufe2 = kosten_service.buche_bei_versand(
        ctx=admin_ctx, vertrag_id=vertrag.id, stufe=2, heute=date(2026, 3, 3),
        versandnachweis_referenz="mahnung:stufe2", akteur="test",
    )
    assert stufe2 is not None
    assert stufe2.gebuehr_cent is None  # §458 UGB: nicht ein zweites Mal

    saldo_positionen = op_service.berechne_saldo(konto.id, stichtag=date(2026, 3, 3)).positionen
    gebuehr_zeilen = [p for p in saldo_positionen if p.aenderungsgrund == "Mahnkosten - Mahnspesen"]
    assert len(gebuehr_zeilen) == 1


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
