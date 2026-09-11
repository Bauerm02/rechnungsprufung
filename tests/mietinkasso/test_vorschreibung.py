from __future__ import annotations

from datetime import date

import pytest

from mietinkasso.domain.exceptions import BindungInkonsistentError, NachweisFehltError
from mietinkasso.op.repository import OPRepository
from mietinkasso.op.service import OPService
from mietinkasso.vorschreibung.repository import VorschreibungRepository
from mietinkasso.vorschreibung.service import VorschreibungService, faelligkeitsdatum


@pytest.fixture
def vorschreibung_service(session_factory, stammdaten_repo) -> VorschreibungService:
    op_service = OPService(OPRepository(session_factory), stammdaten_repo)
    return VorschreibungService(VorschreibungRepository(session_factory), stammdaten_repo, op_service)


@pytest.fixture
def vertrag_mit_komponenten(stammdaten_repo, basis_vertrag):
    vertrag, konto = basis_vertrag
    stammdaten_repo.add_komponente(
        id="K-HMZ", vertrag_id=vertrag.id, art="HMZ", bezeichnung="Hauptmietzins", betrag_cent=50_000,
        ust_satz_promille=10000, gueltig_von=date(2024, 1, 1),
    )
    stammdaten_repo.add_komponente(
        id="K-KUECHE", vertrag_id=vertrag.id, art="KUECHE", bezeichnung="Küche", betrag_cent=5_000,
        ust_satz_promille=10000, gueltig_von=date(2024, 1, 1),
    )
    stammdaten_repo.add_komponente(
        id="K-PARKPLATZ", vertrag_id=vertrag.id, art="PARKPLATZ", bezeichnung="Parkplatz", betrag_cent=8_000,
        ust_satz_promille=20000, gueltig_von=date(2024, 1, 1),
    )
    stammdaten_repo.add_komponente(
        id="K-BKVZ", vertrag_id=vertrag.id, art="BK_VORAUSZAHLUNG", bezeichnung="BK-Vorauszahlung",
        betrag_cent=12_000, ust_satz_promille=10000, gueltig_von=date(2024, 1, 1),
    )
    return vertrag, konto


def test_kueche_und_parkplatz_sind_in_vorschreibung_enthalten(vorschreibung_service, vertrag_mit_komponenten, ctx_factory):
    vertrag, _ = vertrag_mit_komponenten
    ctx = ctx_factory("7DI")
    ergebnis = vorschreibung_service.entwurf_erstellen(ctx=ctx, vertrag=vertrag, monat="2026-04")
    positionen = vorschreibung_service._repository.list_positionen(ergebnis.vorschreibung_id)
    arten = {p.art for p in positionen}
    assert "KUECHE" in arten
    assert "PARKPLATZ" in arten
    assert ergebnis.summe_cent == 50_000 + 5_000 + 8_000 + 12_000


def test_faelligkeit_stammt_aus_vertrag(vertrag_mit_komponenten):
    vertrag, _ = vertrag_mit_komponenten
    vertrag.faelligkeit_tag = 3
    assert faelligkeitsdatum("2026-02", vertrag.faelligkeit_tag) == date(2026, 2, 3)
    # Robust gegen Kurzmonate: Tag 31 im Februar -> letzter Tag des Monats.
    assert faelligkeitsdatum("2026-02", 31) == date(2026, 2, 28)


def test_pro_vertrag_monat_genau_eine_vorschreibung_auch_bei_zwei_workern(
    vorschreibung_service, vertrag_mit_komponenten, ctx_factory
):
    vertrag, konto = vertrag_mit_komponenten
    ctx = ctx_factory("7DI")

    ergebnis_worker_a = vorschreibung_service.entwurf_erstellen(ctx=ctx, vertrag=vertrag, monat="2026-05")
    ergebnis_worker_b = vorschreibung_service.entwurf_erstellen(ctx=ctx, vertrag=vertrag, monat="2026-05")
    assert ergebnis_worker_a.vorschreibung_id == ergebnis_worker_b.vorschreibung_id

    gestellt_a = vorschreibung_service.sollstellen(ctx=ctx, vertrag=vertrag, konto=konto, monat="2026-05")
    gestellt_b = vorschreibung_service.sollstellen(ctx=ctx, vertrag=vertrag, konto=konto, monat="2026-05")
    assert gestellt_a.status == gestellt_b.status == "SOLLGESTELLT"

    saldo = vorschreibung_service._op_service.berechne_saldo(konto.id)
    soll_zeilen = [p for p in saldo.positionen if p.leistungsperiode == "2026-05"]
    assert len(soll_zeilen) == 1  # keine doppelte Sollstellung trotz zweifachem Aufruf
    assert saldo.saldo_cent == 75_000


def test_sollstellen_lehnt_falsch_zugeordnetes_konto_ab(vorschreibung_service, stammdaten_repo, vertrag_mit_komponenten, ctx_factory):
    vertrag, _ = vertrag_mit_komponenten
    ctx = ctx_factory("7DI")
    stammdaten_repo.upsert_debitor(id="DEB-FREMD", name="Fremder Debitor")
    stammdaten_repo.upsert_vertrag(
        id="V-601-FREMD", einheit_id=vertrag.einheit_id, debitor_id="DEB-FREMD", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    fremdes_konto = stammdaten_repo.get_or_create_konto(vertrag=stammdaten_repo.get_vertrag("V-601-FREMD"))

    vorschreibung_service.entwurf_erstellen(ctx=ctx, vertrag=vertrag, monat="2026-06")
    with pytest.raises(BindungInkonsistentError):
        vorschreibung_service.sollstellen(ctx=ctx, vertrag=vertrag, konto=fremdes_konto, monat="2026-06")


def test_dokument_zustellung_und_export_verlangen_echten_nachweis_und_reihenfolge(
    vorschreibung_service, vertrag_mit_komponenten, ctx_factory
):
    """Regression (Abnahmesperre): keine erfundenen Erfolgsstatus ohne
    Nachweis, kein Export direkt aus ENTWURF/SOLLGESTELLT."""

    vertrag, konto = vertrag_mit_komponenten
    ctx = ctx_factory("7DI")
    ergebnis = vorschreibung_service.entwurf_erstellen(ctx=ctx, vertrag=vertrag, monat="2026-07")
    vorschreibung_service.sollstellen(ctx=ctx, vertrag=vertrag, konto=konto, monat="2026-07", heute=date(2026, 7, 1))

    with pytest.raises(NachweisFehltError):
        vorschreibung_service.dokument_zustellen(ctx=ctx, vorschreibung_id=ergebnis.vorschreibung_id, zustellnachweis={})

    with pytest.raises(ValueError):
        vorschreibung_service.hauptbuch_exportieren(
            ctx=ctx, vorschreibung_id=ergebnis.vorschreibung_id, export_nachweis={"zielsystem": "test"}
        )

    vorschreibung_service.dokument_zustellen(
        ctx=ctx, vorschreibung_id=ergebnis.vorschreibung_id,
        zustellnachweis={"kanal": "email", "provider_referenz": "MSG-123"},
    )
    vorschreibung_service.hauptbuch_exportieren(
        ctx=ctx, vorschreibung_id=ergebnis.vorschreibung_id, export_nachweis={"zielsystem": "test", "export_referenz": "EXP-1"}
    )
    exportierte = vorschreibung_service._get_or_raise(ergebnis.vorschreibung_id)
    assert exportierte.status == "EXPORTIERT"
    assert exportierte.export_nachweis == {"zielsystem": "test", "export_referenz": "EXP-1"}
