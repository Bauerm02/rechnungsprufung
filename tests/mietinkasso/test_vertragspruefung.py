"""Tests für `vertragspruefung/service.py` (Auftrag 12.09., Paket B,
Punkt 1): versionierte Vertragsprüfung mit Pflicht-Quellenbeleg,
explizites Rechtsprofil, Freigabe nur bei GEPRUEFT, einzelne
begründete Sperren-Aufhebung, unvollständige Indexangaben als reiner
Prüfbedarf ohne Freigabe."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from mietinkasso.domain.exceptions import BindungInkonsistentError, CrossTenantError, QuellenbelegFehltError
from mietinkasso.vertragspruefung.repository import IndexPruefbedarfRepository, VertragPruefungRepository
from mietinkasso.vertragspruefung.service import VertragspruefungService


@pytest.fixture
def pruefung_service(session_factory, stammdaten_repo) -> VertragspruefungService:
    return VertragspruefungService(
        VertragPruefungRepository(session_factory), IndexPruefbedarfRepository(session_factory), stammdaten_repo,
    )


def test_pruefung_ohne_quellenbeleg_wird_abgelehnt(pruefung_service, basis_vertrag, ctx_factory):
    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    with pytest.raises(QuellenbelegFehltError):
        pruefung_service.pruefung_anlegen(
            ctx=ctx, vertrag=vertrag, rechtsordnung="OESTERREICH_MRG_VOLL", fachstatus="GEPRUEFT",
            quellenbeleg_referenz="   ", kommentar=None, akteur="test",
        )


def test_pruefung_mit_ungueltiger_rechtsordnung_wird_abgelehnt(pruefung_service, basis_vertrag, ctx_factory):
    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    with pytest.raises(ValueError, match="Rechtsordnung"):
        pruefung_service.pruefung_anlegen(
            ctx=ctx, vertrag=vertrag, rechtsordnung="UNSINN", fachstatus="GEPRUEFT",
            quellenbeleg_referenz="Mietvertrag-2024.pdf", kommentar=None, akteur="test",
        )


def test_pruefung_mit_ungueltigem_fachstatus_wird_abgelehnt(pruefung_service, basis_vertrag, ctx_factory):
    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    with pytest.raises(ValueError, match="Fachstatus"):
        pruefung_service.pruefung_anlegen(
            ctx=ctx, vertrag=vertrag, rechtsordnung="OESTERREICH_MRG_VOLL", fachstatus="FREIGEGEBEN",
            quellenbeleg_referenz="Mietvertrag-2024.pdf", kommentar=None, akteur="test",
        )


def test_entwurf_speichert_version_aber_aendert_vertrag_nicht(pruefung_service, stammdaten_repo, basis_vertrag, ctx_factory):
    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    ursprungs_rechtsordnung = vertrag.rechtsordnung

    pruefung = pruefung_service.pruefung_anlegen(
        ctx=ctx, vertrag=vertrag, rechtsordnung="UNGEKLAERT", fachstatus="ENTWURF",
        quellenbeleg_referenz="Notiz: RA-Anfrage noch offen", kommentar="Vorläufig", akteur="markus",
    )
    assert pruefung.version == 1
    assert pruefung.fachstatus == "ENTWURF"

    unveraendert = stammdaten_repo.get_vertrag(vertrag.id)
    assert unveraendert.rechtsordnung == ursprungs_rechtsordnung  # KEINE Freigabe bei ENTWURF


def test_geprueft_schreibt_rechtsordnung_auf_vertrag_zurueck(pruefung_service, stammdaten_repo, basis_vertrag, ctx_factory):
    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")

    pruefung_service.pruefung_anlegen(
        ctx=ctx, vertrag=vertrag, rechtsordnung="OESTERREICH_MRG_TEIL", fachstatus="GEPRUEFT",
        quellenbeleg_referenz="Mietvertrag-2024.pdf, S. 3", kommentar="Teilausnahme laut §1 Abs 4 MRG", akteur="markus",
    )
    aktualisiert = stammdaten_repo.get_vertrag(vertrag.id)
    assert aktualisiert.rechtsordnung == "OESTERREICH_MRG_TEIL"


def test_mehrere_pruefungen_bilden_aufsteigende_versionsgeschichte(pruefung_service, basis_vertrag, ctx_factory):
    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")

    pruefung_service.pruefung_anlegen(
        ctx=ctx, vertrag=vertrag, rechtsordnung="UNGEKLAERT", fachstatus="ENTWURF",
        quellenbeleg_referenz="Erste Sichtung", kommentar=None, akteur="markus",
    )
    pruefung_service.pruefung_anlegen(
        ctx=ctx, vertrag=vertrag, rechtsordnung="OESTERREICH_MRG_VOLL", fachstatus="GEPRUEFT",
        quellenbeleg_referenz="Mietvertrag-2024.pdf", kommentar=None, akteur="markus",
    )
    historie = pruefung_service.liste_pruefungen(vertrag.id)
    assert [p.version for p in historie] == [2, 1]  # neueste zuerst
    assert historie[0].fachstatus == "GEPRUEFT"
    assert historie[1].fachstatus == "ENTWURF"


def test_pruefung_ausserhalb_der_eigenen_gesellschaft_wird_blockiert(pruefung_service, basis_vertrag, ctx_factory):
    vertrag, _ = basis_vertrag
    ctx_fremd = ctx_factory("ANDERE-GESELLSCHAFT")
    with pytest.raises(CrossTenantError):
        pruefung_service.pruefung_anlegen(
            ctx=ctx_fremd, vertrag=vertrag, rechtsordnung="OESTERREICH_MRG_VOLL", fachstatus="GEPRUEFT",
            quellenbeleg_referenz="Mietvertrag-2024.pdf", kommentar=None, akteur="test",
        )


# ---------------------------------------------------------------------------
# Sperren-Aufhebung: einzeln, begründet, Audit/Scope
# ---------------------------------------------------------------------------


def test_sperre_aufheben_ohne_begruendung_wird_abgelehnt(pruefung_service, stammdaten_repo, basis_vertrag, ctx_factory):
    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    sperre_id = stammdaten_repo.sperre_setzen(vertrag_id=vertrag.id, grund="RATENPLAN", kommentar="3 Raten vereinbart")
    with pytest.raises(QuellenbelegFehltError):
        pruefung_service.sperre_aufheben(ctx=ctx, vertrag=vertrag, sperre_id=sperre_id, begruendung="  ", akteur="markus")
    assert len(stammdaten_repo.aktive_sperren(vertrag.id)) == 1  # unverändert aktiv


def test_sperre_aufheben_mit_begruendung_funktioniert_fuer_ratenplan_und_rechtsanwalt(
    pruefung_service, stammdaten_repo, basis_vertrag, ctx_factory
):
    """RATENPLAN/RECHTSANWALT sind NICHT von der Aufhebung ausgeschlossen -
    sie werden nur nie AUTOMATISCH/gebündelt aufgehoben (siehe unten),
    eine einzelne, begründete Aufhebung bleibt möglich."""

    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    ratenplan_id = stammdaten_repo.sperre_setzen(vertrag_id=vertrag.id, grund="RATENPLAN", kommentar="3 Raten")
    ra_id = stammdaten_repo.sperre_setzen(vertrag_id=vertrag.id, grund="RECHTSANWALT", kommentar="RA Dr. Muster")

    pruefung_service.sperre_aufheben(
        ctx=ctx, vertrag=vertrag, sperre_id=ratenplan_id, begruendung="Ratenplan vollständig erfüllt (Zahlungsbeleg X)",
        akteur="markus",
    )
    verbleibend = stammdaten_repo.aktive_sperren(vertrag.id)
    assert len(verbleibend) == 1
    assert verbleibend[0].id == ra_id  # RECHTSANWALT bleibt unangetastet, nur die EINE gewählte Sperre wurde gehoben


def test_sperre_aufheben_bereits_aufgehobener_sperre_ist_fehler(pruefung_service, stammdaten_repo, basis_vertrag, ctx_factory):
    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    sperre_id = stammdaten_repo.sperre_setzen(vertrag_id=vertrag.id, grund="MANUELL")
    pruefung_service.sperre_aufheben(ctx=ctx, vertrag=vertrag, sperre_id=sperre_id, begruendung="Grund A", akteur="markus")
    with pytest.raises(ValueError, match="bereits aufgehoben"):
        pruefung_service.sperre_aufheben(ctx=ctx, vertrag=vertrag, sperre_id=sperre_id, begruendung="Grund B", akteur="markus")


def test_sperre_eines_fremden_vertrags_wird_blockiert(pruefung_service, stammdaten_repo, basis_vertrag, ctx_factory):
    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    stammdaten_repo.upsert_gesellschaft(id="7DI", name="7D Immobilien GmbH")
    stammdaten_repo.upsert_objekt(id="601-ZWEI", gesellschaft_id="7DI", bezeichnung="Anderes Objekt")
    stammdaten_repo.upsert_einheit(id="601-ZWEI-TOP1", objekt_id="601-ZWEI", bezeichnung="Top 1", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten_repo.upsert_debitor(id="DEB-ZWEI", name="Andere Mieterin")
    stammdaten_repo.upsert_vertrag(
        id="V-ZWEI", einheit_id="601-ZWEI-TOP1", debitor_id="DEB-ZWEI", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    fremder_vertrag = stammdaten_repo.get_vertrag("V-ZWEI")
    sperre_id = stammdaten_repo.sperre_setzen(vertrag_id=fremder_vertrag.id, grund="MANUELL")

    with pytest.raises(BindungInkonsistentError):
        pruefung_service.sperre_aufheben(ctx=ctx, vertrag=vertrag, sperre_id=sperre_id, begruendung="Versuchter Fremdzugriff", akteur="markus")
    assert len(stammdaten_repo.aktive_sperren(fremder_vertrag.id)) == 1  # unverändert


# ---------------------------------------------------------------------------
# Index-Prüfbedarf: unvollständig speicherbar, keine Freigabe
# ---------------------------------------------------------------------------


def test_index_pruefbedarf_akzeptiert_komplett_leere_fachfelder(pruefung_service, basis_vertrag, ctx_factory):
    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    eintrag = pruefung_service.index_pruefbedarf_speichern(
        ctx=ctx, vertrag=vertrag, rechtsordnung=None, basis_reihe=None, basis_wert=None, basis_monat=None,
        kommentar="Erste Notiz, Daten folgen", akteur="markus",
    )
    assert eintrag.basis_wert is None
    assert eintrag.basis_reihe is None


def test_index_pruefbedarf_beruehrt_index_klausel_tabelle_nicht(pruefung_service, session_factory, basis_vertrag, ctx_factory):
    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    pruefung_service.index_pruefbedarf_speichern(
        ctx=ctx, vertrag=vertrag, rechtsordnung="OESTERREICH_MRG_VOLL", basis_reihe="VPI2020",
        basis_wert=Decimal("100.0"), basis_monat=None, kommentar=None, akteur="markus",
    )
    from sqlalchemy import select

    from mietinkasso.infrastructure.db.tables import IndexKlauselTable

    with session_factory() as session:
        assert session.execute(select(IndexKlauselTable)).first() is None


def test_index_pruefbedarf_liste_zeigt_neueste_zuerst(pruefung_service, basis_vertrag, ctx_factory):
    vertrag, _ = basis_vertrag
    ctx = ctx_factory("7DI")
    pruefung_service.index_pruefbedarf_speichern(
        ctx=ctx, vertrag=vertrag, rechtsordnung=None, basis_reihe=None, basis_wert=None, basis_monat=None,
        kommentar="Erste", akteur="markus",
    )
    pruefung_service.index_pruefbedarf_speichern(
        ctx=ctx, vertrag=vertrag, rechtsordnung=None, basis_reihe="VPI2020", basis_wert=None, basis_monat=None,
        kommentar="Zweite", akteur="markus",
    )
    liste = pruefung_service.liste_index_pruefbedarf(vertrag.id)
    assert [e.kommentar for e in liste] == ["Zweite", "Erste"]
