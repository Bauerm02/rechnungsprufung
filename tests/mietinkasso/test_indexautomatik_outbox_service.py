from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from mietinkasso.domain.exceptions import QuellenbelegFehltError
from mietinkasso.index.repository import IndexRepository
from mietinkasso.indexautomatik.outbox_service import ErhoehungsschreibenOutboxService
from mietinkasso.indexautomatik.rechtsprofil import RechtsprofilService
from mietinkasso.indexautomatik.repository import ErhoehungsschreibenRepository, RechtsprofilRepository
from mietinkasso.indexautomatik.transport import FakeTransportadapter
from mietinkasso.infrastructure.db.tables import ErhoehungsschreibenTable


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
def outbox_service(outbox_repo, stammdaten_repo, rechtsprofil_repo, rechtsprofil_service) -> ErhoehungsschreibenOutboxService:
    return ErhoehungsschreibenOutboxService(
        outbox_repo, stammdaten_repo, rechtsprofil_repo, rechtsprofil_service, jlb_signatur="JLB Projects GmbH"
    )


def _freigegebenes_profil(admin_ctx, rechtsprofil_service, stammdaten_repo, vertrag, komponente_id="K-1"):
    stammdaten_repo.add_komponente(
        id=komponente_id, vertrag_id=vertrag.id, art="HMZ", bezeichnung="Hauptmietzins", betrag_cent=100_000,
        indexierbar=True, gueltig_von=date(2024, 1, 1),
    )
    profil = rechtsprofil_service.entwurf_anlegen(
        ctx=admin_ctx, vertrag_id=vertrag.id, rechtsordnung="OESTERREICH_MRG_VOLL", ist_wohnungsnutzung=True,
        mrg_zinsbeschraenkung=False, mrg_zinsbeschraenkung_geprueft=True, ist_altvertrag=False, ist_hauptmiete=True, foerderbindung=False, foerderbindung_geprueft=True,
        mietzinsobergrenze_cent=None, mietzinsobergrenze_quellenbeleg=None, mietzinsobergrenze_gueltig_bis=None,
        bezugsjahr=2024, bezugsmonat=1, letzte_basis_war_jahresdurchschnitt=False, basis_komponenten_ids=[komponente_id],
        vertraglich_zulaessiger_betrag_cent=200_000, vertraglicher_quellenbeleg="Punkt 5",
        vertraglicher_fruehestmoeglicher_termin=date(2026, 4, 1), vertrag_beleg_referenz="Vertrag", klausel_referenz=None,
        erstellt_von="markus",
    )
    return rechtsprofil_service.freigeben(profil.id, ctx=admin_ctx, freigegeben_von="markus")


def _bereites_schreiben(
    admin_ctx, outbox_repo, rechtsprofil_service, stammdaten_repo, vertrag, *, massgeblicher_termin=date(2026, 4, 1)
) -> ErhoehungsschreibenTable:
    profil = _freigegebenes_profil(admin_ctx, rechtsprofil_service, stammdaten_repo, vertrag)
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
            mieweg_vorschau_id=None, status="BEREIT", massgeblicher_termin=massgeblicher_termin, erhoehung_cent=1000,
            schreiben_text="Testschreiben", idempotenzschluessel=f"{vertrag.id}:2026", empfaenger_snapshot=empfaenger_snapshot,
        )
    )


def test_versand_deaktiviert_bleibt_bereit_kein_realer_versand(admin_ctx, basis_vertrag, outbox_service, outbox_repo, rechtsprofil_service, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    schreiben = _bereites_schreiben(admin_ctx, outbox_repo, rechtsprofil_service, stammdaten_repo, vertrag)
    transport = FakeTransportadapter()
    ergebnis = outbox_service.versenden(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 9, 1), send_enabled=False,
        mailops_allowlist_bestaetigt=False, transport=transport,
    )
    assert ergebnis.status == "BEREITS_VERARBEITET"
    assert transport.aufrufe == []
    assert outbox_repo.get(schreiben.id).status == "BEREIT"


