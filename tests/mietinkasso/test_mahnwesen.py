from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from mietinkasso.domain.enums import OPTyp, Sperrgrund
from mietinkasso.domain.exceptions import MahnstufeReihenfolgeError
from mietinkasso.mahnwesen.repository import MahnFallRepository, MahnPolicyRepository
from mietinkasso.mahnwesen.service import MahnwesenService, VersandUngewissError


@pytest.fixture
def mahn_policy_repo(session_factory) -> MahnPolicyRepository:
    return MahnPolicyRepository(session_factory)


@pytest.fixture
def freigegebene_policy(mahn_policy_repo):
    policy = mahn_policy_repo.anlegen(
        stufe1_tage_nach_faelligkeit=7,
        stufe2_mindesttage_nach_stufe1_versand=14,
        zinsen_prozent=Decimal("0"),
        gebuehr_cent=0,
        status="ENTWURF",
    )
    return mahn_policy_repo.freigeben(policy.id)


@pytest.fixture
def mahn_service(session_factory, stammdaten_repo, op_service) -> MahnwesenService:
    return MahnwesenService(
        MahnFallRepository(session_factory), stammdaten_repo, op_service, bank_stand_max_age_days=2
    )


def _mit_faelligem_soll(op_service, konto, ctx, betrag_cent=60_000, faelligkeit=date(2026, 4, 5)):
    op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=betrag_cent,
        belegdatum=date(2026, 4, 1), buchungsdatum=date(2026, 4, 1),
        faelligkeit=faelligkeit, beleg_referenz="Miete April",
    )


def test_unklarer_eroeffnungssaldo_wird_nie_bemahnt(mahn_service, op_service, basis_vertrag, ctx_factory, freigegebene_policy):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    # Gesamtsaldo-Eröffnung ohne bekannte Fälligkeit -> sichtbar, aber nicht mahnbar
    op_service.eroeffnen_gesamtsaldo(
        ctx=ctx, konto=konto, betrag_cent=60_000, stichtag=date(2026, 1, 1), import_id="ERO-1", akteur="test"
    )
    ergebnis = mahn_service.planen(
        ctx=ctx, vertrag=vertrag, konto=konto, policy=freigegebene_policy,
        heute=date(2026, 4, 20), bank_stand_alter_tage=0,
    )
    assert ergebnis.status == "KEIN_BETRAG"


def test_aktive_sperre_blockiert_mahnung(mahn_service, op_service, stammdaten_repo, basis_vertrag, ctx_factory, freigegebene_policy):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    _mit_faelligem_soll(op_service, konto, ctx)
    stammdaten_repo.sperre_setzen(vertrag_id=vertrag.id, grund=Sperrgrund.RATENPLAN.value, kommentar="Ratenplan vereinbart")

    ergebnis = mahn_service.planen(
        ctx=ctx, vertrag=vertrag, konto=konto, policy=freigegebene_policy,
        heute=date(2026, 4, 20), bank_stand_alter_tage=0,
    )
    assert ergebnis.status == "BLOCKIERT"
    assert "RATENPLAN" in ergebnis.grund


def test_veraltete_bank_blockiert_mahnung(mahn_service, op_service, basis_vertrag, ctx_factory, freigegebene_policy):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    _mit_faelligem_soll(op_service, konto, ctx)

    ergebnis = mahn_service.planen(
        ctx=ctx, vertrag=vertrag, konto=konto, policy=freigegebene_policy,
        heute=date(2026, 4, 20), bank_stand_alter_tage=5,  # älter als 2 Tage
    )
    assert ergebnis.status == "BLOCKIERT"
    assert "Bankstand" in ergebnis.grund


def test_stufe2_ohne_gesendete_stufe1_ist_verboten(mahn_service, op_service, stammdaten_repo, basis_vertrag, ctx_factory, freigegebene_policy):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    _mit_faelligem_soll(op_service, konto, ctx)

    ergebnis_stufe1 = mahn_service.planen(
        ctx=ctx, vertrag=vertrag, konto=konto, policy=freigegebene_policy,
        heute=date(2026, 4, 20), bank_stand_alter_tage=0,
    )
    assert ergebnis_stufe1.status == "GEPLANT"
    assert ergebnis_stufe1.stufe == 1
    # Stufe 1 wurde geplant, aber NICHT gesendet -> erneutes Planen bleibt bei Stufe 1
    ergebnis_wieder_stufe1 = mahn_service.planen(
        ctx=ctx, vertrag=vertrag, konto=konto, policy=freigegebene_policy,
        heute=date(2026, 4, 21), bank_stand_alter_tage=0,
    )
    assert ergebnis_wieder_stufe1.stufe == 1
    assert ergebnis_wieder_stufe1.mahnfall_id == ergebnis_stufe1.mahnfall_id


def test_stufe2_nur_nach_gesendeter_stufe1(mahn_service, op_service, basis_vertrag, ctx_factory, freigegebene_policy):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    _mit_faelligem_soll(op_service, konto, ctx)

    geplant = mahn_service.planen(
        ctx=ctx, vertrag=vertrag, konto=konto, policy=freigegebene_policy,
        heute=date(2026, 4, 20), bank_stand_alter_tage=0,
    )
    versendet = mahn_service.versenden(
        ctx=ctx, mahnfall_id=geplant.mahnfall_id, vertrag=vertrag, konto=konto,
        heute=date(2026, 4, 20), send_enabled=True, versand_fn=lambda snapshot: None,
    )
    assert versendet.status == "GESENDET"

    # Jetzt korrekt: 15 Tage nach Versand von Stufe 1, mindestens 14 verlangt
    stufe2 = mahn_service.planen(
        ctx=ctx, vertrag=vertrag, konto=konto, policy=freigegebene_policy,
        heute=date(2026, 5, 5), bank_stand_alter_tage=0,
    )
    assert stufe2.status == "GEPLANT"
    assert stufe2.stufe == 2


