from __future__ import annotations

from datetime import date

import pytest

from mietinkasso.domain.exceptions import QuellenbelegFehltError
from mietinkasso.indexautomatik.rechtsprofil import RechtsprofilService
from mietinkasso.indexautomatik.repository import RechtsprofilRepository


@pytest.fixture
def rechtsprofil_repo(session_factory) -> RechtsprofilRepository:
    return RechtsprofilRepository(session_factory)


@pytest.fixture
def rechtsprofil_service(rechtsprofil_repo, stammdaten_repo) -> RechtsprofilService:
    return RechtsprofilService(rechtsprofil_repo, stammdaten_repo)


def _standard_kwargs(**overrides) -> dict:
    basis = dict(
        rechtsordnung="OESTERREICH_MRG_VOLL",
        ist_wohnungsnutzung=True,
        mrg_zinsbeschraenkung=False,
        ist_altvertrag=False,
        ist_hauptmiete=True,
        foerderbindung=False,
        mietzinsobergrenze_cent=None,
        mietzinsobergrenze_quellenbeleg=None,
        mietzinsobergrenze_gueltig_bis=None,
        bezugsjahr=2024,
        bezugsmonat=1,
        letzte_basis_war_jahresdurchschnitt=False,
        basis_komponenten_ids=["K-1"],
        vertraglich_zulaessiger_betrag_cent=101_000,
        vertraglicher_quellenbeleg="Mietvertrag Punkt 5",
        vertraglicher_fruehestmoeglicher_termin=date(2026, 4, 1),
        vertrag_beleg_referenz="Mietvertrag V-601-3, unterfertigt 2024-01-01",
        klausel_referenz="Punkt 5 Wertsicherung",
        erstellt_von="markus",
    )
    basis.update(overrides)
    return basis


def _mit_komponente(stammdaten_repo, vertrag, *, betrag_cent=100_000):
    stammdaten_repo.add_komponente(
        id="K-1",
        vertrag_id=vertrag.id,
        art="HMZ",
        bezeichnung="Hauptmietzins",
        betrag_cent=betrag_cent,
        indexierbar=True,
        gueltig_von=date(2024, 1, 1),
    )


def test_entwurf_ohne_vertragsbeleg_wird_abgelehnt(admin_ctx, basis_vertrag, rechtsprofil_service, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag)
    with pytest.raises(QuellenbelegFehltError):
        rechtsprofil_service.entwurf_anlegen(
            ctx=admin_ctx, vertrag_id=vertrag.id, **_standard_kwargs(vertrag_beleg_referenz="")
        )


def test_foerderbindung_ohne_obergrenze_wird_abgelehnt(admin_ctx, basis_vertrag, rechtsprofil_service, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag)
    with pytest.raises(QuellenbelegFehltError):
        rechtsprofil_service.entwurf_anlegen(
            ctx=admin_ctx,
            vertrag_id=vertrag.id,
            **_standard_kwargs(foerderbindung=True, mietzinsobergrenze_cent=None, mietzinsobergrenze_quellenbeleg=None),
        )


def test_freigabe_setzt_status_und_quelle_hash(admin_ctx, basis_vertrag, rechtsprofil_service, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag)
    profil = rechtsprofil_service.entwurf_anlegen(ctx=admin_ctx, vertrag_id=vertrag.id, **_standard_kwargs())
    freigegeben = rechtsprofil_service.freigeben(profil.id, ctx=admin_ctx, freigegeben_von="markus")
    assert freigegeben.status == "FREIGEGEBEN"
    assert freigegeben.quelle_hash
    assert rechtsprofil_service.ist_noch_gueltig(freigegeben) is True
    assert rechtsprofil_service.aktives_gueltiges_profil(vertrag.id).id == freigegeben.id


def test_neue_freigabe_invalidiert_alte_version(admin_ctx, basis_vertrag, rechtsprofil_service, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag)
    erste = rechtsprofil_service.entwurf_anlegen(ctx=admin_ctx, vertrag_id=vertrag.id, **_standard_kwargs())
    rechtsprofil_service.freigeben(erste.id, ctx=admin_ctx, freigegeben_von="markus")

    zweite = rechtsprofil_service.entwurf_anlegen(
        ctx=admin_ctx, vertrag_id=vertrag.id, **_standard_kwargs(vertraglich_zulaessiger_betrag_cent=105_000)
    )
    rechtsprofil_service.freigeben(zweite.id, ctx=admin_ctx, freigegeben_von="markus")

    profile = {p.id: p for p in rechtsprofil_service.liste_fuer_vertrag(vertrag.id)}
    assert profile[erste.id].status == "INVALIDIERT"
    assert profile[zweite.id].status == "FREIGEGEBEN"


def test_geaenderte_komponente_entwertet_freigabe(admin_ctx, basis_vertrag, rechtsprofil_service, stammdaten_repo):
    """Zentraler Abnahmefall: "Änderungen an Quelle/Basis/Vertrag/Profil
    entwerten alte Freigabe" - eine nachträglich am referenzierten
    Komponentenbetrag geänderte Stammdatenzeile (z. B. eine korrigierte
    HMZ) macht ein bereits freigegebenes Rechtsprofil automatisch
    ungültig, ohne dass der Status manuell nachgezogen werden müsste."""

    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag, betrag_cent=100_000)
    profil = rechtsprofil_service.entwurf_anlegen(ctx=admin_ctx, vertrag_id=vertrag.id, **_standard_kwargs())
    freigegeben = rechtsprofil_service.freigeben(profil.id, ctx=admin_ctx, freigegeben_von="markus")
    assert rechtsprofil_service.ist_noch_gueltig(freigegeben) is True

    # Stammdaten-Korrektur am referenzierten Komponentenbetrag NACH der
    # Freigabe (z. B. durch einen künftigen Korrektur-Workflow) - hier
    # direkt über die DB simuliert, um unabhängig vom konkreten
    # Korrekturpfad zu bleiben.
    with stammdaten_repo._session_factory() as session:
        from mietinkasso.infrastructure.db.tables import VertragsKomponenteTable

        komponente = session.get(VertragsKomponenteTable, "K-1")
        komponente.betrag_cent = 120_000
        session.commit()

    assert rechtsprofil_service.ist_noch_gueltig(freigegeben) is False
    assert rechtsprofil_service.aktives_gueltiges_profil(vertrag.id) is None


def test_geaendertes_vertragsende_entwertet_freigabe(admin_ctx, basis_vertrag, rechtsprofil_service, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag)
    profil = rechtsprofil_service.entwurf_anlegen(ctx=admin_ctx, vertrag_id=vertrag.id, **_standard_kwargs())
    freigegeben = rechtsprofil_service.freigeben(profil.id, ctx=admin_ctx, freigegeben_von="markus")

    stammdaten_repo.upsert_vertrag(
        id=vertrag.id,
        einheit_id=vertrag.einheit_id,
        debitor_id=vertrag.debitor_id,
        gesellschaft_id=vertrag.gesellschaft_id,
        rechtsordnung=vertrag.rechtsordnung,
        gueltig_von=vertrag.gueltig_von,
        gueltig_bis=date(2027, 12, 31),
    )

    assert rechtsprofil_service.ist_noch_gueltig(freigegeben) is False
