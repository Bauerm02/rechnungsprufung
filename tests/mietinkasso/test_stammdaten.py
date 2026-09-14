from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from mietinkasso.domain.enums import Nutzungsstatus, OPTyp
from mietinkasso.stammdaten.service import ObjektAusgeschlossenError, StammdatenService


def test_upsert_debitor_setzt_postadresse_geprueft_bei_echter_adressaenderung_zurueck(stammdaten_repo):
    """Unabhängige Rückprüfung 14.09.2026, echter Bug: `postadresse_
    geprueft=True` blieb auch dann bestehen, wenn sich `adresse` selbst
    tatsächlich geändert hat, ohne dass eine neue Prüfung explizit
    übergeben wurde - eine frühere Prüfung gilt aber nicht für eine
    GENUIN ANDERE, seither nie geprüfte Adresse."""

    stammdaten_repo.upsert_debitor(
        id="DEB-ADR-1", name="Erika Mieterin", email="erika@example.at",
        adresse="Alte Gasse 1, 1010 Wien", postadresse_geprueft=True,
    )
    assert stammdaten_repo.get_debitor("DEB-ADR-1").postadresse_geprueft is True

    # Tatsächliche Adressänderung OHNE erneute explizite Prüfung.
    stammdaten_repo.upsert_debitor(
        id="DEB-ADR-1", name="Erika Mieterin", email="erika@example.at",
        adresse="Neue Gasse 2, 1020 Wien",
    )
    aktualisiert = stammdaten_repo.get_debitor("DEB-ADR-1")
    assert aktualisiert.adresse == "Neue Gasse 2, 1020 Wien"
    assert aktualisiert.postadresse_geprueft is False


def test_upsert_debitor_erhaelt_postadresse_geprueft_bei_reinem_email_update(stammdaten_repo):
    """Gegenprobe: ein routinemäßiges Update anderer Felder (E-Mail) bei
    UNVERÄNDERTER Adresse darf die einmal erteilte Prüfung weiterhin
    NICHT stillschweigend zurücksetzen."""

    stammdaten_repo.upsert_debitor(
        id="DEB-ADR-2", name="Max Mieter", email="max-alt@example.at",
        adresse="Immergleiche Gasse 3, 1030 Wien", postadresse_geprueft=True,
    )
    stammdaten_repo.upsert_debitor(
        id="DEB-ADR-2", name="Max Mieter", email="max-neu@example.at",
        adresse="Immergleiche Gasse 3, 1030 Wien",
    )
    aktualisiert = stammdaten_repo.get_debitor("DEB-ADR-2")
    assert aktualisiert.email == "max-neu@example.at"
    assert aktualisiert.postadresse_geprueft is True

    # Explizites Zurücksetzen bleibt weiterhin über den Parameter möglich.
    stammdaten_repo.upsert_debitor(
        id="DEB-ADR-2", name="Max Mieter", email="max-neu@example.at",
        adresse="Immergleiche Gasse 3, 1030 Wien", postadresse_geprueft=False,
    )
    assert stammdaten_repo.get_debitor("DEB-ADR-2").postadresse_geprueft is False


def test_kaution_ist_strukturell_von_op_getrennt(stammdaten_repo, op_service, basis_vertrag, ctx_factory):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    stammdaten_repo.set_kaution(id="KAU-1", vertrag_id=vertrag.id, betrag_cent=150_000, stichtag=date(2026, 8, 31))

    op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=200_000,
        belegdatum=date(2026, 4, 1), buchungsdatum=date(2026, 4, 1),
        faelligkeit=date(2026, 4, 5), beleg_referenz="Hoher Mietrückstand",
    )
    kaution = stammdaten_repo.get_kaution(vertrag.id)
    assert kaution.betrag_cent == 150_000  # unverändert, nie automatisch verrechnet

    saldo = op_service.berechne_saldo(konto.id)
    assert saldo.saldo_cent == 200_000  # Kaution taucht im OP-Saldo nicht auf


def test_selfstorage_und_kurzzeit_status_bleiben_erhalten_bei_soll_null(stammdaten_repo, op_service, basis_vertrag):
    vertrag, konto = basis_vertrag
    stammdaten_repo.upsert_einheit(
        id="601-SELF1", objekt_id="601", bezeichnung="Selfstorage 1", nutzungsstatus=Nutzungsstatus.SELFSTORAGE.value
    )
    # Saldo 0 (kein Soll gebucht) darf den Status NICHT automatisch auf Leerstand setzen.
    saldo = op_service.berechne_saldo(konto.id)
    assert saldo.saldo_cent == 0
    einheit = stammdaten_repo.get_einheit("601-SELF1")
    assert einheit.nutzungsstatus == Nutzungsstatus.SELFSTORAGE.value

    stammdaten_repo.upsert_einheit(
        id="601-KURZ1", objekt_id="601", bezeichnung="Kurzzeit 1", nutzungsstatus=Nutzungsstatus.KURZZEITVERMIETUNG.value
    )
    einheit_kurz = stammdaten_repo.get_einheit("601-KURZ1")
    assert einheit_kurz.nutzungsstatus == Nutzungsstatus.KURZZEITVERMIETUNG.value


def test_objekt_107_ist_von_der_pilotphase_ausgeschlossen(stammdaten_repo):
    stammdaten_repo.upsert_gesellschaft(id="7DI", name="7D Immobilien GmbH")
    stammdaten_repo.upsert_objekt(id="107", gesellschaft_id="7DI", bezeichnung="Sieben Dörfer", ausgeschlossen=True)
    stammdaten_repo.upsert_objekt(id="601", gesellschaft_id="7DI", bezeichnung="Am Corso", ausgeschlossen=False)
    service = StammdatenService(stammdaten_repo)

    with pytest.raises(ObjektAusgeschlossenError):
        service.pruefe_objekt_erlaubt("107")

    service.pruefe_objekt_erlaubt("601")  # kein Fehler für Pilotobjekt


def test_vertragskomponente_ist_unveraenderlich(stammdaten_repo, basis_vertrag):
    """Regression (Abnahmesperre): eine einmal gebuchte Vertragskomponente
    darf nicht nachträglich verändert werden (keine Update-Methode, gleiche
    ID ein zweites Mal ist ein DB-Fehler statt eines stillen Overwrites)."""

    vertrag, _ = basis_vertrag
    stammdaten_repo.add_komponente(
        id="K-UNVERAENDERLICH", vertrag_id=vertrag.id, art="HMZ", bezeichnung="Hauptmietzins",
        betrag_cent=50_000, gueltig_von=date(2024, 1, 1),
    )
    assert not hasattr(stammdaten_repo, "update_komponente")
    with pytest.raises(IntegrityError):
        stammdaten_repo.add_komponente(
            id="K-UNVERAENDERLICH", vertrag_id=vertrag.id, art="HMZ", bezeichnung="Manipulierter Betrag",
            betrag_cent=99_999, gueltig_von=date(2024, 1, 1),
        )
    komponente = stammdaten_repo.get_komponente("K-UNVERAENDERLICH")
    assert komponente.betrag_cent == 50_000  # unverändert
