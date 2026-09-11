from __future__ import annotations

from pathlib import Path

import pytest

from mietinkasso.domain.exceptions import ImportConflictError
from mietinkasso.op.eroeffnung_import import (
    importiere_eroeffnung_csv,
    importiere_eroeffnung_csv_atomar,
    parse_eroeffnung_csv,
)

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


def test_atomarer_import_gueltige_zeile_gefolgt_von_unbekanntem_konto_bucht_nichts(
    session_factory, op_service, stammdaten_repo, basis_vertrag, ctx_factory
):
    """Regression (Codex-Gegenprobe): eine gültige 100-EUR-Zeile gefolgt
    von einer Zeile mit unbekanntem Konto darf NICHT die erste Zeile
    stehen lassen - der gesamte Import ist EINE Transaktion. Deckt auch
    ab, dass eine innerhalb des Imports geöffnete Stammdaten-Prüfung
    (Objekt-107-Ausschluss) nicht versehentlich eine separate Session
    öffnet, die bei SQLite :memory:/StaticPool die äußere, noch nicht
    committete Buchung zurückrollen würde, ohne dass ein Fehler vorliegt -
    hier MUSS der Fehler (unbekanntes Konto) selbst zum Rollback führen."""

    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    text = (
        f"konto_id,modus,betrag,stichtag,import_id,beleg_referenz\n"
        f"{konto.id},GESAMTSALDO,100.00,2026-01-01,ID-1,ok\n"
        f"UNBEKANNT,GESAMTSALDO,50.00,2026-01-01,ID-2,unbekannt\n"
    )

    with pytest.raises(ValueError):
        importiere_eroeffnung_csv_atomar(
            ctx=ctx, op_service=op_service, text=text, konten_je_id={konto.id: konto},
            akteur="test", session_factory=session_factory,
        )

    assert op_service.berechne_saldo(konto.id).saldo_cent == 0
    assert op_service.bestehende_eroeffnung(konto.id) is None
    assert stammdaten_repo.get_konto(konto.id).eroeffnung_modus is None
    assert konto.eroeffnung_modus is None  # auch das übergebene Objekt bleibt unverändert


def test_atomarer_import_zwei_einzel_op_mit_gleicher_import_id_bucht_nichts(
    session_factory, op_service, stammdaten_repo, basis_vertrag, ctx_factory
):
    """Regression (Codex-Gegenprobe): zwei EINZEL_OP-Zeilen (100/200 EUR)
    mit DERSELBEN import_id in einer Datei sind ein Konflikt (nicht ein
    stiller Replay, da unterschiedlicher Betrag) und dürfen NICHTS
    verbuchen - auch nicht die erste, an sich gültige Zeile."""

    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    text = (
        "konto_id,modus,betrag,stichtag,typ,belegdatum,faelligkeit,import_id,beleg_referenz\n"
        f"{konto.id},EINZEL_OP,100.00,2026-01-01,SOLL,2026-01-01,2026-01-05,SAME-ID,eins\n"
        f"{konto.id},EINZEL_OP,200.00,2026-01-01,SOLL,2026-01-01,2026-01-05,SAME-ID,zwei\n"
    )

    with pytest.raises(ImportConflictError):
        importiere_eroeffnung_csv_atomar(
            ctx=ctx, op_service=op_service, text=text, konten_je_id={konto.id: konto},
            akteur="test", session_factory=session_factory,
        )

    assert op_service.berechne_saldo(konto.id).saldo_cent == 0
    assert len(op_service.list_alle_positionen(konto.id)) == 0
    assert stammdaten_repo.get_konto(konto.id).eroeffnung_modus is None
    assert konto.eroeffnung_modus is None
