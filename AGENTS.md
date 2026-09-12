# AGENTS.md — Arbeitsregeln für dieses Repository

Dieses Repository enthält zwei unabhängige, absichtlich getrennte Codebasen:

- `src/invoice_automation/` — Kreditoren-/Eingangsrechnungs-Automatisierung
  (Migration von Zapier). Siehe `README.md`.
- `src/mietinkasso/` — Hausverwaltung & Mietinkasso (Auftrag
  `HV-20260911-MVP1`). Siehe `docs/hausverwaltung/RAHMENPROGRAMM.md`.

Die beiden Module importieren sich gegenseitig nicht. Wer an einem
Modul arbeitet, lässt das andere unangetastet, außer eine Änderung ist
ausdrücklich repository-weit (z. B. `pyproject.toml`, CI).

## Auftrag HV-20260911-MVP1 (Hausverwaltung & Mietinkasso)

### Scope

Geschütztes Online-Modul mit dauerhaftem, deterministischem Regelbetrieb
OHNE laufende KI-Aufrufe: Eröffnung/Salden, Debitoren/OP, Mietvorschreibung,
Bankabgleich, Index- und BK-Nachverrechnung, genau zwei automatische
Mahnstufen. Pilotobjekte: 601 Am Corso, 616 Fockygasse, 617 Primelweg,
619 Gutenberg. Objekt 107 (Sieben Dörfer) ist ausdrücklich ausgeschlossen.
Die vollständigen Fachregeln stehen in
`docs/hausverwaltung/RAHMENPROGRAMM.md` — dieses Dokument ist die
verbindliche Quelle, nicht diese Kurzfassung.

### Owner-Aufteilung

- **Markus Bauer** — Auftraggeber. Erteilt den Auftrag, entscheidet über
  Fachregeln, Freigaben und Produktivbetrieb. Trifft alle Entscheidungen,
  die über die hier beschriebenen Leitplanken hinausgehen.
- **Codex** — verantwortet Rahmen, Quellenmapping (reale Verwaltungsdaten
  außerhalb dieses Repos) und die unabhängige Code-Abnahme. Prüft die von
  Claude gelieferten Dateien gegenlesend in einem separaten lokalen Klon.
  Codex-Hinweise, die während einer laufenden Sitzung eintreffen, sind
  bindende Zwischen-Reviewpunkte, keine bloßen Vorschläge.
- **Claude** — Implementierer. Baut Code, Migration, Tests und Doku in
  `src/mietinkasso/` auf dem freigegebenen Aufgabenbranch, committet und
  pusht dorthin. Nimmt keine eigenständigen Fachentscheidungen zu
  Rechtsfragen (Index, BK, MRG-Einordnung) vor, sondern baut sie als
  versionierte, überprüfbare Regelprofile, die erst nach fachlicher
  Freigabe wirksam werden.

### Synthetic-only (für Claude weiterhin uneingeschränkt gültig)

- Es werden ausschließlich synthetische Testdaten verwendet
  (`scripts/seed_synthetic_data.py`, `src/mietinkasso/importtemplates/`).
- Keine echten Mieterdaten, keine echten Kontonummern/IBANs, keine
  Originaldokumente aus den lokalen Dropbox-/Windows-Pfaden werden in
  dieses Repository kopiert oder eingelesen. Falls solche Pfade in einer
  Sitzung erwähnt werden: Zugriff prüfen, niemals Inhalte übernehmen.
- Keine echten Mietermails, keine Produktivbuchungen, keine Bankaktionen
  durch Claude. Claude greift nie auf einen Server zu und deployt nie.

### Auftrag HV-20260912-ECHTBETRIEB — differenzierte Freigabe (löst die
### bisherige pauschale Synthetic-only/Kein-Deployment-Grenze für Codex ab)

Markus hat am 12.09.2026 ausdrücklich erlaubt, echte
Hausverwaltungs-Stammdaten/Salden zu übernehmen, zwei Mahnstufen
einzustellen und den geschützten Onlinebetrieb auf seinem bestehenden
Hetzner-Server (über die JLB-Webseite) einzurichten. Automatische
Bankabholung/-zuordnung bleiben ausdrücklich zurückgestellt bis zur
EBS/EBICS-Lösung. Vollständiger Wortlaut:
`docs/hausverwaltung/RAHMENPROGRAMM.md` (Abschnitt HV-20260912-ECHTBETRIEB).

