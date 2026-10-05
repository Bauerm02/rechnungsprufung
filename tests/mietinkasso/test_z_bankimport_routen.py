"""Routentests Bankdatei-Vorschau/-Import mit optionaler eigener
CSV-Kontospalte (siehe docs/hausverwaltung/BANKIMPORT_SICHERHEIT.md).
Wie `test_z_abgleichstatus.py`: Backoffice-Module werden erst IN den
Tests importiert und die Abhängigkeiten per monkeypatch auf eine
isolierte In-Memory-DB umgebogen. Ausschließlich synthetische Daten."""

from __future__ import annotations

import base64
import re

import pytest
from sqlalchemy import select

from mietinkasso.bank.repository import BankRepository
from mietinkasso.bank.service import BankImportService
from mietinkasso.infrastructure.db.tables import BankTransaktionTable
from mietinkasso.op.repository import OPRepository
from mietinkasso.op.service import OPService

IBAN = "AT000000000000000000"
IBAN_FREMD = "AT000000000000000099"


@pytest.fixture
def client_und_csrf(session_factory, stammdaten_repo, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from mietinkasso.audit.service import AuditService
    from mietinkasso.backoffice import dependencies as deps
    from mietinkasso.backoffice.routes import bank as ui
    from mietinkasso.backoffice.security import SessionStore

    stammdaten_repo.upsert_gesellschaft(id="7DI", name="Synthetik 7DI")
    bank_repo = BankRepository(session_factory)
    bank_repo.upsert_bank_konto(id="BK-SYN-R", gesellschaft_id="7DI", iban=IBAN, bezeichnung="Synthetik Route")
    bank_service = BankImportService(bank_repo, stammdaten_repo, OPService(OPRepository(session_factory), stammdaten_repo))
    for name, wert in (
        ("_stammdaten_repo", stammdaten_repo), ("_bank_repo", bank_repo), ("_bank_service", bank_service),
        ("_audit_service", AuditService(session_factory)),
        ("_settings", deps._settings.model_copy(update={"backoffice_password_hash": "configured"})),
        ("_sessions", SessionStore(ttl_sekunden=1000)),
    ):
        monkeypatch.setattr(deps, name, wert)
    app = FastAPI()
    app.include_router(ui.router, prefix="/backoffice")
    client = TestClient(app)
    sid, csrf = deps._sessions.erstellen("synthetik-operator")
    client.cookies.set(deps._COOKIE_NAME, sid)
    return client, csrf


def _form(csrf: str, **extra) -> dict:
    return {
        "csrf_token": csrf, "bank_konto_id": "BK-SYN-R", "format": "CSV",
        "spalte_betrag": "betrag", "spalte_datum": "datum", "spalte_referenz": "referenz",
        "spalte_eindeutig": "bank_id", "dezimaltrennzeichen": ".", **extra,
    }


def _csv(*konten: str, kopf_konto: str = "konto") -> bytes:
    return ("betrag,datum,referenz,bank_id," + kopf_konto + "\n" + "".join(
        f"10.00,2026-04-06,Miete,N-{i},{konto}\n" for i, konto in enumerate(konten)
    )).encode("utf-8")


def _hidden(text: str) -> dict:
    return dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', text))


def _anzahl(session_factory) -> int:
    with session_factory() as session:
        return len(session.execute(select(BankTransaktionTable)).scalars().all())


def test_formular_bietet_optionale_eigene_kontospalte(client_und_csrf):
    client, _ = client_und_csrf
    seite = client.get("/backoffice/bank")
    assert seite.status_code == 200
    assert 'name="spalte_eigene_iban"' in seite.text


def test_eigene_kontospalte_ueberlebt_vorschau_und_import(client_und_csrf, session_factory):
    client, csrf = client_und_csrf
    vorschau = client.post(
        "/backoffice/bank/vorschau", data=_form(csrf, spalte_eigene_iban="konto"),
        files={"datei": ("bank.csv", _csv(IBAN, "at00 0000 0000 0000 0000"), "text/csv")},
    )
    assert vorschau.status_code == 200
    assert "Geprüft: jede CSV-Zeile" in vorschau.text and "kein bankseitiger Herkunftsnachweis" in vorschau.text
    versteckt = _hidden(vorschau.text)
    assert versteckt["spalte_eigene_iban"] == "konto"

    importiert = client.post("/backoffice/bank/importieren", data=versteckt)
    assert importiert.status_code == 200
    assert _anzahl(session_factory) == 2


def test_legacy_vorschau_nennt_fehlenden_kontonachweis(client_und_csrf, session_factory):
    client, csrf = client_und_csrf
    vorschau = client.post(
        "/backoffice/bank/vorschau", data=_form(csrf),
        files={"datei": ("bank.csv", _csv(IBAN_FREMD), "text/csv")},  # Spalte ungeprüft, da nicht gemappt
    )
    assert vorschau.status_code == 200
    assert "Kein Kontonachweis aus der Datei" in vorschau.text
    versteckt = _hidden(vorschau.text)
    assert versteckt["spalte_eigene_iban"] == ""
    assert client.post("/backoffice/bank/importieren", data=versteckt).status_code == 200
    assert _anzahl(session_factory) == 1


@pytest.mark.parametrize(
    "konten,kopf",
    [((IBAN, IBAN_FREMD), "konto"), ((IBAN, ""), "konto"), ((IBAN,), "anderes_konto")],
    ids=["gemischt", "leer", "kopf-fehlt"],
)
def test_vorschau_lehnt_abweichende_kontospalte_mit_fehlerseite_ab(client_und_csrf, session_factory, konten, kopf):
    client, csrf = client_und_csrf
    vorschau = client.post(
        "/backoffice/bank/vorschau", data=_form(csrf, spalte_eigene_iban="konto"),
        files={"datei": ("bank.csv", _csv(*konten, kopf_konto=kopf), "text/csv")},
    )
    assert vorschau.status_code == 400
    assert "Datei nicht importierbar" in vorschau.text
    assert 'name="inhalt_b64"' not in vorschau.text
    assert _anzahl(session_factory) == 0


def test_import_prueft_kontospalte_erneut_auch_ohne_vorschau(client_und_csrf, session_factory):
    """Der Import vertraut der Vorschau nicht: ein direkt (oder mit
    manipulierten Hidden-Feldern) abgeschickter Import wird selbst geprüft."""

    client, csrf = client_und_csrf
    inhalt_b64 = base64.b64encode(_csv(IBAN, IBAN_FREMD)).decode("ascii")
    antwort = client.post("/backoffice/bank/importieren", data=_form(csrf, spalte_eigene_iban="konto", inhalt_b64=inhalt_b64))
    assert antwort.status_code == 400
    assert "NICHTS wurde übernommen" in antwort.text and IBAN_FREMD in antwort.text
    assert _anzahl(session_factory) == 0

    fehlende_spalte = client.post(
        "/backoffice/bank/importieren", data=_form(csrf, spalte_eigene_iban="gibt_es_nicht", inhalt_b64=inhalt_b64),
    )
    assert fehlende_spalte.status_code == 400
    assert _anzahl(session_factory) == 0


def test_spaltenname_wird_in_vorschau_escaped(client_und_csrf):
    client, csrf = client_und_csrf
    spalte = '<b id="x">'
    vorschau = client.post(
        "/backoffice/bank/vorschau", data=_form(csrf, spalte_eigene_iban=spalte),
        files={"datei": ("bank.csv", _csv(IBAN, kopf_konto='"<b id=""x"">"'), "text/csv")},
    )
    assert vorschau.status_code == 200
    assert spalte not in vorschau.text
    assert "&lt;b id=&quot;x&quot;&gt;" in vorschau.text


def test_camt_vorschau_nennt_geprueften_kontonachweis(client_und_csrf):
    client, csrf = client_und_csrf
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"><BkToCstmrStmt><Stmt>
  <Acct><Id><IBAN>{IBAN}</IBAN></Id></Acct>
  <Ntry><Amt Ccy="EUR">10.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><BookgDt><Dt>2026-04-06</Dt></BookgDt>
    <NtryDtls><TxDtls><AcctSvcrRef>N-1</AcctSvcrRef></TxDtls></NtryDtls></Ntry>
</Stmt></BkToCstmrStmt></Document>""".encode("utf-8")
    vorschau = client.post(
        "/backoffice/bank/vorschau", data=_form(csrf, format="CAMT"), files={"datei": ("bank.xml", xml, "text/xml")},
    )
    assert vorschau.status_code == 200
    assert "Geprüft: jedes Statement der CAMT.053-Datei" in vorschau.text