def test_beide_flags_noetig_fuer_realen_versand(admin_ctx, basis_vertrag, outbox_service, outbox_repo, rechtsprofil_service, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    schreiben = _bereites_schreiben(admin_ctx, outbox_repo, rechtsprofil_service, stammdaten_repo, vertrag)
    transport = FakeTransportadapter()
    ergebnis = outbox_service.versenden(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 9, 1), send_enabled=True,
        mailops_allowlist_bestaetigt=False, transport=transport,
    )
    assert ergebnis.status == "BEREITS_VERARBEITET"
    assert transport.aufrufe == []


def test_erfolgreicher_versand_setzt_gesendet(admin_ctx, basis_vertrag, outbox_service, outbox_repo, rechtsprofil_service, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    schreiben = _bereites_schreiben(admin_ctx, outbox_repo, rechtsprofil_service, stammdaten_repo, vertrag)
    transport = FakeTransportadapter()
    ergebnis = outbox_service.versenden(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 9, 1), send_enabled=True,
        mailops_allowlist_bestaetigt=True, transport=transport,
    )
    assert ergebnis.status == "GESENDET"
    assert len(transport.aufrufe) == 1
    aktualisiert = outbox_repo.get(schreiben.id)
    assert aktualisiert.status == "GESENDET"
    assert aktualisiert.versendet_am is not None


def test_versand_vor_wirksamkeitstermin_wird_blockiert(admin_ctx, basis_vertrag, outbox_service, outbox_repo, rechtsprofil_service, stammdaten_repo):
    """Unabhängiger Review (fd8c2b2-Folgereview): "massgeblicher_termin
    darf nicht in Zukunft liegen"."""

    vertrag, _konto = basis_vertrag
    schreiben = _bereites_schreiben(admin_ctx, outbox_repo, rechtsprofil_service, stammdaten_repo, vertrag, massgeblicher_termin=date(2026, 4, 1))
    ergebnis = outbox_service.versenden(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 3, 1), send_enabled=True,
        mailops_allowlist_bestaetigt=True, transport=FakeTransportadapter(),
    )
    assert ergebnis.status == "BLOCKIERT"
    assert outbox_repo.get(schreiben.id).status == "BLOCKIERT"


def test_invalidiertes_rechtsprofil_blockiert_versand(admin_ctx, basis_vertrag, outbox_service, outbox_repo, rechtsprofil_service, rechtsprofil_repo, stammdaten_repo):
    """Unabhängiger Review (fd8c2b2-Folgereview): "keine aktuelle Profil-/
    Komponenten-/VPI-Quellenprüfung" beim Versand."""

    vertrag, _konto = basis_vertrag
    schreiben = _bereites_schreiben(admin_ctx, outbox_repo, rechtsprofil_service, stammdaten_repo, vertrag)
    rechtsprofil_repo.invalidieren(schreiben.rechtsprofil_id)

    ergebnis = outbox_service.versenden(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 9, 1), send_enabled=True,
        mailops_allowlist_bestaetigt=True, transport=FakeTransportadapter(),
    )
    assert ergebnis.status == "BLOCKIERT"


def test_provider_timeout_setzt_unklar_kein_blinder_retry(admin_ctx, basis_vertrag, outbox_service, outbox_repo, rechtsprofil_service, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    schreiben = _bereites_schreiben(admin_ctx, outbox_repo, rechtsprofil_service, stammdaten_repo, vertrag)
    transport = FakeTransportadapter(verhalten="TIMEOUT")
    ergebnis = outbox_service.versenden(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 9, 1), send_enabled=True,
        mailops_allowlist_bestaetigt=True, transport=transport,
    )
    assert ergebnis.status == "UNKLAR"
    assert outbox_repo.get(schreiben.id).status == "UNKLAR"
    zweiter = outbox_service.versenden(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 9, 1), send_enabled=True,
        mailops_allowlist_bestaetigt=True, transport=transport,
    )
    assert zweiter.status == "BEREITS_VERARBEITET"
    assert len(transport.aufrufe) == 1


