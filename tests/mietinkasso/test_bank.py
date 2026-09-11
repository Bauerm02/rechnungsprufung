from __future__ import annotations

from datetime import date

import pytest

from mietinkasso.bank.importer import (
    CamtMehrteiligeBuchungError,
    CamtUnvollstaendigError,
    CsvSpaltenMapping,
)
from mietinkasso.bank.repository import BankRepository
from mietinkasso.bank.service import BankImportService
from mietinkasso.domain.enums import OPTyp
from mietinkasso.domain.exceptions import (
    CrossTenantError,
    FremdwaehrungNichtUnterstuetztError,
    MehrfachbuchungsKonfliktError,
    ZuordnungUngueltigError,
)
from mietinkasso.op.repository import OPRepository
from mietinkasso.op.service import OPService


@pytest.fixture
def bank_service(session_factory, stammdaten_repo) -> BankImportService:
    op_service = OPService(OPRepository(session_factory), stammdaten_repo)
    return BankImportService(BankRepository(session_factory), stammdaten_repo, op_service)


@pytest.fixture
def bank_repo(session_factory) -> BankRepository:
    return BankRepository(session_factory)


CAMT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02">
  <BkToCstmrStmt>
    <Stmt>
      <Ntry>
        <Amt Ccy="EUR">600.00</Amt>
        <CdtDbtInd>CRDT</CdtDbtInd>
        <BookgDt><Dt>2026-04-06</Dt></BookgDt>
        <ValDt><Dt>2026-04-06</Dt></ValDt>
        <NtryDtls>
          <TxDtls>
            <RmtInf><Ustrd>VERTRAG:V-601-3 Miete April</Ustrd></RmtInf>
            <RltdPties><Dbtr><Nm>Max Mustermieter</Nm></Dbtr></RltdPties>
            <AcctSvcrRef>REF-0001</AcctSvcrRef>
          </TxDtls>
        </NtryDtls>
      </Ntry>
    </Stmt>
  </BkToCstmrStmt>
