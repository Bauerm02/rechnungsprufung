"""Integrationstests für das bedienbare Backoffice (FastAPI TestClient
gegen eine ECHTE, temporäre Datei-SQLite-DB - bewusst keine `:memory:`-DB,
weil der Pilot mit einer Datei-DB betrieben wird und `:memory:` mit
mehreren Sessions/StaticPool ein eigenes, hier nicht relevantes
Nebenläufigkeitsverhalten hat).

Diese Tests importieren `mietinkasso.api.app` (und damit
`mietinkasso.backoffice.app`) zum ERSTEN Mal in diesem Prozess, nachdem
die nötigen Umgebungsvariablen gesetzt und `get_settings()` geleert
wurde - beide Module bauen ihre Repositories/Services beim Import einmalig
auf (wie das bestehende `api/app.py` es bereits tut). Deshalb darf kein
anderes Testmodul diese Module vor diesem hier importieren (siehe
Kommentar unten) und dieses Modul MUSS als einziges pro Prozess laufen -
für die Testsuite unproblematisch, da hier nichts anderes `api.app`
importiert."""

from __future__ import annotations

import os
import re
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def backoffice_client():
    from mietinkasso.infrastructure.config import get_settings

    tmp_dir = tempfile.mkdtemp(prefix="mietinkasso-backoffice-test-")
    db_path = Path(tmp_dir) / "demo.db"
    os.environ["MIETINKASSO_DATABASE_URL"] = f"sqlite:///{db_path}"

    from mietinkasso.backoffice.security import hash_passwort

    os.environ["MIETINKASSO_BACKOFFICE_USER"] = "markus"
    os.environ["MIETINKASSO_BACKOFFICE_PASSWORD_HASH"] = hash_passwort("test-passwort-123")
    get_settings.cache_clear()

    from mietinkasso.infrastructure.db.session import build_session_factory, create_all_tables

    settings = get_settings()
    create_all_tables(settings)
    session_factory = build_session_factory(settings.database_url)

    from mietinkasso.op.repository import OPRepository
    from mietinkasso.op.service import OPService
    from mietinkasso.stammdaten.repository import StammdatenRepository

    stammdaten = StammdatenRepository(session_factory)
    op_service = OPService(OPRepository(session_factory), stammdaten)

    stammdaten.upsert_gesellschaft(id="7DI", name="7D Immobilien GmbH (Test)")
    stammdaten.upsert_objekt(id="601", gesellschaft_id="7DI", bezeichnung="Am Corso (Test)")
    stammdaten.upsert_objekt(id="107", gesellschaft_id="7DI", bezeichnung="Sieben Dörfer (Test)", ausgeschlossen=True)
    stammdaten.upsert_einheit(id="601-TOP1", objekt_id="601", bezeichnung="Top 1", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten.upsert_einheit(id="107-TOP1", objekt_id="107", bezeichnung="Top 1", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten.upsert_debitor(id="DEB-1", name="Test Mieterin", email="test@example.at")
    stammdaten.upsert_vertrag(
        id="V-601-1", einheit_id="601-TOP1", debitor_id="DEB-1", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    stammdaten.upsert_vertrag(
        id="V-107-1", einheit_id="107-TOP1", debitor_id="DEB-1", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    stammdaten.add_komponente(
        id="K-601-1-HMZ", vertrag_id="V-601-1", art="HMZ", bezeichnung="Hauptmietzins",
        betrag_cent=55_000, ust_satz_promille=10_000, gueltig_von=date(2024, 1, 1),
    )
    vertrag = stammdaten.get_vertrag("V-601-1")
    konto = stammdaten.get_or_create_konto(vertrag=vertrag)
    vertrag_gesperrt = stammdaten.get_vertrag("V-107-1")
    konto_gesperrt = stammdaten.get_or_create_konto(vertrag=vertrag_gesperrt)

    from fastapi.testclient import TestClient

    from mietinkasso.api.app import app

    with TestClient(app) as client:
        yield client, konto.id, konto_gesperrt.id, op_service

    get_settings.cache_clear()


def _login(client) -> None:
    antwort = client.post(
        "/backoffice/login", data={"username": "markus", "password": "test-passwort-123"}, follow_redirects=False,
    )
    assert antwort.status_code == 303
    assert antwort.headers["location"] == "/backoffice/"


def _csrf_token(client) -> str:
    seite = client.get("/backoffice/")
    assert seite.status_code == 200
    marker = 'name="csrf_token" value="'
    start = seite.text.index(marker) + len(marker)
    ende = seite.text.index('"', start)
    return seite.text[start:ende]


def test_ohne_login_wird_auf_login_umgeleitet(backoffice_client):
    client, konto_id, _konto_gesperrt_id, _op_service = backoffice_client
    antwort = client.get("/backoffice/", follow_redirects=False)
    assert antwort.status_code == 303
    assert antwort.headers["location"] == "/backoffice/login"


def test_login_mit_falschem_passwort_scheitert(backoffice_client):
    client, *_ = backoffice_client
    antwort = client.post(
        "/backoffice/login", data={"username": "markus", "password": "falsch"}, follow_redirects=False,
    )
    assert antwort.status_code == 303
    assert antwort.headers["location"].startswith("/backoffice/login")
    dashboard = client.get("/backoffice/", follow_redirects=False)
    assert dashboard.status_code == 303  # weiterhin nicht angemeldet


def test_login_und_dashboard_zeigt_objekte(backoffice_client):
    client, konto_id, _konto_gesperrt_id, _op_service = backoffice_client
    _login(client)
    dashboard = client.get("/backoffice/", params={"objekt_id": "601"})
    assert dashboard.status_code == 200
    assert "Am Corso" in dashboard.text
    assert konto_id in dashboard.text


def test_hauptnavigation_verlinkt_alle_kontextlosen_arbeitsablaeufe(backoffice_client):
    """Regression (Bedienungsfehler-Meldung): nach der Anmeldung zeigte
    das Dashboard nur die Objektwahl - Eröffnungsimport, Bankimport,
    offene Zuordnungen und Bankvollständigkeit hatten keinen sichtbaren
    Weg dorthin. Diese vier (kontextlosen, d. h. ohne Vertrag/Konto in
    der URL) Arbeitsabläufe müssen im gemeinsamen Layout für jeden
    angemeldeten Benutzer gut lesbar verlinkt sein, auf JEDER Seite -
    nicht nur auf dem Dashboard. Vor der Anmeldung darf die Navigation
    nicht erscheinen (keine funktionslosen Links auf der Login-Seite)."""

    client, konto_id, _konto_gesperrt_id, _op_service = backoffice_client

    login_seite = client.get("/backoffice/login")
    assert login_seite.status_code == 200
    assert "/backoffice/eroeffnung" not in login_seite.text

    _login(client)
    erwartete_links = [
        "/backoffice/eroeffnung",
        "/backoffice/bank",
        "/backoffice/bank/unzugeordnet",
        "/backoffice/bank/vollstaendigkeit",
    ]

    dashboard = client.get("/backoffice/")
    assert dashboard.status_code == 200
    assert "Hausverwaltung &amp; Mietinkasso" in dashboard.text or "Hausverwaltung & Mietinkasso" in dashboard.text
    for link in erwartete_links:
        assert f'href="{link}"' in dashboard.text, f"Navigationslink {link} fehlt auf dem Dashboard"

    # Die Navigation ist Teil des GEMEINSAMEN Layouts, nicht nur einer
    # Seite - auf einer beliebigen anderen Seite (Kontoauszug) ebenfalls
    # sichtbar.
    kontoauszug = client.get(f"/backoffice/konto/{konto_id}")
    assert kontoauszug.status_code == 200
    for link in erwartete_links:
        assert f'href="{link}"' in kontoauszug.text, f"Navigationslink {link} fehlt im Kontoauszug"


def test_objekt_107_ist_im_dashboard_nur_lesend(backoffice_client):
    client, _konto_id, konto_gesperrt_id, _op_service = backoffice_client
    _login(client)
    dashboard = client.get("/backoffice/", params={"objekt_id": "107"})
    assert dashboard.status_code == 200
    assert "ausgeschlossen" in dashboard.text
    # Kein Nachbuchungs-Link für ein gesperrtes Objekt im Kontoauszug.
    kontoauszug = client.get(f"/backoffice/konto/{konto_gesperrt_id}")
    assert "Objekt ist von der Pilotphase ausgeschlossen" in kontoauszug.text
    assert f"/backoffice/konto/{konto_gesperrt_id}/buchen" not in kontoauszug.text


def test_nachbuchung_ohne_csrf_wird_abgelehnt(backoffice_client):
    client, konto_id, _konto_gesperrt_id, op_service = backoffice_client
    _login(client)
    saldo_vorher = op_service.berechne_saldo(konto_id).saldo_cent
    antwort = client.post(
        f"/backoffice/konto/{konto_id}/buchen",
        data={
            "csrf_token": "voellig-falsch", "typ": "SOLL", "betrag": "100,00",
            "belegdatum": date.today().isoformat(), "beleg_referenz": "Test", "grund": "Test",
            "vorgangs_id": "CSRF-TEST-1",
        },
    )
    assert antwort.status_code == 403
    assert op_service.berechne_saldo(konto_id).saldo_cent == saldo_vorher


def test_nachbuchung_und_korrektur_end_to_end(backoffice_client):
    client, konto_id, _konto_gesperrt_id, op_service = backoffice_client
    _login(client)
    csrf = _csrf_token(client)

    antwort = client.post(
        f"/backoffice/konto/{konto_id}/buchen",
        data={
            "csrf_token": csrf, "typ": "SOLL", "betrag": "500,00", "belegdatum": "2026-06-01",
            "beleg_referenz": "Miete Juni", "grund": "Nachbuchung Test", "vorgangs_id": "NACHBUCHUNG-1",
        },
        follow_redirects=False,
    )
    assert antwort.status_code == 303
    assert op_service.berechne_saldo(konto_id).saldo_cent == 50_000

    positionen = op_service.list_alle_positionen(konto_id)
    op_id = next(p.id for p in positionen if p.beleg_referenz == "Miete Juni")

    # Doppelte Formularbestätigung derselben Korrektur (gleiche Vorgangs-ID)
    # muss ein sicherer No-Op bleiben, nicht den Saldo verdoppeln.
    korrektur_daten = {
        "csrf_token": csrf, "grund": "Tippfehler", "neuer_betrag": "600,00",
        "vorgangs_id": "KORREKTUR-DOPPELT-1",
    }
    erster = client.post(f"/backoffice/op/{op_id}/korrigieren", data=korrektur_daten, follow_redirects=False)
    assert erster.status_code == 303
    assert op_service.berechne_saldo(konto_id).saldo_cent == 60_000

    zweiter = client.post(f"/backoffice/op/{op_id}/korrigieren", data=korrektur_daten, follow_redirects=False)
    assert zweiter.status_code == 303  # sicherer No-Op, kein Fehler
    assert op_service.berechne_saldo(konto_id).saldo_cent == 60_000  # NICHT verdoppelt auf 120000


def test_eroeffnung_vorschau_und_atomarer_import(backoffice_client, tmp_path):
    client, _konto_id, _konto_gesperrt_id, op_service = backoffice_client
    _login(client)
    csrf = _csrf_token(client)

    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.stammdaten.repository import StammdatenRepository

    stammdaten = StammdatenRepository(build_session_factory(get_settings().database_url))
    stammdaten.upsert_objekt(id="601", gesellschaft_id="7DI", bezeichnung="Am Corso (Test)")
    stammdaten.upsert_einheit(id="601-TOP-ERO", objekt_id="601", bezeichnung="Top ERO", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten.upsert_vertrag(
        id="V-601-ERO", einheit_id="601-TOP-ERO", debitor_id="DEB-1", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    vertrag = stammdaten.get_vertrag("V-601-ERO")
    konto = stammdaten.get_or_create_konto(vertrag=vertrag)

    csv_text = (
        "konto_id,modus,betrag,stichtag,import_id,beleg_referenz\n"
        f"{konto.id},GESAMTSALDO,321.00,2026-01-01,BACKOFFICE-ERO-1,Testeröffnung\n"
        "UNBEKANNT,GESAMTSALDO,50.00,2026-01-01,BACKOFFICE-ERO-2,unbekannt\n"
    )
    vorschau = client.post(
        "/backoffice/eroeffnung/vorschau", data={"csrf_token": csrf},
        files={"datei": ("eroeffnung.csv", csv_text.encode("utf-8"), "text/csv")},
    )
    assert vorschau.status_code == 200
    assert "UNBEKANNTES KONTO" in vorschau.text
    # Blockierende Datei: kein "Jetzt atomar verbuchen"-Formular vorhanden.
    assert "Jetzt atomar verbuchen" not in vorschau.text

    # Direkter Versuch, TROTZDEM zu verbuchen (z. B. manipuliertes Formular):
    # serverseitige Re-Validierung muss erneut blockieren, nichts buchen.
    verbucht_versuch = client.post(
        "/backoffice/eroeffnung/verbuchen", data={"csrf_token": csrf, "datei_inhalt": csv_text},
    )
    assert verbucht_versuch.status_code == 400
    assert op_service.berechne_saldo(konto.id).saldo_cent == 0

    # Korrigierte Datei (nur die gültige Zeile) geht atomar durch.
    csv_text_korrigiert = (
        "konto_id,modus,betrag,stichtag,import_id,beleg_referenz\n"
        f"{konto.id},GESAMTSALDO,321.00,2026-01-01,BACKOFFICE-ERO-1,Testeröffnung\n"
    )
    vorschau_ok = client.post(
        "/backoffice/eroeffnung/vorschau", data={"csrf_token": csrf},
        files={"datei": ("eroeffnung.csv", csv_text_korrigiert.encode("utf-8"), "text/csv")},
    )
    assert "Jetzt atomar verbuchen" in vorschau_ok.text
    verbucht = client.post(
        "/backoffice/eroeffnung/verbuchen", data={"csrf_token": csrf, "datei_inhalt": csv_text_korrigiert},
    )
    assert verbucht.status_code == 200
    assert op_service.berechne_saldo(konto.id).saldo_cent == 32_100


def test_bankimport_vorschau_import_und_zuordnung(backoffice_client):
    client, konto_id, _konto_gesperrt_id, op_service = backoffice_client
    _login(client)
    csrf = _csrf_token(client)

    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.bank.repository import BankRepository

    bank_repo = BankRepository(build_session_factory(get_settings().database_url))
    bank_repo.upsert_bank_konto(id="BK-TEST-1", gesellschaft_id="7DI", iban="AT000000000000000001", bezeichnung="Test-Bankkonto")

    csv_text = "betrag,datum,referenz\n600.00,2026-04-06,VERTRAG:V-601-1\n"
    vorschau = client.post(
        "/backoffice/bank/vorschau",
        data={
            "csrf_token": csrf, "bank_konto_id": "BK-TEST-1", "format": "CSV",
            "spalte_betrag": "betrag", "spalte_datum": "datum", "spalte_referenz": "referenz",
            "spalte_eindeutig": "", "dezimaltrennzeichen": ".",
        },
        files={"datei": ("bank.csv", csv_text.encode("utf-8"), "text/csv")},
    )
    assert vorschau.status_code == 200
    assert "V-601-1" not in vorschau.text or konto_id in vorschau.text  # Vorschlag zeigt das Zielkonto

    marker = 'name="inhalt_b64" value="'
    start = vorschau.text.index(marker) + len(marker)
    ende = vorschau.text.index('"', start)
    inhalt_b64 = vorschau.text[start:ende]

    saldo_vorher = op_service.berechne_saldo(konto_id).saldo_cent
    importiert = client.post(
        "/backoffice/bank/importieren",
        data={
            "csrf_token": csrf, "bank_konto_id": "BK-TEST-1", "format": "CSV", "inhalt_b64": inhalt_b64,
            "spalte_betrag": "betrag", "spalte_datum": "datum", "spalte_referenz": "referenz",
            "spalte_eindeutig": "", "dezimaltrennzeichen": ".",
        },
    )
    assert importiert.status_code == 200
    assert op_service.berechne_saldo(konto_id).saldo_cent == saldo_vorher  # Import allein bucht nichts

    offene = client.get("/backoffice/bank/unzugeordnet", params={"bank_konto_id": "BK-TEST-1"})
    assert offene.status_code == 200
    treffer = re.search(r"/backoffice/bank/(\d+)/automatisch-zuordnen", offene.text)
    assert treffer is not None
    transaktion_id = treffer.group(1)

    zugeordnet = client.post(
        f"/backoffice/bank/{transaktion_id}/automatisch-zuordnen", data={"csrf_token": csrf}, follow_redirects=False,
    )
    assert zugeordnet.status_code == 303
    assert op_service.berechne_saldo(konto_id).saldo_cent == saldo_vorher - 60_000


def test_vorschreibung_vorschau_und_freigabe(backoffice_client):
    client, konto_id, _konto_gesperrt_id, op_service = backoffice_client
    _login(client)
    csrf = _csrf_token(client)

    vorschau = client.get("/backoffice/vertrag/V-601-1/vorschreibung", params={"monat": "2026-07"})
    assert vorschau.status_code == 200
    assert "Hauptmietzins" in vorschau.text
    assert "Freigeben" in vorschau.text

    saldo_vorher = op_service.berechne_saldo(konto_id).saldo_cent
    freigabe = client.post(
        "/backoffice/vertrag/V-601-1/vorschreibung/sollstellen",
        data={"csrf_token": csrf, "monat": "2026-07"}, follow_redirects=False,
    )
    assert freigabe.status_code == 303
    assert op_service.berechne_saldo(konto_id).saldo_cent == saldo_vorher + 55_000


def test_mahnvorschau_blockiert_ohne_bankbestaetigung_und_sendet_nie_echt(backoffice_client):
    """Ohne bestätigte Bankvollständigkeit (aus persistierten Bankdaten
    serverseitig abgeleitet, nicht aus einem Formularfeld) bleibt die
    Forderung BLOCKIERT; es erscheint kein "Sendebereitschaft"-Button
    (der ohnehin nie einen echten Versand auslösen würde) und erst recht
    kein echter Senden-Button."""

    client, konto_id, _konto_gesperrt_id, op_service = backoffice_client
    _login(client)
    from mietinkasso.domain.enums import OPTyp

    op_service.buchen(
        ctx=_ctx_admin(), konto=_konto_by_id(konto_id), typ=OPTyp.SOLL, betrag_cent=60_000,
        belegdatum=date(2026, 1, 1), buchungsdatum=date(2026, 1, 1), faelligkeit=date(2026, 1, 5),
        beleg_referenz="Mahnvorschau-Testforderung",
    )
    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.mahnwesen.repository import MahnPolicyRepository

    mahn_policy_repo = MahnPolicyRepository(build_session_factory(get_settings().database_url))
    if mahn_policy_repo.aktuelle_freigegebene() is None:
        policy = mahn_policy_repo.anlegen(
            stufe1_tage_nach_faelligkeit=7, stufe2_mindesttage_nach_stufe1_versand=14, status="ENTWURF",
        )
        mahn_policy_repo.freigeben(policy.id)

    heute = (date.today() + timedelta(days=400)).isoformat()
    vorschau = client.get("/backoffice/vertrag/V-601-1/mahnvorschau", params={"heute": heute})
    assert vorschau.status_code == 200
    assert "BLOCKIERT" in vorschau.text
    assert "keine ausreichend aktuelle" in vorschau.text
    assert "sendebereitschaft" not in vorschau.text  # kein Sendebereitschafts-Button für einen BLOCKIERTEN Fall
    assert "Kein Senden-Button löst einen echten Mailversand aus" in vorschau.text


def _ctx_admin():
    from mietinkasso.auth.service import AuthContext
    from mietinkasso.domain.enums import Rolle

    return AuthContext(user_id="test", rolle=Rolle.ADMIN, gesellschaft_ids=None)


def _konto_by_id(konto_id: str):
    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.stammdaten.repository import StammdatenRepository

    return StammdatenRepository(build_session_factory(get_settings().database_url)).get_konto(konto_id)
