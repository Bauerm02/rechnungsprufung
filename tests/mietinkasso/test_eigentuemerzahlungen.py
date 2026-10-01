from datetime import date
import json
from concurrent.futures import ThreadPoolExecutor
from xml.etree import ElementTree as ET

import pytest
from sqlalchemy import select
from mietinkasso.eigentuemerzahlungen.service import (
    save_profile, plan, generate, import_existing, execution_day, Profil, iban, NS,
)
from mietinkasso.eigentuemerzahlungen.models import Datei, Reservierung, Vorschrift
from mietinkasso.infrastructure.db.tables import GesellschaftTable, ObjektTable, BankKontoTable, BankTransaktionTable


@pytest.fixture
def owner(session_factory):
    with session_factory() as s:
        s.add(GesellschaftTable(id="OWNER",name="Synthetic Owner GmbH"))
        s.add(ObjektTable(id="617",gesellschaft_id="OWNER",bezeichnung="Testobjekt"))
        s.add(BankKontoTable(id="BANK-TEST",gesellschaft_id="OWNER",iban="AT611904300234573201",bezeichnung="Synthetic"))
        s.commit()
    return dict(kennung="TEST-UNIT",objekt_id="617",bank_konto_id="BANK-TEST",einheit="Test 1",
        empfaenger="Synthetic WEG",empfaenger_iban="DE89370400440532013000",empfaenger_bic="COBADEFFXXX",
        absender_bic="BKAUATWW",referenz="REF0001",betrag_cent=12345,gueltig_ab="2026-01-01",
        gueltig_bis="2026-12-31",zahlungstag=5,status="FREIGEGEBEN",hinweis="Synthetic confirmed source",
        quelle="synthetic.pdf",quelle_sha256="a"*64,start_monat="2026-10")


def save(sf,p,version=0):
    return save_profile(sf,p,actor="synthetic-test",expected_version=version)


def tx(sf,ref,amount=-12345,when=date(2026,10,1),native=True,beneficiary="DE89370400440532013000"):
    with sf() as s:
        s.add(BankTransaktionTable(bank_konto_id="BANK-TEST",betrag_cent=amount,waehrung="EUR",
            buchungsdatum=when,referenz=ref,gegenkonto_iban=beneficiary,quelle_typ="CSV",quelle_hash="b"*64,
            import_id=f"synthetic-{ref}-{amount}",hat_native_id=native))
        s.commit()


def test_monthly_idempotency_and_new_month(session_factory,owner):
    save(session_factory,owner)
    a=generate(session_factory,today=date(2026,10,1))
    b=generate(session_factory,today=date(2026,10,2))
    c=generate(session_factory,today=date(2026,11,1))
    assert len(a['neue_dateien'])==len(c['neue_dateien'])==1 and b['neue_dateien']==[]
    with session_factory() as s:
        f=s.get(Datei,a['neue_dateien'][0]);root=ET.fromstring(f.xml);ns={'p':NS}
        assert root.find('.//p:CtrlSum',ns).text=='123.45'
        assert root.find('.//p:ReqdExctnDt',ns).text=='2026-10-05'
        assert s.get(Datei,c['neue_dateien'][0]).xml!=f.xml
        assert len(list(s.scalars(select(Reservierung))))==2


@pytest.mark.parametrize('change,status',[
    ({'status':'KLAEREN'},'KLAEREN'),({'status':'AUSGESCHIEDEN','kostenende':'2026-09-30'},'AUSGESCHIEDEN'),
    ({'kostenende':'2026-09-30'},'AUSGESCHIEDEN'),({'kostenende':'2026-10-15'},'KLAEREN'),
    ({'gueltig_bis':'2026-09-30'},'KLAEREN'),({'gueltig_ab':'2026-10-15'},'KLAEREN'),
    ({'start_monat':'2026-11'},'NICHT_FAELLIG'),
])
def test_holds_and_dates(session_factory,owner,change,status):
    save(session_factory,owner|change)
    assert plan(session_factory,'2026-10')[0]['status']==status
    assert not generate(session_factory,today=date(2026,10,1))['neue_dateien']