</Document>
"""


def test_camt053_import_und_eindeutige_automatische_zuordnung(bank_service, bank_repo, stammdaten_repo, basis_vertrag, ctx_factory):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    bank_repo.upsert_bank_konto(id="BK-7DI-1", gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="7DI Hauptkonto")
    bank_konto = bank_repo.get_bank_konto("BK-7DI-1")

    transaktionen = bank_service.importiere_camt053(ctx=ctx, bank_konto=bank_konto, xml_bytes=CAMT_XML.encode("utf-8"))
    assert len(transaktionen) == 1
    transaktion = transaktionen[0]
    assert transaktion.betrag_cent == 60_000

    ergebnis = bank_service.automatisch_zuordnen(ctx=ctx, transaktion=transaktion)
    assert ergebnis.zugeordnet is True

    saldo = OPService(OPRepository(bank_service._op_service._op_repository._session_factory), stammdaten_repo).berechne_saldo(konto.id)
    assert saldo.saldo_cent == -60_000  # reine Zahlung ohne vorherige Sollstellung -> Guthaben


def test_camt053_doppelimport_ist_wirkungslos(bank_service, bank_repo, ctx_factory):
    ctx = ctx_factory("7DI")
    bank_repo.upsert_bank_konto(id="BK-7DI-1", gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="7DI")
    bank_konto = bank_repo.get_bank_konto("BK-7DI-1")
    bank_service.importiere_camt053(ctx=ctx, bank_konto=bank_konto, xml_bytes=CAMT_XML.encode("utf-8"))
    transaktionen = bank_service.importiere_camt053(ctx=ctx, bank_konto=bank_konto, xml_bytes=CAMT_XML.encode("utf-8"))
    assert len(transaktionen) == 1
    unzugeordnet_oder_zugeordnet_count = len(bank_repo.list_unzugeordnet(bank_konto.id))
    assert unzugeordnet_oder_zugeordnet_count == 1  # nicht zwei Zeilen durch den zweiten Import


def test_keine_automatische_zuordnung_ohne_eindeutige_referenz(bank_service, bank_repo, basis_vertrag, ctx_factory):
    """Name/gleicher Betrag allein reichen nicht für eine automatische Zuordnung."""

    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    bank_repo.upsert_bank_konto(id="BK-7DI-1", gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="7DI")
    bank_konto = bank_repo.get_bank_konto("BK-7DI-1")
    csv_text = "betrag,datum,referenz\n600.00,2026-04-06,Miete Max Mustermieter\n"
    mapping = CsvSpaltenMapping(betrag="betrag", buchungsdatum="datum", referenz="referenz")
    transaktionen = bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=csv_text, mapping=mapping)
    ergebnis = bank_service.automatisch_zuordnen(ctx=ctx, transaktion=transaktionen[0])
    assert ergebnis.zugeordnet is False


def test_cross_tenant_zuordnung_wird_db_seitig_blockiert(bank_service, bank_repo, stammdaten_repo, ctx_factory):
    stammdaten_repo.upsert_gesellschaft(id="7DI", name="7D Immobilien GmbH")
    stammdaten_repo.upsert_gesellschaft(id="MABAU", name="MaBau Beteiligungs GmbH")
    stammdaten_repo.upsert_objekt(id="616", gesellschaft_id="MABAU", bezeichnung="Fockygasse")
    stammdaten_repo.upsert_einheit(id="616-TOP1", objekt_id="616", bezeichnung="Top 1", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten_repo.upsert_debitor(id="DEB-2", name="Andere Mieterin")
    stammdaten_repo.upsert_vertrag(
        id="V-616-1", einheit_id="616-TOP1", debitor_id="DEB-2", gesellschaft_id="MABAU",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    vertrag_mabau = stammdaten_repo.get_vertrag("V-616-1")
    konto_mabau = stammdaten_repo.get_or_create_konto(vertrag=vertrag_mabau)

    bank_repo.upsert_bank_konto(id="BK-7DI-1", gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="7DI")
    bank_konto_7di = bank_repo.get_bank_konto("BK-7DI-1")
    ctx_7di = ctx_factory("7DI")
    csv_text = "betrag,datum,referenz\n600.00,2026-04-06,sonstiges\n"
    mapping = CsvSpaltenMapping(betrag="betrag", buchungsdatum="datum", referenz="referenz")
    transaktion = bank_service.importiere_csv(ctx=ctx_7di, bank_konto=bank_konto_7di, text=csv_text, mapping=mapping)[0]

    with pytest.raises(CrossTenantError):
        bank_repo.create_zuordnung(
            bank_transaktion_id=transaktion.id, op_position_id=_dummy_op_position(bank_service, konto_mabau, ctx_factory), betrag_cent=60_000, match_typ="MANUELL"
        )


def _dummy_op_position(bank_service, konto_mabau, ctx_factory):
    ctx_mabau = ctx_factory("MABAU")
    row = bank_service._op_service.buchen(
        ctx=ctx_mabau,
        konto=konto_mabau,
        typ=OPTyp.SOLL,
        betrag_cent=60_000,
        belegdatum=date(2026, 4, 1),
        buchungsdatum=date(2026, 4, 1),
        faelligkeit=date(2026, 4, 5),
        beleg_referenz="Miete April MaBau",
    )
    return row.id


def test_ruecklastschrift_ueber_bank_macht_op_wieder_offen(bank_service, bank_repo, stammdaten_repo, basis_vertrag, ctx_factory):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    bank_repo.upsert_bank_konto(id="BK-7DI-1", gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="7DI")
    bank_konto = bank_repo.get_bank_konto("BK-7DI-1")

    csv_text = f"betrag,datum,referenz\n600.00,2026-04-06,VERTRAG:{vertrag.id}\n"
    mapping = CsvSpaltenMapping(betrag="betrag", buchungsdatum="datum", referenz="referenz")
    transaktion = bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=csv_text, mapping=mapping)[0]
    ergebnis = bank_service.automatisch_zuordnen(ctx=ctx, transaktion=transaktion)
    assert ergebnis.zugeordnet is True

    zahlung_op = bank_service._op_service._op_repository.get(ergebnis.op_position_id)
    rueck_transaktion_csv = f"betrag,datum,referenz\n-600.00,2026-04-10,RUECKLASTSCHRIFT VERTRAG:{vertrag.id}\n"
    rueck_transaktion = bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=rueck_transaktion_csv, mapping=mapping)[0]

    bank_service.verarbeite_ruecklastschrift(ctx=ctx, transaktion=rueck_transaktion, original_op_position=zahlung_op, konto=konto)

    saldo = bank_service._op_service.berechne_saldo(konto.id)
    assert saldo.saldo_cent == 0  # Zahlung (-600) + Rücklastschrift (+600) gleichen sich wieder aus -> OP wieder offen


def test_bankvollstaendigkeit_basiert_auf_buchungsdatum_nicht_auf_mtime(bank_service, bank_repo, ctx_factory):
    ctx = ctx_factory("7DI")
    bank_repo.upsert_bank_konto(id="BK-7DI-1", gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="7DI")
    bank_konto = bank_repo.get_bank_konto("BK-7DI-1")
    assert bank_service.bankstand_alter_tage(bank_konto.id, heute=date(2026, 4, 10)) is None

    csv_text = "betrag,datum,referenz\n100.00,2026-04-06,sonstiges\n"
    mapping = CsvSpaltenMapping(betrag="betrag", buchungsdatum="datum", referenz="referenz")
    bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=csv_text, mapping=mapping)

    alter = bank_service.bankstand_alter_tage(bank_konto.id, heute=date(2026, 4, 10))
    assert alter == 4


def test_csv_ueberlappung_ohne_native_id_ist_konflikt_statt_doppelimport(bank_service, bank_repo, ctx_factory):
    """Regression (Codex-Fund #6): dieselbe wirtschaftliche Zahlung
    (REF-A/06.04./600 EUR) taucht in einem zweiten, überlappenden Export
    an anderer Zeilenposition wieder auf. Ohne bankseitig eindeutige
    Kennung darf das NICHT still als zweite echte Zahlung durchgehen
    (Verdopplung) UND NICHT still als Replay ignoriert werden (könnte eine
    echte zweite Zahlung verschlucken) - es muss ein klarer Konflikt sein."""

    ctx = ctx_factory("7DI")
    bank_repo.upsert_bank_konto(id="BK-7DI-1", gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="7DI")
    bank_konto = bank_repo.get_bank_konto("BK-7DI-1")

    export_1 = "vorherige_zeile,betrag,datum,referenz\nx,600.00,2026-04-06,REF-A\n"
    export_2_ueberlappend = "andere_spalte,betrag,datum,referenz\ny,600.00,2026-04-06,REF-A\n"
    # Zweiter Export hat eine andere Zeilen-/Spaltenstruktur davor (typisch
    # für überlappende Tagesexporte), aber dieselbe wirtschaftliche Zeile.
    mapping_1 = CsvSpaltenMapping(betrag="betrag", buchungsdatum="datum", referenz="referenz")
    mapping_2 = CsvSpaltenMapping(betrag="betrag", buchungsdatum="datum", referenz="referenz")

    bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=export_1, mapping=mapping_1)
    with pytest.raises(MehrfachbuchungsKonfliktError):
        bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=export_2_ueberlappend, mapping=mapping_2)

    # Genau eine Transaktion ist tatsächlich gebucht - keine stille Verdopplung.
    assert len(bank_repo.list_unzugeordnet(bank_konto.id)) == 1


def test_csv_mit_eindeutiger_referenz_ist_doppelimport_sicher(bank_service, bank_repo, ctx_factory):
    """Mit einer bankseitig eindeutigen Kennung (eindeutige_referenz) ist
    ein Wiederholimport hingegen ein sauberer No-Op, kein Konflikt."""

    ctx = ctx_factory("7DI")
    bank_repo.upsert_bank_konto(id="BK-7DI-1", gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="7DI")
    bank_konto = bank_repo.get_bank_konto("BK-7DI-1")
    mapping = CsvSpaltenMapping(
        betrag="betrag", buchungsdatum="datum", referenz="referenz", eindeutige_referenz="buchungs_id"
    )
    csv_text = "buchungs_id,betrag,datum,referenz\nBANK-TX-001,600.00,2026-04-06,REF-A\n"

    bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=csv_text, mapping=mapping)
    transaktionen = bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=csv_text, mapping=mapping)
    assert len(transaktionen) == 1
    assert len(bank_repo.list_unzugeordnet(bank_konto.id)) == 1


def test_camt053_formatierungsaenderung_ist_kein_neuer_inhalt(bank_service, bank_repo, ctx_factory):
    """Regression (Codex-Fund #6): derselbe wirtschaftliche Inhalt mit
    anderer XML-Formatierung/Attributreihenfolge darf keinen
    ImportConflictError auslösen (formatierungsunabhängiger Hash)."""

    ctx = ctx_factory("7DI")
    bank_repo.upsert_bank_konto(id="BK-7DI-1", gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="7DI")
    bank_konto = bank_repo.get_bank_konto("BK-7DI-1")

    anders_formatiert = CAMT_XML.replace(
        '<Amt Ccy="EUR">600.00</Amt>', '<Amt   Ccy="EUR" >600.00</Amt>'
    ).replace("\n      <Ntry>", "\n      <Ntry>\n        <!-- Kommentar eines anderen Bank-Exports -->")

    bank_service.importiere_camt053(ctx=ctx, bank_konto=bank_konto, xml_bytes=CAMT_XML.encode("utf-8"))
    transaktionen = bank_service.importiere_camt053(ctx=ctx, bank_konto=bank_konto, xml_bytes=anders_formatiert.encode("utf-8"))
    assert len(transaktionen) == 1  # kein ImportConflictError, kein zweiter Datensatz


def test_camt053_mehrteilige_ntry_wird_nicht_der_ersten_referenz_zugeordnet(bank_service, bank_repo, ctx_factory):
    ctx = ctx_factory("7DI")
    bank_repo.upsert_bank_konto(id="BK-7DI-1", gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="7DI")
    bank_konto = bank_repo.get_bank_konto("BK-7DI-1")

    sammelbuchung_ohne_teilbetraege = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02">
  <BkToCstmrStmt>
    <Stmt>
      <Ntry>
        <Amt Ccy="EUR">1000.00</Amt>
        <CdtDbtInd>CRDT</CdtDbtInd>
        <BookgDt><Dt>2026-04-06</Dt></BookgDt>
        <NtryDtls>
          <TxDtls><RmtInf><Ustrd>VERTRAG:V-A</Ustrd></RmtInf><AcctSvcrRef>REF-A</AcctSvcrRef></TxDtls>
          <TxDtls><RmtInf><Ustrd>VERTRAG:V-B</Ustrd></RmtInf><AcctSvcrRef>REF-B</AcctSvcrRef></TxDtls>
        </NtryDtls>
      </Ntry>
    </Stmt>
  </BkToCstmrStmt>
</Document>
"""
    with pytest.raises(CamtMehrteiligeBuchungError):
        bank_service.importiere_camt053(ctx=ctx, bank_konto=bank_konto, xml_bytes=sammelbuchung_ohne_teilbetraege.encode("utf-8"))


def test_camt053_fehlendes_pflichtfeld_wird_nicht_still_uebersprungen(bank_service, bank_repo, ctx_factory):
    ctx = ctx_factory("7DI")
    bank_repo.upsert_bank_konto(id="BK-7DI-1", gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="7DI")
    bank_konto = bank_repo.get_bank_konto("BK-7DI-1")

    ohne_buchungsdatum = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02">
  <BkToCstmrStmt><Stmt><Ntry>
    <Amt Ccy="EUR">100.00</Amt>
    <CdtDbtInd>CRDT</CdtDbtInd>
  </Ntry></Stmt></BkToCstmrStmt>
</Document>
"""
    with pytest.raises(CamtUnvollstaendigError):
        bank_service.importiere_camt053(ctx=ctx, bank_konto=bank_konto, xml_bytes=ohne_buchungsdatum.encode("utf-8"))


def test_camt053_fremdwaehrung_wird_blockiert(bank_service, bank_repo, ctx_factory):
    ctx = ctx_factory("7DI")
    bank_repo.upsert_bank_konto(id="BK-7DI-1", gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="7DI")
    bank_konto = bank_repo.get_bank_konto("BK-7DI-1")

    fremdwaehrung = CAMT_XML.replace('Ccy="EUR"', 'Ccy="USD"')
    with pytest.raises(FremdwaehrungNichtUnterstuetztError):
        bank_service.importiere_camt053(ctx=ctx, bank_konto=bank_konto, xml_bytes=fremdwaehrung.encode("utf-8"))


def test_zuordnen_manuell_lehnt_betrag_ueber_transaktionshoehe_ab(bank_service, bank_repo, basis_vertrag, ctx_factory):
    """Regression (Codex-Fund #4): 1000 EUR aus einer 600 EUR Zahlung
    zuzuordnen (und damit -1000 EUR zu buchen) muss abgelehnt werden."""

    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    bank_repo.upsert_bank_konto(id="BK-7DI-1", gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="7DI")
    bank_konto = bank_repo.get_bank_konto("BK-7DI-1")
    csv_text = "betrag,datum,referenz\n600.00,2026-04-06,sonstiges\n"
    mapping = CsvSpaltenMapping(betrag="betrag", buchungsdatum="datum", referenz="referenz")
    transaktion = bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=csv_text, mapping=mapping)[0]

    with pytest.raises(ZuordnungUngueltigError):
        bank_service.zuordnen_manuell(
            ctx=ctx, transaktion=transaktion, konto=konto, betrag_cent=100_000, beleg_referenz="Fehlerhafte Zuordnung"
        )
    assert bank_service._op_service.berechne_saldo(konto.id).saldo_cent == 0  # keine Nebenwirkung


def test_zuordnen_manuell_teilzuordnung_laesst_rest_unzugeordnet(bank_service, bank_repo, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    bank_repo.upsert_bank_konto(id="BK-7DI-1", gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="7DI")
    bank_konto = bank_repo.get_bank_konto("BK-7DI-1")
    csv_text = "betrag,datum,referenz\n600.00,2026-04-06,sonstiges\n"
    mapping = CsvSpaltenMapping(betrag="betrag", buchungsdatum="datum", referenz="referenz")
    transaktion = bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=csv_text, mapping=mapping)[0]

    bank_service.zuordnen_manuell(ctx=ctx, transaktion=transaktion, konto=konto, betrag_cent=400_00, beleg_referenz="Teilzuordnung")
    assert bank_repo.zugeordneter_betrag(transaktion.id) == 400_00

    with pytest.raises(ZuordnungUngueltigError):
        bank_service.zuordnen_manuell(
            ctx=ctx, transaktion=transaktion, konto=konto, betrag_cent=300_00, beleg_referenz="Zu viel für den Rest"
        )
    # verbleibender Rest (200,00 EUR) darf zugeordnet werden
    bank_service.zuordnen_manuell(ctx=ctx, transaktion=transaktion, konto=konto, betrag_cent=200_00, beleg_referenz="Rest")
    assert bank_repo.zugeordneter_betrag(transaktion.id) == 600_00


def test_cross_tenant_zuordnung_hat_keine_op_nebenwirkung_ueber_service(bank_service, bank_repo, stammdaten_repo, ctx_factory):
    """Regression (Codex-Fund #5): die service-seitige Vorabprüfung darf
    NIE eine OP-Zeile hinterlassen, wenn die Zuordnung an der
    Gesellschaftsprüfung scheitert."""

    stammdaten_repo.upsert_gesellschaft(id="7DI", name="7D Immobilien GmbH")
    stammdaten_repo.upsert_gesellschaft(id="MABAU", name="MaBau Beteiligungs GmbH")
    stammdaten_repo.upsert_objekt(id="616", gesellschaft_id="MABAU", bezeichnung="Fockygasse")
    stammdaten_repo.upsert_einheit(id="616-TOP1", objekt_id="616", bezeichnung="Top 1", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten_repo.upsert_debitor(id="DEB-2", name="Andere Mieterin")
    stammdaten_repo.upsert_vertrag(
        id="V-616-1", einheit_id="616-TOP1", debitor_id="DEB-2", gesellschaft_id="MABAU",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    vertrag_mabau = stammdaten_repo.get_vertrag("V-616-1")
    konto_mabau = stammdaten_repo.get_or_create_konto(vertrag=vertrag_mabau)

    bank_repo.upsert_bank_konto(id="BK-7DI-1", gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="7DI")
    bank_konto_7di = bank_repo.get_bank_konto("BK-7DI-1")
    ctx_mabau = ctx_factory("MABAU")
    csv_text = "betrag,datum,referenz\n600.00,2026-04-06,sonstiges\n"
    mapping = CsvSpaltenMapping(betrag="betrag", buchungsdatum="datum", referenz="referenz")
    transaktion = bank_service.importiere_csv(
        ctx=ctx_factory("7DI"), bank_konto=bank_konto_7di, text=csv_text, mapping=mapping
    )[0]

    with pytest.raises(CrossTenantError):
        bank_service.zuordnen_manuell(
            ctx=ctx_mabau, transaktion=transaktion, konto=konto_mabau, betrag_cent=600_00, beleg_referenz="Unbefugt"
        )

    # Keine Phantom-Zahlung wurde für konto_mabau gebucht.
    assert bank_service._op_service.berechne_saldo(konto_mabau.id).saldo_cent == 0
