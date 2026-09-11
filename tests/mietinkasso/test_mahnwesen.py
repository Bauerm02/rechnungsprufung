from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from mietinkasso.domain.enums import MahnStatus, OPTyp, Rolle, Sperrgrund
from mietinkasso.mahnwesen.repository import MahnFallRepository, MahnPolicyRepository
from mietinkasso.mahnwesen.service import MahnwesenService, PolicyNichtFreigegebenError, VersandUngewissError


@pytest.fixture
def mahn_policy_repo(session_factory) -> MahnPolicyRepository:
    return MahnPolicyRepository(session_factory)


@pytest.fixture
def mahn_fall_repo(session_factory) -> MahnFallRepository:
    return MahnFallRepository(session_factory)


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
def mahn_service(mahn_fall_repo, stammdaten_repo, op_service) -> MahnwesenService:
    return MahnwesenService(mahn_fall_repo, stammdaten_repo, op_service, bank_stand_max_age_days=2)


def _mit_faelligem_soll(op_service, konto, ctx, betrag_cent=60_000, faelligkeit=date(2026, 4, 5)):
    op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=betrag_cent,
        belegdatum=date(2026, 4, 1), buchungsdatum=date(2026, 4, 1),
        faelligkeit=faelligkeit, beleg_referenz="Miete April",
    )


def _einzige_forderung(op_service, konto, heute):
    forderungen = op_service.offene_forderungen(konto.id, heute=heute)
    assert len(forderungen) == 1
    return forderungen[0]


def _stufe1_bis_gesendet(mahn_service, op_service, vertrag, konto, ctx, policy, heute_faellig_plus_7=date(2026, 4, 12)):
    forderung = _einzige_forderung(op_service, konto, heute_faellig_plus_7)
    geplant = mahn_service.plane_forderung(
        ctx=ctx, vertrag=vertrag, konto=konto, forderung=forderung, policy=policy,
        heute=heute_faellig_plus_7, bank_stand_alter_tage=0,
    )
    assert geplant.status == "GEPLANT"
    versendet = mahn_service.versenden(
        ctx=ctx, mahnfall_id=geplant.mahnfall_id, heute=heute_faellig_plus_7, bank_stand_alter_tage=0,
        send_enabled=True, versand_fn=lambda snapshot: None,
    )
    assert versendet.status == "GESENDET"
    return geplant, forderung


def test_unklarer_eroeffnungssaldo_wird_nie_bemahnt(mahn_service, op_service, basis_vertrag, ctx_factory, freigegebene_policy):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    op_service.eroeffnen_gesamtsaldo(
        ctx=ctx, konto=konto, betrag_cent=60_000, stichtag=date(2026, 1, 1), import_id="ERO-1", akteur="test"
    )
    forderung = _einzige_forderung(op_service, konto, date(2026, 4, 20))
    ergebnis = mahn_service.plane_forderung(
        ctx=ctx, vertrag=vertrag, konto=konto, forderung=forderung, policy=freigegebene_policy,
        heute=date(2026, 4, 20), bank_stand_alter_tage=0,
    )
    assert ergebnis.status == "KEIN_BETRAG"


def test_aktive_sperre_blockiert_mahnung(mahn_service, op_service, stammdaten_repo, basis_vertrag, ctx_factory, freigegebene_policy):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    _mit_faelligem_soll(op_service, konto, ctx)
    stammdaten_repo.sperre_setzen(vertrag_id=vertrag.id, grund=Sperrgrund.RATENPLAN.value, kommentar="Ratenplan vereinbart")
    forderung = _einzige_forderung(op_service, konto, date(2026, 4, 20))

    ergebnis = mahn_service.plane_forderung(
        ctx=ctx, vertrag=vertrag, konto=konto, forderung=forderung, policy=freigegebene_policy,
        heute=date(2026, 4, 20), bank_stand_alter_tage=0,
    )
    assert ergebnis.status == "BLOCKIERT"
    assert "RATENPLAN" in ergebnis.grund


def test_veraltete_bank_blockiert_mahnung(mahn_service, op_service, basis_vertrag, ctx_factory, freigegebene_policy):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    _mit_faelligem_soll(op_service, konto, ctx)
    forderung = _einzige_forderung(op_service, konto, date(2026, 4, 20))

    ergebnis = mahn_service.plane_forderung(
        ctx=ctx, vertrag=vertrag, konto=konto, forderung=forderung, policy=freigegebene_policy,
        heute=date(2026, 4, 20), bank_stand_alter_tage=5,  # älter als 2 Tage
    )
    assert ergebnis.status == "BLOCKIERT"
    assert "Bankstand" in ergebnis.grund