def test_geaenderter_empfaenger_blockiert_versand(admin_ctx, basis_vertrag, outbox_service, outbox_repo, rechtsprofil_service, stammdaten_repo):
    """"abgelaufener Vertrag und geänderter Empfänger": eine seit der
    Entwurfserstellung geänderte Adresse stoppt den Versand."""

    vertrag, _konto = basis_vertrag
    schreiben = _bereites_schreiben(admin_ctx, outbox_repo, rechtsprofil_service, stammdaten_repo, vertrag)
    debitor = stammdaten_repo.get_debitor(vertrag.debitor_id)
    stammdaten_repo.upsert_debitor(id=debitor.id, name=debitor.name, email=debitor.email, adresse="Andere Straße 5, 1020 Wien")

    ergebnis = outbox_service.versenden(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 9, 1), send_enabled=True,
        mailops_allowlist_bestaetigt=True, transport=FakeTransportadapter(),
    )
    assert ergebnis.status == "BLOCKIERT"
    assert outbox_repo.get(schreiben.id).status == "BLOCKIERT"


def test_abgelaufener_vertrag_blockiert_versand(admin_ctx, basis_vertrag, outbox_service, outbox_repo, rechtsprofil_service, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    schreiben = _bereites_schreiben(admin_ctx, outbox_repo, rechtsprofil_service, stammdaten_repo, vertrag)
    stammdaten_repo.upsert_vertrag(
        id=vertrag.id, einheit_id=vertrag.einheit_id, debitor_id=vertrag.debitor_id, gesellschaft_id=vertrag.gesellschaft_id,
        rechtsordnung=vertrag.rechtsordnung, gueltig_von=vertrag.gueltig_von, gueltig_bis=date(2026, 5, 1),
    )
    ergebnis = outbox_service.versenden(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 9, 1), send_enabled=True,
        mailops_allowlist_bestaetigt=True, transport=FakeTransportadapter(),
    )
    assert ergebnis.status == "BLOCKIERT"


def test_ausgeschlossenes_objekt_blockiert_versand(admin_ctx, basis_vertrag, outbox_service, outbox_repo, rechtsprofil_service, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    schreiben = _bereites_schreiben(admin_ctx, outbox_repo, rechtsprofil_service, stammdaten_repo, vertrag)
    stammdaten_repo.upsert_objekt(id="601", gesellschaft_id="7DI", bezeichnung="Am Corso", ausgeschlossen=True)

    ergebnis = outbox_service.versenden(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 9, 1), send_enabled=True,
        mailops_allowlist_bestaetigt=True, transport=FakeTransportadapter(),
    )
    assert ergebnis.status == "BLOCKIERT"


def test_zugang_email_unbestaetigt_wird_abgelehnt(admin_ctx, basis_vertrag, outbox_service, outbox_repo, rechtsprofil_service, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    schreiben = _bereites_schreiben(admin_ctx, outbox_repo, rechtsprofil_service, stammdaten_repo, vertrag)
    outbox_service.versenden(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 9, 1), send_enabled=True,
        mailops_allowlist_bestaetigt=True, transport=FakeTransportadapter(),
    )
    with pytest.raises(ValueError):
        outbox_service.zugang_bestaetigen(
            ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 9, 10), zugang_datum=date(2026, 9, 2),
            zugangsform="EMAIL_UNBESTAETIGT", zugang_beleg="Versandprotokoll",
        )
    assert outbox_repo.get(schreiben.id).status == "GESENDET"


def test_zugang_ohne_beleg_wird_abgelehnt(admin_ctx, basis_vertrag, outbox_service, outbox_repo, rechtsprofil_service, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    schreiben = _bereites_schreiben(admin_ctx, outbox_repo, rechtsprofil_service, stammdaten_repo, vertrag)
    outbox_service.versenden(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 9, 1), send_enabled=True,
        mailops_allowlist_bestaetigt=True, transport=FakeTransportadapter(),
    )
    with pytest.raises(QuellenbelegFehltError):
        outbox_service.zugang_bestaetigen(
            ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 9, 10), zugang_datum=date(2026, 9, 2),
            zugangsform="EINSCHREIBEN_RUECKSCHEIN", zugang_beleg="",
        )


def test_zugang_in_der_zukunft_wird_abgelehnt(admin_ctx, basis_vertrag, outbox_service, outbox_repo, rechtsprofil_service, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    schreiben = _bereites_schreiben(admin_ctx, outbox_repo, rechtsprofil_service, stammdaten_repo, vertrag)
    outbox_service.versenden(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 9, 1), send_enabled=True,
        mailops_allowlist_bestaetigt=True, transport=FakeTransportadapter(),
    )
    with pytest.raises(ValueError):
        outbox_service.zugang_bestaetigen(
            ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 9, 2), zugang_datum=date(2026, 9, 10),
            zugangsform="EINSCHREIBEN_RUECKSCHEIN", zugang_beleg="Rückschein",
        )


