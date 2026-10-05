"""Bankquellenbindung Objekt -> Bankquelle -> Bankkonto (siehe
docs/hausverwaltung/BANKQUELLENBINDUNG.md): Konfiguration, gebundener
Import, Schutz der Zahlungszuordnung und geschützter Abruf über einen
synthetischen Fake-Adapter. Ausschließlich synthetische Daten/IBANs, keine
echte Bank-/Anbieterverbindung."""

from __future__ import annotations

import dataclasses
from datetime import date
from types import SimpleNamespace
from xml.sax.saxutils import escape

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from mietinkasso.auth.service import AuthContext
from mietinkasso.bank import quellenbindung as quellenbindung_modul
from mietinkasso.bank import service as bank_service_modul
from mietinkasso.bank.importer import CamtKontoMismatchError, CsvKontoMismatchError, CsvSpaltenMapping
from mietinkasso.bank.quellen_models import BankQuelleTable, BankQuellenBindungTable
from mietinkasso.bank.quellenabruf import (
    AnbieterAntwortError,
    AnbieterKontoInfo,
    AnbieterLieferung,
    GeschuetzterBankabruf,
)
from mietinkasso.bank.quellenbindung import (
    STAND_NEU,
    BankquellenBindungError,
    QuellenbindungKonfliktError,
    QuellenbindungUngueltigError,
)
from mietinkasso.bank.repository import BankRepository
from mietinkasso.bank.service import BankImportService
from mietinkasso.domain.enums import OPTyp, Rolle
from mietinkasso.domain.exceptions import CrossTenantError, OptimistischerLockKonfliktError
from mietinkasso.infrastructure.db.base import Base
from mietinkasso.infrastructure.db.session import build_engine
from mietinkasso.infrastructure.db.tables import (
    AuditEventTable,
    BankKontoTable,
    BankTransaktionTable,
    GesellschaftTable,
    OPPositionTable,
    ZuordnungTable,
)

IBAN_A = "AT000000000000000001"
IBAN_B = "AT000000000000000002"
IBAN_C = "AT000000000000000003"

MAPPING = CsvSpaltenMapping(betrag="betrag", buchungsdatum="datum", referenz="referenz", eindeutige_referenz="bank_id")
MAPPING_IBAN = dataclasses.replace(MAPPING, eigene_iban="konto")


@pytest.fixture
def welt(session_factory, stammdaten_repo, op_service):
    """7DI: Objekte 601/616/617 (je ein Vertrag/Konto), 107 ausgeschlossen;
    MABAU: Objekt 901. Bankkonten BK-MIETE und BK-ANDERE (beide 7DI),
    BK-MABAU. Keine Bindung vorkonfiguriert."""

    s = stammdaten_repo
    s.upsert_gesellschaft(id="7DI", name="Synthetik 7DI")
    s.upsert_gesellschaft(id="MABAU", name="Synthetik MABAU")
    for objekt_id, ges, ausgeschlossen in (
        ("601", "7DI", False), ("616", "7DI", False), ("617", "7DI", False), ("107", "7DI", True), ("901", "MABAU", False),
    ):
        s.upsert_objekt(id=objekt_id, gesellschaft_id=ges, bezeichnung=f"Synthetik {objekt_id}", ausgeschlossen=ausgeschlossen)
    konten = {}
    for objekt_id, ges in (("601", "7DI"), ("616", "7DI"), ("617", "7DI"), ("901", "MABAU")):
        s.upsert_einheit(id=f"E-{objekt_id}", objekt_id=objekt_id, bezeichnung="Top 1", nutzungsstatus="DAUERVERMIETUNG")
        s.upsert_debitor(id=f"D-{objekt_id}", name=f"Synthetik Mieter {objekt_id}")
        s.upsert_vertrag(
            id=f"V-{objekt_id}", einheit_id=f"E-{objekt_id}", debitor_id=f"D-{objekt_id}", gesellschaft_id=ges,
            rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
        )
        konten[objekt_id] = s.get_or_create_konto(vertrag=s.get_vertrag(f"V-{objekt_id}"))
    bank_repo = BankRepository(session_factory)
    bank_repo.upsert_bank_konto(id="BK-MIETE", gesellschaft_id="7DI", iban=IBAN_A, bezeichnung="Synthetik Miete")
    bank_repo.upsert_bank_konto(id="BK-ANDERE", gesellschaft_id="7DI", iban=IBAN_B, bezeichnung="Synthetik andere")
    bank_repo.upsert_bank_konto(id="BK-MABAU", gesellschaft_id="MABAU", iban=IBAN_C, bezeichnung="Synthetik MABAU")
    service = BankImportService(bank_repo, s, op_service)
    return SimpleNamespace(bank_repo=bank_repo, bank=service, qb=service.quellenbindung, konten=konten, op=op_service)


