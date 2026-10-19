"""Auftrag HV-20261019-MAHNKOSTEN: synthetische Tests für die getrennte
Rücklastschriftkosten-Erfassung (`bank/ruecklastschriftkosten_service.py`)
- bewusst GETRENNT von der Rückbuchung selbst (`test_bank.py`), prüft nur
das tatsächliche Bankentgelt, Zurechenbarkeit und Doppelbuchungsschutz."""

from __future__ import annotations

from datetime import date

import pytest

from mietinkasso.bank.importer import CsvSpaltenMapping
from mietinkasso.bank.repository import BankRepository
from mietinkasso.bank.ruecklastschriftkosten_service import RuecklastschriftkostenService
from mietinkasso.bank.service import BankImportService
from mietinkasso.domain.enums import OPTyp
from mietinkasso.op.repository import OPRepository
from mietinkasso.op.service import OPService


@pytest.fixture
def bank_service(session_factory, stammdaten_repo) -> BankImportService:
    op_service = OPService(OPRepository(session_factory), stammdaten_repo)
    return BankImportService(BankRepository(session_factory), stammdaten_repo, op_service)


@pytest.fixture
def bank_repo(session_factory) -> BankRepository:
    return BankRepository(session_factory)


@pytest.fixture
def rls_kosten_service(session_factory, op_service) -> RuecklastschriftkostenService:
    return RuecklastschriftkostenService(session_factory=session_factory, op_service=op_service)


def _ruecklastschrift_op(bank_service, bank_repo, vertrag, konto, ctx):
    bank_repo.upsert_bank_konto(id="BK-7DI-1", gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="7DI")
    bank_konto = bank_repo.get_bank_konto("BK-7DI-1")
    mapping = CsvSpaltenMapping(betrag="betrag", buchungsdatum="datum", referenz="referenz")

    csv_text = f"betrag,datum,referenz\n600.00,2026-04-06,VERTRAG:{vertrag.id}\n"
    transaktion = bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=csv_text, mapping=mapping)[0]
    ergebnis = bank_service.automatisch_zuordnen(ctx=ctx, transaktion=transaktion)
    assert ergebnis.zugeordnet is True
    zahlung_op = bank_service._op_service._op_repository.get(ergebnis.op_position_id)

    rueck_csv = f"betrag,datum,referenz\n-600.00,2026-04-10,RUECKLASTSCHRIFT VERTRAG:{vertrag.id}\n"
    rueck_transaktion = bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=rueck_csv, mapping=mapping)[0]
    return bank_service.verarbeite_ruecklastschrift(
        ctx=ctx, transaktion=rueck_transaktion, original_op_position=zahlung_op, konto=konto
    )


def test_zurechenbare_ruecklastschriftkosten_werden_als_forderung_gebucht(
    bank_service, bank_repo, basis_vertrag, ctx_factory, rls_kosten_service,
):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    rls_op = _ruecklastschrift_op(bank_service, bank_repo, vertrag, konto, ctx)

    row = rls_kosten_service.erfasse_und_buche(
        ctx=ctx, ruecklastschrift_op_position=rls_op, vertrag_id=vertrag.id, konto=konto,
        bankentgelt_cent=1250, rueckgabeursache="Mangels Deckung",
        dem_zahlungspflichtigen_zurechenbar=True,
        zurechenbarkeit_begruendung="Dauerhaft fehlende Kontodeckung lt. Bankrückmeldung, vom Mieter zu vertreten.",
        beleg_referenz="Bankbeleg RLS-001", heute=date(2026, 4, 11), akteur="codex-test",
    )

    assert row.bankentgelt_cent == 1250
    assert row.kosten_op_position_id is not None
    gebuchte_position = bank_service._op_service._op_repository.get(row.kosten_op_position_id)
    assert gebuchte_position.typ == OPTyp.SOLL.value
    assert gebuchte_position.betrag_cent == 1250
    # KEINE `bezieht_sich_auf_id` auf die Rücklastschrift: eine explizite
    # OP-Bindung ist laut `op/validierung.py::pruefe_bezug` nur für
    # ZAHLUNG/GUTSCHRIFT (-> Forderung) und RUECKLASTSCHRIFT (-> Zahlung)
    # definiert, NICHT für SOLL. Die Rückverfolgbarkeit läuft stattdessen
    # über `row.kosten_op_position_id`/`row.ruecklastschrift_op_position_id`.
    assert gebuchte_position.bezieht_sich_auf_id is None
    assert row.ruecklastschrift_op_position_id == rls_op.id