def test_zugang_vor_versanddatum_wird_abgelehnt(admin_ctx, basis_vertrag, outbox_service, outbox_repo, rechtsprofil_service, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    schreiben = _bereites_schreiben(admin_ctx, outbox_repo, rechtsprofil_service, stammdaten_repo, vertrag)
    outbox_service.versenden(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 9, 5), send_enabled=True,
        mailops_allowlist_bestaetigt=True,
        transport=FakeTransportadapter(versendet_am=datetime(2026, 9, 5, 22, 30, tzinfo=timezone.utc)),
    )
    with pytest.raises(ValueError):
        outbox_service.zugang_bestaetigen(
            ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 9, 10), zugang_datum=date(2026, 9, 1),
            zugangsform="EINSCHREIBEN_RUECKSCHEIN", zugang_beleg="Rückschein",
        )


def test_ausreichender_zugang_berechnet_zahlungspflicht_und_ausfuehrung(admin_ctx, basis_vertrag, outbox_service, outbox_repo, rechtsprofil_service, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    schreiben = _bereites_schreiben(admin_ctx, outbox_repo, rechtsprofil_service, stammdaten_repo, vertrag)
    outbox_service.versenden(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 4, 1), send_enabled=True,
        mailops_allowlist_bestaetigt=True, transport=FakeTransportadapter(),
    )
    aktualisiert = outbox_service.zugang_bestaetigen(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 4, 5), zugang_datum=date(2026, 4, 1),
        zugangsform="EINSCHREIBEN_RUECKSCHEIN", zugang_beleg="Rückschein Nr. 123",
    )
    assert aktualisiert.status == "ZUGANG_BESTAETIGT"
    # faelligkeit_tag=5 (Default), 14 Tage nach 1.4. = 15.4. -> naechster Zinstermin 5.5.
    assert aktualisiert.zahlungspflicht_ab == date(2026, 5, 5)

    vor_faelligkeit = outbox_service.taegliche_pflege(heute=date(2026, 5, 1))
    assert vor_faelligkeit == []
    nach_faelligkeit = outbox_service.taegliche_pflege(heute=date(2026, 5, 5))
    assert len(nach_faelligkeit) == 1
    assert outbox_repo.get(schreiben.id).status == "SOLL_UMSETZUNG_OFFEN"


def test_zugangsfrist_fuer_nicht_unterstuetzte_rechtsordnung_wird_gesperrt(admin_ctx, basis_vertrag, outbox_service, outbox_repo, rechtsprofil_service, rechtsprofil_repo, stammdaten_repo):
    """Unabhängiger Review (fd8c2b2-Folgereview): "Ungeklärte Zustell-/
    Fristenlage intern sperren, nicht als Warntext an Mieter schicken" -
    für eine nicht unterstützte Rechtsordnung wird KEIN geratener
    Zahlungspflicht-Termin berechnet."""

    vertrag, _konto = basis_vertrag
    schreiben = _bereites_schreiben(admin_ctx, outbox_repo, rechtsprofil_service, stammdaten_repo, vertrag)
    profil = rechtsprofil_repo.get(schreiben.rechtsprofil_id)
    with stammdaten_repo._session_factory() as session:
        from mietinkasso.infrastructure.db.tables import RechtsprofilTable

        row = session.get(RechtsprofilTable, profil.id)
        row.rechtsordnung = "OESTERREICH_GEWERBE"
        session.commit()

    outbox_service.versenden(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 3, 1), send_enabled=True,
        mailops_allowlist_bestaetigt=True, transport=FakeTransportadapter(),
    )
    with pytest.raises(ValueError):
        outbox_service.zugang_bestaetigen(
            ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 3, 5), zugang_datum=date(2026, 3, 1),
            zugangsform="EINSCHREIBEN_RUECKSCHEIN", zugang_beleg="Rückschein",
        )


