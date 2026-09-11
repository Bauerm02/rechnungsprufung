# Betriebsanleitung — Mietinkasso-Modul (MVP1)

Diese Anleitung beschreibt den lokalen Entwicklungs-/Demobetrieb. Sie
ist KEINE Produktions-Deployment-Anleitung — dafür fehlen laut Auftrag
noch ein Mehrbenutzer-/Rollen-Login, ein echter Versandadapter und ein
produktives Datenbank-Setup (siehe `OFFENE_PUNKTE.md`). Seit dieser
Runde existiert ein bedienbares Backoffice (Abschnitt 5b) mit EINEM
lokalen Login für den Piloten.

## 1. Installation

```bash
python -m pip install -e .[dev]
```

Installiert beide Module (`invoice_automation`, `mietinkasso`) inkl.
Testabhängigkeiten. Python 3.11+ erforderlich.

## 2. Konfiguration

Alle Einstellungen kommen aus Umgebungsvariablen mit Prefix
`MIETINKASSO_` (siehe `src/mietinkasso/infrastructure/config.py`).
Wichtigste Variablen:

| Variable | Default | Bedeutung |
|---|---|---|
| `MIETINKASSO_DATABASE_URL` | `sqlite:///./data/mietinkasso.db` | SQLAlchemy-URL. SQLite für Dev/Test, jede von SQLAlchemy unterstützte DB (z. B. PostgreSQL) technisch möglich. |
| `MIETINKASSO_SEND_ENABLED` | `false` | Muss `false` bleiben, bis ein realer, geprüfter Versandadapter existiert. Solange `false`: Mahnwesen bleibt bei Preview/Outbox (Status `GEPLANT`), kein realer Versand. |
| `MIETINKASSO_BANK_STAND_MAX_AGE_DAYS` | `2` | Maximales Alter des letzten Bankimports, damit ein Mahnlauf überhaupt planen darf. |
| `MIETINKASSO_MAHN_STUFE1_TAGE_NACH_FAELLIGKEIT` | `7` | Vorschlagswert für die Mahnpolicy, Stufe 1. |
| `MIETINKASSO_MAHN_STUFE2_MINDESTTAGE_NACH_STUFE1` | `14` | Vorschlagswert für die Mahnpolicy, Stufe 2. |
| `MIETINKASSO_API_TOKEN` | *(leer)* | Ohne diesen Wert antworten die Datenendpunkte von `api/app.py` mit 503 ("closed by default"). Gesetzt, verlangen sie einen passenden `X-API-Key`-Header. Ein geteilter Operator-Token, KEINE Mandantentrennung pro Endanwender. |
| `MIETINKASSO_BACKOFFICE_USER` | `markus` | Login-Benutzername für das Backoffice (Abschnitt 5b). |
| `MIETINKASSO_BACKOFFICE_PASSWORD_HASH` | *(leer)* | Ohne diesen Wert bleibt das gesamte Backoffice geschlossen (503, "closed by default"). Erzeugen mit `python -m mietinkasso.backoffice.security <passwort>`; NIE das Klartext-Passwort hier ablegen. |

Es gibt bewusst **keine** KI-/LLM-Konfiguration (kein API-Key, kein
Modellname) — der laufende Betrieb braucht keinen.

## 3. Datenbank anlegen + synthetische Demodaten

```bash
export MIETINKASSO_DATABASE_URL="sqlite:///./data/mietinkasso_demo.db"
python scripts/seed_synthetic_data.py
```

Legt Schema an (`create_all_tables`) und befüllt es mit rein
synthetischen Gesellschaften/Objekten/Einheiten/Verträgen/Kautionen
für die vier Pilotobjekte plus das ausgeschlossene Objekt 107. Der
Aufruf ist wiederholbar (Restore-fähig): ein zweiter Lauf auf derselben
Datei überschreibt Stammdaten (upsert) und lässt bereits gebuchte
Ledger-Zeilen unverändert.

## 4. Tests

```bash
python -m pytest tests/mietinkasso -q      # nur Mietinkasso
python -m pytest -q                         # ganzes Repo (inkl. invoice_automation)
```

Stand: 105 Mietinkasso-Tests (inkl. Backoffice-Integrationstests gegen
eine echte Datei-SQLite-DB), 210 Tests gesamt, alle grün
(`python -m pytest -q`).

