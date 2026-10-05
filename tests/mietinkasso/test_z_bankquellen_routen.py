"""Routentests Bankquellen je Objekt + gebundener Bankdatei-Import (siehe
docs/hausverwaltung/BANKQUELLENBINDUNG.md). Wie `test_z_bankimport_routen.py`:
Backoffice-Module erst IN den Tests importieren, Abhängigkeiten per
monkeypatch auf eine isolierte In-Memory-DB. Ausschließlich synthetische
Daten."""

from __future__ import annotations

import re

import pytest
from sqlalchemy import func, select

from mietinkasso.bank.quellen_models import BankQuellenBindungTable
from mietinkasso.bank.repository import BankRepository
from mietinkasso.bank.service import BankImportService
from mietinkasso.infrastructure.db.tables import BankTransaktionTable
from mietinkasso.op.repository import OPRepository
from mietinkasso.op.service import OPService

IBAN_A = "AT000000000000000001"
IBAN_B = "AT000000000000000002"


@pytest.fixture
def umgebung(session_factory, stammdaten_repo, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from mietinkasso.audit.service import AuditService
    from mietinkasso.backoffice import dependencies as deps
    from mietinkasso.backoffice.routes import bank as bank_ui
    from mietinkasso.backoffice.routes import bankquellen as quellen_ui
    from mietinkasso.backoffice.security import SessionStore

    stammdaten_repo.upsert_gesellschaft(id="7DI", name="Synthetik 7DI")
    stammdaten_repo.upsert_objekt(id="601", gesellschaft_id="7DI", bezeichnung="Synthetik 601")
    stammdaten_repo.upsert_objekt(id="107", gesellschaft_id="7DI", bezeichnung="Synthetik 107", ausgeschlossen=True)
    bank_repo = BankRepository(session_factory)
    bank_repo.upsert_bank_konto(id="BK-MIETE", gesellschaft_id="7DI", iban=IBAN_A, bezeichnung="Synthetik Miete")
    bank_repo.upsert_bank_konto(id="BK-ANDERE", gesellschaft_id="7DI", iban=IBAN_B, bezeichnung="Synthetik andere")
    bank_service = BankImportService(bank_repo, stammdaten_repo, OPService(OPRepository(session_factory), stammdaten_repo))
    for name, wert in (
        ("_stammdaten_repo", stammdaten_repo), ("_bank_repo", bank_repo), ("_bank_service", bank_service),
        ("_audit_service", AuditService(session_factory)),
        ("_settings", deps._settings.model_copy(update={"backoffice_password_hash": "configured"})),
        ("_sessions", SessionStore(ttl_sekunden=1000)),
    ):
        monkeypatch.setattr(deps, name, wert)
    app = FastAPI()
    app.include_router(bank_ui.router, prefix="/backoffice")
    app.include_router(quellen_ui.router, prefix="/backoffice")
    client = TestClient(app)
    sid, csrf = deps._sessions.erstellen("synthetik-operator")
    client.cookies.set(deps._COOKIE_NAME, sid)
    return client, csrf, bank_service


def _anzahl(session_factory, tabelle) -> int:
    with session_factory() as session:
        return session.execute(select(func.count()).select_from(tabelle)).scalar_one()


def _stand_auf_seite(text: str, objekt_id: str = "601") -> str:
    treffer = re.search(
        rf'name="objekt_id" value="{objekt_id}">\s*<input type="hidden" name="gesellschaft_id" value="7DI">\s*'
        r'<input type="hidden" name="erwarteter_stand" value="([^"]+)"', text,
    )
    assert treffer, "Bindungsformular fehlt"
    return treffer.group(1)


def _bindung(csrf: str, stand: str, **anders) -> dict:
    return {
        "csrf_token": csrf, "objekt_id": "601", "gesellschaft_id": "7DI", "erwarteter_stand": stand,
        "anbieter": "SYN-ANBIETER", "zugang_ref": "ZUGANG-1", "konto_ref": "KONTO-1", "bank_konto_id": "BK-MIETE",
        "iban": IBAN_A, "kontorolle": "MIETE", "nachweis_ref": "SYN-BELEG-1", **anders,
    }


def _hidden(text: str) -> dict:
    return dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', text))


def _csv(iban: str = IBAN_A) -> bytes:
    return f"betrag,datum,referenz,bank_id,konto\n10.00,2026-04-06,Miete,N-1,{iban}\n".encode("utf-8")


def _vorschau_form(csrf: str, **anders) -> dict:
    return {
        "csrf_token": csrf, "objekt_id": "601", "bank_konto_id": "", "format": "CSV", "spalte_betrag": "betrag",
        "spalte_datum": "datum", "spalte_referenz": "referenz", "spalte_eindeutig": "bank_id",
        "spalte_eigene_iban": "konto", "dezimaltrennzeichen": ".", **anders,
    }


def _konfigurieren(client, csrf) -> None:
    stand = _stand_auf_seite(client.get("/backoffice/bank/quellen").text)
    antwort = client.post("/backoffice/bank/quellen/binden", data=_bindung(csrf, stand), follow_redirects=False)
    assert antwort.status_code == 303


def test_ohne_sitzung_oder_csrf_nichts(umgebung, session_factory):
    client, csrf, _ = umgebung
    client.cookies.clear()
    assert client.get("/backoffice/bank/quellen", follow_redirects=False).status_code == 303
    assert client.post(
        "/backoffice/bank/quellen/binden", data=_bindung(csrf, "NEU"), follow_redirects=False,
    ).status_code == 303
    assert _anzahl(session_factory, BankQuellenBindungTable) == 0


def test_falsches_csrf_abgelehnt(umgebung, session_factory):
    client, _, _ = umgebung
    assert client.post("/backoffice/bank/quellen/binden", data=_bindung("falsch", "NEU")).status_code == 403
    assert client.post(
        "/backoffice/bank/quellen/widerrufen",
        data={"csrf_token": "falsch", "objekt_id": "601", "erwarteter_stand": "NEU", "nachweis_ref": "x"},
    ).status_code == 403
    assert _anzahl(session_factory, BankQuellenBindungTable) == 0


def test_uebersicht_ohne_vorbelegung_und_ohne_gruene_anzeige(umgebung):
    client, _, _ = umgebung
    seite = client.get("/backoffice/bank/quellen")
    assert seite.status_code == 200
    assert "nicht konfiguriert" in seite.text
    assert 'class="badge badge-ok"' not in seite.text and "aktiv gebunden" not in seite.text
    assert "Synthetik 107" not in seite.text  # ausgeschlossen
    bank_auswahl = re.findall(r'<select name="bank_konto_id"[^>]*>(.*?)</select>', seite.text, re.S)
    assert bank_auswahl and all("selected" not in auswahl for auswahl in bank_auswahl)
    assert IBAN_A not in seite.text
    assert 'href="/backoffice/bank/quellen"' in client.get("/backoffice/bank").text


def test_konfiguration_braucht_nachweis_und_aktuellen_stand(umgebung, session_factory):
    client, csrf, _ = umgebung
    stand = _stand_auf_seite(client.get("/backoffice/bank/quellen").text)
    ohne_nachweis = client.post("/backoffice/bank/quellen/binden", data=_bindung(csrf, stand, nachweis_ref=" "))
    assert ohne_nachweis.status_code == 400 and "Nicht gespeichert" in ohne_nachweis.text
    assert client.post("/backoffice/bank/quellen/binden", data=_bindung(csrf, stand, kontorolle="KREDIT")).status_code == 400
    assert _anzahl(session_factory, BankQuellenBindungTable) == 0

    _konfigurieren(client, csrf)
    seite = client.get("/backoffice/bank/quellen").text
    assert "aktiv gebunden" in seite and "AT00 **** 0001" in seite and IBAN_A not in seite
    assert "SYN-ANBIETER" in seite and "Rolle MIETE" in seite
    veraltet = client.post(
        "/backoffice/bank/quellen/binden", data=_bindung(csrf, stand, konto_ref="KONTO-2", nachweis_ref="SYN-2"),
    )
    assert veraltet.status_code == 400 and "geändert" in veraltet.text
    assert _anzahl(session_factory, BankQuellenBindungTable) == 1


def test_konfiguration_dann_gebundene_vorschau_und_import(umgebung, session_factory):
    client, csrf, _ = umgebung
    _konfigurieren(client, csrf)
    formular = client.get("/backoffice/bank").text
    assert 'name="objekt_id"' in formular and "Objekt 601" in formular

    vorschau = client.post(
        "/backoffice/bank/vorschau", data=_vorschau_form(csrf), files={"datei": ("bank.csv", _csv(), "text/csv")},
    )
    assert vorschau.status_code == 200
    assert "Revision 1" in vorschau.text and "NICHT anbieterseitig authentifiziert" in vorschau.text
    versteckt = _hidden(vorschau.text)
    assert (versteckt["bank_konto_id"], versteckt["objekt_id"]) == ("BK-MIETE", "601")
    assert versteckt["bindung_token"].startswith("R1-") and versteckt["quelle_id"]

    assert client.post("/backoffice/bank/importieren", data=versteckt).status_code == 200
    assert _anzahl(session_factory, BankTransaktionTable) == 1


@pytest.mark.parametrize(
    "manipulation",
    [{"bank_konto_id": "BK-ANDERE"}, {"bindung_token": "R1-" + "0" * 64}, {"quelle_id": "999"}],
    ids=["bank", "token", "quelle"],
)
def test_manipulierte_hidden_felder_abgelehnt(umgebung, session_factory, manipulation):
    client, csrf, _ = umgebung
    _konfigurieren(client, csrf)
    vorschau = client.post(
        "/backoffice/bank/vorschau", data=_vorschau_form(csrf), files={"datei": ("bank.csv", _csv(), "text/csv")},
    )
    antwort = client.post("/backoffice/bank/importieren", data={**_hidden(vorschau.text), **manipulation})
    assert antwort.status_code == 400 and "NICHTS wurde übernommen" in antwort.text
    assert _anzahl(session_factory, BankTransaktionTable) == 0


def test_veralteter_vorschau_stand_nach_widerruf_abgelehnt(umgebung, session_factory):
    client, csrf, _ = umgebung
    _konfigurieren(client, csrf)
    vorschau = client.post(
        "/backoffice/bank/vorschau", data=_vorschau_form(csrf), files={"datei": ("bank.csv", _csv(), "text/csv")},
    )
    stand = _stand_auf_seite(client.get("/backoffice/bank/quellen").text)
    widerruf = client.post(
        "/backoffice/bank/quellen/widerrufen",
        data={"csrf_token": csrf, "objekt_id": "601", "erwarteter_stand": stand, "nachweis_ref": "SYN-FALSCH"},
        follow_redirects=False,
    )
    assert widerruf.status_code == 303
    assert "widerrufen &ndash; gesperrt" in client.get("/backoffice/bank/quellen").text
    assert "Objekt 601" not in client.get("/backoffice/bank").text

    assert client.post("/backoffice/bank/importieren", data=_hidden(vorschau.text)).status_code == 400
    # Auch der ungebundene Weg auf dasselbe Bankkonto bleibt gesperrt.
    legacy = client.post(
        "/backoffice/bank/vorschau", data=_vorschau_form(csrf, objekt_id="", bank_konto_id="BK-MIETE"),
        files={"datei": ("bank.csv", _csv(), "text/csv")},
    )
    assert legacy.status_code == 400
    assert _anzahl(session_factory, BankTransaktionTable) == 0


def test_gebundene_csv_vorschau_verlangt_eigene_iban_und_passendes_konto(umgebung, session_factory):
    client, csrf, _ = umgebung
    _konfigurieren(client, csrf)
    ohne_spalte = client.post(
        "/backoffice/bank/vorschau", data=_vorschau_form(csrf, spalte_eigene_iban=""),
        files={"datei": ("bank.csv", _csv(), "text/csv")},
    )
    assert ohne_spalte.status_code == 400 and 'name="inhalt_b64"' not in ohne_spalte.text
    falsches_konto = client.post(
        "/backoffice/bank/vorschau", data=_vorschau_form(csrf, bank_konto_id="BK-ANDERE"),
        files={"datei": ("bank.csv", _csv(), "text/csv")},
    )
    assert falsches_konto.status_code == 400
    fremde_iban = client.post(
        "/backoffice/bank/vorschau", data=_vorschau_form(csrf), files={"datei": ("bank.csv", _csv(IBAN_B), "text/csv")},
    )
    assert fremde_iban.status_code == 400
    assert _anzahl(session_factory, BankTransaktionTable) == 0


def test_nie_konfiguriertes_bankkonto_bleibt_legacy(umgebung, session_factory):
    client, csrf, _ = umgebung
    _konfigurieren(client, csrf)
    vorschau = client.post(
        "/backoffice/bank/vorschau", data=_vorschau_form(csrf, objekt_id="", bank_konto_id="BK-ANDERE", spalte_eigene_iban=""),
        files={"datei": ("bank.csv", _csv(IBAN_B), "text/csv")},
    )
    assert vorschau.status_code == 200 and "nie konfiguriertes Bankkonto" in vorschau.text
    assert client.post("/backoffice/bank/importieren", data=_hidden(vorschau.text)).status_code == 200
    assert _anzahl(session_factory, BankTransaktionTable) == 1
