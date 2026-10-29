"""Tests für `bank/ebics_ingest.py` und `scripts/ebics_ingest.py` (Auftrag
HV-20261029-EBICS-INTEGRATION). Ausschließlich synthetische Daten
(Platzhalter-IBANs, erfundene Namen/Referenzen), keine Bank-/Netzwerk-
aktion, kein Server."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from mietinkasso.auth.service import AuthContext
from mietinkasso.bank import ebics_ingest as ei
from mietinkasso.bank.ebics_ingest import (
    EbicsDokumentAbgelehntError,
    EbicsKontoAbweichungError,
    EbicsPlanHashAbweichungError,
    EbicsPlanNichtAnwendbarError,
    apply_ebics_camt053,
    plan_ebics_camt053,
)
from mietinkasso.bank.importer import CamtKontoMismatchError, CsvSpaltenMapping
from mietinkasso.bank.repository import BankRepository
from mietinkasso.bank.service import BankImportService
from mietinkasso.domain.enums import Rolle
from mietinkasso.domain.exceptions import CrossTenantError, FremdwaehrungNichtUnterstuetztError
from mietinkasso.infrastructure.db.base import Base
from mietinkasso.infrastructure.db.session import build_engine, build_session_factory
from mietinkasso.infrastructure.db.tables import BankTransaktionTable, OPPositionTable, ZuordnungTable
from mietinkasso.op.repository import OPRepository
from mietinkasso.op.service import OPService
from mietinkasso.stammdaten.repository import StammdatenRepository

IBAN = "AT000000000000000000"
FREMD_IBAN = "AT990000000000000099"
MIETER_A = "AT110000000000000011"
MIETER_B = "AT220000000000000022"
REF_A = "VERTRAG:V-601-3 Miete April 2026"
REF_B = "VERTRAG:V-601-4 Miete April 2026"


# -- synthetische CAMT.053-Bausteine --------------------------------------------


def _eur(cent: int) -> str:
    return f"{cent // 100}.{cent % 100:02d}"


def _tx(*, ref=None, iban=None, native=None, betrag=None, name="Synthetische Person") -> str:
    amt = f'<AmtDtls><TxAmt><Amt Ccy="EUR">{_eur(betrag)}</Amt></TxAmt></AmtDtls>' if betrag is not None else ""
    acct = f"<DbtrAcct><Id><IBAN>{iban}</IBAN></Id></DbtrAcct>" if iban else ""
    rmt = f"<RmtInf><Ustrd>{ref}</Ustrd></RmtInf>" if ref else ""
    refs = f"<Refs><AcctSvcrRef>{native}</AcctSvcrRef></Refs>" if native else ""
    return f"<TxDtls>{refs}{amt}<RltdPties><Dbtr><Nm>{name}</Nm></Dbtr>{acct}</RltdPties>{rmt}</TxDtls>"


def _ntry(
    betrag: int, datum: str, *, native=None, ref=None, iban=None, valuta=None, sts="BOOK", ccy="EUR",
    teile=None, richtung="CRDT", ntry_acct_ref=None,
) -> str:
    ccy_attr = f' Ccy="{ccy}"' if ccy else ""
    val = f"<ValDt><Dt>{valuta}</Dt></ValDt>" if valuta else ""
    val += f"<AcctSvcrRef>{ntry_acct_ref}</AcctSvcrRef>" if ntry_acct_ref else ""
    if teile:
        details = "".join(_tx(**teil) for teil in teile)
    else:
        details = _tx(ref=ref, iban=iban, native=native)
    return (
        f"<Ntry><Amt{ccy_attr}>{_eur(betrag)}</Amt><CdtDbtInd>{richtung}</CdtDbtInd><Sts>{sts}</Sts>"
        f"<BookgDt><Dt>{datum}</Dt></BookgDt>{val}<NtryDtls>{details}</NtryDtls></Ntry>"
    )


def _stmt(*ntries: str, iban: str = IBAN, stmt_id: str | None = "STMT-SYN-1") -> str:
    id_xml = f"<Id>{stmt_id}</Id>" if stmt_id else ""
    return (
        f"<Stmt>{id_xml}<ElctrncSeqNb>7</ElctrncSeqNb><CreDtTm>2026-04-08T06:00:00</CreDtTm>"
        "<FrToDt><FrDtTm>2026-04-01T00:00:00</FrDtTm><ToDtTm>2026-04-07T23:59:59</ToDtTm></FrToDt>"
        f"<Acct><Id><IBAN>{iban}</IBAN></Id><Ccy>EUR</Ccy></Acct>{''.join(ntries)}</Stmt>"
    )


def _camt(*stmts: str, ns: str = "camt.053.001.02") -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Document xmlns="urn:iso:std:iso:20022:tech:xsd:{ns}"><BkToCstmrStmt>'
        "<GrpHdr><MsgId>MSG-SYN-1</MsgId><CreDtTm>2026-04-08T06:00:00</CreDtTm></GrpHdr>"
        f"{''.join(stmts)}</BkToCstmrStmt></Document>"
    ).encode("utf-8")


# -- Fixtures -------------------------------------------------------------------


@pytest.fixture
def bank_service(session_factory, stammdaten_repo) -> BankImportService:
    return BankImportService(
        BankRepository(session_factory), stammdaten_repo, OPService(OPRepository(session_factory), stammdaten_repo)
    )


@pytest.fixture
def konto(session_factory, stammdaten_repo):
    stammdaten_repo.upsert_gesellschaft(id="7DI", name="7D Immobilien GmbH")
    repo = BankRepository(session_factory)
    repo.upsert_bank_konto(id="BK-7DI-1", gesellschaft_id="7DI", iban=IBAN, bezeichnung="Mietkonto (synthetisch)")
    return repo.get_bank_konto("BK-7DI-1")


@pytest.fixture
def ctx(ctx_factory) -> AuthContext:
    return ctx_factory("7DI")


def _plan(session_factory, ctx, camt: bytes, **kwargs):
    return plan_ebics_camt053(
        session_factory=session_factory, bank_konto_id="BK-7DI-1", erwartete_iban=IBAN, camt_bytes=camt, ctx=ctx,
        **kwargs,
    )


def _apply(session_factory, ctx, camt: bytes, plan_hash: str, **kwargs):
    return apply_ebics_camt053(
        session_factory=session_factory, bank_konto_id="BK-7DI-1", erwartete_iban=IBAN, camt_bytes=camt, ctx=ctx,
        erwarteter_plan_hash=plan_hash, **kwargs,
    )


def _plan_und_apply(session_factory, ctx, camt: bytes):
    plan = _plan(session_factory, ctx, camt)
    return plan, _apply(session_factory, ctx, camt, plan.plan_hash)


def _anzahl(session_factory, tabelle) -> int:
    with session_factory() as session:
        return session.execute(select(func.count()).select_from(tabelle)).scalar_one()


_CSV_MAPPING = CsvSpaltenMapping(
    betrag="betrag", buchungsdatum="datum", referenz="referenz", gegenkonto_iban="iban",
    eindeutige_referenz="id", valuta="valuta",
)


def _csv(bank_service, ctx, konto, zeilen: list[str]):
    text = "betrag,datum,valuta,referenz,iban,id\n" + "\n".join(zeilen) + "\n"
    return bank_service.importiere_csv(ctx=ctx, bank_konto=konto, text=text, mapping=_CSV_MAPPING)


# -- Grundablauf ----------------------------------------------------------------


def test_plan_ist_lesend_und_apply_schreibt_nur_bankzeilen(session_factory, konto, ctx, basis_vertrag):
    camt = _camt(_stmt(
        _ntry(60_000, "2026-04-06", native="EB-1", ref=REF_A, iban=MIETER_A),
        _ntry(45_000, "2026-04-06", native="EB-2", ref=REF_B, iban=MIETER_B),
    ))
    op_vorher = _anzahl(session_factory, OPPositionTable)

    plan = _plan(session_factory, ctx, camt)
    assert [e.status for e in plan.eintraege] == ["NEU", "NEU"]
    assert plan.anwendbar and plan.abgleich_vollstaendig
    assert _anzahl(session_factory, BankTransaktionTable) == 0  # Plan schreibt nichts

    ergebnis = _apply(session_factory, ctx, camt, plan.plan_hash)
    assert len(ergebnis.geschriebene_transaktion_ids) == 2
    with session_factory() as session:
        zeilen = session.execute(select(BankTransaktionTable).order_by(BankTransaktionTable.id)).scalars().all()
    assert [(z.betrag_cent, z.quelle_typ, z.import_id) for z in zeilen] == [
        (60_000, "CAMT053", "BK-7DI-1:CAMT053:EB-1"),
        (45_000, "CAMT053", "BK-7DI-1:CAMT053:EB-2"),
    ]
    # Datenimport und Mietbuchung getrennt: keine OP, keine Zuordnung.
    assert _anzahl(session_factory, OPPositionTable) == op_vorher
    assert _anzahl(session_factory, ZuordnungTable) == 0
    assert ergebnis.to_dict()["op_oder_zuordnung_geaendert"] is False


def test_planhash_stabil_und_json_ohne_stille_nullwerte(session_factory, konto, ctx):
    camt = _camt(_stmt(_ntry(60_000, "2026-04-06", native="EB-1", ref=REF_A)))
    plan_1 = _plan(session_factory, ctx, camt)
    plan_2 = _plan(session_factory, ctx, camt)
    assert plan_1.plan_hash == plan_2.plan_hash
    daten = json.loads(json.dumps(plan_1.to_dict()))
    eintrag = daten["eintraege"][0]
    assert eintrag["betrag_cent"] == 60_000
    assert eintrag["buchungsdatum"] == "2026-04-06"
    assert eintrag["valuta"] is None and eintrag["gegenkonto_iban"] is None  # fehlend ist nicht 0/""
    assert eintrag["quelle"]["ntry_index"] == 1 and len(eintrag["quelle"]["ntry_sha256"]) == 64
    assert daten["quelle_sha256"] == plan_1.quelle_sha256 and len(daten["quelle_sha256"]) == 64
    stmt = daten["dokument"]["statements"][0]
    assert stmt["zeitraum_von_laut_dokument"] == "2026-04-01T00:00:00"
    assert daten["dokument"]["abdeckung_und_salden"].startswith("nicht geprüft")
    assert daten["bankbestand_snapshot"] == {"anzahl": 0, "summe_cent": 0, "max_id": None,
                                             "inhalt_sha256": daten["bankbestand_snapshot"]["inhalt_sha256"]}
    # andere Toleranz -> anderer Plan
    assert _plan(session_factory, ctx, camt, datum_toleranz_tage=1).plan_hash != plan_1.plan_hash


def test_wiederholung_und_ueberlappung_fuegen_keine_zeile_hinzu(session_factory, konto, ctx):
    camt_1 = _camt(_stmt(
        _ntry(60_000, "2026-04-06", native="EB-1", ref=REF_A, iban=MIETER_A),
        _ntry(45_000, "2026-04-07", native="EB-2", ref=REF_B, iban=MIETER_B),
    ))
    _plan_und_apply(session_factory, ctx, camt_1)

    plan_wdh, ergebnis_wdh = _plan_und_apply(session_factory, ctx, camt_1)
    assert {e.grund_code for e in plan_wdh.eintraege} == {ei.GRUND_NATIVE_ID_GLEICH}
    assert ergebnis_wdh.geschriebene_transaktion_ids == ()

    camt_ueberlappend = _camt(_stmt(
        _ntry(45_000, "2026-04-07", native="EB-2", ref=REF_B, iban=MIETER_B),
        _ntry(12_345, "2026-04-08", native="EB-3", ref="VERTRAG:V-601-5 Teilzahlung", iban=MIETER_B),
    ))
    plan, ergebnis = _plan_und_apply(session_factory, ctx, camt_ueberlappend)
    assert [e.status for e in plan.eintraege] == ["VORHANDEN", "NEU"]
    assert len(ergebnis.geschriebene_transaktion_ids) == 1
    assert _anzahl(session_factory, BankTransaktionTable) == 3


# -- quellenübergreifende Dubletten ---------------------------------------------


def test_csv_ohne_native_id_und_camt_exakt_gleich_wird_nicht_neu_geschrieben(
    session_factory, konto, ctx, bank_service
):
    _csv(bank_service, ctx, konto, [f"600.00,2026-04-06,,{REF_A},{MIETER_A},"])
    camt = _camt(_stmt(_ntry(60_000, "2026-04-06", native="EB-1", ref=REF_A, iban=MIETER_A, valuta="2026-04-06")))
    plan, ergebnis = _plan_und_apply(session_factory, ctx, camt)
    assert plan.eintraege[0].status == "VORHANDEN"
    assert plan.eintraege[0].grund_code == ei.GRUND_WIRTSCHAFTLICH_IDENTISCH
    assert ergebnis.geschriebene_transaktion_ids == ()
    assert _anzahl(session_factory, BankTransaktionTable) == 1


def test_csv_mit_valuta_als_datum_wird_kreuzweise_erkannt(session_factory, konto, ctx, bank_service):
    _csv(bank_service, ctx, konto, [f"600.00,2026-04-07,,{REF_A},{MIETER_A},"])
    camt = _camt(_stmt(_ntry(60_000, "2026-04-06", native="EB-1", ref=REF_A, iban=MIETER_A, valuta="2026-04-07")))
    plan = _plan(session_factory, ctx, camt)
    assert plan.eintraege[0].grund_code == ei.GRUND_WIRTSCHAFTLICH_IDENTISCH


def test_gleiche_native_id_quellenuebergreifend(session_factory, konto, ctx, bank_service):
    _csv(bank_service, ctx, konto, ["600.00,2026-04-06,,Miete,,EB-1"])
    camt = _camt(_stmt(_ntry(60_000, "2026-04-06", native="EB-1", ref="Miete")))
    plan = _plan(session_factory, ctx, camt)
    assert plan.eintraege[0].grund_code == ei.GRUND_NATIVE_ID_GLEICH
    assert "CSV" in plan.eintraege[0].grund


def test_gleiche_native_id_mit_widersprechendem_inhalt_bricht_ab(session_factory, konto, ctx):
    _plan_und_apply(session_factory, ctx, _camt(_stmt(_ntry(60_000, "2026-04-06", native="EB-1", ref=REF_A))))
    camt = _camt(_stmt(
        _ntry(65_000, "2026-04-06", native="EB-1", ref=REF_A),
        _ntry(10_000, "2026-04-09", native="EB-9", ref="VERTRAG:V-601-9 neu"),
    ))
    plan = _plan(session_factory, ctx, camt)
    assert plan.eintraege[0].status == "KONFLIKT"
    assert not plan.anwendbar and not plan.abgleich_vollstaendig
    with pytest.raises(EbicsPlanNichtAnwendbarError):
        _apply(session_factory, ctx, camt, plan.plan_hash)
    assert _anzahl(session_factory, BankTransaktionTable) == 1  # auch die sichere neue Zeile nicht


def test_mehrfache_referenz_ohne_statement_id_bleibt_unklar(session_factory, konto, ctx):
    """Rückprüfung 2: eine mehrfach vergebene AcctSvcrRef ist kein
    Widerspruch, aber ohne eindeutige Statement-Id gibt es keine belastbare
    Einzelidentität -> UNKLAR, kein Raten, nichts geschrieben."""

    camt = _camt(_stmt(
        _ntry(60_000, "2026-04-06", native="EB-1", ref=REF_A),
        _ntry(60_001, "2026-04-06", native="EB-1", ref=REF_A),
        _ntry(12_345, "2026-04-06", native="EB-2", ref=REF_B),
        stmt_id=None,
    ))
    plan, ergebnis = _plan_und_apply(session_factory, ctx, camt)
    assert [e.status for e in plan.eintraege] == ["UNKLAR", "UNKLAR", "NEU"]
    assert {e.grund_code for e in plan.unklar} == {ei.GRUND_REFERENZ_NICHT_EINDEUTIG}
    assert plan.anwendbar and not plan.abgleich_vollstaendig
    assert len(ergebnis.geschriebene_transaktion_ids) == 1


# -- Rückprüfung 2: zusammengesetzte Originalidentität ---------------------------


def test_sammelreferenz_in_verschiedenen_ntry_ueber_statementposition_eindeutig(session_factory, konto, ctx):
    camt = _camt(_stmt(
        _ntry(10_000, "2026-04-07", native="BATCH-A", ref="Miete synthetisch A", iban=MIETER_A),
        _ntry(2_000, "2026-04-07", native="BATCH-A", ref="Parkplatz synthetisch B", iban=MIETER_B),
        stmt_id="REPORT-42",
    ))
    plan, ergebnis = _plan_und_apply(session_factory, ctx, camt)
    assert [e.status for e in plan.eintraege] == ["NEU", "NEU"] and not plan.konflikte
    identitaet = plan.to_dict()["eintraege"][1]["identitaet"]
    assert identitaet["art"] == ei.ART_ACCTSVCRREF_POSITION
    assert identitaet["original_acct_svcr_ref"] == "BATCH-A"
    assert identitaet["stmt_id"] == "REPORT-42" and identitaet["stmt_ntry_position"] == 2
    assert len(ergebnis.geschriebene_transaktion_ids) == 2
    with session_factory() as session:
        zeilen = session.execute(select(BankTransaktionTable).order_by(BankTransaktionTable.id)).scalars().all()
    assert all(z.import_id.startswith("BK-7DI-1:CAMT053:BATCH-A#S") for z in zeilen)
    assert len({z.import_id for z in zeilen}) == 2
    assert all(z.roh_zeile.endswith("native_id=BATCH-A") for z in zeilen)  # Herkunft erhalten

    replay, replay_ergebnis = _plan_und_apply(session_factory, ctx, camt)
    assert [e.status for e in replay.eintraege] == ["VORHANDEN", "VORHANDEN"]
    assert replay_ergebnis.geschriebene_transaktion_ids == ()
    assert _anzahl(session_factory, BankTransaktionTable) == 2


def test_identische_betraege_gleicher_sammelref_bleiben_getrennte_zeilen(session_factory, konto, ctx):
    gleich = dict(native="BATCH-A", ref="Miete synthetisch", iban=MIETER_A)
    camt = _camt(_stmt(_ntry(10_000, "2026-04-07", **gleich), _ntry(10_000, "2026-04-07", **gleich),
                       stmt_id="REPORT-42"))
    plan, ergebnis = _plan_und_apply(session_factory, ctx, camt)
    assert [e.status for e in plan.eintraege] == ["NEU", "NEU"]
    assert len(ergebnis.geschriebene_transaktion_ids) == 2
    replay = _plan(session_factory, ctx, camt)
    assert [e.status for e in replay.eintraege] == ["VORHANDEN", "VORHANDEN"]
    assert _anzahl(session_factory, BankTransaktionTable) == 2


def test_identische_zahlungen_ohne_referenz_in_getrennten_ntry(session_factory, konto, ctx):
    gleich = dict(ref=REF_A, iban=MIETER_A)
    camt = _camt(_stmt(_ntry(60_000, "2026-04-06", **gleich), _ntry(60_000, "2026-04-06", **gleich),
                       stmt_id="REPORT-43"))
    plan, ergebnis = _plan_und_apply(session_factory, ctx, camt)
    assert [e.identitaet_art for e in plan.eintraege] == [ei.ART_POSITION, ei.ART_POSITION]
    assert [e.status for e in plan.eintraege] == ["NEU", "NEU"]
    assert len(ergebnis.geschriebene_transaktion_ids) == 2  # trotz gleichem Fingerprint nicht verschmolzen
    with session_factory() as session:
        zeilen = session.execute(select(BankTransaktionTable)).scalars().all()
    assert all(not z.hat_native_id and z.fingerprint_hash for z in zeilen)
    assert all(":CAMT053:NOID:EBICS#S" in z.import_id for z in zeilen)

    replay, replay_ergebnis = _plan_und_apply(session_factory, ctx, camt)
    assert [e.status for e in replay.eintraege] == ["VORHANDEN", "VORHANDEN"]
    assert replay_ergebnis.geschriebene_transaktion_ids == ()
    assert _anzahl(session_factory, BankTransaktionTable) == 2


def test_noid_sammelposten_ohne_eigene_acctsvcrref_wiederholung(session_factory, konto, ctx, basis_vertrag):
    camt = _camt(_stmt(_ntry(30_000, "2026-04-07", ntry_acct_ref="SAMMEL-SYN-1", teile=[
        {"betrag": 10_000, "ref": "Teil A synthetisch", "iban": MIETER_A},
        {"betrag": 20_000, "ref": "Teil B synthetisch", "iban": MIETER_B},
    ]), stmt_id="REPORT-45"))
    op_vorher = _anzahl(session_factory, OPPositionTable)
    plan, ergebnis = _plan_und_apply(session_factory, ctx, camt)
    assert [e.status for e in plan.eintraege] == ["NEU", "NEU"]
    assert plan.to_dict()["eintraege"][0]["identitaet"]["ntry_acct_svcr_ref"] == "SAMMEL-SYN-1"
    assert len(ergebnis.geschriebene_transaktion_ids) == 2

    replay, replay_ergebnis = _plan_und_apply(session_factory, ctx, camt)
    assert [e.status for e in replay.eintraege] == ["VORHANDEN", "VORHANDEN"]
    assert replay_ergebnis.geschriebene_transaktion_ids == ()
    assert _anzahl(session_factory, BankTransaktionTable) == 2
    assert _anzahl(session_factory, OPPositionTable) == op_vorher
    assert _anzahl(session_factory, ZuordnungTable) == 0


def test_geaenderter_beleg_unter_gleicher_originalidentitaet_ist_konflikt(session_factory, konto, ctx):
    original = _camt(_stmt(
        _ntry(10_000, "2026-04-07", native="BATCH-A", ref="Miete synthetisch A", iban=MIETER_A),
        _ntry(2_000, "2026-04-07", native="BATCH-A", ref="Parkplatz synthetisch B", iban=MIETER_B),
        stmt_id="REPORT-42",
    ))
    _plan_und_apply(session_factory, ctx, original)
    geaendert = _camt(_stmt(
        _ntry(10_000, "2026-04-07", native="BATCH-A", ref="Miete synthetisch A", iban=MIETER_A),
        _ntry(2_500, "2026-04-07", native="BATCH-A", ref="Parkplatz synthetisch B", iban=MIETER_B),
        stmt_id="REPORT-42",
    ))
    plan = _plan(session_factory, ctx, geaendert)
    assert [e.status for e in plan.eintraege] == ["VORHANDEN", "KONFLIKT"]
    with pytest.raises(EbicsPlanNichtAnwendbarError):
        _apply(session_factory, ctx, geaendert, plan.plan_hash)
    assert _anzahl(session_factory, BankTransaktionTable) == 2


def test_geaenderte_positionszeile_ohne_referenz_ist_konflikt(session_factory, konto, ctx):
    _plan_und_apply(session_factory, ctx, _camt(_stmt(_ntry(60_000, "2026-04-06", ref=REF_A), stmt_id="REPORT-43")))

    betrag_geaendert = _camt(_stmt(_ntry(65_000, "2026-04-06", ref=REF_A), stmt_id="REPORT-43"))
    assert _plan(session_factory, ctx, betrag_geaendert).eintraege[0].grund_code == ei.GRUND_NATIVE_ID_WIDERSPRUCH

    referenz_nachgereicht = _camt(_stmt(_ntry(60_000, "2026-04-06", native="NACHGEREICHT", ref=REF_A),
                                        stmt_id="REPORT-43"))
    eintrag = _plan(session_factory, ctx, referenz_nachgereicht).eintraege[0]
    assert eintrag.status == "KONFLIKT" and eintrag.grund_code == ei.GRUND_ORIGINALPOSITION_WIDERSPRUCH


def test_neue_position_im_selben_statement_ist_eigenstaendig(session_factory, konto, ctx):
    _plan_und_apply(session_factory, ctx, _camt(_stmt(_ntry(60_000, "2026-04-06", ref=REF_A), stmt_id="REPORT-44")))
    erweitert = _camt(_stmt(_ntry(60_000, "2026-04-06", ref=REF_A), _ntry(60_000, "2026-04-06", ref=REF_A),
                            stmt_id="REPORT-44"))
    plan, ergebnis = _plan_und_apply(session_factory, ctx, erweitert)
    assert [e.status for e in plan.eintraege] == ["VORHANDEN", "NEU"]
    assert len(ergebnis.geschriebene_transaktion_ids) == 1
    assert _anzahl(session_factory, BankTransaktionTable) == 2


def test_andere_bankidentitaet_wird_nie_still_als_vorhanden_behandelt(session_factory, konto, ctx):
    _plan_und_apply(session_factory, ctx, _camt(_stmt(
        _ntry(10_000, "2026-04-07", native="PAYMENT-A", ref=REF_A, iban=MIETER_A), stmt_id="ORIGINAL-1")))

    gleich_aussehend = _camt(_stmt(
        _ntry(10_000, "2026-04-07", native="PAYMENT-B", ref=REF_A, iban=MIETER_A), stmt_id="ORIGINAL-2"))
    plan, ergebnis = _plan_und_apply(session_factory, ctx, gleich_aussehend)
    assert plan.eintraege[0].status == "UNKLAR"
    assert plan.eintraege[0].grund_code == ei.GRUND_ANDERE_BANKIDENTITAET
    assert ergebnis.geschriebene_transaktion_ids == ()

    andere_zahlung = _camt(_stmt(
        _ntry(10_000, "2026-04-07", native="PAYMENT-C", ref=REF_B, iban=MIETER_B), stmt_id="ORIGINAL-3"))
    plan = _plan(session_factory, ctx, andere_zahlung)
    assert plan.eintraege[0].status == "NEU"  # getrennte Bankidentität, kein Gleichbetrag-Klärfall


# -- gleiche Beträge ------------------------------------------------------------


def test_zwei_legitime_gleich_hohe_vorgaenge_werden_nicht_zusammengeworfen(
    session_factory, konto, ctx, bank_service
):
    camt = _camt(_stmt(
        _ntry(60_000, "2026-04-06", native="EB-1", ref=REF_A, iban=MIETER_A),
        _ntry(60_000, "2026-04-06", native="EB-2", ref=REF_B, iban=MIETER_B),
    ))
    plan = _plan(session_factory, ctx, camt)
    assert [e.status for e in plan.eintraege] == ["NEU", "NEU"]

    # Bestand enthält genau EINE davon (CSV) -> nur diese ist vorhanden, die andere bleibt neu.
    _csv(bank_service, ctx, konto, [f"600.00,2026-04-06,,{REF_A},{MIETER_A},"])
    plan, ergebnis = _plan_und_apply(session_factory, ctx, camt)
    assert [e.status for e in plan.eintraege] == ["VORHANDEN", "NEU"]
    assert len(ergebnis.geschriebene_transaktion_ids) == 1
    assert _anzahl(session_factory, BankTransaktionTable) == 2


def test_ein_bestand_zwei_gleiche_dateizeilen_ist_mehrdeutig(session_factory, konto, ctx, bank_service):
    _csv(bank_service, ctx, konto, [f"600.00,2026-04-06,,{REF_A},{MIETER_A},"])
    camt = _camt(_stmt(
        _ntry(60_000, "2026-04-06", native="EB-1", ref=REF_A, iban=MIETER_A),
        _ntry(60_000, "2026-04-06", native="EB-2", ref=REF_A, iban=MIETER_A),
    ))
    plan = _plan(session_factory, ctx, camt)
    assert {e.grund_code for e in plan.eintraege} == {ei.GRUND_MEHRDEUTIG}


def test_unterbestimmter_gleichbetrag_wird_unklar_und_sichere_zeilen_importiert(
    session_factory, konto, ctx, bank_service
):
    _csv(bank_service, ctx, konto, ["600.00,2026-04-06,,Dauerauftrag,,"])  # keine IBAN, andere Referenz
    camt = _camt(_stmt(
        _ntry(60_000, "2026-04-06", native="EB-1", ref=REF_A, iban=MIETER_A),
        _ntry(45_000, "2026-04-06", native="EB-2", ref=REF_B, iban=MIETER_B),
    ))
    plan, ergebnis = _plan_und_apply(session_factory, ctx, camt)
    assert [e.status for e in plan.eintraege] == ["UNKLAR", "NEU"]
    assert plan.eintraege[0].grund_code == ei.GRUND_GLEICHBETRAG
    assert plan.anwendbar and not plan.abgleich_vollstaendig
    daten = ergebnis.to_dict()
    assert daten["anzahl_geschrieben"] == 1
    assert [z["position"] for z in daten["zurueckgehalten"]] == [1]
    assert daten["abgleich_vollstaendig"] is False
    assert _anzahl(session_factory, BankTransaktionTable) == 2


def test_datumsverschiebung_ohne_exakten_tag_wird_unklar(session_factory, konto, ctx, bank_service):
    _csv(bank_service, ctx, konto, [f"600.00,2026-04-03,,{REF_A},{MIETER_A},"])
    camt = _camt(_stmt(_ntry(60_000, "2026-04-06", native="EB-1", ref=REF_A, iban=MIETER_A)))
    plan = _plan(session_factory, ctx, camt)
    assert plan.eintraege[0].grund_code == ei.GRUND_GLEICHBETRAG
    # außerhalb der Toleranz: kein Kandidat mehr
    assert _plan(session_factory, ctx, camt, datum_toleranz_tage=2).eintraege[0].status == "NEU"


def test_dateiintern_ununterscheidbar_ohne_native_id_und_ohne_statement_id(session_factory, konto, ctx):
    camt = _camt(_stmt(
        _ntry(60_000, "2026-04-06", ref=REF_A, iban=MIETER_A),
        _ntry(60_000, "2026-04-06", ref=REF_A, iban=MIETER_A),
        stmt_id=None,
    ))
    plan = _plan(session_factory, ctx, camt)
    assert {e.grund_code for e in plan.eintraege} == {ei.GRUND_DATEI_INTERN}


# -- Sammelbuchung --------------------------------------------------------------


def test_bestehender_sammelkopf_und_neue_einzelposten_werden_zurueckgehalten(
    session_factory, konto, ctx, bank_service
):
    _csv(bank_service, ctx, konto, ["1500.00,2026-04-06,,SEPA Sammelgutschrift,,"])
    camt = _camt(_stmt(_ntry(150_000, "2026-04-06", teile=[
        {"betrag": 100_000, "ref": REF_A, "iban": MIETER_A, "native": "EB-T1"},
        {"betrag": 50_000, "ref": REF_B, "iban": MIETER_B, "native": "EB-T2"},
    ])))
    plan, ergebnis = _plan_und_apply(session_factory, ctx, camt)
    assert [e.grund_code for e in plan.eintraege] == [ei.GRUND_SAMMELBUCHUNG, ei.GRUND_SAMMELBUCHUNG]
    assert [e.txdtls_index for e in plan.eintraege] == [1, 2]
    assert ergebnis.geschriebene_transaktion_ids == ()
    assert _anzahl(session_factory, BankTransaktionTable) == 1


def test_bestehende_einzelposten_und_neuer_sammelkopf_wird_zurueckgehalten(
    session_factory, konto, ctx, bank_service
):
    _csv(bank_service, ctx, konto, [
        f"1000.00,2026-04-06,,{REF_A},{MIETER_A},",
        f"500.00,2026-04-06,,{REF_B},{MIETER_B},",
    ])
    camt = _camt(_stmt(_ntry(150_000, "2026-04-06", native="EB-S", ref="SEPA Sammelgutschrift")))
    plan = _plan(session_factory, ctx, camt)
    assert plan.eintraege[0].grund_code == ei.GRUND_EINZELPOSTEN
    assert len(plan.eintraege[0].bestehende_transaktion_ids) == 2


def test_sammelbuchung_mit_unvollstaendiger_summe_lehnt_datei_ab(session_factory, konto, ctx):
    from mietinkasso.bank.importer import CamtMehrteiligeBuchungError

    camt = _camt(_stmt(_ntry(150_000, "2026-04-06", teile=[
        {"betrag": 100_000, "ref": REF_A}, {"betrag": 40_000, "ref": REF_B},
    ])))
    with pytest.raises(CamtMehrteiligeBuchungError):
        _plan(session_factory, ctx, camt)


# -- Konto / Format -------------------------------------------------------------


def test_fremdkonto_im_dokument_lehnt_gesamte_datei_ab(session_factory, konto, ctx):
    camt = _camt(
        _stmt(_ntry(60_000, "2026-04-06", native="EB-1", ref=REF_A)),
        _stmt(_ntry(1_000, "2026-04-06", native="EB-X"), iban=FREMD_IBAN, stmt_id="STMT-SYN-2"),
    )
    with pytest.raises(CamtKontoMismatchError):
        _plan(session_factory, ctx, camt)


def test_erwartete_iban_muss_zum_db_konto_passen(session_factory, konto, ctx):
    camt = _camt(_stmt(_ntry(60_000, "2026-04-06", native="EB-1"), iban=FREMD_IBAN))
    with pytest.raises(EbicsKontoAbweichungError):
        plan_ebics_camt053(session_factory=session_factory, bank_konto_id="BK-7DI-1", erwartete_iban=FREMD_IBAN,
                           camt_bytes=camt, ctx=ctx)
    with pytest.raises(EbicsKontoAbweichungError):
        plan_ebics_camt053(session_factory=session_factory, bank_konto_id="BK-UNBEKANNT", erwartete_iban=IBAN,
                           camt_bytes=camt, ctx=ctx)


@pytest.mark.parametrize(
    "camt, fehler",
    [
        (_camt(_stmt(_ntry(60_000, "2026-04-06", native="EB-1", sts="PDNG"))), EbicsDokumentAbgelehntError),
        (_camt(_stmt(_ntry(60_000, "2026-04-06", native="EB-1")), ns="camt.052.001.02"), EbicsDokumentAbgelehntError),
        (_camt(_stmt(_ntry(60_000, "2026-04-06", native="EB-1", ccy=None))), EbicsDokumentAbgelehntError),
        (_camt(_stmt(_ntry(60_000, "2026-04-06", native="EB-1", ccy="USD"))), FremdwaehrungNichtUnterstuetztError),
        (b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><Document/>', EbicsDokumentAbgelehntError),
        (b"<kein xml", EbicsDokumentAbgelehntError),
    ],
)
def test_nur_finale_eur_camt053(session_factory, konto, ctx, camt, fehler):
    with pytest.raises(fehler):
        _plan(session_factory, ctx, camt)


# -- Berechtigungen / Atomarität / Planbindung ----------------------------------


def test_berechtigungen(session_factory, konto, ctx_factory):
    camt = _camt(_stmt(_ntry(60_000, "2026-04-06", native="EB-1", ref=REF_A)))
    with pytest.raises(CrossTenantError):
        _plan(session_factory, ctx_factory("MABAU"), camt)

    lesend = ctx_factory("7DI", Rolle.LESEZUGRIFF)
    plan = _plan(session_factory, lesend, camt)  # Lesen erlaubt
    with pytest.raises(CrossTenantError):
        _apply(session_factory, lesend, camt, plan.plan_hash)
    with pytest.raises(CrossTenantError):
        _apply(session_factory, ctx_factory("MABAU"), camt, plan.plan_hash)
    assert _anzahl(session_factory, BankTransaktionTable) == 0


def test_atomarer_rollback_bei_fehler_mitten_im_schreiben(session_factory, konto, ctx, monkeypatch):
    camt = _camt(_stmt(
        _ntry(60_000, "2026-04-06", native="EB-1", ref=REF_A),
        _ntry(45_000, "2026-04-06", native="EB-2", ref=REF_B),
    ))
    plan = _plan(session_factory, ctx, camt)
    original = BankImportService._speichere_roh
    aufrufe = {"n": 0}

    def _zweiter_schlaegt_fehl(self, *args, **kwargs):
        aufrufe["n"] += 1
        if aufrufe["n"] == 2:
            raise RuntimeError("synthetischer Schreibfehler")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(BankImportService, "_speichere_roh", _zweiter_schlaegt_fehl)
    with pytest.raises(RuntimeError):
        _apply(session_factory, ctx, camt, plan.plan_hash)
    assert _anzahl(session_factory, BankTransaktionTable) == 0


def test_geaenderte_datenbank_oder_datei_zwischen_plan_und_apply(session_factory, konto, ctx, bank_service):
    camt = _camt(_stmt(_ntry(60_000, "2026-04-06", native="EB-1", ref=REF_A)))
    plan = _plan(session_factory, ctx, camt)

    _csv(bank_service, ctx, konto, ["1.00,2026-01-01,,Fremdvorgang,,"])  # Bestand ändert sich
    with pytest.raises(EbicsPlanHashAbweichungError):
        _apply(session_factory, ctx, camt, plan.plan_hash)
    assert _anzahl(session_factory, BankTransaktionTable) == 1

    camt_geaendert = _camt(_stmt(_ntry(60_000, "2026-04-06", native="EB-1", ref=REF_A + " ")))
    neu = _plan(session_factory, ctx, camt)
    with pytest.raises(EbicsPlanHashAbweichungError):
        _apply(session_factory, ctx, camt_geaendert, neu.plan_hash)
    with pytest.raises(EbicsPlanHashAbweichungError):
        _apply(session_factory, ctx, camt, "")
    assert _anzahl(session_factory, BankTransaktionTable) == 1


# -- CLI (Datei-SQLite, Read-only-Plan) -----------------------------------------

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ebics_ingest.py"
_SPEC = importlib.util.spec_from_file_location("ebics_ingest_cli", _SCRIPT_PATH)
_CLI = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_CLI)


@pytest.fixture
def datei_db(tmp_path):
    pfad = tmp_path / "bank.db"
    engine = build_engine(f"sqlite:///{pfad}")
    Base.metadata.create_all(engine)
    engine.dispose()
    factory = build_session_factory(f"sqlite:///{pfad}")
    StammdatenRepository(factory).upsert_gesellschaft(id="7DI", name="7D Immobilien GmbH")
    BankRepository(factory).upsert_bank_konto(id="BK-7DI-1", gesellschaft_id="7DI", iban=IBAN, bezeichnung="synthetisch")
    kontext = tmp_path / "operator.json"
    kontext.write_text(json.dumps({"user_id": "op-test", "rolle": "BUCHHALTUNG", "gesellschaft_ids": ["7DI"]}),
                       encoding="utf-8")
    return pfad, factory, kontext


def _cli_args(befehl, pfad, kontext, datei, *extra):
    return [befehl, "--database-url", f"sqlite:///{pfad}", "--bank-id", "BK-7DI-1", "--erwartete-iban", IBAN,
            "--datei", str(datei), "--kontext-datei", str(kontext), *extra]


def test_cli_plan_readonly_und_apply_mit_hash(datei_db, tmp_path, capsys):
    pfad, factory, kontext = datei_db
    datei = tmp_path / "auszug.xml"
    datei.write_bytes(_camt(_stmt(_ntry(60_000, "2026-04-06", native="EB-1", ref=REF_A))))
    vorher = pfad.read_bytes()

    assert _CLI.main(_cli_args("plan", pfad, kontext, datei)) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["zusammenfassung"]["neu"] == 1 and plan["eintraege"][0]["betrag_cent"] == 60_000
    assert pfad.read_bytes() == vorher  # read-only

    assert _CLI.main(_cli_args("apply", pfad, kontext, datei, "--expected-plan-hash", "falsch")) == 2
    assert json.loads(capsys.readouterr().out)["fehler"] == "EbicsPlanHashAbweichungError"

    assert _CLI.main(_cli_args("apply", pfad, kontext, datei, "--expected-plan-hash", plan["plan_hash"])) == 0
    ergebnis = json.loads(capsys.readouterr().out)
    assert ergebnis["anzahl_geschrieben"] == 1
    assert _anzahl(factory, BankTransaktionTable) == 1

    # alter Hash nach Schreiben: Bestand hat sich geändert -> verweigert, nichts doppelt
    assert _CLI.main(_cli_args("apply", pfad, kontext, datei, "--expected-plan-hash", plan["plan_hash"])) == 2
    capsys.readouterr()
    assert _anzahl(factory, BankTransaktionTable) == 1


def test_cli_verlangt_konfigurierten_kontext_und_bestehende_db(datei_db, tmp_path, capsys, monkeypatch):
    pfad, _factory, kontext = datei_db
    datei = tmp_path / "auszug.xml"
    datei.write_bytes(_camt(_stmt(_ntry(60_000, "2026-04-06", native="EB-1"))))
    monkeypatch.delenv(_CLI._KONTEXT_ENV, raising=False)

    args = _cli_args("plan", pfad, kontext, datei)
    ohne_kontext = args[: args.index("--kontext-datei")]
    assert _CLI.main(ohne_kontext) == 3
    assert "Operator-Kontext" in json.loads(capsys.readouterr().out)["meldung"]

    assert _CLI.main(_cli_args("plan", tmp_path / "fehlt.db", kontext, datei)) == 3
    capsys.readouterr()
    assert not (tmp_path / "fehlt.db").exists()

    admin_ohne_scope = tmp_path / "admin.json"
    admin_ohne_scope.write_text(json.dumps({"user_id": "x", "rolle": "ADMIN", "gesellschaft_ids": None}), encoding="utf-8")
    assert _CLI.main(_cli_args("plan", pfad, admin_ohne_scope, datei)) == 3
    capsys.readouterr()