## 5. API/Dashboard lokal starten

```bash
export MIETINKASSO_DATABASE_URL="sqlite:///./data/mietinkasso_demo.db"
export MIETINKASSO_API_TOKEN="ein-lokales-dev-token"
uvicorn mietinkasso.api.app:app --reload --port 8001
```

- `http://localhost:8001/` — Status-Dashboard (Umgebung, `SEND_ENABLED`,
  Pilot-/Ausschlussliste). Offen, zeigt keine Kontodaten.
- `http://localhost:8001/health` — offen, keine Kontodaten.
- `http://localhost:8001/v1/konten/{konto_id}/op` — OP-Liste + Saldo
  eines Kontos (z. B. `KTO-V-601-1` nach dem Seed-Lauf). Verlangt Header
  `X-API-Key: ein-lokales-dev-token`; ohne `MIETINKASSO_API_TOKEN` liefert
  der Endpunkt 503.
- `http://localhost:8001/v1/mahnwesen/outbox/{vertrag_id}` — alle
  Mahnfälle zu einem Vertrag (eine Zeile je Forderung/Stufe). Ebenfalls
  Token-geschützt.
- `http://localhost:8001/docs` — automatische OpenAPI-Doku (FastAPI).

Dies ist bewusst read-only. Schreibende Vorgänge über HTTP laufen
ausschließlich über das Backoffice (5b), nie über einen offenen Endpunkt.

## 5b. Backoffice (bedienbare Oberfläche) lokal starten

```bash
export MIETINKASSO_DATABASE_URL="sqlite:///./data/mietinkasso_demo.db"
export MIETINKASSO_BACKOFFICE_USER="markus"
export MIETINKASSO_BACKOFFICE_PASSWORD_HASH="$(python -m mietinkasso.backoffice.security 'ein-lokales-dev-passwort')"
uvicorn mietinkasso.api.app:app --reload --host 127.0.0.1 --port 8001
```

Dann `http://127.0.0.1:8001/backoffice/login` im Browser öffnen
(**Loopback/127.0.0.1, nicht 0.0.0.0** — kein Mehrbenutzer-
Onlinebetrieb). Deckt die sechs beauftragten Arbeitsabläufe ab:

1. **Dashboard** (`/backoffice/`) — Objekt wählen, Mietkontenübersicht;
   Objekt 107 nur lesend, jede Schreibaktion dafür ist ausgegraut UND
   serverseitig durch `ObjektAusgeschlossenError` blockiert.
2. **Kontoauszug** (`/backoffice/konto/{konto_id}`) — Sollstellung/
   Zahlung/Korrektur/Storno mit Beleg/Fälligkeit, Vertrag/Debitor/
   Einheit getrennt ausgewiesen.
3. **Eröffnungssalden** (`/backoffice/eroeffnung`) — CSV hochladen,
   Vorschau (unklare/gesperrte/widersprüchliche Zeilen rot markiert),
   erst nach ausdrücklicher Bestätigung atomarer Import
   (`op/eroeffnung_import.py::importiere_eroeffnung_csv_atomar`) —
   serverseitig erneut validiert, nicht der Vorschau vertraut.
4. **Nachbuchung/Korrektur** — Formular je Konto (Nachbuchung) bzw. je
   OP-Zeile (Storno/Korrektur), mit Pflicht-Vorgangs-ID; ein doppelt
   abgeschicktes Formular mit derselben Vorgangs-ID bleibt ein
   sicherer No-Op.
5. **Bankimport** (`/backoffice/bank`) — CSV/CAMT hochladen, Vorschau
   mit Zuordnungsvorschlägen, danach Import; Zuordnung selbst ist ein
   separater, expliziter Schritt je Transaktion
   (`/backoffice/bank/unzugeordnet`) sowie eine eigene
   Bankvollständigkeits-Bestätigung (`/backoffice/bank/vollstaendigkeit`).
6. **Vorschreibungsentwurf** (`/backoffice/vertrag/{id}/vorschreibung`)
   — Netto/USt/Brutto je Bestandteil (HMZ/Küche/Stellplatz/BK/HK/
   Sonstige), wirksame Indexversion, Sperre für historische/leerstehende
   Fälle; Freigabe (Sollstellen) ist ein separater Schritt.
