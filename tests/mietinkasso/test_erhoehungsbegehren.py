from datetime import date
from dataclasses import replace
from types import SimpleNamespace
from hashlib import sha256

import pytest

from mietinkasso.indexautomatik import begehren as b
from mietinkasso.indexautomatik.schreiben import SchreibenKontext, SchreibenKomponente, SchreibenJahresschritt


def context(**kw):
    fields = dict(gesellschaft_name='Beispiel Vermietung GmbH', objekt_bezeichnung='Musterhaus',
        objekt_adresse='Testweg 1, 1010 Wien', einheit_bezeichnung='Top 1', mieter_name='Testmieter',
        mieter_adresse='Testweg 1/1, 1010 Wien', rechtsordnung=b.FULL, klausel_referenz='Punkt 4 Wertsicherung',
        geaenderte_komponenten=[SchreibenKomponente('HMZ', 'Hauptmiete', 100000, 101000, 10000)],
        unveraenderte_komponenten=[SchreibenKomponente('BK', 'Betriebskosten', 15000, 15000, 10000)],
        erhoehung_cent=1000, massgeblicher_termin=date(2026,4,1), vertrag_beleg_referenz='interner/beleg.pdf',
        rechtsprofil_version=1, jlb_signatur='JLB Projects GmbH – Hausverwaltung',
        bezugsjahr=2024, bezugsmonat=1, ziel_bewertungsjahr=2026,
        jahresschritte=[SchreibenJahresschritt(2025,'100','104','0.04','0.035','0.01')],
        vertraglich_zulaessiger_betrag_cent=101000, gesetzliche_basis_cent=100000, gesetzliche_grenze_cent=101000)
    fields.update(kw)
    return SchreibenKontext(**fields)


def profile(**kw):
    return SimpleNamespace(**dict(dict(rechtsordnung=b.FULL, ist_wohnungsnutzung=True,
        frist_tage_zugang_bis_wirksamkeit=None, frist_quellenbeleg=None), **kw))


def prepare(k=None, p=None, **kw):
    return b.vorbereiten(k or context(), profil=p or profile(), faelligkeit_tag=5,
                         heute=kw.pop('heute', date(2026,9,20)), **kw)


def test_full_notice_date_totals_and_legal_cap_not_independent_claim():
    text, meta = prepare()
    assert not meta['fehler']
    assert meta['zahlungstermin']=='2026-10-05'
    assert meta['zugang_spaetestens']=='2026-09-21'
    assert '§ 16 Abs. 9 MRG' in text
    assert '1.160,00 €' in text and '1.150,00 €' in text
    assert 'im Namen und Auftrag' in text and 'Beispiel Vermietung GmbH' in text
    assert 'interner/beleg.pdf' not in text and b.FULL not in text
    assert 'gesondert zu prüfen' not in text
    assert 'Veränderung 4 %' in text and 'gedämpft 3,5 %' in text


def test_partial_requires_evidenced_deadline_and_does_not_claim_16_9():
    k = context(rechtsordnung='OESTERREICH_MRG_TEIL')
    p = profile(rechtsordnung=k.rechtsordnung)
    assert prepare(k,p)[1]['fehler']
    p.frist_tage_zugang_bis_wirksamkeit=30
    p.frist_quellenbeleg='Vertrag Punkt 7'
    text, meta=prepare(k,p)
    assert not meta['fehler'] and '§ 16 Abs. 9 MRG' not in text
    assert meta['zahlungstermin']=='2026-11-05'


@pytest.mark.parametrize('days,expected', [(0,14),(40,40)])
def test_full_mandatory_minimum(days,expected):
    assert b.frist_tage(profile(frist_tage_zugang_bis_wirksamkeit=days,frist_quellenbeleg='Beleg'))==expected


def test_commercial_full_mrg_keeps_notice_rule_without_dwelling_cap():
    data=dict(reihe='VPI2020',basis_monat='2024-01',vergleich_monat='2026-08',alter_wert='100',
              neuer_wert='101',prozent='1',basis_cent=100000)
    text, meta=prepare(p=profile(ist_wohnungsnutzung=False),berechnung=data)
    assert not meta['fehler'] and '§ 16 Abs. 9 MRG' in text
    assert 'Mieten-Wertsicherungsgesetz' not in text and '2026-08: 101' in text
    assert prepare(p=profile(ist_wohnungsnutzung=False))[1]['fehler']


@pytest.mark.parametrize('changes', [dict(klausel_referenz=None),dict(erhoehung_cent=999),
    dict(gesetzliche_grenze_cent=100999),dict(gesetzliche_basis_cent=None),dict(mieter_adresse=None)])
def test_missing_or_inconsistent_notice_blocked(changes):
    text,meta=prepare(context(**changes))
    assert meta['fehler'] and text.startswith('ENTWURF')
    assert b.versandfehler(text,meta,heute=date(2026,9,20))


def test_hash_and_expired_deadline_and_legacy():
    text,meta=prepare()
    assert not b.versandfehler(text,meta,heute=date(2026,9,21))
    assert b.versandfehler(text,meta,heute=date(2026,9,22))
    assert b.versandfehler(text+'x',meta,heute=date(2026,9,20))
    assert b.versandfehler(text,None,heute=date(2026,9,20))


def test_snapshot_roundtrip_and_jlb_html_escaping():
    k=context(mieter_name='<script>alert(1)</script>')
    assert b.kontext_aus_snapshot(b.kontext_snapshot(k))==k
    text,_=prepare(k)
    html=b.druckansicht(text,status='ENTWURF <img src=x>')
    assert '<script>' not in html and '&lt;script&gt;' in html
    assert '#C9A86A' in html and '#1F2125' in html and 'hausverwaltung@jlb-immo.at' in html
    assert 'data:' not in html
