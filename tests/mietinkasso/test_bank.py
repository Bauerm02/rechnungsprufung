from __future__ import annotations

from datetime import date

import pytest

from mietinkasso.bank.importer import (
    CamtKontoMismatchError,
    CamtMehrteiligeBuchungError,
    CamtUnvollstaendigError,
    CsvSpaltenMapping,
)
from mietinkasso.bank.repository import BankRepository
from mietinkasso.bank.service import BankImportService
from mietinkasso.domain.enums import OPTyp
from mietinkasso.domain.exceptions import (
    BindungInkonsistentError,
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
      <Acct><Id><IBAN>AT000000000000000000</IBAN></Id></Acct>
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
      <Acct><Id><IBAN>AT000000000000000000</IBAN></Id></Acct>
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
  <BkToCstmrStmt><Stmt><Acct><Id><IBAN>AT000000000000000000</IBAN></Id></Acct><Ntry>
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


# ---------------------------------------------------------------------------
# CAMT.053 Konto-Validierung (Nutzerauftrag vor Paket C): EBICS-C53 kann
# kundenweite Sammeldateien mit MEHREREN Konten liefern - kein Stmt/Acct
# darf pauschal dem ausgewählten Bankkonto zugeordnet werden.
# ---------------------------------------------------------------------------


def test_camt053_korrektes_konto_wird_importiert(bank_service, bank_repo, ctx_factory):
    """Positivfall: ein einzelnes Stmt mit exakt der ausgewählten IBAN wird
    unverändert importiert (Regressionsschutz gegen eine zu strenge
    Konto-Validierung)."""

    ctx = ctx_factory("7DI")
    bank_repo.upsert_bank_konto(id="BK-7DI-1", gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="7DI")
    bank_konto = bank_repo.get_bank_konto("BK-7DI-1")

    transaktionen = bank_service.importiere_camt053(ctx=ctx, bank_konto=bank_konto, xml_bytes=CAMT_XML.encode("utf-8"))
    assert len(transaktionen) == 1


def test_camt053_fremdes_konto_im_zweiten_stmt_lehnt_gesamten_import_ab(bank_service, bank_repo, ctx_factory):
    """EBICS-C53-Gefahr: eine Datei mit ZWEI Stmt-Blöcken, von denen nur der
    ERSTE zum ausgewählten Konto gehört, darf NICHT teilweise (nur der
    passende Stmt) importiert werden - der GESAMTE Import wird abgelehnt,
    es bleiben NULL Transaktionen zurück."""

    ctx = ctx_factory("7DI")
    bank_repo.upsert_bank_konto(id="BK-7DI-1", gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="7DI")
    bank_konto = bank_repo.get_bank_konto("BK-7DI-1")

    gemischte_datei = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02">
  <BkToCstmrStmt>
    <Stmt>
      <Acct><Id><IBAN>AT000000000000000000</IBAN></Id></Acct>
      <Ntry>
        <Amt Ccy="EUR">100.00</Amt>
        <CdtDbtInd>CRDT</CdtDbtInd>
        <BookgDt><Dt>2026-04-06</Dt></BookgDt>
        <NtryDtls><TxDtls><AcctSvcrRef>REF-RICHTIG</AcctSvcrRef></TxDtls></NtryDtls>
      </Ntry>
    </Stmt>
    <Stmt>
      <Acct><Id><IBAN>AT999999999999999999</IBAN></Id></Acct>
      <Ntry>
        <Amt Ccy="EUR">200.00</Amt>
        <CdtDbtInd>CRDT</CdtDbtInd>
        <BookgDt><Dt>2026-04-06</Dt></BookgDt>
        <NtryDtls><TxDtls><AcctSvcrRef>REF-FREMD</AcctSvcrRef></TxDtls></NtryDtls>
      </Ntry>
    </Stmt>
  </BkToCstmrStmt>
</Document>
"""
    with pytest.raises(CamtKontoMismatchError, match="AT999999999999999999"):
        bank_service.importiere_camt053(ctx=ctx, bank_konto=bank_konto, xml_bytes=gemischte_datei.encode("utf-8"))
    assert bank_repo.list_unzugeordnet("BK-7DI-1") == []  # NICHTS wurde übernommen, auch nicht der passende Stmt


def test_camt053_fehlende_iban_lehnt_gesamten_import_ab(bank_service, bank_repo, ctx_factory):
    """Ein Stmt ohne (oder mit leerer) IBAN im Acct-Block wird abgelehnt -
    keine pauschale Zuordnung ohne geprüfte Kontokennung, auch wenn die
    Datei nur ein einziges Konto/Stmt enthält."""

    ctx = ctx_factory("7DI")
    bank_repo.upsert_bank_konto(id="BK-7DI-1", gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="7DI")
    bank_konto = bank_repo.get_bank_konto("BK-7DI-1")

    ohne_iban = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02">
  <BkToCstmrStmt>
    <Stmt>
      <Acct><Id><Othr><Id>INTERNE-KONTOKENNUNG</Id></Othr></Id></Acct>
      <Ntry>
        <Amt Ccy="EUR">100.00</Amt>
        <CdtDbtInd>CRDT</CdtDbtInd>
        <BookgDt><Dt>2026-04-06</Dt></BookgDt>
      </Ntry>
    </Stmt>
  </BkToCstmrStmt>
</Document>
"""
    with pytest.raises(CamtKontoMismatchError, match="IBAN"):
        bank_service.importiere_camt053(ctx=ctx, bank_konto=bank_konto, xml_bytes=ohne_iban.encode("utf-8"))
    assert bank_repo.list_unzugeordnet("BK-7DI-1") == []


def test_camt053_ntry_ausserhalb_des_geprueften_stmt_lehnt_gesamten_import_ab(bank_service, bank_repo, ctx_factory):
    """Codex-Rückprüfung (fa768be): `_pruefe_stmt_konten` validierte bisher
    JEDEN Stmt/Acct, aber die eigentliche Ntry-Sammlung lief weiterhin
    über `root.iter()` und fand damit auch eine Ntry, die GAR NICHT
    innerhalb eines geprüften Stmt liegt (hier: eine Ntry auf Ebene von
    BkToCstmrStmt, ein Geschwisterelement von Stmt statt dessen Kind) -
    diese Bewegung wurde bisher trotz "bestandener" Kontoprüfung ohne
    geprüfte Kontobindung importiert. Muss den GESAMTEN Import ablehnen,
    NULL Transaktionen übrig."""

    ctx = ctx_factory("7DI")
    bank_repo.upsert_bank_konto(id="BK-7DI-1", gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="7DI")
    bank_konto = bank_repo.get_bank_konto("BK-7DI-1")

    ntry_ausserhalb_stmt = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02">'
        '<BkToCstmrStmt>'
        '<Stmt><Acct><Id><IBAN>AT000000000000000000</IBAN></Id></Acct></Stmt>'
        '<Ntry><Amt Ccy="EUR">10.00</Amt><CdtDbtInd>CRDT</CdtDbtInd>'
        '<BookgDt><Dt>2026-04-01</Dt></BookgDt></Ntry>'
        '</BkToCstmrStmt></Document>'
    )
    with pytest.raises(CamtKontoMismatchError, match="außerhalb"):
        bank_service.importiere_camt053(ctx=ctx, bank_konto=bank_konto, xml_bytes=ntry_ausserhalb_stmt.encode("utf-8"))
    assert bank_repo.list_unzugeordnet("BK-7DI-1") == []


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


