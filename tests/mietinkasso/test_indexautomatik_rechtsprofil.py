from __future__ import annotations

from datetime import date

import pytest

from mietinkasso.domain.exceptions import QuellenbelegFehltError
from mietinkasso.index.repository import IndexRepository
from mietinkasso.indexautomatik.rechtsprofil import RechtsprofilService
from mietinkasso.indexautomatik.repository import RechtsprofilRepository
from mietinkasso.infrastructure.db.tables import IndexKlauselTable


@pytest.fixture
def rechtsprofil_repo(session_factory) -> RechtsprofilRepository:
    return RechtsprofilRepository(session_factory)


@pytest.fixture
def index_repo(session_factory) -> IndexRepository:
    return IndexRepository(session_factory)


@pytest.fixture
def rechtsprofil_service(rechtsprofil_repo, stammdaten_repo, index_repo) -> RechtsprofilService:
    return RechtsprofilService(rechtsprofil_repo, stammdaten_repo, index_repo)


def _standard_kwargs(**overrides) -> dict:
    basis = dict(
        rechtsordnung="OESTERREICH_MRG_VOLL",
        ist_wohnungsnutzung=True,
        mrg_zinsbeschraenkung=False,
        mrg_zinsbeschraenkung_geprueft=True,
        ist_altvertrag=False,
        ist_hauptmiete=True,
        foerderbindung=False,
        foerderbindung_geprueft=True,
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


def test_statischer_betrag_und_vertragsklausel_schliessen_sich_aus(
    admin_ctx, basis_vertrag, rechtsprofil_service, stammdaten_repo, index_repo
):
    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag)
    klausel = index_repo.anlegen(
        IndexKlauselTable(
            vertrag_id=vertrag.id,
            version=1,
            rechtsordnung=vertrag.rechtsordnung,
            berechnungsprofil="EINFACHER_SCHWELLENVERGLEICH",
            abschlussdatum=date(2024, 1, 1),
            basis_reihe="VPI20C18",
            basis_wert=100,
            basis_monat="2024-01",
        )
    )
    with pytest.raises(ValueError):
        rechtsprofil_service.entwurf_anlegen(
            ctx=admin_ctx,
            vertrag_id=vertrag.id,
            **_standard_kwargs(vertragsklausel_id=klausel.id),
        )


def test_freigabe_ohne_komponenten_wird_abgelehnt(admin_ctx, basis_vertrag, rechtsprofil_service, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag)
    profil = rechtsprofil_service.entwurf_anlegen(
        ctx=admin_ctx, vertrag_id=vertrag.id, **_standard_kwargs(basis_komponenten_ids=[])
    )
    with pytest.raises(ValueError):
        rechtsprofil_service.freigeben(profil.id, ctx=admin_ctx, freigegeben_von="markus")


def test_spaet_importierte_komponente_ohne_beleg_blockiert_freigabe(
    admin_ctx, basis_vertrag, rechtsprofil_service, stammdaten_repo
):
    """Codex-Rückprüfung: "Historisierungskette löst noch nicht initial
    importierte Bestandskomponenten" - Vertragsbeginn 1.4., belegte
    Indexbasis Februar, eine unveränderte Pauschale aber erst ab 1.8. als
    Komponente importiert. Ohne expliziten Ausnahmenachweis bleibt die
    Freigabe gesperrt - ein bloß spät importierter Altbestand darf nicht
    stillschweigend als "hat nicht bestanden" ODER als "hat bestanden"
    angenommen werden."""

    vertrag, _konto = basis_vertrag
    stammdaten_repo.add_komponente(
        id="K-1", vertrag_id=vertrag.id, art="HMZ", bezeichnung="Pauschale", betrag_cent=50_000,
        indexierbar=True, gueltig_von=date(2026, 8, 1),
    )
    entwurf = rechtsprofil_service.entwurf_anlegen(
        ctx=admin_ctx, vertrag_id=vertrag.id,
        **_standard_kwargs(bezugsjahr=2026, bezugsmonat=2, basis_komponenten_ids=["K-1"]),
    )
    with pytest.raises(ValueError, match="hat sie noch nicht bestanden"):
        rechtsprofil_service.freigeben(entwurf.id, ctx=admin_ctx, freigegeben_von="markus")


def test_belegte_historische_basis_hebt_sperre_fuer_spaeten_import_auf(
    admin_ctx, basis_vertrag, rechtsprofil_service, stammdaten_repo
):
    vertrag, _konto = basis_vertrag
    stammdaten_repo.add_komponente(
        id="K-1", vertrag_id=vertrag.id, art="HMZ", bezeichnung="Pauschale", betrag_cent=50_000,
        indexierbar=True, gueltig_von=date(2026, 8, 1),
    )
    entwurf = rechtsprofil_service.entwurf_anlegen(
        ctx=admin_ctx, vertrag_id=vertrag.id,
        **_standard_kwargs(
            bezugsjahr=2026, bezugsmonat=2, basis_komponenten_ids=["K-1"],
            historische_basis_belege={
                "K-1": {
                    "betrag_cent": 50_000, "datum": "2026-02-01",
                    "quellenbeleg": "Mietvertrag Punkt 3, Altbestand seit Vertragsbeginn",
                },
            },
        ),
    )
    profil = rechtsprofil_service.freigeben(entwurf.id, ctx=admin_ctx, freigegeben_von="markus")
    assert profil.status == "FREIGEGEBEN"


