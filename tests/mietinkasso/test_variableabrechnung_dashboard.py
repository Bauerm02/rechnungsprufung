from __future__ import annotations

from datetime import date

import pytest

from mietinkasso.variableabrechnung.dashboard import berechne_monatsuebersicht
from mietinkasso.variableabrechnung.repository import VariableAbrechnungRepository
from mietinkasso.variableabrechnung.service import VariableAbrechnungService


@pytest.fixture
def repo(session_factory) -> VariableAbrechnungRepository:
    return VariableAbrechnungRepository(session_factory)


@pytest.fixture
def service(repo, stammdaten_repo) -> VariableAbrechnungService:
    return VariableAbrechnungService(repo, stammdaten_repo)


@pytest.fixture
def bestand(stammdaten_repo):
    stammdaten_repo.upsert_gesellschaft(id="7DI", name="7D Immobilien GmbH")
    stammdaten_repo.upsert_objekt(id="601", gesellschaft_id="7DI", bezeichnung="Am Corso")
    stammdaten_repo.upsert_einheit(id="601-TOP3", objekt_id="601", bezeichnung="Top 3", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten_repo.upsert_debitor(id="DEB-1", name="Max Mustermieter")
    stammdaten_repo.upsert_vertrag(
        id="V-601-3", einheit_id="601-TOP3", debitor_id="DEB-1", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    stammdaten_repo.add_komponente(
        id="K-HMZ", vertrag_id="V-601-3", art="HMZ", bezeichnung="Hauptmietzins", betrag_cent=100_000,
        gueltig_von=date(2024, 1, 1),
    )
    stammdaten_repo.add_komponente(
        id="K-BK", vertrag_id="V-601-3", art="BK_VORAUSZAHLUNG", bezeichnung="Betriebskosten", betrag_cent=15_000,
        gueltig_von=date(2024, 1, 1),
    )
    stammdaten_repo.upsert_einheit(id="601-KURZ1", objekt_id="601", bezeichnung="Kurzzeit 1", nutzungsstatus="KURZZEITVERMIETUNG")
    stammdaten_repo.upsert_einheit(id="601-STOR1", objekt_id="601", bezeichnung="Storage 1", nutzungsstatus="SELFSTORAGE")
    return None


def test_dauermiete_soll_netto_ohne_bk(admin_ctx, bestand, service, stammdaten_repo):
    uebersicht = berechne_monatsuebersicht(
        leistungsmonat="2026-08", stammdaten_repository=stammdaten_repo, variable_service=service
    )
    assert uebersicht.dauermiete_soll_netto_cent == 100_000  # NICHT 115.000 (BK ausgeschlossen)


def test_bestaetigter_kurzzeit_anteil_zaehlt(admin_ctx, bestand, service, stammdaten_repo):
    service.erfassen(
        ctx=admin_ctx, einheit_id="601-KURZ1", art="KURZZEITVERMIETUNG", leistungsmonat="2026-08",
        belegdatum=date(2026, 9, 5), quelle_referenz="Report", status="BESTAETIGT",
        unser_netto_anteil_cent=30_000, erstellt_von="markus",
    )
    service.erfassen(
        ctx=admin_ctx, einheit_id="601-STOR1", art="SELFSTORAGE", leistungsmonat="2026-08",
        belegdatum=date(2026, 9, 5), quelle_referenz="Report Storage", status="BESTAETIGT",
        unser_netto_anteil_cent=8_000, erstellt_von="markus",
    )
    uebersicht = berechne_monatsuebersicht(
        leistungsmonat="2026-08", stammdaten_repository=stammdaten_repo, variable_service=service
    )
    assert uebersicht.kurzzeit_netto_anteil_cent == 30_000
    assert uebersicht.selfstorage_netto_anteil_cent == 8_000
    assert uebersicht.nettomieterloes_cent == 100_000 + 30_000 + 8_000
    assert uebersicht.vollstaendig is True


def test_entwurf_wird_nicht_gezaehlt_aber_als_datenluecke_gemeldet(admin_ctx, bestand, service, stammdaten_repo):
    service.erfassen(
        ctx=admin_ctx, einheit_id="601-KURZ1", art="KURZZEITVERMIETUNG", leistungsmonat="2026-08",
        belegdatum=date(2026, 9, 5), quelle_referenz="Report roh", status="ENTWURF", erstellt_von="markus",
    )
    uebersicht = berechne_monatsuebersicht(
        leistungsmonat="2026-08", stammdaten_repository=stammdaten_repo, variable_service=service
    )
    assert uebersicht.kurzzeit_netto_anteil_cent == 0
    assert uebersicht.vollstaendig is False
    assert any("ENTWURF" in g for g in uebersicht.datenluecken)


def test_fehlender_bericht_wird_als_datenluecke_gemeldet(admin_ctx, bestand, service, stammdaten_repo):
    uebersicht = berechne_monatsuebersicht(
        leistungsmonat="2026-08", stammdaten_repository=stammdaten_repo, variable_service=service
    )
    assert any("601-KURZ1" in g and "kein" in g for g in uebersicht.datenluecken)
    assert any("601-STOR1" in g and "kein" in g for g in uebersicht.datenluecken)


def test_doppelzaehlung_dauermiete_und_report_wird_vermieden(admin_ctx, bestand, service, stammdaten_repo):
    """Dieselbe Einheit hat sowohl einen aktiven Dauervermietungs-Vertrag
    ALS AUCH (inkonsistenterweise) einen KURZZEITVERMIETUNG-Report für
    denselben Monat - der Report darf NICHT zusätzlich aufsummiert
    werden."""

    service.erfassen(
        ctx=admin_ctx, einheit_id="601-TOP3", art="KURZZEITVERMIETUNG", leistungsmonat="2026-08",
        belegdatum=date(2026, 9, 5), quelle_referenz="Fehlerhafter Report", status="BESTAETIGT",
        unser_netto_anteil_cent=99_999, erstellt_von="markus",
    )
    uebersicht = berechne_monatsuebersicht(
        leistungsmonat="2026-08", stammdaten_repository=stammdaten_repo, variable_service=service
    )
    assert uebersicht.kurzzeit_netto_anteil_cent == 0
    assert uebersicht.nettomieterloes_cent == 100_000
    assert any("Doppelzählung" in g for g in uebersicht.datenluecken)


def test_vertrag_ausserhalb_des_gewaehlten_monats_zaehlt_nicht(admin_ctx, bestand, service, stammdaten_repo):
    stammdaten_repo.upsert_vertrag(
        id="V-601-3", einheit_id="601-TOP3", debitor_id="DEB-1", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1), gueltig_bis=date(2025, 12, 31),
    )
    uebersicht = berechne_monatsuebersicht(
        leistungsmonat="2026-08", stammdaten_repository=stammdaten_repo, variable_service=service
    )
    assert uebersicht.dauermiete_soll_netto_cent == 0


def test_gesellschaftsscope_filter(admin_ctx, bestand, service, stammdaten_repo):
    stammdaten_repo.upsert_gesellschaft(id="ANDERE", name="Andere GmbH")
    uebersicht = berechne_monatsuebersicht(
        leistungsmonat="2026-08", stammdaten_repository=stammdaten_repo, variable_service=service,
        gesellschaft_id="ANDERE",
    )
    assert uebersicht.dauermiete_soll_netto_cent == 0
    assert uebersicht.datenluecken == ()