Diese Freigabe ist **differenziert nach Owner**, nicht pauschal:

- **Codex** darf ab diesem Auftrag echte Daten mappen (privates
  Datenmapping außerhalb dieses Repos), unabhängig abnehmen und den
  kontrollierten Serverbetrieb auf der bestehenden Hetzner-Infrastruktur
  übernehmen. Für Codex ist die alte Synthetic-only/Kein-Deployment-Grenze
  durch diesen Auftrag abgelöst.
- **Claude bleibt bei synthetischen Testdaten** (siehe Abschnitt oben,
  unverändert gültig) und greift weiterhin nie auf einen Server zu,
  deployt nie und importiert nie echte Personen-, Bank-, Dokument- oder
  Secret-Daten in Git oder Cloud-Code. Claude baut die generische,
  wiederverwendbare Infrastruktur (Intake-Engine, Mahnstufen-Konfiguration,
  Deployment-Paket als Dateien/Anleitung) — Codex befüllt sie mit echten
  Daten und führt den Betrieb.
- Weiterhin gilt: keine tatsächliche Mietermail aus diesem
  Entwicklungsauftrag (`SEND_ENABLED=false` bleibt Standard), keine
  automatische Bankabholung/-zuordnung, kein Rechnungsmodul-Zugriff
  (`src/invoice_automation/` bleibt unangetastet), keine zusätzlichen
  kostenpflichtigen Dienste.

Konkrete Ergänzungen unter diesem Auftrag (Details in RAHMENPROGRAMM.md
und `docs/hausverwaltung/IMPORT_VERTRAG.md`):

1. Generischer, atomarer, idempotenter Intake für Gesellschaften/Objekte/
   Einheiten/Debitoren/Verträge sowie bestätigte Eröffnungssalden und
   separat datierte Nachbuchungen — Dry-run (Plan mit Quelle/Hash) und
   Apply (bindet sich an identischen Plan-Hash) getrennt, private DB
   außerhalb des Repos, keine Vermischung mit dem Demo-Seed.
2. Genau zwei konfigurierbare Mahnstufen, backoffice-sichtbar, ohne
   Zinsen/Gebühren, `SEND_ENABLED=false` unverändert Standard.
3. Minimales Deployment-Paket (Doku/Vorlagen) für Markus als EINEN
   Operator über HTTPS mit persistenten Daten, ohne Demo-Seed — Codex
   führt die tatsächliche Integration auf dem Server durch.

### Send-off / Übergabeprotokoll

Vor jeder Übergabe an Codex zur Abnahme:

1. `pytest` lokal grün (siehe `Makefile` bzw. `python -m pytest`).
2. Alle geänderten/neuen Dateien in der Antwort benannt.
3. Commit mit beschreibender Nachricht auf dem freigegebenen Aufgabenbranch
   erstellt und **nur dieser Branch** gepusht (kein Force-Push, kein Push
   auf `main`).
4. Offene Punkte (fehlende Integrationen, ungeprüfte Rechtsprofile,
   Platzhalter-Parser) explizit in
   `docs/hausverwaltung/OFFENE_PUNKTE.md` benannt, nicht stillschweigend
   weggelassen.
5. `SEND_ENABLED=false` (bzw. das Äquivalent für den jeweiligen
   Versandkanal) bleibt Standard, bis eine Person das Gegenteil
   ausdrücklich freigibt.

### Fachliche Leitplanken (Kurzfassung — Details in RAHMENPROGRAMM.md)

- Sollsalden und Habensalden werden je Debitor/Konto getrennt geführt.
  Die Nettosumme eines ganzen Objekts ist niemals gleichzusetzen mit der
  Summe mahnbarer Forderungen einzelner Mieter.
- Keine Namens- oder Nullsaldo-Heuristiken (z. B. "Soll ist 0, also
  Leerstand" oder "gleicher Name, also derselbe Mieter/dieselbe Zahlung").
  Zuordnungen laufen über explizite, eindeutige Kennungen.
- Technische Verrechnungskonten des Quellsystems (z. B. ein
  Leerstands-Sammelkonto) sind keine Debitoren/Mieter und dürfen nicht
  wie ein Mietkonto behandelt oder mit einem OP-Saldo bemahnt werden.
- Kaution ist strukturell getrennt vom OP-Saldo und wird nie automatisch
  gegen Mietrückstand verrechnet.
