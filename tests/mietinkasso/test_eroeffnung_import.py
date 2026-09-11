from __future__ import annotations

from pathlib import Path

from mietinkasso.op.eroeffnung_import import importiere_eroeffnung_csv, parse_eroeffnung_csv

TEMPLATES_DIR = (
    Path(__file__).resolve().parents[2] / "src" / "mietinkasso" / "importtemplates"
)


def test_gesamtsaldo_vorlage_ist_parsebar_und_importierbar(op_service, stammdaten_repo, basis_vertrag, ctx_factory):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    text = (TEMPLATES_DIR / "eroeffnung_gesamtsaldo.csv").read_text(encoding="utf-8")
    text = text.replace("KTO-V-601-1", konto.id)

    zeilen = parse_eroeffnung_csv(text)
    assert len(zeilen) == 1
    assert zeilen[0].betrag_cent == 123_456

    ergebnisse = importiere_eroeffnung_csv(
        ctx=ctx, op_service=op_service, text=text, konten_je_id={konto.id: konto}, akteur="test"
    )
    assert len(ergebnisse) == 1
    saldo = op_service.berechne_saldo(konto.id)
    assert saldo.saldo_cent == 123_456

    # Wiederholimport mit identischer import_id bleibt wirkungslos
    importiere_eroeffnung_csv(ctx=ctx, op_service=op_service, text=text, konten_je_id={konto.id: konto}, akteur="test")
    assert op_service.berechne_saldo(konto.id).saldo_cent == 123_456


def test_einzel_op_vorlage_bucht_soll_und_zahlung(op_service, stammdaten_repo, basis_vertrag, ctx_factory):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    text = (TEMPLATES_DIR / "eroeffnung_einzel_op.csv").read_text(encoding="utf-8")
    text = text.replace("KTO-V-616-1", konto.id)

    importiere_eroeffnung_csv(ctx=ctx, op_service=op_service, text=text, konten_je_id={konto.id: konto}, akteur="test")
    saldo = op_service.berechne_saldo(konto.id)
    assert saldo.saldo_cent == 48_000 - 20_000