def test_zugangsfrist_konfiguriertes_fristenprofil_hebt_sperre_gezielt_auf(
    admin_ctx, basis_vertrag, outbox_service, outbox_repo, rechtsprofil_service, rechtsprofil_repo, stammdaten_repo
):
    """Auftrag HV-20260913-VERSAND-SOLL, Punkt 2: ein belegtes, geprüftes
    Fristenprofil (frist_tage_zugang_bis_wirksamkeit/frist_quellenbeleg)
    hebt die pauschale Sperre GEZIELT für dieses Rechtsprofil auf - auch
    für eine sonst nicht unterstützte Rechtsordnung (Gewerbe-/
    Jännerklausel) - ohne das bisherige Verhalten für unveränderte
    Fälle (siehe `test_zugangsfrist_fuer_nicht_unterstuetzte_
    rechtsordnung_wird_gesperrt`) zu berühren."""

    vertrag, _konto = basis_vertrag
    stammdaten_repo.add_komponente(
        id="K-1", vertrag_id=vertrag.id, art="HMZ", bezeichnung="Hauptmietzins", betrag_cent=100_000,
        indexierbar=True, gueltig_von=date(2024, 1, 1),
    )
    entwurf = rechtsprofil_service.entwurf_anlegen(
        ctx=admin_ctx, vertrag_id=vertrag.id, rechtsordnung="OESTERREICH_GEWERBE", ist_wohnungsnutzung=False,
        mrg_zinsbeschraenkung=False, mrg_zinsbeschraenkung_geprueft=True, ist_altvertrag=False, ist_hauptmiete=None, foerderbindung=False, foerderbindung_geprueft=True,
        mietzinsobergrenze_cent=None, mietzinsobergrenze_quellenbeleg=None, mietzinsobergrenze_gueltig_bis=None,
        bezugsjahr=2024, bezugsmonat=1, letzte_basis_war_jahresdurchschnitt=False, basis_komponenten_ids=["K-1"],
        vertraglich_zulaessiger_betrag_cent=200_000, vertraglicher_quellenbeleg="Punkt 5",
        vertraglicher_fruehestmoeglicher_termin=date(2026, 4, 1), vertrag_beleg_referenz="Vertrag", klausel_referenz=None,
        erstellt_von="markus", frist_tage_zugang_bis_wirksamkeit=30,
        frist_quellenbeleg="Vertrag Punkt 9, belegte Gewerbeklausel",
    )
    profil = rechtsprofil_service.freigeben(entwurf.id, ctx=admin_ctx, freigegeben_von="markus")
    debitor = stammdaten_repo.get_debitor(vertrag.debitor_id)
    stammdaten_repo.upsert_debitor(id=debitor.id, name=debitor.name, email=debitor.email, adresse="Corsogasse 1/3, 1010 Wien")
    vertrag = stammdaten_repo.get_vertrag(vertrag.id)
    debitor = stammdaten_repo.get_debitor(vertrag.debitor_id)
    schreiben = outbox_repo.anlegen(
        ErhoehungsschreibenTable(
            vertrag_id=vertrag.id, ziel_bewertungsjahr=2026, rechtsprofil_id=profil.id, rechtsprofil_version=profil.version,
            status="BEREIT", massgeblicher_termin=date(2026, 3, 1), erhoehung_cent=1000,
            schreiben_text="Testschreiben", idempotenzschluessel=f"{vertrag.id}:2026",
            empfaenger_snapshot={
                "debitor_id": vertrag.debitor_id, "name": debitor.name, "adresse": debitor.adresse, "email": debitor.email,
                "vertrag_gueltig_bis": vertrag.gueltig_bis.isoformat() if vertrag.gueltig_bis else None,
                "vertrag_rechtsordnung": vertrag.rechtsordnung,
                "komponenten_snapshot": [],
            },
        )
    )

    outbox_service.versenden(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 3, 1), send_enabled=True,
        mailops_allowlist_bestaetigt=True, transport=FakeTransportadapter(),
    )
    aktualisiert = outbox_service.zugang_bestaetigen(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 3, 5), zugang_datum=date(2026, 3, 1),
        zugangsform="EINSCHREIBEN_RUECKSCHEIN", zugang_beleg="Rückschein",
    )
    assert aktualisiert.status == "ZUGANG_BESTAETIGT"
    # 30 Tage nach 1.3. = 31.3., faelligkeit_tag=5 -> naechster Zinstermin 5.4.
    assert aktualisiert.zahlungspflicht_ab == date(2026, 4, 5)