# ---------------------------------------------------------------------------
# Verknüpfung mit bestehender Zahlung (Auftrag 12.09., Paket B, Punkt 2):
# eine Rohtransaktion wird mit einer BEREITS BESTEHENDEN ZAHLUNG-OP verlinkt,
# OHNE einen zweiten Zahlungseintrag zu buchen - Fachregel: bestehende
# Mieterkonto-Buchungen dürfen bei einem späteren Rohbankimport nicht
# doppelt gutgeschrieben werden.
# ---------------------------------------------------------------------------


def _importiere_eine_csv_transaktion(bank_service, bank_repo, ctx, *, betrag_text: str, referenz: str, bank_konto_id="BK-7DI-1"):
    if bank_repo.get_bank_konto(bank_konto_id) is None:
        bank_repo.upsert_bank_konto(id=bank_konto_id, gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="7DI")
    bank_konto = bank_repo.get_bank_konto(bank_konto_id)
    mapping = CsvSpaltenMapping(betrag="betrag", buchungsdatum="datum", referenz="referenz")
    csv_text = f"betrag,datum,referenz\n{betrag_text},2026-04-06,{referenz}\n"
    return bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=csv_text, mapping=mapping)[0]


def test_verknuepfen_erzeugt_keine_neue_op_und_veraendert_saldo_nicht(bank_service, bank_repo, op_service, basis_vertrag, ctx_factory):
    """Der zentrale Fall: eine ZAHLUNG wurde bereits VOR dem Bankfeed
    gebucht (z. B. manuell/Intake) - die spätere Rohtransaktion bestätigt
    sie nur, statt sie ein zweites Mal gutzuschreiben."""

    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    bestehende_zahlung = op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.ZAHLUNG, betrag_cent=60_000,
        belegdatum=date(2026, 4, 1), buchungsdatum=date(2026, 4, 1), faelligkeit=None,
        beleg_referenz="Vor dem Bankfeed manuell erfasste Zahlung",
    )
    saldo_vorher = op_service.berechne_saldo(konto.id).saldo_cent
    transaktion = _importiere_eine_csv_transaktion(bank_service, bank_repo, ctx, betrag_text="600.00", referenz="egal")

    zuordnung = bank_service.verknuepfe_mit_bestehender_zahlung(
        ctx=ctx, transaktion=transaktion, op_position=bestehende_zahlung, konto=konto, betrag_cent=60_000,
        vorgang_id="VERKNUEPFT-1",
    )

    assert zuordnung.op_position_id == bestehende_zahlung.id
    assert op_service.berechne_saldo(konto.id).saldo_cent == saldo_vorher  # KEINE Doppelgutschrift
    assert len(op_service.list_alle_positionen(konto.id)) == 1  # weiterhin nur die EINE ursprüngliche Zahlung
    assert bank_repo.zugeordneter_betrag(transaktion.id) == 60_000