def test_unvollstaendiger_historischer_beleg_hebt_sperre_nicht_auf(
    admin_ctx, basis_vertrag, rechtsprofil_service, stammdaten_repo
):
    """Ein fehlender Quellenbeleg darf die Sperre NICHT aufheben - kein
    Rateversuch, kein automatisches Durchwinken eines unvollständigen
    Nachweises."""

    vertrag, _konto = basis_vertrag
    stammdaten_repo.add_komponente(
        id="K-1", vertrag_id=vertrag.id, art="HMZ", bezeichnung="Pauschale", betrag_cent=50_000,
        indexierbar=True, gueltig_von=date(2026, 8, 1),
    )
    entwurf = rechtsprofil_service.entwurf_anlegen(
        ctx=admin_ctx, vertrag_id=vertrag.id,
        **_standard_kwargs(
            bezugsjahr=2026, bezugsmonat=2, basis_komponenten_ids=["K-1"],
            historische_basis_belege={"K-1": {"betrag_cent": 50_000, "datum": "2026-02-01", "quellenbeleg": ""}},
        ),
    )
    with pytest.raises(ValueError, match="hat sie noch nicht bestanden"):
        rechtsprofil_service.freigeben(entwurf.id, ctx=admin_ctx, freigegeben_von="markus")


def test_geaenderte_vertragsklausel_entwertet_freigabe(
    admin_ctx, basis_vertrag, rechtsprofil_service, stammdaten_repo, index_repo
):
    from mietinkasso.infrastructure.db.tables import IndexKlauselTable

    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag)
    klausel = index_repo.anlegen(
        IndexKlauselTable(
            vertrag_id=vertrag.id,
            version=1,
            rechtsordnung=vertrag.rechtsordnung,
            berechnungsprofil="EINFACHER_SCHWELLENVERGLEICH",
            abschlussdatum=date(2024, 1, 1),
            basis_reihe="VPI20C18",
            basis_wert=100,
            basis_monat="2024-01",
        )
    )
    profil = rechtsprofil_service.entwurf_anlegen(
        ctx=admin_ctx,
        vertrag_id=vertrag.id,
        **_standard_kwargs(vertraglich_zulaessiger_betrag_cent=None, vertraglicher_quellenbeleg=None, vertragsklausel_id=klausel.id),
    )
    freigegeben = rechtsprofil_service.freigeben(profil.id, ctx=admin_ctx, freigegeben_von="markus")
    assert rechtsprofil_service.ist_noch_gueltig(freigegeben) is True

    with stammdaten_repo._session_factory() as session:
        row = session.get(IndexKlauselTable, klausel.id)
        row.schwelle_prozent = 5
        session.commit()

    assert rechtsprofil_service.ist_noch_gueltig(freigegeben) is False


def test_abgelaufene_mietzinsobergrenze_entwertet_freigabe(admin_ctx, basis_vertrag, rechtsprofil_service, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag)
    profil = rechtsprofil_service.entwurf_anlegen(
        ctx=admin_ctx,
        vertrag_id=vertrag.id,
        **_standard_kwargs(
            foerderbindung=True,
            mietzinsobergrenze_cent=90_000,
            mietzinsobergrenze_quellenbeleg="Förderzusicherung Punkt 3",
            mietzinsobergrenze_gueltig_bis=date(2025, 12, 31),
        ),
    )
    freigegeben = rechtsprofil_service.freigeben(profil.id, ctx=admin_ctx, freigegeben_von="markus")
    assert rechtsprofil_service.ist_noch_gueltig(freigegeben, heute=date(2025, 6, 1)) is True
    assert rechtsprofil_service.ist_noch_gueltig(freigegeben, heute=date(2026, 1, 1)) is False


def test_abweichende_vpi_reihe_fuer_mieweg_gesetzesspur_wird_abgelehnt(admin_ctx, basis_vertrag, rechtsprofil_service, stammdaten_repo):
    """UI-Endprüfung (6317f96): "MieWeG-Gesetzesspur muss VPI20C18
    verwenden" - eine abweichende Reihe wird bereits beim Entwurf
    abgelehnt, nicht erst im Monatslauf."""

    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag)
    with pytest.raises(ValueError):
        rechtsprofil_service.entwurf_anlegen(
            ctx=admin_ctx, vertrag_id=vertrag.id, **_standard_kwargs(vpi_reihe="VPI15C18")
        )


def test_bestaetigte_mrg_zinsbeschraenkung_ohne_obergrenze_blockiert_freigabe(
    admin_ctx, basis_vertrag, rechtsprofil_service, stammdaten_repo
):
    """UI-Endprüfung (6317f96): "Fehlende belegte Mietzinsobergrenze bei
    MRG-Voll darf nicht als unbeschränkt gelten, mindestens für
    bestätigte Zinsbeschränkung Pflicht bei Freigabe"."""

    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag)
    profil = rechtsprofil_service.entwurf_anlegen(
        ctx=admin_ctx, vertrag_id=vertrag.id,
        **_standard_kwargs(mrg_zinsbeschraenkung=True, mietzinsobergrenze_cent=None, mietzinsobergrenze_quellenbeleg=None),
    )
    with pytest.raises(ValueError):
        rechtsprofil_service.freigeben(profil.id, ctx=admin_ctx, freigegeben_von="markus")


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
