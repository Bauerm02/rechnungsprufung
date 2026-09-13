from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest

from mietinkasso.index.repository import IndexRepository
from mietinkasso.index.service import IndexService
from mietinkasso.indexautomatik.outbox_service import ErhoehungsschreibenOutboxService
from mietinkasso.indexautomatik.rechtsprofil import RechtsprofilService
from mietinkasso.indexautomatik.repository import (
    ErhoehungsschreibenRepository,
    IndexautomatikLaufRepository,
    RechtsprofilRepository,
    VpiRepository,
)
from mietinkasso.indexautomatik.service import IndexautomatikService
from mietinkasso.infrastructure.db.tables import IndexKlauselTable
from mietinkasso.mieweg_vorschau.repository import MieWegVorschauRepository
from mietinkasso.mieweg_vorschau.service import MieWegVorschauService


@dataclass
class Bundle:
    rechtsprofil_service: RechtsprofilService
    index_service: IndexautomatikService
    outbox_repo: ErhoehungsschreibenRepository
    lauf_repo: IndexautomatikLaufRepository
    vpi_repo: VpiRepository
    index_repo: IndexRepository
    rechtsprofil_repo: RechtsprofilRepository


@pytest.fixture
def bundle(session_factory, stammdaten_repo) -> Bundle:
    rechtsprofil_repo = RechtsprofilRepository(session_factory)
    index_repo = IndexRepository(session_factory)
    lauf_repo = IndexautomatikLaufRepository(session_factory)
    outbox_repo = ErhoehungsschreibenRepository(session_factory)
    vpi_repo = VpiRepository(session_factory)
    mieweg_repo = MieWegVorschauRepository(session_factory)

    rechtsprofil_service = RechtsprofilService(rechtsprofil_repo, stammdaten_repo, index_repo)
    mieweg_service = MieWegVorschauService(mieweg_repo, stammdaten_repo)
    index_service = IndexService(index_repo, stammdaten_repo)
    outbox_service = ErhoehungsschreibenOutboxService(outbox_repo, stammdaten_repo, jlb_signatur="JLB Projects GmbH")

    automatik = IndexautomatikService(
        stammdaten_repository=stammdaten_repo,
        rechtsprofil_service=rechtsprofil_service,
        lauf_repository=lauf_repo,
        outbox_repository=outbox_repo,
        vpi_repository=vpi_repo,
        mieweg_service=mieweg_service,
        index_repository=index_repo,
        index_service=index_service,
        outbox_service=outbox_service,
    )
    return Bundle(rechtsprofil_service, automatik, outbox_repo, lauf_repo, vpi_repo, index_repo, rechtsprofil_repo)


def _seed_vpi(vpi_repo, *, reihe="VPI20C18", jahre_werte: dict[int, str]):
    for jahr, wert in jahre_werte.items():
        vpi_repo.jahreswert_erfassen(
            reihe=reihe, jahr=jahr, wert=Decimal(wert), quelle="Statistik Austria (synthetisch)",
            quelle_datum=date(jahr + 1, 2, 17), erfasst_von="markus",
        )


def _mit_komponente(stammdaten_repo, vertrag, *, id="K-1", betrag_cent=100_000):
    stammdaten_repo.add_komponente(
        id=id, vertrag_id=vertrag.id, art="HMZ", bezeichnung="Hauptmietzins", betrag_cent=betrag_cent,
        indexierbar=True, gueltig_von=date(2024, 1, 1),
    )


def _freigegebenes_wohnungsprofil(admin_ctx, rechtsprofil_service, vertrag, **overrides) -> object:
    basis = dict(
        ctx=admin_ctx, vertrag_id=vertrag.id, rechtsordnung="OESTERREICH_MRG_VOLL", ist_wohnungsnutzung=True,
        mrg_zinsbeschraenkung=False, ist_altvertrag=False, ist_hauptmiete=True, foerderbindung=False,
        mietzinsobergrenze_cent=None, mietzinsobergrenze_quellenbeleg=None, mietzinsobergrenze_gueltig_bis=None,
        bezugsjahr=2024, bezugsmonat=1, letzte_basis_war_jahresdurchschnitt=False, basis_komponenten_ids=["K-1"],
        vertraglich_zulaessiger_betrag_cent=200_000, vertraglicher_quellenbeleg="Mietvertrag Punkt 5",
        vertraglicher_fruehestmoeglicher_termin=date(2026, 4, 1), vertrag_beleg_referenz="Mietvertrag V-601-3",
        klausel_referenz="Punkt 5 Wertsicherung", erstellt_von="markus",
    )
    basis.update(overrides)
    profil = rechtsprofil_service.entwurf_anlegen(**basis)
    return rechtsprofil_service.freigeben(profil.id, ctx=admin_ctx, freigegeben_von="markus")


