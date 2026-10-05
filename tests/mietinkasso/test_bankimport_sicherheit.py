"""Bankimport-Sicherheit: Bankkonto-Bindung, eigene CSV-Kontospalte und
formatübergreifende native Bank-IDs (siehe
docs/hausverwaltung/BANKIMPORT_SICHERHEIT.md). Ausschließlich synthetische
Daten/IBANs."""

from __future__ import annotations

from xml.sax.saxutils import escape

import pytest
from sqlalchemy import select

from mietinkasso.auth.service import AuthContext
from mietinkasso.bank import service as bank_service_modul
from mietinkasso.bank.importer import CamtKontoMismatchError, CsvKontoMismatchError, CsvSpaltenMapping, parse_csv
from mietinkasso.bank.repository import BankRepository
from mietinkasso.bank.service import BankImportService, BankkontoBindungError
from mietinkasso.domain.enums import Rolle
from mietinkasso.domain.exceptions import CrossTenantError, ImportConflictError
from mietinkasso.infrastructure.db.tables import BankKontoTable, BankTransaktionTable
from mietinkasso.op.repository import OPRepository
from mietinkasso.op.service import OPService

IBAN = "AT000000000000000000"
IBAN_B = "AT000000000000000002"
IBAN_FREMD = "AT000000000000000099"

MAPPING = CsvSpaltenMapping(betrag="betrag", buchungsdatum="datum", referenz="referenz", eindeutige_referenz="bank_id")


@pytest.fixture
def bank_repo(session_factory) -> BankRepository:
    return BankRepository(session_factory)


@pytest.fixture
def bank_service(session_factory, stammdaten_repo, bank_repo) -> BankImportService:
    return BankImportService(bank_repo, stammdaten_repo, OPService(OPRepository(session_factory), stammdaten_repo))


@pytest.fixture
def bank_konto(bank_repo, stammdaten_repo) -> BankKontoTable:
    stammdaten_repo.upsert_gesellschaft(id="7DI", name="Synthetik 7DI")
    stammdaten_repo.upsert_gesellschaft(id="MABAU", name="Synthetik MABAU")
    bank_repo.upsert_bank_konto(id="BK-SYN-1", gesellschaft_id="7DI", iban=IBAN, bezeichnung="Synthetik 1")
    return bank_repo.get_bank_konto("BK-SYN-1")


@pytest.fixture
def ctx(ctx_factory) -> AuthContext:
    return ctx_factory("7DI")


