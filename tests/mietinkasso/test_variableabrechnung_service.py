from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from mietinkasso.domain.exceptions import ObjektAusgeschlossenError, OptimistischerLockKonfliktError, VariableAbrechnungKonfliktError
from mietinkasso.variableabrechnung.repository import VariableAbrechnungRepository
from mietinkasso.variableabrechnung.service import VariableAbrechnungService


@pytest.fixture
def repo(session_factory) -> VariableAbrechnungRepository:
    return VariableAbrechnungRepository(session_factory)


@pytest.fixture
def service(repo, stammdaten_repo) -> VariableAbrechnungService:
    return VariableAbrechnungService(repo, stammdaten_repo)


@pytest.fixture
def kurzzeit_einheit(stammdaten_repo):
    """Objekt/Einheit ohne Vertrag - Kurzzeitvermietung hat definitionsgemäß
    keine Dauervermietungs-Vorschreibung."""

    stammdaten_repo.upsert_gesellschaft(id="7DI", name="7D Immobilien GmbH")
    stammdaten_repo.upsert_objekt(id="601", gesellschaft_id="7DI", bezeichnung="Am Corso")
    stammdaten_repo.upsert_einheit(id="601-KURZ1", objekt_id="601", bezeichnung="Kurzzeit 1", nutzungsstatus="KURZZEITVERMIETUNG")
    return "601-KURZ1"


def _basis_kwargs(**overrides) -> dict:
    basis = dict(
        einheit_id="601-KURZ1", art="KURZZEITVERMIETUNG", leistungsmonat="2026-08", belegdatum=date(2026, 9, 5),
        quelle_referenz="Betreiberreport August 2026", status="ENTWURF", erstellt_von="markus",
    )
    basis.update(overrides)
    return basis


def test_erfassen_erste_version(admin_ctx, kurzzeit_einheit, service, repo):
    zeile = service.erfassen(ctx=admin_ctx, **_basis_kwargs())
    assert zeile.version == 1
    assert zeile.ist_aktuell is True
    assert zeile.status == "ENTWURF"
    assert repo.aktuelle_version("601-KURZ1", "KURZZEITVERMIETUNG", "2026-08").id == zeile.id


def test_erfassen_identisch_ist_idempotent(admin_ctx, kurzzeit_einheit, service, repo):
    erste = service.erfassen(ctx=admin_ctx, **_basis_kwargs())
    zweite = service.erfassen(ctx=admin_ctx, **_basis_kwargs())
    assert erste.id == zweite.id
    assert len(repo.liste_versionen("601-KURZ1", "KURZZEITVERMIETUNG", "2026-08")) == 1


def test_erfassen_mit_abweichendem_inhalt_wird_abgelehnt(admin_ctx, kurzzeit_einheit, service):
    service.erfassen(ctx=admin_ctx, **_basis_kwargs())
    with pytest.raises(VariableAbrechnungKonfliktError):
        service.erfassen(ctx=admin_ctx, **_basis_kwargs(quelle_referenz="Anderer Report"))


def test_korrigieren_legt_neue_version_an(admin_ctx, kurzzeit_einheit, service, repo):
    erste = service.erfassen(ctx=admin_ctx, **_basis_kwargs())
    korrigiert = service.korrigieren(
        ctx=admin_ctx, ausgehend_von_id=erste.id, aenderungsgrund="Nettoanteil bestätigt lt. Abrechnung",
        belegdatum=date(2026, 9, 10), quelle_referenz="Betreiberabrechnung final",
        status="BESTAETIGT", unser_netto_anteil_cent=45_000, erstellt_von="markus",
    )
    assert korrigiert.version == 2
    assert korrigiert.ist_aktuell is True
    assert repo.get(erste.id).ist_aktuell is False
    assert repo.aktuelle_version("601-KURZ1", "KURZZEITVERMIETUNG", "2026-08").id == korrigiert.id
    assert len(repo.liste_versionen("601-KURZ1", "KURZZEITVERMIETUNG", "2026-08")) == 2


def test_korrigieren_mit_veralteter_ausgangsversion_wird_abgelehnt(admin_ctx, kurzzeit_einheit, service):
    """Optimistic lock: eine zwischenzeitlich bereits erfolgte Korrektur
    macht einen zweiten, auf der ALTEN Version basierenden Korrekturversuch
    ungültig."""

    erste = service.erfassen(ctx=admin_ctx, **_basis_kwargs())
    service.korrigieren(
        ctx=admin_ctx, ausgehend_von_id=erste.id, aenderungsgrund="Erste Korrektur",
        belegdatum=date(2026, 9, 10), quelle_referenz="Report v2", status="ENTWURF", erstellt_von="markus",
    )
    with pytest.raises(OptimistischerLockKonfliktError):
        service.korrigieren(
            ctx=admin_ctx, ausgehend_von_id=erste.id, aenderungsgrund="Zweite, konkurrierende Korrektur",
            belegdatum=date(2026, 9, 11), quelle_referenz="Report v3-konkurrierend", status="ENTWURF",
            erstellt_von="anderer-user",
        )


