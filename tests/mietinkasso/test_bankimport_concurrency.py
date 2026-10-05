"""Independent regression checks: file-SQLite concurrency and source changes."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy.orm import sessionmaker

from mietinkasso.auth.service import AuthContext
from mietinkasso.bank import service as bank_module
from mietinkasso.bank.importer import CsvSpaltenMapping
from mietinkasso.bank.repository import BankRepository
from mietinkasso.bank.service import BankImportService
from mietinkasso.domain.enums import Rolle
from mietinkasso.domain.exceptions import MietinkassoError
from mietinkasso.infrastructure.db.base import Base
from mietinkasso.infrastructure.db.session import build_engine
from mietinkasso.infrastructure.db.tables import BankKontoTable, BankTransaktionTable
from mietinkasso.op.repository import OPRepository
from mietinkasso.op.service import OPService
from mietinkasso.stammdaten.repository import StammdatenRepository

IBAN = 'AT000000000000000000'
CSV = 'amount,date,id\n10.00,2026-01-02,SYN-ID-1\n'
MAPPING = CsvSpaltenMapping('amount', 'date', eindeutige_referenz='id')
XML = f'<Document><BkToCstmrStmt><Stmt><Acct><Id><IBAN>{IBAN}</IBAN></Id></Acct><Ntry><Amt Ccy="EUR">10.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><BookgDt><Dt>2026-01-02</Dt></BookgDt><NtryDtls><TxDtls><AcctSvcrRef>SYN-ID-1</AcctSvcrRef></TxDtls></NtryDtls></Ntry></Stmt></BkToCstmrStmt></Document>'.encode()


@pytest.fixture
def import_env(tmp_path):
    engine = build_engine(f'sqlite:///{(tmp_path / "synthetic.sqlite").as_posix()}')
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, expire_on_commit=False)
    sr = StammdatenRepository(sf)
    for company in ('SYN-A', 'SYN-B'):
        sr.upsert_gesellschaft(id=company, name=company)
    br = BankRepository(sf)
    br.upsert_bank_konto(id='TEST-BANK', gesellschaft_id='SYN-A', iban=IBAN, bezeichnung='Synthetic')
    bs = BankImportService(br, sr, OPService(OPRepository(sf), sr))
    ctx = AuthContext(user_id='test', rolle=Rolle.BUCHHALTUNG, gesellschaft_ids=frozenset({'SYN-A'}))
    yield sf, br, bs, ctx, br.get_bank_konto('TEST-BANK')
    engine.dispose()


def test_simultaneous_csv_and_camt_are_one_persisted_transaction(import_env, monkeypatch):
    sf, br, bs, ctx, bank = import_env
    barrier = Barrier(2)
    parse_csv = bank_module.parse_csv
    parse_camt = bank_module.parse_camt053

    def synchronized(parser):
        def wrapped(*args, **kwargs):
            rows = parser(*args, **kwargs)
            barrier.wait(timeout=10)
            return rows
        return wrapped

    monkeypatch.setattr(bank_module, 'parse_csv', synchronized(parse_csv))
    monkeypatch.setattr(bank_module, 'parse_camt053', synchronized(parse_camt))
    with ThreadPoolExecutor(max_workers=2) as pool:
        csv_result = pool.submit(bs.importiere_csv, ctx=ctx, bank_konto=bank, text=CSV, mapping=MAPPING)
        camt_result = pool.submit(bs.importiere_camt053, ctx=ctx, bank_konto=bank, xml_bytes=XML)
        assert csv_result.result(timeout=15)[0].id == camt_result.result(timeout=15)[0].id
    with sf() as session:
        assert session.query(BankTransaktionTable).count() == 1


@pytest.mark.parametrize('source', ['csv', 'camt'])
@pytest.mark.parametrize('change', ['iban', 'gesellschaft_id', 'deleted'])
def test_changed_source_after_parsing_prevents_any_write(import_env, monkeypatch, source, change):
    sf, br, bs, ctx, bank = import_env
    name = 'parse_csv' if source == 'csv' else 'parse_camt053'
    original = getattr(bank_module, name)

    def source_changed(*args, **kwargs):
        rows = original(*args, **kwargs)
        with sf() as session:
            row = session.get(BankKontoTable, bank.id)
            if change == 'deleted':
                session.delete(row)
            elif change == 'iban':
                row.iban = 'AT000000000000000001'
            else:
                row.gesellschaft_id = 'SYN-B'
            session.commit()
        return rows

    monkeypatch.setattr(bank_module, name, source_changed)
    with pytest.raises(MietinkassoError):
        if source == 'csv':
            bs.importiere_csv(ctx=ctx, bank_konto=bank, text=CSV, mapping=MAPPING)
        else:
            bs.importiere_camt053(ctx=ctx, bank_konto=bank, xml_bytes=XML)
    with sf() as session:
        assert session.query(BankTransaktionTable).count() == 0
