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
    VorgangIdKonfliktError,
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
            bank_transaktion_id=transaktion.id, op_position_id=_dummy_op_position(bank_service, konto_mabau, ctx_factory),
            betrag_cent=60_000, match_typ="MANUELL", vorgang_id="TEST-VORGANG-1",
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
            ctx=ctx, transaktion=transaktion, konto=konto, betrag_cent=100_000, beleg_referenz="Fehlerhafte Zuordnung",
            vorgang_id="VORGANG-ABLEHNUNG",
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

    bank_service.zuordnen_manuell(
        ctx=ctx, transaktion=transaktion, konto=konto, betrag_cent=400_00, beleg_referenz="Teilzuordnung",
        vorgang_id="VORGANG-1",
    )
    assert bank_repo.zugeordneter_betrag(transaktion.id) == 400_00

    with pytest.raises(ZuordnungUngueltigError):
        bank_service.zuordnen_manuell(
            ctx=ctx, transaktion=transaktion, konto=konto, betrag_cent=300_00, beleg_referenz="Zu viel für den Rest",
            vorgang_id="VORGANG-2-ZU-VIEL",
        )
    # verbleibender Rest (200,00 EUR) darf zugeordnet werden
    bank_service.zuordnen_manuell(
        ctx=ctx, transaktion=transaktion, konto=konto, betrag_cent=200_00, beleg_referenz="Rest",
        vorgang_id="VORGANG-3-REST",
    )
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
            ctx=ctx_mabau, transaktion=transaktion, konto=konto_mabau, betrag_cent=600_00, beleg_referenz="Unbefugt",
            vorgang_id="VORGANG-CROSS-TENANT",
        )

    # Keine Phantom-Zahlung wurde für konto_mabau gebucht.
    assert bank_service._op_service.berechne_saldo(konto_mabau.id).saldo_cent == 0


def test_zuordnung_fehler_nach_op_buchung_rollt_alles_zurueck(bank_service, basis_vertrag, ctx_factory, monkeypatch):
    """Regression (Codex-Rückprüfung Bug 1): create_zuordnung wirft NACH
    erfolgreicher OP-Buchung einen unerwarteten Fehler (z. B. einen
    Speicher-/Absturzfehler). OP-Buchung und Zuordnung laufen in EINER
    DB-Transaktion; der Fehler darf daher nicht dazu führen, dass die
    Zahlung im Ledger stehen bleibt, ohne dass eine Zuordnung existiert."""

    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    bank_service._repository.upsert_bank_konto(
        id="BK-7DI-1", gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="7DI"
    )
    bank_konto = bank_service._repository.get_bank_konto("BK-7DI-1")
    csv_text = "betrag,datum,referenz\n600.00,2026-04-06,sonstiges\n"
    mapping = CsvSpaltenMapping(betrag="betrag", buchungsdatum="datum", referenz="referenz")
    transaktion = bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=csv_text, mapping=mapping)[0]

    def _simulierter_absturz(*args, **kwargs):
        raise RuntimeError("simulierter Speicher-/Absturzfehler nach erfolgreicher OP-Buchung")

    monkeypatch.setattr(bank_service._repository, "create_zuordnung", _simulierter_absturz)

    with pytest.raises(RuntimeError):
        bank_service.zuordnen_manuell(
            ctx=ctx, transaktion=transaktion, konto=konto, betrag_cent=60_000, beleg_referenz="Absturztest",
            vorgang_id="VORGANG-ABSTURZ",
        )

    # Kein Teilzustand: weder Saldo-Auswirkung noch eine hängende Zuordnung.
    assert bank_service._op_service.berechne_saldo(konto.id).saldo_cent == 0
    assert bank_service._repository.zugeordneter_betrag(transaktion.id) == 0


def test_ruecklastschrift_lehnt_dieselbe_positive_transaktion_ab(bank_service, bank_repo, basis_vertrag, ctx_factory):
    """Regression (Codex-Rückprüfung Bug 2): dieselbe positive Bankzahlung,
    die bereits als Zahlung zugeordnet wurde, darf NICHT nochmal als
    (angebliche) Rücklastschrift-Transaktion übergeben und akzeptiert
    werden."""

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

    with pytest.raises(ZuordnungUngueltigError):
        bank_service.verarbeite_ruecklastschrift(
            ctx=ctx, transaktion=transaktion, original_op_position=zahlung_op, konto=konto,
        )

    # Saldo unverändert (-600,00 aus der einen echten Zahlung) - keine "Rücklastschrift" aus dem Nichts.
    assert bank_service._op_service.berechne_saldo(konto.id).saldo_cent == -60_000