def test_provider_timeout_fuehrt_nicht_zu_doppelversand(mahn_service, op_service, basis_vertrag, ctx_factory, freigegebene_policy):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    _mit_faelligem_soll(op_service, konto, ctx)
    geplant = mahn_service.planen(
        ctx=ctx, vertrag=vertrag, konto=konto, policy=freigegebene_policy,
        heute=date(2026, 4, 20), bank_stand_alter_tage=0,
    )

    def timeout_fn(snapshot):
        raise VersandUngewissError("Provider hat nicht rechtzeitig geantwortet")

    erster_versuch = mahn_service.versenden(
        ctx=ctx, mahnfall_id=geplant.mahnfall_id, vertrag=vertrag, konto=konto,
        heute=date(2026, 4, 20), send_enabled=True, versand_fn=timeout_fn,
    )
    assert erster_versuch.status == "UNSICHER"

    # Kein blinder Retry: ein zweiter automatischer Versuch darf NICHT erneut senden
    zweiter_versuch = mahn_service.versenden(
        ctx=ctx, mahnfall_id=geplant.mahnfall_id, vertrag=vertrag, konto=konto,
        heute=date(2026, 4, 20), send_enabled=True,
        versand_fn=lambda snapshot: (_ for _ in ()).throw(AssertionError("darf nicht aufgerufen werden")),
    )
    assert zweiter_versuch.status == "BEREITS_VERARBEITET"


def test_zahlung_zwischen_planung_und_versand_stoppt_versand(mahn_service, op_service, basis_vertrag, ctx_factory, freigegebene_policy):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    _mit_faelligem_soll(op_service, konto, ctx)
    geplant = mahn_service.planen(
        ctx=ctx, vertrag=vertrag, konto=konto, policy=freigegebene_policy,
        heute=date(2026, 4, 20), bank_stand_alter_tage=0,
    )
    assert geplant.status == "GEPLANT"

    # Zahlung trifft NACH der Planung, aber VOR dem Versand ein
    op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.ZAHLUNG, betrag_cent=60_000,
        belegdatum=date(2026, 4, 20), buchungsdatum=date(2026, 4, 20),
        faelligkeit=None, beleg_referenz="Verspätete Zahlung kurz vor Mahnlauf",
    )

    ergebnis = mahn_service.versenden(
        ctx=ctx, mahnfall_id=geplant.mahnfall_id, vertrag=vertrag, konto=konto,
        heute=date(2026, 4, 20), send_enabled=True,
        versand_fn=lambda snapshot: (_ for _ in ()).throw(AssertionError("darf nicht aufgerufen werden")),
    )
    assert ergebnis.status == "UEBERSPRUNGEN"


def test_zwei_worker_versenden_nicht_doppelt(mahn_service, op_service, basis_vertrag, ctx_factory, freigegebene_policy):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    _mit_faelligem_soll(op_service, konto, ctx)
    geplant = mahn_service.planen(
        ctx=ctx, vertrag=vertrag, konto=konto, policy=freigegebene_policy,
        heute=date(2026, 4, 20), bank_stand_alter_tage=0,
    )

    versand_zaehler = {"count": 0}

    def zaehlender_versand(snapshot):
        versand_zaehler["count"] += 1

    ergebnis_a = mahn_service.versenden(
        ctx=ctx, mahnfall_id=geplant.mahnfall_id, vertrag=vertrag, konto=konto,
        heute=date(2026, 4, 20), send_enabled=True, versand_fn=zaehlender_versand,
    )
    ergebnis_b = mahn_service.versenden(
        ctx=ctx, mahnfall_id=geplant.mahnfall_id, vertrag=vertrag, konto=konto,
        heute=date(2026, 4, 20), send_enabled=True, versand_fn=zaehlender_versand,
    )
    assert ergebnis_a.status == "GESENDET"
    assert ergebnis_b.status == "BEREITS_VERARBEITET"
    assert versand_zaehler["count"] == 1


def test_nach_stufe2_kein_stufe3_nur_interner_fall(mahn_service, op_service, basis_vertrag, ctx_factory, freigegebene_policy):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    _mit_faelligem_soll(op_service, konto, ctx)

    stufe1 = mahn_service.planen(
        ctx=ctx, vertrag=vertrag, konto=konto, policy=freigegebene_policy,
        heute=date(2026, 4, 20), bank_stand_alter_tage=0,
    )
    mahn_service.versenden(
        ctx=ctx, mahnfall_id=stufe1.mahnfall_id, vertrag=vertrag, konto=konto,
        heute=date(2026, 4, 20), send_enabled=True, versand_fn=lambda snapshot: None,
    )
    stufe2 = mahn_service.planen(
        ctx=ctx, vertrag=vertrag, konto=konto, policy=freigegebene_policy,
        heute=date(2026, 5, 5), bank_stand_alter_tage=0,
    )
    mahn_service.versenden(
        ctx=ctx, mahnfall_id=stufe2.mahnfall_id, vertrag=vertrag, konto=konto,
        heute=date(2026, 5, 5), send_enabled=True, versand_fn=lambda snapshot: None,
    )

    kein_stufe3 = mahn_service.planen(
        ctx=ctx, vertrag=vertrag, konto=konto, policy=freigegebene_policy,
        heute=date(2026, 6, 1), bank_stand_alter_tage=0,
    )
    assert kein_stufe3.status == "BLOCKIERT"
    assert "interner Bearbeitungsfall" in kein_stufe3.grund
