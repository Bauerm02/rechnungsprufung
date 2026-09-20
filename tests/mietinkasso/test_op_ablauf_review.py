"""Unabhängige Abnahme: synthetische Buchungsketten ohne Versand."""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from mietinkasso.bank.importer import CsvSpaltenMapping
from mietinkasso.bank.repository import BankRepository
from mietinkasso.bank.service import BankImportService
from mietinkasso.domain.enums import OPTyp
from mietinkasso.domain.exceptions import CrossTenantError, StornierungKonfliktError, VorgangIdKonfliktError, ZahlungsbindungInkonsistentError
from mietinkasso.infrastructure.db.tables import MahnFallTable, OPPositionTable, ZuordnungTable
from mietinkasso.mahnwesen.repository import MahnFallRepository, MahnPolicyRepository
from mietinkasso.mahnwesen.service import MahnwesenService
from mietinkasso.vorschreibung.repository import VorschreibungRepository
from mietinkasso.vorschreibung.service import VorschreibungService


def buchen(service, ctx, konto, typ, betrag, tag, ziel=None, periode=None):
    return service.buchen(
        ctx=ctx, konto=konto, typ=typ, betrag_cent=betrag,
        belegdatum=tag, buchungsdatum=tag,
        faelligkeit=tag if typ == OPTyp.SOLL else None,
        beleg_referenz=f"Synthetisch {typ.value} {tag}",
        bezieht_sich_auf_id=ziel, leistungsperiode=periode,
    )


def korrektur(service, ctx, konto, original, betrag, vorgang="KOR-TEST"):
    return service.storniere_und_korrigiere(
        ctx=ctx, konto=konto, original_id=original.id,
        aenderungsgrund="Synthetische Abnahme", neuer_betrag_cent=betrag,
        vorgang_id=vorgang, heute=date(2026, 9, 10),
    )


def bestand(session_factory):
    with session_factory() as session:
        return list(session.execute(select(
            OPPositionTable.id, OPPositionTable.status, OPPositionTable.betrag_cent,
            OPPositionTable.bezieht_sich_auf_id, OPPositionTable.storniert_durch_id,
        ).order_by(OPPositionTable.id)).all())


@pytest.mark.parametrize("typ", [OPTyp.SOLL, OPTyp.ZAHLUNG, OPTyp.GUTSCHRIFT, OPTyp.RUECKLASTSCHRIFT])
@pytest.mark.parametrize("betrag", [-10000, 0])
def test_korrektur_verwendet_betragsregeln_ohne_storno_nebenwirkung(
    op_service, basis_vertrag, admin_ctx, session_factory, typ, betrag,
):
    _, konto = basis_vertrag
    original = buchen(op_service, admin_ctx, konto, typ, 10000, date(2026, 8, 1))
    vorher = bestand(session_factory)
    with pytest.raises(ValueError, match="positiv"):
        korrektur(op_service, admin_ctx, konto, original, betrag)
    assert bestand(session_factory) == vorher


@pytest.mark.parametrize("typ", [OPTyp.EROEFFNUNG, OPTyp.KORREKTUR])
def test_vorzeichenbehaftete_typen_bleiben_korrigierbar(op_service, basis_vertrag, admin_ctx, typ):
    _, konto = basis_vertrag
    original = buchen(op_service, admin_ctx, konto, typ, 10000, date(2026, 8, 1))
    neu = korrektur(op_service, admin_ctx, konto, original, -5000)
    assert neu.betrag_cent == -5000
    assert op_service.berechne_saldo(konto.id).saldo_cent == -5000


