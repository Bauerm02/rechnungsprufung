"""Synthetische Regressionstests: positive KORREKTUR in der OP-/Zinsallokation."""

from __future__ import annotations

from datetime import date

import pytest

from mietinkasso.domain.enums import OPTyp
from mietinkasso.domain.exceptions import ZahlungsbindungInkonsistentError
from mietinkasso.mahnwesen.kosten import balance_zeitreihe_fuer_forderung, berechne_mahnkosten_vorschau


def _buche(op_service, ctx, konto, typ, betrag, tag, referenz, *, faelligkeit=None, periode=None, bezug=None):
    return op_service.buchen(
        ctx=ctx, konto=konto, typ=typ, betrag_cent=betrag, belegdatum=tag, buchungsdatum=tag,
        faelligkeit=faelligkeit, beleg_referenz=referenz, leistungsperiode=periode, bezieht_sich_auf_id=bezug,
    )


def _guthaben_800(op_service, ctx, konto):
    _buche(op_service, ctx, konto, OPTyp.SOLL, 10_000, date(2026, 9, 1), "Miete September", faelligkeit=date(2026, 9, 5))
    _buche(op_service, ctx, konto, OPTyp.ZAHLUNG, 10_800, date(2026, 9, 6), "Ueberzahlung September")


def _november(op_service, ctx, konto):
    return _buche(
        op_service, ctx, konto, OPTyp.SOLL, 10_000, date(2026, 11, 1), "Miete November",
        faelligkeit=date(2026, 11, 5), periode="2026-11",
    )


def _reste(op_service, konto, heute=date(2026, 11, 20)):
    return {f.op_position_id: f.rest_cent for f in op_service.offene_forderungen(konto.id, heute=heute)}


def _vorschau(op_service, konto, heute):
    return berechne_mahnkosten_vorschau(
        vertrag_id="V-601-3", stufe=1,
        forderungen=op_service.offene_forderungen(konto.id, heute=heute),
        alle_positionen=op_service.list_alle_positionen(konto.id),
        heute=heute, zinsprofil_historie=[],
        basiszinssatz_lookup=lambda _datum: None,
        naechster_basiszinssatz_lookup=lambda _datum: None,
        bereits_gebuchte_zinsen_je_op_position={},
        bereits_erhobene_gebuehr_schluessel=frozenset(),
    )