def test_no_silent_rollover_or_late_payment(session_factory,owner):
    save(session_factory,owner)
    assert not generate(session_factory,today=date(2027,1,1))['neue_dateien']
    report=generate(session_factory,today=date(2026,10,6))
    assert not report['neue_dateien'] and 'verstrichen' in report['zeilen'][0]['hinweis']


def test_exact_prepayment_and_partial_payment(session_factory,owner):
    save(session_factory,owner)
    tx(session_factory,'REF0001 WEG 2026-10',amount=-5000,when=date(2026,9,20))
    row=plan(session_factory,'2026-10')[0]
    assert row['betrag_cent']==7345 and row['bezahlt_cent']==5000
    generate(session_factory,today=date(2026,10,1))
    tx(session_factory,'REF0001 WEG 10/2026',amount=-7345)
    assert 'geändert:' in plan(session_factory,'2026-10')[0]['hinweis']
    assert not generate(session_factory,today=date(2026,10,2))['neue_dateien']


@pytest.mark.parametrize('reference,native,status',[
    ('REF0001 WEG',True,'KLAEREN'),('REF0001 2026-10 2026-11',True,'KLAEREN'),
    ('REF0001 WEG 2026-10',False,'KLAEREN'),('REF0001 WEG 2026-10',True,'BEZAHLT'),
    ('REF0001 WEG 09/2026',True,'BEREIT'),('REF00010 WEG 2026-10',True,'BEREIT'),
])
def test_bank_periods_and_exact_reference(session_factory,owner,reference,native,status):
    save(session_factory,owner);tx(session_factory,reference,native=native)
    assert plan(session_factory,'2026-10')[0]['status']==status


def test_receipts_never_reduce_owner_payment(session_factory,owner):
    save(session_factory,owner);tx(session_factory,'REF0001 2026-10',amount=12345)
    assert plan(session_factory,'2026-10')[0]['betrag_cent']==12345


def test_future_version_keeps_previous_month(session_factory,owner):
    save(session_factory,owner)
    save(session_factory,owner|{'betrag_cent':20000,'gueltig_ab':'2026-11-01','quelle_sha256':'c'*64},1)
    assert plan(session_factory,'2026-10')[0]['soll_cent']==12345
    assert plan(session_factory,'2026-11')[0]['soll_cent']==20000
    save(session_factory,owner|{'betrag_cent':13000},2)
    assert plan(session_factory,'2026-10')[0]['soll_cent']==13000
    assert plan(session_factory,'2026-11')[0]['soll_cent']==20000


def test_hyphen_reference_is_not_prefix(session_factory,owner):
    save(session_factory,owner)
    tx(session_factory,'REF0001-2 WEG 2026-10')
    assert plan(session_factory,'2026-10')[0]['status']=='BEREIT'


def test_future_cost_end_preserves_full_month(session_factory,owner):
    save(session_factory,owner|{'status':'AUSGESCHIEDEN','kostenende':'2026-12-31'})
    assert plan(session_factory,'2026-10')[0]['status']=='BEREIT'
    assert plan(session_factory,'2026-12')[0]['status']=='BEREIT'
    assert plan(session_factory,'2027-01')[0]['status']=='AUSGESCHIEDEN'


def test_midmonth_replacement_is_held(session_factory,owner):
    save(session_factory,owner)
    save(session_factory,owner|{'gueltig_ab':'2026-10-15','betrag_cent':22222},1)
    assert plan(session_factory,'2026-10')[0]['status']=='KLAEREN'


def test_paid_own_export_is_not_a_false_change(session_factory,owner):
    save(session_factory,owner);generate(session_factory,today=date(2026,10,1))
    with session_factory() as s:
        snap=json.loads(s.scalar(select(Reservierung)).snapshot)
    tx(session_factory,'REF0001 WEG 2026-10 '+snap['end_to_end_id'])
    row=plan(session_factory,'2026-10')[0]
    assert row['status']=='BEZAHLT' and not row['datei_pruefen']


