"""Independent acceptance probes for bank-source binding."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from datetime import date

import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import sessionmaker

from mietinkasso.auth.service import AuthContext
from mietinkasso.bank import service as import_module
from mietinkasso.bank.importer import CsvSpaltenMapping
from mietinkasso.bank.quellenbindung import QuellenbindungService
from mietinkasso.bank.quellen_models import BankQuelleTable, BankQuellenBindungTable
from mietinkasso.bank.repository import BankRepository
from mietinkasso.bank.service import BankImportService
from mietinkasso.domain.enums import Rolle, OPTyp
from mietinkasso.domain.exceptions import MietinkassoError
from mietinkasso.infrastructure.db.base import Base
from mietinkasso.infrastructure.db.session import build_engine
from mietinkasso.infrastructure.db.tables import AuditEventTable, BankTransaktionTable
from mietinkasso.op.repository import OPRepository
from mietinkasso.op.service import OPService
from mietinkasso.stammdaten.repository import StammdatenRepository

IBANS = {'B1': 'AT000000000000000001', 'B2': 'AT000000000000000002'}
MAPPING = CsvSpaltenMapping('amount', 'date', eindeutige_referenz='id', eigene_iban='own')
CSV = 'amount,date,id,own\n10.00,2026-01-02,SYN-ID-1,'+IBANS['B1']+'\n'


@pytest.fixture
def env(tmp_path):
    engine = build_engine(f'sqlite:///{(tmp_path / "review.sqlite").as_posix()}')
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, expire_on_commit=False)
    sr = StammdatenRepository(sf)
    sr.upsert_gesellschaft(id='SYN', name='Synthetic company')
    for oid in ('OBJ', '107'):
        sr.upsert_objekt(id=oid, gesellschaft_id='SYN', bezeichnung='Synthetic '+oid, ausgeschlossen=False)
    br = BankRepository(sf)
    for key, iban in IBANS.items():
        br.upsert_bank_konto(id=key, gesellschaft_id='SYN', iban=iban, bezeichnung=key)
    qs = QuellenbindungService(sf)
    bs = BankImportService(br, sr, OPService(OPRepository(sf), sr))
    ctx = AuthContext(user_id='test', rolle=Rolle.BUCHHALTUNG, gesellschaft_ids=frozenset({'SYN'}))
    yield sf, br, qs, bs, ctx
    engine.dispose()


def bind(env, *, bank='B1', oid='OBJ', expected='NEU'):
    sf, br, qs, bs, ctx = env
    return qs.binden(ctx=ctx, objekt_id=oid, anbieter='SYN-PROVIDER', zugang_ref='SYN-ACCESS',
                     konto_ref='SYN-ACCOUNT-'+bank, bank_konto_id=bank, gesellschaft_id='SYN',
                     iban=IBANS[bank], kontorolle='MIETE', nachweis_ref='Synthetic proof '+bank,
                     erwarteter_stand=expected)


def counts(env):
    with env[0]() as s:
        return [s.scalar(select(func.count()).select_from(model)) for model in
                (BankQuelleTable, BankQuellenBindungTable, AuditEventTable, BankTransaktionTable)]


def test_object_107_never_bindable_even_if_exclusion_flag_was_lost(env):
    with pytest.raises(MietinkassoError):
        bind(env, oid='107')
    assert counts(env) == [0, 0, 0, 0]


def test_two_concurrent_initial_bindings_cannot_both_win_or_leave_orphan_sources(env):
    barrier = Barrier(2)
    def attempt(bank):
        barrier.wait(timeout=10)
        try:
            return bind(env, bank=bank)
        except MietinkassoError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(attempt, 'B1'), pool.submit(attempt, 'B2')
        results = [a.result(timeout=15), b.result(timeout=15)]
    assert sum(r is not None for r in results) == 1
    assert counts(env) == [1, 1, 1, 0]


def test_binding_created_during_legacy_parse_closes_unbound_import_bypass(env, monkeypatch):
    sf, br, qs, bs, ctx = env
    original = import_module.parse_csv
    def wrapped(*args, **kwargs):
        rows = original(*args, **kwargs)
        bind(env)
        return rows
    monkeypatch.setattr(import_module, 'parse_csv', wrapped)
    with pytest.raises(MietinkassoError):
        bs.importiere_csv(ctx=ctx, bank_konto=br.get_bank_konto('B1'), text=CSV, mapping=MAPPING)
    assert counts(env) == [1, 1, 1, 0]


@pytest.mark.parametrize('change', ['revoke', 'rebind'])
def test_revision_change_after_parsing_rolls_back_import_and_preserves_old_bank_protection(env, monkeypatch, change):
    sf, br, qs, bs, ctx = env
    context = bind(env)
    original = import_module.parse_csv
    def wrapped(*args, **kwargs):
        rows = original(*args, **kwargs)
        if change == 'revoke':
            qs.widerrufen(ctx=ctx, objekt_id='OBJ', nachweis_ref='Synthetic revoked', erwarteter_stand=context.token)
        else:
            bind(env, bank='B2', expected=context.token)
        return rows
    monkeypatch.setattr(import_module, 'parse_csv', wrapped)
    with pytest.raises(MietinkassoError):
        bs.importiere_csv(ctx=ctx, bank_konto=br.get_bank_konto('B1'), text=CSV, mapping=MAPPING, quellen_kontext=context)
    monkeypatch.setattr(import_module, 'parse_csv', original)
    with pytest.raises(MietinkassoError):
        bs.importiere_csv(ctx=ctx, bank_konto=br.get_bank_konto('B1'), text=CSV, mapping=MAPPING)
    assert counts(env)[-1] == 0


def test_stale_initial_form_cannot_rebind_an_existing_object(env):
    context = bind(env)
    before = counts(env)
    with pytest.raises(MietinkassoError):
        bind(env, bank='B2', expected='NEU')
    assert counts(env) == before
    assert env[2].kontext_fuer_objekt(ctx=env[4], objekt_id='OBJ') == context


def test_revocation_during_account_metadata_read_prevents_transaction_fetch(env):
    from mietinkasso.bank.quellenabruf import GeschuetzterBankabruf, AnbieterKontoInfo
    sf, br, qs, bs, ctx = env
    context = bind(env)
    class Adapter:
        transactions_called = False
        def kontoinfo(self, **request):
            qs.widerrufen(ctx=ctx, objekt_id='OBJ', erwarteter_stand=context.token, nachweis_ref='Synthetic revoke')
            return AnbieterKontoInfo(**request, iban=IBANS['B1'], kontorolle='MIETE')
        def umsaetze_abrufen(self, **request):
            self.transactions_called = True
            raise AssertionError('Transactions must not be requested after observed revocation.')
    adapter = Adapter()
    with pytest.raises(MietinkassoError):
        GeschuetzterBankabruf(bs, br).abrufen(ctx=ctx, objekt_id='OBJ', adapter=adapter,
                                           von=date(2026,1,1), bis=date(2026,1,31))
    assert not adapter.transactions_called
    assert counts(env)[-1] == 0


def test_direct_repository_assignment_cannot_bypass_wrong_bank_guard(env):
    sf, br, qs, bs, ctx = env
    sr = StammdatenRepository(sf)
    sr.upsert_einheit(id='UNIT', objekt_id='OBJ', bezeichnung='Synthetic unit', nutzungsstatus='DAUERVERMIETUNG')
    sr.upsert_debitor(id='DEB', name='Synthetic tenant', email='synthetic@example.invalid')
    sr.upsert_vertrag(id='V', einheit_id='UNIT', debitor_id='DEB', gesellschaft_id='SYN',
                     rechtsordnung='OESTERREICH_MRG_VOLL', gueltig_von=date(2026,1,1))
    tenant_account = sr.get_or_create_konto(vertrag=sr.get_vertrag('V'))
    tx = bs.importiere_csv(ctx=ctx, bank_konto=br.get_bank_konto('B2'),
                           text=CSV.replace(IBANS['B1'],IBANS['B2']), mapping=MAPPING)[0]
    op = OPService(OPRepository(sf), sr).buchen(ctx=ctx, konto=tenant_account, typ=OPTyp.ZAHLUNG,
        betrag_cent=1000, belegdatum=date(2026,1,2), buchungsdatum=date(2026,1,2), faelligkeit=None,
        beleg_referenz='Synthetic existing payment', import_id='SYN-OP', quelle_system='review')
    bind(env)
    with pytest.raises(MietinkassoError):
        br.create_zuordnung(bank_transaktion_id=tx.id, op_position_id=op.id, betrag_cent=1000,
                            match_typ='MANUELL', vorgang_id='SYN-LINK')
    assert br.zugeordneter_betrag(tx.id) == 0