def test_korrigieren_ohne_aenderungsgrund_wird_abgelehnt(admin_ctx, kurzzeit_einheit, service):
    erste = service.erfassen(ctx=admin_ctx, **_basis_kwargs())
    with pytest.raises(ValueError):
        service.korrigieren(
            ctx=admin_ctx, ausgehend_von_id=erste.id, aenderungsgrund="", belegdatum=date(2026, 9, 10),
            quelle_referenz="Report v2", status="ENTWURF", erstellt_von="markus",
        )


def test_bestaetigt_ohne_netto_anteil_wird_abgelehnt(admin_ctx, kurzzeit_einheit, service):
    with pytest.raises(ValueError):
        service.erfassen(ctx=admin_ctx, **_basis_kwargs(status="BESTAETIGT", unser_netto_anteil_cent=None))


def test_betrag_ohne_betragsart_wird_abgelehnt(admin_ctx, kurzzeit_einheit, service):
    with pytest.raises(ValueError):
        service.erfassen(ctx=admin_ctx, **_basis_kwargs(berichteter_betrag_cent=100_000, berichteter_betragsart=None))


def test_negativer_betrag_wird_abgelehnt(admin_ctx, kurzzeit_einheit, service):
    with pytest.raises(ValueError):
        service.erfassen(ctx=admin_ctx, **_basis_kwargs(unser_netto_anteil_cent=-100))


def test_ungueltige_art_wird_abgelehnt(admin_ctx, kurzzeit_einheit, service):
    with pytest.raises(ValueError):
        service.erfassen(ctx=admin_ctx, **_basis_kwargs(art="DAUERVERMIETUNG"))


def test_ungueltiger_leistungsmonat_wird_abgelehnt(admin_ctx, kurzzeit_einheit, service):
    with pytest.raises(ValueError):
        service.erfassen(ctx=admin_ctx, **_basis_kwargs(leistungsmonat="2026-13"))


def test_fremde_gesellschaft_wird_abgelehnt(ctx_factory, kurzzeit_einheit, service):
    fremder_ctx = ctx_factory("ANDERE-GESELLSCHAFT")
    with pytest.raises(Exception):
        service.erfassen(ctx=fremder_ctx, **_basis_kwargs())


def test_ausgeschlossenes_objekt_sperrt(admin_ctx, kurzzeit_einheit, stammdaten_repo, service):
    stammdaten_repo.upsert_objekt(id="601", gesellschaft_id="7DI", bezeichnung="Am Corso", ausgeschlossen=True)
    with pytest.raises(ObjektAusgeschlossenError):
        service.erfassen(ctx=admin_ctx, **_basis_kwargs())


def test_import_id_idempotent_replay(admin_ctx, kurzzeit_einheit, service):
    erste = service.erfassen(ctx=admin_ctx, import_id="CSV:601-KURZ1:2026-08", **_basis_kwargs())
    zweite = service.erfassen(ctx=admin_ctx, import_id="CSV:601-KURZ1:2026-08", **_basis_kwargs())
    assert erste.id == zweite.id


def test_import_id_konflikt_bei_abweichendem_inhalt(admin_ctx, kurzzeit_einheit, service):
    service.erfassen(ctx=admin_ctx, import_id="CSV:601-KURZ1:2026-08", **_basis_kwargs())
    with pytest.raises(VariableAbrechnungKonfliktError):
        service.erfassen(
            ctx=admin_ctx, import_id="CSV:601-KURZ1:2026-08", **_basis_kwargs(quelle_referenz="Andere Quelle")
        )


def test_vermietete_flaeche_wird_gespeichert(admin_ctx, kurzzeit_einheit, service):
    zeile = service.erfassen(ctx=admin_ctx, **_basis_kwargs(vermietete_einheiten=3, vermietete_flaeche_qm=Decimal("45.5")))
    assert zeile.vermietete_einheiten == 3
    assert zeile.vermietete_flaeche_qm == Decimal("45.5")


def test_kostenfelder_sind_rein_informativ_und_werden_nicht_verrechnet(admin_ctx, kurzzeit_einheit, service):
    zeile = service.erfassen(
        ctx=admin_ctx,
        **_basis_kwargs(
            status="BESTAETIGT", unser_netto_anteil_cent=50_000, betriebskosten_hinweis_cent=5_000,
            reinigungskosten_hinweis_cent=2_000, verwaltungskosten_hinweis_cent=1_000,
            tatsaechlicher_zahlungseingang_cent=42_000,
        ),
    )
    # unser_netto_anteil_cent bleibt UNVERÄNDERT der maßgebliche Wert -
    # die Kostenfelder werden an keiner Stelle im Service davon abgezogen.
    assert zeile.unser_netto_anteil_cent == 50_000
    assert zeile.tatsaechlicher_zahlungseingang_cent == 42_000