def test_optimistic_lock_and_identity_immutable(session_factory,owner):
    save(session_factory,owner)
    with pytest.raises(ValueError,match='Zwischenzeitlich'):
        save(session_factory,owner|{'betrag_cent':22222},0)
    with pytest.raises(ValueError,match='Identität'):
        save(session_factory,owner|{'referenz':'OTHER'},1)
    with pytest.raises(ValueError,match='bereits vorhanden'):
        save(session_factory,owner|{'kennung':'DUPLICATE'},0)


def test_excluded_and_wrong_entity(session_factory,owner):
    with session_factory() as s:
        s.get(ObjektTable,'617').ausgeschlossen=True;s.commit()
    with pytest.raises(ValueError,match='ausgeschlossen'):
        save(session_factory,owner)


def test_external_import_matches_and_prevents_october_repeat(session_factory,owner):
    save(session_factory,owner)
    # Generate synthetic XML outside production. Reset only synthetic reservation.
    generate(session_factory,today=date(2026,10,1))
    with session_factory() as s:
        f=s.scalar(select(Datei));xml=f.xml
        for r in s.scalars(select(Reservierung)):s.delete(r)
        s.delete(f);s.commit()
    key=import_existing(session_factory,xml=xml,monat='2026-10',bank_konto_id='BANK-TEST',evidence='Synthetic bank upload proof')
    assert import_existing(session_factory,xml=xml,monat='2026-10',bank_konto_id='BANK-TEST',evidence='Same proof')==key
    with pytest.raises(ValueError,match='weicht ab'):
        import_existing(session_factory,xml=xml.replace('123.45','123.46'),monat='2026-10',bank_konto_id='BANK-TEST',evidence='Conflict')
    assert not generate(session_factory,today=date(2026,10,1))['neue_dateien']


def test_supplement_only_released_items(session_factory,owner):
    save(session_factory,owner)
    second=owner|{'kennung':'SECOND','referenz':'REF0002','status':'KLAEREN'}
    save(session_factory,second)
    first=generate(session_factory,today=date(2026,10,1))
    save(session_factory,second|{'status':'FREIGEGEBEN'},1)
    secondrun=generate(session_factory,today=date(2026,10,2))
    assert len(first['neue_dateien'])==len(secondrun['neue_dateien'])==1
    assert first['neue_dateien']!=secondrun['neue_dateien']


@pytest.mark.parametrize('d,day,expected',[(date(2026,10,1),5,date(2026,10,5)),
    (date(2026,12,1),5,date(2026,12,7)),(date(2026,4,1),3,date(2026,4,7)),
    (date(2027,1,1),1,date(2027,1,4))])
def test_target_calendar(d,day,expected):
    assert execution_day(d,day)==expected


def test_invalid_amounts_and_iban(owner):
    for amount in (0,-1,123.456,'12345'):
        with pytest.raises(ValueError):Profil.model_validate(owner|{'betrag_cent':amount})
    with pytest.raises(ValueError):iban('AT001904300234573201')


def test_competing_runs_reserve_once(tmp_path,session_factory,owner):
    from mietinkasso.infrastructure.db.session import build_session_factory,create_all_tables
    from mietinkasso.infrastructure.config import Settings
    url='sqlite:///'+str(tmp_path/'race.sqlite')
    create_all_tables(Settings(database_url=url))
    factory=build_session_factory(url)
    with factory() as s:
        s.add(GesellschaftTable(id='OWNER',name='Synthetic'))
        s.add(ObjektTable(id='617',gesellschaft_id='OWNER',bezeichnung='Synthetic'))
        s.add(BankKontoTable(id='BANK-TEST',gesellschaft_id='OWNER',iban='AT611904300234573201',bezeichnung='Synthetic'))
        s.commit()
    save(factory,owner)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:generate(factory,today=date(2026,10,1)),range(4)))
    assert sum(len(r['neue_dateien']) for r in results)==1