7. **Mahnvorschau** (`/backoffice/vertrag/{id}/mahnvorschau`) — reine
   Planungsansicht mit transparenten Sperrgründen (Bankvollständigkeit/
   ungeklärte Eingänge werden serverseitig aus der DB abgeleitet, nie
   aus einem Formularfeld); kein Button löst einen echten Mailversand
   aus.

Sicherheit: Session-Cookie trägt nur eine opake, zufällige ID
(`backoffice/security.py::SessionStore`) — Zustand lebt ausschließlich
im Server-Prozess (kein JWT, kein API-Token in HTML/URL/LocalStorage).
Jede POST-Route verlangt ein gültiges `csrf_token`-Feld gegen die
Session. Ohne `MIETINKASSO_BACKOFFICE_PASSWORD_HASH` bleibt das gesamte
Backoffice mit 503 geschlossen.

## 6. Ein Monatslauf von Hand (Beispiel, Python-Shell)

```python
from datetime import date
from mietinkasso.auth.service import AuthContext
from mietinkasso.domain.enums import Rolle
from mietinkasso.infrastructure.config import get_settings
from mietinkasso.infrastructure.db.session import build_session_factory
from mietinkasso.jobs.runner import JobRunner
from mietinkasso.op.repository import OPRepository
from mietinkasso.op.service import OPService
from mietinkasso.stammdaten.repository import StammdatenRepository
from mietinkasso.vorschreibung.repository import VorschreibungRepository
from mietinkasso.vorschreibung.service import VorschreibungService

settings = get_settings()
sf = build_session_factory(settings.database_url)
stammdaten = StammdatenRepository(sf)
op_service = OPService(OPRepository(sf), stammdaten)
vorschreibung_service = VorschreibungService(VorschreibungRepository(sf), stammdaten, op_service)
runner = JobRunner(sf)
ctx = AuthContext(user_id="ops", rolle=Rolle.BUCHHALTUNG, gesellschaft_ids=frozenset({"7DI"}))

vertrag = stammdaten.get_vertrag("V-601-1")
konto = stammdaten.get_or_create_konto(vertrag=vertrag)

# Idempotent über zwei Worker/Neustarts hinweg (Fachschlüssel = Vertrag+Monat):
runner.einmalig_ausfuehren(
    job_name="vorschreibung_monatslauf",
    fachschluessel=f"{vertrag.id}:2026-10",
    fn=lambda: {
        "entwurf": vorschreibung_service.entwurf_erstellen(ctx=ctx, vertrag=vertrag, monat="2026-10").__dict__,
        "sollstellung": vorschreibung_service.sollstellen(
            ctx=ctx, vertrag=vertrag, konto=konto, monat="2026-10", heute=date(2026, 10, 1)
        ).__dict__,
    },
)
print(op_service.berechne_saldo(konto.id))
```

## 7. Backup/Restore

SQLite-Dev-DB: Backup ist eine Dateikopie der `.db`-Datei; Restore ist
das Zurückspielen dieser Kopie. `create_all_tables` ist idempotent
(`CREATE TABLE IF NOT EXISTS` via SQLAlchemy `checkfirst=True`) und darf
gegen eine bereits bestehende, wiederhergestellte Datenbank erneut
laufen, ohne Daten zu verlieren oder Fehler zu werfen (siehe
`tests/mietinkasso/test_betrieb.py::test_restore_ist_wiederholbar`). Für
eine echte Serverdatenbank (PostgreSQL) gilt dasselbe Prinzip, aber mit
den dortigen Bordmitteln (`pg_dump`/`pg_restore`) statt Dateikopie —
das ist hier nicht weiter ausgebaut.

## 8. Wichtige Sicherheitsdefaults, die vor jedem Produktivschritt geprüft werden müssen

- `MIETINKASSO_SEND_ENABLED=false`
- Kein Löschen (`ENABLE_FILE_DELETES` o. ä. existiert hier absichtlich
  nicht — dieses Modul löscht nie, es storniert/korrigiert).
- Jede Schreiboperation verlangt einen `AuthContext` mit Zugriff auf die
  betroffene Gesellschaft (`auth/service.py`); es gibt keinen impliziten
  Vollzugriff außer für die Rolle `ADMIN`.
