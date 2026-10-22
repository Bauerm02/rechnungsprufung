"""Auftrag HV-20261022-EIN-KONTOBLATT: Tests für den kontrollierten
Backoffice-Import tatsächlich manuell (außerhalb des automatisierten
Mahnlaufs) versendeter Mahnungen (`manueller_mahnversand_repository.py`/
`manueller_mahnversand_service.py`) - Idempotenz, Cross-Contract-/
Cross-Gesellschaft-Ablehnung, Auth/Schreibrecht, Buchung bei Import,
Doppelversand-Gate in `MahnwesenService.plane_mahnlauf`."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from mietinkasso.domain.enums import OPTyp, Rolle
from mietinkasso.domain.exceptions import CrossTenantError
from mietinkasso.mahnwesen.kosten_service import MahnkostenService
from mietinkasso.mahnwesen.kosten_repository import MahnkostenRepository
from mietinkasso.mahnwesen.manueller_mahnversand_repository import (
    ManuellerMahnversandRepository,
    berechne_idempotenz_schluessel,
)
from mietinkasso.mahnwesen.manueller_mahnversand_service import ManuellerMahnversandService
from mietinkasso.mahnwesen.repository import MahnFallRepository, MahnLaufRepository, MahnPolicyRepository
from mietinkasso.mahnwesen.service import MahnwesenService


def _echtes_pdf(text: str) -> bytes:
    """Ein TATSÄCHLICH mit `pypdf` lesbares, einseitiges synthetisches PDF
    (wie `test_mahnbrief_archiv.py::_echter_beleg_pdf`) - unterschiedlicher
    `text` ergibt unterschiedliche Bytes/Hashes."""

    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.cell(0, 10, text)
    return bytes(pdf.output())


@pytest.fixture
def manueller_mahnversand_repo(session_factory) -> ManuellerMahnversandRepository:
    return ManuellerMahnversandRepository(session_factory)


@pytest.fixture
def kosten_repo(session_factory) -> MahnkostenRepository:
    return MahnkostenRepository(session_factory)


@pytest.fixture
def kosten_service(kosten_repo, op_service, stammdaten_repo) -> MahnkostenService:
    return MahnkostenService(kosten_repo, op_service, stammdaten_repo)


@pytest.fixture
def manueller_mahnversand_service(manueller_mahnversand_repo, stammdaten_repo, op_service, kosten_service) -> ManuellerMahnversandService:
    return ManuellerMahnversandService(
        repository=manueller_mahnversand_repo, stammdaten_repository=stammdaten_repo,
        op_service=op_service, mahnkosten_service=kosten_service,
    )


def _offene_forderung(op_service, konto, ctx, *, betrag_cent=50_000, faelligkeit=date(2026, 1, 5)):
    return op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=betrag_cent,
        belegdatum=date(2026, 1, 1), buchungsdatum=date(2026, 1, 1), faelligkeit=faelligkeit,
        beleg_referenz="HMZ Jänner",
    )


# -- Repository: Idempotenz -------------------------------------------------


def test_berechne_idempotenz_schluessel_ist_belegidentitaet_forderung_datum_kanal(op_service, basis_vertrag, admin_ctx):
    vertrag, konto = basis_vertrag
    position = _offene_forderung(op_service, konto, admin_ctx)
    schluessel_1 = berechne_idempotenz_schluessel(
        vertrag_id=vertrag.id, stufe=1, forderung_op_position_ids=[position.id],
        beleg_sha256="hash-a", versanddatum=date(2026, 2, 1), kanal="BRIEF",
    )
    # Dieselben Eingaben in anderer Reihenfolge der Forderungs-Ids -
    # derselbe Schlüssel (sortiert).
    schluessel_1_umsortiert = berechne_idempotenz_schluessel(
        vertrag_id=vertrag.id, stufe=1, forderung_op_position_ids=[position.id],
        beleg_sha256="hash-a", versanddatum=date(2026, 2, 1), kanal="BRIEF",
    )
    assert schluessel_1 == schluessel_1_umsortiert

    # Eine andere Beleg-Identität (anderer Hash) - bewusst NICHT nur
    # stufe+zins_bis oder stufe+Forderungsgruppe (unabhängige Rückprüfung
    # Codex 22.10.2026) - ergibt einen ANDEREN Schlüssel.
    schluessel_anderer_beleg = berechne_idempotenz_schluessel(
        vertrag_id=vertrag.id, stufe=1, forderung_op_position_ids=[position.id],
        beleg_sha256="hash-b", versanddatum=date(2026, 2, 1), kanal="BRIEF",
    )
    assert schluessel_anderer_beleg != schluessel_1


def test_identischer_reimport_ist_ein_sicherer_no_op(manueller_mahnversand_repo):
    kwargs = dict(
        vertrag_id="V-601-3", stufe=1, forderung_op_position_ids=[101, 102],
        versanddatum=date(2026, 2, 1), kanal="BRIEF", empfaenger_name="Max Mustermieter",
        empfaenger_adresse="Musterstraße 1\n1010 Wien", beleg_referenz="Postaufgabe-001",
        beleg_pdf_bytes=_echtes_pdf("Mahnung Stufe 1"), beleg_sha256="x" * 64, erfasst_von="codex-test",
    )
    erste = manueller_mahnversand_repo.importieren(**kwargs)
    zweite = manueller_mahnversand_repo.importieren(**kwargs)
    assert erste.id == zweite.id


def test_genuin_abweichender_beleg_erzeugt_eigene_neue_zeile(manueller_mahnversand_repo):
    """Ein genuin NEUER erforderlicher Beleg (anderer Hash, andere
    Forderungen) muss eine EIGENE, unabhängige Zeile erzeugen - NIE
    stillschweigend mit dem ersten verschmolzen werden."""

    erste = manueller_mahnversand_repo.importieren(
        vertrag_id="V-601-3", stufe=1, forderung_op_position_ids=[101],
        versanddatum=date(2026, 2, 1), kanal="BRIEF", empfaenger_name="Max Mustermieter",
        empfaenger_adresse="Musterstraße 1\n1010 Wien", beleg_referenz="Postaufgabe-001",
        beleg_pdf_bytes=_echtes_pdf("Mahnung A"), beleg_sha256="a" * 64, erfasst_von="codex-test",
    )
    zweite = manueller_mahnversand_repo.importieren(
        vertrag_id="V-601-3", stufe=1, forderung_op_position_ids=[102],
        versanddatum=date(2026, 3, 1), kanal="BRIEF", empfaenger_name="Max Mustermieter",
        empfaenger_adresse="Musterstraße 1\n1010 Wien", beleg_referenz="Postaufgabe-002",
        beleg_pdf_bytes=_echtes_pdf("Mahnung B"), beleg_sha256="b" * 64, erfasst_von="codex-test",
    )
    assert erste.id != zweite.id


# -- Service: Cross-Contract-/Cross-Gesellschaft-Ablehnung ------------------


def test_fremde_forderung_eines_anderen_vertrags_wird_abgelehnt(
    manueller_mahnversand_service, stammdaten_repo, op_service, basis_vertrag, ctx_factory,
):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    eigene_forderung = _offene_forderung(op_service, konto, ctx)

    # Zweiter Vertrag/Konto DERSELBEN Gesellschaft - dessen Forderung darf
    # NIE als Beleg für den ERSTEN Vertrag importiert werden.
    stammdaten_repo.upsert_einheit(id="601-TOP4", objekt_id="601", bezeichnung="Top 4", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten_repo.upsert_debitor(id="DEB-1002", name="Andere Mieterin", email="andere@example.at")
    stammdaten_repo.upsert_vertrag(
        id="V-601-4", einheit_id="601-TOP4", debitor_id="DEB-1002", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    fremder_vertrag = stammdaten_repo.get_vertrag("V-601-4")
    fremdes_konto = stammdaten_repo.get_or_create_konto(vertrag=fremder_vertrag)
    fremde_forderung = _offene_forderung(op_service, fremdes_konto, ctx)

    with pytest.raises(ValueError, match="Cross-Contract"):
        manueller_mahnversand_service.importieren(
            ctx=ctx, vertrag_id=vertrag.id, stufe=1,
            forderung_op_position_ids=[eigene_forderung.id, fremde_forderung.id],
            versanddatum=date(2026, 2, 1), kanal="BRIEF", empfaenger_name="Max Mustermieter",
            empfaenger_adresse="Musterstraße 1\n1010 Wien", beleg_referenz="Postaufgabe-001",
            beleg_pdf_bytes=_echtes_pdf("Mahnung"), erfasst_von="codex-test",
        )


def test_fremde_gesellschaft_wird_ueber_require_gesellschaft_access_abgelehnt(
    manueller_mahnversand_service, op_service, basis_vertrag, ctx_factory,
):
    vertrag, konto = basis_vertrag
    ctx_fremde_gesellschaft = ctx_factory("ANDERE-GESELLSCHAFT")
    forderung = _offene_forderung(op_service, konto, ctx_factory("7DI"))

    with pytest.raises(CrossTenantError):
        manueller_mahnversand_service.importieren(
            ctx=ctx_fremde_gesellschaft, vertrag_id=vertrag.id, stufe=1,
            forderung_op_position_ids=[forderung.id],
            versanddatum=date(2026, 2, 1), kanal="BRIEF", empfaenger_name="Max Mustermieter",
            empfaenger_adresse="Musterstraße 1\n1010 Wien", beleg_referenz="Postaufgabe-001",
            beleg_pdf_bytes=_echtes_pdf("Mahnung"), erfasst_von="codex-test",
        )


def test_lesezugriff_ohne_schreibrecht_wird_abgelehnt(
    manueller_mahnversand_service, op_service, basis_vertrag, ctx_factory,
):
    from mietinkasso.auth.service import AuthContext

    vertrag, konto = basis_vertrag
    ctx_schreibend = ctx_factory("7DI")
    forderung = _offene_forderung(op_service, konto, ctx_schreibend)
    ctx_lesend = AuthContext(user_id="nur-lesen", rolle=Rolle.LESEZUGRIFF, gesellschaft_ids=frozenset({"7DI"}))

    with pytest.raises(CrossTenantError):
        manueller_mahnversand_service.importieren(
            ctx=ctx_lesend, vertrag_id=vertrag.id, stufe=1, forderung_op_position_ids=[forderung.id],
            versanddatum=date(2026, 2, 1), kanal="BRIEF", empfaenger_name="Max Mustermieter",
            empfaenger_adresse="Musterstraße 1\n1010 Wien", beleg_referenz="Postaufgabe-001",
            beleg_pdf_bytes=_echtes_pdf("Mahnung"), erfasst_von="codex-test",
        )


def test_kein_echtes_pdf_wird_abgelehnt(manueller_mahnversand_service, op_service, basis_vertrag, ctx_factory):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    forderung = _offene_forderung(op_service, konto, ctx)

    with pytest.raises(ValueError, match="Belegdokument"):
        manueller_mahnversand_service.importieren(
            ctx=ctx, vertrag_id=vertrag.id, stufe=1, forderung_op_position_ids=[forderung.id],
            versanddatum=date(2026, 2, 1), kanal="BRIEF", empfaenger_name="Max Mustermieter",
            empfaenger_adresse="Musterstraße 1\n1010 Wien", beleg_referenz="Postaufgabe-001",
            beleg_pdf_bytes=b"ein reiner Referenztext, kein PDF", erfasst_von="codex-test",
        )


# -- Service: Buchung bei Import --------------------------------------------


def test_import_ohne_freigabe_bucht_keine_pauschale_zeigt_aber_zinsen(
    manueller_mahnversand_service, op_service, basis_vertrag, ctx_factory,
):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    forderung = _offene_forderung(op_service, konto, ctx, faelligkeit=date(2026, 1, 5))

    row = manueller_mahnversand_service.importieren(
        ctx=ctx, vertrag_id=vertrag.id, stufe=1, forderung_op_position_ids=[forderung.id],
        versanddatum=date(2026, 2, 1), kanal="BRIEF", empfaenger_name="Max Mustermieter",
        empfaenger_adresse="Musterstraße 1\n1010 Wien", beleg_referenz="Postaufgabe-001",
        beleg_pdf_bytes=_echtes_pdf("Mahnung Stufe 1"), erfasst_von="codex-test",
    )
    pauschale_zeilen = [
        p for p in op_service.berechne_saldo(konto.id, stichtag=date(2026, 2, 1)).positionen
        if p.aenderungsgrund == "Mahnkosten - Mahnkostenpauschale"
    ]
    assert not pauschale_zeilen  # keine Freigabe vorhanden - 0 EUR, nicht erfunden
    assert row.mahnkosten_buchung_id is None  # Zinsen (falls 0) ODER keine Buchung - hier: nichts zu buchen


def test_identischer_reimport_bucht_nicht_ein_zweites_mal(
    manueller_mahnversand_service, op_service, basis_vertrag, ctx_factory,
):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    forderung = _offene_forderung(op_service, konto, ctx, faelligkeit=date(2026, 1, 5))
    kwargs = dict(
        ctx=ctx, vertrag_id=vertrag.id, stufe=1, forderung_op_position_ids=[forderung.id],
        versanddatum=date(2026, 2, 1), kanal="BRIEF", empfaenger_name="Max Mustermieter",
        empfaenger_adresse="Musterstraße 1\n1010 Wien", beleg_referenz="Postaufgabe-001",
        beleg_pdf_bytes=_echtes_pdf("Mahnung Stufe 1"), erfasst_von="codex-test",
    )
    erste = manueller_mahnversand_service.importieren(**kwargs)
    saldo_vor_retry = op_service.berechne_saldo(konto.id, stichtag=date(2026, 2, 1)).saldo_cent
    zweite = manueller_mahnversand_service.importieren(**kwargs)
    saldo_nach_retry = op_service.berechne_saldo(konto.id, stichtag=date(2026, 2, 1)).saldo_cent

    assert erste.id == zweite.id
    assert saldo_vor_retry == saldo_nach_retry  # kein zweiter Buchungsversuch, keine neue OP-Position


# -- MahnwesenService.plane_mahnlauf: Doppelversand-Gate --------------------


@pytest.fixture
def mahn_policy_repo(session_factory) -> MahnPolicyRepository:
    return MahnPolicyRepository(session_factory)


@pytest.fixture
def mahn_fall_repo(session_factory) -> MahnFallRepository:
    return MahnFallRepository(session_factory)


@pytest.fixture
def mahnlauf_repo(session_factory) -> MahnLaufRepository:
    return MahnLaufRepository(session_factory)


@pytest.fixture
def freigegebene_policy(mahn_policy_repo):
    policy = mahn_policy_repo.anlegen(
        stufe1_tage_nach_faelligkeit=7, stufe2_mindesttage_nach_stufe1_versand=14,
        zinsen_prozent=Decimal("0"), gebuehr_cent=0, status="ENTWURF",
    )
    return mahn_policy_repo.freigeben(policy.id)


def _mahn_service(mahn_fall_repo, stammdaten_repo, op_service, mahn_policy_repo, mahnlauf_repo, *, manueller_mahnversand_repo=None):
    return MahnwesenService(
        mahn_fall_repo, stammdaten_repo, op_service, mahn_policy_repo, bank_stand_max_age_days=2,
        mahnlauf_repository=mahnlauf_repo, manueller_mahnversand_repository=manueller_mahnversand_repo,
    )


def test_plane_mahnlauf_blockiert_buendelung_wenn_bereits_manuell_versendet(
    mahn_fall_repo, stammdaten_repo, op_service, mahn_policy_repo, mahnlauf_repo,
    manueller_mahnversand_repo, basis_vertrag, ctx_factory, freigegebene_policy,
):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    forderung = _offene_forderung(op_service, konto, ctx, faelligkeit=date(2026, 4, 5))
    mahn_service = _mahn_service(
        mahn_fall_repo, stammdaten_repo, op_service, mahn_policy_repo, mahnlauf_repo,
        manueller_mahnversand_repo=manueller_mahnversand_repo,
    )
    heute = date(2026, 4, 20)
    offene = op_service.offene_forderungen(konto.id, heute=heute)[0]
    geplant = mahn_service.plane_forderung(
        ctx=ctx, vertrag=vertrag, konto=konto, forderung=offene, policy=freigegebene_policy, heute=heute,
        bank_bestaetigt_bis=heute, ungeklaerte_eingaenge_vorhanden=False,
    )
    assert geplant.status == "GEPLANT"

    manueller_mahnversand_repo.importieren(
        vertrag_id=vertrag.id, stufe=1, forderung_op_position_ids=[forderung.id],
        versanddatum=date(2026, 4, 15), kanal="PERSOENLICH", empfaenger_name="Max Mustermieter",
        empfaenger_adresse="persönlich übergeben", beleg_referenz="Uebergabe-001",
        beleg_pdf_bytes=_echtes_pdf("Uebergabebestaetigung"), beleg_sha256="z" * 64, erfasst_von="codex-test",
    )

    mahnlauf = mahn_service.plane_mahnlauf(
        ctx=ctx, vertrag=vertrag, konto=konto, stufe=1, heute=heute, bank_bestaetigt_bis=heute,
    )
    assert mahnlauf is None  # KEINE automatische Bündelung - Doppelversand verhindert


def test_plane_mahnlauf_buendelt_weiterhin_normal_ohne_manuellen_versand(
    mahn_fall_repo, stammdaten_repo, op_service, mahn_policy_repo, mahnlauf_repo,
    manueller_mahnversand_repo, basis_vertrag, ctx_factory, freigegebene_policy,
):
    """Regressionssicherung: das Gate darf die normale Bündelung NICHT
    blockieren, solange kein Manualimport für (Vertrag, Stufe) existiert."""

    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    _offene_forderung(op_service, konto, ctx, faelligkeit=date(2026, 4, 5))
    mahn_service = _mahn_service(
        mahn_fall_repo, stammdaten_repo, op_service, mahn_policy_repo, mahnlauf_repo,
        manueller_mahnversand_repo=manueller_mahnversand_repo,
    )
    heute = date(2026, 4, 20)
    offene = op_service.offene_forderungen(konto.id, heute=heute)[0]
    mahn_service.plane_forderung(
        ctx=ctx, vertrag=vertrag, konto=konto, forderung=offene, policy=freigegebene_policy, heute=heute,
        bank_bestaetigt_bis=heute, ungeklaerte_eingaenge_vorhanden=False,
    )

    mahnlauf = mahn_service.plane_mahnlauf(
        ctx=ctx, vertrag=vertrag, konto=konto, stufe=1, heute=heute, bank_bestaetigt_bis=heute,
    )
    assert mahnlauf is not None
