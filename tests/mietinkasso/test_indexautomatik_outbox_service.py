from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from mietinkasso.domain.exceptions import QuellenbelegFehltError, TransportFehlerUngewissError
from mietinkasso.indexautomatik.outbox_service import ErhoehungsschreibenOutboxService
from mietinkasso.indexautomatik.repository import ErhoehungsschreibenRepository, RechtsprofilRepository
from mietinkasso.indexautomatik.transport import FakeTransportadapter
from mietinkasso.infrastructure.db.tables import ErhoehungsschreibenTable, RechtsprofilTable


@pytest.fixture
def outbox_repo(session_factory) -> ErhoehungsschreibenRepository:
    return ErhoehungsschreibenRepository(session_factory)


@pytest.fixture
def rechtsprofil_repo(session_factory) -> RechtsprofilRepository:
    return RechtsprofilRepository(session_factory)


@pytest.fixture
def outbox_service(outbox_repo, stammdaten_repo) -> ErhoehungsschreibenOutboxService:
    return ErhoehungsschreibenOutboxService(outbox_repo, stammdaten_repo, jlb_signatur="JLB Projects GmbH")


def _bereites_schreiben(outbox_repo, rechtsprofil_repo, stammdaten_repo, vertrag) -> ErhoehungsschreibenTable:
    profil = rechtsprofil_repo.anlegen(
        RechtsprofilTable(
            vertrag_id=vertrag.id, version=1, rechtsordnung=vertrag.rechtsordnung, bezugsjahr=2024, bezugsmonat=1,
            basis_komponenten_ids=[], vertrag_beleg_referenz="Beleg", erstellt_von="test",
        )
    )
    debitor = stammdaten_repo.get_debitor(vertrag.debitor_id)
    stammdaten_repo.upsert_debitor(id=debitor.id, name=debitor.name, email=debitor.email, adresse="Corsogasse 1/3, 1010 Wien")
    vertrag = stammdaten_repo.get_vertrag(vertrag.id)
    debitor = stammdaten_repo.get_debitor(vertrag.debitor_id)
    empfaenger_snapshot = {
        "debitor_id": vertrag.debitor_id, "name": debitor.name, "adresse": debitor.adresse, "email": debitor.email,
        "vertrag_gueltig_bis": vertrag.gueltig_bis.isoformat() if vertrag.gueltig_bis else None,
        "vertrag_rechtsordnung": vertrag.rechtsordnung,
    }
    return outbox_repo.anlegen(
        ErhoehungsschreibenTable(
            vertrag_id=vertrag.id, ziel_bewertungsjahr=2026, rechtsprofil_id=profil.id, rechtsprofil_version=profil.version,
            mieweg_vorschau_id=None, status="BEREIT", massgeblicher_termin=date(2026, 4, 1), erhoehung_cent=1000,
            schreiben_text="Testschreiben", idempotenzschluessel=f"{vertrag.id}:2026", empfaenger_snapshot=empfaenger_snapshot,
        )
    )


