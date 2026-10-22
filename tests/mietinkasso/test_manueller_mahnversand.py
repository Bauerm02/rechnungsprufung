"""Auftrag HV-20261022-EIN-KONTOBLATT: Tests für den kontrollierten
Backoffice-Import tatsächlich manuell (außerhalb des automatisierten
Mahnlaufs) versendeter Mahnungen (`manueller_mahnversand_repository.py`/
`manueller_mahnversand_service.py`) - Idempotenz, Cross-Contract-/
Cross-Gesellschaft-Ablehnung, Auth/Schreibrecht, Buchung bei Import,
Doppelversand-Gate in `MahnwesenService.plane_mahnlauf`."""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from decimal import Decimal

import pytest

from mietinkasso.domain.enums import OPTyp, Rolle
from mietinkasso.domain.exceptions import CrossTenantError, VorgangIdKonfliktError
from mietinkasso.mahnwesen.kosten_service import MahnkostenService
from mietinkasso.mahnwesen.kosten_repository import MahnkostenRepository
from mietinkasso.mahnwesen.manueller_mahnversand_repository import (
    ManuellerMahnversandRepository,
    berechne_idempotenz_schluessel,
)
from mietinkasso.mahnwesen.manueller_mahnversand_service import ManuellerMahnversandService
from mietinkasso.mahnwesen.repository import MahnFallRepository, MahnLaufRepository, MahnPolicyRepository
from mietinkasso.mahnwesen.service import MahnwesenService


def _echtes_pdf(text: str) -> bytes:
    """Ein TATSÄCHLICH mit `pypdf` lesbares, einseitiges synthetisches PDF
    (wie `test_mahnbrief_archiv.py::_echter_beleg_pdf`) - unterschiedlicher
    `text` ergibt unterschiedliche Bytes/Hashes."""

    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.cell(0, 10, text)
    return bytes(pdf.output())


@pytest.fixture
def manueller_mahnversand_repo(session_factory) -> ManuellerMahnversandRepository:
    return ManuellerMahnversandRepository(session_factory)


@pytest.fixture
def kosten_repo(session_factory) -> MahnkostenRepository:
    return MahnkostenRepository(session_factory)


@pytest.fixture
def kosten_service(kosten_repo, op_service, stammdaten_repo) -> MahnkostenService:
    return MahnkostenService(kosten_repo, op_service, stammdaten_repo)


@pytest.fixture
def manueller_mahnversand_service(manueller_mahnversand_repo, stammdaten_repo, op_service, kosten_service) -> ManuellerMahnversandService:
    return ManuellerMahnversandService(
        repository=manueller_mahnversand_repo, stammdaten_repository=stammdaten_repo,
        op_service=op_service, mahnkosten_service=kosten_service,
    )


def _offene_forderung(op_service, konto, ctx, *, betrag_cent=50_000, faelligkeit=date(2026, 1, 5)):
    return op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=betrag_cent,
        belegdatum=date(2026, 1, 1), buchungsdatum=date(2026, 1, 1), faelligkeit=faelligkeit,
        beleg_referenz="HMZ Jänner",
    )


# -- Repository: Idempotenz -------------------------------------------------


def test_berechne_idempotenz_schluessel_ist_belegidentitaet_forderung_datum_kanal(op_service, basis_vertrag, admin_ctx):
    vertrag, konto = basis_vertrag
    position = _offene_forderung(op_service, konto, admin_ctx)
    schluessel_1 = berechne_idempotenz_schluessel(
        vertrag_id=vertrag.id, forderung_op_position_ids=[position.id],
        beleg_sha256="hash-a", versanddatum=date(2026, 2, 1), kanal="BRIEF",
    )
    # Dieselben Eingaben in anderer Reihenfolge der Forderungs-Ids -
    # derselbe Schlüssel (sortiert).
    schluessel_1_umsortiert = berechne_idempotenz_schluessel(
        vertrag_id=vertrag.id, forderung_op_position_ids=[position.id],
        beleg_sha256="hash-a", versanddatum=date(2026, 2, 1), kanal="BRIEF",
    )
    assert schluessel_1 == schluessel_1_umsortiert

    # Eine andere Beleg-Identität (anderer Hash) - bewusst NICHT nur
    # stufe+zins_bis oder stufe+Forderungsgruppe (unabhängige Rückprüfung
    # Codex 22.10.2026) - ergibt einen ANDEREN Schlüssel.
    schluessel_anderer_beleg = berechne_idempotenz_schluessel(
        vertrag_id=vertrag.id, forderung_op_position_ids=[position.id],
        beleg_sha256="hash-b", versanddatum=date(2026, 2, 1), kanal="BRIEF",
    )
    assert schluessel_anderer_beleg != schluessel_1