def _camt(eintraege: list[tuple[str, str, str]], *, iban: str = IBAN) -> bytes:
    """eintraege: (native_id, betrag, referenz) - ohne ValDt/Gegenpartei,
    damit der Inhalts-Hash mit der gleichwertigen CSV-Zeile übereinstimmt."""

    ntry = "".join(
        f"""
      <Ntry>
        <Amt Ccy="EUR">{betrag}</Amt><CdtDbtInd>CRDT</CdtDbtInd><BookgDt><Dt>2026-04-06</Dt></BookgDt>
        <NtryDtls><TxDtls>
          <RmtInf><Ustrd>{escape(referenz)}</Ustrd></RmtInf><AcctSvcrRef>{escape(native_id)}</AcctSvcrRef>
        </TxDtls></NtryDtls>
      </Ntry>"""
        for native_id, betrag, referenz in eintraege
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"><BkToCstmrStmt><Stmt>
  <Acct><Id><IBAN>{iban}</IBAN></Id></Acct>{ntry}
</Stmt></BkToCstmrStmt></Document>""".encode("utf-8")


def _csv(eintraege: list[tuple[str, str, str]]) -> str:
    return "betrag,datum,referenz,bank_id\n" + "".join(
        f"{betrag},2026-04-06,{referenz},{native_id}\n" for native_id, betrag, referenz in eintraege
    )


def _zeilen(session_factory, bank_konto_id: str | None = None) -> list[BankTransaktionTable]:
    with session_factory() as session:
        statement = select(BankTransaktionTable).order_by(BankTransaktionTable.id)
        if bank_konto_id is not None:
            statement = statement.where(BankTransaktionTable.bank_konto_id == bank_konto_id)
        return list(session.execute(statement).scalars().all())


def _snapshot(session_factory) -> list[tuple]:
    return [
        (r.id, r.bank_konto_id, r.import_id, r.quelle_typ, r.quelle_hash, r.betrag_cent, r.referenz)
        for r in _zeilen(session_factory)
    ]


# -- 3. Native Bank-IDs über Formatgrenzen ---------------------------------------


def test_camt_dann_csv_replay_liefert_dieselbe_zeile(bank_service, bank_konto, ctx, session_factory):
    erst = bank_service.importiere_camt053(ctx=ctx, bank_konto=bank_konto, xml_bytes=_camt([("N-1", "600.00", "Miete")]))
    dann = bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=_csv([("N-1", "600.00", "Miete")]), mapping=MAPPING)
    assert [r.id for r in dann] == [r.id for r in erst]
    assert len(_zeilen(session_factory, "BK-SYN-1")) == 1
    assert dann[0].quelle_typ == "CAMT053"  # bestehende Zeile unverändert zurückgegeben


def test_csv_dann_camt_replay_liefert_dieselbe_zeile(bank_service, bank_konto, ctx, session_factory):
    erst = bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=_csv([("N-1", "600.00", "Miete")]), mapping=MAPPING)
    dann = bank_service.importiere_camt053(ctx=ctx, bank_konto=bank_konto, xml_bytes=_camt([("N-1", "600.00", "Miete")]))
    assert [r.id for r in dann] == [r.id for r in erst]
    assert len(_zeilen(session_factory, "BK-SYN-1")) == 1


def test_gleiches_format_replay_bleibt_idempotent(bank_service, bank_konto, ctx, session_factory):
    for _ in range(2):
        bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=_csv([("N-1", "600.00", "Miete")]), mapping=MAPPING)
        bank_service.importiere_camt053(ctx=ctx, bank_konto=bank_konto, xml_bytes=_camt([("N-2", "100.00", "Miete")]))
    assert len(_zeilen(session_factory, "BK-SYN-1")) == 2


def test_gleiches_format_abweichender_inhalt_bleibt_konflikt(bank_service, bank_konto, ctx, session_factory):
    bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=_csv([("N-1", "600.00", "Miete")]), mapping=MAPPING)
    vorher = _snapshot(session_factory)
    with pytest.raises(ImportConflictError):
        bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=_csv([("N-1", "601.00", "Miete")]), mapping=MAPPING)
    assert _snapshot(session_factory) == vorher


@pytest.mark.parametrize("richtung", ["camt_zuerst", "csv_zuerst"])
def test_formatwechsel_mit_abweichendem_inhalt_rollt_ganze_datei_zurueck(
    bank_service, bank_konto, ctx, session_factory, richtung
):
    if richtung == "camt_zuerst":
        bank_service.importiere_camt053(ctx=ctx, bank_konto=bank_konto, xml_bytes=_camt([("N-1", "600.00", "Miete")]))
    else:
        bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=_csv([("N-1", "600.00", "Miete")]), mapping=MAPPING)
    vorher = _snapshot(session_factory)

    # Eine NEUE Zeile steht VOR dem Konflikt - auch sie darf nicht bleiben.
    datei = [("N-NEU", "50.00", "Neu"), ("N-1", "601.00", "Miete")]
    with pytest.raises(ImportConflictError, match="N-1"):
        if richtung == "camt_zuerst":
            bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=_csv(datei), mapping=MAPPING)
        else:
            bank_service.importiere_camt053(ctx=ctx, bank_konto=bank_konto, xml_bytes=_camt(datei))
    assert _snapshot(session_factory) == vorher


def test_historische_doppelzeilen_beider_formate_werden_gemeldet_nicht_veraendert(
    bank_service, bank_konto, ctx, session_factory
):
    bank_service.importiere_camt053(ctx=ctx, bank_konto=bank_konto, xml_bytes=_camt([("N-9", "600.00", "Miete")]))
    camt_zeile = _zeilen(session_factory, "BK-SYN-1")[0]
    # Altbestand vor diesem Fix: dieselbe native ID zusätzlich als CSV-Zeile.
    with session_factory() as session:
        session.add(BankTransaktionTable(
            bank_konto_id="BK-SYN-1", betrag_cent=60_000, waehrung="EUR", buchungsdatum=camt_zeile.buchungsdatum,
            referenz="Miete", quelle_typ="CSV", quelle_hash=camt_zeile.quelle_hash, import_id="BK-SYN-1:CSV:N-9",
            hat_native_id=True, roh_zeile="synthetischer Altbestand",
        ))
        session.commit()
    vorher = _snapshot(session_factory)

    for importieren in (
        lambda: bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=_csv([("N-9", "600.00", "Miete")]), mapping=MAPPING),
        lambda: bank_service.importiere_camt053(ctx=ctx, bank_konto=bank_konto, xml_bytes=_camt([("N-9", "600.00", "Miete")])),
    ):
        with pytest.raises(ImportConflictError, match="MEHRFACH"):
            importieren()
        assert _snapshot(session_factory) == vorher  # nichts gelöscht, zusammengelegt oder geändert


def test_gleiche_native_id_auf_anderem_bankkonto_ist_eigene_zeile(bank_service, bank_repo, bank_konto, ctx, session_factory):
    bank_repo.upsert_bank_konto(id="BK-SYN-2", gesellschaft_id="7DI", iban=IBAN_B, bezeichnung="Synthetik 2")
    bank_konto_b = bank_repo.get_bank_konto("BK-SYN-2")
    a = bank_service.importiere_camt053(ctx=ctx, bank_konto=bank_konto, xml_bytes=_camt([("N-1", "600.00", "Miete")]))
    b = bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto_b, text=_csv([("N-1", "600.00", "Miete")]), mapping=MAPPING)
    c = bank_service.importiere_camt053(ctx=ctx, bank_konto=bank_konto_b, xml_bytes=_camt([("N-1", "600.00", "Miete")], iban=IBAN_B))
    assert a[0].id != b[0].id
    assert b[0].id == c[0].id
    assert len(_zeilen(session_factory)) == 2


def test_verschiedene_native_ids_mit_gleichen_wirtschaftlichen_feldern_bleiben_getrennt(
    bank_service, bank_konto, ctx, session_factory
):
    bank_service.importiere_csv(
        ctx=ctx, bank_konto=bank_konto, mapping=MAPPING,
        text=_csv([("N-A", "600.00", "Miete"), ("N-B", "600.00", "Miete")]),
    )
    bank_service.importiere_camt053(ctx=ctx, bank_konto=bank_konto, xml_bytes=_camt([("N-C", "600.00", "Miete")]))
    assert len(_zeilen(session_factory, "BK-SYN-1")) == 3


def test_native_ids_mit_sonderzeichen_werden_exakt_verglichen(bank_service, bank_konto, ctx, session_factory):
    ids = ["A:B", "A:B:C", "REF_1", "REF%1", "REFx1", "%", "_", "CSV:N-1", "CAMT053:N-1"]
    eintraege = [(nid, "10.00", "Sonderzeichen") for nid in ids]
    erst = bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=_csv(eintraege), mapping=MAPPING)
    assert len({r.id for r in erst}) == len(ids)
    dann = bank_service.importiere_camt053(ctx=ctx, bank_konto=bank_konto, xml_bytes=_camt(eintraege))
    assert [r.id for r in dann] == [r.id for r in erst]
    assert len(_zeilen(session_factory, "BK-SYN-1")) == len(ids)


def test_kontopraefix_kollision_ist_kein_replay(bank_service, bank_repo, bank_konto, ctx, session_factory):
    """Konto "BK-SYN-1:CSV" mit ID "X" und Konto "BK-SYN-1" mit ID "CSV:X"
    ergeben dieselbe import_id-Zeichenkette - das darf nie als Replay der
    fremden Zeile durchgehen."""

    bank_repo.upsert_bank_konto(id="BK-SYN-1:CSV", gesellschaft_id="7DI", iban=IBAN_B, bezeichnung="Synthetik Präfix")
    fremd = bank_service.importiere_csv(
        ctx=ctx, bank_konto=bank_repo.get_bank_konto("BK-SYN-1:CSV"), text=_csv([("X", "600.00", "Miete")]), mapping=MAPPING,
    )
    vorher = _snapshot(session_factory)
    with pytest.raises(ImportConflictError):
        bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=_csv([("CSV:X", "600.00", "Miete")]), mapping=MAPPING)
    assert _snapshot(session_factory) == vorher
    # Im anderen Format kollidiert die import_id nicht - eigene, neue Zeile.
    eigen = bank_service.importiere_camt053(ctx=ctx, bank_konto=bank_konto, xml_bytes=_camt([("CSV:X", "600.00", "Miete")]))
    assert eigen[0].id != fremd[0].id
    assert eigen[0].bank_konto_id == "BK-SYN-1"


def test_ohne_native_id_bleibt_konservativer_fingerprint_konflikt(bank_service, bank_konto, ctx):
    from mietinkasso.domain.exceptions import MehrfachbuchungsKonfliktError

    mapping = CsvSpaltenMapping(betrag="betrag", buchungsdatum="datum", referenz="referenz")
    text = "betrag,datum,referenz\n600.00,2026-04-06,Miete\n"
    bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=text, mapping=mapping)
    with pytest.raises(MehrfachbuchungsKonfliktError):
        bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=text, mapping=mapping)


# -- 1. Bankkonto-Bindung --------------------------------------------------------


def test_unbekanntes_bankkonto_objekt_wird_abgelehnt(bank_service, bank_konto, ctx, session_factory):
    unbekannt = BankKontoTable(id="BK-GIBT-ES-NICHT", gesellschaft_id="7DI", iban=IBAN, bezeichnung="erfunden")
    with pytest.raises(BankkontoBindungError, match="Unbekanntes Bankkonto"):
        bank_service.importiere_camt053(ctx=ctx, bank_konto=unbekannt, xml_bytes=_camt([("N-1", "600.00", "Miete")]))
    with pytest.raises(BankkontoBindungError):
        bank_service.importiere_csv(ctx=ctx, bank_konto=unbekannt, text=_csv([("N-1", "600.00", "Miete")]), mapping=MAPPING)
    assert _zeilen(session_factory) == []


def test_gefaelschte_gesellschaft_wird_gegen_persistierte_identitaet_geprueft(
    bank_service, bank_konto, ctx_factory, admin_ctx, session_factory
):
    gefaelscht = BankKontoTable(id="BK-SYN-1", gesellschaft_id="MABAU", iban=IBAN, bezeichnung="gefälscht")
    # Zugriff hängt an der GESPEICHERTEN Gesellschaft (7DI), nicht an der behaupteten.
    with pytest.raises(CrossTenantError):
        bank_service.importiere_csv(
            ctx=ctx_factory("MABAU"), bank_konto=gefaelscht, text=_csv([("N-1", "600.00", "Miete")]), mapping=MAPPING,
        )
    with pytest.raises(BankkontoBindungError, match="Gesellschaft"):
        bank_service.importiere_csv(ctx=admin_ctx, bank_konto=gefaelscht, text=_csv([("N-1", "600.00", "Miete")]), mapping=MAPPING)
    assert _zeilen(session_factory) == []


def test_gefaelschte_iban_kann_camt_pruefung_nicht_umgehen(bank_service, bank_konto, ctx, session_factory):
    gefaelscht = BankKontoTable(id="BK-SYN-1", gesellschaft_id="7DI", iban=IBAN_FREMD, bezeichnung="gefälscht")
    with pytest.raises(BankkontoBindungError, match="IBAN"):
        bank_service.importiere_camt053(
            ctx=ctx, bank_konto=gefaelscht, xml_bytes=_camt([("N-1", "600.00", "Miete")], iban=IBAN_FREMD),
        )
    assert _zeilen(session_factory) == []


def test_veraltetes_kontoobjekt_nach_ibanaenderung_wird_abgelehnt(bank_service, bank_repo, bank_konto, ctx, session_factory):
    bank_repo.upsert_bank_konto(id="BK-SYN-1", gesellschaft_id="7DI", iban=IBAN_B, bezeichnung="Synthetik 1")
    with pytest.raises(BankkontoBindungError):
        bank_service.importiere_camt053(ctx=ctx, bank_konto=bank_konto, xml_bytes=_camt([("N-1", "600.00", "Miete")]))
    assert _zeilen(session_factory) == []


def test_iban_formatierung_des_kontoobjekts_wird_normalisiert(bank_service, bank_konto, ctx, session_factory):
    formatiert = BankKontoTable(id="BK-SYN-1", gesellschaft_id="7DI", iban=" at00 0000\t0000 0000 0000 ", bezeichnung="x")
    zeilen = bank_service.importiere_camt053(ctx=ctx, bank_konto=formatiert, xml_bytes=_camt([("N-1", "600.00", "Miete")]))
    assert len(zeilen) == 1


def test_zugriff_und_schreibrecht_auf_persistierte_gesellschaft(bank_service, bank_konto, ctx_factory, session_factory):
    with pytest.raises(CrossTenantError):
        bank_service.importiere_csv(
            ctx=ctx_factory("MABAU"), bank_konto=bank_konto, text=_csv([("N-1", "600.00", "Miete")]), mapping=MAPPING,
        )
    lesend = AuthContext(user_id="lesend", rolle=Rolle.LESEZUGRIFF, gesellschaft_ids=frozenset({"7DI"}))
    with pytest.raises(CrossTenantError, match="Schreibrecht"):
        bank_service.importiere_camt053(ctx=lesend, bank_konto=bank_konto, xml_bytes=_camt([("N-1", "600.00", "Miete")]))
    assert _zeilen(session_factory) == []


@pytest.mark.parametrize("aenderung", [{"iban": IBAN_B}, {"gesellschaft_id": "MABAU"}])
def test_kontoaenderung_zwischen_pruefung_und_schreiben_wird_in_transaktion_erkannt(
    bank_service, bank_repo, bank_konto, admin_ctx, session_factory, monkeypatch, aenderung
):
    original_parse = bank_service_modul.parse_csv

    def _parse_und_konto_aendern(*args, **kwargs):
        ergebnis = original_parse(*args, **kwargs)
        felder = {"id": "BK-SYN-1", "gesellschaft_id": "7DI", "iban": IBAN, "bezeichnung": "Synthetik 1"} | aenderung
        bank_repo.upsert_bank_konto(**felder)
        return ergebnis

    monkeypatch.setattr(bank_service_modul, "parse_csv", _parse_und_konto_aendern)
    with pytest.raises(BankkontoBindungError):
        bank_service.importiere_csv(ctx=admin_ctx, bank_konto=bank_konto, text=_csv([("N-1", "600.00", "Miete")]), mapping=MAPPING)
    assert _zeilen(session_factory) == []


# -- 2. Eigene CSV-Kontospalte ---------------------------------------------------

EIGEN_MAPPING = CsvSpaltenMapping(
    betrag="betrag", buchungsdatum="datum", referenz="referenz", eindeutige_referenz="bank_id",
    gegenkonto_iban="gegen_iban", eigene_iban="konto",
)


def _csv_eigen(*konten: str, gegen: str = IBAN_FREMD) -> str:
    return "betrag,datum,referenz,bank_id,gegen_iban,konto\n" + "".join(
        f"10.00,2026-04-06,Miete,N-{i},{gegen},{konto}\n" for i, konto in enumerate(konten)
    )


def test_csv_eigene_iban_passend_mit_formatierung(bank_service, bank_konto, ctx):
    zeilen = parse_csv(_csv_eigen(IBAN, " at00 0000 0000 0000 0000 ", "AT00 0000 00000000 0000"), EIGEN_MAPPING, erwartete_iban=IBAN)
    assert len(zeilen) == 3
    assert all(z.gegenkonto_iban == IBAN_FREMD for z in zeilen)  # Gegenkonto unverändert, nicht verwechselt
    importiert = bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=_csv_eigen(IBAN, IBAN), mapping=EIGEN_MAPPING)
    assert len(importiert) == 2


@pytest.mark.parametrize(
    "konten",
    [(IBAN, ""), (IBAN, "   "), (IBAN_FREMD,), (IBAN, IBAN_FREMD), (IBAN_FREMD, IBAN)],
    ids=["leer", "blank", "falsch", "gemischt-hinten", "gemischt-vorne"],
)
def test_csv_eigene_iban_fehlend_falsch_oder_gemischt_lehnt_ganze_datei_ab(
    bank_service, bank_konto, ctx, session_factory, konten
):
    with pytest.raises(CsvKontoMismatchError):
        parse_csv(_csv_eigen(*konten), EIGEN_MAPPING, erwartete_iban=IBAN)
    with pytest.raises(CsvKontoMismatchError):
        bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=_csv_eigen(*konten), mapping=EIGEN_MAPPING)
    assert _zeilen(session_factory) == []


def test_csv_kurze_zeile_ohne_kontowert_wird_abgelehnt():
    text = "betrag,datum,referenz,bank_id,gegen_iban,konto\n10.00,2026-04-06,Miete,N-1,X\n"
    with pytest.raises(CsvKontoMismatchError):
        parse_csv(text, EIGEN_MAPPING, erwartete_iban=IBAN)


def test_csv_gegenkonto_iban_ist_kein_kontonachweis():
    # Eigene IBAN steht nur in der Gegenkonto-Spalte, eigene Kontospalte fremd.
    with pytest.raises(CsvKontoMismatchError):
        parse_csv(_csv_eigen(IBAN_FREMD, gegen=IBAN), EIGEN_MAPPING, erwartete_iban=IBAN)
    verwechselt = CsvSpaltenMapping(betrag="betrag", buchungsdatum="datum", gegenkonto_iban="konto", eigene_iban="konto")
    with pytest.raises(CsvKontoMismatchError, match="Gegenkonto"):
        parse_csv(_csv_eigen(IBAN), verwechselt, erwartete_iban=IBAN)


@pytest.mark.parametrize(
    "kopf",
    [
        "betrag,datum,referenz,bank_id,gegen_iban,eigenes_konto",
        "betrag,datum,referenz,bank_id,konto,konto",
        "betrag,datum,referenz,bank_id, Konto ,konto",
    ],
    ids=["fehlt", "doppelt", "doppelt-normalisiert"],
)
def test_csv_eigene_kontospalte_fehlt_oder_doppelt(kopf):
    text = f"{kopf}\n10.00,2026-04-06,Miete,N-1,{IBAN},{IBAN}\n"
    with pytest.raises(CsvKontoMismatchError, match="Kopfzeile"):
        parse_csv(text, EIGEN_MAPPING, erwartete_iban=IBAN)


def test_csv_eigene_kontospalte_fehlt_auch_bei_leerer_datei():
    with pytest.raises(CsvKontoMismatchError):
        parse_csv("", EIGEN_MAPPING, erwartete_iban=IBAN)


@pytest.mark.parametrize("erwartet", [None, "", "  "])
def test_csv_leere_erwartete_iban_wird_abgelehnt(erwartet):
    with pytest.raises(CsvKontoMismatchError, match="Kein IBAN"):
        parse_csv(_csv_eigen(IBAN), EIGEN_MAPPING, erwartete_iban=erwartet)


def test_csv_eigene_kontospalte_auf_konto_ohne_iban_wird_abgelehnt(bank_service, bank_repo, bank_konto, ctx, session_factory):
    bank_repo.upsert_bank_konto(id="BK-OHNE-IBAN", gesellschaft_id="7DI", iban="", bezeichnung="Synthetik ohne IBAN")
    with pytest.raises(CsvKontoMismatchError):
        bank_service.importiere_csv(
            ctx=ctx, bank_konto=bank_repo.get_bank_konto("BK-OHNE-IBAN"), text=_csv_eigen(IBAN), mapping=EIGEN_MAPPING,
        )
    assert _zeilen(session_factory) == []


def test_legacy_csv_ohne_kontospalte_bleibt_kompatibel(bank_service, bank_repo, bank_konto, ctx):
    positional = CsvSpaltenMapping("betrag", "datum", None, "referenz", "bank_id")
    assert positional.eigene_iban is None
    text = _csv([("N-1", "600.00", "Miete")])
    assert len(parse_csv(text, positional)) == 1
    assert len(parse_csv(text, positional, erwartete_iban="")) == 1
    # Auch ein Konto ohne hinterlegte IBAN importiert Legacy-CSV wie bisher.
    bank_repo.upsert_bank_konto(id="BK-OHNE-IBAN", gesellschaft_id="7DI", iban="", bezeichnung="Synthetik ohne IBAN")
    assert len(bank_service.importiere_csv(ctx=ctx, bank_konto=bank_repo.get_bank_konto("BK-OHNE-IBAN"), text=text, mapping=positional)) == 1


def test_camt_bleibt_unveraendert_streng(bank_service, bank_konto, ctx):
    with pytest.raises(CamtKontoMismatchError):
        bank_service.importiere_camt053(ctx=ctx, bank_konto=bank_konto, xml_bytes=_camt([("N-1", "1.00", "x")], iban=IBAN_FREMD))
