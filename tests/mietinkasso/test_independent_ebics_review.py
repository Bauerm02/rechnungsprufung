"""Independent synthetic acceptance cases; no real data or bank requests."""
from test_ebics_ingest import (
    konto,ctx,bank_service,_ntry,_stmt,_camt,_plan,_apply,MIETER_A,MIETER_B,REF_A,
)

def test_reused_service_reference_separate_statement_entries(session_factory,konto,ctx):
    doc=_camt(_stmt(
        _ntry(10000,'2026-04-07',native='BATCH-REFERENCE',ref='Miete fuer A',iban=MIETER_A),
        _ntry(2000,'2026-04-07',native='BATCH-REFERENCE',ref='Parkplatz fuer B',iban=MIETER_B),
    ))
    plan=_plan(session_factory,ctx,doc)
    assert len(plan.neu)==2 and not plan.konflikte
    _apply(session_factory,ctx,doc,plan.plan_hash)
    replay=_plan(session_factory,ctx,doc)
    assert len(replay.neu)==0 and len(replay.vorhanden)==2

def test_new_authoritative_bank_identity_does_not_disappear(session_factory,konto,ctx):
    first=_camt(_stmt(_ntry(10000,'2026-04-07',native='PAYMENT-A',ref=REF_A,iban=MIETER_A),stmt_id='ORIGINAL-1'))
    plan=_plan(session_factory,ctx,first);_apply(session_factory,ctx,first,plan.plan_hash)
    other=_camt(_stmt(_ntry(10000,'2026-04-07',native='PAYMENT-B',ref=REF_A,iban=MIETER_A),stmt_id='ORIGINAL-2'))
    plan=_plan(session_factory,ctx,other)
    assert len(plan.vorhanden)==0, 'A different bank identity cannot silently be called the old payment'

def test_noid_batch_replay(session_factory,konto,ctx):
    doc=_camt(_stmt(_ntry(30000,'2026-04-07',teile=[
        {'betrag':10000,'ref':'Teil A Zahlung','iban':MIETER_A},
        {'betrag':20000,'ref':'Teil B Zahlung','iban':MIETER_B},
    ])))
    plan=_plan(session_factory,ctx,doc);_apply(session_factory,ctx,doc,plan.plan_hash)
    replay=_plan(session_factory,ctx,doc)
    assert len(replay.neu)==0 and len(replay.vorhanden)==2