def test_verknuepfen_replay_derselben_vorgang_id_ist_wirkungslos(bank_service, bank_repo, op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    zahlung = op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.ZAHLUNG, betrag_cent=60_000,
        belegdatum=date(2026, 4, 1), buchungsdatum=date(2026, 4, 1), faelligkeit=None, beleg_referenz="Zahlung",
    )
    transaktion = _importiere_eine_csv_transaktion(bank_service, bank_repo, ctx, betrag_text="600.00", referenz="egal")

    erste = bank_service.verknuepfe_mit_bestehender_zahlung(
        ctx=ctx, transaktion=transaktion, op_position=zahlung, konto=konto, betrag_cent=60_000, vorgang_id="RETRY-1",
    )
    zweite = bank_service.verknuepfe_mit_bestehender_zahlung(
        ctx=ctx, transaktion=transaktion, op_position=zahlung, konto=konto, betrag_cent=60_000, vorgang_id="RETRY-1",
    )
    assert erste.id == zweite.id
    assert bank_repo.zugeordneter_betrag(transaktion.id) == 60_000  # keine Verdopplung


def test_verknuepfen_link_duplikat_mit_anderer_vorgang_id_wird_abgelehnt(bank_service, bank_repo, op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    zahlung = op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.ZAHLUNG, betrag_cent=60_000,
        belegdatum=date(2026, 4, 1), buchungsdatum=date(2026, 4, 1), faelligkeit=None, beleg_referenz="Zahlung",
    )
    transaktion = _importiere_eine_csv_transaktion(bank_service, bank_repo, ctx, betrag_text="600.00", referenz="egal")
    bank_service.verknuepfe_mit_bestehender_zahlung(
        ctx=ctx, transaktion=transaktion, op_position=zahlung, konto=konto, betrag_cent=30_000, vorgang_id="ERSTE-VERKNUEPFUNG",
    )
    with pytest.raises(ZuordnungUngueltigError, match="Link-Duplikat"):
        bank_service.verknuepfe_mit_bestehender_zahlung(
            ctx=ctx, transaktion=transaktion, op_position=zahlung, konto=konto, betrag_cent=30_000,
            vorgang_id="ZWEITE-VERKNUEPFUNG-DESSELBEN-PAARS",
        )
    assert bank_repo.zugeordneter_betrag(transaktion.id) == 30_000  # unverändert, kein zweiter Link


