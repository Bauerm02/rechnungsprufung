from __future__ import annotations

from datetime import date

import pytest

from mietinkasso.domain.enums import Nutzungsstatus, OPTyp
from mietinkasso.stammdaten.service import ObjektAusgeschlossenError, StammdatenService


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