@pytest.mark.parametrize("typ", [OPTyp.ZAHLUNG, OPTyp.GUTSCHRIFT])
@pytest.mark.parametrize("periode", [None, "2026-09"])
def test_korrektur_erhaelt_ziel_und_monat_auch_bei_wiederholung(
    op_service, basis_vertrag, admin_ctx, session_factory, typ, periode,
):
    _, konto = basis_vertrag
    alt = buchen(op_service, admin_ctx, konto, OPTyp.SOLL, 10000, date(2026, 8, 1), periode="2026-08")
    ziel = buchen(op_service, admin_ctx, konto, OPTyp.SOLL, 10000, date(2026, 9, 1), periode="2026-09")
    zahlung = buchen(op_service, admin_ctx, konto, typ, 10000, date(2026, 9, 2), ziel.id, periode)
    neu = korrektur(op_service, admin_ctx, konto, zahlung, 10000)
    assert neu.bezieht_sich_auf_id == ziel.id
    assert neu.leistungsperiode == periode
    assert {f.op_position_id: f.rest_cent for f in op_service.offene_forderungen(konto.id)} == {alt.id: 10000}
    vorher = bestand(session_factory)
    assert korrektur(op_service, admin_ctx, konto, zahlung, 10000).id == neu.id
    assert bestand(session_factory) == vorher
    with pytest.raises(StornierungKonfliktError):
        korrektur(op_service, admin_ctx, konto, zahlung, 9000)
    assert bestand(session_factory) == vorher


@pytest.mark.parametrize("ersatz", [None, 9000])
def test_ziel_forderung_mit_aktiver_zahlung_bleibt_unveraendert(
    op_service, basis_vertrag, admin_ctx, session_factory, ersatz,
):
    _, konto = basis_vertrag
    ziel = buchen(op_service, admin_ctx, konto, OPTyp.SOLL, 10000, date(2026, 9, 1))
    buchen(op_service, admin_ctx, konto, OPTyp.ZAHLUNG, 5000, date(2026, 9, 2), ziel.id)
    vorher = bestand(session_factory)
    with pytest.raises(ValueError):
        korrektur(op_service, admin_ctx, konto, ziel, ersatz)
    assert bestand(session_factory) == vorher
    assert op_service.offene_forderungen(konto.id)[0].rest_cent == 5000


def test_abgelehnter_commit_rollt_storno_und_ersatz_gemeinsam_zurueck(
    op_service, basis_vertrag, admin_ctx, session_factory,
):
    _, konto = basis_vertrag
    original = buchen(op_service, admin_ctx, konto, OPTyp.SOLL, 10000, date(2026, 9, 1))
    vorher = bestand(session_factory)

    def verweigere_commit(session):
        raise RuntimeError("Synthetischer Commitfehler")

    event.listen(Session, "before_commit", verweigere_commit)
    try:
        with pytest.raises(RuntimeError, match="Synthetischer Commitfehler"):
            korrektur(op_service, admin_ctx, konto, original, 9000)
    finally:
        event.remove(Session, "before_commit", verweigere_commit)
    assert bestand(session_factory) == vorher
    assert korrektur(op_service, admin_ctx, konto, original, 9000).betrag_cent == 9000


def test_korrektur_prueft_frisches_konto_statt_manipuliertes_aufruferobjekt(
    op_service, basis_vertrag, admin_ctx, ctx_factory, session_factory,
):
    _, konto = basis_vertrag
    original = buchen(op_service, admin_ctx, konto, OPTyp.SOLL, 10000, date(2026, 9, 1))
    vorher = bestand(session_factory)
    konto.gesellschaft_id = "FREMD"
    with pytest.raises(CrossTenantError):
        korrektur(op_service, ctx_factory("FREMD"), konto, original, 9000)
    assert bestand(session_factory) == vorher