def test_januarvertrag_ausserhalb_januar_wird_korrekt_geprueft(admin_ctx, basis_vertrag, bundle, stammdaten_repo):
    """"Januarvertrag außerhalb Januar": der Monatslauf darf zu JEDEM
    Kalendermonat laufen (nicht nur im April/Januar) - nur das
    ERGEBNIS hängt vom April-Termin ab, nicht der Ausführungsmonat."""

    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag)
    _freigegebenes_wohnungsprofil(admin_ctx, bundle.rechtsprofil_service, vertrag)
    _seed_vpi(bundle.vpi_repo, jahre_werte={2023: "100", 2024: "102", 2025: "104"})

    lauf = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 9, 13), akteur="test")
    assert lauf.status == "ERHOEHUNG_ERZEUGT"
    schreiben = bundle.outbox_repo.get(lauf.erhoehungsschreiben_id)
    assert schreiben.ziel_bewertungsjahr == 2026
    assert schreiben.erhoehung_cent > 0


def test_termin_nicht_erreicht_vor_erstem_moeglichen_april(admin_ctx, basis_vertrag, bundle, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag)
    _freigegebenes_wohnungsprofil(admin_ctx, bundle.rechtsprofil_service, vertrag, bezugsjahr=2026, bezugsmonat=1)

    lauf = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 6, 1), akteur="test")
    assert lauf.status == "TERMIN_NICHT_ERREICHT"


def test_fehlende_vpi_publikation_blockiert_ohne_erfundenen_wert(admin_ctx, basis_vertrag, bundle, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag)
    _freigegebenes_wohnungsprofil(admin_ctx, bundle.rechtsprofil_service, vertrag)
    # bewusst KEINE VPI-Werte erfasst

    lauf = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 9, 13), akteur="test")
    assert lauf.status == "BLOCKIERT"
    assert any("VPI" in grund for grund in lauf.blockiert_gruende)


def test_monatslauf_ist_idempotent_je_periode(admin_ctx, basis_vertrag, bundle, stammdaten_repo):
    """Doppel-/Parallelstart: zwei Aufrufe für dieselbe Periode liefern
    denselben Lauf, kein zweites Erhöhungsschreiben."""

    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag)
    _freigegebenes_wohnungsprofil(admin_ctx, bundle.rechtsprofil_service, vertrag)
    _seed_vpi(bundle.vpi_repo, jahre_werte={2023: "100", 2024: "102", 2025: "104"})

    erster = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 9, 13), akteur="test")
    zweiter = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 9, 20), akteur="test")

    assert erster.id == zweiter.id
    assert len(bundle.outbox_repo.liste_fuer_vertrag(vertrag.id)) == 1


def test_bereits_erfasstes_ziel_bewertungsjahr_erzeugt_kein_zweites_schreiben(admin_ctx, basis_vertrag, bundle, stammdaten_repo):
    """Anderer Kalendermonat, aber dasselbe Ziel-Bewertungsjahr - z. B.
    ein manuell gelöschter Lauf-Eintrag darf trotzdem kein zweites
    Erhöhungsschreiben für dasselbe Jahr erzeugen (Outbox-Unique bleibt
    die tatsächliche Garantie)."""

    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag)
    _freigegebenes_wohnungsprofil(admin_ctx, bundle.rechtsprofil_service, vertrag)
    _seed_vpi(bundle.vpi_repo, jahre_werte={2023: "100", 2024: "102", 2025: "104"})

    bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 9, 13), akteur="test")
    lauf_oktober = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 10, 13), akteur="test")

    assert lauf_oktober.status == "BEREITS_ERFASST"
    assert len(bundle.outbox_repo.liste_fuer_vertrag(vertrag.id)) == 1


def test_kein_rechtsprofil_blockiert_ohne_rechtsannahme(admin_ctx, basis_vertrag, bundle, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    lauf = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 9, 13), akteur="test")
    assert lauf.status == "BLOCKIERT"