def test_refund_for_same_month_holds(session_factory,owner):
    save(session_factory,owner)
    tx(session_factory,'REF0001 WEG 2026-10',amount=-12345)
    tx(session_factory,'REF0001 WEG 2026-10 RUECKBUCHUNG',amount=12345)
    assert plan(session_factory,'2026-10')[0]['status']=='KLAEREN'


def test_foreign_beneficiary_never_counts_paid(session_factory,owner):
    save(session_factory,owner)
    tx(session_factory,'REF0001 2026-10',beneficiary='AT611904300234573201')
    assert plan(session_factory,'2026-10')[0]['status']=='KLAEREN'


def test_generated_file_invalidated_by_later_hold(session_factory,owner):
    save(session_factory,owner);generate(session_factory,today=date(2026,10,1))
    save(session_factory,owner|{'status':'KLAEREN'},1)
    row=plan(session_factory,'2026-10')[0]
    assert row['datei_pruefen'] and row['datei_id']
    assert not generate(session_factory,today=date(2026,10,2))['neue_dateien']


def test_ui_auth_csrf_upload_and_stale_download(session_factory,owner,monkeypatch,tmp_path):
    import io
    from datetime import datetime
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pypdf import PdfWriter
    from mietinkasso.backoffice import dependencies as deps
    from mietinkasso.backoffice.security import SessionStore
    from mietinkasso.backoffice.routes import eigentuemerzahlungen as ui
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return cls(2026,10,1,tzinfo=tz)
    monkeypatch.setattr(ui,'datetime',Clock)
    monkeypatch.setattr(deps,'_session_factory',session_factory)
    monkeypatch.setattr(deps,'_settings',deps._settings.model_copy(update={
        'backoffice_password_hash':'configured','database_url':'sqlite:///'+str(tmp_path/'isolated.db')}))
    sessions=SessionStore(ttl_sekunden=1000)
    monkeypatch.setattr(deps,'_sessions',sessions)
    app=FastAPI();app.include_router(ui.router,prefix='/backoffice')
    client=TestClient(app)
    save(session_factory,owner)
    assert client.get('/backoffice/eigentuemerzahlungen',follow_redirects=False).status_code==303
    sid,csrf=sessions.erstellen('synthetic-owner')
    client.cookies.set(deps._COOKIE_NAME,sid)
    assert client.get('/backoffice/eigentuemerzahlungen?monat=2026-10').status_code==200
    assert client.get('/backoffice/eigentuemerzahlungen?monat=bad').status_code==400
    endpoint='/backoffice/eigentuemerzahlungen/profil/TEST-UNIT'
    assert client.get(endpoint).status_code==200
    assert client.post(endpoint,data={'csrf_token':'bad'}).status_code==403
    run=generate(session_factory,today=date(2026,10,1))
    download='/backoffice/eigentuemerzahlungen/datei/'+run['neue_dateien'][0]
    assert client.get(download).status_code==200
    form=dict(csrf_token=csrf,version='1',betrag='200.00',gueltig_ab='2026-01-01',
        gueltig_bis='2026-12-31',status='FREIGEGEBEN',hinweis='New synthetic source',bestaetigt='ja')
    assert client.post(endpoint,data=form).status_code==400  # new amount requires source
    writer=PdfWriter();writer.add_blank_page(width=100,height=100);stream=io.BytesIO();writer.write(stream)
    result=client.post(endpoint,data=form,files={'beleg':('source.pdf',stream.getvalue(),'application/pdf')},follow_redirects=False)
    assert result.status_code==303
    assert list((tmp_path/'eigentuemer-belege').glob('*.pdf'))
    assert client.get(download).status_code==409
    assert client.post(endpoint,data=form,files={'beleg':('source.pdf',stream.getvalue(),'application/pdf')}).status_code==400