@pytest.mark.parametrize("fehler", ["storniert", "falscher_typ", "falsche_periode", "fehlendes_ziel"])
def test_ungueltige_altbindung_wird_bei_korrektur_nicht_uebernommen(
    op_service, basis_vertrag, admin_ctx, session_factory, fehler,
):
    _, konto = basis_vertrag
    ziel = buchen(op_service, admin_ctx, konto, OPTyp.SOLL, 10000, date(2026, 9, 1), periode="2026-09")
    zahlung = buchen(op_service, admin_ctx, konto, OPTyp.ZAHLUNG, 5000, date(2026, 9, 2), ziel.id, "2026-09")
    # Synthetischer Altbestand: In der bisherigen API erst beim Lesen erkannt.
    with session_factory() as session:
        row = session.get(OPPositionTable, ziel.id)
        if fehler == "storniert":
            row.status = "STORNIERT"
        elif fehler == "falscher_typ":
            row.typ = OPTyp.ZAHLUNG.value
        elif fehler == "falsche_periode":
            row.leistungsperiode = "2026-08"
        else:
            session.get(OPPositionTable, zahlung.id).bezieht_sich_auf_id = 999999
        session.commit()
    vorher = bestand(session_factory)
    with pytest.raises((ValueError, ZahlungsbindungInkonsistentError)):
        korrektur(op_service, admin_ctx, konto, zahlung, 6000)
    assert bestand(session_factory) == vorher


@pytest.mark.parametrize("gleicher_vorgang", [True, False])
def test_gleichzeitige_korrekturen_erzeugen_nur_einen_ersatz(
    op_service, basis_vertrag, admin_ctx, session_factory, tmp_path, gleicher_vorgang,
):
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy.orm import sessionmaker
    from mietinkasso.infrastructure.db.session import build_engine
    from mietinkasso.op.repository import OPRepository
    from mietinkasso.op.service import OPService
    from mietinkasso.stammdaten.repository import StammdatenRepository

    _, konto = basis_vertrag
    original = buchen(op_service, admin_ctx, konto, OPTyp.SOLL, 10000, date(2026, 9, 1))
    datei = tmp_path / "synthetische-nebenlaeufigkeit.sqlite"
    connection = session_factory.kw["bind"].raw_connection()
    ziel = sqlite3.connect(datei)
    try:
        connection.driver_connection.backup(ziel)
    finally:
        ziel.close()
        connection.close()
    engine = build_engine(f"sqlite+pysqlite:///{datei.as_posix()}")
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    service = OPService(OPRepository(factory), StammdatenRepository(factory))
    start = Barrier(2)

    def worker(nummer):
        start.wait(timeout=10)
        try:
            neu = korrektur(service, admin_ctx, konto, original, 9000, "SYN-RETRY" if gleicher_vorgang else f"SYN-{nummer}")
            return neu.id
        except StornierungKonfliktError:
            return "Konflikt"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            ergebnisse = list(pool.map(worker, [1, 2]))
        ids = [wert for wert in ergebnisse if isinstance(wert, int)]
        assert len(set(ids)) == 1
        assert ergebnisse.count("Konflikt") == (0 if gleicher_vorgang else 1)
        rows = bestand(factory)
        assert len(rows) == 2
        assert sum(row.status == "AKTIV" for row in rows) == 1
        assert service.berechne_saldo(konto.id).saldo_cent == 9000
    finally:
        engine.dispose()


@pytest.fixture
def bank(session_factory, stammdaten_repo, op_service, basis_vertrag):
    repo = BankRepository(session_factory)
    repo.upsert_bank_konto(id="BK-ABLAUF", gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="Synthetisch")
    return BankImportService(repo, stammdaten_repo, op_service), repo


def bankimport(bank, ctx, betrag, tag, referenz):
    service, repo = bank
    return service.importiere_csv(
        ctx=ctx, bank_konto=repo.get_bank_konto("BK-ABLAUF"),
        text=f"betrag,datum,referenz,bank_id\n{betrag},{tag},{referenz},SYN-{tag}-{betrag}\n",
        mapping=CsvSpaltenMapping(betrag="betrag", buchungsdatum="datum", referenz="referenz", eindeutige_referenz="bank_id"),
    )[0]


