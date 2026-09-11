from __future__ import annotations

from datetime import date

import pytest

from mietinkasso.domain.enums import OPTyp, Rolle
from mietinkasso.domain.exceptions import (
    CrossTenantError,
    DoppelteEroeffnungsartError,
    ImportConflictError,
    ObjektAusgeschlossenError,
)


def test_eroeffnen_gesamtsaldo_sperrt_objekt_107_auch_fuer_admin(op_service, stammdaten_repo, ctx_factory):
    """Regression (Codex-Rückprüfung): `pruefe_objekt_erlaubt` existierte
    zuvor nur als isolierte, nie aufgerufene Hilfsfunktion. Codex' Gegenprobe
    legte Objekt 107 mit ausgeschlossen=True an und rief
    OPService.eroeffnen_gesamtsaldo(50000) auf - es wurden trotzdem 500 EUR
    gebucht. Der Ausschluss muss zentral aus dem persistierten
    Konto->Vertrag->Einheit->Objekt erzwungen werden, rollenunabhängig
    (ADMIN eingeschlossen)."""

    stammdaten_repo.upsert_gesellschaft(id="7DI", name="7D Immobilien GmbH")
    stammdaten_repo.upsert_objekt(id="107", gesellschaft_id="7DI", bezeichnung="Sieben Dörfer", ausgeschlossen=True)
    stammdaten_repo.upsert_einheit(id="107-TOP1", objekt_id="107", bezeichnung="Top 1", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten_repo.upsert_debitor(id="DEB-107", name="Mieterin 107")
    stammdaten_repo.upsert_vertrag(
        id="V-107-1", einheit_id="107-TOP1", debitor_id="DEB-107", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    vertrag = stammdaten_repo.get_vertrag("V-107-1")
    konto = stammdaten_repo.get_or_create_konto(vertrag=vertrag)
    ctx_admin = ctx_factory("7DI", rolle=Rolle.ADMIN)

    with pytest.raises(ObjektAusgeschlossenError):
        op_service.eroeffnen_gesamtsaldo(
            ctx=ctx_admin, konto=konto, betrag_cent=50_000, stichtag=date(2026, 1, 1),
            import_id="ERO-107", akteur="admin-gegenprobe",
        )

    assert op_service.berechne_saldo(konto.id).saldo_cent == 0


def test_eroeffnung_soll_zahlung_ergibt_erwarteten_saldo(op_service, basis_vertrag, ctx_factory):
    """Eröffnung 100 + Soll 600 - Zahlung 200 = 500."""

    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")

    op_service.eroeffnen_gesamtsaldo(
        ctx=ctx, konto=konto, betrag_cent=10_000, stichtag=date(2026, 1, 1), import_id="ERO-1", akteur="test"
    )
    op_service.buchen(
        ctx=ctx,
        konto=konto,
        typ=OPTyp.SOLL,
        betrag_cent=60_000,
        belegdatum=date(2026, 2, 1),
        buchungsdatum=date(2026, 2, 1),
        faelligkeit=date(2026, 2, 5),
        beleg_referenz="Miete Februar",
        leistungsperiode="2026-02",
    )
    op_service.buchen(
        ctx=ctx,
        konto=konto,
        typ=OPTyp.ZAHLUNG,
        betrag_cent=20_000,
        belegdatum=date(2026, 2, 10),
        buchungsdatum=date(2026, 2, 10),
        faelligkeit=None,
        beleg_referenz="Teilzahlung Bank",
    )

    saldo = op_service.berechne_saldo(konto.id)
    assert saldo.saldo_cent == 50_000


def test_doppelimport_ist_wirkungslos(op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    for _ in range(2):
        op_service.buchen(
            ctx=ctx,
            konto=konto,
            typ=OPTyp.SOLL,
            betrag_cent=60_000,
            belegdatum=date(2026, 3, 1),
            buchungsdatum=date(2026, 3, 1),
            faelligkeit=date(2026, 3, 5),
            beleg_referenz="Miete März",
            import_id="ZINSLISTE-2026-03-V601-3",
        )
    saldo = op_service.berechne_saldo(konto.id)
    assert saldo.saldo_cent == 60_000
    assert len(saldo.positionen) == 1


def test_geaenderte_gleiche_import_id_ist_konflikt(op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    op_service.buchen(
        ctx=ctx,
        konto=konto,
        typ=OPTyp.SOLL,
        betrag_cent=60_000,
        belegdatum=date(2026, 3, 1),
        buchungsdatum=date(2026, 3, 1),
        faelligkeit=date(2026, 3, 5),
        beleg_referenz="Miete März",
        import_id="ZINSLISTE-2026-03-V601-3",
    )
    with pytest.raises(ImportConflictError):
        op_service.buchen(
            ctx=ctx,
            konto=konto,
            typ=OPTyp.SOLL,
            betrag_cent=61_000,  # geänderter Inhalt, gleiche import_id
            belegdatum=date(2026, 3, 1),
            buchungsdatum=date(2026, 3, 1),
            faelligkeit=date(2026, 3, 5),
            beleg_referenz="Miete März",
            import_id="ZINSLISTE-2026-03-V601-3",
        )


def test_saldo_und_enthaltenes_altjournal_nicht_doppelt_gebucht(op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    op_service.eroeffnen_gesamtsaldo(
        ctx=ctx, konto=konto, betrag_cent=10_000, stichtag=date(2026, 1, 31), import_id="ERO-1", akteur="test"
    )
    with pytest.raises(DoppelteEroeffnungsartError):
        op_service.buchen(
            ctx=ctx,
            konto=konto,
            typ=OPTyp.SOLL,
            betrag_cent=5_000,
            belegdatum=date(2026, 1, 15),  # liegt vor dem Gesamtsaldo-Stichtag
            buchungsdatum=date(2026, 1, 15),
            faelligkeit=date(2026, 1, 20),
            beleg_referenz="Altes Journal Jänner",
        )


def test_eroeffnung_gesamtsaldo_gleicher_fakt_mit_anderer_import_id_ist_replay(op_service, basis_vertrag, ctx_factory):
    """Regression (Codex-Fund #3): zwei Importe mit identischem Konto/
    Stichtag/Betrag aber UNTERSCHIEDLICHEN import_ids (z. B. zwei Dateien
    mit verschiedenem Namen für dieselbe Eröffnung) dürfen die Eröffnung
    NICHT verdoppeln - Eröffnung ist einmalig je Konto, nicht je Dateiname."""

    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    op_service.eroeffnen_gesamtsaldo(
        ctx=ctx, konto=konto, betrag_cent=60_000, stichtag=date(2026, 8, 31), import_id="A", akteur="test"
    )
    op_service.eroeffnen_gesamtsaldo(
        ctx=ctx, konto=konto, betrag_cent=60_000, stichtag=date(2026, 8, 31), import_id="B", akteur="test"
    )
    saldo = op_service.berechne_saldo(konto.id)
    assert saldo.saldo_cent == 60_000  # NICHT 120_000
    assert len([p for p in saldo.positionen if p.typ == "EROEFFNUNG"]) == 1


def test_eroeffnung_gesamtsaldo_widersprechender_zweitimport_wird_blockiert(op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    op_service.eroeffnen_gesamtsaldo(
        ctx=ctx, konto=konto, betrag_cent=60_000, stichtag=date(2026, 8, 31), import_id="A", akteur="test"
    )
    with pytest.raises(DoppelteEroeffnungsartError):
        op_service.eroeffnen_gesamtsaldo(
            ctx=ctx, konto=konto, betrag_cent=99_999, stichtag=date(2026, 8, 31), import_id="B", akteur="test"
        )
    saldo = op_service.berechne_saldo(konto.id)
    assert saldo.saldo_cent == 60_000  # unverändert, der Zweitimport hat nichts gebucht


def test_gesamtsaldo_und_einzel_op_gleichzeitig_ist_konflikt(op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    op_service.eroeffnen_gesamtsaldo(
        ctx=ctx, konto=konto, betrag_cent=10_000, stichtag=date(2026, 1, 31), import_id="ERO-1", akteur="test"
    )
    with pytest.raises(DoppelteEroeffnungsartError):
        op_service.eroeffnen_einzel_op(
            ctx=ctx,
            konto=konto,
            stichtag=date(2026, 1, 31),
            import_id="ERO-EINZEL-1",
            typ=OPTyp.SOLL,
            betrag_cent=4_000,
            belegdatum=date(2026, 1, 10),
            faelligkeit=date(2026, 1, 15),
            beleg_referenz="Alt-OP Jänner",
            akteur="test",
        )


def test_teilzahlung(op_service, basis_vertrag, ctx_factory):
    """700 - 300 = 400."""

    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    op_service.buchen(
        ctx=ctx,
        konto=konto,
        typ=OPTyp.SOLL,
        betrag_cent=70_000,
        belegdatum=date(2026, 4, 1),
        buchungsdatum=date(2026, 4, 1),
        faelligkeit=date(2026, 4, 5),
        beleg_referenz="Miete April",
    )
    op_service.buchen(
        ctx=ctx,
        konto=konto,
        typ=OPTyp.ZAHLUNG,
        betrag_cent=30_000,
        belegdatum=date(2026, 4, 12),
        buchungsdatum=date(2026, 4, 12),
        faelligkeit=None,
        beleg_referenz="Teilzahlung",
    )
    saldo = op_service.berechne_saldo(konto.id)
    assert saldo.saldo_cent == 40_000


def test_ueberzahlung_ergibt_guthaben(op_service, basis_vertrag, ctx_factory):
    """800 auf 700 = 100 Guthaben (negativer Saldo)."""

    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    op_service.buchen(
        ctx=ctx,
        konto=konto,
        typ=OPTyp.SOLL,
        betrag_cent=70_000,
        belegdatum=date(2026, 5, 1),
        buchungsdatum=date(2026, 5, 1),
        faelligkeit=date(2026, 5, 5),
        beleg_referenz="Miete Mai",
    )
    op_service.buchen(
        ctx=ctx,
        konto=konto,
        typ=OPTyp.ZAHLUNG,
        betrag_cent=80_000,
        belegdatum=date(2026, 5, 6),
        buchungsdatum=date(2026, 5, 6),
        faelligkeit=None,
        beleg_referenz="Überzahlung",
    )
    saldo = op_service.berechne_saldo(konto.id)
    assert saldo.saldo_cent == -10_000  # 100,00 EUR Guthaben


def test_ruecklastschrift_macht_op_wieder_offen(op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    op_service.buchen(
        ctx=ctx,
        konto=konto,
        typ=OPTyp.SOLL,
        betrag_cent=60_000,
        belegdatum=date(2026, 6, 1),
        buchungsdatum=date(2026, 6, 1),
        faelligkeit=date(2026, 6, 5),
        beleg_referenz="Miete Juni",
    )
    op_service.buchen(
        ctx=ctx,
        konto=konto,
        typ=OPTyp.ZAHLUNG,
        betrag_cent=60_000,
        belegdatum=date(2026, 6, 5),
        buchungsdatum=date(2026, 6, 5),
        faelligkeit=None,
        beleg_referenz="Zahlung Juni",
    )
    saldo_ausgeglichen = op_service.berechne_saldo(konto.id)
    assert saldo_ausgeglichen.saldo_cent == 0

    op_service.buchen(
        ctx=ctx,
        konto=konto,
        typ=OPTyp.RUECKLASTSCHRIFT,
        betrag_cent=60_000,
        belegdatum=date(2026, 6, 8),
        buchungsdatum=date(2026, 6, 8),
        faelligkeit=date(2026, 6, 8),
        beleg_referenz="Rücklastschrift Zahlung Juni",
    )
    saldo_wieder_offen = op_service.berechne_saldo(konto.id)
    assert saldo_wieder_offen.saldo_cent == 60_000


def test_cross_tenant_buchung_wird_blockiert(op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    fremde_ctx = ctx_factory("ANDERE-GESELLSCHAFT")
    with pytest.raises(CrossTenantError):
        op_service.buchen(
            ctx=fremde_ctx,
            konto=konto,
            typ=OPTyp.SOLL,
            betrag_cent=60_000,
            belegdatum=date(2026, 6, 1),
            buchungsdatum=date(2026, 6, 1),
            faelligkeit=date(2026, 6, 5),
            beleg_referenz="Unbefugte Buchung",
        )


def test_korrektur_ersetzt_original_ohne_historie_zu_ueberschreiben(op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    original = op_service.buchen(
        ctx=ctx,
        konto=konto,
        typ=OPTyp.SOLL,
        betrag_cent=60_000,
        belegdatum=date(2026, 7, 1),
        buchungsdatum=date(2026, 7, 1),
        faelligkeit=date(2026, 7, 5),
        beleg_referenz="Miete Juli (Tippfehler)",
    )
    op_service.storniere_und_korrigiere(
        ctx=ctx,
        konto=konto,
        original_id=original.id,
        aenderungsgrund="Tippfehler: falscher Betrag erfasst",
        neuer_betrag_cent=65_000,
    )
    alle = op_service._op_repository.list_alle(konto.id)
    assert len(alle) == 2  # Original bleibt erhalten (storniert), neue Zeile kommt dazu
    original_row = next(p for p in alle if p.id == original.id)
    assert original_row.status == "STORNIERT"
    saldo = op_service.berechne_saldo(konto.id)
    assert saldo.saldo_cent == 65_000