def test_ruecklastschrift_lehnt_falsches_bankkonto_ab(bank_service, bank_repo, basis_vertrag, ctx_factory):
    """Eine echte negative Transaktion auf einem ANDEREN Bankkonto als dem
    der Ursprungszahlung darf nicht als deren Rücklastschrift durchgehen."""

    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    bank_repo.upsert_bank_konto(id="BK-7DI-1", gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="7DI Haupt")
    bank_repo.upsert_bank_konto(id="BK-7DI-2", gesellschaft_id="7DI", iban="AT000000000000000001", bezeichnung="7DI Neben")
    bank_konto_1 = bank_repo.get_bank_konto("BK-7DI-1")
    bank_konto_2 = bank_repo.get_bank_konto("BK-7DI-2")
    mapping = CsvSpaltenMapping(betrag="betrag", buchungsdatum="datum", referenz="referenz")

    csv_zahlung = f"betrag,datum,referenz\n600.00,2026-04-06,VERTRAG:{vertrag.id}\n"
    transaktion = bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto_1, text=csv_zahlung, mapping=mapping)[0]
    ergebnis = bank_service.automatisch_zuordnen(ctx=ctx, transaktion=transaktion)
    assert ergebnis.zugeordnet is True
    zahlung_op = bank_service._op_service._op_repository.get(ergebnis.op_position_id)

    csv_rueck = f"betrag,datum,referenz\n-600.00,2026-04-10,RUECKLASTSCHRIFT VERTRAG:{vertrag.id}\n"
    rueck_transaktion = bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto_2, text=csv_rueck, mapping=mapping)[0]

    with pytest.raises(ZuordnungUngueltigError):
        bank_service.verarbeite_ruecklastschrift(
            ctx=ctx, transaktion=rueck_transaktion, original_op_position=zahlung_op, konto=konto,
        )


def test_ruecklastschrift_kumulative_ruecklastgrenze_der_ursprungszahlung(bank_service, bank_repo, basis_vertrag, ctx_factory):
    """Mehrere Teil-Rücklastschriften dürfen in Summe nie mehr zurückbuchen
    als ursprünglich bezahlt wurde."""

    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    bank_repo.upsert_bank_konto(id="BK-7DI-1", gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="7DI")
    bank_konto = bank_repo.get_bank_konto("BK-7DI-1")
    mapping = CsvSpaltenMapping(betrag="betrag", buchungsdatum="datum", referenz="referenz")

    csv_zahlung = f"betrag,datum,referenz\n600.00,2026-04-06,VERTRAG:{vertrag.id}\n"
    transaktion = bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=csv_zahlung, mapping=mapping)[0]
    ergebnis = bank_service.automatisch_zuordnen(ctx=ctx, transaktion=transaktion)
    zahlung_op = bank_service._op_service._op_repository.get(ergebnis.op_position_id)

    csv_rueck_1 = f"betrag,datum,referenz\n-1000.00,2026-04-10,RUECKLASTSCHRIFT-1 VERTRAG:{vertrag.id}\n"
    rueck_1 = bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=csv_rueck_1, mapping=mapping)[0]
    bank_service.verarbeite_ruecklastschrift(
        ctx=ctx, transaktion=rueck_1, original_op_position=zahlung_op, konto=konto, betrag_cent=400_00,
    )

    csv_rueck_2 = f"betrag,datum,referenz\n-1000.00,2026-04-11,RUECKLASTSCHRIFT-2 VERTRAG:{vertrag.id}\n"
    rueck_2 = bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=csv_rueck_2, mapping=mapping)[0]
    with pytest.raises(ZuordnungUngueltigError):
        # 400,00 bereits zurückgebucht + 300,00 würde 600,00 (Ursprungsbetrag) überschreiten.
        bank_service.verarbeite_ruecklastschrift(
            ctx=ctx, transaktion=rueck_2, original_op_position=zahlung_op, konto=konto, betrag_cent=300_00,
        )
    # Der verbleibende Rest (200,00) darf hingegen noch zurückgebucht werden.
    bank_service.verarbeite_ruecklastschrift(
        ctx=ctx, transaktion=rueck_2, original_op_position=zahlung_op, konto=konto, betrag_cent=200_00,
    )


