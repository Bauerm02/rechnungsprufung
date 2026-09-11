from __future__ import annotations

from datetime import date

import pytest

from mietinkasso.bank.importer import CsvSpaltenMapping
from mietinkasso.bank.repository import BankRepository
from mietinkasso.bank.service import BankImportService
from mietinkasso.domain.enums import OPTyp
from mietinkasso.domain.exceptions import CrossTenantError, ImportConflictError
from mietinkasso.op.repository import OPRepository
from mietinkasso.op.service import OPService


@pytest.fixture
def bank_service(session_factory, stammdaten_repo) -> BankImportService:
    op_service = OPService(OPRepository(session_factory), stammdaten_repo)
    return BankImportService(BankRepository(session_factory), stammdaten_repo, op_service)


@pytest.fixture
def bank_repo(session_factory) -> BankRepository:
    return BankRepository(session_factory)


CAMT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02">
  <BkToCstmrStmt>
    <Stmt>
      <Ntry>
        <Amt Ccy="EUR">600.00</Amt>
        <CdtDbtInd>CRDT</CdtDbtInd>
        <BookgDt><Dt>2026-04-06</Dt></BookgDt>
        <ValDt><Dt>2026-04-06</Dt></ValDt>
        <NtryDtls>
          <TxDtls>
            <RmtInf><Ustrd>VERTRAG:V-601-3 Miete April</Ustrd></RmtInf>
            <RltdPties><Dbtr><Nm>Max Mustermieter</Nm></Dbtr></RltdPties>
            <AcctSvcrRef>REF-0001</AcctSvcrRef>
          </TxDtls>
        </NtryDtls>
      </Ntry>
    </Stmt>
  </BkToCstmrStmt>