def test_entwurf_policy_darf_nichts_planen(mahn_service, op_service, mahn_policy_repo, basis_vertrag, ctx_factory):
    """Regression (Codex-Fund #2, Teil 1): `planen` ignorierte bisher, ob
    die Policy überhaupt FREIGEGEBEN ist."""

    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    entwurf_policy = mahn_policy_repo.anlegen(stufe1_tage_nach_faelligkeit=7, status="ENTWURF")
    _mit_faelligem_soll(op_service, konto, ctx)
    forderung = _einzige_forderung(op_service, konto, date(2026, 4, 20))

    with pytest.raises(PolicyNichtFreigegebenError):
        mahn_service.plane_forderung(
            ctx=ctx, vertrag=vertrag, konto=konto, forderung=forderung, policy=entwurf_policy,
            heute=date(2026, 4, 20), bank_stand_alter_tage=0,
        )


def test_stufe1_erst_nach_wartefrist_nicht_schon_am_faelligkeitstag(mahn_service, op_service, basis_vertrag, ctx_factory, freigegebene_policy):
    """Regression (Codex-Fund #2, Teil 2): bei Fälligkeit 05.04. und
    Stufe1=7 Tage darf NICHT schon am 05.04. geplant werden, sondern erst
    ab dem 12.04."""

    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    _mit_faelligem_soll(op_service, konto, ctx, faelligkeit=date(2026, 4, 5))
    forderung_am_faelligkeitstag = _einzige_forderung(op_service, konto, date(2026, 4, 5))

    zu_frueh = mahn_service.plane_forderung(
        ctx=ctx, vertrag=vertrag, konto=konto, forderung=forderung_am_faelligkeitstag, policy=freigegebene_policy,
        heute=date(2026, 4, 5), bank_stand_alter_tage=0,
    )
    assert zu_frueh.status == "ZU_FRUEH"

    knapp_davor = mahn_service.plane_forderung(
        ctx=ctx, vertrag=vertrag, konto=konto, forderung=_einzige_forderung(op_service, konto, date(2026, 4, 11)),
        policy=freigegebene_policy, heute=date(2026, 4, 11), bank_stand_alter_tage=0,
    )
    assert knapp_davor.status == "ZU_FRUEH"

    genau_am_tag_7 = mahn_service.plane_forderung(
        ctx=ctx, vertrag=vertrag, konto=konto, forderung=_einzige_forderung(op_service, konto, date(2026, 4, 12)),
        policy=freigegebene_policy, heute=date(2026, 4, 12), bank_stand_alter_tage=0,
    )
    assert genau_am_tag_7.status == "GEPLANT"


def test_stufe2_ohne_gesendete_stufe1_ist_verboten(mahn_service, op_service, basis_vertrag, ctx_factory, freigegebene_policy):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    _mit_faelligem_soll(op_service, konto, ctx)
    forderung = _einzige_forderung(op_service, konto, date(2026, 4, 20))

    ergebnis_stufe1 = mahn_service.plane_forderung(
        ctx=ctx, vertrag=vertrag, konto=konto, forderung=forderung, policy=freigegebene_policy,
        heute=date(2026, 4, 20), bank_stand_alter_tage=0,
    )
    assert ergebnis_stufe1.status == "GEPLANT"
    assert ergebnis_stufe1.stufe == 1
    # Stufe 1 wurde geplant, aber NICHT gesendet -> erneutes Planen bleibt bei Stufe 1
    ergebnis_wieder_stufe1 = mahn_service.plane_forderung(
        ctx=ctx, vertrag=vertrag, konto=konto, forderung=_einzige_forderung(op_service, konto, date(2026, 4, 21)),
        policy=freigegebene_policy, heute=date(2026, 4, 21), bank_stand_alter_tage=0,
    )
    assert ergebnis_wieder_stufe1.stufe == 1
    assert ergebnis_wieder_stufe1.mahnfall_id == ergebnis_stufe1.mahnfall_id