def test_nicht_zurechenbare_ruecklastschriftkosten_bleiben_rein_dokumentarisch(
    bank_service, bank_repo, basis_vertrag, ctx_factory, rls_kosten_service,
):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    rls_op = _ruecklastschrift_op(bank_service, bank_repo, vertrag, konto, ctx)

    row = rls_kosten_service.erfasse_und_buche(
        ctx=ctx, ruecklastschrift_op_position=rls_op, vertrag_id=vertrag.id, konto=konto,
        bankentgelt_cent=1250, rueckgabeursache="Bankseitiger Fehler lt. Bankauskunft",
        dem_zahlungspflichtigen_zurechenbar=False,
        zurechenbarkeit_begruendung="Bank bestätigt eigenen Verarbeitungsfehler, Mieter trifft kein Verschulden.",
        beleg_referenz="Bankbeleg RLS-002", heute=date(2026, 4, 11), akteur="codex-test",
    )

    assert row.kosten_op_position_id is None  # keine Forderung ohne belegte Zurechenbarkeit


def test_doppelte_erfassung_derselben_ruecklastschrift_bucht_nicht_doppelt(
    bank_service, bank_repo, basis_vertrag, ctx_factory, rls_kosten_service,
):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    rls_op = _ruecklastschrift_op(bank_service, bank_repo, vertrag, konto, ctx)

    kwargs = dict(
        ctx=ctx, ruecklastschrift_op_position=rls_op, vertrag_id=vertrag.id, konto=konto,
        bankentgelt_cent=1250, rueckgabeursache="Mangels Deckung",
        dem_zahlungspflichtigen_zurechenbar=True,
        zurechenbarkeit_begruendung="Dauerhaft fehlende Kontodeckung, vom Mieter zu vertreten.",
        beleg_referenz="Bankbeleg RLS-001", heute=date(2026, 4, 11), akteur="codex-test",
    )
    erste = rls_kosten_service.erfasse_und_buche(**kwargs)
    zweite = rls_kosten_service.erfasse_und_buche(**kwargs)

    assert erste.id == zweite.id
    saldo = bank_service._op_service.berechne_saldo(konto.id)
    # Rücklastschrift (+600) gleicht die ursprüngliche Zahlung (-600) aus,
    # genau EINE Bankentgelt-Forderung (+12,50) kommt dazu - kein zweiter
    # Ansatz durch den wiederholten Aufruf.
    assert saldo.saldo_cent == 1250


def test_ungueltiges_bankentgelt_wird_abgelehnt(bank_service, bank_repo, basis_vertrag, ctx_factory, rls_kosten_service):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    rls_op = _ruecklastschrift_op(bank_service, bank_repo, vertrag, konto, ctx)

    with pytest.raises(ValueError):
        rls_kosten_service.erfasse_und_buche(
            ctx=ctx, ruecklastschrift_op_position=rls_op, vertrag_id=vertrag.id, konto=konto,
            bankentgelt_cent=0, rueckgabeursache="Mangels Deckung",
            dem_zahlungspflichtigen_zurechenbar=True, zurechenbarkeit_begruendung="Begründung",
            beleg_referenz="Beleg", heute=date(2026, 4, 11), akteur="codex-test",
        )