</Document>
"""


def test_camt053_import_und_eindeutige_automatische_zuordnung(bank_service, bank_repo, stammdaten_repo, basis_vertrag, ctx_factory):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    bank_repo.upsert_bank_konto(id="BK-7DI-1", gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="7DI Hauptkonto")
    bank_konto = bank_repo.get_bank_konto("BK-7DI-1")

    transaktionen = bank_service.importiere_camt053(ctx=ctx, bank_konto=bank_konto, xml_bytes=CAMT_XML.encode("utf-8"))
    assert len(transaktionen) == 1
    transaktion = transaktionen[0]
    assert transaktion.betrag_cent == 60_000

    ergebnis = bank_service.automatisch_zuordnen(ctx=ctx, transaktion=transaktion)
    assert ergebnis.zugeordnet is True

    saldo = OPService(OPRepository(bank_service._op_service._op_repository._session_factory), stammdaten_repo).berechne_saldo(konto.id)
    assert saldo.saldo_cent == -60_000  # reine Zahlung ohne vorherige Sollstellung -> Guthaben


def test_camt053_doppelimport_ist_wirkungslos(bank_service, bank_repo, ctx_factory):
    ctx = ctx_factory("7DI")
    bank_repo.upsert_bank_konto(id="BK-7DI-1", gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="7DI")
    bank_konto = bank_repo.get_bank_konto("BK-7DI-1")
    bank_service.importiere_camt053(ctx=ctx, bank_konto=bank_konto, xml_bytes=CAMT_XML.encode("utf-8"))
    transaktionen = bank_service.importiere_camt053(ctx=ctx, bank_konto=bank_konto, xml_bytes=CAMT_XML.encode("utf-8"))
    assert len(transaktionen) == 1
    unzugeordnet_oder_zugeordnet_count = len(bank_repo.list_unzugeordnet(bank_konto.id))
    assert unzugeordnet_oder_zugeordnet_count == 1  # nicht zwei Zeilen durch den zweiten Import


def test_keine_automatische_zuordnung_ohne_eindeutige_referenz(bank_service, bank_repo, basis_vertrag, ctx_factory):
    """Name/gleicher Betrag allein reichen nicht für eine automatische Zuordnung."""

    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    bank_repo.upsert_bank_konto(id="BK-7DI-1", gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="7DI")
    bank_konto = bank_repo.get_bank_konto("BK-7DI-1")
    csv_text = "betrag,datum,referenz\n600.00,2026-04-06,Miete Max Mustermieter\n"
    mapping = CsvSpaltenMapping(betrag="betrag", buchungsdatum="datum", referenz="referenz")
    transaktionen = bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=csv_text, mapping=mapping)
    ergebnis = bank_service.automatisch_zuordnen(ctx=ctx, transaktion=transaktionen[0])
    assert ergebnis.zugeordnet is False


def test_cross_tenant_zuordnung_wird_db_seitig_blockiert(bank_service, bank_repo, stammdaten_repo, ctx_factory):
    stammdaten_repo.upsert_gesellschaft(id="7DI", name="7D Immobilien GmbH")
    stammdaten_repo.upsert_gesellschaft(id="MABAU", name="MaBau Beteiligungs GmbH")
    stammdaten_repo.upsert_objekt(id="616", gesellschaft_id="MABAU", bezeichnung="Fockygasse")
    stammdaten_repo.upsert_einheit(id="616-TOP1", objekt_id="616", bezeichnung="Top 1", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten_repo.upsert_debitor(id="DEB-2", name="Andere Mieterin")
    stammdaten_repo.upsert_vertrag(
        id="V-616-1", einheit_id="616-TOP1", debitor_id="DEB-2", gesellschaft_id="MABAU",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    vertrag_mabau = stammdaten_repo.get_vertrag("V-616-1")
    konto_mabau = stammdaten_repo.get_or_create_konto(vertrag=vertrag_mabau)

    bank_repo.upsert_bank_konto(id="BK-7DI-1", gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="7DI")
    bank_konto_7di = bank_repo.get_bank_konto("BK-7DI-1")
    ctx_7di = ctx_factory("7DI")
    csv_text = "betrag,datum,referenz\n600.00,2026-04-06,sonstiges\n"
    mapping = CsvSpaltenMapping(betrag="betrag", buchungsdatum="datum", referenz="referenz")
    transaktion = bank_service.importiere_csv(ctx=ctx_7di, bank_konto=bank_konto_7di, text=csv_text, mapping=mapping)[0]

    with pytest.raises(CrossTenantError):
        bank_repo.create_zuordnung(
            bank_transaktion_id=transaktion.id, op_position_id=_dummy_op_position(bank_service, konto_mabau, ctx_factory), betrag_cent=60_000, match_typ="MANUELL"
        )


def _dummy_op_position(bank_service, konto_mabau, ctx_factory):
    ctx_mabau = ctx_factory("MABAU")
    row = bank_service._op_service.buchen(
        ctx=ctx_mabau,
        konto=konto_mabau,
        typ=OPTyp.SOLL,
        betrag_cent=60_000,
        belegdatum=date(2026, 4, 1),
        buchungsdatum=date(2026, 4, 1),
        faelligkeit=date(2026, 4, 5),
        beleg_referenz="Miete April MaBau",
    )
    return row.id


def test_ruecklastschrift_ueber_bank_macht_op_wieder_offen(bank_service, bank_repo, stammdaten_repo, basis_vertrag, ctx_factory):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    bank_repo.upsert_bank_konto(id="BK-7DI-1", gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="7DI")
    bank_konto = bank_repo.get_bank_konto("BK-7DI-1")

    csv_text = f"betrag,datum,referenz\n600.00,2026-04-06,VERTRAG:{vertrag.id}\n"
    mapping = CsvSpaltenMapping(betrag="betrag", buchungsdatum="datum", referenz="referenz")
    transaktion = bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=csv_text, mapping=mapping)[0]
    ergebnis = bank_service.automatisch_zuordnen(ctx=ctx, transaktion=transaktion)
    assert ergebnis.zugeordnet is True

    zahlung_op = bank_service._op_service._op_repository.get(ergebnis.op_position_id)
    rueck_transaktion_csv = f"betrag,datum,referenz\n-600.00,2026-04-10,RUECKLASTSCHRIFT VERTRAG:{vertrag.id}\n"
    rueck_transaktion = bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=rueck_transaktion_csv, mapping=mapping)[0]

    bank_service.verarbeite_ruecklastschrift(ctx=ctx, transaktion=rueck_transaktion, original_op_position=zahlung_op, konto=konto)

    saldo = bank_service._op_service.berechne_saldo(konto.id)
    assert saldo.saldo_cent == 0  # Zahlung (-600) + Rücklastschrift (+600) gleichen sich wieder aus -> OP wieder offen


def test_bankvollstaendigkeit_basiert_auf_buchungsdatum_nicht_auf_mtime(bank_service, bank_repo, ctx_factory):
    ctx = ctx_factory("7DI")
    bank_repo.upsert_bank_konto(id="BK-7DI-1", gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="7DI")
    bank_konto = bank_repo.get_bank_konto("BK-7DI-1")
    assert bank_service.bankstand_alter_tage(bank_konto.id, heute=date(2026, 4, 10)) is None

    csv_text = "betrag,datum,referenz\n100.00,2026-04-06,sonstiges\n"
    mapping = CsvSpaltenMapping(betrag="betrag", buchungsdatum="datum", referenz="referenz")
    bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=csv_text, mapping=mapping)

    alter = bank_service.bankstand_alter_tage(bank_konto.id, heute=date(2026, 4, 10))
    assert alter == 4