def test_rueckzahlung_vor_neuer_miete_ergibt_vollen_rest(op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    _guthaben_800(op_service, ctx, konto)
    _buche(op_service, ctx, konto, OPTyp.KORREKTUR, 800, date(2026, 10, 8), "Guthabenerstattung")
    november = _november(op_service, ctx, konto)

    saldo = op_service.berechne_saldo(konto.id, stichtag=date(2026, 11, 20))
    assert saldo.saldo_cent == 10_000
    assert saldo.faelliger_unstrittiger_rest_cent == 10_000
    assert _reste(op_service, konto) == {november.id: 10_000}
    assert len(op_service.list_alle_positionen(konto.id)) == 4  # nichts wird nachgebucht


def test_alte_positive_korrektur_ohne_faelligkeit_verbraucht_guthabenrest(op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    op_service.eroeffnen_gesamtsaldo(
        ctx=ctx, konto=konto, betrag_cent=-1_000, stichtag=date(2026, 8, 31), import_id="ERO-GUTHABEN", akteur="test",
    )
    _buche(op_service, ctx, konto, OPTyp.KORREKTUR, 800, date(2026, 9, 15), "Alte Erstattung")
    november = _november(op_service, ctx, konto)

    assert op_service.berechne_saldo(konto.id).saldo_cent == 9_800
    assert _reste(op_service, konto) == {november.id: 9_800}


def test_teilrueckzahlung_laesst_restguthaben_fuer_neue_miete(op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    _guthaben_800(op_service, ctx, konto)
    _buche(op_service, ctx, konto, OPTyp.KORREKTUR, 300, date(2026, 10, 8), "Teilerstattung")
    november = _november(op_service, ctx, konto)

    assert op_service.berechne_saldo(konto.id).saldo_cent == 9_500
    assert _reste(op_service, konto) == {november.id: 9_500}


def test_gebundene_ueberzahlung_und_erstattung_gleicher_periode_ergeben_null(op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    _buche(op_service, ctx, konto, OPTyp.ZAHLUNG, 5_000, date(2026, 10, 20), "Ueberzahlung Oktober", periode="2026-10")
    _buche(op_service, ctx, konto, OPTyp.KORREKTUR, 5_000, date(2026, 10, 25), "Erstattung Oktober", periode="2026-10")

    saldo = op_service.berechne_saldo(konto.id, stichtag=date(2026, 11, 20))
    assert saldo.saldo_cent == 0
    assert saldo.faelliger_unstrittiger_rest_cent == 0
    assert _reste(op_service, konto) == {}
    assert not any(p.typ == OPTyp.SOLL.value for p in op_service.list_alle_positionen(konto.id))


def test_teilerstattung_gebundener_ueberzahlung_tilgt_keine_andere_periode(op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    august = _buche(
        op_service, ctx, konto, OPTyp.SOLL, 8_000, date(2026, 8, 1), "Miete August",
        faelligkeit=date(2026, 8, 5), periode="2026-08",
    )
    _buche(op_service, ctx, konto, OPTyp.ZAHLUNG, 5_000, date(2026, 10, 20), "Ueberzahlung Oktober", periode="2026-10")
    _buche(op_service, ctx, konto, OPTyp.KORREKTUR, 2_000, date(2026, 10, 25), "Teilerstattung Oktober", periode="2026-10")

    assert op_service.berechne_saldo(konto.id).saldo_cent == 5_000
    assert _reste(op_service, konto) == {august.id: 8_000}  # 3.000 bleiben gebundenes Guthaben


def test_erstattung_anderer_periode_verbraucht_gebundenes_guthaben_nicht(op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    _buche(op_service, ctx, konto, OPTyp.ZAHLUNG, 5_000, date(2026, 10, 20), "Ueberzahlung Oktober", periode="2026-10")
    korrektur = _buche(op_service, ctx, konto, OPTyp.KORREKTUR, 5_000, date(2026, 10, 25), "Erstattung September", periode="2026-09")

    assert op_service.berechne_saldo(konto.id).saldo_cent == 0
    assert _reste(op_service, konto) == {korrektur.id: 5_000}


def test_gebundene_zahlung_bleibt_vorrangig(op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    september = _buche(
        op_service, ctx, konto, OPTyp.SOLL, 10_000, date(2026, 9, 1), "Miete September",
        faelligkeit=date(2026, 9, 5), periode="2026-09",
    )
    oktober = _buche(
        op_service, ctx, konto, OPTyp.SOLL, 10_000, date(2026, 10, 1), "Miete Oktober",
        faelligkeit=date(2026, 10, 5), periode="2026-10",
    )
    _buche(op_service, ctx, konto, OPTyp.ZAHLUNG, 10_000, date(2026, 10, 6), "Zahlung Oktober", periode="2026-10", bezug=oktober.id)
    korrektur = _buche(op_service, ctx, konto, OPTyp.KORREKTUR, 500, date(2026, 10, 8), "Korrektur ohne Deckung")

    assert _reste(op_service, konto) == {september.id: 10_000, korrektur.id: 500}


def test_zahlungsbindung_auf_korrektur_bleibt_fehlbindung(op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    korrektur = _buche(op_service, ctx, konto, OPTyp.KORREKTUR, 800, date(2026, 10, 8), "Korrektur")
    with pytest.raises(ZahlungsbindungInkonsistentError):
        _buche(op_service, ctx, konto, OPTyp.ZAHLUNG, 800, date(2026, 10, 9), "Falsch gebunden", bezug=korrektur.id)
        op_service.offene_forderungen(konto.id)


def test_negative_korrektur_bleibt_minderung(op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    soll = _buche(
        op_service, ctx, konto, OPTyp.SOLL, 10_000, date(2026, 9, 1), "Miete September", faelligkeit=date(2026, 9, 5),
    )
    _buche(op_service, ctx, konto, OPTyp.KORREKTUR, -2_500, date(2026, 9, 20), "Minderung")

    assert _reste(op_service, konto) == {soll.id: 7_500}
    assert op_service.berechne_saldo(konto.id).saldo_cent == 7_500
    perioden = balance_zeitreihe_fuer_forderung(
        ziel_op_position_id=soll.id, alle_positionen=op_service.list_alle_positionen(konto.id), heute=date(2026, 10, 1),
    )
    assert perioden is not None
    assert (perioden[0].von, perioden[0].bis, perioden[0].rest_cent) == (date(2026, 9, 6), date(2026, 9, 20), 10_000)
    assert perioden[-1].rest_cent == 7_500


def test_stornierte_erstattung_ist_nicht_wirksam(op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    _guthaben_800(op_service, ctx, konto)
    erstattung = _buche(op_service, ctx, konto, OPTyp.KORREKTUR, 800, date(2026, 10, 8), "Erstattung")
    november = _november(op_service, ctx, konto)
    op_service.storniere_und_korrigiere(ctx=ctx, konto=konto, original_id=erstattung.id, aenderungsgrund="Fehlbuchung")

    assert op_service.berechne_saldo(konto.id).saldo_cent == 9_200
    assert _reste(op_service, konto) == {november.id: 9_200}
    perioden = balance_zeitreihe_fuer_forderung(
        ziel_op_position_id=november.id, alle_positionen=op_service.list_alle_positionen(konto.id), heute=date(2026, 11, 20),
    )
    assert [(p.von, p.bis, p.rest_cent) for p in perioden] == [(date(2026, 11, 6), date(2026, 11, 20), 9_200)]


def test_zinszeitreihe_gleicher_rest_und_start_am_tag_nach_faelligkeit(op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    _guthaben_800(op_service, ctx, konto)
    _buche(op_service, ctx, konto, OPTyp.KORREKTUR, 800, date(2026, 10, 8), "Guthabenerstattung")
    november = _november(op_service, ctx, konto)

    perioden = balance_zeitreihe_fuer_forderung(
        ziel_op_position_id=november.id, alle_positionen=op_service.list_alle_positionen(konto.id), heute=date(2026, 11, 20),
    )
    assert [(p.von, p.bis, p.rest_cent) for p in perioden] == [(date(2026, 11, 6), date(2026, 11, 20), 10_000)]
    assert _reste(op_service, konto) == {november.id: 10_000}
    vorschau = _vorschau(op_service, konto, date(2026, 11, 20))
    assert {s.op_position_id for s in vorschau.zins_segmente} == {november.id}
    assert all(s.rest_cent == 10_000 and s.von == date(2026, 11, 6) for s in vorschau.zins_segmente)


def test_ungedeckte_korrektur_ohne_faelligkeit_bleibt_sichtbar_ohne_zinsen_und_mahnfreigabe(op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    korrektur = _buche(op_service, ctx, konto, OPTyp.KORREKTUR, 800, date(2026, 10, 8), "Korrektur ohne Deckung")
    heute = date(2026, 10, 20)

    saldo = op_service.berechne_saldo(konto.id, stichtag=heute)
    assert saldo.saldo_cent == 800
    assert saldo.faelliger_unstrittiger_rest_cent == 0
    forderung = op_service.offene_forderungen(konto.id, heute=heute)[0]
    assert (forderung.op_position_id, forderung.rest_cent) == (korrektur.id, 800)
    assert forderung.faelligkeit is None and forderung.faelligkeit_bekannt is False
    assert balance_zeitreihe_fuer_forderung(
        ziel_op_position_id=korrektur.id, alle_positionen=op_service.list_alle_positionen(konto.id), heute=heute,
    ) is None
    vorschau = _vorschau(op_service, konto, heute)
    assert vorschau.neue_zinsen_cent == 0 and vorschau.zins_segmente == ()
    assert vorschau.gebuehr_segmente == ()
    assert vorschau.ausgeschlossene_forderungen_hinweis


def test_ungedeckte_korrektur_mit_faelligkeit_erfindet_keinen_zins_und_keine_gebuehr(op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    korrektur = _buche(
        op_service, ctx, konto, OPTyp.KORREKTUR, 800, date(2026, 10, 8), "Korrektur mit Faelligkeit", faelligkeit=date(2026, 10, 15),
    )
    heute = date(2026, 10, 20)

    assert op_service.berechne_saldo(konto.id, stichtag=heute).faelliger_unstrittiger_rest_cent == 800
    perioden = balance_zeitreihe_fuer_forderung(
        ziel_op_position_id=korrektur.id, alle_positionen=op_service.list_alle_positionen(konto.id), heute=heute,
    )
    assert [(p.von, p.bis, p.rest_cent) for p in perioden] == [(date(2026, 10, 16), date(2026, 10, 20), 800)]
    vorschau = _vorschau(op_service, konto, heute)
    assert vorschau.zins_segmente == ()
    assert vorschau.gebuehr_segmente == ()


def test_erstattung_nach_faelligkeit_offener_miete_bleibt_fifo(op_service, basis_vertrag, ctx_factory):
    """Dokumentierte Abweichung: Das Guthaben mindert zuerst die aeltere Miete, die KORREKTUR bleibt offen."""
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    op_service.eroeffnen_gesamtsaldo(
        ctx=ctx, konto=konto, betrag_cent=-800, stichtag=date(2026, 8, 31), import_id="ERO-GUTHABEN", akteur="test",
    )
    oktober = _buche(
        op_service, ctx, konto, OPTyp.SOLL, 10_000, date(2026, 10, 1), "Miete Oktober",
        faelligkeit=date(2026, 10, 5), periode="2026-10",
    )
    korrektur = _buche(op_service, ctx, konto, OPTyp.KORREKTUR, 800, date(2026, 10, 8), "Erstattung nach Faelligkeit")

    saldo = op_service.berechne_saldo(konto.id, stichtag=date(2026, 10, 20))
    assert saldo.saldo_cent == 10_000
    assert saldo.faelliger_unstrittiger_rest_cent == 9_200
    assert _reste(op_service, konto) == {oktober.id: 9_200, korrektur.id: 800}