def test_stufe2_nur_nach_gesendeter_stufe1(mahn_service, op_service, basis_vertrag, ctx_factory, freigegebene_policy):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    _mit_faelligem_soll(op_service, konto, ctx)
    _stufe1_bis_gesendet(mahn_service, op_service, vertrag, konto, ctx, freigegebene_policy)

    # 15 Tage nach Versand von Stufe 1, mindestens 14 verlangt
    stufe2 = mahn_service.plane_forderung(
        ctx=ctx, vertrag=vertrag, konto=konto, forderung=_einzige_forderung(op_service, konto, date(2026, 4, 27)),
        policy=freigegebene_policy, heute=date(2026, 4, 27), bank_stand_alter_tage=0,
    )
    assert stufe2.status == "GEPLANT"
    assert stufe2.stufe == 2


def test_provider_timeout_fuehrt_nicht_zu_doppelversand(mahn_service, op_service, basis_vertrag, ctx_factory, freigegebene_policy):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    _mit_faelligem_soll(op_service, konto, ctx)
    geplant = mahn_service.plane_forderung(
        ctx=ctx, vertrag=vertrag, konto=konto, forderung=_einzige_forderung(op_service, konto, date(2026, 4, 20)),
        policy=freigegebene_policy, heute=date(2026, 4, 20), bank_stand_alter_tage=0,
    )

    def timeout_fn(snapshot):
        raise VersandUngewissError("Provider hat nicht rechtzeitig geantwortet")

    erster_versuch = mahn_service.versenden(
        ctx=ctx, mahnfall_id=geplant.mahnfall_id, heute=date(2026, 4, 20), bank_stand_alter_tage=0,
        send_enabled=True, versand_fn=timeout_fn,
    )
    assert erster_versuch.status == "UNSICHER"

    # Kein blinder Retry: ein zweiter automatischer Versuch darf NICHT erneut senden
    zweiter_versuch = mahn_service.versenden(
        ctx=ctx, mahnfall_id=geplant.mahnfall_id, heute=date(2026, 4, 20), bank_stand_alter_tage=0,
        send_enabled=True,
        versand_fn=lambda snapshot: (_ for _ in ()).throw(AssertionError("darf nicht aufgerufen werden")),
    )
    assert zweiter_versuch.status == "BEREITS_VERARBEITET"


def test_claim_fuer_versand_ist_exklusiv(mahn_fall_repo, mahn_service, op_service, basis_vertrag, ctx_factory, freigegebene_policy):
    """Regression (Codex-Fund #1): zwei aufeinanderfolgende
    `claim_fuer_versand`-Aufrufe dürfen NICHT beide True liefern."""

    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    _mit_faelligem_soll(op_service, konto, ctx)
    geplant = mahn_service.plane_forderung(
        ctx=ctx, vertrag=vertrag, konto=konto, forderung=_einzige_forderung(op_service, konto, date(2026, 4, 20)),
        policy=freigegebene_policy, heute=date(2026, 4, 20), bank_stand_alter_tage=0,
    )
    erster_claim = mahn_fall_repo.claim_fuer_versand(geplant.mahnfall_id)
    zweiter_claim = mahn_fall_repo.claim_fuer_versand(geplant.mahnfall_id)
    assert erster_claim is True
    assert zweiter_claim is False
    assert mahn_fall_repo.get(geplant.mahnfall_id).status == MahnStatus.IN_VERSAND.value


def test_verwaiste_in_versand_faelle_werden_nicht_automatisch_erneut_versucht(mahn_fall_repo, mahn_service, op_service, basis_vertrag, ctx_factory, freigegebene_policy):
    """Absturz-Recovery: ein Fall, der seit dem Claim zu lange in
    IN_VERSAND feststeckt, wird auf UNSICHER gesetzt statt automatisch
    erneut versucht zu werden."""

    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    _mit_faelligem_soll(op_service, konto, ctx)
    geplant = mahn_service.plane_forderung(
        ctx=ctx, vertrag=vertrag, konto=konto, forderung=_einzige_forderung(op_service, konto, date(2026, 4, 20)),
        policy=freigegebene_policy, heute=date(2026, 4, 20), bank_stand_alter_tage=0,
    )
    lange_her = datetime.now(timezone.utc) - timedelta(hours=1)
    mahn_fall_repo.claim_fuer_versand(geplant.mahnfall_id, jetzt=lange_her)

    verwaiste = mahn_service.markiere_verwaiste_als_unsicher(max_alter=timedelta(minutes=15))
    assert len(verwaiste) == 1
    assert mahn_fall_repo.get(geplant.mahnfall_id).status == MahnStatus.UNSICHER.value


