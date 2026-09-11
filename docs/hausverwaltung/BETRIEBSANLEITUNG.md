# Betriebsanleitung — Mietinkasso-Modul (MVP1)

Diese Anleitung beschreibt den lokalen Entwicklungs-/Demobetrieb. Sie
ist KEINE Produktions-Deployment-Anleitung — dafür fehlen laut Auftrag
noch Auth/Login, Versandadapter und ein produktives Datenbank-Setup
(siehe `OFFENE_PUNKTE.md`).

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

Stand dieses Checkpoints: 42 Mietinkasso-Tests, 147 Tests gesamt, alle
grün (`python -m pytest -q`).

## 5. API/Dashboard lokal starten

```bash
export MIETINKASSO_DATABASE_URL="sqlite:///./data/mietinkasso_demo.db"
uvicorn mietinkasso.api.app:app --reload --port 8001
```

- `http://localhost:8001/` — Status-Dashboard (Umgebung, `SEND_ENABLED`,
  Pilot-/Ausschlussliste).
- `http://localhost:8001/health`
- `http://localhost:8001/v1/konten/{konto_id}/op` — OP-Liste + Saldo
  eines Kontos (z. B. `KTO-V-601-1` nach dem Seed-Lauf).
- `http://localhost:8001/v1/mahnwesen/outbox/{vertrag_id}` — letzter
  Mahnfall zu einem Vertrag.
- `http://localhost:8001/docs` — automatische OpenAPI-Doku (FastAPI).

Dies ist bewusst read-only. Schreibende Vorgänge (Eröffnung,
Vorschreibung, Bankimport, Mahnlauf) laufen über die Service-Klassen in
`op/`, `vorschreibung/`, `bank/`, `mahnwesen/` — angesteuert von einem
Skript, einem Scheduler/Worker-Prozess oder später einem
authentifizierten Backoffice, nicht von einem offenen HTTP-Endpunkt.

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