def test_ruecklastschrift_verfuegbarer_belastungsbetrag_der_transaktion(bank_service, bank_repo, stammdaten_repo, ctx_factory):
    """Eine Sammel-Rücklastschrift darf über mehrere Ursprungszahlungen
    hinweg nie mehr verwenden, als ihr eigener (negativer) Betrag hergibt."""

    ctx = ctx_factory("7DI")
    stammdaten_repo.upsert_gesellschaft(id="7DI", name="7D Immobilien GmbH")
    stammdaten_repo.upsert_objekt(id="601", gesellschaft_id="7DI", bezeichnung="Am Corso")
    stammdaten_repo.upsert_einheit(id="601-TOP1", objekt_id="601", bezeichnung="Top 1", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten_repo.upsert_einheit(id="601-TOP2", objekt_id="601", bezeichnung="Top 2", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten_repo.upsert_debitor(id="DEB-A", name="Mieterin A")
    stammdaten_repo.upsert_debitor(id="DEB-B", name="Mieter B")
    stammdaten_repo.upsert_vertrag(
        id="V-601-A", einheit_id="601-TOP1", debitor_id="DEB-A", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    stammdaten_repo.upsert_vertrag(
        id="V-601-B", einheit_id="601-TOP2", debitor_id="DEB-B", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    vertrag_a = stammdaten_repo.get_vertrag("V-601-A")
    vertrag_b = stammdaten_repo.get_vertrag("V-601-B")
    konto_a = stammdaten_repo.get_or_create_konto(vertrag=vertrag_a)
    konto_b = stammdaten_repo.get_or_create_konto(vertrag=vertrag_b)

    bank_repo.upsert_bank_konto(id="BK-7DI-1", gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="7DI")
    bank_konto = bank_repo.get_bank_konto("BK-7DI-1")
    mapping = CsvSpaltenMapping(betrag="betrag", buchungsdatum="datum", referenz="referenz")

    zahlung_a = bank_service.importiere_csv(
        ctx=ctx, bank_konto=bank_konto,
        text=f"betrag,datum,referenz\n400.00,2026-04-06,VERTRAG:{vertrag_a.id}\n", mapping=mapping,
    )[0]
    ergebnis_a = bank_service.automatisch_zuordnen(ctx=ctx, transaktion=zahlung_a)
    op_a = bank_service._op_service._op_repository.get(ergebnis_a.op_position_id)

    zahlung_b = bank_service.importiere_csv(
        ctx=ctx, bank_konto=bank_konto,
        text=f"betrag,datum,referenz\n400.00,2026-04-06,VERTRAG:{vertrag_b.id}\n", mapping=mapping,
    )[0]
    ergebnis_b = bank_service.automatisch_zuordnen(ctx=ctx, transaktion=zahlung_b)
    op_b = bank_service._op_service._op_repository.get(ergebnis_b.op_position_id)

    # Eine einzige Sammel-Rücklastschrift über 500,00 EUR soll beide Zahlungen abdecken.
    sammel_rueck = bank_service.importiere_csv(
        ctx=ctx, bank_konto=bank_konto,
        text="betrag,datum,referenz\n-500.00,2026-04-10,SAMMEL-RUECKLASTSCHRIFT\n", mapping=mapping,
    )[0]
    bank_service.verarbeite_ruecklastschrift(
        ctx=ctx, transaktion=sammel_rueck, original_op_position=op_a, konto=konto_a, betrag_cent=400_00,
    )
    with pytest.raises(ZuordnungUngueltigError):
        # 400,00 bereits von dieser Transaktion verwendet + 100,00 würde ihren eigenen Betrag (500,00) NICHT
        # überschreiten - wohl aber, wenn wir stattdessen 200,00 für Konto B verlangen.
        bank_service.verarbeite_ruecklastschrift(
            ctx=ctx, transaktion=sammel_rueck, original_op_position=op_b, konto=konto_b, betrag_cent=200_00,
        )
    # Der tatsächlich noch verfügbare Rest (100,00) darf hingegen verwendet werden.
    bank_service.verarbeite_ruecklastschrift(
        ctx=ctx, transaktion=sammel_rueck, original_op_position=op_b, konto=konto_b, betrag_cent=100_00,
    )


def test_vorgang_id_unterscheidet_retry_von_unabhaengiger_teilzuordnung(bank_service, bank_repo, basis_vertrag, ctx_factory):
    """Regression: derselbe `vorgang_id`-Wert (Retry, z. B. nach einem
    Netzwerk-Timeout) darf keine zweite Zuordnung erzeugen; ein NEUER Wert
    für eine echte, unabhängige zweite Teilzuordnung mit zufällig
    identischem Betrag hingegen schon."""

    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    bank_repo.upsert_bank_konto(id="BK-7DI-1", gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="7DI")
    bank_konto = bank_repo.get_bank_konto("BK-7DI-1")
    csv_text = "betrag,datum,referenz\n600.00,2026-04-06,sonstiges\n"
    mapping = CsvSpaltenMapping(betrag="betrag", buchungsdatum="datum", referenz="referenz")
    transaktion = bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=csv_text, mapping=mapping)[0]

    zuordnung_1 = bank_service.zuordnen_manuell(
        ctx=ctx, transaktion=transaktion, konto=konto, betrag_cent=200_00, beleg_referenz="Teilzahlung 1",
        vorgang_id="VORGANG-RETRY-TEST",
    )
    # Retry mit DERSELBEN vorgang_id -> No-Op, keine zweite Buchung.
    zuordnung_retry = bank_service.zuordnen_manuell(
        ctx=ctx, transaktion=transaktion, konto=konto, betrag_cent=200_00, beleg_referenz="Teilzahlung 1",
        vorgang_id="VORGANG-RETRY-TEST",
    )
    assert zuordnung_retry.id == zuordnung_1.id
    assert bank_repo.zugeordneter_betrag(transaktion.id) == 200_00

    # Eine ECHTE zweite, unabhängige Teilzuordnung mit zufällig identischem
    # Betrag (neue vorgang_id) muss hingegen als eigene Buchung durchgehen.
    zuordnung_2 = bank_service.zuordnen_manuell(
        ctx=ctx, transaktion=transaktion, konto=konto, betrag_cent=200_00, beleg_referenz="Teilzahlung 2",
        vorgang_id="VORGANG-UNABHAENGIG",
    )
    assert zuordnung_2.id != zuordnung_1.id
    assert bank_repo.zugeordneter_betrag(transaktion.id) == 400_00


def test_ruecklastschrift_ignoriert_manipuliertes_python_objekt_und_laedt_frisch_aus_db(
    bank_service, bank_repo, basis_vertrag, ctx_factory
):
    """Regression (Codex-Rückprüfung #1): nach regulärer Zuordnung wird NUR
    das vom Repository abgelöste Python-Objekt für `transaktion` manipuliert
    (tx.betrag_cent=-60000); die DB enthält weiterhin +60000. Die Prüfung
    darf sich nicht auf das übergebene Objekt verlassen, sondern muss
    Transaktion/Original-OP/Konto/Bankkonto per ID frisch und gesperrt aus
    der DB laden."""

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

    # Manipulation NUR am losgelösten Python-Objekt - die DB enthält
    # weiterhin den echten, positiven Betrag.
    transaktion.betrag_cent = -60_000

    with pytest.raises(ZuordnungUngueltigError):
        bank_service.verarbeite_ruecklastschrift(
            ctx=ctx, transaktion=transaktion, original_op_position=zahlung_op, konto=konto,
        )

    assert bank_service._op_service.berechne_saldo(konto.id).saldo_cent == -60_000


def test_vorgang_id_wiederverwendung_fuer_andere_transaktion_ist_konflikt_und_rollt_zurueck(
    bank_service, bank_repo, basis_vertrag, ctx_factory
):
    """Regression (Codex-Rückprüfung #3): Bank A +60000 wird mit
    vorgang_id='SAME-OPERATION' zugeordnet; danach wird dieselbe vorgang_id
    für eine ANDERE, separate Transaktion (+5000) verwendet. Das darf nicht
    die alte Zuordnung zurückliefern, während der neu gebuchte OP bestehen
    bleibt - Wiederverwendung mit anderer Transaktion/Betrag muss ein
    Konflikt sein, der die GESAMTE Buchung zurückrollt; ein echter
    identischer Retry bleibt weiterhin ein No-op (siehe
    `test_vorgang_id_unterscheidet_retry_von_unabhaengiger_teilzuordnung`)."""

    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    bank_repo.upsert_bank_konto(id="BK-7DI-1", gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="7DI")
    bank_konto = bank_repo.get_bank_konto("BK-7DI-1")
    mapping = CsvSpaltenMapping(betrag="betrag", buchungsdatum="datum", referenz="referenz")

    tx_a = bank_service.importiere_csv(
        ctx=ctx, bank_konto=bank_konto, text="betrag,datum,referenz\n600.00,2026-04-06,A\n", mapping=mapping
    )[0]
    bank_service.zuordnen_manuell(
        ctx=ctx, transaktion=tx_a, konto=konto, betrag_cent=60_000, beleg_referenz="A",
        vorgang_id="SAME-OPERATION",
    )

    tx_b = bank_service.importiere_csv(
        ctx=ctx, bank_konto=bank_konto, text="betrag,datum,referenz\n50.00,2026-04-07,B\n", mapping=mapping
    )[0]
    with pytest.raises(VorgangIdKonfliktError):
        bank_service.zuordnen_manuell(
            ctx=ctx, transaktion=tx_b, konto=konto, betrag_cent=5_000, beleg_referenz="B",
            vorgang_id="SAME-OPERATION",
        )

    assert bank_service._op_service.berechne_saldo(konto.id).saldo_cent == -60_000
    assert bank_repo.zugeordneter_betrag(tx_b.id) == 0