def test_versand_deaktiviert_bleibt_bereit_kein_realer_versand(admin_ctx, basis_vertrag, outbox_service, outbox_repo, rechtsprofil_repo, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    schreiben = _bereites_schreiben(outbox_repo, rechtsprofil_repo, stammdaten_repo, vertrag)
    transport = FakeTransportadapter()
    ergebnis = outbox_service.versenden(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, send_enabled=False, mailops_allowlist_bestaetigt=False,
        transport=transport,
    )
    assert ergebnis.status == "BEREITS_VERARBEITET"
    assert transport.aufrufe == []
    assert outbox_repo.get(schreiben.id).status == "BEREIT"


def test_beide_flags_noetig_fuer_realen_versand(admin_ctx, basis_vertrag, outbox_service, outbox_repo, rechtsprofil_repo, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    schreiben = _bereites_schreiben(outbox_repo, rechtsprofil_repo, stammdaten_repo, vertrag)
    transport = FakeTransportadapter()
    ergebnis = outbox_service.versenden(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, send_enabled=True, mailops_allowlist_bestaetigt=False,
        transport=transport,
    )
    assert ergebnis.status == "BEREITS_VERARBEITET"
    assert transport.aufrufe == []


def test_erfolgreicher_versand_setzt_gesendet(admin_ctx, basis_vertrag, outbox_service, outbox_repo, rechtsprofil_repo, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    schreiben = _bereites_schreiben(outbox_repo, rechtsprofil_repo, stammdaten_repo, vertrag)
    transport = FakeTransportadapter()
    ergebnis = outbox_service.versenden(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, send_enabled=True, mailops_allowlist_bestaetigt=True,
        transport=transport,
    )
    assert ergebnis.status == "GESENDET"
    assert len(transport.aufrufe) == 1
    aktualisiert = outbox_repo.get(schreiben.id)
    assert aktualisiert.status == "GESENDET"
    assert aktualisiert.versendet_am is not None


def test_provider_timeout_setzt_unklar_kein_blinder_retry(admin_ctx, basis_vertrag, outbox_service, outbox_repo, rechtsprofil_repo, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    schreiben = _bereites_schreiben(outbox_repo, rechtsprofil_repo, stammdaten_repo, vertrag)
    transport = FakeTransportadapter(verhalten="TIMEOUT")
    ergebnis = outbox_service.versenden(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, send_enabled=True, mailops_allowlist_bestaetigt=True,
        transport=transport,
    )
    assert ergebnis.status == "UNKLAR"
    assert outbox_repo.get(schreiben.id).status == "UNKLAR"
    # Ein zweiter Versuch darf NICHT automatisch erneut senden.
    zweiter = outbox_service.versenden(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, send_enabled=True, mailops_allowlist_bestaetigt=True,
        transport=transport,
    )
    assert zweiter.status == "BEREITS_VERARBEITET"
    assert len(transport.aufrufe) == 1


def test_geaenderter_empfaenger_blockiert_versand(admin_ctx, basis_vertrag, outbox_service, outbox_repo, rechtsprofil_repo, stammdaten_repo):
    """"abgelaufener Vertrag und geänderter Empfänger": eine seit der
    Entwurfserstellung geänderte Adresse stoppt den Versand."""

    vertrag, _konto = basis_vertrag
    schreiben = _bereites_schreiben(outbox_repo, rechtsprofil_repo, stammdaten_repo, vertrag)
    debitor = stammdaten_repo.get_debitor(vertrag.debitor_id)
    stammdaten_repo.upsert_debitor(id=debitor.id, name=debitor.name, email=debitor.email, adresse="Andere Straße 5, 1020 Wien")

    ergebnis = outbox_service.versenden(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, send_enabled=True, mailops_allowlist_bestaetigt=True,
        transport=FakeTransportadapter(),
    )
    assert ergebnis.status == "BLOCKIERT"
    assert outbox_repo.get(schreiben.id).status == "BLOCKIERT"


def test_abgelaufener_vertrag_blockiert_versand(admin_ctx, basis_vertrag, outbox_service, outbox_repo, rechtsprofil_repo, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    schreiben = _bereites_schreiben(outbox_repo, rechtsprofil_repo, stammdaten_repo, vertrag)
    stammdaten_repo.upsert_objekt(id="601", gesellschaft_id="7DI", bezeichnung="Am Corso", ausgeschlossen=True)

    ergebnis = outbox_service.versenden(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, send_enabled=True, mailops_allowlist_bestaetigt=True,
        transport=FakeTransportadapter(),
    )
    assert ergebnis.status == "BLOCKIERT"


def test_zugang_email_unbestaetigt_wird_abgelehnt(admin_ctx, basis_vertrag, outbox_service, outbox_repo, rechtsprofil_repo, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    schreiben = _bereites_schreiben(outbox_repo, rechtsprofil_repo, stammdaten_repo, vertrag)
    outbox_service.versenden(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, send_enabled=True, mailops_allowlist_bestaetigt=True,
        transport=FakeTransportadapter(),
    )
    with pytest.raises(ValueError):
        outbox_service.zugang_bestaetigen(
            ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, zugang_datum=date(2026, 3, 1),
            zugangsform="EMAIL_UNBESTAETIGT", zugang_beleg="Versandprotokoll",
        )
    assert outbox_repo.get(schreiben.id).status == "GESENDET"


def test_zugang_ohne_beleg_wird_abgelehnt(admin_ctx, basis_vertrag, outbox_service, outbox_repo, rechtsprofil_repo, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    schreiben = _bereites_schreiben(outbox_repo, rechtsprofil_repo, stammdaten_repo, vertrag)
    outbox_service.versenden(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, send_enabled=True, mailops_allowlist_bestaetigt=True,
        transport=FakeTransportadapter(),
    )
    with pytest.raises(QuellenbelegFehltError):
        outbox_service.zugang_bestaetigen(
            ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, zugang_datum=date(2026, 3, 1),
            zugangsform="EINSCHREIBEN_RUECKSCHEIN", zugang_beleg="",
        )


def test_ausreichender_zugang_berechnet_zahlungspflicht_und_ausfuehrung(admin_ctx, basis_vertrag, outbox_service, outbox_repo, rechtsprofil_repo, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    schreiben = _bereites_schreiben(outbox_repo, rechtsprofil_repo, stammdaten_repo, vertrag)
    outbox_service.versenden(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, send_enabled=True, mailops_allowlist_bestaetigt=True,
        transport=FakeTransportadapter(),
    )
    aktualisiert = outbox_service.zugang_bestaetigen(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, zugang_datum=date(2026, 3, 1),
        zugangsform="EINSCHREIBEN_RUECKSCHEIN", zugang_beleg="Rückschein Nr. 123",
    )
    assert aktualisiert.status == "ZUGANG_BESTAETIGT"
    # faelligkeit_tag=5 (Default), 14 Tage nach 1.3. = 15.3. -> naechster Zinstermin 5.4.
    assert aktualisiert.zahlungspflicht_ab == date(2026, 4, 5)

    vor_faelligkeit = outbox_service.taegliche_pflege(heute=date(2026, 4, 1))
    assert vor_faelligkeit == []
    nach_faelligkeit = outbox_service.taegliche_pflege(heute=date(2026, 4, 5))
    assert len(nach_faelligkeit) == 1
    assert outbox_repo.get(schreiben.id).status == "AUSGEFUEHRT"


def test_verwaiste_in_versand_werden_markiert(admin_ctx, basis_vertrag, outbox_service, outbox_repo, rechtsprofil_repo, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    schreiben = _bereites_schreiben(outbox_repo, rechtsprofil_repo, stammdaten_repo, vertrag)
    alt = datetime.now(timezone.utc) - timedelta(minutes=30)
    outbox_repo.claim_fuer_versand(schreiben.id, jetzt=alt)
    verwaiste = outbox_service.markiere_verwaiste_als_unklar()
    assert [r.id for r in verwaiste] == [schreiben.id]
    assert outbox_repo.get(schreiben.id).status == "UNKLAR"


def test_mehrkomponenten_werden_vor_versand_blockiert(admin_ctx, basis_vertrag, outbox_service, stammdaten_repo):
    from mietinkasso.mieweg_vorschau.repository import MieWegVorschauRepository
    from mietinkasso.mieweg_vorschau.service import MieWegVorschauService, VpiWert

    vertrag, _konto = basis_vertrag
    stammdaten_repo.add_komponente(id="K-1", vertrag_id=vertrag.id, art="HMZ", bezeichnung="HMZ", betrag_cent=100_000, indexierbar=True, gueltig_von=date(2024, 1, 1))
    stammdaten_repo.add_komponente(id="K-2", vertrag_id=vertrag.id, art="KUECHE", bezeichnung="Küche", betrag_cent=10_000, indexierbar=True, gueltig_von=date(2024, 1, 1))

    from mietinkasso.indexautomatik.rechtsprofil import RechtsprofilService
    from mietinkasso.index.repository import IndexRepository

    profil_repo = RechtsprofilRepository(stammdaten_repo._session_factory)
    rechtsprofil_service = RechtsprofilService(profil_repo, stammdaten_repo, IndexRepository(stammdaten_repo._session_factory))
    profil = rechtsprofil_service.entwurf_anlegen(
        ctx=admin_ctx, vertrag_id=vertrag.id, rechtsordnung="OESTERREICH_MRG_VOLL", ist_wohnungsnutzung=True,
        mrg_zinsbeschraenkung=False, ist_altvertrag=False, ist_hauptmiete=True, foerderbindung=False,
        mietzinsobergrenze_cent=None, mietzinsobergrenze_quellenbeleg=None, mietzinsobergrenze_gueltig_bis=None,
        bezugsjahr=2024, bezugsmonat=1, letzte_basis_war_jahresdurchschnitt=False, basis_komponenten_ids=["K-1", "K-2"],
        vertraglich_zulaessiger_betrag_cent=200_000, vertraglicher_quellenbeleg="Punkt 5",
        vertraglicher_fruehestmoeglicher_termin=date(2026, 4, 1), vertrag_beleg_referenz="Vertrag", klausel_referenz=None,
        erstellt_von="markus",
    )
    profil = rechtsprofil_service.freigeben(profil.id, ctx=admin_ctx, freigegeben_von="markus")

    mieweg_service = MieWegVorschauService(MieWegVorschauRepository(stammdaten_repo._session_factory), stammdaten_repo)
    vorschau = mieweg_service.vorschau_erstellen(
        ctx=admin_ctx, vertrag=vertrag, rechtsordnung="OESTERREICH_MRG_VOLL", ist_wohnungsnutzung=True,
        mrg_zinsbeschraenkung=False, ist_altvertrag=False, bezugsjahr=2024, bezugsmonat=1,
        letzte_basis_war_jahresdurchschnitt=False, ziel_bewertungsjahr=2026, basis_betrag_cent=110_000,
        basis_komponenten_ids=["K-1", "K-2"], vpi_jahresdurchschnitte={
            2023: VpiWert(wert="100", quelle="test", datum="2024-01-01"),
            2024: VpiWert(wert="102", quelle="test", datum="2025-01-01"),
            2025: VpiWert(wert="104", quelle="test", datum="2026-01-01"),
        },
        vertraglich_zulaessiger_betrag_cent=200_000, vertraglicher_quellenbeleg="Punkt 5",
        vertraglicher_fruehestmoeglicher_termin=date(2026, 4, 1), aktuell_verrechneter_betrag_cent=110_000,
        aktuell_verrechnet_quellenbeleg="Test", aktuell_verrechnet_stichtag=date(2026, 9, 1),
        zustellnachweis_referenz=None, kommentar=None, akteur="test",
    )
    komponenten = [stammdaten_repo.get_komponente("K-1"), stammdaten_repo.get_komponente("K-2")]
    schreiben = outbox_service.erstellen_aus_mieweg(
        ctx=admin_ctx, vertrag=vertrag, profil=profil, vorschau=vorschau, ziel_bewertungsjahr=2026,
        massgeblicher_termin=date(2026, 4, 1), erhoehung_cent=1000, aktuell_verrechnet_cent=110_000,
        referenzierte_komponenten=komponenten, unveraenderte_komponenten=[], akteur="test",
    )
    assert schreiben.status == "BLOCKIERT"
    assert any("Mehr" in g for g in schreiben.blockiert_gruende)
