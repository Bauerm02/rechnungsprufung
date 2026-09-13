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

from datetime import date

import pytest
from sqlalchemy import select, update as sa_update

from mietinkasso.domain.exceptions import CrossTenantError
from mietinkasso.index.repository import IndexRepository
from mietinkasso.indexautomatik.rechtsprofil import RechtsprofilService
from mietinkasso.indexautomatik.repository import ErhoehungsschreibenRepository, RechtsprofilRepository
from mietinkasso.indexautomatik.umsetzung_service import IndexSollUmsetzungService
from mietinkasso.infrastructure.db.tables import ErhoehungsschreibenTable, IndexSollUmsetzungTable, RechtsprofilTable
from mietinkasso.vorschreibung.repository import VorschreibungRepository


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


def _profil_und_komponente(admin_ctx, rechtsprofil_service, stammdaten_repo, vertrag, *, komponente_id="K-1"):
    stammdaten_repo.add_komponente(
        id=komponente_id, vertrag_id=vertrag.id, art="HMZ", bezeichnung="Hauptmietzins", betrag_cent=100_000,
        indexierbar=True, gueltig_von=date(2024, 1, 1),
    )
    entwurf = rechtsprofil_service.entwurf_anlegen(
        ctx=admin_ctx, vertrag_id=vertrag.id, rechtsordnung="OESTERREICH_MRG_VOLL", ist_wohnungsnutzung=True,
        mrg_zinsbeschraenkung=False, ist_altvertrag=False, ist_hauptmiete=True, foerderbindung=False,
        mietzinsobergrenze_cent=None, mietzinsobergrenze_quellenbeleg=None, mietzinsobergrenze_gueltig_bis=None,
        bezugsjahr=2024, bezugsmonat=1, letzte_basis_war_jahresdurchschnitt=False, basis_komponenten_ids=[komponente_id],
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
    zahlungspflicht_ab=date(2026, 4, 15),
    zugang_bestaetigt_am=date(2026, 4, 1),
) -> ErhoehungsschreibenTable:
    return outbox_repo.anlegen(
        ErhoehungsschreibenTable(
            vertrag_id=vertrag.id,
            ziel_bewertungsjahr=2026,
            rechtsprofil_id=profil.id,
            rechtsprofil_version=profil.version,
            status="SOLL_UMSETZUNG_OFFEN",
            massgeblicher_termin=date(2026, 4, 1),
            erhoehung_cent=erhoehung_cent,
            schreiben_text="Testschreiben",
            idempotenzschluessel=f"{vertrag.id}:mieweg:2026",
            zugangsform="EINSCHREIBEN",
            zugang_bestaetigt_am=zugang_bestaetigt_am,
            zugang_beleg="RSb-1",
            zahlungspflicht_ab=zahlungspflicht_ab,
            empfaenger_snapshot={"debitor_id": vertrag.debitor_id},
            komponenten_verteilung={
                "komponente_id": komponente_id,
                "alter_betrag_cent": alter_betrag_cent,
                "neuer_betrag_cent": alter_betrag_cent + erhoehung_cent,
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
    assert alte_komponente.gueltig_bis == date(2026, 4, 14)
    assert alte_komponente.betrag_cent == 100_000  # NIE in-place geändert

    neue_komponente = stammdaten_repo.get_komponente(ergebnis.neue_komponente_id)
    assert neue_komponente is not None
    assert neue_komponente.betrag_cent == 101_000
    assert neue_komponente.gueltig_von == date(2026, 4, 15)
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
        assert zeile.wirksam_ab == date(2026, 4, 15)


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
    assert any("Rechtsprofil-Freigabe geändert" in g for g in ergebnis.gruende)
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