@pytest.mark.parametrize("ersatz", [None, 5000])
def test_banklink_sperrt_manuelle_korrektur_ohne_verlust(
    op_service, basis_vertrag, admin_ctx, session_factory, bank, ersatz,
):
    _, konto = basis_vertrag
    zahlung = buchen(op_service, admin_ctx, konto, OPTyp.ZAHLUNG, 5000, date(2026, 9, 2))
    tx = bankimport(bank, admin_ctx, "50.00", "2026-09-02", "Synthetischer Eingang")
    bank[0].verknuepfe_mit_bestehender_zahlung(
        ctx=admin_ctx, transaktion=tx, op_position=zahlung, konto=konto,
        betrag_cent=5000, vorgang_id="LINK-ABLAUF",
    )
    vorher = bestand(session_factory)
    with pytest.raises(ValueError):
        korrektur(op_service, admin_ctx, konto, zahlung, ersatz)
    assert bestand(session_factory) == vorher
    assert bank[1].zugeordneter_betrag(tx.id) == 5000
    with session_factory() as session:
        assert session.scalar(select(ZuordnungTable.op_position_id)) == zahlung.id


def ruecklastschriftfall(bank, op_service, ctx, konto):
    zahlung = buchen(op_service, ctx, konto, OPTyp.ZAHLUNG, 6000, date(2026, 9, 2))
    eingang = bankimport(bank, ctx, "60.00", "2026-09-02", "Synthetischer Eingang")
    bank[0].verknuepfe_mit_bestehender_zahlung(
        ctx=ctx, transaktion=eingang, op_position=zahlung, konto=konto,
        betrag_cent=6000, vorgang_id="LINK-RLS",
    )
    belastung = bankimport(bank, ctx, "-20.00", "2026-09-12", "Synthetische Ruecklastschrift")
    args = dict(ctx=ctx, transaktion=belastung, original_op_position=zahlung, konto=konto, betrag_cent=2000)
    return bank[0].verarbeite_ruecklastschrift(**args), args


@pytest.mark.parametrize("abweichung", ["betrag", "status", "periode", "typ"])
def test_ruecklastschrift_retry_lehnt_abweichungen_ohne_zusatzbuchung_ab(
    op_service, basis_vertrag, admin_ctx, session_factory, bank, abweichung,
):
    _, konto = basis_vertrag
    rueck, args = ruecklastschriftfall(bank, op_service, admin_ctx, konto)
    if abweichung == "betrag":
        args["betrag_cent"] = 1000
    else:
        with session_factory() as session:
            row = session.get(OPPositionTable, rueck.id)
            if abweichung == "status":
                row.status = "STORNIERT"
            elif abweichung == "periode":
                row.leistungsperiode = "2026-08"
            else:
                row.typ = OPTyp.ZAHLUNG.value
            session.commit()
    vorher = bestand(session_factory)
    with pytest.raises(VorgangIdKonfliktError):
        bank[0].verarbeite_ruecklastschrift(**args)
    assert bestand(session_factory) == vorher


def test_ruecklastschrift_retry_ueberspringt_keine_zugriffspruefung(
    op_service, basis_vertrag, admin_ctx, ctx_factory, session_factory, bank,
):
    _, konto = basis_vertrag
    _, args = ruecklastschriftfall(bank, op_service, admin_ctx, konto)
    args["ctx"] = ctx_factory("FREMD")
    konto.gesellschaft_id = "FREMD"
    vorher = bestand(session_factory)
    with pytest.raises(CrossTenantError):
        bank[0].verarbeite_ruecklastschrift(**args)
    assert bestand(session_factory) == vorher