def test_untermiete_ungeklaert_blockiert(admin_ctx, basis_vertrag, bundle, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag)
    _freigegebenes_wohnungsprofil(admin_ctx, bundle.rechtsprofil_service, vertrag, ist_hauptmiete=False)
    lauf = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 9, 13), akteur="test")
    assert lauf.status == "BLOCKIERT"
    assert any("Untermiete" in g or "Hauptmiete" in g for g in lauf.blockiert_gruende)


def test_geschaeftsraum_ohne_klausel_blockiert_kein_pauschales_mieweg(admin_ctx, basis_vertrag, bundle, stammdaten_repo):
    """"Geschäft unter MRG, unbekanntes Profil": MRG_TEIL OHNE bestätigte
    Wohnungsnutzung UND ohne Vertragsklausel darf keine automatische
    Berechnung auslösen."""

    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag)
    _freigegebenes_wohnungsprofil(
        admin_ctx, bundle.rechtsprofil_service, vertrag, rechtsordnung="OESTERREICH_MRG_TEIL", ist_wohnungsnutzung=False,
    )
    lauf = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 9, 13), akteur="test")
    assert lauf.status == "BLOCKIERT"
    assert any("Geschäftsraum" in g or "Vertragsklausel" in g for g in lauf.blockiert_gruende)


def test_geschaeftsraum_mit_klausel_berechnet_ueber_index_service(admin_ctx, basis_vertrag, bundle, stammdaten_repo):
    """Geschäftsraum mit einer geprüften, freigegebenen IndexKlausel
    wird über index/service.py berechnet, NICHT über MieWeG."""

    vertrag, _konto = basis_vertrag
    _mit_komponente(stammdaten_repo, vertrag, betrag_cent=200_000)
    klausel = bundle.index_repo.anlegen(
        IndexKlauselTable(
            vertrag_id=vertrag.id, version=1, rechtsordnung="OESTERREICH_MRG_TEIL",
            berechnungsprofil="EINFACHER_SCHWELLENVERGLEICH", abschlussdatum=date(2024, 1, 1),
            basis_reihe="VPI20C18", basis_wert=Decimal("100"), basis_monat="2024-01",
            schwelle_prozent=Decimal("0"), indexierbare_komponenten=["HMZ"],
        )
    )
    bundle.index_repo.freigeben(klausel.id, freigegeben_von="markus")

    _freigegebenes_wohnungsprofil(
        admin_ctx, bundle.rechtsprofil_service, vertrag, rechtsordnung="OESTERREICH_MRG_TEIL",
        ist_wohnungsnutzung=False, vertraglich_zulaessiger_betrag_cent=None, vertraglicher_quellenbeleg=None,
        vertragsklausel_id=klausel.id,
    )
    bundle.vpi_repo.monatswert_erfassen(
        reihe="VPI20C18", jahr=2026, monat=8, wert=Decimal("105"), finalitaet="ENDGUELTIG",
        quelle_datei="synthetisch", quelle_zeile=1, quelle_hash="deadbeef",
        abgerufen_am=__import__("datetime").datetime(2026, 9, 1, tzinfo=__import__("datetime").timezone.utc),
        importiert_von="markus",
    )

    lauf = bundle.index_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 9, 13), akteur="test")
    assert lauf.status == "ERHOEHUNG_ERZEUGT"
    schreiben = bundle.outbox_repo.get(lauf.erhoehungsschreiben_id)
    assert schreiben.ziel_bewertungsjahr is None
    assert schreiben.index_anpassung_id is not None
    assert schreiben.erhoehung_cent > 0


def test_abgelaufener_vertrag_wird_im_batch_uebersprungen(admin_ctx, basis_vertrag, bundle, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    stammdaten_repo.upsert_vertrag(
        id=vertrag.id, einheit_id=vertrag.einheit_id, debitor_id=vertrag.debitor_id,
        gesellschaft_id=vertrag.gesellschaft_id, rechtsordnung=vertrag.rechtsordnung, gueltig_von=vertrag.gueltig_von,
        gueltig_bis=date(2026, 1, 1),
    )
    laeufe = bundle.index_service.monatslauf_alle(ctx=admin_ctx, heute=date(2026, 9, 13), akteur="test")
    assert laeufe == []