def test_verknuepfen_betrag_ueber_restbetrag_der_zahlung_wird_abgelehnt(bank_service, bank_repo, op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    zahlung = op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.ZAHLUNG, betrag_cent=60_000,
        belegdatum=date(2026, 4, 1), buchungsdatum=date(2026, 4, 1), faelligkeit=None, beleg_referenz="Zahlung",
    )
    transaktion = _importiere_eine_csv_transaktion(bank_service, bank_repo, ctx, betrag_text="900.00", referenz="egal")
    with pytest.raises(ZuordnungUngueltigError, match="Restbetrag der"):
        bank_service.verknuepfe_mit_bestehender_zahlung(
            ctx=ctx, transaktion=transaktion, op_position=zahlung, konto=konto, betrag_cent=90_000,
            vorgang_id="ZU-VIEL-FUER-DIE-ZAHLUNG",
        )
    assert bank_repo.zugeordneter_betrag(transaktion.id) == 0


def test_verknuepfen_betrag_ueber_restbetrag_der_transaktion_wird_abgelehnt(bank_service, bank_repo, op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    zahlung = op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.ZAHLUNG, betrag_cent=90_000,
        belegdatum=date(2026, 4, 1), buchungsdatum=date(2026, 4, 1), faelligkeit=None, beleg_referenz="Zahlung",
    )
    transaktion = _importiere_eine_csv_transaktion(bank_service, bank_repo, ctx, betrag_text="600.00", referenz="egal")
    with pytest.raises(ZuordnungUngueltigError, match="verbleibenden"):
        bank_service.verknuepfe_mit_bestehender_zahlung(
            ctx=ctx, transaktion=transaktion, op_position=zahlung, konto=konto, betrag_cent=90_000,
            vorgang_id="ZU-VIEL-FUER-DIE-TRANSAKTION",
        )


def test_verknuepfen_falscher_optyp_wird_abgelehnt(bank_service, bank_repo, op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    soll = op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.SOLL, betrag_cent=60_000,
        belegdatum=date(2026, 4, 1), buchungsdatum=date(2026, 4, 1), faelligkeit=date(2026, 4, 5), beleg_referenz="Miete",
    )
    transaktion = _importiere_eine_csv_transaktion(bank_service, bank_repo, ctx, betrag_text="600.00", referenz="egal")
    with pytest.raises(ZuordnungUngueltigError, match="ZAHLUNG-OP"):
        bank_service.verknuepfe_mit_bestehender_zahlung(
            ctx=ctx, transaktion=transaktion, op_position=soll, konto=konto, betrag_cent=60_000, vorgang_id="FALSCHER-TYP",
        )


def test_verknuepfen_storniertes_op_wird_abgelehnt(bank_service, bank_repo, op_service, basis_vertrag, ctx_factory):
    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    zahlung = op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.ZAHLUNG, betrag_cent=60_000,
        belegdatum=date(2026, 4, 1), buchungsdatum=date(2026, 4, 1), faelligkeit=None, beleg_referenz="Zahlung",
    )
    op_service.storniere_und_korrigiere(ctx=ctx, konto=konto, original_id=zahlung.id, aenderungsgrund="Fehlbuchung")
    transaktion = _importiere_eine_csv_transaktion(bank_service, bank_repo, ctx, betrag_text="600.00", referenz="egal")
    with pytest.raises(ZuordnungUngueltigError, match="storniert"):
        bank_service.verknuepfe_mit_bestehender_zahlung(
            ctx=ctx, transaktion=transaktion, op_position=zahlung, konto=konto, betrag_cent=60_000, vorgang_id="STORNIERT",
        )