def test_zugangsfrist_konfiguriert_ohne_quellenbeleg_wird_gesperrt(
    admin_ctx, basis_vertrag, outbox_service, outbox_repo, rechtsprofil_service, rechtsprofil_repo, stammdaten_repo
):
    """Eine konfigurierte Frist OHNE Beleg wird NIE stillschweigend
    angewendet - lieber intern sperren als eine unbelegte Frist an den
    Mieter zu kommunizieren."""

    vertrag, _konto = basis_vertrag
    schreiben = _bereites_schreiben(admin_ctx, outbox_repo, rechtsprofil_service, stammdaten_repo, vertrag)
    profil = rechtsprofil_repo.get(schreiben.rechtsprofil_id)
    with stammdaten_repo._session_factory() as session:
        from mietinkasso.infrastructure.db.tables import RechtsprofilTable

        row = session.get(RechtsprofilTable, profil.id)
        row.frist_tage_zugang_bis_wirksamkeit = 30
        row.frist_quellenbeleg = None
        session.commit()

    outbox_service.versenden(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 3, 1), send_enabled=True,
        mailops_allowlist_bestaetigt=True, transport=FakeTransportadapter(),
    )
    with pytest.raises(ValueError):
        outbox_service.zugang_bestaetigen(
            ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 3, 5), zugang_datum=date(2026, 3, 1),
            zugangsform="EINSCHREIBEN_RUECKSCHEIN", zugang_beleg="Rückschein",
        )


def test_mrg_teil_wird_bereits_vor_versand_gesperrt(
    admin_ctx, basis_vertrag, outbox_service, outbox_repo, rechtsprofil_service, rechtsprofil_repo, stammdaten_repo
):
    """Ergänzende Abnahmepunkte (Endprüfung): "MRG-Teil/sonstige
    ungeklärte Fristen VOR Versand blockieren; ein Mieterschreiben
    'Frist ist gesondert zu prüfen' darf niemals automatisch
    herausgehen" - MRG_TEIL ist NICHT (mehr) in
    _ZUGANGSFRIST_UNTERSTUETZTE_RECHTSORDNUNGEN und muss daher schon
    beim Versandversuch blockiert werden, nicht erst bei der späteren
    Zugangsbestätigung."""

    vertrag, _konto = basis_vertrag
    schreiben = _bereites_schreiben(admin_ctx, outbox_repo, rechtsprofil_service, stammdaten_repo, vertrag)
    profil = rechtsprofil_repo.get(schreiben.rechtsprofil_id)
    with stammdaten_repo._session_factory() as session:
        from mietinkasso.infrastructure.db.tables import RechtsprofilTable

        row = session.get(RechtsprofilTable, profil.id)
        row.rechtsordnung = "OESTERREICH_MRG_TEIL"
        session.commit()

    transport = FakeTransportadapter()
    ergebnis = outbox_service.versenden(
        ctx=admin_ctx, erhoehungsschreiben_id=schreiben.id, heute=date(2026, 9, 1), send_enabled=True,
        mailops_allowlist_bestaetigt=True, transport=transport,
    )
    assert ergebnis.status == "BLOCKIERT"
    assert transport.aufrufe == []
    assert outbox_repo.get(schreiben.id).status == "BLOCKIERT"


def test_verwaiste_in_versand_werden_markiert(admin_ctx, basis_vertrag, outbox_service, outbox_repo, rechtsprofil_service, stammdaten_repo):
    vertrag, _konto = basis_vertrag
    schreiben = _bereites_schreiben(admin_ctx, outbox_repo, rechtsprofil_service, stammdaten_repo, vertrag)
    alt = datetime.now(timezone.utc) - timedelta(minutes=30)
    outbox_repo.claim_fuer_versand(schreiben.id, jetzt=alt)
    verwaiste = outbox_service.markiere_verwaiste_als_unklar()
    assert [r.id for r in verwaiste] == [schreiben.id]
    assert outbox_repo.get(schreiben.id).status == "UNKLAR"


