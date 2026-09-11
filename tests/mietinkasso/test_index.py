from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from mietinkasso.domain.enums import IndexAnpassungStatus
from mietinkasso.domain.exceptions import IndexKlauselFehltError
from mietinkasso.domain.money import round_index_half_cent_down
from mietinkasso.index.repository import IndexRepository
from mietinkasso.index.service import IndexService


@pytest.fixture
def index_service(session_factory, stammdaten_repo) -> IndexService:
    return IndexService(IndexRepository(session_factory), stammdaten_repo)


def test_halber_cent_wird_laut_par1_abs2_z3_abgerundet():
    # 10.005 ist exakt der halbe Cent -> abwärts auf 10.00, NICHT 10.01
    assert round_index_half_cent_down(Decimal("10.005")) == Decimal("10.00")
    # Alles andere rundet normal kaufmännisch
    assert round_index_half_cent_down(Decimal("10.006")) == Decimal("10.01")
    assert round_index_half_cent_down(Decimal("10.004")) == Decimal("10.00")


def test_fehlende_klausel_blockiert_erhoehung(index_service, basis_vertrag):
    vertrag, _ = basis_vertrag
    with pytest.raises(IndexKlauselFehltError):
        index_service.berechne_vorschlag(
            vertrag_id=vertrag.id, stichtag=date(2026, 4, 1), neuer_wert=Decimal("110.0"), quelle_referenz="VPI"
        )


def test_bk_vorauszahlung_wird_nicht_indexiert(index_service, stammdaten_repo, basis_vertrag):
    vertrag, _ = basis_vertrag
    stammdaten_repo.add_komponente(
        id="K-HMZ", vertrag_id=vertrag.id, art="HMZ", bezeichnung="Hauptmietzins", betrag_cent=50_000,
        indexierbar=True, gueltig_von=date(2024, 1, 1),
    )
    # BK-Vorauszahlung wird versehentlich auch als indexierbar markiert -> muss trotzdem ausgeschlossen bleiben
    stammdaten_repo.add_komponente(
        id="K-BKVZ", vertrag_id=vertrag.id, art="BK_VORAUSZAHLUNG", bezeichnung="BK-Vorauszahlung",
        betrag_cent=12_000, indexierbar=True, gueltig_von=date(2024, 1, 1),
    )
    klausel = index_service.klausel_anlegen(
        vertrag_id=vertrag.id, rechtsordnung="OESTERREICH_MRG_VOLL", abschlussdatum=date(2024, 1, 1),
        basis_reihe="VPI2020", basis_wert=Decimal("100.0"), basis_monat="2024-01",
    )
    assert index_service._repository.freigegebene_klausel(vertrag.id) is None

    index_service.klausel_freigeben(klausel.id, freigegeben_von="markus")

    vorschlag = index_service.berechne_vorschlag(
        vertrag_id=vertrag.id, stichtag=date(2026, 4, 1), neuer_wert=Decimal("110.0"), quelle_referenz="VPI"
    )
    # 10% auf 500,00 EUR HMZ = 50,00 EUR; BK 120,00 EUR bleibt unangetastet
    assert vorschlag.erhoehung_cent == 5_000


def test_aenderung_nach_freigabe_invalidiert_offenen_vorschlag(index_service, stammdaten_repo, basis_vertrag):
    vertrag, _ = basis_vertrag
    stammdaten_repo.add_komponente(
        id="K-HMZ", vertrag_id=vertrag.id, art="HMZ", bezeichnung="Hauptmietzins", betrag_cent=50_000,
        indexierbar=True, gueltig_von=date(2024, 1, 1),
    )
    klausel_v1 = index_service.klausel_anlegen(
        vertrag_id=vertrag.id, rechtsordnung="OESTERREICH_MRG_VOLL", abschlussdatum=date(2024, 1, 1),
        basis_reihe="VPI2020", basis_wert=Decimal("100.0"), basis_monat="2024-01",
    )
    index_service.klausel_freigeben(klausel_v1.id, freigegeben_von="markus")
    vorschlag = index_service.berechne_vorschlag(
        vertrag_id=vertrag.id, stichtag=date(2026, 4, 1), neuer_wert=Decimal("110.0"), quelle_referenz="VPI"
    )
    assert vorschlag.status == IndexAnpassungStatus.VORSCHLAG.value

    # Neue Version der Klausel ersetzt die freigegebene -> offener Vorschlag wird ungültig
    index_service.klausel_anlegen(
        vertrag_id=vertrag.id, rechtsordnung="OESTERREICH_MRG_VOLL", abschlussdatum=date(2024, 1, 1),
        basis_reihe="VPI2020", basis_wert=Decimal("102.0"), basis_monat="2025-01",
    )
    invalidiert = index_service._repository.get_anpassung(vorschlag.id)
    assert invalidiert.status == IndexAnpassungStatus.INVALIDIERT.value