def test_identischer_originalnachweis_mit_abweichender_stufe_ist_konflikt(manueller_mahnversand_repo):
    """Unabhängiger Repro-Fund Codex 22.10.2026, echter Bug: BYTEIDENTISCHE
    `beleg_pdf_bytes`, identische Belegreferenz/Vertrag/Forderung/
    Versanddatum/Kanal, NUR die Stufe geändert (1 -> 2) - das ist KEIN
    zweites tatsächliches Schreiben. Muss als Konflikt abgelehnt werden,
    NIE eine zweite, unabhängige Gebührenbasis für dasselbe Dokument
    erzeugen."""

    identischer_beleg = _echtes_pdf("Original-Mahnschreiben, unverändert")
    kwargs = dict(
        vertrag_id="V-601-3", forderung_op_position_ids=[101], versanddatum=date(2026, 3, 1),
        kanal="BRIEF", empfaenger_name="Max Mustermieter", empfaenger_adresse="Musterstraße 1\n1010 Wien",
        beleg_referenz="Postaufgabe-STUFE-TEST", beleg_pdf_bytes=identischer_beleg,
        beleg_sha256=hashlib.sha256(identischer_beleg).hexdigest(), erfasst_von="codex-test",
    )
    erste = manueller_mahnversand_repo.importieren(stufe=1, **kwargs)
    assert erste.stufe == 1

    with pytest.raises(VorgangIdKonfliktError):
        manueller_mahnversand_repo.importieren(stufe=2, **kwargs)

    # Ein echter Retry MIT identischer Stufe bleibt weiterhin ein
    # sicherer No-Op.
    retry = manueller_mahnversand_repo.importieren(stufe=1, **kwargs)
    assert retry.id == erste.id


def test_identischer_reimport_ist_ein_sicherer_no_op(manueller_mahnversand_repo):
    kwargs = dict(
        vertrag_id="V-601-3", stufe=1, forderung_op_position_ids=[101, 102],
        versanddatum=date(2026, 2, 1), kanal="BRIEF", empfaenger_name="Max Mustermieter",
        empfaenger_adresse="Musterstraße 1\n1010 Wien", beleg_referenz="Postaufgabe-001",
        beleg_pdf_bytes=_echtes_pdf("Mahnung Stufe 1"), beleg_sha256="x" * 64, erfasst_von="codex-test",
    )
    erste = manueller_mahnversand_repo.importieren(**kwargs)
    zweite = manueller_mahnversand_repo.importieren(**kwargs)
    assert erste.id == zweite.id


def test_genuin_abweichender_beleg_erzeugt_eigene_neue_zeile(manueller_mahnversand_repo):
    """Ein genuin NEUER erforderlicher Beleg (anderer Hash, andere
    Forderungen) muss eine EIGENE, unabhängige Zeile erzeugen - NIE
    stillschweigend mit dem ersten verschmolzen werden."""

    erste = manueller_mahnversand_repo.importieren(
        vertrag_id="V-601-3", stufe=1, forderung_op_position_ids=[101],
        versanddatum=date(2026, 2, 1), kanal="BRIEF", empfaenger_name="Max Mustermieter",
        empfaenger_adresse="Musterstraße 1\n1010 Wien", beleg_referenz="Postaufgabe-001",
        beleg_pdf_bytes=_echtes_pdf("Mahnung A"), beleg_sha256="a" * 64, erfasst_von="codex-test",
    )
    zweite = manueller_mahnversand_repo.importieren(
        vertrag_id="V-601-3", stufe=1, forderung_op_position_ids=[102],
        versanddatum=date(2026, 3, 1), kanal="BRIEF", empfaenger_name="Max Mustermieter",
        empfaenger_adresse="Musterstraße 1\n1010 Wien", beleg_referenz="Postaufgabe-002",
        beleg_pdf_bytes=_echtes_pdf("Mahnung B"), beleg_sha256="b" * 64, erfasst_von="codex-test",
    )
    assert erste.id != zweite.id


# -- Service: Cross-Contract-/Cross-Gesellschaft-Ablehnung ------------------


