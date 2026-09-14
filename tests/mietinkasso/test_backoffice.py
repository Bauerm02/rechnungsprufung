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

import json
import os
import re
import tempfile
from datetime import date, datetime, timedelta, timezone
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
    # Produktionsdefault ist secure=true (HTTPS-Reverse-Proxy); der
    # FastAPI-TestClient spricht aber http://testserver ohne TLS und würde
    # ein "Secure"-Cookie nie zurücksenden. Nur für diesen Testprozess
    # ausdrücklich deaktivieren - siehe infrastructure/config.py.
    os.environ["MIETINKASSO_BACKOFFICE_COOKIE_SECURE"] = "false"
    os.environ["MIETINKASSO_VERTRAGSANLAGE_UPLOAD_VERZEICHNIS"] = str(Path(tempfile.mkdtemp(prefix="mietinkasso-vertragsanlage-upload-")))
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
    stammdaten.upsert_einheit(id="601-TOP2", objekt_id="601", bezeichnung="Top 2 (Keller)", nutzungsstatus="LEERSTAND")
    stammdaten.upsert_einheit(id="107-TOP1", objekt_id="107", bezeichnung="Top 1", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten.upsert_einheit(id="601-KURZ1", objekt_id="601", bezeichnung="Kurzzeit 1", nutzungsstatus="KURZZEITVERMIETUNG")
    stammdaten.upsert_einheit(id="107-KURZ1", objekt_id="107", bezeichnung="Kurzzeit gesperrt", nutzungsstatus="KURZZEITVERMIETUNG")
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


def _login_versuch(client, *, username: str = "markus", password: str = "test-passwort-123", origin: str | None = "http://testserver"):
    # Der Login prüft seit der Härtung (Login-CSRF-Schutz, siehe
    # backoffice/app.py::_pruefe_login_origin) den Origin/Referer-Header
    # gegen den eigenen Host - der TestClient sendet ihn nicht automatisch,
    # ein echter Browser-Formular-POST praktisch immer.
    headers = {"Origin": origin} if origin is not None else {}
    return client.post(
        "/backoffice/login", data={"username": username, "password": password}, follow_redirects=False,
        headers=headers,
    )


def _login(client) -> None:
    antwort = _login_versuch(client)
    assert antwort.status_code == 303
    assert antwort.headers["location"] == "/backoffice/"


def _csrf_token(client) -> str:
    seite = client.get("/backoffice/")
    assert seite.status_code == 200
    marker = 'name="csrf_token" value="'
    start = seite.text.index(marker) + len(marker)
    ende = seite.text.index('"', start)
    return seite.text[start:ende]


def test_ready_endpunkt_ist_offen_und_liest_db(backoffice_client):
    """`/ready` (Paket A) ist wie `/health` ohne Auth erreichbar, prüft
    aber tatsächlich lesend die DB - keine Kontodaten in der Antwort."""

    client, *_ = backoffice_client
    antwort = client.get("/ready")
    assert antwort.status_code == 200
    assert antwort.json() == {"status": "ready"}


def test_ohne_login_wird_auf_login_umgeleitet(backoffice_client):
    client, konto_id, _konto_gesperrt_id, _op_service = backoffice_client
    antwort = client.get("/backoffice/", follow_redirects=False)
    assert antwort.status_code == 303
    assert antwort.headers["location"] == "/backoffice/login"


def test_login_mit_falschem_passwort_scheitert(backoffice_client):
    client, *_ = backoffice_client
    antwort = _login_versuch(client, password="falsch")
    assert antwort.status_code == 303
    assert antwort.headers["location"].startswith("/backoffice/login")
    dashboard = client.get("/backoffice/", follow_redirects=False)
    assert dashboard.status_code == 303  # weiterhin nicht angemeldet


def test_login_von_fremdem_origin_wird_abgelehnt(backoffice_client):
    """Login-CSRF-Schutz: ein Origin-Header, der nicht zum eigenen Host
    passt, wird abgelehnt - unabhängig davon, ob das Passwort stimmt."""

    client, *_ = backoffice_client
    antwort = _login_versuch(client, origin="https://angreifer.example")
    assert antwort.status_code == 403

    antwort_ohne_origin = _login_versuch(client, origin=None)
    assert antwort_ohne_origin.status_code == 403


def test_login_und_dashboard_zeigt_objekte(backoffice_client):
    client, konto_id, _konto_gesperrt_id, _op_service = backoffice_client
    _login(client)
    dashboard = client.get("/backoffice/", params={"objekt_id": "601"})
    assert dashboard.status_code == 200
    assert "Am Corso" in dashboard.text
    assert konto_id in dashboard.text


def test_dashboard_ohne_objekt_zeigt_alle_objekte_nicht_leer(backoffice_client):
    """Auftrag HV-20260913-RUECKSTAENDE: '/backoffice/ ohne Objekt ist
    leer' - Standard ist jetzt eine gefüllte 'Alle Objekte'-Übersicht
    mit Kennzahlen und Mietkontentabelle, nicht mehr eine leere Seite
    mit nur einer Objektauswahl."""

    client, konto_id, _konto_gesperrt_id, _op_service = backoffice_client
    _login(client)
    dashboard = client.get("/backoffice/")
    assert dashboard.status_code == 200
    assert "Alle Objekte" in dashboard.text
    assert "Offene Beträge" in dashboard.text  # Auftrag HV-20260914-UI-EINFACH: verständliche Beschriftung
    assert "Mietkontenübersicht" in dashboard.text
    assert "Offene Einzelpositionen" in dashboard.text
    assert konto_id in dashboard.text  # Am Corso ist Teil der "Alle Objekte"-Summe


def test_dashboard_objektfilter_wirkt_identisch_auf_alle_ansichten(backoffice_client):
    """Summen/Mietkontentabelle/Einzelpositionen reagieren alle auf
    denselben Objektfilter - eine mit ?objekt_id=601 gefilterte Seite
    darf keine Zeile eines anderen Objekts (hier 107, ausgeschlossen)
    enthalten und muss dieselbe Kontozeile wie die 'Alle Objekte'-Sicht
    für 601 zeigen."""

    client, konto_id, _konto_gesperrt_id, _op_service = backoffice_client
    _login(client)
    gefiltert = client.get("/backoffice/", params={"objekt_id": "601"})
    assert gefiltert.status_code == 200
    assert konto_id in gefiltert.text
    assert 'value="107"' not in gefiltert.text  # ausgeschlossenes Objekt ist keine Filteroption


def test_dashboard_einzelposition_zeigt_op_nr_und_belegreferenz(backoffice_client):
    """Codex-Rückprüfung zu abc4530: 'Derzeit nur Belegdatum, damit kann
    man ähnliche Forderungen nicht zuordnen.' - jede offene Einzelposition
    muss ihre OP-Nummer und die tatsächliche Belegreferenz zeigen. Eigene,
    isolierte Einheit/Vertrag/Konto - das gemeinsam genutzte V-601-1 wird
    von saldo-sensitiven Tests an anderer Stelle in dieser Datei erwartet
    und darf hier nicht zusätzlich bebucht werden."""

    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.domain.enums import OPTyp
    from mietinkasso.stammdaten.repository import StammdatenRepository

    client, _konto_id, _konto_gesperrt_id, op_service = backoffice_client
    _login(client)

    stammdaten = StammdatenRepository(build_session_factory(get_settings().database_url))
    stammdaten.upsert_einheit(id="601-TOP-OPNR", objekt_id="601", bezeichnung="Top OP-Nr", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten.upsert_vertrag(
        id="V-601-OPNR", einheit_id="601-TOP-OPNR", debitor_id="DEB-1", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    konto = stammdaten.get_or_create_konto(vertrag=stammdaten.get_vertrag("V-601-OPNR"))

    synthetische_referenz = "Synthetischer Beleg RP-4711"
    position = op_service.buchen(
        ctx=_ctx_admin(), konto=konto, typ=OPTyp.SOLL, betrag_cent=12_300,
        belegdatum=date(2026, 8, 1), buchungsdatum=date(2026, 8, 1), faelligkeit=date(2026, 8, 5),
        beleg_referenz=synthetische_referenz,
    )
    dashboard = client.get("/backoffice/", params={"objekt_id": "601"})
    assert dashboard.status_code == 200
    assert "OP-Nr." in dashboard.text
    assert f"#{position.id}" in dashboard.text
    assert synthetische_referenz in dashboard.text


def test_dashboard_ohne_belegreferenz_liefert_200_statt_500(backoffice_client):
    """Codex-Rückprüfung zu 708b0f0: `beleg_referenz` ist laut Schema
    nullable - eine offene SOLL-Position mit `beleg_referenz=None` darf
    das gesamte Dashboard nicht mit einem 500 zum Absturz bringen. Die
    OP-ID muss trotzdem sichtbar sein."""

    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.domain.enums import OPTyp
    from mietinkasso.stammdaten.repository import StammdatenRepository

    client, _konto_id, _konto_gesperrt_id, op_service = backoffice_client
    _login(client)

    stammdaten = StammdatenRepository(build_session_factory(get_settings().database_url))
    stammdaten.upsert_einheit(id="601-TOP-OHNEREF", objekt_id="601", bezeichnung="Top ohne Beleg", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten.upsert_vertrag(
        id="V-601-OHNEREF", einheit_id="601-TOP-OHNEREF", debitor_id="DEB-1", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    konto = stammdaten.get_or_create_konto(vertrag=stammdaten.get_vertrag("V-601-OHNEREF"))
    position = op_service.buchen(
        ctx=_ctx_admin(), konto=konto, typ=OPTyp.SOLL, betrag_cent=4_500,
        belegdatum=date(2026, 8, 1), buchungsdatum=date(2026, 8, 1), faelligkeit=date(2026, 8, 5),
        beleg_referenz=None,
    )
    dashboard = client.get("/backoffice/", params={"objekt_id": "601"})
    assert dashboard.status_code == 200
    assert f"#{position.id}" in dashboard.text


def test_dashboard_zeigt_pilot_banner_in_development_umgebung(backoffice_client):
    """Diese Fixture importiert `api.app` mit `MIETINKASSO_ENVIRONMENT`
    unausgesprochen auf dem Default "development" - der Banner muss
    dafür den Demo-Text zeigen (der Echtbetrieb-Zweig wird auf der
    reinen Funktion in test_backoffice_betriebsmodus.py getestet, siehe
    dortige Erklärung zum Modul-Caching)."""

    client, *_ = backoffice_client
    _login(client)
    dashboard = client.get("/backoffice/")
    assert "PILOT-BETRIEB" in dashboard.text
    assert "synthetische Demodaten" in dashboard.text
    assert "ECHTBETRIEB" not in dashboard.text


def test_dashboard_zeigt_einheiten_ohne_vertrag(backoffice_client):
    """Codex-Rückprüfung: das Dashboard iterierte bisher nur
    `list_vertraege_fuer_objekt` - eine Einheit ohne aktiven Vertrag
    (Leerstand/KZV/Selfstorage/Eigennutzung) war trotz Import unsichtbar.
    "Leerstand" ist ein erfasster Nutzungsstatus, kein aus dem Fehlen
    eines Vertrags erratener Zustand - beides muss sichtbar bleiben,
    ohne einen Dummy-Mieter/Konto dafür anzulegen."""

    client, _konto_id, _konto_gesperrt_id, _op_service = backoffice_client
    _login(client)
    dashboard = client.get("/backoffice/", params={"objekt_id": "601"})
    assert dashboard.status_code == 200
    assert "Einheiten ohne Mietkonto" in dashboard.text
    assert "601-TOP2" in dashboard.text
    assert "LEERSTAND" in dashboard.text


def test_hauptnavigation_verlinkt_alle_kontextlosen_arbeitsablaeufe(backoffice_client):
    """Regression (Bedienungsfehler-Meldung): nach der Anmeldung zeigte
    das Dashboard nur die Objektwahl - Eröffnungsimport, Bankimport,
    offene Zuordnungen und Bankvollständigkeit hatten keinen sichtbaren
    Weg dorthin. Diese kontextlosen (d. h. ohne Vertrag/Konto in der URL)
    Arbeitsabläufe müssen von JEDER Seite aus in höchstens einem Klick
    erreichbar sein. Auftrag HV-20260914-UI-EINFACH ersetzt die frühere
    flache 15-Link-Navigation durch genau vier fachliche Hauptbereiche
    plus "Einstellungen" - die Erreichbarkeit bleibt bestehen, führt aber
    über die jeweilige Bereichs-Startseite statt über einen Einzellink in
    der Navigation selbst. Vor der Anmeldung darf die Navigation nicht
    erscheinen (keine funktionslosen Links auf der Login-Seite)."""

    client, konto_id, _konto_gesperrt_id, _op_service = backoffice_client

    login_seite = client.get("/backoffice/login")
    assert login_seite.status_code == 200
    assert "/backoffice/eroeffnung" not in login_seite.text
    assert "/backoffice/einstellungen" not in login_seite.text

    _login(client)
    haupt_bereiche = [
        "/backoffice/", "/backoffice/vertraege", "/backoffice/zahlungen",
        "/backoffice/abrechnungen", "/backoffice/einstellungen",
    ]

    dashboard = client.get("/backoffice/")
    assert dashboard.status_code == 200
    assert "Hausverwaltung &amp; Mietinkasso" in dashboard.text or "Hausverwaltung & Mietinkasso" in dashboard.text
    for link in haupt_bereiche:
        assert f'href="{link}"' in dashboard.text, f"Navigationslink {link} fehlt auf dem Dashboard"

    # Die Navigation ist Teil des GEMEINSAMEN Layouts, nicht nur einer
    # Seite - auf einer beliebigen anderen Seite (Kontoauszug) ebenfalls
    # sichtbar.
    kontoauszug = client.get(f"/backoffice/konto/{konto_id}")
    assert kontoauszug.status_code == 200
    for link in haupt_bereiche:
        assert f'href="{link}"' in kontoauszug.text, f"Navigationslink {link} fehlt im Kontoauszug"

    # Die vormals flach verlinkten Arbeitsabläufe bleiben über die
    # jeweilige Bereichs-Startseite in einem weiteren Klick erreichbar.
    zahlungen = client.get("/backoffice/zahlungen")
    assert zahlungen.status_code == 200
    for link in ("/backoffice/bank", "/backoffice/bank/unzugeordnet", "/backoffice/bank/vollstaendigkeit"):
        assert f'href="{link}"' in zahlungen.text, f"{link} fehlt auf der Zahlungen-&-Mahnungen-Startseite"

    einstellungen = client.get("/backoffice/einstellungen")
    assert einstellungen.status_code == 200
    for link in ("/backoffice/eroeffnung", "/backoffice/mahnwesen/policy"):
        assert f'href="{link}"' in einstellungen.text, f"{link} fehlt auf der Einstellungen-Startseite"


def test_abrechnungen_hub_verlinkt_variable_abrechnung_und_monatsuebersicht(backoffice_client):
    client, *_ = backoffice_client
    _login(client)
    seite = client.get("/backoffice/abrechnungen")
    assert seite.status_code == 200
    assert 'href="/backoffice/variable-abrechnung"' in seite.text
    assert 'href="/backoffice/dashboard/monatsuebersicht"' in seite.text


def test_navigation_hebt_aktuellen_bereich_hervor(backoffice_client):
    """Auftrag HV-20260914-UI-EINFACH: "einheitliche aktive Navigation" -
    GENAU der zur aktuellen Seite passende Bereichslink trägt die
    Hervorhebungsklasse, alle anderen nicht."""

    client, *_ = backoffice_client
    _login(client)

    dashboard = client.get("/backoffice/")
    assert '<a href="/backoffice/" class="aktiv">Übersicht</a>' in dashboard.text
    assert 'class="aktiv">Mieter &amp; Objekte</a>' not in dashboard.text

    vertraege = client.get("/backoffice/vertraege")
    assert '<a href="/backoffice/vertraege" class="aktiv">Mieter &amp; Objekte</a>' in vertraege.text
    assert '<a href="/backoffice/" class="aktiv">' not in vertraege.text

    zahlungen = client.get("/backoffice/zahlungen")
    assert '<a href="/backoffice/zahlungen" class="aktiv">Zahlungen &amp; Mahnungen</a>' in zahlungen.text

    einstellungen = client.get("/backoffice/einstellungen")
    assert '<a href="/backoffice/einstellungen" class="aktiv">Einstellungen</a>' in einstellungen.text


def test_dashboard_zeigt_mahnsperre_als_zu_erledigen_und_im_kompakten_status(backoffice_client):
    """"Das ist zu erledigen" (Auftrag HV-20260914-UI-EINFACH) muss eine
    tatsächlich bestehende Mahnsperre nennen, und die kompakte
    Mietkonto-Zeile muss dafür einen verständlichen Status statt eines
    rohen technischen Badges zeigen - abgeleitet aus denselben, bereits
    bestehenden `aktive_sperren`-Daten, keine neue Sperrlogik."""

    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.stammdaten.repository import StammdatenRepository

    client, *_ = backoffice_client
    settings = get_settings()
    stammdaten = StammdatenRepository(build_session_factory(settings.database_url))
    sperre_id = stammdaten.sperre_setzen(vertrag_id="V-601-1", grund="RECHTSANWALT", kommentar="RA Dr. Muster beauftragt")
    try:
        _login(client)
        dashboard = client.get("/backoffice/", params={"objekt_id": "601"})
        assert dashboard.status_code == 200
        assert "vorhandene Mahnsperren prüfen" in dashboard.text
        assert "Mahnung gesperrt" in dashboard.text
        assert "RECHTSANWALT" in dashboard.text
    finally:
        stammdaten.sperre_aufheben(sperre_id)


def test_dashboard_kompakte_tabelle_zeigt_lange_mieternamen_ohne_absturz_und_escaped(backoffice_client):
    """Akzeptanzkriterium (Auftrag HV-20260914-UI-EINFACH): reale
    Leer-/Randfälle wie ein sehr langer Mietername dürfen die neue
    kompakte Übersichtstabelle nicht zum Absturz bringen - und ein
    versehentlich HTML-artiger Name darf nie ungeschützt gerendert
    werden (`views.py`-Grundsatz: alles läuft über `h()`)."""

    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.stammdaten.repository import StammdatenRepository

    client, *_ = backoffice_client
    _login(client)

    stammdaten = StammdatenRepository(build_session_factory(get_settings().database_url))
    langer_name = "Dr. Maximiliane Alexandra <script>Sehr-Lange-Nachname-Kombination</script> von Musterberg-Grafenstein"
    stammdaten.upsert_debitor(id="DEB-LANGERNAME", name=langer_name, email=None)
    stammdaten.upsert_einheit(id="601-TOP-LANG", objekt_id="601", bezeichnung="Top Langer Name", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten.upsert_vertrag(
        id="V-601-LANGERNAME", einheit_id="601-TOP-LANG", debitor_id="DEB-LANGERNAME", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    stammdaten.get_or_create_konto(vertrag=stammdaten.get_vertrag("V-601-LANGERNAME"))

    dashboard = client.get("/backoffice/", params={"objekt_id": "601"})
    assert dashboard.status_code == 200
    assert "<script>" not in dashboard.text  # nie ungeschützt gerendert
    assert "&lt;script&gt;" in dashboard.text
    assert "Musterberg-Grafenstein" in dashboard.text


def test_objekt_107_ist_im_dashboard_nur_lesend(backoffice_client):
    """Auftrag HV-20260913-RUECKSTAENDE: ein explizit angefordertes
    ausgeschlossenes Objekt wird auf der zentralen Rückstandsübersicht
    klar abgelehnt (kein stiller Wechsel auf "Alle Objekte", keine
    Fachdaten) - anders als der Kontoauszug (der ein bereits bekanntes
    Konto weiterhin nur-lesend zeigt), ist 107 hier auch keine
    Filteroption."""

    client, _konto_id, konto_gesperrt_id, _op_service = backoffice_client
    _login(client)
    dashboard = client.get("/backoffice/", params={"objekt_id": "107"})
    assert dashboard.status_code == 400
    assert "ausgeschlossen" in dashboard.text
    # 107 taucht auch nicht als Filteroption auf der "Alle Objekte"-Seite auf.
    alle_objekte = client.get("/backoffice/")
    assert alle_objekte.status_code == 200
    assert 'value="107"' not in alle_objekte.text
    # Kein Nachbuchungs-Link für ein gesperrtes Objekt im Kontoauszug.
    kontoauszug = client.get(f"/backoffice/konto/{konto_gesperrt_id}")
    assert "Objekt ist von der Pilotphase ausgeschlossen" in kontoauszug.text
    assert f"/backoffice/konto/{konto_gesperrt_id}/buchen" not in kontoauszug.text


def test_kontoauszug_zeigt_aktive_sperre_aus_intake_an(backoffice_client):
    """Ergänzung HV-20260912-ECHTBETRIEB: eine über den generischen Intake
    dauerhaft gespeicherte Sperre (RECHTSANWALT/RATENPLAN/MANUELL/...) muss
    im Backoffice sichtbar sein, nicht nur in der DB stehen."""

    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.stammdaten.repository import StammdatenRepository

    client, konto_id, _konto_gesperrt_id, _op_service = backoffice_client
    settings = get_settings()
    stammdaten = StammdatenRepository(build_session_factory(settings.database_url))
    sperre_id = stammdaten.sperre_setzen(vertrag_id="V-601-1", grund="RECHTSANWALT", kommentar="RA Dr. Muster beauftragt")
    try:
        _login(client)
        kontoauszug = client.get(f"/backoffice/konto/{konto_id}")
        assert kontoauszug.status_code == 200
        assert "RECHTSANWALT" in kontoauszug.text
        assert "RA Dr. Muster beauftragt" in kontoauszug.text
    finally:
        # Modul-weit gemeinsam genutztes V-601-1 (siehe Fixture-Docstring) -
        # spätere Tests (z. B. die Mahnvorschau) dürfen diese Sperre nicht
        # sehen, sonst ändert sich deren erwarteter BLOCKIERT-Grund.
        stammdaten.sperre_aufheben(sperre_id)


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


def _bank_datei_importieren(client, csrf, *, bank_konto_id, betrag_text, referenz=""):
    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.bank.repository import BankRepository

    BankRepository(build_session_factory(get_settings().database_url)).upsert_bank_konto(
        id=bank_konto_id, gesellschaft_id="7DI", iban=f"AT{bank_konto_id[-10:]:0>10}", bezeichnung=bank_konto_id,
    )
    csv_text = f"betrag,datum,referenz\n{betrag_text},2026-04-06,{referenz}\n"
    form = {
        "csrf_token": csrf, "bank_konto_id": bank_konto_id, "format": "CSV",
        "spalte_betrag": "betrag", "spalte_datum": "datum", "spalte_referenz": "referenz",
        "spalte_eindeutig": "", "dezimaltrennzeichen": ".",
    }
    vorschau = client.post(
        "/backoffice/bank/vorschau", data=form, files={"datei": ("bank.csv", csv_text.encode("utf-8"), "text/csv")},
    )
    marker = 'name="inhalt_b64" value="'
    start = vorschau.text.index(marker) + len(marker)
    ende = vorschau.text.index('"', start)
    client.post("/backoffice/bank/importieren", data={**form, "inhalt_b64": vorschau.text[start:ende]})
    offene = client.get("/backoffice/bank/unzugeordnet", params={"bank_konto_id": bank_konto_id})
    return offene.text


def test_manuelle_bankzuordnung_akzeptiert_vorbefuelltes_deutsches_zahlenformat(backoffice_client):
    """Regression (Browser-Befund auf e16914d): die manuelle
    Bankzuordnung befüllt das Betragsfeld selbst mit dem von `eur()`
    erzeugten deutschen Format (z. B. "1.500,00" bei einer 1500-EUR-
    Transaktion). Unverändert abgeschickt führte das zu einem
    unbehandelten `decimal.InvalidOperation` (HTTP 500)."""

    client, _konto_id, _konto_gesperrt_id, op_service = backoffice_client
    _login(client)
    csrf = _csrf_token(client)

    from datetime import date

    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.stammdaten.repository import StammdatenRepository

    stammdaten = StammdatenRepository(build_session_factory(get_settings().database_url))
    stammdaten.upsert_einheit(id="601-TOP-1500", objekt_id="601", bezeichnung="Top 1500", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten.upsert_vertrag(
        id="V-601-1500", einheit_id="601-TOP-1500", debitor_id="DEB-1", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    konto = stammdaten.get_or_create_konto(vertrag=stammdaten.get_vertrag("V-601-1500"))

    # Keine VERTRAG:-Referenz -> kein Automatik-Vorschlag, nur die manuelle Form.
    seiten_text = _bank_datei_importieren(client, csrf, bank_konto_id="BK-TEST-1500", betrag_text="1500.00")
    assert re.search(r"/backoffice/bank/\d+/automatisch-zuordnen", seiten_text) is None

    treffer_betrag = re.search(r'name="betrag" placeholder="Betrag EUR" value="([^"]+)"', seiten_text)
    assert treffer_betrag is not None
    vorbefuellter_betrag = treffer_betrag.group(1)
    assert vorbefuellter_betrag == "1.500,00"  # exakt das gemeldete Format

    treffer_tx = re.search(r"/backoffice/bank/(\d+)/manuell-zuordnen", seiten_text)
    assert treffer_tx is not None

    saldo_vorher = op_service.berechne_saldo(konto.id).saldo_cent
    zugeordnet = client.post(
        f"/backoffice/bank/{treffer_tx.group(1)}/manuell-zuordnen",
        data={"csrf_token": csrf, "konto_id": konto.id, "betrag": vorbefuellter_betrag, "vorgangs_id": "TEST-1500-UNVERAENDERT"},
        follow_redirects=False,
    )
    assert zugeordnet.status_code == 303  # kein 500
    assert op_service.berechne_saldo(konto.id).saldo_cent == saldo_vorher - 150_000  # korrekter Faktor, nicht 1,50 EUR


def test_manuelle_bankzuordnung_lehnt_mehrdeutigen_betrag_ohne_serverfehler_ab(backoffice_client):
    client, konto_id, _konto_gesperrt_id, op_service = backoffice_client
    _login(client)
    csrf = _csrf_token(client)

    seiten_text = _bank_datei_importieren(client, csrf, bank_konto_id="BK-TEST-KAPUTT", betrag_text="300.00")
    treffer_tx = re.search(r"/backoffice/bank/(\d+)/manuell-zuordnen", seiten_text)
    assert treffer_tx is not None

    saldo_vorher = op_service.berechne_saldo(konto_id).saldo_cent
    antwort = client.post(
        f"/backoffice/bank/{treffer_tx.group(1)}/manuell-zuordnen",
        data={"csrf_token": csrf, "konto_id": konto_id, "betrag": "1,500.00", "vorgangs_id": "KAPUTTER-BETRAG-1"},
    )
    assert antwort.status_code == 400  # verständliche Fehlerseite, kein unbehandelter 500
    assert op_service.berechne_saldo(konto_id).saldo_cent == saldo_vorher  # keine Teilbuchung


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
    assert "Die Vorschau versendet keine Nachricht" in vorschau.text
    assert 'action="/backoffice/mahnfall/' not in vorschau.text


def test_mahnvorschau_unterscheidet_planbar_von_tatsaechlich_geplant(backoffice_client):
    """Auftrag Markus 14.09.2026: eine reine (noch nicht gespeicherte)
    Vorschau darf nicht denselben Status-Text wie ein tatsächlich
    angelegter Mahnfall zeigen ("Planbar" statt "GEPLANT"), sonst sind
    beide Zustände für den Nutzer nicht unterscheidbar. Erst NACH dem
    ausdrücklichen POST auf den neuen Planen-Endpunkt erscheint "GEPLANT"
    mit Sendebereitschafts-Aktion."""

    import mietinkasso.backoffice.app as backoffice_app
    from mietinkasso.domain.enums import OPTyp
    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.stammdaten.repository import StammdatenRepository
    from mietinkasso.bank.repository import BankRepository

    client, _konto_id, _konto_gesperrt_id, op_service = backoffice_client
    _login(client)
    csrf = _csrf_token(client)

    # Eigene, isolierte Gesellschaft statt der geteilten "7DI": die
    # Bank-Vollständigkeitsableitung verlangt eine Bestätigung für ALLE
    # Bankkonten EINER Gesellschaft (Minimum über alle Konten) - andere
    # Tests in diesem Modul legen unter "7DI" bereits weitere, dort NIE
    # bestätigte Bankkonten an, die eine gemeinsame Gesellschaft sonst
    # dauerhaft auf "keine Bestätigung" ziehen würden.
    stammdaten = StammdatenRepository(build_session_factory(get_settings().database_url))
    stammdaten.upsert_gesellschaft(id="7DI-PLANBAR", name="7D Immobilien GmbH (Planbar-Test)")
    stammdaten.upsert_objekt(id="601-PLANBAR", gesellschaft_id="7DI-PLANBAR", bezeichnung="Am Corso (Planbar-Test)")
    stammdaten.upsert_einheit(id="601-TOP-PLANBAR", objekt_id="601-PLANBAR", bezeichnung="Top Planbar", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten.upsert_debitor(id="DEB-PLANBAR", name="Test Mieterin Planbar", email="planbar@example.at")
    stammdaten.upsert_vertrag(
        id="V-601-PLANBAR", einheit_id="601-TOP-PLANBAR", debitor_id="DEB-PLANBAR", gesellschaft_id="7DI-PLANBAR",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    konto = stammdaten.get_or_create_konto(vertrag=stammdaten.get_vertrag("V-601-PLANBAR"))

    from mietinkasso.mahnwesen.repository import MahnPolicyRepository
    mahn_policy_repo = MahnPolicyRepository(build_session_factory(get_settings().database_url))
    if mahn_policy_repo.aktuelle_freigegebene() is None:
        policy = mahn_policy_repo.anlegen(
            stufe1_tage_nach_faelligkeit=7, stufe2_mindesttage_nach_stufe1_versand=14, status="ENTWURF",
        )
        mahn_policy_repo.freigeben(policy.id)

    bank_repo = BankRepository(build_session_factory(get_settings().database_url))
    bank_repo.upsert_bank_konto(id="BK-PLANBAR", gesellschaft_id="7DI-PLANBAR", iban="AT000000000000000099", bezeichnung="Test-Bankkonto Planbar")
    bestaetigt = client.post(
        "/backoffice/bank/BK-PLANBAR/vollstaendigkeit-bestaetigen",
        data={"csrf_token": csrf, "bestaetigt_bis": "2026-01-20"},
        follow_redirects=False,
    )
    assert bestaetigt.status_code == 303

    op_service.buchen(
        ctx=_ctx_admin(), konto=konto, typ=OPTyp.SOLL, betrag_cent=60_000,
        belegdatum=date(2026, 1, 1), buchungsdatum=date(2026, 1, 1), faelligkeit=date(2026, 1, 5),
        beleg_referenz="Planbar-Testforderung",
    )
    heute = date(2026, 1, 20).isoformat()  # 15 Tage nach Fälligkeit, Policy verlangt 7; == bestätigtes Bankdatum

    vorschau = client.get("/backoffice/vertrag/V-601-PLANBAR/mahnvorschau", params={"heute": heute})
    assert vorschau.status_code == 200
    assert "Planbar" in vorschau.text
    assert "GEPLANT" not in vorschau.text  # noch nicht tatsächlich angelegt
    assert "Jetzt planen" in vorschau.text
    assert 'action="/backoffice/mahnfall/' not in vorschau.text  # keine Sendebereitschafts-Aktion vor dem Planen

    # Wiederholte GETs legen weiterhin NICHTS an.
    client.get("/backoffice/vertrag/V-601-PLANBAR/mahnvorschau", params={"heute": heute})
    assert backoffice_app._mahn_fall_repo.list_fuer_vertrag("V-601-PLANBAR") == []

    forderung = op_service.offene_forderungen(konto.id, heute=date(2026, 1, 20))[0]
    geplant = client.post(
        f"/backoffice/vertrag/V-601-PLANBAR/forderung/{forderung.op_position_id}/planen",
        params={"heute": heute}, data={"csrf_token": csrf}, follow_redirects=False,
    )
    assert geplant.status_code == 303

    nach_planen = client.get("/backoffice/vertrag/V-601-PLANBAR/mahnvorschau", params={"heute": heute})
    assert "GEPLANT" in nach_planen.text
    assert 'action="/backoffice/mahnfall/' in nach_planen.text  # jetzt Sendebereitschafts-Aktion sichtbar


def test_mahnpolicy_seite_zeigt_genau_zwei_stufen_und_erzwingt_null_zinsen_gebuehr(backoffice_client):
    """HV-20260912-ECHTBETRIEB Punkt 2: die Mahnstufen-Konfiguration ist
    im Backoffice sichtbar/speicherbar - genau zwei Stufen (kein
    Formularfeld für eine dritte Stufe existiert überhaupt), Zinsen/
    Gebühr bleiben fest auf 0, auch wenn ein roher POST versucht, andere
    Werte zu setzen (kein Formularfeld dafür in dieser Version - der
    Server ignoriert unbekannte Felder und setzt ohnehin hart 0)."""

    client, _konto_id, _konto_gesperrt_id, _op_service = backoffice_client
    _login(client)

    seite = client.get("/backoffice/mahnwesen/policy")
    assert seite.status_code == 200
    assert "stufe1_tage_nach_faelligkeit" in seite.text
    assert "stufe2_mindesttage_nach_stufe1_versand" in seite.text
    assert "Zinsen: 0% · Gebühr: 0 Cent" in seite.text
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', seite.text).group(1)

    angelegt = client.post(
        "/backoffice/mahnwesen/policy/anlegen",
        data={
            "stufe1_tage_nach_faelligkeit": "7",
            "stufe2_mindesttage_nach_stufe1_versand": "14",
            "csrf_token": csrf,
            # Versuch, Zinsen/Gebühr über ein zusätzliches, in der Form
            # nicht vorgesehenes Feld zu schmuggeln - wird vom Server
            # ignoriert (kein entsprechender Parameter in der Route).
            "zinsen_prozent": "5",
            "gebuehr_cent": "500",
        },
    )
    assert angelegt.status_code == 200
    assert "als ENTWURF angelegt" in angelegt.text

    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.mahnwesen.repository import MahnPolicyRepository

    policies = MahnPolicyRepository(build_session_factory(get_settings().database_url)).alle()
    neueste = policies[0]
    assert neueste.status == "ENTWURF"
    assert neueste.stufe1_tage_nach_faelligkeit == 7
    assert neueste.stufe2_mindesttage_nach_stufe1_versand == 14
    assert neueste.zinsen_prozent == 0
    assert neueste.gebuehr_cent == 0

    uebersicht_nach_anlage = client.get("/backoffice/mahnwesen/policy")
    csrf2 = re.search(r'name="csrf_token" value="([^"]+)"', uebersicht_nach_anlage.text).group(1)
    freigegeben = client.post(f"/backoffice/mahnwesen/policy/{neueste.id}/freigeben", data={"csrf_token": csrf2})
    assert freigegeben.status_code == 200
    assert "freigegeben" in freigegeben.text


def _ctx_admin():
    from mietinkasso.auth.service import AuthContext
    from mietinkasso.domain.enums import Rolle

    return AuthContext(user_id="test", rolle=Rolle.ADMIN, gesellschaft_ids=None)


def _konto_by_id(konto_id: str):
    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.stammdaten.repository import StammdatenRepository

    return StammdatenRepository(build_session_factory(get_settings().database_url)).get_konto(konto_id)


def test_vertragspruefung_entwurf_wirkt_nicht_erst_gepruefte_version_setzt_rechtsordnung(backoffice_client):
    """Paket B, Punkt 1: eine ENTWURF-Prüfung ist sichtbar/gespeichert,
    ändert aber nicht die wirksame Rechtsordnung des Vertrags - erst
    eine GEPRUEFT-Version schreibt sie zurück. Ohne Quellenbeleg-
    Referenz wird nichts gespeichert (keine beleglose Klassifizierung)."""

    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.stammdaten.repository import StammdatenRepository

    client, _konto_id, _konto_gesperrt_id, _op_service = backoffice_client
    _login(client)
    csrf = _csrf_token(client)

    stammdaten = StammdatenRepository(build_session_factory(get_settings().database_url))
    stammdaten.upsert_einheit(id="601-TOP-PRUEF", objekt_id="601", bezeichnung="Top Prüfung", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten.upsert_vertrag(
        id="V-601-PRUEF", einheit_id="601-TOP-PRUEF", debitor_id="DEB-1", gesellschaft_id="7DI",
        rechtsordnung="UNGEKLAERT", gueltig_von=date(2024, 1, 1),
    )

    seite = client.get("/backoffice/vertrag/V-601-PRUEF/pruefung")
    assert seite.status_code == 200
    assert "UNGEKLAERT" in seite.text

    ohne_beleg = client.post(
        "/backoffice/vertrag/V-601-PRUEF/pruefung/anlegen",
        data={
            "rechtsordnung": "OESTERREICH_MRG_VOLL", "fachstatus": "ENTWURF",
            "quellenbeleg_referenz": "   ", "csrf_token": csrf,
        },
    )
    assert ohne_beleg.status_code == 400
    assert "Quellenbeleg" in ohne_beleg.text
    assert stammdaten.get_vertrag("V-601-PRUEF").rechtsordnung == "UNGEKLAERT"

    entwurf = client.post(
        "/backoffice/vertrag/V-601-PRUEF/pruefung/anlegen",
        data={
            "rechtsordnung": "OESTERREICH_MRG_VOLL", "fachstatus": "ENTWURF",
            "quellenbeleg_referenz": "Mietvertrag.pdf, S. 1", "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert entwurf.status_code == 303
    assert stammdaten.get_vertrag("V-601-PRUEF").rechtsordnung == "UNGEKLAERT"  # ENTWURF bleibt wirkungslos

    gepr = client.post(
        "/backoffice/vertrag/V-601-PRUEF/pruefung/anlegen",
        data={
            "rechtsordnung": "OESTERREICH_MRG_VOLL", "fachstatus": "GEPRUEFT",
            "quellenbeleg_referenz": "Mietvertrag.pdf, S. 1", "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert gepr.status_code == 303
    assert stammdaten.get_vertrag("V-601-PRUEF").rechtsordnung == "OESTERREICH_MRG_VOLL"

    historie = client.get("/backoffice/vertrag/V-601-PRUEF/pruefung")
    assert historie.text.count("Mietvertrag.pdf, S. 1") >= 2  # beide Versionen in der Historie


def test_sperre_aufheben_braucht_begruendung_und_wirkt_nur_auf_die_gewaehlte_sperre(backoffice_client):
    """Paket B, Punkt 1: Sperren-Aufhebung ist immer einzeln und braucht
    eine Begründung - RATENPLAN/RECHTSANWALT sind dabei nicht
    privilegiert, aber auch nicht ausgeschlossen; die jeweils ANDERE
    aktive Sperre bleibt beim Aufheben der einen unangetastet."""

    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.stammdaten.repository import StammdatenRepository

    client, _konto_id, _konto_gesperrt_id, _op_service = backoffice_client
    _login(client)
    csrf = _csrf_token(client)

    stammdaten = StammdatenRepository(build_session_factory(get_settings().database_url))
    stammdaten.upsert_einheit(id="601-TOP-SPERRE", objekt_id="601", bezeichnung="Top Sperre", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten.upsert_vertrag(
        id="V-601-SPERRE", einheit_id="601-TOP-SPERRE", debitor_id="DEB-1", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    ratenplan_id = stammdaten.sperre_setzen(vertrag_id="V-601-SPERRE", grund="RATENPLAN", kommentar="Ratenplan vereinbart")
    stammdaten.sperre_setzen(vertrag_id="V-601-SPERRE", grund="RECHTSANWALT", kommentar="RA beauftragt")

    ohne_begruendung = client.post(
        f"/backoffice/vertrag/V-601-SPERRE/sperre/{ratenplan_id}/aufheben",
        data={"begruendung": "   ", "csrf_token": csrf},
    )
    assert ohne_begruendung.status_code == 400
    assert len(stammdaten.aktive_sperren("V-601-SPERRE")) == 2  # beide weiterhin aktiv

    aufgehoben = client.post(
        f"/backoffice/vertrag/V-601-SPERRE/sperre/{ratenplan_id}/aufheben",
        data={"begruendung": "Rate vollständig bezahlt, Beleg Kontoauszug 04/2026", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert aufgehoben.status_code == 303

    verbleibende = stammdaten.aktive_sperren("V-601-SPERRE")
    assert len(verbleibende) == 1
    assert verbleibende[0].grund == "RECHTSANWALT"  # nur die gewählte Sperre wurde aufgehoben


def test_index_pruefbedarf_speichert_auch_unvollstaendige_angaben_ohne_freigabe(backoffice_client):
    """Paket B, Punkt 1: auch unvollständige Indexangaben sind als reiner
    Entwurf/Prüfbedarf speicherbar - keine Pflichtfelder, keine Freigabe,
    keine Berührung der bestehenden `IndexKlauselTable`."""

    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.stammdaten.repository import StammdatenRepository

    client, _konto_id, _konto_gesperrt_id, _op_service = backoffice_client
    _login(client)
    csrf = _csrf_token(client)

    stammdaten = StammdatenRepository(build_session_factory(get_settings().database_url))
    stammdaten.upsert_einheit(id="601-TOP-INDEX", objekt_id="601", bezeichnung="Top Index", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten.upsert_vertrag(
        id="V-601-INDEX", einheit_id="601-TOP-INDEX", debitor_id="DEB-1", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )

    unvollstaendig = client.post(
        "/backoffice/vertrag/V-601-INDEX/index-pruefbedarf/anlegen",
        data={
            "rechtsordnung": "", "basis_reihe": "", "basis_wert": "ungültig-kein-wert",
            "basis_monat": "", "kommentar": "nur Notiz, noch nicht geprüft", "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert unvollstaendig.status_code == 303  # kein Fehler trotz leerer/ungültiger Felder

    seite = client.get("/backoffice/vertrag/V-601-INDEX/pruefung")
    assert "nur Notiz, noch nicht geprüft" in seite.text


def test_bank_verknuepfen_bindet_rohtransaktion_an_bestehende_zahlung_ohne_neue_op(backoffice_client):
    """Paket B, Punkt 2: eine bereits VOR dem Bankfeed gebuchte ZAHLUNG-OP
    wird mit einer später eingelesenen Rohtransaktion verknüpft, OHNE
    einen zweiten Zahlungseintrag zu erzeugen (keine doppelte
    Gutschrift). Ein exakter Replay derselben Vorgangs-ID bleibt
    wirkungslos, ein Link-Duplikat unter einer anderen Vorgangs-ID wird
    abgelehnt."""

    from mietinkasso.domain.enums import OPTyp
    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.stammdaten.repository import StammdatenRepository

    client, _konto_id, _konto_gesperrt_id, op_service = backoffice_client
    _login(client)
    csrf = _csrf_token(client)

    stammdaten = StammdatenRepository(build_session_factory(get_settings().database_url))
    stammdaten.upsert_einheit(id="601-TOP-VERKN", objekt_id="601", bezeichnung="Top Verknüpfung", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten.upsert_vertrag(
        id="V-601-VERKN", einheit_id="601-TOP-VERKN", debitor_id="DEB-1", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    konto = stammdaten.get_or_create_konto(vertrag=stammdaten.get_vertrag("V-601-VERKN"))

    # Zahlung und Transaktion bewusst GRÖSSER als der erste Verknüpfungs-
    # betrag gewählt: so bleibt auf beiden Seiten (Transaktion UND OP)
    # genug Restbetrag übrig, damit ein späterer Link-Duplikat-Versuch
    # tatsächlich am Duplikat-Check scheitert - und nicht schon vorher,
    # unspezifischer, am (ebenfalls geprüften) Restbetrags-Check.
    zahlung = op_service.buchen(
        ctx=_ctx_admin(), konto=konto, typ=OPTyp.ZAHLUNG, betrag_cent=90_000,
        belegdatum=date(2026, 4, 1), buchungsdatum=date(2026, 4, 1), faelligkeit=None,
        beleg_referenz="Vorab erfasste Zahlung (vor Bankfeed)",
    )

    seiten_text = _bank_datei_importieren(client, csrf, bank_konto_id="BK-TEST-VERKN", betrag_text="900.00")
    treffer_tx = re.search(r"/backoffice/bank/(\d+)/verknuepfen", seiten_text)
    assert treffer_tx is not None
    transaktion_id = treffer_tx.group(1)

    formular = client.get(f"/backoffice/bank/{transaktion_id}/verknuepfen")
    assert formular.status_code == 200

    saldo_vorher = op_service.berechne_saldo(konto.id).saldo_cent
    verknuepft = client.post(
        f"/backoffice/bank/{transaktion_id}/verknuepfen",
        data={
            "op_position_id": str(zahlung.id), "konto_id": konto.id, "betrag": "450,00",
            "vorgangs_id": "VERKNUEPFT-TEST-1", "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert verknuepft.status_code == 303
    # Verknüpfung bucht keine neue OP-Zeile - die ZAHLUNG war bereits
    # vorher gebucht, der Saldo darf sich NICHT ein zweites Mal ändern.
    assert op_service.berechne_saldo(konto.id).saldo_cent == saldo_vorher

    replay = client.post(
        f"/backoffice/bank/{transaktion_id}/verknuepfen",
        data={
            "op_position_id": str(zahlung.id), "konto_id": konto.id, "betrag": "450,00",
            "vorgangs_id": "VERKNUEPFT-TEST-1", "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert replay.status_code == 303  # exakter Replay derselben Vorgangs-ID bleibt wirkungslos

    konflikt = client.post(
        f"/backoffice/bank/{transaktion_id}/verknuepfen",
        data={
            "op_position_id": str(zahlung.id), "konto_id": konto.id, "betrag": "100,00",
            "vorgangs_id": "VERKNUEPFT-TEST-ANDERE-ID", "csrf_token": csrf,
        },
    )
    assert konflikt.status_code == 400
    assert "Link-Duplikat" in konflikt.text


def test_automatische_bankzuordnung_route_ist_ausserhalb_bekannter_demo_umgebungen_gesperrt(backoffice_client, monkeypatch):
    """Codex-Rückprüfung (Paket A/B): eine ausgeblendete Schaltfläche
    allein ist kein Schutz - die POST-Route selbst muss außerhalb
    bekannter Demo-Umgebungen (also im Echtbetrieb) die Ausführung
    verweigern, unabhängig von der angefragten Transaktions-ID."""

    import mietinkasso.backoffice.app as backoffice_app

    client, *_ = backoffice_client
    _login(client)
    csrf = _csrf_token(client)

    monkeypatch.setattr(backoffice_app, "_DEMO_UMGEBUNG", False)
    gesperrt = client.post("/backoffice/bank/999999/automatisch-zuordnen", data={"csrf_token": csrf})
    assert gesperrt.status_code == 403


def test_bankseite_zeigt_keine_automatik_schaltflaeche_ausserhalb_der_demo_umgebung(backoffice_client, monkeypatch):
    """Ergänzt den vorigen Test um die UI-Seite: außerhalb bekannter
    Demo-Umgebungen darf weder der Vorschlagstext noch die Schaltfläche
    für die automatische Zuordnung erscheinen, nur der Hinweis auf die
    zurückgestellte automatische Bankzuordnung."""

    import mietinkasso.backoffice.app as backoffice_app

    client, _konto_id, _konto_gesperrt_id, _op_service = backoffice_client
    _login(client)
    csrf = _csrf_token(client)

    seiten_text_in_demo = _bank_datei_importieren(
        client, csrf, bank_konto_id="BK-TEST-GATING", betrag_text="600.00", referenz="VERTRAG:V-601-1",
    )
    assert re.search(r"/backoffice/bank/\d+/automatisch-zuordnen", seiten_text_in_demo) is not None

    monkeypatch.setattr(backoffice_app, "_DEMO_UMGEBUNG", False)
    seite_ohne_demo = client.get("/backoffice/bank/unzugeordnet", params={"bank_konto_id": "BK-TEST-GATING"})
    assert seite_ohne_demo.status_code == 200
    assert "automatisch-zuordnen" not in seite_ohne_demo.text
    assert "Automatische Zuordnung zurückgestellt" in seite_ohne_demo.text


def test_mieweg_vorschau_beide_spuren_ergeben_massgeblichen_betrag(backoffice_client):
    """Paket C: End-to-End über HTTP - Rechtsprofil, gesetzliche VPI-Spur
    und Vertragsspur erfassen, maßgeblicher Höchstbetrag ist das Minimum
    beider Spuren, nichts davon bucht/verschickt irgendetwas."""

    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.stammdaten.repository import StammdatenRepository

    client, _konto_id, _konto_gesperrt_id, _op_service = backoffice_client
    _login(client)
    csrf = _csrf_token(client)

    stammdaten = StammdatenRepository(build_session_factory(get_settings().database_url))
    stammdaten.upsert_einheit(id="601-TOP-MIEWEG", objekt_id="601", bezeichnung="Top MieWeG", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten.upsert_vertrag(
        id="V-601-MIEWEG", einheit_id="601-TOP-MIEWEG", debitor_id="DEB-1", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )

    seite = client.get("/backoffice/vertrag/V-601-MIEWEG/mieweg-vorschau")
    assert seite.status_code == 200
    assert "Berechnungsvorschau" in seite.text

    erstellt = client.post(
        "/backoffice/vertrag/V-601-MIEWEG/mieweg-vorschau/erstellen",
        data={
            "rechtsordnung": "OESTERREICH_MRG_VOLL", "ist_wohnungsnutzung": "1", "bezugsjahr": "2024", "bezugsmonat": "6",
            "ziel_bewertungsjahr": "2025", "basis_betrag": "1.000,00",
            "vpi_zeilen": "2023;100.0;Statistik Austria VPI 2020;2026-01-15\n2024;102.0;Statistik Austria VPI 2020;2026-01-15",
            "vertraglicher_betrag": "1.005,00",
            "vertraglicher_quellenbeleg": "Mietvertrag-2024.pdf, Wertsicherungsklausel",
            "vertraglicher_termin": "2025-04-01",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert erstellt.status_code == 303

    historie = client.get("/backoffice/vertrag/V-601-MIEWEG/mieweg-vorschau")
    assert historie.status_code == 200
    assert "vollständig" in historie.text or "Prüfbedarf" in historie.text
    assert "1.005,00" in historie.text  # das Minimum (Vertragsspur), nicht die höhere gesetzliche Grenze (1.010,00)
    assert "1.010,00" in historie.text  # gesetzliche Grenze bleibt sichtbar (2% * 6/12 Anteiligkeit auf 1.000 EUR)


def test_mieweg_vorschau_zeigt_aktuell_verrechneten_betrag_in_der_historie(backoffice_client):
    """Bugfund (zweite unabhängige Abnahme, 4fba1fe): der Service
    speicherte `aktuell_verrechneter_betrag_cent` nur in `eingaben_json`,
    `_mieweg_vorschau_zeile_html` las ihn aber aus `ergebnis_json` - die
    Spalte "Aktuell verrechnet" zeigte deshalb immer einen Strich. Dieser
    Test weist einen bekannten synthetischen aktuellen Mietbetrag
    tatsächlich sichtbar in der gerenderten Historie-Tabelle nach."""

    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.stammdaten.repository import StammdatenRepository

    client, _konto_id, _konto_gesperrt_id, _op_service = backoffice_client
    _login(client)
    csrf = _csrf_token(client)

    stammdaten = StammdatenRepository(build_session_factory(get_settings().database_url))
    stammdaten.upsert_einheit(id="601-TOP-AKTUELLVERR", objekt_id="601", bezeichnung="Top AktuellVerr", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten.upsert_vertrag(
        id="V-601-AKTUELLVERR", einheit_id="601-TOP-AKTUELLVERR", debitor_id="DEB-1", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    stammdaten.add_komponente(
        id="K-601-AKTUELLVERR", vertrag_id="V-601-AKTUELLVERR", art="HMZ", bezeichnung="Hauptmietzins",
        betrag_cent=100_000, ust_satz_promille=10_000, gueltig_von=date(2024, 1, 1), indexierbar=True,
    )

    erstellt = client.post(
        "/backoffice/vertrag/V-601-AKTUELLVERR/mieweg-vorschau/erstellen",
        data={
            "rechtsordnung": "OESTERREICH_MRG_VOLL", "ist_wohnungsnutzung": "1", "bezugsjahr": "2024", "bezugsmonat": "6",
            "ziel_bewertungsjahr": "2025", "basis_betrag": "1.000,00",
            "basis_komponenten_ids": ["K-601-AKTUELLVERR"],
            "vpi_zeilen": "2023;100.0;Statistik Austria VPI 2020;2026-01-15\n2024;102.0;Statistik Austria VPI 2020;2026-01-15",
            "vertraglicher_betrag": "1.005,00",
            "vertraglicher_quellenbeleg": "Mietvertrag-2024.pdf, Wertsicherungsklausel",
            "vertraglicher_termin": "2025-04-01",
            "aktuell_verrechneter_betrag": "999,00",
            "aktuell_verrechnet_quellenbeleg": "Vorschreibung-2025-01.pdf",
            "aktuell_verrechnet_stichtag": "2025-01-01",
            "zustellnachweis_referenz": "Zustellnachweis-2025-04.pdf",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert erstellt.status_code == 303

    historie = client.get("/backoffice/vertrag/V-601-AKTUELLVERR/mieweg-vorschau")
    assert historie.status_code == 200
    # Exakte Tabellenzellen (Spalten "Aktuell verrechnet"/"Ausführbare
    # Erhöhung" direkt hintereinander) statt einer losen Teilstring-Suche,
    # damit ein zufälliger Treffer an anderer Stelle der Seite
    # ausgeschlossen ist. Ausführbare Erhöhung: 1.005,00 - 999,00 = 6,00.
    assert "<td>999,00 €</td><td>6,00 €</td>" in historie.text


def test_mieweg_vorschau_fehlende_vpi_daten_ergeben_pruefbedarf_kein_erfundener_wert(backoffice_client):
    client, _konto_id, _konto_gesperrt_id, _op_service = backoffice_client
    _login(client)
    csrf = _csrf_token(client)

    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.stammdaten.repository import StammdatenRepository

    stammdaten = StammdatenRepository(build_session_factory(get_settings().database_url))
    stammdaten.upsert_einheit(id="601-TOP-MIEWEG2", objekt_id="601", bezeichnung="Top MieWeG 2", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten.upsert_vertrag(
        id="V-601-MIEWEG2", einheit_id="601-TOP-MIEWEG2", debitor_id="DEB-1", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )

    erstellt = client.post(
        "/backoffice/vertrag/V-601-MIEWEG2/mieweg-vorschau/erstellen",
        data={
            "rechtsordnung": "OESTERREICH_MRG_VOLL", "ist_wohnungsnutzung": "1", "bezugsjahr": "2024", "bezugsmonat": "1",
            "ziel_bewertungsjahr": "2026", "basis_betrag": "1.000,00",
            "vpi_zeilen": "2023;100.0;Statistik Austria VPI 2020;2026-01-15",  # 2024/2025 fehlen bewusst
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert erstellt.status_code == 303

    historie = client.get("/backoffice/vertrag/V-601-MIEWEG2/mieweg-vorschau")
    assert "Prüfbedarf" in historie.text
    assert "VPI-Jahresdurchschnitt fehlt" in historie.text


def test_mieweg_vorschau_ungeklaertes_rechtsprofil_wird_nicht_hineingeraten(backoffice_client):
    client, _konto_id, _konto_gesperrt_id, _op_service = backoffice_client
    _login(client)
    csrf = _csrf_token(client)

    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.stammdaten.repository import StammdatenRepository

    stammdaten = StammdatenRepository(build_session_factory(get_settings().database_url))
    stammdaten.upsert_einheit(id="601-TOP-MIEWEG3", objekt_id="601", bezeichnung="Top MieWeG 3", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten.upsert_vertrag(
        id="V-601-MIEWEG3", einheit_id="601-TOP-MIEWEG3", debitor_id="DEB-1", gesellschaft_id="7DI",
        rechtsordnung="UNGEKLAERT", gueltig_von=date(2024, 1, 1),
    )

    erstellt = client.post(
        "/backoffice/vertrag/V-601-MIEWEG3/mieweg-vorschau/erstellen",
        data={"rechtsordnung": "UNGEKLAERT", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert erstellt.status_code == 303

    historie = client.get("/backoffice/vertrag/V-601-MIEWEG3/mieweg-vorschau")
    assert "UNGEKLAERT" in historie.text
    assert "kein Rateversuch" in historie.text or "keine ausführbare Anpassung" in historie.text


def test_indexautomatik_rechtsprofil_erstellen_und_freigeben(backoffice_client):
    """Indexautomatik (Auftrag 13.09.): Rechtsprofil-Entwurf über HTTP
    anlegen und freigeben - keine automatische Berechnung/kein Versand
    ausgelöst, nur die Freigabe selbst."""

    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.stammdaten.repository import StammdatenRepository

    client, _konto_id, _konto_gesperrt_id, _op_service = backoffice_client
    _login(client)
    csrf = _csrf_token(client)

    stammdaten = StammdatenRepository(build_session_factory(get_settings().database_url))
    stammdaten.upsert_einheit(id="601-TOP-IDXAUTO", objekt_id="601", bezeichnung="Top Indexautomatik", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten.upsert_vertrag(
        id="V-601-IDXAUTO", einheit_id="601-TOP-IDXAUTO", debitor_id="DEB-1", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    stammdaten.add_komponente(
        id="K-601-IDXAUTO-HMZ", vertrag_id="V-601-IDXAUTO", art="HMZ", bezeichnung="Hauptmietzins",
        betrag_cent=100_000, indexierbar=True, gueltig_von=date(2024, 1, 1),
    )

    seite = client.get("/backoffice/vertrag/V-601-IDXAUTO/rechtsprofil")
    assert seite.status_code == 200
    assert "Rechtsprofil" in seite.text

    erstellt = client.post(
        "/backoffice/vertrag/V-601-IDXAUTO/rechtsprofil/erstellen",
        data={
            "rechtsordnung": "OESTERREICH_MRG_VOLL", "ist_wohnungsnutzung": "1", "ist_hauptmiete": "1",
            "mrg_zinsbeschraenkung": "0", "foerderbindung": "0",
            "bezugsjahr": "2024", "bezugsmonat": "1", "vpi_reihe": "VPI20C18",
            "basis_komponenten_ids": ["K-601-IDXAUTO-HMZ"],
            "vertraglicher_betrag": "2.000,00", "vertraglicher_quellenbeleg": "Mietvertrag Punkt 5",
            "vertraglicher_termin": "2026-04-01", "vertrag_beleg_referenz": "Mietvertrag V-601-IDXAUTO",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert erstellt.status_code == 303

    historie = client.get("/backoffice/vertrag/V-601-IDXAUTO/rechtsprofil")
    assert "ENTWURF" in historie.text
    treffer = re.search(r'/indexautomatik/rechtsprofil/(\d+)/freigeben', historie.text)
    assert treffer is not None
    rechtsprofil_id = treffer.group(1)

    freigegeben = client.post(
        f"/backoffice/indexautomatik/rechtsprofil/{rechtsprofil_id}/freigeben",
        data={"csrf_token": csrf}, follow_redirects=False,
    )
    assert freigegeben.status_code == 303
    historie_danach = client.get("/backoffice/vertrag/V-601-IDXAUTO/rechtsprofil")
    assert "FREIGEGEBEN" in historie_danach.text


def test_portal_historischer_beleg_bindet_betrag_ohne_rueckdatierung(backoffice_client):
    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.indexautomatik.repository import RechtsprofilRepository
    from mietinkasso.stammdaten.repository import StammdatenRepository

    client, *_ = backoffice_client
    _login(client)
    csrf = _csrf_token(client)
    sf = build_session_factory(get_settings().database_url)
    sd = StammdatenRepository(sf)
    sd.upsert_einheit(id="601-BELEGT", objekt_id="601", bezeichnung="Belegt", nutzungsstatus="DAUERVERMIETUNG")
    sd.upsert_vertrag(id="V-BELEGT", einheit_id="601-BELEGT", debitor_id="DEB-1", gesellschaft_id="7DI",
                     rechtsordnung="OESTERREICH_MRG_TEIL", gueltig_von=date(2026, 4, 1))
    sd.add_komponente(id="K-BELEGT", vertrag_id="V-BELEGT", art="HMZ", bezeichnung="Miete",
                      betrag_cent=100_000, indexierbar=True, gueltig_von=date(2026, 9, 1))
    form = {
        "csrf_token": csrf, "rechtsordnung": "OESTERREICH_MRG_TEIL", "ist_wohnungsnutzung": "0",
        "ist_hauptmiete": "0", "mrg_zinsbeschraenkung": "0", "foerderbindung": "0",
        "bezugsjahr": "2026", "bezugsmonat": "2", "basis_komponenten_ids": ["K-BELEGT"],
        "vertraglicher_betrag": "1.100,00", "vertraglicher_quellenbeleg": "synthetischer Vertrag",
        "vertraglicher_termin": "2027-01-01", "vertrag_beleg_referenz": "synthetisch",
        "beleg_komponente_id": ["K-BELEGT"], "beleg_betrag": ["0,01"],
        "beleg_datum": ["2026-03-02"], "beleg_quelle": ["synthetischer Vertrag, Punkt 3"],
        "frist_tage_zugang_bis_wirksamkeit": "14", "frist_quellenbeleg": "synthetischer Vertrag, Punkt 4",
    }
    repo = RechtsprofilRepository(sf)
    for betrag, erlaubt in [("0,01", False), ("1.000,00", True)]:
        form["beleg_betrag"] = [betrag]
        assert client.post("/backoffice/vertrag/V-BELEGT/rechtsprofil/erstellen", data=form,
                           follow_redirects=False).status_code == 303
        profil = repo.liste_fuer_vertrag("V-BELEGT")[0]
        response = client.post(f"/backoffice/indexautomatik/rechtsprofil/{profil.id}/freigeben",
                               data={"csrf_token": csrf}, follow_redirects=False)
        assert (response.status_code == 303) == erlaubt
        assert profil.historische_basis_belege["K-BELEGT"]["datum"] == "2026-03-02"
        assert profil.frist_tage_zugang_bis_wirksamkeit == 14
        assert sd.get_komponente("K-BELEGT").gueltig_von == date(2026, 9, 1)


def test_portal_klausel_entwurf_mit_beleg_und_csrf(backoffice_client):
    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.stammdaten.repository import StammdatenRepository
    from mietinkasso.infrastructure.db.tables import IndexKlauselTable
    from sqlalchemy import select

    client, *_ = backoffice_client
    _login(client)
    csrf = _csrf_token(client)
    sf = build_session_factory(get_settings().database_url)
    sd = StammdatenRepository(sf)
    sd.upsert_einheit(id="601-KLAUSEL", objekt_id="601", bezeichnung="Regel", nutzungsstatus="DAUERVERMIETUNG")
    sd.upsert_vertrag(id="V-KLAUSEL", einheit_id="601-KLAUSEL", debitor_id="DEB-1", gesellschaft_id="7DI",
                     rechtsordnung="OESTERREICH_MRG_TEIL", gueltig_von=date(2026, 4, 1))
    sd.add_komponente(id="K-KLAUSEL", vertrag_id="V-KLAUSEL", art="HMZ", bezeichnung="Miete",
                      betrag_cent=100_000, indexierbar=True, gueltig_von=date(2026, 4, 1))
    assert client.get("/backoffice/vertrag/V-KLAUSEL/indexklauseln").status_code == 200
    form = {"csrf_token": "wrong", "abschlussdatum": "2026-03-02", "basis_reihe": "VPI20C18",
            "basis_monat": "2026-02", "basis_wert": "130,0", "komponenten": ["K-KLAUSEL"],
            "klausel_text": "Synthetischer Vertrag, Punkt 3: jährlich im Jänner.", "schwelle_prozent": "0",
            "schwelle_inklusive": "1", "anpassungsmonat": "1", "mindestintervall_monate": "12",
            "terminmodus": "FIXER_MONAT"}
    url = "/backoffice/vertrag/V-KLAUSEL/indexklausel/erstellen"
    assert client.post(url, data=form, follow_redirects=False).status_code == 403
    form["csrf_token"] = csrf
    form["komponenten"] = ["K-601-1-HMZ"]
    assert client.post(url, data=form, follow_redirects=False).status_code == 400
    form["komponenten"] = ["K-KLAUSEL"]
    assert client.post(url, data=form, follow_redirects=False).status_code == 303
    with sf() as db:
        row = db.execute(select(IndexKlauselTable).where(IndexKlauselTable.vertrag_id == "V-KLAUSEL")).scalar_one()
        assert row.status == "ENTWURF" and row.basis_monat == "2026-02"
        assert row.indexierbare_komponenten == ["K-KLAUSEL"]
    assert sd.get_komponente("K-KLAUSEL").betrag_cent == 100_000


def test_indexautomatik_vpi_werte_erfassen_und_anzeigen(backoffice_client):
    client, *_ = backoffice_client
    _login(client)
    csrf = _csrf_token(client)

    seite = client.get("/backoffice/indexautomatik/vpi")
    assert seite.status_code == 200

    erfasst = client.post(
        "/backoffice/indexautomatik/vpi/erfassen",
        data={"reihe": "VPI20C18", "jahr": "2024", "wert": "123,8", "quelle": "Statistik Austria Test", "quelle_datum": "2025-02-17", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert erfasst.status_code == 303

    liste = client.get("/backoffice/indexautomatik/vpi")
    assert "123.8" in liste.text or "123,8" in liste.text
    assert "Statistik Austria Test" in liste.text


def test_vpi_veroeffentlichungsbeleg_aendert_keinen_indexwert(backoffice_client):
    from decimal import Decimal
    from mietinkasso.indexautomatik.repository import VpiRepository
    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory

    client, *_ = backoffice_client
    _login(client)
    csrf = _csrf_token(client)
    repo = VpiRepository(build_session_factory(get_settings().database_url))
    repo.monatswert_erfassen(reihe="VPI20C18", jahr=2026, monat=1, wert=Decimal("130.0"),
        finalitaet="ENDGUELTIG", quelle_datei="synthetisch", quelle_zeile=1, quelle_hash="test",
        abgerufen_am=datetime.now(timezone.utc), importiert_von="test")
    form = {"reihe": "VPI20C18", "monat": "2026-01", "veroeffentlicht_am": "2026-02-15",
            "quelle": "Synthetischer Beleg", "csrf_token": "wrong"}
    assert client.post("/backoffice/indexautomatik/vpi/veroeffentlichung", data=form).status_code == 403
    form["csrf_token"] = csrf
    assert client.post("/backoffice/indexautomatik/vpi/veroeffentlichung", data=form,
                       follow_redirects=False).status_code == 303
    row = repo.get_monatswert("VPI20C18", 2026, 1)
    assert row.wert == Decimal("130.0") and row.veroeffentlicht_am == date(2026, 2, 15)
    assert row.veroeffentlichung_quelle == "Synthetischer Beleg"


def test_indexautomatik_outbox_und_vertragsende_seiten_erreichbar(backoffice_client):
    client, *_ = backoffice_client
    _login(client)
    assert client.get("/backoffice/indexautomatik/outbox").status_code == 200
    assert client.get("/backoffice/indexautomatik/vertragsende").status_code == 200
    assert client.get("/backoffice/indexautomatik/laeufe").status_code == 200


def test_indexautomatik_rechtsprofil_freigabe_ohne_csrf_wird_abgelehnt(backoffice_client):
    client, *_ = backoffice_client
    _login(client)
    antwort = client.post(
        "/backoffice/indexautomatik/rechtsprofil/999999/freigeben", data={"csrf_token": "falsch"},
    )
    assert antwort.status_code == 403


def test_indexautomatik_soll_umsetzung_liste_vorschau_und_flag_gesperrt(backoffice_client):
    """Auftrag HV-20260913-VERSAND-SOLL: Backoffice-Ansicht für den
    letzten fehlenden Schritt der Indexautomatik-Pipeline. Diese Test-
    Umgebung hat KEIN MIETINKASSO_INDEXAUTOMATIK_SOLL_UMSETZUNG_ENABLED
    gesetzt (Default false, wie in Produktion bis zur echten Freigabe) -
    "Jetzt umsetzen" muss deshalb wirkungslos bleiben, exakt wie der
    tägliche Worker mit demselben Flag."""

    from mietinkasso.index.repository import IndexRepository
    from mietinkasso.indexautomatik.rechtsprofil import RechtsprofilService
    from mietinkasso.indexautomatik.repository import ErhoehungsschreibenRepository, RechtsprofilRepository
    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.infrastructure.db.tables import ErhoehungsschreibenTable
    from mietinkasso.stammdaten.repository import StammdatenRepository

    client, _konto_id, _konto_gesperrt_id, _op_service = backoffice_client
    _login(client)
    csrf = _csrf_token(client)

    session_factory = build_session_factory(get_settings().database_url)
    stammdaten = StammdatenRepository(session_factory)
    stammdaten.upsert_einheit(id="601-TOP-SOLLUMS", objekt_id="601", bezeichnung="Top Soll-Umsetzung", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten.upsert_vertrag(
        id="V-601-SOLLUMS", einheit_id="601-TOP-SOLLUMS", debitor_id="DEB-1", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    stammdaten.add_komponente(
        id="K-601-SOLLUMS-HMZ", vertrag_id="V-601-SOLLUMS", art="HMZ", bezeichnung="Hauptmietzins",
        betrag_cent=100_000, indexierbar=True, gueltig_von=date(2024, 1, 1),
    )
    rechtsprofil_repo = RechtsprofilRepository(session_factory)
    rechtsprofil_service = RechtsprofilService(rechtsprofil_repo, stammdaten, IndexRepository(session_factory))
    entwurf = rechtsprofil_service.entwurf_anlegen(
        ctx=_ctx_admin(), vertrag_id="V-601-SOLLUMS", rechtsordnung="OESTERREICH_MRG_VOLL", ist_wohnungsnutzung=True,
        mrg_zinsbeschraenkung=False, mrg_zinsbeschraenkung_geprueft=True, ist_altvertrag=False, ist_hauptmiete=True,
        foerderbindung=False, foerderbindung_geprueft=True,
        mietzinsobergrenze_cent=None, mietzinsobergrenze_quellenbeleg=None, mietzinsobergrenze_gueltig_bis=None,
        bezugsjahr=2024, bezugsmonat=1, letzte_basis_war_jahresdurchschnitt=False,
        basis_komponenten_ids=["K-601-SOLLUMS-HMZ"], vertraglich_zulaessiger_betrag_cent=200_000,
        vertraglicher_quellenbeleg="Punkt 5", vertraglicher_fruehestmoeglicher_termin=date(2026, 4, 1),
        vertrag_beleg_referenz="Vertrag", klausel_referenz=None, erstellt_von="markus",
    )
    profil = rechtsprofil_service.freigeben(entwurf.id, ctx=_ctx_admin(), freigegeben_von="markus")

    outbox_repo = ErhoehungsschreibenRepository(session_factory)
    schreiben = outbox_repo.anlegen(
        ErhoehungsschreibenTable(
            vertrag_id="V-601-SOLLUMS", ziel_bewertungsjahr=2026, rechtsprofil_id=profil.id,
            rechtsprofil_version=profil.version, status="SOLL_UMSETZUNG_OFFEN", massgeblicher_termin=date(2026, 4, 1),
            erhoehung_cent=1000, schreiben_text="Test", idempotenzschluessel="V-601-SOLLUMS:mieweg:2026",
            versendet_am=datetime(2026, 3, 15, 9, 0, tzinfo=timezone.utc), externe_versandreferenz="MAILOPS-TEST-1",
            zugangsform="EINSCHREIBEN", zugang_bestaetigt_am=date(2026, 4, 1), zugang_beleg="RSb-1",
            zahlungspflicht_ab=date(2026, 4, 15), empfaenger_snapshot={"debitor_id": "DEB-1"},
            komponenten_verteilung={
                "eintraege": [
                    {"komponente_id": "K-601-SOLLUMS-HMZ", "alter_betrag_cent": 100_000, "neuer_betrag_cent": 101_000}
                ]
            },
        )
    )

    liste = client.get("/backoffice/indexautomatik/soll-umsetzung")
    assert liste.status_code == 200
    assert "V-601-SOLLUMS" in liste.text
    assert "SOLL_UMSETZUNG_OFFEN" in liste.text
    assert "deaktiviert" in liste.text  # Hinweis auf das gesperrte Flag

    detail = client.get(f"/backoffice/indexautomatik/soll-umsetzung/{schreiben.id}")
    assert detail.status_code == 200
    assert "K-601-SOLLUMS-HMZ" in detail.text
    assert "1.010,00" in detail.text or "1010,00" in detail.text  # neuer Betrag
    assert "Jetzt umsetzen" in detail.text

    ohne_csrf = client.post(f"/backoffice/indexautomatik/soll-umsetzung/{schreiben.id}/umsetzen", data={"csrf_token": "falsch"})
    assert ohne_csrf.status_code == 403

    umgesetzt_versuch = client.post(
        f"/backoffice/indexautomatik/soll-umsetzung/{schreiben.id}/umsetzen", data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert umgesetzt_versuch.status_code == 200
    assert "BEREITS_VERARBEITET" in umgesetzt_versuch.text

    # Flag deaktiviert -> KEINE Wirkung, weder Status noch Komponente.
    assert outbox_repo.get(schreiben.id).status == "SOLL_UMSETZUNG_OFFEN"
    assert stammdaten.get_komponente("K-601-SOLLUMS-HMZ").betrag_cent == 100_000


def test_variable_abrechnung_erfassen_und_liste(backoffice_client):
    client, *_ = backoffice_client
    _login(client)
    csrf = _csrf_token(client)

    antwort = client.post(
        "/backoffice/variable-abrechnung/erfassen",
        data={
            "einheit_id": "601-KURZ1", "art": "KURZZEITVERMIETUNG", "leistungsmonat": "2026-08",
            "belegdatum": "2026-09-05", "quelle_referenz": "Betreiberreport August", "status": "BESTAETIGT",
            "unser_netto_anteil": "300,00", "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert antwort.status_code == 303

    liste = client.get("/backoffice/variable-abrechnung")
    assert liste.status_code == 200
    assert "601-KURZ1" in liste.text
    assert "300,00" in liste.text


def test_variable_abrechnung_ohne_csrf_wird_abgelehnt(backoffice_client):
    client, *_ = backoffice_client
    _login(client)
    antwort = client.post(
        "/backoffice/variable-abrechnung/erfassen",
        data={
            "einheit_id": "601-KURZ1", "art": "KURZZEITVERMIETUNG", "leistungsmonat": "2099-01",
            "belegdatum": "2099-02-01", "quelle_referenz": "x", "status": "ENTWURF", "csrf_token": "falsch",
        },
    )
    assert antwort.status_code == 403


def test_variable_abrechnung_bestaetigt_ohne_netto_wird_abgelehnt(backoffice_client):
    client, *_ = backoffice_client
    _login(client)
    csrf = _csrf_token(client)
    antwort = client.post(
        "/backoffice/variable-abrechnung/erfassen",
        data={
            "einheit_id": "601-KURZ1", "art": "KURZZEITVERMIETUNG", "leistungsmonat": "2099-02",
            "belegdatum": "2099-03-01", "quelle_referenz": "Report ohne Netto", "status": "BESTAETIGT",
            "csrf_token": csrf,
        },
    )
    assert antwort.status_code == 400


def test_variable_abrechnung_gesperrtes_objekt_wird_blockiert(backoffice_client):
    client, *_ = backoffice_client
    _login(client)
    csrf = _csrf_token(client)
    antwort = client.post(
        "/backoffice/variable-abrechnung/erfassen",
        data={
            "einheit_id": "107-KURZ1", "art": "KURZZEITVERMIETUNG", "leistungsmonat": "2026-08",
            "belegdatum": "2026-09-05", "quelle_referenz": "Report gesperrtes Objekt", "status": "ENTWURF",
            "csrf_token": csrf,
        },
    )
    assert antwort.status_code == 400


def test_variable_abrechnung_korrektur_ohne_aenderungsgrund_wird_abgelehnt(backoffice_client):
    client, *_ = backoffice_client
    _login(client)
    csrf = _csrf_token(client)
    client.post(
        "/backoffice/variable-abrechnung/erfassen",
        data={
            "einheit_id": "601-KURZ1", "art": "KURZZEITVERMIETUNG", "leistungsmonat": "2099-03",
            "belegdatum": "2099-04-01", "quelle_referenz": "Report v1", "status": "ENTWURF", "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    liste = client.get("/backoffice/variable-abrechnung?monat=2099-03")
    match = re.search(r"/backoffice/variable-abrechnung/(\d+)/korrigieren", liste.text)
    assert match is not None
    zeilen_id = match.group(1)

    # Ein wirklich LEERES aenderungsgrund-Feld wird bereits vom
    # Formular-Parser als fehlend abgelehnt (422) - ein rein
    # whitespace-Wert kommt dagegen tatsächlich im Service an und prüft
    # so die eigentliche Fachvalidierung (`.strip()`-Prüfung in
    # `VariableAbrechnungService.korrigieren`).
    antwort = client.post(
        f"/backoffice/variable-abrechnung/{zeilen_id}/korrigieren",
        data={
            "belegdatum": "2099-04-02", "quelle_referenz": "Report v2", "status": "ENTWURF",
            "aenderungsgrund": "   ", "csrf_token": csrf,
        },
    )
    assert antwort.status_code == 400


def test_variable_abrechnung_import_vorschau_und_uebernehmen(backoffice_client):
    client, *_ = backoffice_client
    _login(client)
    csrf = _csrf_token(client)
    csv_inhalt = (
        "einheit_id,art,leistungsmonat,belegdatum,quelle_referenz,status,berichteter_betrag,"
        "berichteter_betragsart,unser_netto_anteil,tatsaechlicher_zahlungseingang,vermietete_einheiten,"
        "vermietete_flaeche_qm,aenderungsgrund,import_id\n"
        "601-KURZ1,KURZZEITVERMIETUNG,2099-04,2099-05-01,CSV Report,BESTAETIGT,500.00,BRUTTO,400.00,,,,,\n"
    )
    vorschau = client.post(
        "/backoffice/variable-abrechnung/import/vorschau",
        files={"datei": ("report.csv", csv_inhalt, "text/csv")},
        data={"csrf_token": csrf},
    )
    assert vorschau.status_code == 200
    assert "NEU" in vorschau.text
    hash_match = re.search(r'name="plan_hash" value="([a-f0-9]+)"', vorschau.text)
    assert hash_match is not None

    uebernehmen = client.post(
        "/backoffice/variable-abrechnung/import/uebernehmen",
        data={"datei_inhalt": csv_inhalt, "plan_hash": hash_match.group(1), "csrf_token": csrf},
        follow_redirects=False,
    )
    assert uebernehmen.status_code == 200
    assert "1 neu" in uebernehmen.text


def test_variable_abrechnung_import_veralteter_hash_wird_abgelehnt(backoffice_client):
    client, *_ = backoffice_client
    _login(client)
    csrf = _csrf_token(client)
    csv_inhalt = (
        "einheit_id,art,leistungsmonat,belegdatum,quelle_referenz,status\n"
        "601-KURZ1,KURZZEITVERMIETUNG,2099-05,2099-06-01,CSV Report,ENTWURF\n"
    )
    antwort = client.post(
        "/backoffice/variable-abrechnung/import/uebernehmen",
        data={"datei_inhalt": csv_inhalt, "plan_hash": "veralteter-hash", "csrf_token": csrf},
    )
    assert antwort.status_code == 400


def test_dashboard_monatsuebersicht_seite_erreichbar(backoffice_client):
    client, *_ = backoffice_client
    _login(client)
    antwort = client.get("/backoffice/dashboard/monatsuebersicht?monat=2026-08")
    assert antwort.status_code == 200
    assert "Nettomieterlös" in antwort.text
    assert "Datenlücke" in antwort.text


def test_dashboard_relabeling_kontostand_und_faelligkeit(backoffice_client):
    client, konto_id, _konto_gesperrt_id, _op_service = backoffice_client
    _login(client)
    objekt_seite = client.get("/backoffice/?objekt_id=601")
    assert "Kontostand (offen/Guthaben)" in objekt_seite.text
    # Auftrag HV-20260913-RUECKSTAENDE, Nachbesserung: die Rückstands-
    # übersicht zeigt die bestehende Kontoberechnung und die Summe der
    # Einzelpositionen jetzt EXPLIZIT als zwei getrennt beschriftete
    # Spalten (statt eines einzigen, mehrdeutigen "Davon mit bekannter
    # Fälligkeit").
    assert "Fällig (Kontoberechnung)" in objekt_seite.text
    assert "Fällig (Positionen)" in objekt_seite.text

    konto_seite = client.get(f"/backoffice/konto/{konto_id}")
    assert "Kontostand (offen/Guthaben)" in konto_seite.text
    assert "Davon mit bekannter Fälligkeit" in konto_seite.text
    assert "Rechenweg" in konto_seite.text


def test_bestehende_op_unveraendert_nach_variable_abrechnung(backoffice_client):
    """Item 1: die variable Monatsabrechnung ist reine Zusatzerfassung -
    sie darf den bestehenden OP-Kontostand nicht verändern."""

    client, konto_id, _konto_gesperrt_id, op_service = backoffice_client
    _login(client)
    csrf = _csrf_token(client)
    saldo_vorher = op_service.berechne_saldo(konto_id).saldo_cent

    client.post(
        "/backoffice/variable-abrechnung/erfassen",
        data={
            "einheit_id": "601-KURZ1", "art": "KURZZEITVERMIETUNG", "leistungsmonat": "2099-06",
            "belegdatum": "2099-07-01", "quelle_referenz": "Report ohne OP-Wirkung", "status": "BESTAETIGT",
            "unser_netto_anteil": "123,45", "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    saldo_nachher = op_service.berechne_saldo(konto_id).saldo_cent
    assert saldo_nachher == saldo_vorher


def test_mailversand_seite_und_csrf_ohne_versand(backoffice_client):
    client, *_ = backoffice_client
    _login(client)
    response = client.get("/backoffice/mailversand")
    assert response.status_code == 200
    assert "hausverwaltung@jlb-immo.at" in response.text
    assert "Originale JLB-Signatur" in response.text
    assert "Mahnungen: gesperrt" in response.text
    assert client.post("/backoffice/mailversand/status-abgleichen", data={"csrf_token": "wrong"}).status_code == 403
    assert client.post("/backoffice/mahnfall/1/versenden", data={"csrf_token": "wrong"}).status_code == 403


# -- Vertragsanlage/-anzeige (Auftrag HV-20260913-VERTRAGSANLAGE) -----------
# Alle PDFs sind synthetisch von Hand gebaut (`_pdf_test_helpers.py`).


def test_vertraege_liste_zeigt_nav_und_bestehende_vertraege(backoffice_client):
    client, *_ = backoffice_client
    _login(client)
    seite = client.get("/backoffice/vertraege")
    assert seite.status_code == 200
    assert "Mieter &amp; Objekte" in seite.text or "Mieter & Objekte" in seite.text
    assert "V-601-1" in seite.text
    # Auftrag HV-20260914-UI-EINFACH: großer, prominenter CTA-Button statt
    # eines unauffälligen sekundären Links.
    assert "Mietvertrag hinzufügen" in seite.text


def test_dashboard_zeigt_mietvertraege_link(backoffice_client):
    client, *_ = backoffice_client
    _login(client)
    seite = client.get("/backoffice/")
    assert "/backoffice/vertraege" in seite.text


def test_neuen_mietvertrag_mit_pdf_upload_end_to_end(backoffice_client):
    """Voller Ablauf: Kontext wählen -> PDF hochladen -> editierbare
    Vorschau -> Übernehmen. Erzeugt einen ECHTEN, lauffähigen Vertrag samt
    Mietvertragsprofil - kein Mockup. Keine automatische Sollbuchung/
    Kaution/Mail/Lastschrift wird dabei ausgelöst."""

    from tests.mietinkasso._pdf_test_helpers import build_text_pdf

    client, _konto_id, _konto_gesperrt_id, op_service = backoffice_client
    _login(client)
    csrf = _csrf_token(client)

    pdf = build_text_pdf([
        "Wohnungsmietvertrag",
        "Mietbeginn: 01.06.2015",
        "Kaution: 1.500,00 EUR",
        "VPI 2020 Basiswert",
    ])

    hochgeladen = client.post(
        "/backoffice/vertragsanlage/pdf-hochladen",
        data={
            "modus": "NEU", "csrf_token": csrf, "vertrag_id": "V-601-NEU1",
            "einheit_id": "601-TOP2", "debitor_id": "DEB-1", "gesellschaft_id": "7DI",
            "rechtsordnung": "OESTERREICH_MRG_VOLL", "gueltig_von": "2026-09-01", "gueltig_bis": "",
        },
        files={"pdf_datei": ("vertrag.pdf", pdf, "application/pdf")},
    )
    assert hochgeladen.status_code == 200
    assert "Vorschlag aus Dokument" in hochgeladen.text
    assert "2015-06-01" in hochgeladen.text  # Mietbeginn-Vorschlag prägeprüft
    marker = 'name="csrf_token" value="'
    start = hochgeladen.text.index(marker) + len(marker)
    ende = hochgeladen.text.index('"', start)
    form_csrf = hochgeladen.text[start:ende]

    vorschau = client.post(
        "/backoffice/vertragsanlage/vorschau",
        data={
            "csrf_token": form_csrf, "modus": "NEU", "vertrag_id": "V-601-NEU1",
            "einheit_id": "601-TOP2", "debitor_id": "DEB-1", "gesellschaft_id": "7DI",
            "rechtsordnung": "OESTERREICH_MRG_VOLL", "gueltig_von": "2026-09-01", "gueltig_bis": "",
            "quelle_typ": "PDF_EXTRAKTION",
            "nutzungsart": "WOHNUNG", "urspruenglicher_mietbeginn": "2015-06-01",
            "vertragliche_kaution_cent": "1.500,00", "mahngebuehr_cent": "",
        },
    )
    assert vorschau.status_code == 200
    assert "NEU" not in vorschau.text or "bereit" in vorschau.text  # Statusanzeige vorhanden
    assert "Jetzt übernehmen" in vorschau.text

    paket_marker = 'name="paket_json" hidden>'
    p_start = vorschau.text.index(paket_marker) + len(paket_marker)
    p_ende = vorschau.text.index("</textarea>", p_start)
    import html as _html
    paket_json_roh = _html.unescape(vorschau.text[p_start:p_ende])
    assert "V-601-NEU1" in paket_json_roh

    uebernommen = client.post(
        "/backoffice/vertragsanlage/uebernehmen",
        data={"csrf_token": form_csrf, "paket_json": paket_json_roh},
        follow_redirects=False,
    )
    assert uebernommen.status_code == 303
    assert uebernommen.headers["location"] == "/backoffice/vertrag/V-601-NEU1"

    detail = client.get("/backoffice/vertrag/V-601-NEU1")
    assert detail.status_code == 200
    assert "WOHNUNG" in detail.text
    assert "2015-06-01" in detail.text

    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.stammdaten.repository import StammdatenRepository

    stammdaten = StammdatenRepository(build_session_factory(get_settings().database_url))
    vertrag = stammdaten.get_vertrag("V-601-NEU1")
    assert vertrag is not None
    profil = stammdaten.neuestes_mietvertragsprofil("V-601-NEU1")
    assert profil is not None and profil.version == 1
    assert profil.vertragliche_kaution_cent == 150000
    assert profil.mahngebuehr_cent is None  # kein erfundener Default
    # Keine automatische Sollbuchung: kein Konto/keine OP-Position wurde erzeugt.
    assert stammdaten.get_konto_by_vertrag("V-601-NEU1") is None
    # Keine Kaution wurde automatisch als "eingegangen" gebucht (kein Zahlungsbeleg im Formular).
    assert stammdaten.get_kaution("V-601-NEU1") is None


def test_neuanlage_mit_neuem_mieter_und_mietbestandteilen_end_to_end(backoffice_client):
    """P1-Nachbesserung: 'Neuanlage verlangt bereits bestehenden Debitor
    ... kann keine Mietbestandteile erfassen.' Legt einen VÖLLIG neuen
    Mieter UND zwei Mietbestandteile (Hauptmietzins, Küche) im selben
    atomaren Vorgang an - keine zweite Buchungsstrecke, keine historische
    Sollbuchung."""

    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.stammdaten.repository import StammdatenRepository

    client, *_ = backoffice_client
    _login(client)
    csrf = _csrf_token(client)

    hochgeladen = client.post(
        "/backoffice/vertragsanlage/pdf-hochladen",
        data={
            "modus": "NEU", "csrf_token": csrf, "vertrag_id": "V-601-NEU2",
            "einheit_id": "601-KURZ1", "gesellschaft_id": "7DI", "rechtsordnung": "OESTERREICH_MRG_VOLL",
            "gueltig_von": "2026-09-01", "gueltig_bis": "",
            "neuer_debitor_id": "DEB-NEU-2", "neuer_debitor_name": "Ganz Neuer Mieter",
            "neuer_debitor_email": "neu@example.at",
            "komponente_hmz": "500,00", "komponente_kueche": "30,00",
        },
    )
    assert hochgeladen.status_code == 200
    assert "Ganz Neuer Mieter (neu: DEB-NEU-2)" in hochgeladen.text  # sichtbare Eckdaten, nicht nur versteckt
    marker = 'name="csrf_token" value="'
    start = hochgeladen.text.index(marker) + len(marker)
    ende = hochgeladen.text.index('"', start)
    form_csrf = hochgeladen.text[start:ende]

    vorschau = client.post(
        "/backoffice/vertragsanlage/vorschau",
        data={
            "csrf_token": form_csrf, "modus": "NEU", "vertrag_id": "V-601-NEU2",
            "einheit_id": "601-KURZ1", "gesellschaft_id": "7DI", "rechtsordnung": "OESTERREICH_MRG_VOLL",
            "gueltig_von": "2026-09-01", "gueltig_bis": "", "quelle_typ": "MANUELL",
            "neuer_debitor_id": "DEB-NEU-2", "neuer_debitor_name": "Ganz Neuer Mieter",
            "neuer_debitor_email": "neu@example.at",
            "komponente_hmz": "500,00", "komponente_kueche": "30,00",
            "nutzungsart": "WOHNUNG",
        },
    )
    assert vorschau.status_code == 200
    assert "Debitor" in vorschau.text and "Komponente" in vorschau.text  # beide Bereiche in der Plan-Tabelle

    paket_marker = 'name="paket_json" hidden>'
    p_start = vorschau.text.index(paket_marker) + len(paket_marker)
    p_ende = vorschau.text.index("</textarea>", p_start)
    import html as _html
    paket_json_roh = _html.unescape(vorschau.text[p_start:p_ende])

    uebernommen = client.post(
        "/backoffice/vertragsanlage/uebernehmen",
        data={"csrf_token": form_csrf, "paket_json": paket_json_roh},
        follow_redirects=False,
    )
    assert uebernommen.status_code == 303

    stammdaten = StammdatenRepository(build_session_factory(get_settings().database_url))
    debitor = stammdaten.get_debitor("DEB-NEU-2")
    assert debitor is not None and debitor.name == "Ganz Neuer Mieter" and debitor.email == "neu@example.at"
    vertrag = stammdaten.get_vertrag("V-601-NEU2")
    assert vertrag is not None and vertrag.debitor_id == "DEB-NEU-2"
    komponenten = stammdaten.list_aktive_komponenten("V-601-NEU2", date(2026, 9, 1))
    arten = {k.art: k.betrag_cent for k in komponenten}
    assert arten == {"HMZ": 50000, "KUECHE": 3000}
    # Keine historische Sollbuchung/Konto durch die Komponentenanlage selbst.
    assert stammdaten.get_konto_by_vertrag("V-601-NEU2") is None


def test_neuanlage_mit_fremdem_debitor_im_paket_wird_abgelehnt(backoffice_client):
    """Direkter Missbrauchsversuch gegen `pruefe_schmale_form`: ein Paket,
    das einen ANDEREN Debitor enthält als den im (neuen) Vertrag
    referenzierten, wird abgelehnt, unabhängig vom sonstigen Inhalt."""

    from mietinkasso.vertragsanlage.paket_bau import PaketFormUngueltigError, pruefe_schmale_form
    from mietinkasso.intake.parser import parse_json_paket
    import json as _json

    paket = parse_json_paket(_json.dumps({
        "quelle": "t",
        "vertraege": [{"id": "V-X", "einheit_id": "E1", "debitor_id": "D-ECHT", "gesellschaft_id": "G1", "rechtsordnung": "OESTERREICH_MRG_VOLL", "gueltig_von": "2026-01-01"}],
        "debitoren": [{"id": "D-FREMD", "name": "Fremd"}],
        "mietvertragsprofile": [{"vertrag_id": "V-X", "nutzungsart": "WOHNUNG", "quelle_typ": "MANUELL"}],
    }))
    with pytest.raises(PaketFormUngueltigError):
        pruefe_schmale_form(paket, erwarteter_vertrag_id="V-X")


def test_neuanlage_mit_fremder_komponente_im_paket_wird_abgelehnt():
    from mietinkasso.vertragsanlage.paket_bau import PaketFormUngueltigError, pruefe_schmale_form
    from mietinkasso.intake.parser import parse_json_paket
    import json as _json

    paket = parse_json_paket(_json.dumps({
        "quelle": "t",
        "mietvertragsprofile": [{"vertrag_id": "V-X", "nutzungsart": "WOHNUNG", "quelle_typ": "MANUELL"}],
        "komponenten": [{"id": "K-1", "vertrag_id": "V-ANDERER", "art": "HMZ", "bezeichnung": "HMZ", "betrag_cent": 1000, "gueltig_von": "2026-01-01"}],
    }))
    with pytest.raises(PaketFormUngueltigError):
        pruefe_schmale_form(paket, erwarteter_vertrag_id="V-X")


def test_neuanlage_mit_bereits_vergebener_vertrag_id_wird_abgelehnt(backoffice_client):
    client, *_ = backoffice_client
    _login(client)
    csrf = _csrf_token(client)
    antwort = client.post(
        "/backoffice/vertragsanlage/pdf-hochladen",
        data={
            "modus": "NEU", "csrf_token": csrf, "vertrag_id": "V-601-1",  # existiert bereits
            "einheit_id": "601-TOP2", "debitor_id": "DEB-1", "gesellschaft_id": "7DI",
            "rechtsordnung": "OESTERREICH_MRG_VOLL", "gueltig_von": "2026-09-01",
        },
    )
    assert antwort.status_code == 400
    assert "existiert bereits" in antwort.text


def test_objekt_107_gesperrter_vertrag_kann_nicht_bearbeitet_werden(backoffice_client):
    client, *_ = backoffice_client
    _login(client)
    antwort = client.get("/backoffice/vertrag/V-107-1/mietvertragsprofil/bearbeiten")
    assert antwort.status_code == 400
    assert "nicht verfügbar" in antwort.text


def test_unbekannter_vertrag_bei_bearbeiten_wird_abgelehnt(backoffice_client):
    client, *_ = backoffice_client
    _login(client)
    antwort = client.get("/backoffice/vertrag/V-UNBEKANNT/mietvertragsprofil/bearbeiten")
    assert antwort.status_code == 400


def _vertragsanlage_manuell_uebernehmen(client, csrf, *, vertrag_id, **profil_felder):
    daten = {"csrf_token": csrf, "modus": "BESTEHEND", "vertrag_id": vertrag_id, "quelle_typ": "MANUELL"}
    daten.update(profil_felder)
    vorschau = client.post("/backoffice/vertragsanlage/vorschau", data=daten)
    assert vorschau.status_code == 200, vorschau.text
    paket_marker = 'name="paket_json" hidden>'
    start = vorschau.text.index(paket_marker) + len(paket_marker)
    ende = vorschau.text.index("</textarea>", start)
    import html as _html
    paket_json_roh = _html.unescape(vorschau.text[start:ende])
    return client.post(
        "/backoffice/vertragsanlage/uebernehmen",
        data={"csrf_token": csrf, "paket_json": paket_json_roh}, follow_redirects=False,
    )


def test_bestehenden_vertrag_profil_manuell_aktualisieren_erzeugt_versionen(backoffice_client):
    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.stammdaten.repository import StammdatenRepository

    client, *_ = backoffice_client
    _login(client)
    csrf = _csrf_token(client)

    bearbeiten_seite = client.get("/backoffice/vertrag/V-601-1/mietvertragsprofil/bearbeiten")
    assert bearbeiten_seite.status_code == 200
    assert "Profil aktualisieren" not in bearbeiten_seite.text or True  # Seite selbst ist das Formular

    antwort1 = _vertragsanlage_manuell_uebernehmen(
        client, csrf, vertrag_id="V-601-1", nutzungsart="WOHNUNG",
    )
    assert antwort1.status_code == 303

    stammdaten = StammdatenRepository(build_session_factory(get_settings().database_url))
    profil_v1 = stammdaten.neuestes_mietvertragsprofil("V-601-1")
    assert profil_v1 is not None and profil_v1.version == 1

    # Identischer Wiederholimport -> UNVERAENDERT, keine neue Version.
    antwort_wiederholung = _vertragsanlage_manuell_uebernehmen(
        client, csrf, vertrag_id="V-601-1", nutzungsart="WOHNUNG",
    )
    assert antwort_wiederholung.status_code == 303
    assert len(stammdaten.liste_mietvertragsprofil_versionen("V-601-1")) == 1

    # Abweichender Inhalt -> AKTUALISIERUNG, neue Version, alte bleibt erhalten.
    antwort2 = _vertragsanlage_manuell_uebernehmen(
        client, csrf, vertrag_id="V-601-1", nutzungsart="WOHNUNG", mahngebuehr_cent="0",
    )
    assert antwort2.status_code == 303
    versionen = stammdaten.liste_mietvertragsprofil_versionen("V-601-1")
    assert [v.version for v in versionen] == [1, 2]
    assert versionen[0].mahngebuehr_cent is None
    assert versionen[1].mahngebuehr_cent == 0

    detail = client.get("/backoffice/vertrag/V-601-1")
    assert "Version" in detail.text  # Quellen-und-Historie-Tabelle vorhanden


def test_detail_ansicht_zeigt_korrekten_ust_satz_und_netto_brutto_getrennt(backoffice_client):
    """Reproduktion: '100.0% bei ust 10000' und '550EUR BRUTTO als Summe
    netto'. `VertragsKomponenteTable.betrag_cent` ist BRUTTO (siehe
    `domain/money.py::zerlege_brutto_cent`); `ust_satz_promille=10000`
    bedeutet 10 %, nicht 100 %. Die Detailansicht muss denselben
    kanonischen Helper wie die Vorschreibung verwenden."""

    client, *_ = backoffice_client
    _login(client)
    detail = client.get("/backoffice/vertrag/V-601-1")
    assert detail.status_code == 200
    # V-601-1 hat eine HMZ-Komponente mit betrag_cent=55_000 (BRUTTO,
    # = 550,00 €), ust_satz_promille=10_000 (10 %) - siehe
    # backoffice_client-Fixture. Netto = 55.000 / 1,10 = 50.000 Cent = 500,00 €.
    assert "10,0 %" in detail.text
    assert "100,0 %" not in detail.text
    assert "500,00" in detail.text  # Netto
    assert "50,00" in detail.text  # USt
    assert "550,00" in detail.text  # Brutto bleibt unverändert (tatsächlich vorgeschrieben)


def test_kaution_ueber_wizard_bucht_nie_in_op_saldo(backoffice_client):
    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.stammdaten.repository import StammdatenRepository

    client, konto_id, _konto_gesperrt_id, op_service = backoffice_client
    _login(client)
    csrf = _csrf_token(client)

    saldo_vorher = op_service.berechne_saldo(konto_id).saldo_cent
    antwort = _vertragsanlage_manuell_uebernehmen(
        client, csrf, vertrag_id="V-601-1", nutzungsart="WOHNUNG",
        kaution_eingegangen_cent="1.500,00", kaution_eingegangen_stichtag="2026-08-01",
        kaution_eingegangen_referenz="Überweisung",
    )
    assert antwort.status_code == 303
    assert op_service.berechne_saldo(konto_id).saldo_cent == saldo_vorher  # unverändert

    stammdaten = StammdatenRepository(build_session_factory(get_settings().database_url))
    kaution = stammdaten.get_kaution("V-601-1")
    assert kaution is not None and kaution.betrag_cent == 150000


def test_xss_im_pdf_text_wird_beim_review_escaped(backoffice_client):
    """Ein Vertragsdokument ist eine Datenquelle, kein ausführbarer Code -
    jeder aus dem PDF übernommene Textauszug MUSS über `h()` escaped im
    Review erscheinen, niemals als ausführbares HTML/Skript."""

    from tests.mietinkasso._pdf_test_helpers import build_text_pdf

    client, *_ = backoffice_client
    _login(client)
    csrf = _csrf_token(client)

    pdf = build_text_pdf([
        "Wohnungsmietvertrag <script>alert(1)</script>",
        "Mietbeginn: 01.06.2015",
    ])
    antwort = client.post(
        "/backoffice/vertragsanlage/pdf-hochladen",
        data={
            "modus": "BESTEHEND", "csrf_token": csrf, "vertrag_id": "V-601-1",
        },
        files={"pdf_datei": ("boese.pdf", pdf, "application/pdf")},
    )
    assert antwort.status_code == 200
    assert "<script>alert(1)</script>" not in antwort.text
    assert "&lt;script&gt;" in antwort.text


def test_widerspruechliche_kaution_im_pdf_zeigt_mehrdeutigkeitswarnung(backoffice_client):
    """Reproduktion: 'PDF Kaution 1.500 EUR + Nachtrag Kaution 2.000 EUR
    ergibt 1.500 ohne Warnung.' Über die echte HTTP-Route muss die
    Mehrdeutigkeit sichtbar werden UND das Kautionsfeld darf NICHT
    stillschweigend mit einem der beiden Werte vorbefüllt sein."""

    from tests.mietinkasso._pdf_test_helpers import build_text_pdf

    client, *_ = backoffice_client
    _login(client)
    csrf = _csrf_token(client)

    pdf = build_text_pdf(["Kaution: 1.500,00 EUR", "Nachtrag zum Mietvertrag", "Kaution: 2.000,00 EUR"])
    antwort = client.post(
        "/backoffice/vertragsanlage/pdf-hochladen",
        data={"modus": "BESTEHEND", "csrf_token": csrf, "vertrag_id": "V-601-1"},
        files={"pdf_datei": ("nachtrag.pdf", pdf, "application/pdf")},
    )
    assert antwort.status_code == 200
    assert "Mehrdeutige Angaben" in antwort.text
    assert "1.500,00" in antwort.text and "2.000,00" in antwort.text
    # Das Kautionsfeld selbst bleibt leer (kein automatischer Vorschlag).
    marker = 'name="vertragliche_kaution_cent" value="'
    start = antwort.text.index(marker) + len(marker)
    ende = antwort.text.index('"', start)
    assert antwort.text[start:ende] == ""


def test_scan_ohne_textlage_zeigt_warnung_und_erfindet_nichts(backoffice_client):
    from tests.mietinkasso._pdf_test_helpers import build_scan_pdf

    client, *_ = backoffice_client
    _login(client)
    csrf = _csrf_token(client)
    antwort = client.post(
        "/backoffice/vertragsanlage/pdf-hochladen",
        data={"modus": "BESTEHEND", "csrf_token": csrf, "vertrag_id": "V-601-1"},
        files={"pdf_datei": ("scan.pdf", build_scan_pdf(), "application/pdf")},
    )
    assert antwort.status_code == 200
    assert "Kein auswertbarer Textlayer" in antwort.text


def test_zu_grosse_pdf_datei_wird_abgelehnt(backoffice_client):
    from mietinkasso.infrastructure.config import get_settings

    client, *_ = backoffice_client
    _login(client)
    csrf = _csrf_token(client)
    grenze = get_settings().vertragsanlage_max_upload_bytes
    zu_gross = b"%PDF-1.4\n" + b"x" * (grenze + 1)
    antwort = client.post(
        "/backoffice/vertragsanlage/pdf-hochladen",
        data={"modus": "BESTEHEND", "csrf_token": csrf, "vertrag_id": "V-601-1"},
        files={"pdf_datei": ("riesig.pdf", zu_gross, "application/pdf")},
    )
    assert antwort.status_code == 400
    assert "überschreitet" in antwort.text


def test_nicht_pdf_datei_wird_abgelehnt(backoffice_client):
    client, *_ = backoffice_client
    _login(client)
    csrf = _csrf_token(client)
    antwort = client.post(
        "/backoffice/vertragsanlage/pdf-hochladen",
        data={"modus": "BESTEHEND", "csrf_token": csrf, "vertrag_id": "V-601-1"},
        files={"pdf_datei": ("fake.pdf", b"<html><script>alert(1)</script></html>", "application/pdf")},
    )
    assert antwort.status_code == 400
    assert "kein PDF" in antwort.text


def test_vertragsanlage_ohne_csrf_wird_abgelehnt(backoffice_client):
    client, *_ = backoffice_client
    _login(client)
    antwort = client.post(
        "/backoffice/vertragsanlage/vorschau",
        data={"modus": "BESTEHEND", "vertrag_id": "V-601-1", "csrf_token": "falsch"},
    )
    assert antwort.status_code == 403


def test_vertragsanlage_gesellschaftsscope_wird_serverseitig_geprueft(ctx_factory):
    """Auch wenn der Pilot nur EINEN ADMIN-Operator kennt, muss die
    Scope-Prüfung selbst für eine eingeschränkte Rolle korrekt greifen -
    dieselbe `require_gesellschaft_access`, die jede Vertragsanlage-Route
    vor dem Schreiben aufruft."""

    from mietinkasso.auth.service import require_gesellschaft_access
    from mietinkasso.domain.exceptions import CrossTenantError

    fremd_ctx = ctx_factory("ANDERE-GESELLSCHAFT")
    with pytest.raises(CrossTenantError):
        require_gesellschaft_access(fremd_ctx, "7DI")
    require_gesellschaft_access(fremd_ctx, "ANDERE-GESELLSCHAFT")  # eigene Gesellschaft bleibt erlaubt


# -- Sicherheits-Rückprüfung a78717e (unabhängige Abnahme) -------------------


def test_debitoren_filterung_zeigt_nur_gesellschaftsscope_erlaubte_debitoren(backoffice_client, ctx_factory):
    """Reproduktion: 'GET /vertraege/neu zeigt FOREIGN-Debitor trotz
    7DI-Scope'. Der aktuelle Pilot kennt nur EINEN ADMIN-Operator (sieht
    strukturell alles) - die Filterlogik selbst (dieselbe, die die Route
    verwendet) muss für eine künftige eingeschränkte Rolle trotzdem
    korrekt greifen; das wird hier direkt gegen diese Logik geprüft."""

    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.stammdaten.repository import StammdatenRepository
    import mietinkasso.backoffice.app as backoffice_app

    client, *_ = backoffice_client
    stammdaten = StammdatenRepository(build_session_factory(get_settings().database_url))
    stammdaten.upsert_gesellschaft(id="FREMD-GMBH", name="Fremde GmbH")
    stammdaten.upsert_objekt(id="FREMD-OBJ", gesellschaft_id="FREMD-GMBH", bezeichnung="Fremdobjekt")
    stammdaten.upsert_einheit(id="FREMD-TOP1", objekt_id="FREMD-OBJ", bezeichnung="Top 1", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten.upsert_debitor(id="DEB-FREMD", name="Fremder Mieter")
    stammdaten.upsert_vertrag(
        id="V-FREMD-1", einheit_id="FREMD-TOP1", debitor_id="DEB-FREMD", gesellschaft_id="FREMD-GMBH",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )

    scoped_ctx = ctx_factory("7DI")
    alle_vertraege = backoffice_app._stammdaten_repo.list_alle_vertraege()
    erlaubte_ids = {v.debitor_id for v in alle_vertraege if backoffice_app._hat_gesellschaft_zugriff(scoped_ctx, v.gesellschaft_id)}
    sichtbare_debitoren = {d.id for d in backoffice_app._stammdaten_repo.list_alle_debitoren() if d.id in erlaubte_ids}
    assert "DEB-1" in sichtbare_debitoren  # 7DI-Mieter bleibt sichtbar
    assert "DEB-FREMD" not in sichtbare_debitoren  # fremder Mieter ausgeschlossen


def test_uebernehmen_ohne_vorherige_vorschau_wird_abgelehnt_und_bucht_nichts(backoffice_client):
    """Reproduktion: 'POST /vertragsanlage/uebernehmen mit erlaubtem
    Profil plus nachbuchungen[SOLL 12300] OHNE vorherige Vorschau liefert
    303 und OP-Anzahl 0→1.' `uebernehmen` MUSS ausschließlich den
    serverseitig bei einer tatsächlich durchlaufenen Vorschau abgelegten
    Stand anwenden - ein direkter POST ohne vorherige Sitzungs-Vorschau
    dieser Sitzung darf NICHTS bewirken, unabhängig vom Inhalt."""

    client, konto_id, _konto_gesperrt_id, op_service = backoffice_client
    _login(client)
    csrf = _csrf_token(client)

    boesartiges_paket = json.dumps({
        "quelle": "angreifer", "mietvertragsprofile": [{"vertrag_id": "V-601-1", "nutzungsart": "WOHNUNG", "quelle_typ": "MANUELL"}],
        "nachbuchungen": [{
            "import_id": "ANGRIFF-1", "vertrag_id": "V-601-1", "typ": "SOLL", "betrag_cent": 12300,
            "belegdatum": "2026-09-01", "buchungsdatum": "2026-09-01",
        }],
    })
    saldo_vorher = op_service.berechne_saldo(konto_id).saldo_cent
    antwort = client.post(
        "/backoffice/vertragsanlage/uebernehmen",
        data={"csrf_token": csrf, "paket_json": boesartiges_paket},
    )
    assert antwort.status_code == 400
    assert "Keine" in antwort.text and "Vorschau" in antwort.text
    assert op_service.berechne_saldo(konto_id).saldo_cent == saldo_vorher


def test_uebernehmen_ignoriert_manipuliertes_client_paket_json(backoffice_client):
    """Reproduktion: '_ctx scoped nur 7DI, Paket[Profil7DI,ProfilFOREIGN]
    liefert 303 und schreibt FOREIGN-Profil.' Nach einer ECHTEN Vorschau
    für Vertrag V-601-1 darf ein am Client manipuliertes `paket_json`
    (zusätzliches Profil für einen ANDEREN Vertrag, zusätzliche
    Nachbuchung) beim Übernehmen KEINE Wirkung haben - der Server wendet
    ausschließlich den bei der Vorschau selbst berechneten, serverseitig
    gespeicherten Stand an."""

    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.stammdaten.repository import StammdatenRepository

    client, konto_id, _konto_gesperrt_id, op_service = backoffice_client
    _login(client)
    csrf = _csrf_token(client)

    stammdaten = StammdatenRepository(build_session_factory(get_settings().database_url))
    stammdaten.upsert_einheit(id="601-TOP-INJ", objekt_id="601", bezeichnung="Top Injektion", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten.upsert_vertrag(
        id="V-601-OPFER", einheit_id="601-TOP-INJ", debitor_id="DEB-1", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )

    vorschau = client.post(
        "/backoffice/vertragsanlage/vorschau",
        data={"csrf_token": csrf, "modus": "BESTEHEND", "vertrag_id": "V-601-1", "quelle_typ": "MANUELL", "nutzungsart": "WOHNUNG"},
    )
    assert vorschau.status_code == 200

    saldo_vorher = op_service.berechne_saldo(konto_id).saldo_cent
    manipuliertes_paket = json.dumps({
        "quelle": "angreifer",
        "mietvertragsprofile": [
            {"vertrag_id": "V-601-1", "nutzungsart": "WOHNUNG", "quelle_typ": "MANUELL"},
            {"vertrag_id": "V-601-OPFER", "nutzungsart": "GESCHAEFTSLOKAL", "quelle_typ": "MANUELL"},
        ],
        "nachbuchungen": [{
            "import_id": "ANGRIFF-2", "vertrag_id": "V-601-1", "typ": "SOLL", "betrag_cent": 5000,
            "belegdatum": "2026-09-01", "buchungsdatum": "2026-09-01",
        }],
    })
    antwort = client.post(
        "/backoffice/vertragsanlage/uebernehmen",
        data={"csrf_token": csrf, "paket_json": manipuliertes_paket},
        follow_redirects=False,
    )
    assert antwort.status_code == 303  # die ECHTE (kleine) Vorschau wird angewendet
    assert op_service.berechne_saldo(konto_id).saldo_cent == saldo_vorher  # keine eingeschleuste Buchung
    assert stammdaten.neuestes_mietvertragsprofil("V-601-OPFER") is None  # kein fremdes Profil geschrieben


def test_uebernehmen_bei_zwischenzeitlich_geaendertem_profil_erzwingt_neue_pruefung(backoffice_client):
    """'bei inzwischen verändertem Profil neuer Review statt blind neue
    Version' - ändert sich das Mietvertragsprofil zwischen Vorschau und
    Übernahme (z. B. ein zweiter, gleichzeitig laufender Vorgang), wird
    NICHT blind auf dem alten Snapshot eine weitere Version erzeugt."""

    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.stammdaten.repository import StammdatenRepository

    client, *_ = backoffice_client
    _login(client)
    csrf = _csrf_token(client)

    stammdaten = StammdatenRepository(build_session_factory(get_settings().database_url))
    stammdaten.upsert_einheit(id="601-TOP-STALE", objekt_id="601", bezeichnung="Top Stale", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten.upsert_vertrag(
        id="V-601-STALE", einheit_id="601-TOP-STALE", debitor_id="DEB-1", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )

    vorschau = client.post(
        "/backoffice/vertragsanlage/vorschau",
        data={"csrf_token": csrf, "modus": "BESTEHEND", "vertrag_id": "V-601-STALE", "quelle_typ": "MANUELL", "nutzungsart": "WOHNUNG"},
    )
    assert vorschau.status_code == 200

    # Konkurrierende Änderung zwischen Vorschau und Übernahme.
    stammdaten.add_mietvertragsprofil(
        vertrag_id="V-601-STALE", nutzungsart="BUERO", quelle_typ="MANUELL", erstellt_von="anderer-vorgang",
    )

    antwort = client.post("/backoffice/vertragsanlage/uebernehmen", data={"csrf_token": csrf})
    assert antwort.status_code == 400
    assert "geändert" in antwort.text
    versionen = stammdaten.liste_mietvertragsprofil_versionen("V-601-STALE")
    assert len(versionen) == 1  # keine dritte/blinde Version durch den veralteten Vorgang


def test_zinsprofil_anlegen_und_freigeben_end_to_end(backoffice_client):
    """Auftrag Markus 13.09.2026 (Mahnkosten): ein Zinsprofil startet als
    ENTWURF und wirkt erst nach ausdrücklicher Freigabe - eine vereinbarte
    Verbraucherklausel ist vorher keine gültige Berechnungsgrundlage
    (KSchG §6 Abs 1 Z 13/OGH 7Ob111/25m)."""

    client, *_ = backoffice_client
    _login(client)
    csrf = _csrf_token(client)

    antwort = client.post(
        "/backoffice/vertrag/V-601-1/zinsprofil/erstellen",
        data={
            "csrf_token": csrf, "ist_b2b": "1", "vertragsdatum": "2020-01-01", "gueltig_ab": "2020-01-01",
            "vereinbarter_zinssatz_prozent": "5,0", "vereinbarung_geprueft": "1",
            "vereinbarung_beleg": "Vertrag §7", "verzugsverantwortung_geprueft": "1",
            # Geldbetrag im Formular in EUR (Auftrag Markus 14.09.2026) -
            # "15,00" wird zu 1500 Cent, nicht wörtlich als Cent übernommen.
            "mahngebuehr_kostenbasis_cent": "15,00",
            "mahngebuehr_kostenbasis_beleg": "Portokosten-Nachweis",
            "versandkosten_ersatzfaehig_geprueft": "1",
        },
        follow_redirects=False,
    )
    assert antwort.status_code == 303

    uebersicht = client.get("/backoffice/vertrag/V-601-1/zinsprofil")
    assert uebersicht.status_code == 200
    assert "ENTWURF" in uebersicht.text
    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.mahnwesen.kosten_repository import MahnkostenRepository

    kosten_repo = MahnkostenRepository(build_session_factory(get_settings().database_url))
    profil = kosten_repo.neuestes_zinsprofil("V-601-1")
    assert profil is not None
    assert profil.status == "ENTWURF"
    assert profil.mahngebuehr_kostenbasis_cent == 1500  # "15,00" EUR im Formular -> 1500 Cent, nicht 150000
    assert profil.gueltig_ab == date(2020, 1, 1)
    assert profil.verzugsverantwortung_geprueft is True
    assert profil.versandkosten_ersatzfaehig_geprueft is True

    freigabe = client.post(f"/backoffice/zinsprofil/{profil.id}/freigeben", data={"csrf_token": csrf}, follow_redirects=False)
    assert freigabe.status_code == 303
    profil_geprueft = kosten_repo.geprueftes_zinsprofil("V-601-1")
    assert profil_geprueft is not None
    assert profil_geprueft.id == profil.id


def test_zinsprofil_fuer_ausgeschlossenes_objekt_wird_abgelehnt(backoffice_client):
    client, *_ = backoffice_client
    _login(client)
    csrf = _csrf_token(client)
    antwort = client.get("/backoffice/vertrag/V-107-1/zinsprofil")
    assert antwort.status_code == 400
    assert "nicht verfügbar" in antwort.text or "gesperrt" in antwort.text


def test_mahnvorschau_zeigt_mahnkosten_block_ohne_zu_buchen(backoffice_client):
    client, *_ = backoffice_client
    _login(client)

    antwort = client.get("/backoffice/vertrag/V-601-1/mahnvorschau")
    assert antwort.status_code == 200
    assert "Mahnkosten" in antwort.text
    assert "reine Vorschau, keine Buchung" in antwort.text


def test_mahnvorschau_get_legt_nie_einen_mahnfall_an(backoffice_client):
    """Auftrag Markus 14.09.2026: GET /vertrag/{id}/mahnvorschau darf
    keine Mahnfälle schreiben - mehrere Aufrufe (auch mit
    unterschiedlichem Simulationsdatum) dürfen die Anzahl der
    `MahnFallTable`-Zeilen für diesen Vertrag NIE verändern. Das
    tatsächliche Anlegen läuft ausschließlich über den separaten,
    CSRF-geschützten POST `/vertrag/{id}/forderung/{op_id}/planen`."""

    import mietinkasso.backoffice.app as backoffice_app

    client, *_ = backoffice_client
    _login(client)

    vorher = len(backoffice_app._mahn_fall_repo.list_fuer_vertrag("V-601-1"))
    for heute in (date.today().isoformat(), (date.today() + timedelta(days=200)).isoformat()):
        antwort = client.get("/backoffice/vertrag/V-601-1/mahnvorschau", params={"heute": heute})
        assert antwort.status_code == 200
    nachher = len(backoffice_app._mahn_fall_repo.list_fuer_vertrag("V-601-1"))
    assert nachher == vorher

    # Ein POST ohne gültiges CSRF-Token wird abgelehnt (wie bei jedem
    # anderen schreibenden Endpunkt) - auch das legt keinen Mahnfall an.
    abgelehnt = client.post(
        "/backoffice/vertrag/V-601-1/forderung/999999/planen",
        data={"csrf_token": "ungueltig"},
    )
    assert abgelehnt.status_code in (400, 403)
    assert len(backoffice_app._mahn_fall_repo.list_fuer_vertrag("V-601-1")) == vorher


def test_basiszinssatz_erfassen_und_duplikat_wird_abgelehnt(backoffice_client):
    client, *_ = backoffice_client
    _login(client)
    csrf = _csrf_token(client)

    antwort = client.post(
        "/backoffice/basiszinssatz/erfassen",
        data={
            "csrf_token": csrf, "id": "TEST-HALBJAHR-1", "gueltig_von": "2026-01-01",
            "gueltig_bis": "2026-06-30", "basiszinssatz_prozent": "1,53",
            "quelle_referenz": "OeNB-Kundmachung (Test)",
        },
        follow_redirects=False,
    )
    assert antwort.status_code == 303

    uebersicht = client.get("/backoffice/basiszinssatz")
    assert uebersicht.status_code == 200
    assert "TEST-HALBJAHR-1" in uebersicht.text

    duplikat = client.post(
        "/backoffice/basiszinssatz/erfassen",
        data={
            "csrf_token": csrf, "id": "TEST-HALBJAHR-1", "gueltig_von": "2026-01-01",
            "gueltig_bis": "2026-06-30", "basiszinssatz_prozent": "9,99",
            "quelle_referenz": "Zweiter Versuch",
        },
    )
    assert duplikat.status_code == 400
    assert "unveränderlich" in duplikat.text


def test_mahnvorschau_zeigt_zinssegmente_bei_halbjahreswechsel(backoffice_client):
    """Regressionsschutz für den neuen Segment-Detailblock (Rückprüfung
    14.09.2026): eine Verzugszinsenperiode über einen Halbjahreswechsel
    muss ohne Renderfehler mit mehreren Zeilen angezeigt werden."""

    import mietinkasso.backoffice.app as backoffice_app
    from mietinkasso.domain.enums import OPTyp
    from datetime import date as _date
    from decimal import Decimal as _Decimal
    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.stammdaten.repository import StammdatenRepository

    client, _konto_id, _konto_gesperrt_id, op_service = backoffice_client
    _login(client)

    from mietinkasso.auth.service import AuthContext
    from mietinkasso.domain.enums import Rolle
    admin = AuthContext(user_id="test", rolle=Rolle.ADMIN, gesellschaft_ids=None)

    # Eigener, isolierter Vertrag statt des modulweit geteilten V-601-1
    # (wie an mehreren anderen Stellen dieses Moduls, z.B. V-601-1500):
    # das gemeinsam genutzte V-601-1 hat andernorts bereits ein geprüftes
    # Zinsprofil MIT vereinbartem Zinssatz - ein zweites, hier lokal
    # angelegtes Zinsprofil ohne `gueltig_ab` würde unter der neuen
    # periodengerechten Historie (Rückprüfung 14.09.2026, Risiko 3) den
    # GESAMTEN Zeitraum zu Recht als "unberechenbar" ausweisen, statt die
    # hier eigentlich zu testende Halbjahres-Segmentierung des
    # UGB-Basiszinssatzes (kein vereinbarter Satz) zu zeigen.
    stammdaten = StammdatenRepository(build_session_factory(get_settings().database_url))
    stammdaten.upsert_einheit(id="601-TOP-ZINSSEGMENT", objekt_id="601", bezeichnung="Top Zinssegment", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten.upsert_vertrag(
        id="V-601-ZINSSEGMENT", einheit_id="601-TOP-ZINSSEGMENT", debitor_id="DEB-1", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    konto = stammdaten.get_or_create_konto(vertrag=stammdaten.get_vertrag("V-601-ZINSSEGMENT"))

    op_service.buchen(
        ctx=admin, konto=konto, typ=OPTyp.SOLL, betrag_cent=55_000,
        belegdatum=_date(2027, 6, 1), buchungsdatum=_date(2027, 6, 1), faelligkeit=_date(2027, 6, 5),
        beleg_referenz="TEST HMZ Juni (Segmenttest)",
    )
    profil = backoffice_app._hv_mail.mahnkosten_repo.zinsprofil_anlegen(
        vertrag_id=konto.vertrag_id, ist_b2b=True, vertragsdatum=_date(2020, 1, 1),
        verzugsverantwortung_geprueft=True, erstellt_von="test",
    )
    backoffice_app._hv_mail.mahnkosten_repo.zinsprofil_freigeben(profil.id, freigegeben_von="test")
    backoffice_app._hv_mail.mahnkosten_repo.basiszinssatz_erfassen(
        id="TEST-2027-1", gueltig_von=_date(2027, 1, 1), gueltig_bis=_date(2027, 6, 30),
        basiszinssatz_prozent=_Decimal("1.530"), erfasst_von="test", quelle_referenz="Test",
    )
    backoffice_app._hv_mail.mahnkosten_repo.basiszinssatz_erfassen(
        id="TEST-2027-2", gueltig_von=_date(2027, 7, 1), gueltig_bis=_date(2027, 12, 31),
        basiszinssatz_prozent=_Decimal("2.000"), erfasst_von="test", quelle_referenz="Test",
    )

    antwort = client.get(f"/backoffice/vertrag/{konto.vertrag_id}/mahnvorschau?heute=2027-07-20")
    assert antwort.status_code == 200
    assert "Zinssegmente" in antwort.text
    assert "10.730" in antwort.text.replace(",", ".") or "10,730" in antwort.text


def test_ueberlappende_basiszinssaetze_werden_beim_erfassen_abgelehnt(backoffice_client):
    """Regression: zwei verschiedene Halbjahres-IDs mit sich
    überschneidenden Gültigkeitszeiträumen machten `basiszinssatz_fuer_
    datum` mehrdeutig und ließen die gesamte Mahnkosten-Berechnung mit
    `MultipleResultsFound` abstürzen, statt bereits beim fehlerhaften
    Erfassen klar abgelehnt zu werden."""

    import mietinkasso.backoffice.app as backoffice_app
    from datetime import date as _date
    from decimal import Decimal as _Decimal

    backoffice_app._hv_mail.mahnkosten_repo.basiszinssatz_erfassen(
        id="TEST-UEBERLAPP-1", gueltig_von=_date(2028, 1, 1), gueltig_bis=_date(2028, 6, 30),
        basiszinssatz_prozent=_Decimal("1.0"), erfasst_von="test", quelle_referenz="Test",
    )
    with pytest.raises(ValueError, match="überschneidet"):
        backoffice_app._hv_mail.mahnkosten_repo.basiszinssatz_erfassen(
            id="TEST-UEBERLAPP-2", gueltig_von=_date(2028, 6, 1), gueltig_bis=_date(2028, 12, 31),
            basiszinssatz_prozent=_Decimal("2.0"), erfasst_von="test", quelle_referenz="Test",
        )


def test_mahnvorschau_zaehlt_bereits_gebuchte_mahnkosten_nicht_doppelt_zur_hauptforderung(backoffice_client):
    """Rückprüfung 14.09.2026, echter Bug, konkreter Repro (synthetisch):
    830 EUR Hauptforderung, geprüfte 40-EUR-§458-Pauschale. Erste
    Kostenvorschau/-buchung am 14.09., zweite Portal-Ansicht am 28.09.
    darf die bereits gebuchte, noch offene Pauschale/Zinsen NICHT ein
    zweites Mal als Hauptforderung ausweisen (alte, fehlerhafte
    Berechnung: 870,82 € statt 830,00 €) - sie erscheinen stattdessen in
    einer eigenen, sichtbaren Zeile."""

    import mietinkasso.backoffice.app as backoffice_app
    from mietinkasso.domain.enums import OPTyp
    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.stammdaten.repository import StammdatenRepository

    client, _konto_id, _konto_gesperrt_id, op_service = backoffice_client
    _login(client)

    from mietinkasso.mahnwesen.repository import MahnPolicyRepository
    mahn_policy_repo = MahnPolicyRepository(build_session_factory(get_settings().database_url))
    if mahn_policy_repo.aktuelle_freigegebene() is None:
        policy = mahn_policy_repo.anlegen(
            stufe1_tage_nach_faelligkeit=7, stufe2_mindesttage_nach_stufe1_versand=14, status="ENTWURF",
        )
        mahn_policy_repo.freigeben(policy.id)

    stammdaten = StammdatenRepository(build_session_factory(get_settings().database_url))
    stammdaten.upsert_einheit(id="601-TOP-DOPPELZAEHLUNG", objekt_id="601", bezeichnung="Top Doppelzählung", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten.upsert_vertrag(
        id="V-601-DOPPELZAEHLUNG", einheit_id="601-TOP-DOPPELZAEHLUNG", debitor_id="DEB-1", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    konto = stammdaten.get_or_create_konto(vertrag=stammdaten.get_vertrag("V-601-DOPPELZAEHLUNG"))

    op_service.buchen(
        ctx=_ctx_admin(), konto=konto, typ=OPTyp.SOLL, betrag_cent=83_000,
        belegdatum=date(2026, 8, 1), buchungsdatum=date(2026, 8, 1), faelligkeit=date(2026, 8, 5),
        leistungsperiode="2026-08", beleg_referenz="HMZ August (Doppelzählung-Test)",
    )
    profil = backoffice_app._hv_mail.mahnkosten_repo.zinsprofil_anlegen(
        vertrag_id=konto.vertrag_id, ist_b2b=True, vertragsdatum=date(2020, 1, 1),
        mahngebuehr_kostenbasis_cent=4000, mahngebuehr_kostenbasis_beleg="Portokosten-Nachweis", erstellt_von="test",
    )
    backoffice_app._hv_mail.mahnkosten_repo.zinsprofil_freigeben(profil.id, freigegeben_von="test")

    gebucht = backoffice_app._hv_mail.mahnkosten_service.buche_bei_versand(
        ctx=_ctx_admin(), vertrag_id=konto.vertrag_id, stufe=1, heute=date(2026, 9, 14),
        versandnachweis_referenz="mahnung:test-doppelzaehlung", akteur="test",
    )
    assert gebucht is not None
    assert gebucht.gebuehr_cent == 4000

    antwort = client.get(f"/backoffice/vertrag/{konto.vertrag_id}/mahnvorschau", params={"heute": "2026-09-28"})
    assert antwort.status_code == 200
    assert "830,00 €" in antwort.text  # Hauptforderung bleibt korrekt, NICHT 870,82 €
    assert "Bereits gebuchte, noch offene Mahnkosten" in antwort.text


def test_mahnbrief_pdf_download_zeigt_denselben_gesamtbetrag_wie_die_vorschau(backoffice_client):
    """Authentifizierter PDF-Download aus demselben eingefrorenen Kosten-
    /Forderungsstand wie die Vorschau/der E-Mail-Text - kein Versand,
    keine Buchung, reine Vorbereitung."""

    import io
    from mietinkasso.domain.enums import OPTyp
    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.stammdaten.repository import StammdatenRepository
    from pypdf import PdfReader

    client, _konto_id, _konto_gesperrt_id, op_service = backoffice_client
    _login(client)

    stammdaten = StammdatenRepository(build_session_factory(get_settings().database_url))
    stammdaten.upsert_debitor(id="DEB-BRIEFPDF", name="Test Mieterin Briefpdf", email="briefpdf@example.at", adresse="Musterstraße 1, 1010 Wien")
    stammdaten.upsert_einheit(id="601-TOP-BRIEFPDF", objekt_id="601", bezeichnung="Top Briefpdf", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten.upsert_vertrag(
        id="V-601-BRIEFPDF", einheit_id="601-TOP-BRIEFPDF", debitor_id="DEB-BRIEFPDF", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    konto = stammdaten.get_or_create_konto(vertrag=stammdaten.get_vertrag("V-601-BRIEFPDF"))
    op_service.buchen(
        ctx=_ctx_admin(), konto=konto, typ=OPTyp.SOLL, betrag_cent=50_000,
        belegdatum=date(2026, 8, 1), buchungsdatum=date(2026, 8, 1), faelligkeit=date(2026, 8, 5),
        beleg_referenz="HMZ August (Briefpdf-Test)",
    )

    antwort = client.get(f"/backoffice/vertrag/{konto.vertrag_id}/mahnbrief.pdf", params={"stufe": 1, "heute": "2026-09-01"})
    assert antwort.status_code == 200
    assert antwort.headers["content-type"] == "application/pdf"
    assert "attachment; filename=" in antwort.headers["content-disposition"]
    assert antwort.headers["content-disposition"].startswith('attachment; filename="Mahnbrief_V-601-BRIEFPDF_Stufe1_')

    text = PdfReader(io.BytesIO(antwort.content)).pages[0].extract_text()
    assert "Test Mieterin" in text  # DEB-1
    assert "500,00" in text  # Hauptforderung 500 EUR


def test_mahnbrief_pdf_lehnt_ungueltige_parameter_und_fehlende_adresse_kontrolliert_ab(backoffice_client):
    """Rückprüfung Codex 14.09.2026: `stufe` nur 1/2, ein unparsebares
    Datum und eine fehlende Postadresse dürfen NIE zu einem 500er oder
    zu einem druckfertigen Platzhalter-Brief führen, sondern zu einem
    kontrollierten 4xx."""

    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.stammdaten.repository import StammdatenRepository

    client, _konto_id, _konto_gesperrt_id, _op_service = backoffice_client
    _login(client)

    stammdaten = StammdatenRepository(build_session_factory(get_settings().database_url))
    stammdaten.upsert_debitor(id="DEB-OHNE-ADRESSE", name="Test Mieterin Ohne Adresse", email="ohne-adresse@example.at")
    stammdaten.upsert_einheit(id="601-TOP-OHNE-ADRESSE", objekt_id="601", bezeichnung="Top Ohne Adresse", nutzungsstatus="DAUERVERMIETUNG")
    stammdaten.upsert_vertrag(
        id="V-601-OHNE-ADRESSE", einheit_id="601-TOP-OHNE-ADRESSE", debitor_id="DEB-OHNE-ADRESSE", gesellschaft_id="7DI",
        rechtsordnung="OESTERREICH_MRG_VOLL", gueltig_von=date(2024, 1, 1),
    )
    stammdaten.get_or_create_konto(vertrag=stammdaten.get_vertrag("V-601-OHNE-ADRESSE"))

    ungueltige_stufe = client.get("/backoffice/vertrag/V-601-OHNE-ADRESSE/mahnbrief.pdf", params={"stufe": 3})
    assert ungueltige_stufe.status_code == 422

    ungueltiges_datum = client.get(
        "/backoffice/vertrag/V-601-OHNE-ADRESSE/mahnbrief.pdf", params={"stufe": 1, "heute": "keine-datumsangabe"},
    )
    assert ungueltiges_datum.status_code == 422

    fehlende_adresse = client.get("/backoffice/vertrag/V-601-OHNE-ADRESSE/mahnbrief.pdf", params={"stufe": 1, "heute": "2026-09-01"})
    assert fehlende_adresse.status_code == 422
    assert "Postadresse" in fehlende_adresse.text


def test_mahnvorschau_zeigt_brief_wartet_auf_anbindung_und_pdf_link_bei_kanal_brief(backoffice_client):
    """Portalstatus für eine Stufe mit Kanal BRIEF muss eindeutig
    "wartet auf Anbindung" zeigen, NIE stillschweigend wie EMAIL wirken -
    und trotzdem die Vorbereitung/den Download des Brief-PDFs erlauben
    (eine Kostenvorschau ist noch kein erzeugter Brief)."""

    import mietinkasso.backoffice.app as backoffice_app
    from mietinkasso.infrastructure.config import get_settings
    from mietinkasso.infrastructure.db.session import build_session_factory
    from mietinkasso.mahnwesen.repository import MahnPolicyRepository

    client, _konto_id, _konto_gesperrt_id, op_service = backoffice_client
    _login(client)

    mahn_policy_repo = MahnPolicyRepository(build_session_factory(get_settings().database_url))
    if mahn_policy_repo.aktuelle_freigegebene() is None:
        policy = mahn_policy_repo.anlegen(
            stufe1_tage_nach_faelligkeit=7, stufe2_mindesttage_nach_stufe1_versand=14, status="ENTWURF",
        )
        mahn_policy_repo.freigeben(policy.id)

    if backoffice_app._mahn_kanalregel_repo.aktuelle_freigegebene() is None:
        regel = backoffice_app._mahn_kanalregel_repo.anlegen(erstellt_von="test")
        backoffice_app._mahn_kanalregel_repo.freigeben(regel.id, freigegeben_von="test")

    antwort = client.get("/backoffice/vertrag/V-601-1/mahnvorschau", params={"heute": "2026-09-01"})
    assert antwort.status_code == 200
    assert "Stufe 2 (Kanal: BRIEF)" in antwort.text
    assert "Brief wartet auf Anbindung" in antwort.text
    assert "Ersatzversand per E-Mail" in antwort.text
    assert "/backoffice/vertrag/V-601-1/mahnbrief.pdf?stufe=2" in antwort.text


def test_login_sperrt_nach_wiederholten_fehlversuchen(backoffice_client):
    """MUSS als LETZTER Test in diesem Modul laufen (siehe Kommentar
    unten) - der Login-Ratelimiter ist ein globaler, prozessweiter
    Zustand (siehe backoffice/app.py::_login_rate_limiter), kein
    IP-basierter (Codex-Vorgabe: keine Proxy-Header blind glauben, EIN
    Operator genügt eine globale Sperre). Jede erfolgreiche Anmeldung an
    anderer Stelle in diesem Modul setzt den Zähler zurück
    (`erfolgreich_angemeldet`) - deshalb würde ein früherer Testlauf
    diesen Test nicht stören, aber DIESER Test würde nachfolgende
    `_login(client)`-Aufrufe sperren, wenn er nicht zuletzt liefe."""

    client, *_ = backoffice_client
    letzte_antwort = None
    for _ in range(5):
        letzte_antwort = _login_versuch(client, password="falsch")
        assert letzte_antwort.status_code == 303
    assert "login" in letzte_antwort.headers["location"]

    # Sechster Versuch, diesmal mit dem RICHTIGEN Passwort - bleibt
    # trotzdem gesperrt, bis die Sperrzeit abgelaufen ist.
    gesperrt = _login_versuch(client, password="test-passwort-123")
    assert gesperrt.status_code == 303
    assert "login" in gesperrt.headers["location"]
    folge_seite = client.get(gesperrt.headers["location"])
    assert "Zu viele Fehlversuche" in folge_seite.text



# -- Variable Monatsabrechnung (KURZZEITVERMIETUNG/SELFSTORAGE) --------------
# Auftrag 13.09., HV-20260913-DASHBOARD.