def test_zahlung_zwischen_planung_und_versand_stoppt_versand(mahn_service, op_service, basis_vertrag, ctx_factory, freigegebene_policy):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    _mit_faelligem_soll(op_service, konto, ctx)
    geplant = mahn_service.plane_forderung(
        ctx=ctx, vertrag=vertrag, konto=konto, forderung=_einzige_forderung(op_service, konto, date(2026, 4, 20)),
        policy=freigegebene_policy, heute=date(2026, 4, 20), bank_stand_alter_tage=0,
    )
    assert geplant.status == "GEPLANT"

    # Zahlung trifft NACH der Planung, aber VOR dem Versand ein
    op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.ZAHLUNG, betrag_cent=60_000,
        belegdatum=date(2026, 4, 20), buchungsdatum=date(2026, 4, 20),
        faelligkeit=None, beleg_referenz="Verspätete Zahlung kurz vor Mahnlauf",
    )

    ergebnis = mahn_service.versenden(
        ctx=ctx, mahnfall_id=geplant.mahnfall_id, heute=date(2026, 4, 20), bank_stand_alter_tage=0,
        send_enabled=True,
        versand_fn=lambda snapshot: (_ for _ in ()).throw(AssertionError("darf nicht aufgerufen werden")),
    )
    assert ergebnis.status == "UEBERSPRUNGEN"


def test_bankstand_veraltet_bei_versand_stoppt_auch_wenn_bei_planung_frisch(mahn_service, op_service, basis_vertrag, ctx_factory, freigegebene_policy):
    """Acceptance-Blocker: `versenden` muss die Bankfrische SELBST erneut
    prüfen, nicht nur zur Planungszeit."""

    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    _mit_faelligem_soll(op_service, konto, ctx)
    geplant = mahn_service.plane_forderung(
        ctx=ctx, vertrag=vertrag, konto=konto, forderung=_einzige_forderung(op_service, konto, date(2026, 4, 20)),
        policy=freigegebene_policy, heute=date(2026, 4, 20), bank_stand_alter_tage=0,
    )
    ergebnis = mahn_service.versenden(
        ctx=ctx, mahnfall_id=geplant.mahnfall_id, heute=date(2026, 4, 20), bank_stand_alter_tage=5,
        send_enabled=True,
        versand_fn=lambda snapshot: (_ for _ in ()).throw(AssertionError("darf nicht aufgerufen werden")),
    )
    assert ergebnis.status == "BLOCKIERT"


def test_zwei_worker_versenden_nicht_doppelt(mahn_service, op_service, basis_vertrag, ctx_factory, freigegebene_policy):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    _mit_faelligem_soll(op_service, konto, ctx)
    geplant = mahn_service.plane_forderung(
        ctx=ctx, vertrag=vertrag, konto=konto, forderung=_einzige_forderung(op_service, konto, date(2026, 4, 20)),
        policy=freigegebene_policy, heute=date(2026, 4, 20), bank_stand_alter_tage=0,
    )

    versand_zaehler = {"count": 0}

    def zaehlender_versand(snapshot):
        versand_zaehler["count"] += 1

    ergebnis_a = mahn_service.versenden(
        ctx=ctx, mahnfall_id=geplant.mahnfall_id, heute=date(2026, 4, 20), bank_stand_alter_tage=0,
        send_enabled=True, versand_fn=zaehlender_versand,
    )
    ergebnis_b = mahn_service.versenden(
        ctx=ctx, mahnfall_id=geplant.mahnfall_id, heute=date(2026, 4, 20), bank_stand_alter_tage=0,
        send_enabled=True, versand_fn=zaehlender_versand,
    )
    assert ergebnis_a.status == "GESENDET"
    assert ergebnis_b.status == "BEREITS_VERARBEITET"
    assert versand_zaehler["count"] == 1


def test_nach_stufe2_kein_stufe3_nur_interner_fall(mahn_service, op_service, basis_vertrag, ctx_factory, freigegebene_policy):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    _mit_faelligem_soll(op_service, konto, ctx)
    _, forderung = _stufe1_bis_gesendet(mahn_service, op_service, vertrag, konto, ctx, freigegebene_policy)

    stufe2 = mahn_service.plane_forderung(
        ctx=ctx, vertrag=vertrag, konto=konto, forderung=_einzige_forderung(op_service, konto, date(2026, 4, 27)),
        policy=freigegebene_policy, heute=date(2026, 4, 27), bank_stand_alter_tage=0,
    )
    mahn_service.versenden(
        ctx=ctx, mahnfall_id=stufe2.mahnfall_id, heute=date(2026, 4, 27), bank_stand_alter_tage=0,
        send_enabled=True, versand_fn=lambda snapshot: None,
    )

    kein_stufe3 = mahn_service.plane_forderung(
        ctx=ctx, vertrag=vertrag, konto=konto, forderung=_einzige_forderung(op_service, konto, date(2026, 6, 1)),
        policy=freigegebene_policy, heute=date(2026, 6, 1), bank_stand_alter_tage=0,
    )
    assert kein_stufe3.status == "BLOCKIERT"
    assert "interner Bearbeitungsfall" in kein_stufe3.grund