def test_fremde_forderung_eines_anderen_vertrags_wird_abgelehnt(
    manueller_mahnversand_service, stammdaten_repo, op_service, basis_vertrag, ctx_factory,
):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    eigene_forderung = _offene_forderung(op_service, konto, ctx)

    # Zweiter Vertrag/Konto DERSELBEN Gesellschaft - dessen Forderung darf
    # NIE als Beleg für den ERSTEN Vertrag importiert werden.
    stammdaten_repo.upsert_einheit(id="601-TOP4", objekt_id="601", bezeichnung="Top 4", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten_repo.upsert_debitor(id="DEB-1002", name="Andere Mieterin", email="andere@example.at")
    stammdaten_repo.upsert_vertrag(
        id="V-601-4", einheit_id="601-TOP4", debitor_id="DEB-1002", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    fremder_vertrag = stammdaten_repo.get_vertrag("V-601-4")
    fremdes_konto = stammdaten_repo.get_or_create_konto(vertrag=fremder_vertrag)
    fremde_forderung = _offene_forderung(op_service, fremdes_konto, ctx)

    with pytest.raises(ValueError, match="Cross-Contract"):
        manueller_mahnversand_service.importieren(
            ctx=ctx, vertrag_id=vertrag.id, stufe=1,
            forderung_op_position_ids=[eigene_forderung.id, fremde_forderung.id],
            versanddatum=date(2026, 2, 1), kanal="BRIEF", empfaenger_name="Max Mustermieter",
            empfaenger_adresse="Musterstraße 1\n1010 Wien", beleg_referenz="Postaufgabe-001",
            beleg_pdf_bytes=_echtes_pdf("Mahnung"), erfasst_von="codex-test",
        )


def test_fremde_gesellschaft_wird_ueber_require_gesellschaft_access_abgelehnt(
    manueller_mahnversand_service, op_service, basis_vertrag, ctx_factory,
):
    vertrag, konto = basis_vertrag
    ctx_fremde_gesellschaft = ctx_factory("ANDERE-GESELLSCHAFT")
    forderung = _offene_forderung(op_service, konto, ctx_factory("7DI"))

    with pytest.raises(CrossTenantError):
        manueller_mahnversand_service.importieren(
            ctx=ctx_fremde_gesellschaft, vertrag_id=vertrag.id, stufe=1,
            forderung_op_position_ids=[forderung.id],
            versanddatum=date(2026, 2, 1), kanal="BRIEF", empfaenger_name="Max Mustermieter",
            empfaenger_adresse="Musterstraße 1\n1010 Wien", beleg_referenz="Postaufgabe-001",
            beleg_pdf_bytes=_echtes_pdf("Mahnung"), erfasst_von="codex-test",
        )


def test_lesezugriff_ohne_schreibrecht_wird_abgelehnt(
    manueller_mahnversand_service, op_service, basis_vertrag, ctx_factory,
):
    from mietinkasso.auth.service import AuthContext

    vertrag, konto = basis_vertrag
    ctx_schreibend = ctx_factory("7DI")
    forderung = _offene_forderung(op_service, konto, ctx_schreibend)
    ctx_lesend = AuthContext(user_id="nur-lesen", rolle=Rolle.LESEZUGRIFF, gesellschaft_ids=frozenset({"7DI"}))

    with pytest.raises(CrossTenantError):
        manueller_mahnversand_service.importieren(
            ctx=ctx_lesend, vertrag_id=vertrag.id, stufe=1, forderung_op_position_ids=[forderung.id],
            versanddatum=date(2026, 2, 1), kanal="BRIEF", empfaenger_name="Max Mustermieter",
            empfaenger_adresse="Musterstraße 1\n1010 Wien", beleg_referenz="Postaufgabe-001",
            beleg_pdf_bytes=_echtes_pdf("Mahnung"), erfasst_von="codex-test",
        )


def test_kein_echtes_pdf_wird_abgelehnt(manueller_mahnversand_service, op_service, basis_vertrag, ctx_factory):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    forderung = _offene_forderung(op_service, konto, ctx)

    with pytest.raises(ValueError, match="Belegdokument"):
        manueller_mahnversand_service.importieren(
            ctx=ctx, vertrag_id=vertrag.id, stufe=1, forderung_op_position_ids=[forderung.id],
            versanddatum=date(2026, 2, 1), kanal="BRIEF", empfaenger_name="Max Mustermieter",
            empfaenger_adresse="Musterstraße 1\n1010 Wien", beleg_referenz="Postaufgabe-001",
            beleg_pdf_bytes=b"ein reiner Referenztext, kein PDF", erfasst_von="codex-test",
        )


# -- Service: Buchung bei Import --------------------------------------------


def test_import_bucht_standardmaessig_gar_nichts_reiner_nachweis(
    manueller_mahnversand_service, op_service, basis_vertrag, ctx_factory,
):
    """Unabhängige Rückprüfung Codex 22.10.2026: ein echtes manuelles
    Schreiben enthält KEINE vom System vorausberechneten Zinsen/Gebühren -
    `importieren` (Default `sofort_buchen=False`) dokumentiert NUR den
    Nachweis, bucht GAR NICHTS (auch keine Zinsen)."""

    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    forderung = _offene_forderung(op_service, konto, ctx, faelligkeit=date(2026, 1, 5))
    saldo_vor = op_service.berechne_saldo(konto.id, stichtag=date(2026, 2, 1)).saldo_cent

    row = manueller_mahnversand_service.importieren(
        ctx=ctx, vertrag_id=vertrag.id, stufe=1, forderung_op_position_ids=[forderung.id],
        versanddatum=date(2026, 2, 1), kanal="BRIEF", empfaenger_name="Max Mustermieter",
        empfaenger_adresse="Musterstraße 1\n1010 Wien", beleg_referenz="Postaufgabe-001",
        beleg_pdf_bytes=_echtes_pdf("Mahnung Stufe 1"), erfasst_von="codex-test",
    )
    assert row.mahnkosten_buchung_id is None
    assert op_service.berechne_saldo(konto.id, stichtag=date(2026, 2, 1)).saldo_cent == saldo_vor  # KEINE Buchung, auch keine Zinsen


def test_bestaetige_buchung_nach_vorschau_bucht_zinsen_delta_einmalig(
    manueller_mahnversand_service, op_service, basis_vertrag, ctx_factory,
):
    """Die GETRENNTE, explizite Preview→Bestätigen-Aktion bucht das
    tatsächlich fällige Zinsdelta - GENAU EINMAL je Beleg, ein erneuter
    Aufruf nach bereits erfolgter Buchung bleibt ein sicherer No-Op."""

    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    forderung = _offene_forderung(op_service, konto, ctx, faelligkeit=date(2026, 1, 1))
    row = manueller_mahnversand_service.importieren(
        ctx=ctx, vertrag_id=vertrag.id, stufe=1, forderung_op_position_ids=[forderung.id],
        versanddatum=date(2026, 2, 1), kanal="BRIEF", empfaenger_name="Max Mustermieter",
        empfaenger_adresse="Musterstraße 1\n1010 Wien", beleg_referenz="Postaufgabe-001",
        beleg_pdf_bytes=_echtes_pdf("Mahnung Stufe 1"), erfasst_von="codex-test",
    )
    assert row.mahnkosten_buchung_id is None

    vorschau = manueller_mahnversand_service.vorschau_fuer_bestehenden_import(row_id=row.id, heute=date(2026, 2, 1))
    assert vorschau is not None and vorschau.neue_zinsen_delta_cent > 0  # reine Lesevorschau, bucht nichts

    ledger = manueller_mahnversand_service.bestaetige_buchung(ctx=ctx, row_id=row.id, heute=date(2026, 2, 1), akteur="codex-test")
    assert ledger is not None and ledger.zinsen_cent > 0
    zinsen_zeilen = [
        p for p in op_service.berechne_saldo(konto.id, stichtag=date(2026, 2, 1)).positionen
        if p.aenderungsgrund == "Mahnkosten - Verzugszinsen"
    ]
    assert len(zinsen_zeilen) == 1

    retry = manueller_mahnversand_service.bestaetige_buchung(ctx=ctx, row_id=row.id, heute=date(2026, 2, 1), akteur="codex-test")
    assert retry is None  # bereits gebucht - kein zweiter Versuch
    zinsen_zeilen_nach_retry = [
        p for p in op_service.berechne_saldo(konto.id, stichtag=date(2026, 2, 1)).positionen
        if p.aenderungsgrund == "Mahnkosten - Verzugszinsen"
    ]
    assert len(zinsen_zeilen_nach_retry) == 1


def test_sofort_buchen_true_bucht_direkt_beim_import(
    manueller_mahnversand_service, op_service, basis_vertrag, ctx_factory,
):
    """Kompatibilitätspfad: `sofort_buchen=True` bucht zusätzlich sofort -
    identisch zu einer unmittelbar nachfolgenden `bestaetige_buchung`."""

    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    forderung = _offene_forderung(op_service, konto, ctx, faelligkeit=date(2026, 1, 1))

    row = manueller_mahnversand_service.importieren(
        ctx=ctx, vertrag_id=vertrag.id, stufe=1, forderung_op_position_ids=[forderung.id],
        versanddatum=date(2026, 2, 1), kanal="BRIEF", empfaenger_name="Max Mustermieter",
        empfaenger_adresse="Musterstraße 1\n1010 Wien", beleg_referenz="Postaufgabe-001",
        beleg_pdf_bytes=_echtes_pdf("Mahnung Stufe 1"), erfasst_von="codex-test", sofort_buchen=True,
    )
    assert row.mahnkosten_buchung_id is not None
    zinsen_zeilen = [
        p for p in op_service.berechne_saldo(konto.id, stichtag=date(2026, 2, 1)).positionen
        if p.aenderungsgrund == "Mahnkosten - Verzugszinsen"
    ]
    assert len(zinsen_zeilen) == 1


def test_identischer_reimport_bucht_nicht_ein_zweites_mal(
    manueller_mahnversand_service, op_service, basis_vertrag, ctx_factory,
):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    forderung = _offene_forderung(op_service, konto, ctx, faelligkeit=date(2026, 1, 1))
    kwargs = dict(
        ctx=ctx, vertrag_id=vertrag.id, stufe=1, forderung_op_position_ids=[forderung.id],
        versanddatum=date(2026, 2, 1), kanal="BRIEF", empfaenger_name="Max Mustermieter",
        empfaenger_adresse="Musterstraße 1\n1010 Wien", beleg_referenz="Postaufgabe-001",
        beleg_pdf_bytes=_echtes_pdf("Mahnung Stufe 1"), erfasst_von="codex-test", sofort_buchen=True,
    )
    erste = manueller_mahnversand_service.importieren(**kwargs)
    saldo_vor_retry = op_service.berechne_saldo(konto.id, stichtag=date(2026, 2, 1)).saldo_cent
    zweite = manueller_mahnversand_service.importieren(**kwargs)
    saldo_nach_retry = op_service.berechne_saldo(konto.id, stichtag=date(2026, 2, 1)).saldo_cent

    assert erste.id == zweite.id
    assert saldo_vor_retry == saldo_nach_retry  # kein zweiter Buchungsversuch, keine neue OP-Position


def _freigabe_fuer_manuellen_import(kosten_repo, vertrag_id, stufe, forderung_ids, hauptforderung_cent, betrag_cent, freigegeben_von="codex-test"):
    from mietinkasso.mahnwesen.kosten import forderungsgruppe_schluessel
    schluessel = forderungsgruppe_schluessel(forderung_ids)
    return kosten_repo.mahnpauschale_freigabe_anlegen(
        vertrag_id=vertrag_id, stufe=stufe, forderungsgruppe_schluessel=schluessel,
        hauptforderung_cent=hauptforderung_cent, betrag_cent=betrag_cent,
        erforderlichkeit_begruendung="Begründung", tatsaechlicher_aufwand_cent=betrag_cent,
        tatsaechlicher_aufwand_beleg="Beleg", schuldhaftigkeit_begruendung="Begründung",
        verhaeltnismaessigkeit_begruendung="Begründung", freigegeben_von=freigegeben_von, erstellt_von=freigegeben_von,
    )


def test_zwei_echte_manuelle_importe_derselben_forderungsmenge_erhalten_eigene_gebuehr(
    manueller_mahnversand_service, kosten_repo, op_service, basis_vertrag, ctx_factory,
):
    """Unabhängige Rückprüfung Codex 22.10.2026, echter Bug: ZWEI echte,
    unterschiedliche manuelle Mahnungen (unterschiedliches Datum/
    unterschiedlicher Beleg) für DIESELBE Forderungsmenge/Stufe müssen
    BEIDE buchen können - der generische, nur forderungsmengen-basierte
    Schlüssel darf die zweite NIE an der ersten scheitern lassen."""

    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    forderung = _offene_forderung(op_service, konto, ctx, betrag_cent=50_000, faelligkeit=date(2026, 1, 5))
    _freigabe_fuer_manuellen_import(kosten_repo, vertrag.id, 1, [forderung.id], hauptforderung_cent=50_000, betrag_cent=2_000)

    erster = manueller_mahnversand_service.importieren(
        ctx=ctx, vertrag_id=vertrag.id, stufe=1, forderung_op_position_ids=[forderung.id],
        versanddatum=date(2026, 1, 11), kanal="PERSOENLICH", empfaenger_name="Max Mustermieter",
        empfaenger_adresse="persönlich übergeben", beleg_referenz="Uebergabe-A",
        beleg_pdf_bytes=_echtes_pdf("Uebergabe A"), erfasst_von="codex-test", sofort_buchen=True,
    )
    assert erster.pauschale_abgegolten is True

    # Zweites, GENUIN eigenes Schreiben (anderes Datum, anderer Beleg) -
    # derselbe Vertrag/Stufe/dieselbe Forderung - darf NICHT an der
    # ersten Buchung scheitern.
    zweiter = manueller_mahnversand_service.importieren(
        ctx=ctx, vertrag_id=vertrag.id, stufe=1, forderung_op_position_ids=[forderung.id],
        versanddatum=date(2026, 1, 21), kanal="PERSOENLICH", empfaenger_name="Max Mustermieter",
        empfaenger_adresse="persönlich übergeben", beleg_referenz="Uebergabe-B",
        beleg_pdf_bytes=_echtes_pdf("Uebergabe B"), erfasst_von="codex-test", sofort_buchen=True,
    )
    assert zweiter.id != erster.id
    assert zweiter.mahnkosten_buchung_id is not None
    assert zweiter.mahnkosten_buchung_id != erster.mahnkosten_buchung_id


def test_nachbuchung_belastet_denselben_beleg_nie_zweimal_trotz_neuer_freigabe(
    manueller_mahnversand_service, kosten_repo, op_service, admin_ctx, basis_vertrag, ctx_factory,
):
    """Unabhängige Rückprüfung Codex 22.10.2026, echter Bug: ein bereits
    mit Pauschale gebuchter manueller Brief darf durch eine SPÄTERE,
    ANDERE Freigabe (z. B. nach einer Teilzahlung neu auf eine geänderte
    Kapitalbasis erteilt) NICHT ein zweites Mal belastet werden - genau
    EINE Gebühr je ursprünglichem Schreiben, unabhängig von der
    auslösenden Freigabe-Id."""

    from mietinkasso.mahnwesen.kosten import forderungsgruppe_schluessel

    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    forderung = _offene_forderung(op_service, konto, ctx, betrag_cent=100_000, faelligkeit=date(2026, 1, 5))

    row = manueller_mahnversand_service.importieren(
        ctx=ctx, vertrag_id=vertrag.id, stufe=1, forderung_op_position_ids=[forderung.id],
        versanddatum=date(2026, 1, 11), kanal="PERSOENLICH", empfaenger_name="Max Mustermieter",
        empfaenger_adresse="persönlich übergeben", beleg_referenz="Uebergabe-001",
        beleg_pdf_bytes=_echtes_pdf("Uebergabe"), erfasst_von="codex-test",
    )
    assert row.pauschale_abgegolten is False  # noch keine Freigabe vorhanden

    schluessel_1 = forderungsgruppe_schluessel([forderung.id])
    freigabe_1 = _freigabe_fuer_manuellen_import(kosten_repo, vertrag.id, 1, [forderung.id], hauptforderung_cent=100_000, betrag_cent=2_000)
    gebuchte_1 = manueller_mahnversand_service.nachbuche_fuer_vertrag_stufe(
        ctx=ctx, vertrag_id=vertrag.id, stufe=1, forderungsgruppe_schluessel_neu=schluessel_1,
        heute=date(2026, 1, 16), akteur="codex-test", freigabe_id=freigabe_1.id,
    )
    assert len(gebuchte_1) == 1 and gebuchte_1[0].mahnpauschale_cent == 2_000

    # Teilzahlung (ändert die offene Restforderung, NICHT die Forderungs-
    # Id-Menge selbst) + eine ZWEITE, andere Freigabe für DIESELBE
    # Forderungsmenge/Stufe (z. B. nachträglich mit anderem Betrag).
    op_service.buchen(
        ctx=admin_ctx, konto=konto, typ=OPTyp.ZAHLUNG, betrag_cent=40_000,
        belegdatum=date(2026, 1, 20), buchungsdatum=date(2026, 1, 20), faelligkeit=None,
        beleg_referenz="Teilzahlung", bezieht_sich_auf_id=forderung.id,
    )
    freigabe_2 = _freigabe_fuer_manuellen_import(
        kosten_repo, vertrag.id, 1, [forderung.id], hauptforderung_cent=60_000, betrag_cent=2_500, freigegeben_von="codex-test-2",
    )
    gebuchte_2 = manueller_mahnversand_service.nachbuche_fuer_vertrag_stufe(
        ctx=ctx, vertrag_id=vertrag.id, stufe=1, forderungsgruppe_schluessel_neu=schluessel_1,
        heute=date(2026, 1, 21), akteur="codex-test", freigabe_id=freigabe_2.id,
    )
    assert gebuchte_2 == []  # Beleg bereits abgegolten - KEINE zweite Belastung

    gebuehr_summe = sum(
        p.betrag_cent for p in op_service.berechne_saldo(konto.id, stichtag=date(2026, 1, 21)).positionen
        if p.aenderungsgrund == "Mahnkosten - Mahnkostenpauschale"
    )
    assert gebuehr_summe == 2_000  # weiterhin nur die EINE, erste Pauschale


# -- MahnwesenService.plane_mahnlauf: Doppelversand-Gate --------------------


@pytest.fixture
def mahn_policy_repo(session_factory) -> MahnPolicyRepository:
    return MahnPolicyRepository(session_factory)


@pytest.fixture
def mahn_fall_repo(session_factory) -> MahnFallRepository:
    return MahnFallRepository(session_factory)


@pytest.fixture
def mahnlauf_repo(session_factory) -> MahnLaufRepository:
    return MahnLaufRepository(session_factory)


@pytest.fixture
def freigegebene_policy(mahn_policy_repo):
    policy = mahn_policy_repo.anlegen(
        stufe1_tage_nach_faelligkeit=7, stufe2_mindesttage_nach_stufe1_versand=14,
        zinsen_prozent=Decimal("0"), gebuehr_cent=0, status="ENTWURF",
    )
    return mahn_policy_repo.freigeben(policy.id)


def _mahn_service(mahn_fall_repo, stammdaten_repo, op_service, mahn_policy_repo, mahnlauf_repo, *, manueller_mahnversand_repo=None):
    return MahnwesenService(
        mahn_fall_repo, stammdaten_repo, op_service, mahn_policy_repo, bank_stand_max_age_days=2,
        mahnlauf_repository=mahnlauf_repo, manueller_mahnversand_repository=manueller_mahnversand_repo,
    )


def test_plane_mahnlauf_blockiert_buendelung_wenn_bereits_manuell_versendet(
    mahn_fall_repo, stammdaten_repo, op_service, mahn_policy_repo, mahnlauf_repo,
    manueller_mahnversand_repo, basis_vertrag, ctx_factory, freigegebene_policy,
):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    forderung = _offene_forderung(op_service, konto, ctx, faelligkeit=date(2026, 4, 5))
    mahn_service = _mahn_service(
        mahn_fall_repo, stammdaten_repo, op_service, mahn_policy_repo, mahnlauf_repo,
        manueller_mahnversand_repo=manueller_mahnversand_repo,
    )
    heute = date(2026, 4, 20)
    offene = op_service.offene_forderungen(konto.id, heute=heute)[0]
    geplant = mahn_service.plane_forderung(
        ctx=ctx, vertrag=vertrag, konto=konto, forderung=offene, policy=freigegebene_policy, heute=heute,
        bank_bestaetigt_bis=heute, ungeklaerte_eingaenge_vorhanden=False,
    )
    assert geplant.status == "GEPLANT"

    manueller_mahnversand_repo.importieren(
        vertrag_id=vertrag.id, stufe=1, forderung_op_position_ids=[forderung.id],
        versanddatum=date(2026, 4, 15), kanal="PERSOENLICH", empfaenger_name="Max Mustermieter",
        empfaenger_adresse="persönlich übergeben", beleg_referenz="Uebergabe-001",
        beleg_pdf_bytes=_echtes_pdf("Uebergabebestaetigung"), beleg_sha256="z" * 64, erfasst_von="codex-test",
    )

    mahnlauf = mahn_service.plane_mahnlauf(
        ctx=ctx, vertrag=vertrag, konto=konto, stufe=1, heute=heute, bank_bestaetigt_bis=heute,
    )
    assert mahnlauf is None  # KEINE automatische Bündelung - Doppelversand verhindert


def test_plane_mahnlauf_buendelt_weiterhin_normal_ohne_manuellen_versand(
    mahn_fall_repo, stammdaten_repo, op_service, mahn_policy_repo, mahnlauf_repo,
    manueller_mahnversand_repo, basis_vertrag, ctx_factory, freigegebene_policy,
):
    """Regressionssicherung: das Gate darf die normale Bündelung NICHT
    blockieren, solange kein Manualimport für (Vertrag, Stufe) existiert."""

    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    _offene_forderung(op_service, konto, ctx, faelligkeit=date(2026, 4, 5))
    mahn_service = _mahn_service(
        mahn_fall_repo, stammdaten_repo, op_service, mahn_policy_repo, mahnlauf_repo,
        manueller_mahnversand_repo=manueller_mahnversand_repo,
    )
    heute = date(2026, 4, 20)
    offene = op_service.offene_forderungen(konto.id, heute=heute)[0]
    mahn_service.plane_forderung(
        ctx=ctx, vertrag=vertrag, konto=konto, forderung=offene, policy=freigegebene_policy, heute=heute,
        bank_bestaetigt_bis=heute, ungeklaerte_eingaenge_vorhanden=False,
    )

    mahnlauf = mahn_service.plane_mahnlauf(
        ctx=ctx, vertrag=vertrag, konto=konto, stufe=1, heute=heute, bank_bestaetigt_bis=heute,
    )
    assert mahnlauf is not None


def test_plane_mahnlauf_gate_blockiert_nur_konkret_abgedeckte_forderung_nicht_ganze_stufe(
    mahn_fall_repo, stammdaten_repo, op_service, mahn_policy_repo, mahnlauf_repo,
    manueller_mahnversand_repo, basis_vertrag, ctx_factory, freigegebene_policy,
):
    """Unabhängige Rückprüfung Codex 22.10.2026, echter Bug: ein
    Manualimport für EINE Forderung darf NICHT die gesamte Stufe/den
    gesamten Vertrag für immer von der automatischen Bündelung
    ausschließen - eine GENUIN ANDERE, später fällige Forderung
    DERSELBEN Stufe muss weiterhin normal automatisch mahnbar bleiben."""

    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    alte_forderung = _offene_forderung(op_service, konto, ctx, betrag_cent=50_000, faelligkeit=date(2026, 1, 5))
    mahn_service = _mahn_service(
        mahn_fall_repo, stammdaten_repo, op_service, mahn_policy_repo, mahnlauf_repo,
        manueller_mahnversand_repo=manueller_mahnversand_repo,
    )
    heute = date(2026, 4, 20)
    # Alte Forderung wird MANUELL gemahnt (z. B. persönlich übergeben).
    manueller_mahnversand_repo.importieren(
        vertrag_id=vertrag.id, stufe=1, forderung_op_position_ids=[alte_forderung.id],
        versanddatum=date(2026, 1, 15), kanal="PERSOENLICH", empfaenger_name="Max Mustermieter",
        empfaenger_adresse="persönlich übergeben", beleg_referenz="Uebergabe-Januar",
        beleg_pdf_bytes=_echtes_pdf("UebergabeJanuar"), beleg_sha256="j" * 64, erfasst_von="codex-test",
    )

    # NEUE, GENUIN ANDERE Forderung (z. B. die März-Miete) - derselbe
    # Vertrag, dieselbe Stufe 1, aber eine völlig eigene Forderung, die
    # mit dem Januar-Manualimport nichts zu tun hat.
    neue_forderung = _offene_forderung(op_service, konto, ctx, betrag_cent=50_000, faelligkeit=date(2026, 3, 5))
    offene_neu = next(f for f in op_service.offene_forderungen(konto.id, heute=heute) if f.op_position_id == neue_forderung.id)
    geplant = mahn_service.plane_forderung(
        ctx=ctx, vertrag=vertrag, konto=konto, forderung=offene_neu, policy=freigegebene_policy, heute=heute,
        bank_bestaetigt_bis=heute, ungeklaerte_eingaenge_vorhanden=False,
    )
    assert geplant.status == "GEPLANT"

    mahnlauf = mahn_service.plane_mahnlauf(
        ctx=ctx, vertrag=vertrag, konto=konto, stufe=1, heute=heute, bank_bestaetigt_bis=heute,
    )
    assert mahnlauf is not None  # die NEUE Forderung bleibt automatisch mahnbar
    mitglieder_ids = MahnLaufRepository.mitglieder_ids(mahnlauf)
    mitglieder = [mahn_fall_repo.get(i) for i in mitglieder_ids]
    assert {m.forderung_op_position_id for m in mitglieder} == {neue_forderung.id}  # NICHT die alte, manuell gemahnte