@pytest.fixture
def ctx(ctx_factory) -> AuthContext:
    return ctx_factory("7DI")


def _binden(welt, ctx, *, objekt_id="601", stand=STAND_NEU, **anders):
    args = dict(
        ctx=ctx, objekt_id=objekt_id, anbieter="SYN-ANBIETER", zugang_ref="ZUGANG-1", konto_ref="KONTO-1",
        bank_konto_id="BK-MIETE", gesellschaft_id="7DI", iban=IBAN_A, kontorolle="MIETE",
        nachweis_ref="SYN-BELEG-1", erwarteter_stand=stand,
    )
    args.update(anders)
    return welt.qb.binden(**args)


def _anzahl(session_factory, tabelle) -> int:
    with session_factory() as session:
        return session.execute(select(func.count()).select_from(tabelle)).scalar_one()


def _stand(session_factory) -> dict:
    return {
        t.__tablename__: _anzahl(session_factory, t)
        for t in (BankQuelleTable, BankQuellenBindungTable, AuditEventTable, BankTransaktionTable, ZuordnungTable, OPPositionTable)
    }


def _csv(zeilen, *, iban=IBAN_A) -> str:
    return "betrag,datum,referenz,bank_id,konto\n" + "".join(
        f"{betrag},2026-04-06,{referenz},{native_id},{iban}\n" for native_id, betrag, referenz in zeilen
    )