def test_mehrkomponenten_werden_centgenau_verteilt(admin_ctx, basis_vertrag, outbox_service, rechtsprofil_service, stammdaten_repo):
    """Codex-Rückprüfung (499c36f/8f499c9): "Mehrkomponenten bleibt komplett
    gesperrt, obwohl HMZ+Küche beauftragt - explizite centgenaue Verteilung
    implementieren". Zwei referenzierte Komponenten (HMZ 100.000 Cent,
    Küche 10.000 Cent) mit Gesamterhöhung 1000 Cent werden proportional zum
    jeweiligen Anteil verteilt (größte-Rest-Verfahren): HMZ 909/110.000-tel,
    Küche 91/110.000-tel - Summe exakt 1000 Cent, kein Rundungsverlust."""
    from mietinkasso.mieweg_vorschau.repository import MieWegVorschauRepository
    from mietinkasso.mieweg_vorschau.service import MieWegVorschauService, VpiWert

    vertrag, _konto = basis_vertrag
    stammdaten_repo.add_komponente(id="K-1", vertrag_id=vertrag.id, art="HMZ", bezeichnung="HMZ", betrag_cent=100_000, indexierbar=True, gueltig_von=date(2024, 1, 1))
    stammdaten_repo.add_komponente(id="K-2", vertrag_id=vertrag.id, art="KUECHE", bezeichnung="Küche", betrag_cent=10_000, indexierbar=True, gueltig_von=date(2024, 1, 1))

    profil = rechtsprofil_service.entwurf_anlegen(
        ctx=admin_ctx, vertrag_id=vertrag.id, rechtsordnung="OESTERREICH_MRG_VOLL", ist_wohnungsnutzung=True,
        mrg_zinsbeschraenkung=False, mrg_zinsbeschraenkung_geprueft=True, ist_altvertrag=False, ist_hauptmiete=True, foerderbindung=False, foerderbindung_geprueft=True,
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
    # Ohne belegte Postadresse bleibt das Schreiben BLOCKIERT - Mehrkomponenten
    # selbst ist aber kein Blockiergrund mehr.
    assert schreiben.status == "BLOCKIERT"
    assert not any("Mehr" in g for g in schreiben.blockiert_gruende)
    eintraege = {e["komponente_id"]: e for e in schreiben.komponenten_verteilung["eintraege"]}
    assert eintraege["K-1"] == {"komponente_id": "K-1", "alter_betrag_cent": 100_000, "neuer_betrag_cent": 100_909}
    assert eintraege["K-2"] == {"komponente_id": "K-2", "alter_betrag_cent": 10_000, "neuer_betrag_cent": 10_091}
    summe_delta = sum(e["neuer_betrag_cent"] - e["alter_betrag_cent"] for e in eintraege.values())
    assert summe_delta == 1000
    assert "HMZ" in schreiben.schreiben_text and "Küche" in schreiben.schreiben_text

    # Nach behobener Quelle (hier: Postadresse ergänzt) muss ein Retry über
    # `bestehende_id` die Verteilung neu berechnen und BEREIT setzen.
    debitor = stammdaten_repo.get_debitor(vertrag.debitor_id)
    stammdaten_repo.upsert_debitor(id=debitor.id, name=debitor.name, email=debitor.email, adresse="Corsogasse 1/3, 1010 Wien")
    komponenten_aktuell = [stammdaten_repo.get_komponente("K-1"), stammdaten_repo.get_komponente("K-2")]
    erneuert = outbox_service.erstellen_aus_mieweg(
        ctx=admin_ctx, vertrag=vertrag, profil=profil, vorschau=vorschau, ziel_bewertungsjahr=2026,
        massgeblicher_termin=date(2026, 4, 1), erhoehung_cent=1000, aktuell_verrechnet_cent=110_000,
        referenzierte_komponenten=komponenten_aktuell, unveraenderte_komponenten=[], akteur="test",
        bestehende_id=schreiben.id,
    )
    assert erneuert.id == schreiben.id
    assert erneuert.status == "BEREIT"
    eintraege_erneuert = {e["komponente_id"]: e for e in erneuert.komponenten_verteilung["eintraege"]}
    assert eintraege_erneuert["K-1"]["neuer_betrag_cent"] == 100_909
    assert eintraege_erneuert["K-2"]["neuer_betrag_cent"] == 10_091


def test_erstellen_aus_mieweg_befuellt_komponenten_verteilung_bei_genau_einer_komponente(
    admin_ctx, basis_vertrag, outbox_service, rechtsprofil_service, stammdaten_repo
):
    """Auftrag HV-20260913-VERSAND-SOLL, Punkt 1: `komponenten_verteilung`
    ist die centgenaue Grundlage für die spätere Soll-Umsetzung
    (`umsetzung_service.py`) - befüllt GENAU DANN, wenn (wie bei jedem
    nicht blockierten Schreiben) exakt eine Komponente referenziert
    ist."""

    from mietinkasso.mieweg_vorschau.repository import MieWegVorschauRepository
    from mietinkasso.mieweg_vorschau.service import MieWegVorschauService, VpiWert

    vertrag, _konto = basis_vertrag
    stammdaten_repo.add_komponente(id="K-1", vertrag_id=vertrag.id, art="HMZ", bezeichnung="HMZ", betrag_cent=100_000, indexierbar=True, gueltig_von=date(2024, 1, 1))

    profil = rechtsprofil_service.entwurf_anlegen(
        ctx=admin_ctx, vertrag_id=vertrag.id, rechtsordnung="OESTERREICH_MRG_VOLL", ist_wohnungsnutzung=True,
        mrg_zinsbeschraenkung=False, mrg_zinsbeschraenkung_geprueft=True, ist_altvertrag=False, ist_hauptmiete=True, foerderbindung=False, foerderbindung_geprueft=True,
        mietzinsobergrenze_cent=None, mietzinsobergrenze_quellenbeleg=None, mietzinsobergrenze_gueltig_bis=None,
        bezugsjahr=2024, bezugsmonat=1, letzte_basis_war_jahresdurchschnitt=False, basis_komponenten_ids=["K-1"],
        vertraglich_zulaessiger_betrag_cent=200_000, vertraglicher_quellenbeleg="Punkt 5",
        vertraglicher_fruehestmoeglicher_termin=date(2026, 4, 1), vertrag_beleg_referenz="Vertrag", klausel_referenz=None,
        erstellt_von="markus",
    )
    profil = rechtsprofil_service.freigeben(profil.id, ctx=admin_ctx, freigegeben_von="markus")

    mieweg_service = MieWegVorschauService(MieWegVorschauRepository(stammdaten_repo._session_factory), stammdaten_repo)
    vorschau = mieweg_service.vorschau_erstellen(
        ctx=admin_ctx, vertrag=vertrag, rechtsordnung="OESTERREICH_MRG_VOLL", ist_wohnungsnutzung=True,
        mrg_zinsbeschraenkung=False, ist_altvertrag=False, bezugsjahr=2024, bezugsmonat=1,
        letzte_basis_war_jahresdurchschnitt=False, ziel_bewertungsjahr=2026, basis_betrag_cent=100_000,
        basis_komponenten_ids=["K-1"], vpi_jahresdurchschnitte={
            2023: VpiWert(wert="100", quelle="test", datum="2024-01-01"),
            2024: VpiWert(wert="102", quelle="test", datum="2025-01-01"),
            2025: VpiWert(wert="104", quelle="test", datum="2026-01-01"),
        },
        vertraglich_zulaessiger_betrag_cent=200_000, vertraglicher_quellenbeleg="Punkt 5",
        vertraglicher_fruehestmoeglicher_termin=date(2026, 4, 1), aktuell_verrechneter_betrag_cent=100_000,
        aktuell_verrechnet_quellenbeleg="Test", aktuell_verrechnet_stichtag=date(2026, 9, 1),
        zustellnachweis_referenz=None, kommentar=None, akteur="test",
    )
    komponente = stammdaten_repo.get_komponente("K-1")
    schreiben = outbox_service.erstellen_aus_mieweg(
        ctx=admin_ctx, vertrag=vertrag, profil=profil, vorschau=vorschau, ziel_bewertungsjahr=2026,
        massgeblicher_termin=date(2026, 4, 1), erhoehung_cent=1000, aktuell_verrechnet_cent=100_000,
        referenzierte_komponenten=[komponente], unveraenderte_komponenten=[], akteur="test",
    )
    assert schreiben.komponenten_verteilung == {
        "eintraege": [{"komponente_id": "K-1", "alter_betrag_cent": 100_000, "neuer_betrag_cent": 101_000}]
    }