@pytest.mark.parametrize("wiederholungen", [1, 3])
def test_vorschreibung_teilzahlung_korrektur_banklink_ruecklastschrift_mahnpruefung(
    op_service, stammdaten_repo, basis_vertrag, admin_ctx, session_factory, bank, wiederholungen,
):
    vertrag, konto = basis_vertrag
    stammdaten_repo.add_komponente(
        id="SYN-HMZ", vertrag_id=vertrag.id, art="HMZ", bezeichnung="Synthetische Miete",
        betrag_cent=10000, ust_satz_promille=0, gueltig_von=date(2026, 8, 1),
    )
    vorschreibung = VorschreibungService(VorschreibungRepository(session_factory), stammdaten_repo, op_service)
    for _ in range(wiederholungen):
        vorschreibung.entwurf_erstellen(ctx=admin_ctx, vertrag=vertrag, monat="2026-09")
        vorschreibung.sollstellen(ctx=admin_ctx, vertrag=vertrag, konto=konto, monat="2026-09")
    soll = op_service.offene_forderungen(konto.id)[0]
    assert soll.rest_cent == 10000
    zahlung = buchen(op_service, admin_ctx, konto, OPTyp.ZAHLUNG, 4000, date(2026, 9, 5), soll.op_position_id, "2026-09")
    for _ in range(wiederholungen):
        neu = korrektur(op_service, admin_ctx, konto, zahlung, 6000)
    assert op_service.offene_forderungen(konto.id)[0].rest_cent == 4000
    for _ in range(wiederholungen):
        eingang = bankimport(bank, admin_ctx, "60.00", "2026-09-05", "VERTRAG:V-601-3 September 2026")
        bank[0].verknuepfe_mit_bestehender_zahlung(
            ctx=admin_ctx, transaktion=eingang, op_position=neu, konto=konto,
            betrag_cent=6000, vorgang_id="LINK-KORREKTUR",
        )
    for _ in range(wiederholungen):
        belastung = bankimport(bank, admin_ctx, "-20.00", "2026-09-12", "Synthetische Ruecklastschrift")
        rueck = bank[0].verarbeite_ruecklastschrift(
            ctx=admin_ctx, transaktion=belastung, original_op_position=neu,
            konto=konto, betrag_cent=2000,
        )
    assert rueck.bezieht_sich_auf_id == neu.id
    assert op_service.berechne_saldo(konto.id).saldo_cent == 6000
    assert sum(f.rest_cent for f in op_service.offene_forderungen(konto.id)) == 6000
    assert bank[1].zugeordneter_betrag(eingang.id) == 6000
    vorher = bestand(session_factory)
    for op in (neu, rueck):
        with pytest.raises(ValueError):
            korrektur(op_service, admin_ctx, konto, op, None)
    assert bestand(session_factory) == vorher

    policies = MahnPolicyRepository(session_factory)
    policy = policies.freigeben(policies.anlegen(
        stufe1_tage_nach_faelligkeit=7, stufe2_mindesttage_nach_stufe1_versand=14,
        zinsen_prozent=Decimal("0"), gebuehr_cent=0, status="ENTWURF",
    ).id)
    mahn = MahnwesenService(MahnFallRepository(session_factory), stammdaten_repo, op_service, policies, bank_stand_max_age_days=2)
    for _ in range(wiederholungen):
        forderung = next(f for f in op_service.offene_forderungen(konto.id) if f.op_position_id == soll.op_position_id)
        ergebnis = mahn.vorschau_forderung(
            ctx=admin_ctx, vertrag=vertrag, konto=konto, forderung=forderung,
            policy=policy, heute=date(2026, 9, 20), bank_bestaetigt_bis=date(2026, 9, 20),
            ungeklaerte_eingaenge_vorhanden=bank[1].hat_ungeklaerte_relevante_eingaenge(bank_konto_id="BK-ABLAUF", vertrag_id=vertrag.id),
        )
        assert ergebnis.status == "GEPLANT"
        assert ergebnis.mahnfall_id is None
    assert bestand(session_factory) == vorher
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(MahnFallTable)) == 0
        assert session.scalar(select(func.count()).select_from(ZuordnungTable)) == 1
        assert session.scalar(select(func.count()).select_from(OPPositionTable)) == 4