def _camt(zeilen, *, iban=IBAN_A) -> bytes:
    ntry = "".join(
        f"""
      <Ntry><Amt Ccy="EUR">{betrag}</Amt><CdtDbtInd>CRDT</CdtDbtInd><BookgDt><Dt>2026-04-06</Dt></BookgDt>
        <NtryDtls><TxDtls><RmtInf><Ustrd>{escape(referenz)}</Ustrd></RmtInf><AcctSvcrRef>{escape(native_id)}</AcctSvcrRef></TxDtls></NtryDtls>
      </Ntry>"""
        for native_id, betrag, referenz in zeilen
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"><BkToCstmrStmt><Stmt>
  <Acct><Id><IBAN>{iban}</IBAN></Id></Acct>{ntry}
</Stmt></BkToCstmrStmt></Document>""".encode("utf-8")


def _import_csv(welt, ctx, bank_konto_id, zeilen, *, kontext=None, mapping=MAPPING_IBAN, iban=None):
    bank_konto = welt.bank_repo.get_bank_konto(bank_konto_id)
    return welt.bank.importiere_csv(
        ctx=ctx, bank_konto=bank_konto, text=_csv(zeilen, iban=iban or bank_konto.iban), mapping=mapping,
        quellen_kontext=kontext,
    )


# -- Konfiguration --------------------------------------------------------------


def test_erstbindung_mit_audit_und_exakter_retry_ist_noop(welt, ctx, session_factory):
    kontext = _binden(welt, ctx)
    assert (kontext.objekt_id, kontext.revision, kontext.bank_konto_id) == ("601", 1, "BK-MIETE")
    assert (kontext.iban_norm, kontext.kontorolle, kontext.gesellschaft_id) == (IBAN_A, "MIETE", "7DI")
    stand = _stand(session_factory)
    assert stand["bank_quellen"] == 1 and stand["bank_quellen_bindungen"] == 1
    with session_factory() as session:
        audit = session.execute(select(AuditEventTable)).scalars().all()
    assert [(a.entity_typ, a.entity_id, a.aktion) for a in audit] == [("bank_quellen_bindung", "601", "gebunden")]
    assert IBAN_A not in str(audit[0].payload)  # nur maskiert

    assert _binden(welt, ctx) == kontext
    assert _binden(welt, ctx, stand=kontext.token) == kontext
    assert _stand(session_factory) == stand
    assert welt.qb.kontext_fuer_objekt(ctx=ctx, objekt_id="601") == kontext


@pytest.mark.parametrize(
    "anders,fehler",
    [
        (dict(iban=IBAN_B), QuellenbindungUngueltigError),
        (dict(gesellschaft_id="MABAU"), CrossTenantError),
        (dict(bank_konto_id="BK-MABAU", iban=IBAN_C), CrossTenantError),
        (dict(bank_konto_id="BK-GIBT-ES-NICHT"), QuellenbindungUngueltigError),
        (dict(kontorolle="KREDIT"), QuellenbindungUngueltigError),
        (dict(kontorolle="KAUTION"), QuellenbindungUngueltigError),
        (dict(kontorolle="UNBEKANNT"), QuellenbindungUngueltigError),
        (dict(kontorolle="GIRO"), QuellenbindungUngueltigError),
        (dict(anbieter=" "), QuellenbindungUngueltigError),
        (dict(nachweis_ref=""), QuellenbindungUngueltigError),
        (dict(zugang_ref="geheimes passwort"), QuellenbindungUngueltigError),
        (dict(iban="kein iban"), QuellenbindungUngueltigError),
        (dict(objekt_id="999"), QuellenbindungUngueltigError),
        (dict(objekt_id="107"), QuellenbindungUngueltigError),
        (dict(objekt_id="901"), CrossTenantError),
        (dict(erwarteter_stand=""), QuellenbindungUngueltigError),
    ],
)
def test_ungueltige_konfiguration_schreibt_nichts(welt, ctx, admin_ctx, session_factory, anders, fehler):
    vorher = _stand(session_factory)
    with pytest.raises(fehler):
        _binden(welt, admin_ctx if anders.get("objekt_id") == "901" else ctx, **anders)
    assert _stand(session_factory) == vorher


def test_anbieter_tupel_nie_auf_zweites_bankkonto(welt, ctx, session_factory):
    _binden(welt, ctx)
    vorher = _stand(session_factory)
    with pytest.raises(QuellenbindungKonfliktError):
        _binden(welt, ctx, objekt_id="616", bank_konto_id="BK-ANDERE", iban=IBAN_B)
    assert _stand(session_factory) == vorher


def test_rechte_lesezugriff_und_fremde_gesellschaft(welt, ctx, ctx_factory, session_factory):
    with pytest.raises(CrossTenantError):
        _binden(welt, ctx_factory("7DI", Rolle.LESEZUGRIFF))
    with pytest.raises(CrossTenantError):
        _binden(welt, ctx_factory("MABAU"))
    assert _stand(session_factory)["bank_quellen"] == 0

    kontext = _binden(welt, ctx)
    leser = ctx_factory("7DI", Rolle.LESEZUGRIFF)
    assert {s.objekt_id for s in welt.qb.uebersicht(ctx=leser)} == {"601", "616", "617"}  # 107 ausgeschlossen
    assert {s.objekt_id for s in welt.qb.uebersicht(ctx=ctx_factory("MABAU"))} == {"901"}
    with pytest.raises(CrossTenantError):
        welt.qb.kontext_fuer_objekt(ctx=ctx_factory("MABAU"), objekt_id="601")
    with pytest.raises(CrossTenantError):
        welt.qb.widerrufen(ctx=leser, objekt_id="601", nachweis_ref="x", erwarteter_stand=kontext.token)


def test_neubindung_nur_mit_aktuellem_stand_und_atomar(welt, ctx, session_factory):
    erste = _binden(welt, ctx)
    vorher = _stand(session_factory)
    andere = dict(konto_ref="KONTO-2", bank_konto_id="BK-ANDERE", iban=IBAN_B, nachweis_ref="SYN-BELEG-2")
    for veraltet in (STAND_NEU, "R1-" + "0" * 64, "R2-" + erste.fingerprint):
        with pytest.raises(OptimistischerLockKonfliktError):
            _binden(welt, ctx, stand=veraltet, **andere)
    # Die im selben Versuch angelegte neue Quelle ist mit zurückgerollt.
    assert _stand(session_factory) == vorher

    zweite = _binden(welt, ctx, stand=erste.token, **andere)
    assert (zweite.revision, zweite.bank_konto_id) == (2, "BK-ANDERE")
    with pytest.raises(OptimistischerLockKonfliktError):
        _binden(welt, ctx, stand=erste.token, konto_ref="KONTO-3", nachweis_ref="SYN-BELEG-3")
    with pytest.raises(QuellenbindungKonfliktError):
        _binden(welt, ctx, stand=zweite.token, **dict(andere, nachweis_ref="anderer Beleg"))


def test_auditfehler_rollt_quelle_und_bindung_zurueck(welt, ctx, session_factory, monkeypatch):
    def _kaputt(**_):
        raise RuntimeError("synthetischer Auditfehler")

    vorher = _stand(session_factory)
    monkeypatch.setattr(quellenbindung_modul, "AuditEventTable", _kaputt)
    with pytest.raises(RuntimeError):
        _binden(welt, ctx)
    assert _stand(session_factory) == vorher and vorher["bank_quellen"] == 0


def test_geteilte_quelle_fuer_mehrere_objekte(welt, ctx, session_factory):
    k601 = _binden(welt, ctx)
    k616 = _binden(welt, ctx, objekt_id="616", nachweis_ref="SYN-BELEG-616")
    assert k601.quelle_id == k616.quelle_id and _anzahl(session_factory, BankQuelleTable) == 1

    zeilen = _import_csv(welt, ctx, "BK-MIETE", [("N-1", "100.00", "Miete"), ("N-2", "80.00", "Miete")], kontext=k616)
    for transaktion, objekt_id in zip(zeilen, ("601", "616")):
        welt.bank.zuordnen_manuell(
            ctx=ctx, transaktion=transaktion, konto=welt.konten[objekt_id], betrag_cent=transaktion.betrag_cent,
            beleg_referenz="synthetisch", vorgang_id=f"SYN-{objekt_id}",
        )
    assert _anzahl(session_factory, ZuordnungTable) == 2


# -- Import -----------------------------------------------------------------------


def test_ungebundener_import_auf_gebundenes_oder_widerrufenes_konto_abgelehnt(welt, ctx, session_factory):
    kontext = _binden(welt, ctx)
    for widerrufen in (False, True):
        if widerrufen:
            welt.qb.widerrufen(ctx=ctx, objekt_id="601", nachweis_ref="SYN-WIDERRUF", erwarteter_stand=kontext.token)
        with pytest.raises(BankquellenBindungError):
            _import_csv(welt, ctx, "BK-MIETE", [("N-1", "10.00", "Miete")])
        with pytest.raises(BankquellenBindungError):
            _import_csv(welt, ctx, "BK-MIETE", [("N-1", "10.00", "Miete")], mapping=MAPPING)
        with pytest.raises(BankquellenBindungError):
            welt.bank.importiere_camt053(
                ctx=ctx, bank_konto=welt.bank_repo.get_bank_konto("BK-MIETE"), xml_bytes=_camt([("N-1", "10.00", "x")]),
            )
    with pytest.raises(BankquellenBindungError):
        _import_csv(welt, ctx, "BK-MIETE", [("N-1", "10.00", "Miete")], kontext=kontext)
    assert _anzahl(session_factory, BankTransaktionTable) == 0
    # Nie konfiguriertes Bankkonto: unverändert manueller Legacy-Import.
    assert len(_import_csv(welt, ctx, "BK-ANDERE", [("L-1", "10.00", "Miete")], mapping=MAPPING)) == 1


def test_gebundener_camt_und_csv_import_mit_eigener_iban(welt, ctx, session_factory):
    kontext = _binden(welt, ctx)
    bank_konto = welt.bank_repo.get_bank_konto("BK-MIETE")
    assert len(welt.bank.importiere_camt053(
        ctx=ctx, bank_konto=bank_konto, xml_bytes=_camt([("C-1", "10.00", "Miete")]), quellen_kontext=kontext,
    )) == 1
    assert len(_import_csv(welt, ctx, "BK-MIETE", [("S-1", "11.00", "Miete")], kontext=kontext)) == 1

    with pytest.raises(BankquellenBindungError):
        _import_csv(welt, ctx, "BK-MIETE", [("S-2", "12.00", "Miete")], kontext=kontext, mapping=MAPPING)
    with pytest.raises(CsvKontoMismatchError):
        _import_csv(welt, ctx, "BK-MIETE", [("S-3", "13.00", "Miete")], kontext=kontext, iban=IBAN_B)
    with pytest.raises(CamtKontoMismatchError):
        welt.bank.importiere_camt053(
            ctx=ctx, bank_konto=bank_konto, xml_bytes=_camt([("C-2", "10.00", "x")], iban=IBAN_B), quellen_kontext=kontext,
        )
    assert _anzahl(session_factory, BankTransaktionTable) == 2


def test_veralteter_oder_gefaelschter_kontext_abgelehnt(welt, ctx, admin_ctx, session_factory, stammdaten_repo):
    kontext = _binden(welt, ctx)
    gefaelscht = [
        dataclasses.replace(kontext, bank_konto_id="BK-ANDERE", iban_norm=IBAN_B),
        dataclasses.replace(kontext, revision=2),
        dataclasses.replace(kontext, fingerprint="0" * 64),
        dataclasses.replace(kontext, objekt_id="616"),
        SimpleNamespace(**dataclasses.asdict(kontext)),
    ]
    for falsch in gefaelscht:
        with pytest.raises(BankquellenBindungError):
            _import_csv(welt, ctx, falsch.bank_konto_id if falsch.bank_konto_id == "BK-ANDERE" else "BK-MIETE",
                        [("F-1", "10.00", "x")], kontext=falsch)
    with pytest.raises(BankquellenBindungError):
        _import_csv(welt, ctx, "BK-ANDERE", [("F-1", "10.00", "x")], kontext=kontext)

    # Änderung am zugrunde liegenden Bankkonto/Objekt entwertet den Kontext.
    welt.bank_repo.upsert_bank_konto(id="BK-MIETE", gesellschaft_id="7DI", iban=IBAN_C, bezeichnung="geändert")
    with pytest.raises(BankquellenBindungError):
        welt.qb.kontext_fuer_objekt(ctx=ctx, objekt_id="601")
    with pytest.raises(BankquellenBindungError):
        _import_csv(welt, ctx, "BK-MIETE", [("F-2", "10.00", "x")], kontext=kontext)
    welt.bank_repo.upsert_bank_konto(id="BK-MIETE", gesellschaft_id="7DI", iban=IBAN_A, bezeichnung="Synthetik Miete")
    stammdaten_repo.upsert_objekt(id="601", gesellschaft_id="MABAU", bezeichnung="umgehängt")
    with pytest.raises(BankquellenBindungError):  # ADMIN: Zugriff ok, Gesellschaftsabweichung sperrt
        _import_csv(welt, admin_ctx, "BK-MIETE", [("F-3", "10.00", "x")], kontext=kontext)
    assert _anzahl(session_factory, BankTransaktionTable) == 0


@pytest.mark.parametrize("aenderung", ["widerruf", "neubindung"])
def test_aenderung_waehrend_import_schreibt_nichts(welt, ctx, session_factory, monkeypatch, aenderung):
    kontext = _binden(welt, ctx)
    original = bank_service_modul.parse_csv

    def _parse_mit_zwischenaenderung(*args, **kwargs):
        if aenderung == "widerruf":
            welt.qb.widerrufen(ctx=ctx, objekt_id="601", nachweis_ref="SYN-MITTEN", erwarteter_stand=kontext.token)
        else:
            _binden(welt, ctx, stand=kontext.token, konto_ref="KONTO-2", nachweis_ref="SYN-MITTEN")
        return original(*args, **kwargs)

    monkeypatch.setattr(bank_service_modul, "parse_csv", _parse_mit_zwischenaenderung)
    with pytest.raises(BankquellenBindungError):
        _import_csv(welt, ctx, "BK-MIETE", [("M-1", "10.00", "Miete")], kontext=kontext)
    assert _anzahl(session_factory, BankTransaktionTable) == 0


# -- Zahlungszuordnung --------------------------------------------------------------


def test_fremdes_bankkonto_gleicher_gesellschaft_nie_zuordenbar(welt, ctx, session_factory):
    # Vor der Bindung (Legacy): Zahlung von BK-ANDERE an Mieter 601 + spätere Rücklastschrift.
    alt, ruecklast = _import_csv(
        welt, ctx, "BK-ANDERE", [("A-1", "100.00", "Miete"), ("A-2", "-100.00", "Retoure")], mapping=MAPPING,
    )
    alt_zuordnung = welt.bank.zuordnen_manuell(
        ctx=ctx, transaktion=alt, konto=welt.konten["601"], betrag_cent=10_000, beleg_referenz="alt", vorgang_id="ALT-1",
    )
    _binden(welt, ctx)  # Objekt 601 -> BK-MIETE
    (neu,) = _import_csv(welt, ctx, "BK-ANDERE", [("A-3", "50.00", "VERTRAG:V-601")], mapping=MAPPING)
    bestehende_zahlung = welt.op.buchen(
        ctx=ctx, konto=welt.konten["601"], typ=OPTyp.ZAHLUNG, betrag_cent=5_000, belegdatum=date(2026, 4, 1),
        buchungsdatum=date(2026, 4, 1), faelligkeit=None, beleg_referenz="synthetische Vorab-Zahlung",
        import_id="SYN-VORAB-1",
    )
    vorher = _stand(session_factory)

    with pytest.raises(BankquellenBindungError):
        welt.bank.zuordnen_manuell(
            ctx=ctx, transaktion=neu, konto=welt.konten["601"], betrag_cent=5_000, beleg_referenz="x", vorgang_id="M-1",
        )
    with pytest.raises(BankquellenBindungError):
        welt.bank.automatisch_zuordnen(ctx=ctx, transaktion=neu)
    with pytest.raises(BankquellenBindungError):
        welt.bank.verknuepfe_mit_bestehender_zahlung(
            ctx=ctx, transaktion=neu, op_position=bestehende_zahlung, konto=welt.konten["601"], betrag_cent=5_000,
            vorgang_id="V-1",
        )
    with pytest.raises(BankquellenBindungError):
        welt.bank.verarbeite_ruecklastschrift(
            ctx=ctx, transaktion=ruecklast, original_op_position=welt.op.get_position(alt_zuordnung.op_position_id),
            konto=welt.konten["601"],
        )
    assert _stand(session_factory) == vorher

    # Nie konfiguriertes Objekt 617: unverändertes Verhalten.
    welt.bank.zuordnen_manuell(
        ctx=ctx, transaktion=neu, konto=welt.konten["617"], betrag_cent=5_000, beleg_referenz="x", vorgang_id="M-617",
    )


def test_zuordnung_vom_gebundenen_konto_und_widerruf_sperrt(welt, ctx, session_factory):
    kontext = _binden(welt, ctx)
    eins, zwei = _import_csv(welt, ctx, "BK-MIETE", [("B-1", "10.00", "Miete"), ("B-2", "20.00", "Miete")], kontext=kontext)
    welt.bank.zuordnen_manuell(
        ctx=ctx, transaktion=eins, konto=welt.konten["601"], betrag_cent=1_000, beleg_referenz="x", vorgang_id="OK-1",
    )
    welt.qb.widerrufen(ctx=ctx, objekt_id="601", nachweis_ref="SYN-FALSCH", erwarteter_stand=kontext.token)
    vorher = _stand(session_factory)
    with pytest.raises(BankquellenBindungError):
        welt.bank.zuordnen_manuell(
            ctx=ctx, transaktion=zwei, konto=welt.konten["601"], betrag_cent=2_000, beleg_referenz="x", vorgang_id="NO-1",
        )
    assert _stand(session_factory) == vorher


def test_widerruf_retry_noop_und_neubindung_nur_mit_widerrufsstand(welt, ctx, session_factory):
    kontext = _binden(welt, ctx)
    widerruf = welt.qb.widerrufen(ctx=ctx, objekt_id="601", nachweis_ref="SYN-W", erwarteter_stand=kontext.token)
    assert (widerruf.revision, widerruf.status) == (2, "WIDERRUFEN")
    vorher = _stand(session_factory)
    assert welt.qb.widerrufen(ctx=ctx, objekt_id="601", nachweis_ref="SYN-W", erwarteter_stand=kontext.token) == widerruf
    assert _stand(session_factory) == vorher
    with pytest.raises(OptimistischerLockKonfliktError):
        welt.qb.widerrufen(ctx=ctx, objekt_id="601", nachweis_ref="anders", erwarteter_stand=kontext.token)
    with pytest.raises(BankquellenBindungError):
        welt.qb.kontext_fuer_objekt(ctx=ctx, objekt_id="601")
    status = next(s for s in welt.qb.uebersicht(ctx=ctx) if s.objekt_id == "601")
    assert status.status == "WIDERRUFEN" and status.iban_maskiert == "AT00 **** 0001"
    with pytest.raises(OptimistischerLockKonfliktError):
        _binden(welt, ctx, stand=kontext.token, nachweis_ref="SYN-NEU")
    neu = _binden(welt, ctx, stand=widerruf.token, nachweis_ref="SYN-NEU")
    assert neu.revision == 3 and welt.qb.kontext_fuer_objekt(ctx=ctx, objekt_id="601") == neu


# -- Geschützter Abruf (nur Fake-Adapter) ------------------------------------------


class _FakeAdapter:
    def __init__(self, info, lieferung, bei_abruf=None):
        self.info, self.lieferung, self.bei_abruf, self.aufrufe = info, lieferung, bei_abruf, []

    def kontoinfo(self, **anfrage):
        self.aufrufe.append(("kontoinfo", anfrage))
        return self.info

    def umsaetze_abrufen(self, **anfrage):
        self.aufrufe.append(("umsaetze", anfrage))
        if self.bei_abruf:
            self.bei_abruf()
        return self.lieferung


def _info(**anders):
    werte = dict(anbieter="SYN-ANBIETER", zugang_ref="ZUGANG-1", konto_ref="KONTO-1", iban=IBAN_A, kontorolle="MIETE")
    return AnbieterKontoInfo(**{**werte, **anders})


def _lieferung(**anders):
    werte = dict(
        anbieter="SYN-ANBIETER", zugang_ref="ZUGANG-1", konto_ref="KONTO-1", iban=IBAN_A, format="CAMT053",
        inhalt=_camt([("P-1", "10.00", "Miete"), ("P-2", "20.00", "Miete")]),
    )
    return AnbieterLieferung(**{**werte, **anders})


def _abruf(welt, ctx, adapter, objekt_id="601"):
    return GeschuetzterBankabruf(welt.bank, welt.bank_repo).abrufen(
        ctx=ctx, objekt_id=objekt_id, adapter=adapter, von=date(2026, 4, 1), bis=date(2026, 4, 30),
    )


@pytest.mark.parametrize(
    "info",
    [_info(konto_ref="KONTO-FREMD"), _info(kontorolle="KREDIT"), _info(kontorolle="KAUTION"), _info(iban=IBAN_B)],
    ids=["fremdes-konto", "kredit", "kaution", "fremde-iban"],
)
def test_abruf_holt_bei_falscher_kontoinfo_keine_umsaetze(welt, ctx, session_factory, info):
    _binden(welt, ctx)
    adapter = _FakeAdapter(info, _lieferung())
    with pytest.raises(AnbieterAntwortError):
        _abruf(welt, ctx, adapter)
    assert [name for name, _ in adapter.aufrufe] == ["kontoinfo"]
    assert _anzahl(session_factory, BankTransaktionTable) == 0


@pytest.mark.parametrize(
    "lieferung", [_lieferung(konto_ref="KONTO-FREMD"), _lieferung(iban=IBAN_B), _lieferung(format="CSV")],
    ids=["fremdes-konto", "fremde-iban", "format"],
)
def test_abruf_prueft_lieferidentitaet(welt, ctx, session_factory, lieferung):
    _binden(welt, ctx)
    with pytest.raises(AnbieterAntwortError):
        _abruf(welt, ctx, _FakeAdapter(_info(), lieferung))
    assert _anzahl(session_factory, BankTransaktionTable) == 0


def test_abruf_importiert_genau_einmal_ueber_gebundenen_kontext(welt, ctx, session_factory):
    _binden(welt, ctx)
    adapter = _FakeAdapter(_info(), _lieferung())
    assert len(_abruf(welt, ctx, adapter)) == 2
    erwartete_anfrage = dict(anbieter="SYN-ANBIETER", zugang_ref="ZUGANG-1", konto_ref="KONTO-1")
    assert adapter.aufrufe[0] == ("kontoinfo", erwartete_anfrage)
    assert adapter.aufrufe[1][1] == {**erwartete_anfrage, "von": date(2026, 4, 1), "bis": date(2026, 4, 30)}
    _abruf(welt, ctx, adapter)  # Wiederholung: native IDs -> Replay, keine neuen Zeilen
    with session_factory() as session:
        zeilen = session.execute(select(BankTransaktionTable)).scalars().all()
    assert len(zeilen) == 2 and {z.bank_konto_id for z in zeilen} == {"BK-MIETE"}


@pytest.mark.parametrize("aenderung", ["widerruf", "neubindung"])
def test_abruf_bricht_bei_aenderung_waehrend_abruf_ab(welt, ctx, session_factory, aenderung):
    kontext = _binden(welt, ctx)

    def _aendern():
        if aenderung == "widerruf":
            welt.qb.widerrufen(ctx=ctx, objekt_id="601", nachweis_ref="SYN-MITTEN", erwarteter_stand=kontext.token)
        else:
            _binden(welt, ctx, stand=kontext.token, konto_ref="KONTO-2", nachweis_ref="SYN-MITTEN")

    with pytest.raises(BankquellenBindungError):
        _abruf(welt, ctx, _FakeAdapter(_info(), _lieferung(), bei_abruf=_aendern))
    assert _anzahl(session_factory, BankTransaktionTable) == 0


def test_abruf_ohne_bindung_ruft_adapter_nie_auf(welt, ctx):
    adapter = _FakeAdapter(_info(), _lieferung())
    with pytest.raises(BankquellenBindungError):
        _abruf(welt, ctx, adapter, objekt_id="617")
    assert adapter.aufrufe == []


# -- Schema --------------------------------------------------------------------------


def test_create_all_additiv_und_bestehende_zeilen_unveraendert():
    engine = build_engine("sqlite+pysqlite:///:memory:")
    neu = {"bank_quellen", "bank_quellen_bindungen"}
    Base.metadata.create_all(engine, tables=[t for t in Base.metadata.sorted_tables if t.name not in neu])
    assert neu.isdisjoint(inspect(engine).get_table_names())
    factory = sessionmaker(bind=engine, future=True, expire_on_commit=False, class_=Session)
    with factory() as session:
        session.add(GesellschaftTable(id="7DI", name="Synthetik"))
        session.add(BankKontoTable(id="BK-ALT", gesellschaft_id="7DI", iban=IBAN_A, bezeichnung="Bestand"))
        session.commit()

    for _ in range(2):
        Base.metadata.create_all(engine)
    assert neu <= set(inspect(engine).get_table_names())
    with factory() as session:
        zeilen = [(b.id, b.gesellschaft_id, b.iban, b.bezeichnung) for b in session.execute(select(BankKontoTable)).scalars()]
        assert zeilen == [("BK-ALT", "7DI", IBAN_A, "Bestand")]
        assert session.execute(select(func.count()).select_from(BankQuelleTable)).scalar_one() == 0