def test_neue_forderung_bekommt_eigenen_zyklus_nach_stufe2_der_alten(mahn_service, op_service, basis_vertrag, ctx_factory, freigegebene_policy):
    """Regression (Abnahmesperre): eine Forderung, die schon bei Stufe 2
    (oder danach nur noch intern) ist, darf eine SPÄTERE, ANDERE Forderung
    (z. B. die Miete des Folgemonats) nicht blockieren."""

    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    _mit_faelligem_soll(op_service, konto, ctx, betrag_cent=60_000, faelligkeit=date(2026, 4, 5))
    _stufe1_bis_gesendet(mahn_service, op_service, vertrag, konto, ctx, freigegebene_policy)
    stufe2 = mahn_service.plane_forderung(
        ctx=ctx, vertrag=vertrag, konto=konto,
        forderung=[f for f in op_service.offene_forderungen(konto.id, heute=date(2026, 4, 27))][0],
        policy=freigegebene_policy, heute=date(2026, 4, 27), bank_stand_alter_tage=0,
    )
    mahn_service.versenden(
        ctx=ctx, mahnfall_id=stufe2.mahnfall_id, heute=date(2026, 4, 27), bank_stand_alter_tage=0,
        send_enabled=True, versand_fn=lambda snapshot: None,
    )

    # Neue, eigenständige Forderung: Miete Mai, eigene Fälligkeit
    op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=60_000,
        belegdatum=date(2026, 5, 1), buchungsdatum=date(2026, 5, 1),
        faelligkeit=date(2026, 5, 5), beleg_referenz="Miete Mai",
    )
    forderungen_mitte_mai = op_service.offene_forderungen(konto.id, heute=date(2026, 5, 20))
    assert len(forderungen_mitte_mai) == 2  # alte (April, weiter offen) + neue (Mai)
    neue_forderung = next(f for f in forderungen_mitte_mai if f.belegdatum == date(2026, 5, 1))

    ergebnis_neue_forderung = mahn_service.plane_forderung(
        ctx=ctx, vertrag=vertrag, konto=konto, forderung=neue_forderung, policy=freigegebene_policy,
        heute=date(2026, 5, 20), bank_stand_alter_tage=0,
    )
    assert ergebnis_neue_forderung.status == "GEPLANT"
    assert ergebnis_neue_forderung.stufe == 1  # eigener Zyklus, NICHT durch die alte Stufe2 blockiert


def test_lesezugriff_darf_nicht_planen_oder_versenden(mahn_service, op_service, basis_vertrag, ctx_factory, freigegebene_policy):
    vertrag, konto = basis_vertrag
    ctx_schreibend = ctx_factory("7DI")
    _mit_faelligem_soll(op_service, konto, ctx_schreibend)
    ctx_lesend = ctx_factory("7DI", Rolle.LESEZUGRIFF)

    with pytest.raises(Exception):
        mahn_service.plane_forderung(
            ctx=ctx_lesend, vertrag=vertrag, konto=konto,
            forderung=_einzige_forderung(op_service, konto, date(2026, 4, 20)),
            policy=freigegebene_policy, heute=date(2026, 4, 20), bank_stand_alter_tage=0,
        )

    geplant = mahn_service.plane_forderung(
        ctx=ctx_schreibend, vertrag=vertrag, konto=konto,
        forderung=_einzige_forderung(op_service, konto, date(2026, 4, 20)),
        policy=freigegebene_policy, heute=date(2026, 4, 20), bank_stand_alter_tage=0,
    )
    with pytest.raises(Exception):
        mahn_service.versenden(
            ctx=ctx_lesend, mahnfall_id=geplant.mahnfall_id, heute=date(2026, 4, 20), bank_stand_alter_tage=0,
            send_enabled=True,
            versand_fn=lambda snapshot: (_ for _ in ()).throw(AssertionError("darf nicht aufgerufen werden")),
        )