def test_verknuepfen_falsches_konto_wird_abgelehnt(bank_service, bank_repo, op_service, stammdaten_repo, basis_vertrag, ctx_factory):
    vertrag, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    zahlung = op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.ZAHLUNG, betrag_cent=60_000,
        belegdatum=date(2026, 4, 1), buchungsdatum=date(2026, 4, 1), faelligkeit=None, beleg_referenz="Zahlung",
    )
    stammdaten_repo.upsert_einheit(id="601-TOP9", objekt_id="601", bezeichnung="Top 9", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten_repo.upsert_debitor(id="DEB-ANDERE", name="Andere Mieterin")
    stammdaten_repo.upsert_vertrag(
        id="V-ANDERER", einheit_id="601-TOP9", debitor_id="DEB-ANDERE", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    anderer_vertrag = stammdaten_repo.get_vertrag("V-ANDERER")
    anderes_konto = stammdaten_repo.get_or_create_konto(vertrag=anderer_vertrag)
    transaktion = _importiere_eine_csv_transaktion(bank_service, bank_repo, ctx, betrag_text="600.00", referenz="egal")

    with pytest.raises(BindungInkonsistentError):
        bank_service.verknuepfe_mit_bestehender_zahlung(
            ctx=ctx, transaktion=transaktion, op_position=zahlung, konto=anderes_konto, betrag_cent=60_000,
            vorgang_id="FALSCHES-KONTO",
        )


def test_verknuepfen_zwei_transaktionen_koennen_eine_zahlung_gemeinsam_erklaeren(bank_service, bank_repo, op_service, basis_vertrag, ctx_factory):
    """Zwei ECHTE, unabhängige Teilbeträge (z. B. weil eine Sammelzahlung
    auf zwei Bankzeilen aufgeteilt eingegangen ist) dürfen gemeinsam,
    aber nie über den Restbetrag der Zahlung hinaus, verknüpft werden."""

    _, konto = basis_vertrag
    ctx = ctx_factory("7DI")
    zahlung = op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.ZAHLUNG, betrag_cent=60_000,
        belegdatum=date(2026, 4, 1), buchungsdatum=date(2026, 4, 1), faelligkeit=None, beleg_referenz="Zahlung",
    )
    tx_a = _importiere_eine_csv_transaktion(bank_service, bank_repo, ctx, betrag_text="300.00", referenz="teil-a")
    tx_b = _importiere_eine_csv_transaktion(bank_service, bank_repo, ctx, betrag_text="300.00", referenz="teil-b")

    bank_service.verknuepfe_mit_bestehender_zahlung(
        ctx=ctx, transaktion=tx_a, op_position=zahlung, konto=konto, betrag_cent=30_000, vorgang_id="TEIL-A",
    )
    bank_service.verknuepfe_mit_bestehender_zahlung(
        ctx=ctx, transaktion=tx_b, op_position=zahlung, konto=konto, betrag_cent=30_000, vorgang_id="TEIL-B",
    )
    assert len(op_service.list_alle_positionen(konto.id)) == 1  # weiterhin nur die eine ursprüngliche Zahlung

    tx_c = _importiere_eine_csv_transaktion(bank_service, bank_repo, ctx, betrag_text="100.00", referenz="ueberschuss")
    with pytest.raises(ZuordnungUngueltigError, match="Restbetrag der"):
        bank_service.verknuepfe_mit_bestehender_zahlung(
            ctx=ctx, transaktion=tx_c, op_position=zahlung, konto=konto, betrag_cent=10_000, vorgang_id="TEIL-C-ZU-VIEL",
        )


def test_verknuepfen_replay_ueber_fremdes_konto_liefert_nicht_die_fremde_zuordnung(
    bank_service, bank_repo, op_service, stammdaten_repo, basis_vertrag, ctx_factory,
):
    """Codex-Rückprüfung Paket B: der frühere Code prüfte den Replay-
    Kurzschluss (identische vorgang_id/Transaktion/OP/Betrag) VOR den
    Bindungs-/Mandantenprüfungen. Ein Aufrufer B mit einem EIGENEN,
    an sich berechtigten Konto konnte dadurch, wenn er die IDs einer
    FREMDEN Transaktion/OP von Gesellschaft A plus deren vorgang_id/
    Betrag kannte oder wiederverwendete, die FREMDE Zuordnung als
    vermeintlichen "eigenen Replay" zurückbekommen - ohne dass deren
    tatsächliche Konto-/Mandantenzugehörigkeit je geprüft wurde. Muss
    stattdessen unabhängig vom Replay-Zustand mit einem Bindungsfehler
    abgelehnt werden."""

    _, konto_a = basis_vertrag
    ctx_a = ctx_factory("7DI")
    zahlung_a = op_service.buchen(
        ctx=ctx_a, konto=konto_a, typ=OPTyp.ZAHLUNG, betrag_cent=60_000,
        belegdatum=date(2026, 4, 1), buchungsdatum=date(2026, 4, 1), faelligkeit=None, beleg_referenz="Zahlung A",
    )
    transaktion_a = _importiere_eine_csv_transaktion(bank_service, bank_repo, ctx_a, betrag_text="600.00", referenz="a")
    bank_service.verknuepfe_mit_bestehender_zahlung(
        ctx=ctx_a, transaktion=transaktion_a, op_position=zahlung_a, konto=konto_a, betrag_cent=60_000,
        vorgang_id="GEMEINSAME-VORGANG-ID",
    )

    stammdaten_repo.upsert_gesellschaft(id="ANDERE-GESELLSCHAFT", name="Andere GmbH")
    stammdaten_repo.upsert_objekt(id="999", gesellschaft_id="ANDERE-GESELLSCHAFT", bezeichnung="Fremdes Objekt")
    stammdaten_repo.upsert_einheit(id="999-TOP1", objekt_id="999", bezeichnung="Top 1", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten_repo.upsert_debitor(id="DEB-FREMD", name="Fremde Mieterin")
    stammdaten_repo.upsert_vertrag(
        id="V-FREMD", einheit_id="999-TOP1", debitor_id="DEB-FREMD", gesellschaft_id="ANDERE-GESELLSCHAFT",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    konto_b = stammdaten_repo.get_or_create_konto(vertrag=stammdaten_repo.get_vertrag("V-FREMD"))
    ctx_b = ctx_factory("ANDERE-GESELLSCHAFT")

    with pytest.raises(BindungInkonsistentError):
        bank_service.verknuepfe_mit_bestehender_zahlung(
            ctx=ctx_b, transaktion=transaktion_a, op_position=zahlung_a, konto=konto_b, betrag_cent=60_000,
            vorgang_id="GEMEINSAME-VORGANG-ID",
        )
    # Die echte Zuordnung von A bleibt unverändert - kein fremder
    # Zweitzugriff hat sie angerührt.
    assert bank_repo.zugeordneter_betrag(transaktion_a.id) == 60_000


def test_verknuepfen_datei_sqlite_gleichzeitige_verknuepfungen_ueberschreiten_zahlung_nicht(tmp_path):
    """Codex-Rückprüfung Paket B: unter Datei-SQLite ist `with_for_update`
    ein Kein-Op (kein echtes Zeilen-Locking) - ohne echte
    Schreibserialisierung (`schreibgesperrte_session`) könnten zwei ECHT
    gleichzeitige Verknüpfungsversuche denselben, noch nicht committeten
    Restbetrag der Zahlung lesen und GEMEINSAM über sie hinausgehen.
    Reproduktion mit zwei echten Threads auf eine echte Datei-SQLite-DB
    (bewusst NICHT `:memory:`/StaticPool, wo eine einzige geteilte
    Verbindung die Frage gar nicht stellt)."""

    import threading

    from mietinkasso.auth.service import AuthContext
    from mietinkasso.domain.enums import Rolle
    from mietinkasso.infrastructure.db.base import Base
    from mietinkasso.infrastructure.db.session import build_engine, build_session_factory
    from mietinkasso.stammdaten.repository import StammdatenRepository

    db_pfad = tmp_path / "verknuepfen-nebenlaeufig.db"
    engine = build_engine(f"sqlite:///{db_pfad}")
    Base.metadata.create_all(engine)
    engine.dispose()
    session_factory = build_session_factory(f"sqlite:///{db_pfad}")

    stammdaten_repo = StammdatenRepository(session_factory)
    op_service = OPService(OPRepository(session_factory), stammdaten_repo)
    bank_repo = BankRepository(session_factory)
    bank_service = BankImportService(bank_repo, stammdaten_repo, op_service)
    ctx = AuthContext(user_id="test", rolle=Rolle.BUCHHALTUNG, gesellschaft_ids=frozenset({"7DI"}))

    stammdaten_repo.upsert_gesellschaft(id="7DI", name="7D Immobilien GmbH")
    stammdaten_repo.upsert_objekt(id="601", gesellschaft_id="7DI", bezeichnung="Am Corso")
    stammdaten_repo.upsert_einheit(id="601-TOP3", objekt_id="601", bezeichnung="Top 3", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten_repo.upsert_debitor(id="DEB-1001", name="Max Mustermieter")
    stammdaten_repo.upsert_vertrag(
        id="V-601-3", einheit_id="601-TOP3", debitor_id="DEB-1001", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    konto = stammdaten_repo.get_or_create_konto(vertrag=stammdaten_repo.get_vertrag("V-601-3"))

    zahlung = op_service.buchen(
        ctx=ctx, konto=konto, typ=OPTyp.ZAHLUNG, betrag_cent=100_000,
        belegdatum=date(2026, 4, 1), buchungsdatum=date(2026, 4, 1), faelligkeit=None, beleg_referenz="Zahlung 1.000,00",
    )
    bank_repo.upsert_bank_konto(id="BK-7DI-1", gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="7DI")
    bank_konto = bank_repo.get_bank_konto("BK-7DI-1")
    mapping = CsvSpaltenMapping(betrag="betrag", buchungsdatum="datum", referenz="referenz")

    def _importiere(referenz: str):
        csv_text = f"betrag,datum,referenz\n1000.00,2026-04-06,{referenz}\n"
        return bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=csv_text, mapping=mapping)[0]

    # Bereits VOR der Nebenläufigkeit ein bestehender 100-EUR-Link -
    # verbleibender "erklärter" Restbetrag der Zahlung: 900 EUR.
    tx_vorher = _importiere("vorher")
    bank_service.verknuepfe_mit_bestehender_zahlung(
        ctx=ctx, transaktion=tx_vorher, op_position=zahlung, konto=konto, betrag_cent=10_000, vorgang_id="VORHER",
    )

    tx_a = _importiere("thread-a")
    tx_b = _importiere("thread-b")

    barrier = threading.Barrier(2)
    ergebnisse: dict[str, tuple[str, object]] = {}

    def _verknuepfen(schluessel: str, transaktion, vorgang_id: str):
        barrier.wait(timeout=5)
        try:
            zuordnung = bank_service.verknuepfe_mit_bestehender_zahlung(
                ctx=ctx, transaktion=transaktion, op_position=zahlung, konto=konto, betrag_cent=60_000,
                vorgang_id=vorgang_id,
            )
            ergebnisse[schluessel] = ("ok", zuordnung)
        except Exception as exc:  # noqa: BLE001 - Ergebnis wird unten geprüft, nicht verschluckt
            ergebnisse[schluessel] = ("fehler", exc)

    thread_a = threading.Thread(target=_verknuepfen, args=("a", tx_a, "THREAD-A"))
    thread_b = threading.Thread(target=_verknuepfen, args=("b", tx_b, "THREAD-B"))
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=15)
    thread_b.join(timeout=15)

    assert set(ergebnisse) == {"a", "b"}  # beide Threads sind tatsächlich fertig geworden, kein Hänger
    erfolgreiche = [k for k, (status, _) in ergebnisse.items() if status == "ok"]
    fehlgeschlagene = [k for k, (status, _) in ergebnisse.items() if status == "fehler"]
    assert len(erfolgreiche) == 1, f"genau EIN gleichzeitiger 600-EUR-Versuch darf erfolgreich sein: {ergebnisse}"
    assert len(fehlgeschlagene) == 1
    _, fehler = ergebnisse[fehlgeschlagene[0]]
    assert isinstance(fehler, ZuordnungUngueltigError)

    gesamt_verknuepft = bank_repo.verknuepfter_betrag_fuer_op(zahlung.id)
    assert gesamt_verknuepft == 70_000  # 100 EUR vorher + GENAU EINER der beiden 600-EUR-Versuche, NIE 1.300 EUR


def test_importiere_atomar_datei_sqlite_gleichzeitiger_identischer_csv_import_dupliziert_nicht(tmp_path):
    """Nutzerauftrag (vor Paket C): kurze Prüfung, ob ein zweiter
    gleichzeitiger identischer CSV-Import allein durch vorhandene
    Unique Constraints vollständig abgefangen wird. Ergebnis: NUR für
    Zeilen MIT bankseitig eindeutiger `native_id` (dort schützt der
    echte DB-UNIQUE-Constraint auf `import_id`, unabhängig vom
    Locking). Für Zeilen OHNE `native_id` (reiner CSV-Fingerprint-Import,
    wie hier) hängt die Dublettenprüfung an `find_by_fingerprint` - einem
    SELECT-dann-Entscheiden OHNE DB-Backstop - und war unter Datei-SQLite
    NICHT geschützt, bevor `_importiere_atomar` auf
    `schreibgesperrte_session` umgestellt wurde. Reproduktion mit zwei
    echten Threads auf eine echte Datei-SQLite-DB (bewusst NICHT
    `:memory:`)."""

    import threading

    from mietinkasso.auth.service import AuthContext
    from mietinkasso.domain.enums import Rolle
    from mietinkasso.infrastructure.db.base import Base
    from mietinkasso.infrastructure.db.session import build_engine, build_session_factory

    db_pfad = tmp_path / "import-nebenlaeufig.db"
    engine = build_engine(f"sqlite:///{db_pfad}")
    Base.metadata.create_all(engine)
    engine.dispose()
    session_factory = build_session_factory(f"sqlite:///{db_pfad}")

    bank_repo = BankRepository(session_factory)
    from mietinkasso.stammdaten.repository import StammdatenRepository

    stammdaten_repo = StammdatenRepository(session_factory)
    op_service = OPService(OPRepository(session_factory), stammdaten_repo)
    bank_service = BankImportService(bank_repo, stammdaten_repo, op_service)
    ctx = AuthContext(user_id="test", rolle=Rolle.BUCHHALTUNG, gesellschaft_ids=frozenset({"7DI"}))

    stammdaten_repo.upsert_gesellschaft(id="7DI", name="7D Immobilien GmbH")
    bank_repo.upsert_bank_konto(id="BK-7DI-1", gesellschaft_id="7DI", iban="AT000000000000000000", bezeichnung="7DI")
    bank_konto = bank_repo.get_bank_konto("BK-7DI-1")

    # KEINE spalte_eindeutig -> hat_native_id=False, ausschließlich
    # Fingerprint-basierte Dublettenprüfung (der hier relevante Fall).
    mapping = CsvSpaltenMapping(betrag="betrag", buchungsdatum="datum", referenz="referenz")
    csv_text = "betrag,datum,referenz\n600.00,2026-04-06,identischer-import\n"

    barrier = threading.Barrier(2)
    ergebnisse: dict[str, tuple[str, object]] = {}

    def _importieren(schluessel: str):
        barrier.wait(timeout=5)
        try:
            zeilen = bank_service.importiere_csv(ctx=ctx, bank_konto=bank_konto, text=csv_text, mapping=mapping)
            ergebnisse[schluessel] = ("ok", zeilen)
        except Exception as exc:  # noqa: BLE001 - Ergebnis wird unten geprüft
            ergebnisse[schluessel] = ("fehler", exc)

    thread_a = threading.Thread(target=_importieren, args=("a",))
    thread_b = threading.Thread(target=_importieren, args=("b",))
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=15)
    thread_b.join(timeout=15)

    assert set(ergebnisse) == {"a", "b"}
    erfolgreiche = [k for k, (status, _) in ergebnisse.items() if status == "ok"]
    fehlgeschlagene = [k for k, (status, _) in ergebnisse.items() if status == "fehler"]
    assert len(erfolgreiche) == 1, f"genau EIN gleichzeitiger identischer Import darf durchgehen: {ergebnisse}"
    assert len(fehlgeschlagene) == 1
    _, fehler = ergebnisse[fehlgeschlagene[0]]
    assert isinstance(fehler, MehrfachbuchungsKonfliktError)

    gesamt = bank_repo.list_unzugeordnet("BK-7DI-1")
    assert len(gesamt) == 1  # NIE zwei Zeilen für dieselbe wirtschaftliche Zahlung
