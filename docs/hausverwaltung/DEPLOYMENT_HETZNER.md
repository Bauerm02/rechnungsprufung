# Deployment-Paket — Hausverwaltung & Mietinkasso auf dem bestehenden Hetzner-Server

Auftrag HV-20260912-ECHTBETRIEB, Punkt 3 (Produktionsstart). Dieses
Dokument ist ein Vorlagen-/Beispielpaket für Codex, das den EIGENEN
Vertragsteil ("Codex prüft vorhandene Serverinfrastruktur und übernimmt
Integration") ausfüllt — Claude greift nie auf den Server zu, deployt
nie, und dieses Repository enthält keine echten Zugangsdaten. Alle
Beispielwerte hier sind Platzhalter.

## Geltungsbereich und Grenzen

- **Ein Operator, ein Prozess:** Der Produktionsstart ist ausdrücklich
  für EINEN Operator (Markus) geplant, nicht für Mehrbenutzerbetrieb
  (siehe OFFENE_PUNKTE.md, "Kein Mehrbenutzer-Onlinebetrieb"). Der
  Session-Store (`backoffice/security.py`) ist ein reines In-Memory-Dict
  EINES Prozesses — **genau EIN Uvicorn-Worker**, kein `--workers N>1`,
  kein mehrfach gestarteter Prozess hinter demselben Socket. Ein
  Neustart des Prozesses meldet den Operator ab (akzeptabel für einen
  einzelnen Operator, siehe Backup/Restore unten).
- **Keine Demo-Daten in Produktion:** `scripts/seed_synthetic_data.py`
  darf NIEMALS gegen die Produktions-DB laufen. Der einzige Weg, echte
  Stammdaten/Salden einzuspielen, ist `scripts/intake_import.py` (siehe
  `IMPORT_VERTRAG.md`) gegen eine ausdrücklich außerhalb dieses Repos
  liegende Datenbank — das Skript verweigert von sich aus jeden Pfad
  innerhalb des Repos und jede `:memory:`-DB.
- **Eigener Zugang, keine Unterroute des CEO-Cockpits:** Die
  Hausverwaltung bekommt eine EIGENE Domain
  (`verwaltung.jlb-immo.at`, Beispielname) mit EIGENEM, von diesem
  Repository mitgelieferten Login — ausdrücklich NICHT als Unterroute
  unter dem bestehenden CEO-Cockpit. Der bestehende Cockpit-Login (lokaler
  bcrypt-Hash + signierte Host-Cookies, kein SSO-Provider) wird NICHT
  wiederverwendet/integriert — dieses Modul bringt sein eigenes,
  bereits vorhandenes Login mit (PBKDF2-Hash + opakes Session-Cookie,
  siehe `backoffice/security.py`), das strukturell gleichwertig
  "closed by default" ist (ohne konfigurierten Passwort-Hash bleibt das
  Backoffice mit 503 komplett geschlossen).
- **Kein neuer öffentlicher Port:** Läuft Caddy bereits im Hetzner-
  Container (mountet z. B. `/run/jlb-cockpit`), bindet dieser Prozess an
  einen **Unix-Domain-Socket** statt an einen TCP-Port — Caddy kann
  dann als Reverse Proxy auf denselben Host/dasselbe Netzwerk-Namespace
  zugreifen, ohne dass ein zusätzlicher öffentlicher Port geöffnet
  werden muss. Codex besitzt/konfiguriert Caddy und DNS; dieses
  Dokument liefert nur das Uvicorn-seitige Gegenstück (Bind-Adresse,
  systemd-Unit).

## 1. Code bereitstellen

```bash
# Auf dem Server, außerhalb dieses Git-Arbeitsverzeichnisses ODER als
# reiner Checkout ohne .env/Secrets darin:
git clone <repo-url> /opt/mietinkasso-hausverwaltung/app
cd /opt/mietinkasso-hausverwaltung/app
python3 -m venv /opt/mietinkasso-hausverwaltung/venv
/opt/mietinkasso-hausverwaltung/venv/bin/pip install -e .
```

`src/invoice_automation/` (das getrennte Rechnungsmodul, siehe
`Dockerfile`) ist von diesem Deployment nicht betroffen und läuft ggf.
als eigener, unabhängiger Prozess/Container weiter.

## 2. Persistente Datenbank AUSSERHALB des Repos

```bash
mkdir -p /var/lib/mietinkasso-hausverwaltung
# SQLite-Datei (Pilotgröße) ODER PostgreSQL - siehe env.production.example
```

Tabellen werden beim ersten Start automatisch angelegt
(`Base.metadata.create_all`, wie bereits von `scripts/intake_import.py`
und den bestehenden Tests genutzt) — kein separates Migrationswerkzeug
in MVP1.

## 3. Umgebungsvariablen AUSSERHALB von Git

Vorlage: `docs/hausverwaltung/deploy/env.production.example`. Die echte
Datei lebt z. B. unter `/etc/mietinkasso-hausverwaltung/env` mit
restriktiven Dateirechten (nur vom Service-User lesbar) und wird NIE
committet.

```bash
python -m mietinkasso.backoffice.security "<echtes Passwort>"
# -> Hash in MIETINKASSO_BACKOFFICE_PASSWORD_HASH eintragen, NIE das
#    Klartext-Passwort selbst ablegen.
```

## 4. Als Unix-Domain-Socket starten (kein neuer öffentlicher Port)

```bash
mkdir -p /run/mietinkasso-hausverwaltung
/opt/mietinkasso-hausverwaltung/venv/bin/uvicorn mietinkasso.api.app:app \
    --uds /run/mietinkasso-hausverwaltung/uvicorn.sock \
    --workers 1
```

`--workers 1` ist PFLICHT (siehe Einzelinstanz-Hinweis oben), nicht nur
ein Beispielwert. Ein systemd-Unit-Beispiel dafür liegt unter
`docs/hausverwaltung/deploy/mietinkasso-hausverwaltung.service.example`
(inkl. `EnvironmentFile=` außerhalb von Git, automatischem Neustart bei
Absturz, aber ohne den Prozess mehrfach gleichzeitig zu starten).

## 5. Caddy-Reverse-Proxy (Beispiel/Vorlage — Codex besitzt die reale Konfiguration)

```caddyfile
verwaltung.jlb-immo.at {
    reverse_proxy unix//run/mietinkasso-hausverwaltung/uvicorn.sock
}
```

Wichtig für die sicheren Cookies dieses Moduls
(`MIETINKASSO_BACKOFFICE_COOKIE_SECURE=true`, Produktionsdefault): das
Session-Cookie wird nur über HTTPS übertragen — Caddy muss also
tatsächlich TLS terminieren (Standardverhalten bei einer öffentlichen
Domain mit automatischem HTTPS), sonst meldet sich der Browser nie
erfolgreich an (siehe `env.production.example`, Kommentar bei
`MIETINKASSO_BACKOFFICE_COOKIE_SECURE`). Mit sicherem Cookie trägt es
zusätzlich das `__Host-`-Präfix (verlangt genau das: Secure, Path=/,
keine Domain — hier bereits erfüllt), was der Browser zusätzlich
gegen ein untergeschobenes Cookie von einer Subdomain absichert.

**Startvalidierung:** Bei `MIETINKASSO_ENVIRONMENT=production` prüft
der Prozess beim Start selbst (`infrastructure/config.py::pruefe_produktionskonfiguration`,
aufgerufen in `api/app.py`), dass `SEND_ENABLED=false`, ein
Passwort-Hash konfiguriert und Cookies sicher sind — fehlt eines davon,
startet der Prozess gar nicht erst (lauter Fehler statt eines still
laufenden, unsicher konfigurierten Prozesses).

Der Login begrenzt zusätzlich wiederholte Fehlversuche (kurzzeitige,
globale Sperre — bewusst nicht IP-basiert, siehe
`backoffice/security.py::LoginRateLimiter`) und prüft den
Origin-/Referer-Header gegen Login-CSRF.

## 6. Health-Check ohne Kundendaten

`GET /health` (siehe `api/app.py`) ist absichtlich ohne Auth erreichbar
und liefert ausschließlich `{"status": "ok", "environment": ...,
"send_enabled": ...}` — keine Konto-/Vertrags-/Personendaten. Geeignet
für einen externen Uptime-Check oder `systemd`'s eigenen Healthcheck,
ohne dass dafür ein API-Token verteilt werden muss.

## 7. Backup/Restore

**SQLite (Pilotgröße):**
```bash
# Backup (Prozess kurz stoppen ODER sqlite3 .backup für einen
# konsistenten Snapshot ohne Downtime):
sqlite3 /var/lib/mietinkasso-hausverwaltung/produktiv.db ".backup /pfad/backup/produktiv-$(date +%F).db"

# Restore: Prozess stoppen, Datei ersetzen, Prozess wieder starten.
systemctl stop mietinkasso-hausverwaltung
cp /pfad/backup/produktiv-2026-09-01.db /var/lib/mietinkasso-hausverwaltung/produktiv.db
systemctl start mietinkasso-hausverwaltung
```

**PostgreSQL (falls stattdessen gewählt):** Standard `pg_dump`/
`pg_restore` gegen die in `MIETINKASSO_DATABASE_URL` konfigurierte DB —
keine mietinkasso-spezifische Logik nötig, da SQLAlchemy DB-agnostisch
arbeitet (siehe OFFENE_PUNKTE.md).

## 8. Einzelinstanz-/Transaktionsschutz (bereits vorhandene Grundlage)

Alle mehrzeiligen Importe/Buchungen dieses Moduls laufen bereits als
EINE DB-Transaktion (Eröffnungsimport, Bankimport, Echtbetrieb-Intake —
siehe `IMPORT_VERTRAG.md`, "Ein Fehler ⇒ gesamter Lauf unverändert") und
Idempotenz läuft über DB-Unique-Constraints
(`op_positionen.import_id`, `vorschreibungen(vertrag_id, monat)`,
`mahn_faelle.outbox_key`), nicht über In-Prozess-Sperren — das bleibt
auch bei einem Prozess-Neustart mitten in einer Transaktion korrekt
(SQLite/PostgreSQL rollen eine unvollständige Transaktion beim Absturz
automatisch zurück). Der EINE-Worker-Constraint betrifft ausschließlich
den In-Memory-Session-Store des Logins, nicht die Buchungslogik.

## 9. Was dieses Dokument NICHT abdeckt (Codex-Aufgabe)

- Tatsächliche Caddy-/DNS-Konfiguration und TLS-Zertifikatsverwaltung
  auf dem realen Server.
- Firewall-/Netzwerk-Härtung des Hetzner-Containers insgesamt.
- Monitoring/Alerting über den bloßen `/health`-Endpunkt hinaus.
- Das reale Datenmapping/den ersten `intake_import.py apply`-Lauf mit
  echten Stammdaten (siehe `IMPORT_VERTRAG.md`).
