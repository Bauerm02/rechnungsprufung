"""Tests für `indexautomatik/umsetzung_service.py::IndexSollUmsetzungService`
(Auftrag HV-20260913-VERSAND-SOLL, Punkt 1) - der bisher fehlende
letzte Schritt der Indexautomatik-Pipeline (SOLL_UMSETZUNG_OFFEN ->
tatsächliche Komponenten-/Rechtsprofiländerung).

Deckt: erfolgreiche Umsetzung (Komponentenhistorisierung + neue
Rechtsprofilversion), Wiederholung/Replay (kein Doppelergebnis),
Stale-Snapshot (Komponente bzw. Rechtsprofilquelle seit Entwurf
geändert), falscher Mandant (Gesellschaftsscope), unveränderte
BK-Komponente bleibt unangetastet, Zugangs-/Zeitpunktprüfung (noch
nicht wirksam), bereits gebuchte Folgeperiode (keine Doppelbuchung) und
Transaktionsatomarität bei einem Fehler mitten in der Umsetzung."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select, update as sa_update

from decimal import Decimal

from mietinkasso.domain.exceptions import CrossTenantError
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
from mietinkasso.indexautomatik.transport import FakeTransportadapter
from mietinkasso.indexautomatik.umsetzung_service import IndexSollUmsetzungService
from mietinkasso.infrastructure.db.tables import ErhoehungsschreibenTable, IndexSollUmsetzungTable, RechtsprofilTable
from mietinkasso.mieweg_vorschau.repository import MieWegVorschauRepository
from mietinkasso.mieweg_vorschau.service import MieWegVorschauService
from mietinkasso.vorschreibung.repository import VorschreibungRepository
from mietinkasso.vorschreibung.service import VorschreibungService


@pytest.fixture
def outbox_repo(session_factory) -> ErhoehungsschreibenRepository:
    return ErhoehungsschreibenRepository(session_factory)


@pytest.fixture
def rechtsprofil_repo(session_factory) -> RechtsprofilRepository:
    return RechtsprofilRepository(session_factory)


@pytest.fixture
def index_repo(session_factory) -> IndexRepository:
    return IndexRepository(session_factory)


@pytest.fixture
def rechtsprofil_service(rechtsprofil_repo, stammdaten_repo, index_repo) -> RechtsprofilService:
    return RechtsprofilService(rechtsprofil_repo, stammdaten_repo, index_repo)


@pytest.fixture
def umsetzung_service(session_factory, stammdaten_repo, rechtsprofil_service, outbox_repo) -> IndexSollUmsetzungService:
    return IndexSollUmsetzungService(
        session_factory=session_factory,
        stammdaten_repository=stammdaten_repo,
        rechtsprofil_service=rechtsprofil_service,
        erhoehungsschreiben_repository=outbox_repo,
    )


@pytest.fixture
def outbox_service(outbox_repo, stammdaten_repo, rechtsprofil_repo, rechtsprofil_service) -> ErhoehungsschreibenOutboxService:
    return ErhoehungsschreibenOutboxService(
        outbox_repo, stammdaten_repo, rechtsprofil_repo, rechtsprofil_service, jlb_signatur="JLB Projects GmbH"
    )


@pytest.fixture
def vorschreibung_service(session_factory, stammdaten_repo, op_service) -> VorschreibungService:
    return VorschreibungService(VorschreibungRepository(session_factory), stammdaten_repo, op_service)


@pytest.fixture
def vpi_repo(session_factory) -> VpiRepository:
    return VpiRepository(session_factory)


@pytest.fixture
def indexautomatik_service(
    session_factory, stammdaten_repo, rechtsprofil_service, outbox_repo, vpi_repo, index_repo, outbox_service,
) -> IndexautomatikService:
    lauf_repo = IndexautomatikLaufRepository(session_factory)
    mieweg_service = MieWegVorschauService(MieWegVorschauRepository(session_factory), stammdaten_repo)
    index_service = IndexService(index_repo, stammdaten_repo)
    return IndexautomatikService(
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


def _seed_vpi(vpi_repo, *, jahre_werte: dict[int, str], reihe="VPI20C18"):
    for jahr, wert in jahre_werte.items():
        vpi_repo.jahreswert_erfassen(
            reihe=reihe, jahr=jahr, wert=Decimal(wert), quelle="Statistik Austria (synthetisch)",
            quelle_datum=date(jahr + 1, 2, 17), erfasst_von="markus",
        )


def _profil_und_komponente(
    admin_ctx, rechtsprofil_service, stammdaten_repo, vertrag, *,
    komponente_id="K-1", komponente_gueltig_bis=None, bezugsjahr=2024, bezugsmonat=1,
    letzte_basis_war_jahresdurchschnitt=False,
):
    stammdaten_repo.add_komponente(
        id=komponente_id, vertrag_id=vertrag.id, art="HMZ", bezeichnung="Hauptmietzins", betrag_cent=100_000,
        indexierbar=True, gueltig_von=date(2024, 1, 1), gueltig_bis=komponente_gueltig_bis,
    )
    entwurf = rechtsprofil_service.entwurf_anlegen(
        ctx=admin_ctx, vertrag_id=vertrag.id, rechtsordnung="OESTERREICH_MRG_VOLL", ist_wohnungsnutzung=True,
        mrg_zinsbeschraenkung=False, mrg_zinsbeschraenkung_geprueft=True, ist_altvertrag=False, ist_hauptmiete=True, foerderbindung=False, foerderbindung_geprueft=True,
        mietzinsobergrenze_cent=None, mietzinsobergrenze_quellenbeleg=None, mietzinsobergrenze_gueltig_bis=None,
        bezugsjahr=bezugsjahr, bezugsmonat=bezugsmonat, letzte_basis_war_jahresdurchschnitt=letzte_basis_war_jahresdurchschnitt,
        basis_komponenten_ids=[komponente_id],
        vertraglich_zulaessiger_betrag_cent=200_000, vertraglicher_quellenbeleg="Punkt 5",
        vertraglicher_fruehestmoeglicher_termin=date(2026, 4, 1), vertrag_beleg_referenz="Vertrag", klausel_referenz=None,
        erstellt_von="markus",
    )
    return rechtsprofil_service.freigeben(entwurf.id, ctx=admin_ctx, freigegeben_von="markus")


def _soll_umsetzung_offenes_schreiben(
    outbox_repo,
    vertrag,
    profil,
    *,
    komponente_id="K-1",
    alter_betrag_cent=100_000,
    erhoehung_cent=1_000,
    ziel_bewertungsjahr=2026,
    zahlungspflicht_ab=date(2026, 4, 15),
    zugang_bestaetigt_am=date(2026, 4, 1),
    versendet_am=datetime(2026, 3, 15, 9, 0, tzinfo=timezone.utc),
    externe_versandreferenz="MAILOPS-TEST-1",
) -> ErhoehungsschreibenTable:
    """Direkt konstruierte Zeile für gezielte Unit-Tests EINZELNER
    Validierungszweige (Stale-Snapshot, falscher Mandant, ...) - der
    tatsächliche Versand-/Zugangs-Ablauf über `outbox_service` selbst
    wird separat, End-to-End, in
    `test_umsetzen_end_to_end_ueber_echten_versand_und_zugang` geprüft
    (Codex-Rückprüfung 499c36f: "tatsächlichen Versand + qualifizierten
    Zugang vollständig prüfen"). `versendet_am`/`externe_versandreferenz`
    sind hier bewusst PFLICHTPARAMETER-artig mit realistischen Default-
    werten belegt, nicht mehr stillschweigend leer wie zuvor."""

    return outbox_repo.anlegen(
        ErhoehungsschreibenTable(
            vertrag_id=vertrag.id,
            ziel_bewertungsjahr=ziel_bewertungsjahr,
            rechtsprofil_id=profil.id,
            rechtsprofil_version=profil.version,
            status="SOLL_UMSETZUNG_OFFEN",
            massgeblicher_termin=date(ziel_bewertungsjahr, 4, 1),
            erhoehung_cent=erhoehung_cent,
            schreiben_text="Testschreiben",
            idempotenzschluessel=f"{vertrag.id}:mieweg:{ziel_bewertungsjahr}",
            versendet_am=versendet_am,
            externe_versandreferenz=externe_versandreferenz,
            zugangsform="EINSCHREIBEN",
            zugang_bestaetigt_am=zugang_bestaetigt_am,
            zugang_beleg="RSb-1",
            zahlungspflicht_ab=zahlungspflicht_ab,
            empfaenger_snapshot={"debitor_id": vertrag.debitor_id},
            komponenten_verteilung={
                "eintraege": [
                    {
                        "komponente_id": komponente_id,
                        "alter_betrag_cent": alter_betrag_cent,
                        "neuer_betrag_cent": alter_betrag_cent + erhoehung_cent,
                    }
                ]
            },
        )
    )


def test_umsetzen_erfolgreich_historisiert_komponente_und_neues_rechtsprofil(
    admin_ctx, basis_vertrag, umsetzung_service, outbox_repo, rechtsprofil_service, rechtsprofil_repo, stammdaten_repo,
    session_factory,
):
    vertrag, _konto = basis_vertrag
    profil = _profil_und_komponente(admin_ctx, rechtsprofil_service, stammdaten_repo, vertrag)
    schreiben = _soll_umsetzung_offenes_schreiben(outbox_repo, vertrag, profil)

    ergebnis = umsetzung_service.umsetzen(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 4, 20), akteur="markus",
        soll_umsetzung_enabled=True,
    )

    assert ergebnis.status == "UMGESETZT"
    assert ergebnis.neue_komponente_id is not None
    assert ergebnis.neues_rechtsprofil_id is not None

    aktualisiertes_schreiben = outbox_repo.get(schreiben.id)
    assert aktualisiertes_schreiben.status == "SOLL_UMGESETZT"

    alte_komponente = stammdaten_repo.get_komponente("K-1")
    # Anspruchsmonat = April (zahlungspflicht_ab=15.4.) - die technische
    # Komponentenwirksamkeit liegt bewusst auf dem Monatsersten (siehe
    # `_anspruchsmonat_start`), NICHT auf dem taggenauen 15.4.
    assert alte_komponente.gueltig_bis == date(2026, 3, 31)
    assert alte_komponente.betrag_cent == 100_000  # NIE in-place geändert

    neue_komponente = stammdaten_repo.get_komponente(ergebnis.neue_komponente_id)
    assert neue_komponente is not None
    assert neue_komponente.betrag_cent == 101_000
    assert neue_komponente.gueltig_von == date(2026, 4, 1)
    assert neue_komponente.gueltig_bis is None
    assert neue_komponente.art == "HMZ"

    neues_profil = rechtsprofil_repo.get(ergebnis.neues_rechtsprofil_id)
    assert neues_profil.status == "FREIGEGEBEN"
    assert neues_profil.basis_komponenten_ids == [ergebnis.neue_komponente_id]
    assert neues_profil.version == profil.version + 1
    assert neues_profil.quelle_hash

    altes_profil = rechtsprofil_repo.get(profil.id)
    assert altes_profil.status == "INVALIDIERT"

    # Das neue Profil muss in EINEM SPÄTEREN, ganz normalen Aufruf
    # weiter als gültig erkannt werden (kein durch die In-Transaktions-
    # Hashberechnung verursachter dauerhafter Stale-Zustand).
    assert rechtsprofil_service.ist_noch_gueltig(neues_profil, heute=date(2026, 5, 1)) is True

    with session_factory() as session:
        zeile = session.execute(
            select(IndexSollUmsetzungTable).where(IndexSollUmsetzungTable.erhoehungsschreiben_id == schreiben.id)
        ).scalar_one()
        assert zeile.status == "UMGESETZT"
        assert zeile.neue_komponente_id == ergebnis.neue_komponente_id
        assert zeile.beendete_komponente_id == "K-1"
        assert zeile.wirksam_ab == date(2026, 4, 1)
        assert zeile.neue_komponenten_ids == [ergebnis.neue_komponente_id]
        assert zeile.beendete_komponenten_ids == ["K-1"]
    assert ergebnis.neue_komponenten_ids == [ergebnis.neue_komponente_id]


def test_umsetzen_mehrkomponenten_historisiert_alle_betroffenen_komponenten(
    admin_ctx, basis_vertrag, umsetzung_service, outbox_repo, rechtsprofil_service, stammdaten_repo, session_factory,
):
    """Codex-Rückprüfung (499c36f/8f499c9): "Mehrkomponenten bleibt komplett
    gesperrt, obwohl HMZ+Küche beauftragt - explizite centgenaue Verteilung
    implementieren". Zwei referenzierte Komponenten (HMZ 100.000 Cent,
    Küche 10.000 Cent), Gesamterhöhung 1.000 Cent, centgenau verteilt
    (909/91 Cent, größte-Rest-Verfahren) - `umsetzen()` muss BEIDE
    Komponenten in derselben Transaktion historisieren, beide neuen Zeilen
    referenzieren, und das neue Rechtsprofil auf BEIDE neuen IDs zeigen."""

    vertrag, _konto = basis_vertrag
    stammdaten_repo.add_komponente(
        id="K-1", vertrag_id=vertrag.id, art="HMZ", bezeichnung="Hauptmietzins", betrag_cent=100_000,
        indexierbar=True, gueltig_von=date(2024, 1, 1),
    )
    stammdaten_repo.add_komponente(
        id="K-2", vertrag_id=vertrag.id, art="KUECHE", bezeichnung="Küche", betrag_cent=10_000,
        indexierbar=True, gueltig_von=date(2024, 1, 1),
    )
    entwurf = rechtsprofil_service.entwurf_anlegen(
        ctx=admin_ctx, vertrag_id=vertrag.id, rechtsordnung="OESTERREICH_MRG_VOLL", ist_wohnungsnutzung=True,
        mrg_zinsbeschraenkung=False, mrg_zinsbeschraenkung_geprueft=True, ist_altvertrag=False, ist_hauptmiete=True,
        foerderbindung=False, foerderbindung_geprueft=True,
        mietzinsobergrenze_cent=None, mietzinsobergrenze_quellenbeleg=None, mietzinsobergrenze_gueltig_bis=None,
        bezugsjahr=2024, bezugsmonat=1, letzte_basis_war_jahresdurchschnitt=False,
        basis_komponenten_ids=["K-1", "K-2"],
        vertraglich_zulaessiger_betrag_cent=200_000, vertraglicher_quellenbeleg="Punkt 5",
        vertraglicher_fruehestmoeglicher_termin=date(2026, 4, 1), vertrag_beleg_referenz="Vertrag", klausel_referenz=None,
        erstellt_von="markus",
    )
    profil = rechtsprofil_service.freigeben(entwurf.id, ctx=admin_ctx, freigegeben_von="markus")

    schreiben = outbox_repo.anlegen(
        ErhoehungsschreibenTable(
            vertrag_id=vertrag.id, ziel_bewertungsjahr=2026, rechtsprofil_id=profil.id,
            rechtsprofil_version=profil.version, status="SOLL_UMSETZUNG_OFFEN", massgeblicher_termin=date(2026, 4, 1),
            erhoehung_cent=1_000, schreiben_text="Testschreiben", idempotenzschluessel=f"{vertrag.id}:mieweg:2026",
            versendet_am=datetime(2026, 3, 15, 9, 0, tzinfo=timezone.utc), externe_versandreferenz="MAILOPS-TEST-1",
            zugangsform="EINSCHREIBEN", zugang_bestaetigt_am=date(2026, 4, 1), zugang_beleg="RSb-1",
            zahlungspflicht_ab=date(2026, 4, 15), empfaenger_snapshot={"debitor_id": vertrag.debitor_id},
            komponenten_verteilung={
                "eintraege": [
                    {"komponente_id": "K-1", "alter_betrag_cent": 100_000, "neuer_betrag_cent": 100_909},
                    {"komponente_id": "K-2", "alter_betrag_cent": 10_000, "neuer_betrag_cent": 10_091},
                ]
            },
        )
    )

    ergebnis = umsetzung_service.umsetzen(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 4, 20), akteur="markus",
        soll_umsetzung_enabled=True,
    )

    assert ergebnis.status == "UMGESETZT"
    assert ergebnis.neue_komponente_id is None  # Bequemlichkeitsfeld NUR im Ein-Komponenten-Fall gesetzt
    assert len(ergebnis.neue_komponenten_ids) == 2

    alte_k1 = stammdaten_repo.get_komponente("K-1")
    alte_k2 = stammdaten_repo.get_komponente("K-2")
    assert alte_k1.gueltig_bis == date(2026, 3, 31)
    assert alte_k2.gueltig_bis == date(2026, 3, 31)
    assert alte_k1.betrag_cent == 100_000 and alte_k2.betrag_cent == 10_000  # NIE in-place geändert

    neue = {k.id: k for k in (stammdaten_repo.get_komponente(kid) for kid in ergebnis.neue_komponenten_ids)}
    neue_k1 = next(k for k in neue.values() if k.historisiert_von_id == "K-1")
    neue_k2 = next(k for k in neue.values() if k.historisiert_von_id == "K-2")
    assert neue_k1.betrag_cent == 100_909
    assert neue_k2.betrag_cent == 10_091
    assert neue_k1.gueltig_von == date(2026, 4, 1) and neue_k2.gueltig_von == date(2026, 4, 1)

    neues_profil = rechtsprofil_service.aktives_gueltiges_profil(vertrag.id, heute=date(2026, 4, 20))
    assert neues_profil.id == ergebnis.neues_rechtsprofil_id
    assert set(neues_profil.basis_komponenten_ids) == {neue_k1.id, neue_k2.id}

    with session_factory() as session:
        zeile = session.execute(
            select(IndexSollUmsetzungTable).where(IndexSollUmsetzungTable.erhoehungsschreiben_id == schreiben.id)
        ).scalar_one()
        assert zeile.status == "UMGESETZT"
        assert zeile.neue_komponente_id is None
        assert zeile.beendete_komponente_id is None
        assert set(zeile.neue_komponenten_ids) == {neue_k1.id, neue_k2.id}
        assert set(zeile.beendete_komponenten_ids) == {"K-1", "K-2"}


def test_umsetzen_bei_deaktiviertem_flag_bleibt_ohne_wirkung(
    admin_ctx, basis_vertrag, umsetzung_service, outbox_repo, rechtsprofil_service, stammdaten_repo
):
    """`soll_umsetzung_enabled=False` (Default) muss den Fall UNVERÄNDERT
    lassen - kein Claim, keine Statusänderung, keine Komponentenänderung -
    exakt wie `outbox_service.versenden()` mit `send_enabled=False`."""

    vertrag, _konto = basis_vertrag
    profil = _profil_und_komponente(admin_ctx, rechtsprofil_service, stammdaten_repo, vertrag)
    schreiben = _soll_umsetzung_offenes_schreiben(outbox_repo, vertrag, profil)

    ergebnis = umsetzung_service.umsetzen(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 4, 20), akteur="markus",
        soll_umsetzung_enabled=False,
    )

    assert ergebnis.status == "BEREITS_VERARBEITET"
    assert outbox_repo.get(schreiben.id).status == "SOLL_UMSETZUNG_OFFEN"
    assert stammdaten_repo.get_komponente("K-1").betrag_cent == 100_000


def test_umsetzen_wiederholung_ist_bereits_verarbeitet_ohne_doppelte_aenderung(
    admin_ctx, basis_vertrag, umsetzung_service, outbox_repo, rechtsprofil_service, rechtsprofil_repo, stammdaten_repo
):
    vertrag, _konto = basis_vertrag
    profil = _profil_und_komponente(admin_ctx, rechtsprofil_service, stammdaten_repo, vertrag)
    schreiben = _soll_umsetzung_offenes_schreiben(outbox_repo, vertrag, profil)

    erster = umsetzung_service.umsetzen(ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 4, 20), akteur="markus", soll_umsetzung_enabled=True)
    zweiter = umsetzung_service.umsetzen(ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 4, 20), akteur="markus", soll_umsetzung_enabled=True)

    assert erster.status == "UMGESETZT"
    assert zweiter.status == "BEREITS_VERARBEITET"
    assert zweiter.neue_komponente_id is None

    alle_profile = rechtsprofil_repo.liste_fuer_vertrag(vertrag.id)
    assert len(alle_profile) == 2  # ursprüngliches + genau EINE neue Version, keine zweite


def test_umsetzen_paralleler_claim_wird_abgewiesen(
    admin_ctx, basis_vertrag, umsetzung_service, outbox_repo, rechtsprofil_service, stammdaten_repo, session_factory
):
    """Simuliert einen bereits laufenden Claim eines anderen Workers
    (Status SOLL_UMSETZUNG_IN_PRUEFUNG) - ein zweiter Aufruf darf NICHT
    erneut umsetzen (analog `claim_fuer_versand`-CAS-Tests: sequenziell
    statt über echtes Threading geprüft, siehe test_sqlite_write_lock.py
    zur Begründung)."""

    vertrag, _konto = basis_vertrag
    profil = _profil_und_komponente(admin_ctx, rechtsprofil_service, stammdaten_repo, vertrag)
    schreiben = _soll_umsetzung_offenes_schreiben(outbox_repo, vertrag, profil)
    outbox_repo.aktualisieren(schreiben.id, status="SOLL_UMSETZUNG_IN_PRUEFUNG")

    ergebnis = umsetzung_service.umsetzen(ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 4, 20), akteur="markus", soll_umsetzung_enabled=True)

    assert ergebnis.status == "BEREITS_VERARBEITET"
    assert outbox_repo.get(schreiben.id).status == "SOLL_UMSETZUNG_IN_PRUEFUNG"
    assert stammdaten_repo.get_komponente("K-1").betrag_cent == 100_000


def test_umsetzen_stale_snapshot_komponente_seither_geaendert_blockiert(
    admin_ctx, basis_vertrag, umsetzung_service, outbox_repo, rechtsprofil_service, stammdaten_repo
):
    vertrag, _konto = basis_vertrag
    profil = _profil_und_komponente(admin_ctx, rechtsprofil_service, stammdaten_repo, vertrag)
    schreiben = _soll_umsetzung_offenes_schreiben(outbox_repo, vertrag, profil)

    # Jemand hat die Komponente zwischenzeitlich anderweitig geändert
    # (z. B. manuelle Korrektur) - der im Schreiben festgehaltene
    # Altbetrag stimmt nicht mehr.
    stammdaten_repo.add_komponente(
        id="K-1-ERSATZ", vertrag_id=vertrag.id, art="HMZ", bezeichnung="Hauptmietzins", betrag_cent=999_999,
        indexierbar=True, gueltig_von=date(2026, 2, 1),
    )
    from mietinkasso.infrastructure.db.tables import VertragsKomponenteTable

    with umsetzung_service._session_factory() as session:  # noqa: SLF001
        session.execute(sa_update(VertragsKomponenteTable).where(VertragsKomponenteTable.id == "K-1").values(betrag_cent=150_000))
        session.commit()

    ergebnis = umsetzung_service.umsetzen(ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 4, 20), akteur="markus", soll_umsetzung_enabled=True)

    assert ergebnis.status == "BLOCKIERT"
    assert any("Stale-Snapshot" in g for g in ergebnis.gruende)
    assert outbox_repo.get(schreiben.id).status == "SOLL_UMSETZUNG_BLOCKIERT"
    assert stammdaten_repo.get_komponente("K-1").betrag_cent == 150_000  # unverändert durch umsetzen()


def test_umsetzen_stale_snapshot_rechtsprofil_seither_invalidiert_blockiert(
    admin_ctx, basis_vertrag, umsetzung_service, outbox_repo, rechtsprofil_service, rechtsprofil_repo, stammdaten_repo
):
    vertrag, _konto = basis_vertrag
    profil = _profil_und_komponente(admin_ctx, rechtsprofil_service, stammdaten_repo, vertrag)
    schreiben = _soll_umsetzung_offenes_schreiben(outbox_repo, vertrag, profil)

    # Vertrag wurde zwischenzeitlich beendet -> Rechtsprofil-Quelle
    # (vertrag_gueltig_bis fließt in den Hash ein) hat sich geändert.
    stammdaten_repo.upsert_vertrag(
        id=vertrag.id, einheit_id=vertrag.einheit_id, debitor_id=vertrag.debitor_id,
        gesellschaft_id=vertrag.gesellschaft_id, rechtsordnung=vertrag.rechtsordnung,
        gueltig_von=vertrag.gueltig_von, gueltig_bis=date(2030, 12, 31),
    )

    ergebnis = umsetzung_service.umsetzen(ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 4, 20), akteur="markus", soll_umsetzung_enabled=True)

    assert ergebnis.status == "BLOCKIERT"
    assert any("Rechtsprofil ist nicht mehr gültig" in g for g in ergebnis.gruende)
    assert rechtsprofil_repo.get(profil.id).status == "FREIGEGEBEN"  # unverändert, keine stille Invalidierung durch umsetzen()


def test_umsetzen_falscher_mandant_wird_abgewiesen(
    admin_ctx, basis_vertrag, umsetzung_service, outbox_repo, rechtsprofil_service, stammdaten_repo, ctx_factory
):
    vertrag, _konto = basis_vertrag
    profil = _profil_und_komponente(admin_ctx, rechtsprofil_service, stammdaten_repo, vertrag)
    schreiben = _soll_umsetzung_offenes_schreiben(outbox_repo, vertrag, profil)

    fremder_ctx = ctx_factory("FREMDE-GESELLSCHAFT")
    with pytest.raises(CrossTenantError):
        umsetzung_service.umsetzen(ctx=fremder_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 4, 20), akteur="fremd", soll_umsetzung_enabled=True)

    assert outbox_repo.get(schreiben.id).status == "SOLL_UMSETZUNG_OFFEN"


def test_umsetzen_unveraenderte_bk_komponente_bleibt_unangetastet(
    admin_ctx, basis_vertrag, umsetzung_service, outbox_repo, rechtsprofil_service, stammdaten_repo
):
    vertrag, _konto = basis_vertrag
    profil = _profil_und_komponente(admin_ctx, rechtsprofil_service, stammdaten_repo, vertrag)
    stammdaten_repo.add_komponente(
        id="K-BK", vertrag_id=vertrag.id, art="BK_VORAUSZAHLUNG", bezeichnung="Betriebskosten", betrag_cent=15_000,
        indexierbar=False, gueltig_von=date(2024, 1, 1),
    )
    schreiben = _soll_umsetzung_offenes_schreiben(outbox_repo, vertrag, profil)

    ergebnis = umsetzung_service.umsetzen(ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 4, 20), akteur="markus", soll_umsetzung_enabled=True)

    assert ergebnis.status == "UMGESETZT"
    bk_danach = stammdaten_repo.get_komponente("K-BK")
    assert bk_danach.betrag_cent == 15_000
    assert bk_danach.gueltig_bis is None  # unverändert - NIE mitindexiert/beendet


def test_umsetzen_vor_wirksamkeit_der_zahlungspflicht_blockiert(
    admin_ctx, basis_vertrag, umsetzung_service, outbox_repo, rechtsprofil_service, stammdaten_repo
):
    vertrag, _konto = basis_vertrag
    profil = _profil_und_komponente(admin_ctx, rechtsprofil_service, stammdaten_repo, vertrag)
    schreiben = _soll_umsetzung_offenes_schreiben(outbox_repo, vertrag, profil, zahlungspflicht_ab=date(2026, 5, 1))

    ergebnis = umsetzung_service.umsetzen(ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 4, 20), akteur="markus", soll_umsetzung_enabled=True)

    assert ergebnis.status == "BLOCKIERT"
    assert any("noch nicht" in g for g in ergebnis.gruende)
    assert stammdaten_repo.get_komponente("K-1").betrag_cent == 100_000


def test_umsetzen_bereits_gebuchte_folgeperiode_blockiert_keine_doppelbuchung(
    admin_ctx, basis_vertrag, umsetzung_service, outbox_repo, rechtsprofil_service, stammdaten_repo, session_factory
):
    vertrag, _konto = basis_vertrag
    profil = _profil_und_komponente(admin_ctx, rechtsprofil_service, stammdaten_repo, vertrag)
    schreiben = _soll_umsetzung_offenes_schreiben(outbox_repo, vertrag, profil)

    vorschreibung_repo = VorschreibungRepository(session_factory)
    april = vorschreibung_repo.get_or_create_entwurf(vertrag_id=vertrag.id, monat="2026-04", faelligkeit=date(2026, 4, 5))
    vorschreibung_repo.update_status(april.id, status="SOLLGESTELLT")

    ergebnis = umsetzung_service.umsetzen(ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 4, 20), akteur="markus", soll_umsetzung_enabled=True)

    assert ergebnis.status == "BLOCKIERT"
    assert any("Bereits gebuchte Monatsvorschreibung" in g for g in ergebnis.gruende)
    assert stammdaten_repo.get_komponente("K-1").betrag_cent == 100_000


def test_umsetzen_transaktion_bricht_bei_fehler_vollstaendig_ab(
    admin_ctx, basis_vertrag, umsetzung_service, outbox_repo, rechtsprofil_service, rechtsprofil_repo, stammdaten_repo,
    monkeypatch,
):
    """Ein Fehler MITTEN in der Umsetzung (nach dem Claim, nach der
    Komponentenhistorisierung) darf NICHT teilweise wirksam bleiben -
    Claim, Komponentenänderung und Rechtsprofilversion werden zusammen
    committet oder gemeinsam zurückgerollt."""

    vertrag, _konto = basis_vertrag
    profil = _profil_und_komponente(admin_ctx, rechtsprofil_service, stammdaten_repo, vertrag)
    schreiben = _soll_umsetzung_offenes_schreiben(outbox_repo, vertrag, profil)

    def _kaputt(*args, **kwargs):
        raise RuntimeError("Simulierter Absturz mitten in der Umsetzung")

    monkeypatch.setattr(rechtsprofil_service, "_quelle_snapshot", _kaputt)

    with pytest.raises(RuntimeError):
        umsetzung_service.umsetzen(ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 4, 20), akteur="markus", soll_umsetzung_enabled=True)

    # Rollback der GESAMTEN Transaktion: Claim UND Komponentenhistorisierung
    # sind mit zurückgerollt, kein "halb umgesetzter" Zustand.
    assert outbox_repo.get(schreiben.id).status == "SOLL_UMSETZUNG_OFFEN"
    alte_komponente = stammdaten_repo.get_komponente("K-1")
    assert alte_komponente.betrag_cent == 100_000
    assert alte_komponente.gueltig_bis is None
    alle_profile = rechtsprofil_repo.liste_fuer_vertrag(vertrag.id)
    assert len(alle_profile) == 1  # keine verwaiste neue Version übrig geblieben

    # Ein erneuter, unbeschädigter Versuch (ohne den Monkeypatch) muss
    # danach weiterhin normal funktionieren - kein dauerhaft verwaister
    # Zustand durch den vorherigen Absturz.
    monkeypatch.undo()
    ergebnis = umsetzung_service.umsetzen(ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 4, 20), akteur="markus", soll_umsetzung_enabled=True)
    assert ergebnis.status == "UMGESETZT"


def test_umsetzen_ohne_versandbeleg_wird_blockiert(
    admin_ctx, basis_vertrag, umsetzung_service, outbox_repo, rechtsprofil_service, stammdaten_repo
):
    """Codex-Rückprüfung (499c36f, Fund a): ein Fall OHNE tatsächlichen,
    vom Transport bestätigten Versand (versendet_am/
    externe_versandreferenz) darf nie umgesetzt werden - unabhängig
    davon, was `zugang_bestaetigt_am` sagt."""

    vertrag, _konto = basis_vertrag
    profil = _profil_und_komponente(admin_ctx, rechtsprofil_service, stammdaten_repo, vertrag)
    schreiben = _soll_umsetzung_offenes_schreiben(outbox_repo, vertrag, profil, versendet_am=None, externe_versandreferenz=None)

    ergebnis = umsetzung_service.umsetzen(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 4, 20), akteur="markus",
        soll_umsetzung_enabled=True,
    )

    assert ergebnis.status == "BLOCKIERT"
    assert any("Versandbeleg" in g for g in ergebnis.gruende)
    assert stammdaten_repo.get_komponente("K-1").betrag_cent == 100_000


def test_umsetzen_bei_abgelaufener_mietzinsobergrenze_blockiert(
    admin_ctx, basis_vertrag, umsetzung_service, outbox_repo, rechtsprofil_service, rechtsprofil_repo, stammdaten_repo
):
    """Codex-Rückprüfung (499c36f, Fund b): ein reiner Hash-Vergleich
    übersieht eine seither VERSTRICHENE `mietzinsobergrenze_gueltig_bis`
    - `ist_noch_gueltig()` deckt das jetzt ab."""

    vertrag, _konto = basis_vertrag
    profil = _profil_und_komponente(admin_ctx, rechtsprofil_service, stammdaten_repo, vertrag)
    with stammdaten_repo._session_factory() as session:
        row = session.get(RechtsprofilTable, profil.id)
        row.mietzinsobergrenze_cent = 150_000
        row.mietzinsobergrenze_quellenbeleg = "Förderzusicherung"
        row.mietzinsobergrenze_gueltig_bis = date(2026, 4, 10)
        session.commit()

    schreiben = _soll_umsetzung_offenes_schreiben(outbox_repo, vertrag, profil)

    ergebnis = umsetzung_service.umsetzen(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 4, 20), akteur="markus",
        soll_umsetzung_enabled=True,
    )

    assert ergebnis.status == "BLOCKIERT"
    assert any("abgelaufen" in g for g in ergebnis.gruende)
    assert stammdaten_repo.get_komponente("K-1").betrag_cent == 100_000


def test_umsetzen_bewahrt_urspruengliches_enddatum_der_alten_komponente(
    admin_ctx, basis_vertrag, umsetzung_service, outbox_repo, rechtsprofil_service, stammdaten_repo
):
    """Codex-Rückprüfung (499c36f, Fund c): die alte Komponente hat ein
    ursprünglich geplantes Enddatum (z. B. eine befristete Klausel) -
    das darf durch die Umsetzung nicht zu "unbefristet" werden, sondern
    muss auf die neue Komponente übertragen werden."""

    vertrag, _konto = basis_vertrag
    profil = _profil_und_komponente(
        admin_ctx, rechtsprofil_service, stammdaten_repo, vertrag, komponente_gueltig_bis=date(2026, 6, 30)
    )

    schreiben = _soll_umsetzung_offenes_schreiben(outbox_repo, vertrag, profil)
    ergebnis = umsetzung_service.umsetzen(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 4, 20), akteur="markus",
        soll_umsetzung_enabled=True,
    )

    assert ergebnis.status == "UMGESETZT"
    alte_komponente = stammdaten_repo.get_komponente("K-1")
    assert alte_komponente.gueltig_bis == date(2026, 3, 31)  # letzter Tag vor dem Anspruchsmonat
    neue_komponente = stammdaten_repo.get_komponente(ergebnis.neue_komponente_id)
    assert neue_komponente.gueltig_bis == date(2026, 6, 30)  # ursprüngliches Enddatum erhalten


def test_umsetzen_manipulierte_verteilung_wird_blockiert(
    admin_ctx, basis_vertrag, umsetzung_service, outbox_repo, rechtsprofil_service, stammdaten_repo
):
    """Codex-Rückprüfung (499c36f, Fund d): `neuer_betrag_cent` muss
    exakt `alter_betrag_cent + erhoehung_cent` entsprechen - eine
    abweichende/manipulierte Verteilung wird nie blind gebucht."""

    vertrag, _konto = basis_vertrag
    profil = _profil_und_komponente(admin_ctx, rechtsprofil_service, stammdaten_repo, vertrag)
    schreiben = outbox_repo.anlegen(
        ErhoehungsschreibenTable(
            vertrag_id=vertrag.id, ziel_bewertungsjahr=2026, rechtsprofil_id=profil.id,
            rechtsprofil_version=profil.version, status="SOLL_UMSETZUNG_OFFEN", massgeblicher_termin=date(2026, 4, 1),
            erhoehung_cent=1_000, schreiben_text="Testschreiben", idempotenzschluessel=f"{vertrag.id}:mieweg:2026",
            versendet_am=datetime(2026, 3, 15, 9, 0, tzinfo=timezone.utc), externe_versandreferenz="MAILOPS-TEST-1",
            zugangsform="EINSCHREIBEN", zugang_bestaetigt_am=date(2026, 4, 1), zugang_beleg="RSb-1",
            zahlungspflicht_ab=date(2026, 4, 15), empfaenger_snapshot={"debitor_id": vertrag.debitor_id},
            komponenten_verteilung={
                "eintraege": [{"komponente_id": "K-1", "alter_betrag_cent": 100_000, "neuer_betrag_cent": 999_999}]
            },
        )
    )

    ergebnis = umsetzung_service.umsetzen(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 4, 20), akteur="markus",
        soll_umsetzung_enabled=True,
    )

    assert ergebnis.status == "BLOCKIERT"
    assert any("inkonsistent" in g for g in ergebnis.gruende)
    assert stammdaten_repo.get_komponente("K-1").betrag_cent == 100_000


def test_umsetzen_end_to_end_ueber_echten_versand_und_zugang_und_vorschreibung(
    admin_ctx, basis_vertrag, umsetzung_service, outbox_service, outbox_repo, rechtsprofil_service, stammdaten_repo,
    vorschreibung_service, op_service,
):
    """Codex-Rückprüfung (499c36f): End-to-End über den ECHTEN Versand-/
    Zugangs-Ablauf (`outbox_service.versenden`/`zugang_bestaetigen`/
    `taegliche_pflege`), nicht nur eine direkt konstruierte Zeile - UND
    Nachweis über eine echte `VorschreibungService.entwurf_erstellen`,
    dass der Anspruchsmonat tatsächlich den NEUEN Betrag verwendet
    (Komponentenwirksamkeit korrekt auf den Monatsersten gelegt)."""

    vertrag, konto = basis_vertrag
    profil = _profil_und_komponente(admin_ctx, rechtsprofil_service, stammdaten_repo, vertrag)
    debitor = stammdaten_repo.get_debitor(vertrag.debitor_id)
    stammdaten_repo.upsert_debitor(id=debitor.id, name=debitor.name, email=debitor.email, adresse="Corsogasse 1/3, 1010 Wien")

    schreiben = outbox_repo.anlegen(
        ErhoehungsschreibenTable(
            vertrag_id=vertrag.id, ziel_bewertungsjahr=2026, rechtsprofil_id=profil.id,
            rechtsprofil_version=profil.version, status="BEREIT", massgeblicher_termin=date(2026, 3, 1),
            erhoehung_cent=1_000, schreiben_text="Testschreiben", idempotenzschluessel=f"{vertrag.id}:mieweg:2026",
            empfaenger_snapshot={
                "debitor_id": vertrag.debitor_id, "name": debitor.name, "adresse": "Corsogasse 1/3, 1010 Wien",
                "email": debitor.email, "vertrag_gueltig_bis": None, "vertrag_rechtsordnung": vertrag.rechtsordnung,
                "komponenten_snapshot": [
                    {
                        "id": "K-1", "betrag_cent": 100_000, "art": "HMZ", "ust_satz_promille": 10_000,
                        "gueltig_von": "2024-01-01", "gueltig_bis": None,
                    }
                ],
            },
            komponenten_verteilung={
                "eintraege": [{"komponente_id": "K-1", "alter_betrag_cent": 100_000, "neuer_betrag_cent": 101_000}]
            },
        )
    )

    versand = outbox_service.versenden(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 3, 1), send_enabled=True,
        mailops_allowlist_bestaetigt=True, transport=FakeTransportadapter(),
    )
    assert versand.status == "GESENDET"

    zugang = outbox_service.zugang_bestaetigen(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 3, 5), zugang_datum=date(2026, 3, 1),
        zugangsform="EINSCHREIBEN_RUECKSCHEIN", zugang_beleg="Rückschein Post AG Nr. 123",
    )
    assert zugang.status == "ZUGANG_BESTAETIGT"
    zahlungspflicht_ab = zugang.zahlungspflicht_ab
    assert zahlungspflicht_ab is not None

    faellige = outbox_service.taegliche_pflege(heute=zahlungspflicht_ab)
    assert len(faellige) == 1
    assert outbox_repo.get(schreiben.id).status == "SOLL_UMSETZUNG_OFFEN"

    ergebnis = umsetzung_service.umsetzen(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=zahlungspflicht_ab, akteur="markus",
        soll_umsetzung_enabled=True,
    )
    assert ergebnis.status == "UMGESETZT"

    anspruchsmonat = f"{zahlungspflicht_ab.year:04d}-{zahlungspflicht_ab.month:02d}"
    vorschreibungs_ergebnis = vorschreibung_service.entwurf_erstellen(ctx=admin_ctx, vertrag=vertrag, monat=anspruchsmonat)
    assert vorschreibungs_ergebnis.summe_cent == 101_000  # NICHT mehr der alte Betrag (100_000)


def test_umsetzen_zwei_echte_aufeinanderfolgende_monatslaeufe_ohne_wiederholte_aliquotierung(
    admin_ctx, basis_vertrag, umsetzung_service, outbox_repo, outbox_service, rechtsprofil_repo, rechtsprofil_service,
    stammdaten_repo, vorschreibung_service, vpi_repo, indexautomatik_service,
):
    """Unabhängige Rückprüfung (fb34ecb): der vorherige Test erzeugte
    `schreiben_2` künstlich mit einem erfundenen `erhoehung_cent` - KEIN
    echter zweiter Monatslauf. Dieser Test läuft beide Zyklen
    VOLLSTÄNDIG über die echten Produktionspfade
    (`IndexautomatikService.monatslauf_fuer_vertrag` mit amtlichen
    synthetischen Jahres-VPI-Werten, `outbox_service.versenden`/
    `zugang_bestaetigen`/`taegliche_pflege`, `umsetzung_service.
    umsetzen`) und weist eine ECHTE, POSITIVE volle Jahresänderung im
    zweiten Zyklus nach - durch unabhängige Neuberechnung über
    `berechne_gesetzliche_hoechstgrenze` exakt verifiziert (kein bloßes
    ">0"). April 2026 verwendet dabei den Jahresdurchschnitt 2025
    (keine erfundene Dezember-2026-Basis)."""

    from mietinkasso.mieweg_vorschau.berechnung import berechne_gesetzliche_hoechstgrenze

    vertrag, _konto = basis_vertrag
    debitor = stammdaten_repo.get_debitor(vertrag.debitor_id)
    stammdaten_repo.upsert_debitor(id=debitor.id, name=debitor.name, email=debitor.email, adresse="Corsogasse 1/3, 1010 Wien")

    # Ursprüngliches Profil: unterjähriger erster Bezug (Bezugsmonat 7 -
    # ein NEUER Vertrag/eine neue Komponente, noch KEIN Jahresdurchschnitt).
    # Der (großzügige) vertragliche Höchstbetrag aus `_profil_und_komponente`
    # (200.000 Cent) bindet bei den hier gewählten kleinen VPI-Schritten
    # nicht - der Test prüft die GESETZLICHE Spur gegen
    # `berechne_gesetzliche_hoechstgrenze`.
    profil = _profil_und_komponente(
        admin_ctx, rechtsprofil_service, stammdaten_repo, vertrag,
        bezugsjahr=2024, bezugsmonat=7, letzte_basis_war_jahresdurchschnitt=False,
    )

    # Amtliche (synthetische) Jahresdurchschnitte - jeweils unter der
    # 3%-Dämpfungsschwelle, damit die Rechnung klar nachvollziehbar bleibt.
    _seed_vpi(vpi_repo, jahre_werte={2023: "100", 2024: "102", 2025: "104", 2026: "106"})

    # --- Zyklus 1: echter Monatslauf für April 2026 ---------------------
    lauf_1 = indexautomatik_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2026, 4, 5), akteur="test")
    assert lauf_1.status == "ERHOEHUNG_ERZEUGT"
    schreiben_1 = outbox_repo.get(lauf_1.erhoehungsschreiben_id)
    assert schreiben_1.status == "BEREIT"
    assert schreiben_1.erhoehung_cent > 0

    versand_1 = outbox_service.versenden(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben_1.id, heute=date(2026, 4, 5), send_enabled=True,
        mailops_allowlist_bestaetigt=True, transport=FakeTransportadapter(),
    )
    assert versand_1.status == "GESENDET"
    zugang_1 = outbox_service.zugang_bestaetigen(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben_1.id, heute=date(2026, 4, 6), zugang_datum=date(2026, 4, 5),
        zugangsform="EINSCHREIBEN_RUECKSCHEIN", zugang_beleg="Rückschein Post AG Nr. 1",
    )
    assert zugang_1.status == "ZUGANG_BESTAETIGT"
    zahlungspflicht_ab_1 = zugang_1.zahlungspflicht_ab

    faellige_1 = outbox_service.taegliche_pflege(heute=zahlungspflicht_ab_1)
    assert len(faellige_1) == 1

    ergebnis_1 = umsetzung_service.umsetzen(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben_1.id, heute=zahlungspflicht_ab_1, akteur="markus",
        soll_umsetzung_enabled=True,
    )
    assert ergebnis_1.status == "UMGESETZT"

    neues_profil_1 = rechtsprofil_repo.get(ergebnis_1.neues_rechtsprofil_id)
    # Bewusst EIN JAHR VOR dem verarbeiteten Bewertungsjahr (siehe
    # umsetzung_service.py-Kommentar zum "Leerschritt") - NICHT das
    # Bewertungsjahr selbst und NICHT Dezember 2026 als erfundene Basis.
    assert neues_profil_1.bezugsjahr == 2025
    assert neues_profil_1.bezugsmonat == 12
    assert neues_profil_1.letzte_basis_war_jahresdurchschnitt is True
    assert neues_profil_1.basis_komponenten_ids == [ergebnis_1.neue_komponente_id]

    neue_komponente_1 = stammdaten_repo.get_komponente(ergebnis_1.neue_komponente_id)
    assert neue_komponente_1.betrag_cent == 100_000 + schreiben_1.erhoehung_cent

    # --- Zyklus 2: echter, UNMITTELBAR folgender Monatslauf für April 2027 ---
    lauf_2 = indexautomatik_service.monatslauf_fuer_vertrag(ctx=admin_ctx, vertrag=vertrag, heute=date(2027, 4, 5), akteur="test")
    assert lauf_2.status == "ERHOEHUNG_ERZEUGT", lauf_2.blockiert_gruende
    schreiben_2 = outbox_repo.get(lauf_2.erhoehungsschreiben_id)
    assert schreiben_2.komponenten_verteilung["eintraege"][0]["komponente_id"] == ergebnis_1.neue_komponente_id

    # Unabhängige Neuberechnung derselben Periode über den ECHTEN Rechner
    # (kein zweites, eigenes Rechenmodell) - beweist eine ECHTE, POSITIVE
    # volle Jahresänderung (2026 gegenüber 2025), NICHT bloß "> 0".
    erwartetes_ergebnis = berechne_gesetzliche_hoechstgrenze(
        mrg_zinsbeschraenkung=False, erster_bezug_jahr=neues_profil_1.bezugsjahr, erster_bezug_monat=12,
        ziel_bewertungsjahr=2027, vpi_jahresdurchschnitte={2024: Decimal("102"), 2025: Decimal("104"), 2026: Decimal("106")},
        basis_betrag_cent=neue_komponente_1.betrag_cent,
    )
    assert erwartetes_ergebnis.vollstaendig
    # Der EINZIGE mit vollem Gewicht (anteil=1) zählende Schritt ist 2026
    # gegenüber 2025 - 2025 gegenüber 2024 ist der bewusste Leerschritt
    # (anteil=0), keine doppelte Zählung des bereits verarbeiteten Jahres.
    volle_schritte = [s for s in erwartetes_ergebnis.jahresschritte if s.anteil == 1]
    assert [s.jahr for s in volle_schritte] == [2026]
    erwartete_erhoehung_cent = erwartetes_ergebnis.hoechstbetrag_cent - neue_komponente_1.betrag_cent
    assert erwartete_erhoehung_cent > 0
    assert schreiben_2.erhoehung_cent == erwartete_erhoehung_cent

    versand_2 = outbox_service.versenden(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben_2.id, heute=date(2027, 4, 5), send_enabled=True,
        mailops_allowlist_bestaetigt=True, transport=FakeTransportadapter(),
    )
    assert versand_2.status == "GESENDET"
    zugang_2 = outbox_service.zugang_bestaetigen(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben_2.id, heute=date(2027, 4, 6), zugang_datum=date(2027, 4, 5),
        zugangsform="EINSCHREIBEN_RUECKSCHEIN", zugang_beleg="Rückschein Post AG Nr. 2",
    )
    assert zugang_2.status == "ZUGANG_BESTAETIGT"
    zahlungspflicht_ab_2 = zugang_2.zahlungspflicht_ab

    faellige_2 = outbox_service.taegliche_pflege(heute=zahlungspflicht_ab_2)
    assert len(faellige_2) == 1

    ergebnis_2 = umsetzung_service.umsetzen(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben_2.id, heute=zahlungspflicht_ab_2, akteur="markus",
        soll_umsetzung_enabled=True,
    )
    assert ergebnis_2.status == "UMGESETZT"

    neue_komponente_2 = stammdaten_repo.get_komponente(ergebnis_2.neue_komponente_id)
    assert neue_komponente_2.betrag_cent == neue_komponente_1.betrag_cent + erwartete_erhoehung_cent

    anspruchsmonat_2 = f"{zahlungspflicht_ab_2.year:04d}-{zahlungspflicht_ab_2.month:02d}"
    vorschreibungs_ergebnis = vorschreibung_service.entwurf_erstellen(ctx=admin_ctx, vertrag=vertrag, monat=anspruchsmonat_2)
    assert vorschreibungs_ergebnis.summe_cent == neue_komponente_2.betrag_cent
