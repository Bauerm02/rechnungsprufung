import pytest

from mietinkasso.bank.importer import CsvSpaltenMapping
from mietinkasso.bank.repository import BankRepository
from mietinkasso.bank.service import BankImportService


def _bank(session_factory, stammdaten_repo, op_service, ctx, referenz):
    repo = BankRepository(session_factory)
    service = BankImportService(repo, stammdaten_repo, op_service)
    repo.upsert_bank_konto(
        id="BK-REVIEW", gesellschaft_id="7DI",
        iban="AT000000000000000000", bezeichnung="Synthetic review",
    )
    rows = service.importiere_csv(
        ctx=ctx, bank_konto=repo.get_bank_konto("BK-REVIEW"),
        text=f"betrag,datum,referenz\n600.00,2026-09-20,{referenz}\n",
        mapping=CsvSpaltenMapping(betrag="betrag", buchungsdatum="datum", referenz="referenz"),
    )
    return repo, service, rows[0]


def test_mehrere_vertragskennungen_duerfen_keine_automatische_buchung_ausloesen(
    session_factory, stammdaten_repo, op_service, basis_vertrag, ctx_factory,
):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    repo, service, row = _bank(
        session_factory, stammdaten_repo, op_service, ctx,
        "VERTRAG:V-601-3 und VERTRAG:V-601-4",
    )
    result = service.automatisch_zuordnen(ctx=ctx, transaktion=row)
    assert result.zugeordnet is False
    assert op_service.berechne_saldo(konto.id).saldo_cent == 0
    assert repo.list_zuordnungen(row.id) == []


def test_laengere_vertragskennung_blockiert_nicht_die_mahnung_des_praefixes(
    session_factory, stammdaten_repo, op_service, basis_vertrag, ctx_factory,
):
    _ = basis_vertrag
    repo, _, _ = _bank(
        session_factory, stammdaten_repo, op_service, ctx_factory("7DI"),
        "VERTRAG:V-601-3-NACHFOLGER",
    )
    assert repo.hat_ungeklaerte_relevante_eingaenge(
        bank_konto_id="BK-REVIEW", vertrag_id="V-601-3",
    ) is False
    assert repo.hat_ungeklaerte_relevante_eingaenge(
        bank_konto_id="BK-REVIEW", vertrag_id="V-601-3-NACHFOLGER",
    ) is True


@pytest.mark.parametrize("referenz", [
    "VERTRAG:V-601-3 VERTRAG:UNBEKANNT",
    "VERTRAG:UNBEKANNT VERTRAG:V-601-3",
    "KEINVERTRAG:V-601-3",
    "VERTRAG:V-601-3_NACHFOLGER",
    "VERTRAG:V-601-3ä",
    "VERTRAG:V-601-3 VERTRAG:",
])
def test_unklare_referenz_auch_in_vorschau_ohne_buchung(
    session_factory, stammdaten_repo, op_service, basis_vertrag, ctx_factory, referenz,
):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    repo, service, row = _bank(session_factory, stammdaten_repo, op_service, ctx, referenz)
    assert service.schlage_konto_vor(row)[0] is None
    assert service.automatisch_zuordnen(ctx=ctx, transaktion=row).zugeordnet is False
    assert op_service.berechne_saldo(konto.id).saldo_cent == 0
    assert repo.list_zuordnungen(row.id) == []


def test_wiederholte_einzelkennung_bucht_einmal_und_replay_bleibt_wirkungslos(
    session_factory, stammdaten_repo, op_service, basis_vertrag, ctx_factory,
):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    repo, service, row = _bank(
        session_factory, stammdaten_repo, op_service, ctx,
        "VERTRAG:V-601-3 / VERTRAG:V-601-3",
    )
    assert service.automatisch_zuordnen(ctx=ctx, transaktion=row).zugeordnet is True
    assert service.automatisch_zuordnen(ctx=ctx, transaktion=row).zugeordnet is False
    assert len(repo.list_zuordnungen(row.id)) == 1
    assert op_service.berechne_saldo(konto.id).saldo_cent == -60000
