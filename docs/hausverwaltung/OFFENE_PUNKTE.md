# Offene Punkte — Mietinkasso-Modul

Stand: lokaler synthetischer Backoffice-Pilot, 11.09.2026; keine Produktionsfreigabe.
Alles hier ist bewusst offen gelassen bzw. bewusst konservativ gebaut —
keine stillschweigend übersprungenen Punkte.

## Paket A — produktionsgeeigneter Start (Nutzerauftrag 12.09., Nachmittag)

Codex ist für den geschützten Onlinebetrieb auf dem bestehenden
Hetzner-Server ausdrücklich autorisiert; dieser Abschnitt listet, was
Paket A liefert und WAS ES BEWUSST NICHT TUT (Risiken für die
Rückprüfung):

- **Kein Auto-Deploy, kein Serverzugriff durch Claude:** alle
  Artefakte (`Dockerfile.mietinkasso`, `scripts/backup_sqlite.py`,
  Timer-/Service-Vorlagen) sind Vorlagen/Code - niemand hat sie gegen
  den echten Hetzner-Server ausgeführt. Codex muss den ersten
  `backup_sqlite.py`-Lauf, den ersten Container-/systemd-Start und die
  Caddy-Anbindung real verifizieren.
- **Backup deckt nur Datei-SQLite ab:** `infrastructure/backup.py`
  lehnt PostgreSQL/`:memory:` bewusst ab (eigenes, DB-seitiges
  Verfahren nötig, z. B. `pg_dump`). Falls Codex PostgreSQL statt
  SQLite einsetzt, ist der neue Backupjob NICHT anwendbar und muss
  ersetzt werden.
- **Backup-Wiederherstellungsprobe prüft Lesbarkeit, nicht
  Anwendungs-Constraints:** `PRAGMA integrity_check` +
  "sqlite_master lesbar" beweisen eine strukturell intakte SQLite-
  Datei, aber KEINE vollständige Restore-Drill-Automatisierung (echtes
  Wiedereinspielen in einen laufenden Prozess, Vergleich der
  Zeilenzahlen o. Ä.) - das bleibt ein manueller/Codex-seitiger Schritt
  vor dem ersten produktiven Ernstfall.
- **Genau ein Backupjob/Timer wie beauftragt** - keine automatische
  Offsite-Replikation, keine Verschlüsselung der Backup-Dateien selbst
  (Dateisystemrechte/Backup-Zielspeicher müssen das absichern).
- **`/ready` bestätigt nur DB-Erreichbarkeit**, keine
  Anwendungs-Vollständigkeit (z. B. fehlende Tabellen nach einem
  abgebrochenen Migrationslauf würden `/ready` nicht zwingend als
  Fehler zeigen, solange `SELECT 1` funktioniert).
- **Betriebsmodus-Banner ist eine reine Anzeigeentscheidung**
  (`backoffice/views.py::betriebsmodus_banner`, gesteuert über die
  bereits vorhandene `MIETINKASSO_ENVIRONMENT`-Variable) - er
  aktiviert/entsperrt NICHTS automatisch. Ein falsch gesetztes
  `MIETINKASSO_ENVIRONMENT` (z. B. `production`, obwohl noch
  Demo-Daten in der DB stehen) zeigt einen falschen Banner, ändert aber
  keine Fachlogik.
- **Dockerfile ist ungetestet gegen den echten Hetzner-Container:**
  lokal nur auf Korrektheit der `pip install .`-Abhängigkeiten und
  Struktur geprüft (kein Docker-Daemon in dieser Sitzung verfügbar) -
  Codex muss den Build/Start real verifizieren, bevor er produktiv
  läuft.
- **Bestehende Konfiguration bleibt kompatibel:** keine neue Pflicht-
  Umgebungsvariable, keine geänderten Defaults in
  `infrastructure/config.py` - alle bestehenden Dev-/Test-/Demo-Abläufe
  (`Betriebsanleitung.md`) funktionieren unverändert.

## Paket B — Vertragsprüfung, Sperren-Aufhebung, manueller Bankabgleich (Nutzerauftrag 12.09., Anschluss an Paket A)

Umfasst NUR die drei explizit beauftragten Punkte; das vollständige
MieWeG-Profil und die Mail-/Jobpipeline sind ausdrücklich NICHT Teil
davon (folgen erst nach Rückmeldung des Nutzers).

- **Vertragsprüfseite (`/backoffice/vertrag/{id}/pruefung`):** jede
  Prüfung ist eine neue, unveränderliche, versionierte Zeile
  (`VertragPruefungTable`, `UniqueConstraint(vertrag_id, version)`) mit
  Pflicht-Quellenbeleg-Referenz (`QuellenbelegFehltError` ohne Beleg —
  keine beleglose Klassifizierung) und explizitem Rechtsprofil
  (`domain/enums.py::Rechtsordnung`, inkl. `UNGEKLAERT`). NUR
  `fachstatus == GEPRUEFT` schreibt die gewählte Rechtsordnung
  tatsächlich auf den Vertrag zurück (`upsert_vertrag`); ein `ENTWURF`
  bleibt sichtbar/gespeichert, aber wirkungslos für Mahnung/Index/
  Sollstellung. Es gibt (bewusst) keine automatische Freigabe und kein
  neues Datenmodell für den Vertrag selbst — nur diese zusätzliche,
  rein additive Audit-Tabelle.
- **Sperren-Aufhebung ist strukturell IMMER einzeln:** `sperre_aufheben`
  (Service) nimmt genau EINE `sperre_id` entgegen, verlangt eine
  nicht-leere Begründung (sonst `QuellenbelegFehltError`), prüft die
  Zugehörigkeit zum Vertrag (`BindungInkonsistentError` sonst) und dass
  sie nicht bereits aufgehoben ist. Es existiert an keiner Stelle im
  Modul ein Sammel-/Automatik-Aufhebungspfad — RATENPLAN/RECHTSANWALT
  sind dabei nicht privilegiert, aber auch nicht ausgenommen: jede
  Aufhebung braucht denselben Beleg/dieselbe Begründung, protokolliert
  im Audit-Log.
- **Index-Prüfbedarf (`IndexPruefbedarfTable`) ist bewusst eine eigene,
  UNVERBUNDENE Tabelle**, nicht eine Lockerung von `IndexKlauselTable`:
  alle Felder nullable, keine Pflichtfeld-Prüfung, kein
  Freigabemechanismus, keine Wirkung auf `IndexKlauselTable`/Buchungen.
  Grund: SQLite kann NOT-NULL-Constraints nicht ohne vollständigen
  Tabellen-Rebuild lockern, und das hätte ein unnötiges Migrationsrisiko
  für eine ggf. bereits befüllte Tabelle bedeutet. Eine echte,
  freigebbare Indexklausel entsteht weiterhin ausschließlich über den
  bestehenden `index/service.py::klausel_anlegen`-Weg mit unveränderten
  Pflichtfeldern.
- **Manueller Bankabgleich ohne Zweitbuchung
  (`/backoffice/bank/{id}/verknuepfen`,
  `BankImportService.verknuepfe_mit_bestehender_zahlung`):** verknüpft
  eine eingelesene Rohtransaktion mit einer EXPLIZIT gewählten, bereits
  bestehenden ZAHLUNG-OP, ohne eine zweite OP-Zeile zu buchen — die
  Fachregel "bestehende Mieterkonto-Buchungen dürfen bei einem späteren
  Rohbankimport nicht doppelt gutgeschrieben werden" ist damit
  eingehalten. Kein neues Linktabellen-Konstrukt nötig: die bereits
  bestehende generische `ZuordnungTable` (Bank-Transaktion ↔ OP-Position)
  wird wiederverwendet, ergänzt um eine kleine additive Prüfmethode
  (`BankRepository.verknuepfter_betrag_fuer_op`, prüft den noch nicht
  "erklärten" Restbetrag der ZAHLUNG selbst — die bestehende
  `create_zuordnung` prüfte bisher nur den Restbetrag der TRANSAKTION).
  Geprüft werden atomar (frisch per ID geladen, `with_for_update`):
  Gesellschaft, Konto, Währung, beide Restbeträge getrennt, sowie ein
  Link-Duplikat (dasselbe Transaktion/OP-Paar unter einer ANDEREN
  `vorgang_id`). Ein exakter Retry MIT derselben `vorgang_id` bleibt ein
  sicherer No-Op — geprüft VOR den Restbetrags-Validierungen (sonst
  würde der bereits durch denselben Vorgang belegte Betrag den Retry
  selbst als "über dem Restbetrag" ablehnen). Bestehende Ledger-Zeilen
  bleiben unverändert (append-only).
- **Automatische Zuordnung bleibt Nutzer-seitig zurückgestellt, jetzt
  auch route-seitig hart geschlossen — nicht nur ausgeblendet:** die
  POST-Route `/backoffice/bank/{id}/automatisch-zuordnen` verweigert die
  Ausführung (HTTP 403) außerhalb bekannter Demo-Umgebungen
  (`ist_bekannte_demo_umgebung`), unabhängig davon, ob im UI ein Button
  dafür sichtbar war — die reine Anzeige-Ausblendung (bereits Teil des
  Banner-Fixes aus Paket A) wäre allein kein Schutz gegen einen direkten
  POST, wie von Codex bei der Paket-A-Rückprüfung angemerkt. Manuelle
  Zuordnung (`/manuell-zuordnen`) und die neue Verknüpfung
  (`/verknuepfen`) bleiben in jeder Umgebung möglich. Ein reiner
  Raw-Import bestätigt weiterhin NIE Vollständigkeit (siehe
  "Bankvollständigkeit" unten).
- **Was Paket B bewusst NICHT tut:** kein vollständiges MieWeG-
  Berechnungsprofil (siehe "Index-Rechtsprofile" unten, unverändert),
  keine Mailpipeline/Jobs (Paket C, noch nicht begonnen), keine
  automatische Bankabholung/-zuordnung (weiterhin bis EBS/EBICS
  zurückgestellt), keine Änderung an `src/invoice_automation/`.
- Regressionstests: `tests/mietinkasso/test_vertragspruefung.py` (14
  Tests: Prüfung/Freigabe, Sperren-Aufhebung, Index-Prüfbedarf), neue
  Fälle in `tests/mietinkasso/test_bank.py` (9 Tests für
  `verknuepfe_mit_bestehender_zahlung`, inkl. Replay/Konflikt/
  Restbetrags-Grenzen) sowie Backoffice-HTTP-Integrationstests in
  `tests/mietinkasso/test_backoffice.py` (Vertragsprüfseite ENTWURF vs.
  GEPRUEFT, Sperren-Aufhebung mit/ohne Begründung, Index-Prüfbedarf,
  Bank-Verknüpfung inkl. Replay/Link-Duplikat, sowie der route-seitige
  403-Block der automatischen Zuordnung außerhalb bekannter
  Demo-Umgebungen über `monkeypatch` auf `backoffice/app.py::_DEMO_UMGEBUNG`
  — ein direkter End-to-End-Test mit echtem
  `MIETINKASSO_ENVIRONMENT=production`-Prozessstart ist wegen der
  bereits in Paket A dokumentierten Modul-Import-Einmaligkeit von
  `api.app`/`backoffice.app` pro Testprozess nicht praktikabel).

### Codex-Rückprüfung Paket B (12.09., vor Produktionsfreigabe) — behoben

Codex hat Paket B mit einer isolierten synthetischen Datei-SQLite-DB
(keine Echtdaten) unabhängig geprüft und drei konkrete Befunde
reproduziert; Codex hält Paket B bis zu dieser Korrektur von der
Produktion zurück:

1. **Cross-Tenant-Leak über den Replay-Kurzschluss in
   `verknuepfe_mit_bestehender_zahlung`:** der frühere Code prüfte einen
   exakten Replay derselben `vorgang_id` (identische Transaktion/OP/
   Betrag) VOR den Bindungs-/Mandanten-/Währungsprüfungen. Ein Aufrufer
   mit einem EIGENEN, an sich berechtigten Konto konnte dadurch, wenn er
   die IDs einer FREMDEN Transaktion/OP samt deren `vorgang_id`/Betrag
   kannte oder wiederverwendete, die FREMDE Zuordnung als vermeintlichen
   "eigenen Replay" zurückbekommen — ohne dass deren tatsächliche Konto-
   /Mandantenzugehörigkeit je geprüft wurde. Behoben durch Umordnung:
   ALLE Identitäts-/Bindungsprüfungen (Konto-Zugehörigkeit der OP,
   OP-Typ/-Status, Gesellschaft, Währung) laufen jetzt VOR dem
   Replay-Kurzschluss; nur die nachfolgenden Restbetrags-/Link-Duplikat-
   Prüfungen dürfen für einen echten Replay noch übersprungen werden.
   Regressionstest:
   `test_verknuepfen_replay_ueber_fremdes_konto_liefert_nicht_die_fremde_zuordnung`
   in `tests/mietinkasso/test_bank.py`.
2. **SQLite-Schreibserialisierung fehlte tatsächlich** (siehe Korrektur
   im Abschnitt "Bank-Zuordnung" unter "Integration/Betrieb" oben) —
   `with_for_update` schützt SQLite nicht, echte Nebenläufigkeit auf
   `_zuordnen_atomar`, `verarbeite_ruecklastschrift` und
   `verknuepfe_mit_bestehender_zahlung` konnte denselben Restbetrag
   mehrfach beanspruchen. Behoben durch
   `infrastructure/db/sqlite_write_lock.py::schreibgesperrte_session`
   (`BEGIN IMMEDIATE` ab dem ersten Statement, für Datei-SQLite).
3. **Objektausschluss (Pilotobjekt 107) fehlte bei zwei der drei neuen
   Vertragsprüfungspfade:** `sperre_aufheben` und
   `index_pruefbedarf_speichern` riefen
   `StammdatenRepository.pruefe_vertrag_nicht_ausgeschlossen` bisher
   NICHT auf (anders als `pruefung_anlegen`, das ihn von Anfang an
   geprüft hat) — beide ergänzt. Zusätzlich: die Freigabe
   (`pruefung_anlegen` mit `fachstatus=GEPRUEFT`) schrieb die
   Prüfungszeile und die Rechtsordnung-Rückschreibung auf den Vertrag
   bisher als ZWEI getrennte Commits — ein Fehler zwischen beiden hätte
   eine "halb gespeicherte Freigabe" hinterlassen können (eine
   GEPRUEFT-Prüfungszeile, deren Rechtsordnung der Vertrag nie
   übernommen hat). Beide Schritte laufen jetzt in EINER gemeinsamen
   DB-Transaktion mit gemeinsamem Rollback. Der Audit-Eintrag bleibt
   bewusst ein separater, nachgelagerter Schritt der aufrufenden
   Backoffice-Route — wie bei jeder anderen Fachaktion in diesem Modul
   (Mahnpolicy-Freigabe, Nachbuchung, ...); eine vollständig
   transaktionale Audit-Kette wäre eine größere, hier nicht angefragte
   Architekturänderung. Regressionstests in
   `tests/mietinkasso/test_vertragspruefung.py`:
   `test_sperre_aufheben_auf_ausgeschlossenem_objekt_wird_blockiert`,
   `test_index_pruefbedarf_speichern_auf_ausgeschlossenem_objekt_wird_blockiert`,
   `test_geprueft_freigabe_ist_atomar_bei_fehler_zwischen_pruefung_und_vertragsschreibung`.

Alle drei Fixes wurden gegen absichtlich deaktivierte Fassungen des
jeweiligen Fixes gegengetestet (der zugehörige Regressionstest schlägt
ohne den Fix nachweislich fehl), um zu bestätigen, dass die Tests die
Regression tatsächlich erkennen und nicht nur zufällig grün sind.

## Paket C — MieWeG-2026-Berechnungsvorschau (Nutzerauftrag 12.09., Anschluss an Paket B)

Neues, vollständig additives Modul `src/mietinkasso/mieweg_vorschau/`
(eigene Tabelle `mieweg_vorschauen`, keine Änderung an
`VertragTable`/`VertragsKomponenteTable`/`OPPositionTable`/
`IndexKlauselTable`) - eine ausdrücklich als BERECHNUNGSVORSCHAU
gekennzeichnete, deterministische Modellierung der MieWeG-2026-
Wertsicherungsregeln, KEIN Freigabe-/Buchungsmechanismus:

- Löst keine Vorschreibung, keine Sollstellung, keine Mahnung und
  keinen Mailversand aus.
- Ruft zur Laufzeit KEIN KI-Modell auf - reine `Decimal`-Arithmetik
  (`mieweg_vorschau/berechnung.py`).
- Verwendet AUSDRÜCKLICH NICHT das bestehende
  `index/service.py::EINFACHER_SCHWELLENVERGLEICH`-Profil (dessen
  eigene Sperre gegen andere Rechtsprofile bleibt unverändert gültig -
  siehe "Index-Rechtsprofile" unten) - ein eigenständiges, neues Modell
  für genau diesen Auftrag.

### Quellenlage (Codex-Hinweis für die fachliche Abnahme)

Die drei vom Nutzer genannten Primärquellen (RIS Bundesgesetzblatt
NOR40274266/NOR40274269, Parlament-Erläuterungen) waren in dieser
Sitzung über die Netzwerk-Egress-Policy blockiert (`ris.bka.gv.at`,
`parlament.gv.at` sowie mehrere Kanzlei-Sekundärquellen nicht
erreichbar). Die implementierten Regeln stützen sich auf die vom
Nutzer wörtlich übermittelte Spezifikation, gegengeprüft über
Web-Suche (Kanzlei-/WKO-Zusammenfassungen zum 5. MILG/MieWeG 2026,
u. a. zu den Referenzjahr-Übergangsgrenzen 2025/2026 und zur
Mitteilungsfrist nach § 16 Abs 9 MRG) - **keine direkte Prüfung gegen
den amtlichen Gesetzestext durch diese Session**. Vor Produktiv-
Verwendung MUSS Codex (mit Zugriff auf die Primärquellen) die
implementierten Formeln in `berechnung.py` gegen den tatsächlichen
Gesetzestext verifizieren, insbesondere:

- Die exakte Formulierung/Randfälle der Referenzjahr-Übergangsgrenzen
  (2025: max. 1 %, 2026: max. 2 %) und der Jahresübergang zur
  allgemeinen 3%-Regel ab 2028.
- Die Reihenfolge Dämpfung-vor-Anteiligkeit und die genaue Definition
  von "volle Monate nach Abschlussmonat" für Randmonate.
- Die Altvertrags-Übergangsregel (Bezugsmonat statt Abschlussdatum,
  fixe erste Modellbewertung April 2026, "Annualaverage als bisherige
  Basis → Dezember").

### Zwei Rechenspuren - bewusste Vereinfachung der Vertragsspur

Die GESETZLICHE Höchstgrenze wird vollständig nach der in
`berechnung.py::berechne_gesetzliche_hoechstgrenze` implementierten
Formel berechnet (VPI-Jahresdurchschnitt-Differenz, allgemeine
3%-Dämpfung symmetrisch auch bei Deflation, MRG-Vollanwendungs-
Übergangsdeckel 2025/2026, Erstjahresanteiligkeit, mehrjährige
Historie ohne Doppelanrechnung, Rundung halber-Cent-ab/mehr-auf).

Die VERTRAGLICH zulässige Änderung wird bewusst NICHT aus den
VPI-Daten hergeleitet - individuelle Vertragsklauseln variieren zu
stark, um sie generisch nachzubilden, ohne die konkrete Klausel zu
erraten (dieselbe Zurückhaltung wie bei der Rechtsordnungs-
Klassifizierung in `vertragspruefung/service.py`). Stattdessen ist die
Vertragsspur ein vom Operator manuell geprüfter, mit Pflicht-
Quellenbeleg belegter Betrag samt optionalem vertraglichen
frühestmöglichen Termin. Der maßgebliche Höchstbetrag ist das MINIMUM
beider Spuren; fehlt eine der beiden Spuren (unvollständige VPI-
Historie ODER noch nicht erfasste Vertragsspur), bleibt der maßgebliche
Betrag ausdrücklich `None`/Prüfbedarf - es wird NIE nur eine Seite
verwendet ("fehlende Belege müssen Prüfbedarf ergeben", wörtliche
Marschroute). Der frühestmögliche Gesamttermin ist das MAXIMUM aus
gesetzlichem und vertraglichem Termin (eine vertraglich später
ausgelöste Schwelle wirkt nie vor dem Vertragstermin; eine spätere
gesetzliche Grenze allein löst nie automatisch eine Erhöhung aus - hier
strukturell dadurch abgesichert, dass diese Route NIE etwas bucht,
sondern ausschließlich einen Vorschauwert anzeigt).

**Weiterhin offen:** eine generische "Vertragsklausel-Interpretation"
aus Freitext oder Strukturdaten ist NICHT gebaut und explizit nicht
Teil dieses Pakets - die Vertragsspur bleibt manuelle Fachprüfung.

### § 16 Abs 9 MRG / Zustellnachweis - nie eine ausführbare Fälligkeit

Die Berechnung liefert einen gesetzlich zulässigen Bewertungstermin
(1. April des Ziel-Bewertungsjahres) - das ist AUSDRÜCKLICH NICHT die
erste tatsächliche Fälligkeit. Nach § 16 Abs 9 MRG muss eine Mitteilung
NACH Wirksamkeit erfolgen und dem Mieter mindestens 14 Tage vor dem
Zinstermin zugehen. Da dieses Modul keine Mitteilung versendet und
keinen Zustellnachweis technisch prüfen kann, wird ein fehlender
`zustellnachweis_referenz`-Eintrag IMMER als offener Nachweis
ausgewiesen ("Ergebnis bleibt reine Vorschau, keine ausführbare
Fälligkeit") - unabhängig davon, ob die restliche Berechnung
vollständig ist.

### Komponenten - nur explizit vertragsindexierte Beträge

Der Basisbetrag kann optional auf konkrete `VertragsKomponenteTable`-
Zeilen des Vertrags referenzieren (`basis_komponenten_ids`); der
Service lehnt jede referenzierte Komponente ab, die nicht
`indexierbar=True` trägt (z. B. Garage, Betriebskosten-Vorauszahlung -
diese werden NIE automatisch indexiert, unabhängig vom eingegebenen
Basisbetrag).

**Korrigiert (Folgeauftrag Markus, Schutzlogik gegen bereits enthaltene
Erhöhungen, siehe eigener Abschnitt unten):** ursprünglich war der
Basisbetrag ein von den Komponenten UNABHÄNGIGER, frei eingegebener
EUR-Wert ("kein automatisches Aufsummieren") - das war eine echte
Lücke, kein bewusstes Feature: eine referenzierte Komponente behauptet
"meine Basis besteht aus GENAU diesem Betrag", ein davon losgelöster
`basis_betrag_cent` hätte einen bereits in der Komponente enthaltenen
Erhöhungsschritt unbemerkt verfälschen können. Der Service verlangt
jetzt zwingend `basis_betrag_cent == Summe der referenzierten
Komponenten-Beträge` (sonst `ValueError`) UND dass jede referenzierte
Komponente laut `gueltig_von`/`gueltig_bis` zum angegebenen
Bezugsjahr/-monat bereits bestanden hat (sonst `ValueError`) - eine
erst später vereinbarte Komponente (z. B. eine 2025 nachträglich
vereinbarte Küche) darf nicht rückwirkend in eine ab 2024 laufende
Kumulierung einfließen.

### Folgeauftrag Markus (12.09., Fortsetzung): unabhängige Prüfung der Schutzlogik gegen bereits enthaltene Erhöhungen

Markus hat NACH der ursprünglichen Paket-C-Auslieferung ausdrücklich die
Schutzlogik gegen Doppelzählung unabhängig geprüft (teils durch direktes
Lesen der RIS-Primärquelle, `ris.bka.gv.at` ist über die Netzwerk-
Egress-Policy dieser Sitzung selbst nicht erreichbar) und mehrere
konkrete, mit Tests reproduzierte Lücken benannt. Alle unten
beschriebenen Korrekturen sind mit synthetischen Regressionstests
belegt (`tests/mietinkasso/test_mieweg_berechnung.py`,
`tests/mietinkasso/test_mieweg_vorschau_service.py`); es wurde an
diesem Bestand NICHTS freigegeben, keine automatische Vorschreibung/
kein Versand ergänzt.

1. **`daempfe()` dämpfte fälschlich auch Senkungen (Deflation)
   symmetrisch.** Nach Markus' Lektüre der Primärquelle (BGBl. I Nr.
   114/2025, §1 Abs 2 Z1) dämpft das Gesetz AUSSCHLIESSLICH eine
   Erhöhung über 3 % - für eine symmetrische Halbierung bei einer
   Senkung unter -3 % gibt es keine Rechtsgrundlage. Korrigiert: eine
   Senkung wird jetzt immer in voller Höhe durchgereicht. **Nicht in
   dieser Sitzung selbst gegen die Primärquelle nachvollzogen** (Netz-
   zugriff blockiert) - Codex sollte §1 Abs 2 Z1 vor Produktivnutzung
   trotzdem gegenlesen.
2. **Vertraglicher Termin konnte einen ungültigen (Nicht-April-)Termin
   oder einen Termin in einem nicht berechneten Jahr liefern.** Das
   reine `max(gesetzlicher Termin, vertraglicher Termin)` konnte z. B.
   einen vertraglichen September-Termin unverändert ausgeben, obwohl
   § 1 Abs 4 nur den 1. April als Anpassungsstichtag zulässt. Korrigiert:
   der kombinierte Termin wird auf den nächsten gültigen 1. April
   aufgerundet; landet dieser dadurch in einem ANDEREN Jahr als dem
   berechneten `ziel_bewertungsjahr` (das würde zusätzliche, hier nicht
   angefragte VPI-Jahreswerte voraussetzen), wird KEIN Termin
   ausgegeben, sondern ein offener Nachweis erzwungen - kein
   automatischer Sprung auf einen nicht berechneten Zeitraum.
3. **Historischer Basisbetrag wurde mit der heute tatsächlich
   verrechneten Miete verwechselt - der zentrale Doppelzählungsfund.**
   `massgeblicher_hoechstbetrag_cent` ist die gesetzliche/vertragliche
   OBERGRENZE seit dem historischen Bezugszeitpunkt, NICHT automatisch
   der neue Zielbetrag - zwischen damals und heute können bereits
   (teilweise) Erhöhungen umgesetzt worden sein, die dieses Modul nicht
   kennt. Neues, GETRENNTES Pflichtfeld-Trio (analog zur Vertragsspur,
   mit Pflicht-Quellenbeleg): `aktuell_verrechneter_betrag_cent` +
   `aktuell_verrechnet_quellenbeleg` + `aktuell_verrechnet_stichtag`.
   Neues Ergebnisfeld `ausfuehrbare_erhoehung_cent` = `max(0,
   massgeblicher_hoechstbetrag_cent - aktuell_verrechneter_betrag_cent)`
   - NIE eine negative "Erhöhung" (Senkung), aber auch NIE ein
   Aufschlag auf einen bereits teilweise erhöhten Betrag. Ohne
   dokumentierten aktuellen Vergleichswert bleibt `ausfuehrbare_
   erhoehung_cent` `None` (Prüfbedarf) - "ohne Historie keine
   ausführbare Erhöhung", wörtliche Vorgabe.
4. **Leere/doppelte `basis_komponenten_ids` sowie BK/HK-Aliasarten
   wurden nicht abgefangen.** Eine doppelte ID hätte denselben Betrag
   zweimal in die Summenprüfung eingerechnet; eine fälschlich
   `indexierbar=True` markierte Betriebs-/Heizkosten(-Vorauszahlungs)-
   Komponente (auch unter den Aliasarten `BK_VZ`/`HK_VZ`/
   `BK_PARKPLATZ`) wäre sonst durchgerutscht. Beides wird jetzt hart
   abgelehnt, unabhängig vom `indexierbar`-Flag (Verteidigung in der
   Tiefe, analog zu `index/service.py::_NIE_INDEXIERBARE_ARTEN` - bewusst
   eine eigene, lokale Liste in `mieweg_vorschau/service.py`, um die
   Modultrennung laut `AGENTS.md` nicht aufzuweichen; Codex sollte beide
   Listen bei künftigen Änderungen synchron halten).
5. **Netto/Brutto-Klarstellung.** Nach Rückfrage bestätigt: alle
   Cent-Beträge in diesem Modul sind BRUTTO (verbindliche Konvention
   dieses Repositories, siehe `domain/money.py::zerlege_brutto_cent`).
   Komponenten mit unterschiedlichen `ust_satz_promille`-Sätzen dürfen
   zu einer Brutto-Summe addiert werden, diese Summe darf aber
   NIRGENDS so behandelt werden, als wäre sie über einen einzigen
   Netto-Steuersatz herleitbar - im aktuellen Code passiert das nicht
   (`berechnung.py` rechnet ausschließlich mit VPI-Verhältniszahlen),
   ist aber als Grenze für künftige Erweiterungen dokumentiert.
6. **MRG-Vollanwendung/-Teilanwendung wurde ohne Nachweis der
   Wohnungsnutzung als Wohnungsrechner-Fall behandelt.** MieWeG §1 Abs1
   gilt ausdrücklich nur für WOHNUNGEN - ein Geschäftsraum unter
   MRG-Vollanwendung ist kein Wohnungsrechner-Fall, auch wenn die
   Rechtsordnung `OESTERREICH_MRG_VOLL` lautet. Da es dafür kein
   bestehendes Stammdatenfeld gibt (`Nutzungsstatus` beschreibt nur den
   Belegungsstatus, nicht die Nutzungsart), neuer Pflichtparameter
   `ist_wohnungsnutzung: bool | None` - `None`/`False` sperrt den
   Wohnungsrechner genauso wie ein falsches Rechtsprofil. **Offener
   Punkt:** eine strukturierte Wohnung-vs-Geschäftsraum-Kennzeichnung
   auf `EinheitTable`/`VertragTable` existiert weiterhin nicht; die
   Bestätigung bleibt manuelle Fachprüfung je Vorschau.
7. **Altvertrag-Bezugsjahr konnte blind den ursprünglichen
   Vertragsbeginn statt des zuletzt verwendeten Indexmonats verwenden.**
   § 4 Abs 2: maßgeblich ist bei Altverträgen der zuletzt TATSÄCHLICH
   verwendete Indexmonat. Eine verlässliche automatische Prüfung dafür
   gibt es nicht (dafür fehlt eine belastbare Datenquelle über die
   bisherige Indexnutzung in diesem Modul) - als Kompromiss erzeugt ein
   exaktes Zusammentreffen von Bezugsjahr/-monat mit
   `VertragTable.gueltig_von` einen offenen Nachweis (Verdachtsmoment,
   kein Hartstopp: ein Altvertrag, der tatsächlich noch nie indexiert
   wurde, ist ein legitimer Fall). **Offener Punkt:** eine echte
   Cross-Prüfung gegen `IndexKlauselTable`/`IndexAnpassungTable` (falls
   für denselben Vertrag bereits ein späterer Index-Anpassungsstichtag
   dokumentiert ist) ist NICHT gebaut - das würde eine neue Abhängigkeit
   dieses Moduls auf `index/repository.py` erfordern, die außerhalb
   dieses eng gefassten Folgeauftrags nicht ergänzt wurde.

**Dritte, unabhängige Gegenprobe (14 synthetische Fälle, 9/14 bestanden,
5 konkrete Lücken behoben):**

8. `basis_komponenten_ids=[]` rutschte durch alle Komponentenprüfungen
   (`any([])`/`len([]) != len(set([]))` sind beide `False`) und konnte
   trotzdem `vollstaendig=True` ergeben - eine tatsächlich durchgeführte
   numerische Berechnung verlangt jetzt eine ECHTE, nichtleere
   Komponentenliste (offener Nachweis, kein Hartstopp für unvollständige
   Entwürfe).
9. `WASSER`/`STROM` fehlten in `_NIE_INDEXIERBARE_ARTEN` und wurden bei
   fälschlich gesetztem `indexierbar=True` akzeptiert - ergänzt.
10. `ausfuehrbare_erhoehung_cent` wurde bislang unabhängig von
    `offene_nachweise` berechnet - ein fehlender Stichtag, ein
    fehlender Zustellnachweis oder ein in ein anderes Jahr verschobener
    Termin machten das Ergebnis zwar `vollstaendig=False`, die
    "ausführbare Erhöhung" wurde aber trotzdem beziffert ausgegeben.
    Jetzt getrennt: `rechnerische_differenz_cent` (informativ, immer
    sichtbar sobald beide Beträge vorliegen) und
    `ausfuehrbare_erhoehung_cent` (NUR gesetzt, wenn keinerlei offener
    Nachweis mehr aussteht - keine Behauptung von Ausführbarkeit ohne
    vollständige Nachweise).
11. Ein negativer `aktuell_verrechneter_betrag_cent` oder
    `vertraglich_zulaessiger_betrag_cent` wurde akzeptiert und hätte
    eine fiktive, überhöhte "Erhöhung" erzeugen können - beide werden
    jetzt bei negativem Wert hart abgelehnt; Null bleibt bewusst
    zulässig (Prüfung über `is not None`, nicht über Wahrheitswert).
12. Zitatkorrektur: der Grund für "nur der 1. April ist ein gültiger
    Anpassungstermin" ist § 1 Abs 4 MieWeG, nicht Abs 3 (in Code-
    Kommentaren, Docstrings und dieser Doku korrigiert).
13. **UI-Folgefund (kein Berechnungsfehler):** `backoffice/app.py::
    _mieweg_vorschau_zeile_html` las `aktuell_verrechneter_betrag_cent`
    aus `ergebnis_json`, der Service hatte dieses Feld dort aber nicht
    gespeichert (nur in `eingaben_json`) - die Spalte "Aktuell
    verrechnet" zeigte deshalb immer einen Strich, obwohl der Wert
    korrekt verarbeitet wurde. Ergänzt in `ergebnis_json` (dupliziert
    wie `vertraglich_zulaessiger_betrag_cent`); mit einem HTTP-Test
    abgesichert, der einen bekannten synthetischen Betrag tatsächlich
    in der gerenderten Tabelle nachweist.

### Bedienung

`/backoffice/vertrag/{id}/mieweg-vorschau`: pro Vertrag zugängliche,
versionierte, unveränderliche Vorschauhistorie (Quellen/Datum/Beträge/
Bezugsmonate/Rechtsprofil/Komponenten/Vertragsschwelle sichtbar je
Version); fehlende Werte werden konkret benannt (offene Nachweise als
Klartext-Liste je Version), nie stillschweigend übersprungen.
CSRF-Schutz, Objekt-107-Ausschluss und echte Gesellschafts-Scope-
Prüfung wie bei jeder anderen Fachaktion in diesem Modul. VPI-
Jahresdurchschnittswerte werden über ein einfaches Zeilenformat
(`JAHR;WERT;QUELLE;DATUM`) statt einer dynamischen JS-Eingabetabelle
erfasst (bewusst klein gehalten) - eine fehlerhafte Zeile wird als
Fehler gemeldet, nie still ignoriert.

### Tests

- `tests/mietinkasso/test_mieweg_berechnung.py` (28 Tests): unabhängige
  Grenzfalltests je Regel - Dämpfung (NUR bei Erhöhung über 3 %, siehe
  Korrektur oben; keine unbegründete Kappung von Senkungen auf 0),
  Rundung (exakte Decimal-Randtests bei genau halbem Cent),
  Erstjahresanteiligkeit (Dezember=0/12, Juni=6/12, Dämpfung-vor-
  Anteiligkeit), Übergangsdeckel 2025/2026 (inkl. "gilt nicht ohne
  Zinsbeschränkungsflag", "gilt nicht für Deflation", "gilt nicht mehr
  ab 2027"), mehrjährige Altvertrags-Historie ohne Doppelanrechnung,
  unabhängige Kumulierung auf der ursprünglichen Basis, fehlende
  VPI-Werte (kein erfundener Wert), ungültige Eingaben.
- `tests/mietinkasso/test_mieweg_vorschau_service.py` (37 Tests):
  Rechtsprofil-Gating (UNGEKLAERT/Gewerbe/WGG/frei/Deutschland kein
  Wohnungsrechner-Fall, Zinsbeschränkung nur bei Vollanwendung,
  MRG_VOLL/MRG_TEIL ohne bestätigte Wohnungsnutzung kein Wohnungsrechner-
  Fall), Kombination der zwei Spuren (Minimum, gültiger-1.-April-Termin
  inkl. Ablehnung eines Termins in einem anderen Jahr), Quellenbeleg-
  Pflicht der Vertrags- UND der Aktuell-verrechnet-Spur, Komponenten-
  Validierung (Summenabgleich, Gültigkeitszeitraum, leere/doppelte IDs,
  BK/HK-Aliasarten), Objektausschluss, Altvertrag-Mindestjahr und
  -Bezugsjahr-Plausibilität, Versionierung, Überlappungswarnung über
  mehrere Vorschau-Versionen, ausführbare Erhöhung (`max(0, ...)`).
- `tests/mietinkasso/test_backoffice.py` (+3 HTTP-Integrationstests):
  beide Spuren kombiniert, fehlende VPI-Historie ergibt Prüfbedarf,
  UNGEKLAERT wird nicht hineingeraten.

## Echtbetrieb-Intake (HV-20260912-ECHTBETRIEB, 12.09.2026)

- **Quellenmapping bleibt vollständig bei Codex:** `src/mietinkasso/intake/`
  + `scripts/intake_import.py` implementieren NUR den generischen
  Schnittstellenvertrag (`docs/hausverwaltung/IMPORT_VERTRAG.md`) — die
  Zuordnung der realen Quelldokumente (Deb-/Kred-/Sachkontensalden,
  Journal, Stammblätter, Zinslisten, Kaution, Verträge) auf dieses
  Schema ist NICHT Teil dieser Codebasis und ausdrücklich Codex'
  Aufgabe. Kein Test in diesem Repository prüft echte Quelldateien.
- **Rechtsordnung UNGEKLAERT ist erlaubt, aber sperrt drei Wege:** ein
  Vertrag mit `rechtsordnung: "UNGEKLAERT"` ist anlegbar, aber technisch
  von Sollstellung (`vorschreibung/service.py`), Index-Anpassung
  (`index/service.py`) und Mahnung (`mahnwesen/service.py`) gesperrt
  (`domain/enums.py::rechtsordnung_geklaert`). Das Aufheben dieser Sperre
  passiert automatisch, sobald ein späterer Import/eine spätere manuelle
  Korrektur eine der anderen `Rechtsordnung`-Kategorien setzt — es gibt
  keine eigene "Freigabe"-Aktion dafür.
- **Sperren (`sperren[]`) sind additiv, ein Aufheben ist NICHT Teil des
  Intakes:** eine über den Intake gesetzte Sperre (RECHTSANWALT/
  RATENPLAN/MANUELL/...) bleibt aktiv, bis sie MANUELL im Backoffice
  aufgehoben wird (`StammdatenRepository.sperre_aufheben`) — dafür gibt
  es aktuell noch keine eigene Backoffice-Bedienoberfläche (nur die
  Anzeige im Kontoauszug), nur den bestehenden Repository-Aufruf.
- **Eröffnungskorrektur ist bewusst schmal geschnitten:** der neue Pfad
  (`op/service.py::eroeffnungskorrektur_buchen`, Intake-Entität
  `eroeffnungskorrekturen[]`) deckt GENAU den Fall "im bestätigten
  Gesamtsaldo nachweislich fehlender Posten mit Datum vor/auf dem
  Stichtag" ab. Er ersetzt/erweitert nicht die Möglichkeit, eine bereits
  eingespielte EINZEL_OP-Eröffnung nachträglich zu korrigieren (dafür
  bleibt `storniere_und_korrigiere` der richtige Weg) und öffnet kein
  allgemeines Altjournal-Tor für Gesamtsaldo-Konten.
- **Vertragskomponenten-Intake (`komponenten[]`) ersetzt keinen echten
  Zinslisten-Parser:** wie schon vor dieser Ergänzung dokumentiert
  (siehe "Zinslisten-Import" unten) bleibt das automatische Einlesen
  eines realen Zinslisten-Exportformats offen; der Intake nimmt nur
  bereits einzeln aufbereitete Komponentenzeilen entgegen.
- **Mahnstufen-Konfiguration im Backoffice** (`/backoffice/mahnwesen/policy`)
  erlaubt das Anlegen/Freigeben neuer `MahnPolicy`-Versionen mit genau
  zwei Stufenparametern (Tage nach Fälligkeit / Mindestabstand nach
  Stufe1); Zinsen/Gebühren sind serverseitig hart auf 0 erzwungen (kein
  Formularfeld akzeptiert einen anderen Wert). Es gibt keine Möglichkeit,
  eine bereits FREIGEGEBENE Policy-Version zu deaktivieren/zurückzuziehen
  — nur eine NEUE Version anzulegen und freizugeben (bestehendes
  Verhalten von `MahnPolicyRepository`, hier nur sichtbar gemacht).

### Codex-Rückprüfung Commit 1d72611 — behoben

- **`scripts/intake_import.py plan` war NICHT rein lesend:**
  `create_all_tables_fuer` lief bisher VOR jeder Prüfung und legte bei
  Bedarf eine neue Datei/ein neues Schema an, auch bei `plan` und bei
  einem `apply` mit falschem Hash/ungültigem Paket. Behoben:
  `plan` öffnet eine bestehende SQLite-Datei jetzt über eine ECHTE
  Read-Only-Verbindung (`mode=ro`) und plant gegen eine noch nicht
  vorhandene Datei strukturell gegen eine synthetische, rein
  prozessinterne In-Memory-Leerdatenbank (kein neues
  Dateisystemobjekt). `apply` prüft ZUERST den Hash (reine Berechnung,
  keine DB-Verbindung) und führt DANACH dieselbe rein lesende
  strukturelle Vorprüfung durch - ein Schema an der echten Ziel-DB
  entsteht erst, wenn beides erfolgreich war. Regressionstests:
  `tests/mietinkasso/test_intake_cli.py`.
- **Dashboard zeigte Einheiten ohne Vertrag nicht:** das Dashboard
  iterierte nur `list_vertraege_fuer_objekt` - eine Einheit mit
  Nutzungsstatus LEERSTAND/KURZZEITVERMIETUNG/SELFSTORAGE/EIGENNUTZUNG
  ohne (noch) aktiven Vertrag blieb unsichtbar, obwohl sie importiert
  war. Behoben durch eine zusätzliche, rein lesende Bestandsliste
  "Einheiten ohne aktiven Vertrag" je Objekt (kein Dummy-Mieter/-Konto
  wird dafür angelegt).
- **Login ohne Fehlversuchsbegrenzung/Origin-Prüfung:** ergänzt um eine
  GLOBALE (bewusst nicht IP-basierte - siehe
  `backoffice/security.py::LoginRateLimiter`-Docstring, kein Vertrauen
  in Proxy-Header) kurzzeitige Sperre nach 5 Fehlversuchen sowie eine
  Origin-/Referer-Prüfung gegen Login-CSRF
  (`backoffice/app.py::_pruefe_login_origin`, da vor der Anmeldung noch
  kein sitzungsgebundenes CSRF-Token existiert). Das Session-Cookie
  trägt bei `MIETINKASSO_BACKOFFICE_COOKIE_SECURE=true` zusätzlich das
  `__Host-`-Präfix. Neu: `infrastructure/config.py::pruefe_produktionskonfiguration`
  lässt den Prozess in `MIETINKASSO_ENVIRONMENT=production` gar nicht
  erst starten, wenn `SEND_ENABLED`/Passwort-Hash/Cookie-Sicherheit
  nicht dem erwarteten Produktionsstand entsprechen (aufgerufen in
  `api/app.py` beim Import).
- **IMPORT_VERTRAG.md, explizite Betragssemantik:** dokumentiert und
  TECHNISCH erzwungen, dass `betrag_cent` bei `nachbuchungen[]`/
  `EINZEL_OP`-Eröffnungen/`eroeffnungskorrekturen[]` immer POSITIV
  einzugeben ist (Vorzeichen kommt aus `typ`, siehe
  `op/service.py::_pruefe_betrag_positiv` - greift auch außerhalb des
  Intakes, z. B. bei der manuellen Backoffice-Nachbuchung), während
  `GESAMTSALDO` weiterhin ein Guthaben (negativ) sein darf. Außerdem
  richtiggestellt: `paket_hash` ist ein Hash über den GEPARSTEN,
  semantischen Inhalt, keine Datei-Prüfsumme über rohe Bytes.

### Codex-Rückprüfung Commit bb08f92 — behoben

- **Paketinterne ID-Dubletten wurden nicht erkannt:** `pruefe_paket`
  verglich die Primär-/Quell-IDs jeder Zeile nur GEGEN die DB
  (`session.get(...)`), nie GEGENEINANDER innerhalb desselben Pakets.
  Zwei verschiedene Zeilen mit derselben `id` (z. B. zwei
  `KomponenteZeile` mit `id="K1"`, aber unterschiedlichem Betrag)
  konnten dadurch BEIDE als "NEU" durchgehen - `apply()` hätte die
  erste beim Schreiben je nach Reihenfolge/Flush-Zeitpunkt still
  verdrängt, ohne dass der Plan das angezeigt hätte. Behoben durch eine
  paketinterne Dublettenprüfung (`planner.py::_mehrfache_werte`) VOR
  jeder DB-Prüfung, für Gesellschaft/Objekt/Einheit/Debitor/Vertrag/
  Komponente (jeweils eigener ID-Raum) sowie für `import_id` GEMEINSAM
  über Eröffnungen/Nachbuchungen/Eröffnungskorrekturen hinweg (diese
  drei schreiben alle in denselben DB-weiten Unique-Index
  `uq_op_import_id`). Eine erkannte Dublette markiert JEDE betroffene
  Zeile als KONFLIKT und blockiert damit den gesamten Lauf (bestehende
  "Ein Fehler ⇒ gesamter Lauf unverändert"-Garantie). Sperren behalten
  ihre bewusst andere, bereits dokumentierte Dedupe-Logik (`sperre_ist_bereits_aktiv`
  über `(vertrag_id, grund, kommentar)`) - eine Sperre ist ein additiver
  Fakt, mehrere unterschiedliche Sperren desselben Vertrags sind
  fachlich normal und bleiben kein Konflikt. Regressionstests in
  `tests/mietinkasso/test_intake.py`.

## Deployment-Paket (HV-20260912-ECHTBETRIEB, Punkt 3) — Vorlage, keine Produktivfreigabe

`docs/hausverwaltung/DEPLOYMENT_HETZNER.md` +
`docs/hausverwaltung/deploy/` liefern ein Vorlagen-/Beispielpaket
(Env-Datei-Struktur, systemd-Unit, Caddy-Snippet) für Codex' eigene
Integration auf dem bestehenden Hetzner-Server — Claude greift nie auf
den Server zu, deployt nie, und nichts davon wurde gegen den echten
Server getestet. Konkret offen/einzuhalten:

- **Genau EIN Worker-Prozess ist Pflicht, nicht optional:** der
  In-Memory-Login-Session-Store verträgt keinen Mehrprozessbetrieb (
  siehe "Kein Mehrbenutzer-Onlinebetrieb" unten). Ein `--workers`-Wert
  >1 oder ein zweiter parallel laufender Prozess auf demselben Socket
  ist ein Betriebsfehler, kein unterstützter Skalierungsweg.
- **Sichere Cookies verlangen echtes TLS:** `MIETINKASSO_BACKOFFICE_COOKIE_SECURE=true`
  (Produktionsdefault) setzt voraus, dass Caddy tatsächlich HTTPS
  terminiert; ohne TLS meldet sich niemand mehr erfolgreich an (kein
  Fallback, bewusst "der sicherere Fehler").
- **Eigene Domain/eigener Login, keine Unterroute des CEO-Cockpits:**
  wie in RAHMENPROGRAMM.md (Ergänzung 12.09.2026) und der
  Steuerungsnachricht vom selben Tag festgehalten — der bestehende
  Cockpit-Login (bcrypt + signierte Host-Cookies, kein SSO) wird nicht
  wiederverwendet, dieses Modul bringt seinen eigenen PBKDF2-Login mit.
- **Caddy/DNS/TLS-Zertifikate, Firewall-Härtung, Monitoring über
  `/health` hinaus** sind ausdrücklich NICHT Teil dieses Repos, sondern
  Codex' eigene Serverintegration.
- **Kein realer Produktionsstart wurde durchgeführt** — dieses Paket ist
  ungeprüfte Vorlage, bis Codex es gegen den echten Server verifiziert.

## Backoffice-Pilot (diese Runde) — technisch gesperrt, nicht nur dokumentiert

- **Kein Mehrbenutzer-Onlinebetrieb:** EIN lokaler Login
  (`MIETINKASSO_BACKOFFICE_USER`/`_PASSWORD_HASH`), Session-Store ist ein
  reines In-Memory-Dict eines einzelnen Prozesses (`backoffice/security.py`)
  — überlebt keinen Neustart, kein Worker-übergreifendes Teilen. Für
  echten Mehrbenutzerbetrieb: externer Session-Store (Redis/DB) plus
  echtes Rollen-/Gesellschafts-Login pro Anwender.
- **Kein echter Versandadapter:** die Mahnvorschau ruft `versenden()`
  ausschließlich mit `send_enabled=False` auf ("Sendebereitschaft
  prüfen"); es gibt keinen Button/Pfad, der `send_enabled=True` setzt
  oder einen realen Mail-Provider anspricht.
- **Keine echten Bank-Credentials/EBICS:** Bankimport bleibt manueller
  CSV/CAMT-Dateiupload, keine automatisierte Abholung.
- **Zwei getrennte SQLAlchemy-Engines** (`api/app.py` und
  `backoffice/app.py` bauen je eine eigene `session_factory` gegen
  dieselbe `MIETINKASSO_DATABASE_URL`): für die dokumentierte Datei-
  SQLite/PostgreSQL unproblematisch (mehrere Verbindungen auf dieselbe
  DB sind Standard); `MIETINKASSO_DATABASE_URL=sqlite:///:memory:` NICHT
  für den kombinierten Prozess verwenden, da dann zwei isolierte
  In-Memory-Datenbanken entstünden.
- **CSRF-Schutz** ist ein einfacher, sitzungsgebundener Zufalls-Token-
  Vergleich (kein Double-Submit-Cookie, kein Origin-Header-Check
  zusätzlich) — für den lokalen Ein-Operator-Pilot ausreichend, vor
  Internet-Exposition zu härten.
- **"Unsigniert" als Vertragszustand** ist in der Vorschreibungs-Vorschau
  NICHT technisch geprüft (kein solches Feld im Schema) — nur
  "historisch" (`gueltig_bis` überschritten) und "Leerstand"
  (`Nutzungsstatus`) sperren die Freigabe. Eine echte
  Unterschriften-/Signaturverfolgung ist nicht gebaut.

## Integration / Betrieb

- **George-Business-CSV-Adapter ist nur eine Vorschau, kein Import:**
  `mietinkasso/bank/george_business_csv.py` +
  `scripts/george_business_preview.py` lesen einen George-Business-CSV-
  Export rein lesend und klassifizieren jede Zeile (Kandidat/Sammel-
  Summenzeile/Prüffall/Abgelehnt) — es gibt KEINE Anbindung an
  `bank/service.py`, keine Buchung, keine Vertrags-/Mieterzuordnung.
  Sammelgruppen werden über `(Eigene IBAN, Währung, Buchungsdatum, echte
  Buchungsreferenz)` plus einem S/D-Markierungsprofil in "Enthaltene
  Überweisung ID" zusammengeführt (die sichtbare `(Sammel-) Überweisung
  ID` ist laut Fachprüfung KEIN zuverlässiger Gruppenschlüssel — die
  Summenzeile teilt sie sich nur mit dem ersten Einzelposten). Das
  S/D-Profil (107 Zeichen: IBAN+14 Nullen+Währung+9-stelliges Präfix+
  Jahr+Marker+56-stelliger Hex-Hash) ist strukturell exakt bekannt; eine
  ID, die lang genug für einen Profilversuch ist, aber inhaltlich davon
  abweicht (falsches Konto/Währung/Padding/Jahr/Hex/abgeschnittener
  Suffix), wird NIE als gewöhnlicher Einzelumsatz durchgereicht, sondern
  als Prüffall ausgewiesen und "poisoned" jede sonst zufällig valide
  erscheinende Restgruppe mit gleichem Konto/Währung/Datum/Referenz —
  AUSSER die ID entspricht exakt einem separat bestätigten anderen
  Einzelumsatz-ID-Format (aktuell: 118 Zeichen = IBAN+14 Nullen+
  Währung+17-stelliges opakes Präfix+64-stelliger Hex-Hash, KEIN
  S/D-Profil; siehe `_ist_beobachtetes_einzelprofil`). Eine Zeile ohne
  jede brauchbare ID (weder "Enthaltene Überweisung ID" noch "(Sammel-)
  Überweisung ID") wird nie automatisch Kandidat — eine Buchungsreferenz
  allein zählt nicht als eindeutiger Schlüssel, egal wie viele Zeilen die
  Datei hat. Eine Datei ohne jede Datenzeile (nur Kopfzeile) gilt nicht
  als bestätigt vollständig, sondern wird abgelehnt.

  **Formatbeleg (lokaler Vier-Dateien-Abgleich, außerhalb dieses
  Repositories, keine Echtdaten übertragen) — Verlauf:** Für Commit
  6b95af9 bestanden: vier echte George-Business-CSV-Exporte lokal rein
  lesend verglichen — alle 44 Zeilen erhalten, vier Sammelsummen korrekt
  erkannt, 40 Umsätze, alle vier Bankkontrollen (Anfangs-/Endsaldo)
  stimmen centgenau. Commit c317b6a hat denselben Vier-Dateien-Abgleich
  NICHT bestanden: die reine Längenregel für "kaputtes Sammelprofil"
  hat 15 echte, gültige Einzelumsätze (118-Zeichen-Format, siehe oben)
  fälschlich als Prüffall zurückgestellt, statt sie als Kandidaten zu
  erkennen. Mit dem 118-Zeichen-Einzelprofil korrigiert; der nächste
  Commit wird erneut lokal gegen alle vier Dateien geprüft. Das
  bestätigt das Spaltenschema und die Grundklassifizierung gegen echte
  Daten — **keine Produktionsfreigabe**: der Adapter bleibt eine manuell
  zu prüfende Vorschau ohne Anbindung an Buchung/Versand/Mahnwesen, und
  beide ID-Profile (S/D und Einzelumsatz) wurden nur an diesen vier
  Dateien beobachtet, nicht umfassend gegen alle denkbaren
  George-Exportvarianten abgesichert.
- **Zahlungszuordnung erkennt bisher nur explizite Vertragsreferenzen:**
  Die Relevanzprüfung ungeklärter Zahlungseingänge erkennt bisher nur
  explizite `VERTRAG:<id>`-Referenzen. Namenlose Eingänge/freie
  Referenzen werden nicht als möglicherweise zugehörig erkannt. Vor
  automatischem Produktiv-Mahnversand echte Exportfelder/Vertragskennungen
  abgleichen und ungeklärte Eingänge vollständig klassifizieren oder
  betroffene Mahnläufe sperren. Exportvollständigkeit allein löst die
  Zuordnungsfrage nicht. Dieser Punkt bleibt Produktionsblocker.
- **Quellenmapping (Codex-Aufgabe):** Zuordnung der realen 12
  PDF-Quelldokumente (Deb-/Kred-/Sachkontensalden, Journal,
  Stammblätter, Zinslisten, Kaution) auf die hier definierten
  Import-Schnittstellen (`bank/importer.py`, `op/service.py`
  Eröffnungsfunktionen) ist NICHT Teil dieser Codebasis und muss
  gegen die echten Dateien validiert werden, bevor irgendein Import
  produktiv läuft. Insbesondere: Dateinamen sind keine verlässlichen
  Stichtags-Cutoffs (siehe RAHMENPROGRAMM.md, Klarstellung).
- **Bankformate real härten:** `bank/importer.py` parst CAMT.053
  minimal-konform (Ntry/Amt/CdtDbtInd/BookgDt/RmtInf/AcctSvcrRef) und
  ein konfigurierbares CSV-Format. Gegen echte Exportdateien der
  tatsächlich genutzten Bank(en) noch nicht getestet.
- **EBICS/Bank-API:** laut Auftrag zunächst CAMT/CSV-Dropfolder; eine
  automatisierte Abholung (EBICS oder Bank-API) ist bewusst nicht
  gebaut - Schlüsselklärung läuft nutzerseitig, noch keine Anbindung.
  **Verbindliche Leitplanke für den künftigen Adapter (Nutzerauftrag
  12.09., vor Paket C):** EBICS-C53 kann kundenweite Sammeldateien mit
  MEHREREN Konten in einer einzigen Antwort liefern (mehrere
  `Stmt`/`Acct`-Blöcke mit unterschiedlicher IBAN in einer CAMT.053-
  Datei). Der künftige Adapter MUSS jeden `Stmt`/`Acct`/IBAN-Block
  GETRENNT verarbeiten und gegen eine EXPLIZITE Konto-Allowlist prüfen
  - er darf NIEMALS eine gemischte, mehrere Konten enthaltende CAMT-
  Datei pauschal einem einzelnen, vom Operator ausgewählten Mietkonto
  zuordnen (sonst würden Umsätze eines fremden/falschen Kontos
  fälschlich diesem Bankkonto gutgeschrieben).

  **Schutzkorrektur ergänzt (Nutzerauftrag, vor Paket C):**
  `bank/importer.py::parse_camt053` sammelte `Ntry`-Elemente bisher über
  `root.iter()` GLOBAL im gesamten Dokument, OHNE jemals festzuhalten,
  aus welchem `Stmt`/`Acct`/welcher IBAN ein `Ntry` stammt - bei einer
  mehrere Konten enthaltenden Datei wären alle Umsätze aller Konten
  ununterscheidbar in eine einzige flache Liste gemischt worden. Jetzt
  verlangt `parse_camt053` einen Pflichtparameter `erwartete_iban` (die
  IBAN des für DIESEN Import explizit ausgewählten Bankkontos) und
  prüft VOR dem Einlesen jeder `Ntry` über `_pruefe_stmt_konten`, dass
  JEDES `Stmt`/`Acct` in der Datei genau diese IBAN führt - fehlt eine
  IBAN, gehört sie zu einem anderen Konto, oder enthält die Datei
  mehrere unterschiedliche Konten (auch wenn eines davon das richtige
  ist), wird der GESAMTE Import mit `CamtKontoMismatchError` abgelehnt,
  BEVOR irgendeine Zeile eingelesen oder in die DB geschrieben wird -
  kein stilles Herausfiltern des fremden Kontos, keine Teilübernahme
  des passenden Stmt. Gilt für beide Aufrufer (`BankImportService.
  importiere_camt053` und die reine Vorschau `/backoffice/bank/vorschau`).
  Regressionstests in `tests/mietinkasso/test_bank.py`:
  `test_camt053_korrektes_konto_wird_importiert`,
  `test_camt053_fremdes_konto_im_zweiten_stmt_lehnt_gesamten_import_ab`
  (zwei Stmt, nur der zweite fremd - trotzdem NULL Transaktionen
  übrig), `test_camt053_fehlende_iban_lehnt_gesamten_import_ab`.

  **Weiterhin offen (Aufteilung, nicht Ablehnung):** diese Korrektur
  LEHNT eine gemischte Mehrkonten-Datei vollständig ab - sie TRENNT sie
  nicht automatisch nach zugelassenen Konten auf. Ein künftiger EBICS-
  C53-Adapter, der eine kundenweite Sammeldatei mit MEHREREN eigenen,
  zugelassenen Konten sinnvoll verarbeiten soll (statt sie pauschal
  abzulehnen, nur weil sie mehr als ein Konto enthält), muss selbst je
  `Stmt`/`Acct` gruppieren, gegen eine Allowlist zugelassener Konten
  prüfen und für JEDES zugelassene Konto einen eigenen, isolierten
  Import-Aufruf mit dessen eigener `erwartete_iban` auslösen - das
  existiert heute nicht und ist für den aktuellen manuellen
  Einzelkonto-Dateiupload auch nicht nötig. Diese Session hat KEINE
  EBICS-Anbindung implementiert (weiterhin nur manueller CAMT/CSV-
  Dateiupload); CSV-Import und der synthetische Demo-Ablauf bleiben
  unverändert (die Konto-Validierung betrifft ausschließlich
  `parse_camt053`).
- **Mail-/Dokumentversand:** Mahnwesen erzeugt nur eine Outbox
  (Preview/Snapshot), `SEND_ENABLED=false` per Default. Ein realer
  Versandadapter (SMTP/Provider) mit Zustellbestätigung ist nicht
  angebunden; die Schnittstelle (`mahnwesen/service.py`) ist so
  geschnitten, dass ein Adapter andocken kann, ohne die Statuslogik zu
  ändern.
- **Dokumentzustellung Vorschreibung:** `dokument_zustellen()`/
  `hauptbuch_exportieren()` verlangen zwar jetzt einen echten,
  nicht-leeren Nachweis (`zustellnachweis`/`export_nachweis`) und lehnen
  sonst ab — aber es gibt keinen Adapter, der diesen Nachweis tatsächlich
  ERZEUGT (PDF-Rendering, Mailversand, Hauptbuch-Zielsystem). Der Nachweis
  muss aktuell von außen (Mensch oder künftiger Adapter) beigebracht
  werden.
- **Auth/Login:** `auth/service.py` bildet Rollen- und
  Gesellschafts-Scoping als Autorisierungsschicht ab; ein echtes
  Login/Session-Handling (z. B. OAuth, Passwort-Reset) ist nicht Teil
  dieser Lieferung und muss vor Internet-Exposition ergänzt werden.
  `api/app.py` ist bis dahin mit einem einzigen geteilten
  `MIETINKASSO_API_TOKEN` (X-API-Key-Header) "closed by default"
  abgesichert — das ist eine Übergangslösung für einen internen
  Operator, KEINE Mandantentrennung pro Endanwender/Gesellschaft auf
  HTTP-Ebene.
- **Bank-Zuordnung: Atomarität behoben, Restrisiko bei echter
  Nebenläufigkeit bleibt dokumentiert:** OP-Buchung
  (`op_service.buchen`), Zuordnungserstellung
  (`bank_repo.create_zuordnung`) und Audit laufen seit der
  Codex-Rückprüfung in EINER gemeinsamen DB-Transaktion
  (`BankImportService._zuordnen_atomar`/`_importiere_atomar`): schlägt
  irgendein Schritt fehl — auch ein unerwarteter Fehler wie der von
  Codex simulierte `RuntimeError` nach erfolgreicher OP-Buchung —, wird
  die GESAMTE Transaktion zurückgerollt; Konto, Transaktion und
  Original-OP werden dabei serverseitig frisch geladen, nicht aus vom
  Aufrufer übergebenen (potenziell veralteten) Objekten übernommen.
  Regressionstest: `test_zuordnung_fehler_nach_op_buchung_rollt_alles_zurueck`
  in `tests/mietinkasso/test_bank.py`. Mehrere echte Teilzuordnungen mit
  zufällig identischem Betrag sind über eine vom Aufrufer vergebene,
  eindeutige `vorgang_id` von bloßen Retries unterscheidbar
  (`test_vorgang_id_unterscheidet_retry_von_unabhaengiger_teilzuordnung`).
  **Korrektur (Codex-Rückprüfung Paket B, 12.09.):** eine frühere Version
  dieses Punkts behauptete hier fälschlich, SQLite-Schreibtransaktionen
  seien für ein SELECT-dann-INSERT wie den Restbetrags-Check "ohnehin
  serialisiert" und ein Doppelbuchungsfenster damit unter SQLite gar
  nicht beobachtbar. Das ist FALSCH: `session.get(..., with_for_update=True)`
  ist unter SQLite ein reines Kein-Op (der Dialekt kompiliert `FOR
  UPDATE` weg), und eine gewöhnliche SQLite-Transaktion nimmt ihren
  Schreib-Lock (DEFERRED) erst bei ihrem ERSTEN Schreibbefehl, nicht bei
  ihrem ersten Lesebefehl — zwei echte, gleichzeitige Datei-SQLite-
  Verbindungen konnten dieselbe, noch nicht committete Zwischensumme
  lesen, bevor eine von beiden schreibt (von Codex mit zwei echten
  Threads auf `verknuepfe_mit_bestehender_zahlung` reproduziert:
  zusammen mit einem bereits bestehenden 100-EUR-Link hätten zwei
  gleichzeitige 600-EUR-Verknüpfungsversuche auf eine 1.000-EUR-Zahlung
  BEIDE durchgehen und sie auf 1.300 EUR überzeichnen können).

  **Behoben** für die drei Bank-Schreibpfade, die exakt dieses "lesen,
  dann entscheiden, dann schreiben"-Muster verwenden -
  `BankImportService._zuordnen_atomar`, `verarbeite_ruecklastschrift`,
  `verknuepfe_mit_bestehender_zahlung` UND `_importiere_atomar` (Datei-
  Import je Aufruf, ergänzt beim kurzen Nutzer-Check vor Paket C - siehe
  unten) — durch
  `infrastructure/db/sqlite_write_lock.py::schreibgesperrte_session`:
  unter Datei-SQLite läuft die jeweilige Transaktion über eine eigene,
  dedizierte Verbindung zur selben Datei, deren ERSTES Statement
  `BEGIN IMMEDIATE` ausführt (nimmt den Schreib-Lock sofort statt erst
  beim Schreibbefehl) — eine zweite gleichzeitige kritische Sektion
  blockiert (bis zum sqlite3-Busy-Timeout), statt denselben veralteten
  Zwischenstand zu lesen. PostgreSQL bleibt davon unberührt (dort
  gelten weiterhin die bestehenden `with_for_update=True`-Zeilensperren
  unverändert als korrekt); für `:memory:`-Datenbanken (Unit-Tests) ist
  die Umschaltung wirkungslos, aber auch nicht nötig (dort teilt ein
  `StaticPool` ohnehin eine einzige Verbindung). Regressionstests:
  `tests/mietinkasso/test_sqlite_write_lock.py` (deterministischer
  Primitiv-Test über zwei per `threading.Event` kontrolliert
  synchronisierte Threads — ein reiner Zwei-Thread-Business-Logik-Test
  kann eine echte Interleaving-Race nicht zuverlässig/wiederholbar
  erzwingen) sowie
  `test_verknuepfen_datei_sqlite_gleichzeitige_verknuepfungen_ueberschreiten_zahlung_nicht`
  in `tests/mietinkasso/test_bank.py` (Integrationstest mit echter
  Datei-SQLite-DB, zwei echten Threads, `threading.Barrier`).

  **Ergänzung (Nutzer-Check vor Paket C):** kurz geprüft, ob
  `_importiere_atomar` durch vorhandene Unique Constraints bereits
  ausreichend geschützt ist. Ergebnis differenziert: Zeilen MIT
  bankseitig eindeutiger `native_id` sind unabhängig vom Locking sicher
  (echter DB-`UNIQUE`-Constraint `uq_bank_import_id` auf `import_id` -
  eine zweite, echt gleichzeitige Transaktion mit demselben `import_id`
  schlägt dort so oder so mit einem Constraint-Fehler fehl statt still
  zu duplizieren). Zeilen OHNE `native_id` (reiner CSV-Fingerprint-
  Import) waren dagegen NICHT geschützt: ihre Dublettenprüfung
  (`find_by_fingerprint`) ist ein reines SELECT-dann-Entscheiden ohne
  DB-Backstop (`fingerprint_hash` ist nur indiziert, nicht `UNIQUE`) -
  zwei echt gleichzeitige Importe derselben Datei ohne eindeutige
  Kennung hätten sich gegenseitig als "noch nicht vorhanden" sehen und
  beide dieselbe wirtschaftliche Zahlung einbuchen können (reproduziert
  mit zwei echten Threads). Daher ebenfalls auf `schreibgesperrte_session`
  umgestellt. Regressionstest:
  `test_importiere_atomar_datei_sqlite_gleichzeitiger_identischer_csv_import_dupliziert_nicht`
  in `tests/mietinkasso/test_bank.py`.

  Weiterhin offen: unter PostgreSQL bleibt der `SELECT ... FOR UPDATE`-
  Row-Lock für `_zuordnen_atomar`/`verarbeite_ruecklastschrift`/
  `verknuepfe_mit_bestehender_zahlung` die einzige Absicherung (dort
  technisch korrekt, aber ungetestet gegen ein echtes Postgres); der
  `import_id`-`UNIQUE`-Constraint für `_importiere_atomar` gilt
  datenbankunabhängig.
- **Rücklastschrift-Validierung verschärft:** `verarbeite_ruecklastschrift`
  verlangt jetzt einen tatsächlich negativen Bankeingang (keine
  wiederverwendete positive Zahlungstransaktion — Regressionstest
  `test_ruecklastschrift_lehnt_dieselbe_positive_transaktion_ab`), ein
  zur Ursprungszahlung passendes Bankkonto, übereinstimmende
  Gesellschaft/Währung, eine kumulative Rückbuchungsgrenze der
  Ursprungszahlung sowie einen begrenzten verfügbaren Belastungsbetrag
  der Rücklastschrift-Transaktion selbst (letztere zwei jeweils mit
  eigenem Regressionstest in `tests/mietinkasso/test_bank.py`).
- **Bankvollständigkeit ist eine explizite Bestätigung, kein
  Datumsschluss:** Das bloße Vorhandensein einer aktuellen
  Banktransaktion (`bankstand_alter_tage`) beweist KEINE
  Exportvollständigkeit. Mahnwesen prüft daher `bank_bestaetigt_bis`
  aus der separaten `BankVollstaendigkeitTable`
  (`bestaetige_bankvollstaendigkeit`/`bankvollstaendigkeit_bestaetigt_bis`)
  — eine explizite menschliche/prozessuale Bestätigung "Import
  lückenlos bis Datum X" — und blockiert zusätzlich bei relevanten,
  auf den Vertrag referenzierten, aber noch nicht vollständig
  zugeordneten Bankeingängen (`hat_ungeklaerte_relevante_eingaenge`).
  Beides muss vor jedem Mahnlauf aktiv gepflegt/aufgerufen werden — es
  gibt (bewusst) keine automatische Herleitung "Bank ist vollständig".
- **CAMT-Sammelbuchungen (mehrere TxDtls):** werden nur automatisch
  aufgeteilt, wenn JEDE TxDtls einen eigenen Betrag trägt, der exakt auf
  den Ntry-Gesamtbetrag aufsummiert; alles andere (unvollständige
  Teilbeträge, abweichende Rundungsdifferenzen) wird bewusst mit
  `CamtMehrteiligeBuchungError` zur manuellen Klärung verweigert statt
  geraten.
- **Company OS / 7d-invoice / jlb-cockpit:** Die Beziehung dieses
  Repos zu den auf jlb-hetzner geprüften Systemen ist laut Auftrag
  NICHT belegt und wurde hier nicht angenommen. Eine etwaige spätere
  Anbindung (z. B. Hauptbuch-Export-Zielsystem) ist ein offener
  Architekturpunkt für Codex/Markus.

## Fachlich (bewusst konservativ gehalten)

- **Index-Rechtsprofile:** `index/service.py` implementiert genau EIN
  `berechnungsprofil`: `EINFACHER_SCHWELLENVERGLEICH` (Basisreihe/-wert/
  -monat, Schwelle inklusive/exklusiv konfigurierbar, Dämpfung,
  vertragliche Grenze). Jede `IndexKlausel` mit einem anderen
  `berechnungsprofil` wird von `klausel_anlegen`/`berechne_vorschlag`
  technisch mit `RechtsprofilNichtImplementiertError` GESPERRT, nicht nur
  dokumentiert. Die MieWeG-2026-spezifischen Detailregeln (April-Termine,
  Jahresdurchschnittsbildung, 1 %-/2 %-Schwellenlogik, anteilige
  Erstvalorisierung, Altvertragsübergang) sind damit ausdrücklich NICHT
  als Parameter des bestehenden Profils abbildbar — sie erfordern
  jeweils ein eigenes, gegen die RIS-Quellen (NOR40274266, NOR40274269)
  geprüftes und freigegebenes Berechnungsprofil, das erst noch gebaut
  werden muss, bevor eine Klausel damit wirksame Vorschläge erzeugen
  darf.
- **BK-Umlageschlüssel:** `bk/service.py` erwartet den
  Verteilungsschlüssel (Anteil in %) je Vertrag als geprüfte Eingabe;
  eine automatische Herleitung aus Fläche/Miteigentumsanteilen/
  Verbrauch für alle Fallkonstellationen (insb. Mieterwechsel
  unterjährig, Verbrauchsabrechnung Heizung/Wasser) ist nicht
  gebaut und bleibt bewusst ein manueller/geprüfter Schritt.
- **Mahntexte:** Das Mahnwesen erzeugt Snapshot-Daten (Betrag,
  Empfänger, Regelversion), aber keine rechtlich geprüften Brieftexte;
  diese sind vor Aktivierung von `SEND_ENABLED=true` fachlich
  freizugeben.

- **Zinslisten-Import:** kein automatischer CSV/Excel-Parser für
  `VertragsKomponenteTable` (HMZ/Küche/Parkplatz/BK-VZ/...); siehe
  `importtemplates/README.md`. `StammdatenRepository.add_komponente`
  nimmt die Felder bereits programmatisch entgegen, ein Parserlayer für
  das reale Format fehlt bewusst, bis das Quellenmapping steht.

## Technisch (nächste Ausbaustufe, nicht MVP1-blockierend)

- Überholt seit dem Backoffice-Piloten: Es gibt inzwischen ein
  server-gerendertes, session-authentifiziertes Backoffice mit
  schreibenden HTTP-Endpunkten unter `/backoffice/*`
  (`backoffice/app.py`, EIN lokaler Login, "closed by default" ohne
  `MIETINKASSO_BACKOFFICE_PASSWORD_HASH`) sowie automatisierte
  HTTP-Integrationstests dafür (`tests/mietinkasso/test_backoffice.py`,
  `fastapi.testclient.TestClient` gegen eine echte Datei-SQLite-DB;
  `httpx`/`python-multipart` sind jetzt Abhängigkeiten). Die
  Read-Only-Endpunkte unter `/v1/*` (`api/app.py`) bleiben unverändert
  Token-geschützt und weiterhin ohne eigene HTTP-Tests - dort gilt der
  alte Hinweis fort: sollte vor einem produktiven Einsatz ergänzt werden.
  Weiterhin fehlt ein Mehrbenutzer-/Rollen-Login (siehe
  "Backoffice-Pilot" oben) und ein REST/HTML-Frontend mit echter
  Benutzerverwaltung außerhalb des lokalen Ein-Operator-Piloten.
- Keine Observability (Metriken/Alerting) über structlog hinaus.
- SQLite als Default-URL für Entwicklung/Tests; PostgreSQL wird über
  `MIETINKASSO_DATABASE_URL` unterstützt (SQLAlchemy-Engine ist
  DB-agnostisch), aber nicht gegen ein echtes Postgres getestet.
- `JobRunner` (generische Job-Sperre) ist ein Zusatzbaustein für Jobs
  ohne natürliche Eindeutigkeit; die eigentliche Doppel-Buchungs-/
  Doppel-Versand-Sicherheit kommt aus den DB-Unique-Constraints in
  `vorschreibungen` (Vertrag+Monat) und `mahn_faelle` (outbox_key) plus
  dem atomaren Compare-and-Swap in `MahnFallRepository.claim_fuer_versand`.

## Paket D — EBICS-Downloadclient (Nutzerauftrag 12.09., eigenständiges PHP-Modul)

Vollständig separates Modul `ebics-downloader/` (eigener Composer-Baum,
eigene Tests, kein Import von/nach `src/mietinkasso/` oder
`src/invoice_automation/`) - ein isolierter EBICS-Downloadclient für
camt.053-Kontoauszüge auf Basis der MIT-lizenzierten Bibliothek
`ebics-api/ebics-client-php`. Vollständige Betriebsgrenzen, offene
Punkte und Architektur stehen in `ebics-downloader/README.md`
(Abschnitt „Betriebsgrenzen“) und `ebics-downloader/docs/ONBOARDING_EBICS.md`
- hier nur die Kurzfassung für den Querverweis aus dem
Mietinkasso-Kontext:

- **Kein Cutover.** Dieses Paket liefert ausschließlich ein
  Übergabeverzeichnis (`release_dir`) mit geprüften, nach Objekt
  getrennten camt.053-Einzelstatements. Ein automatischer Import
  dieser Dateien in `src/mietinkasso/` (Ablösung der bestehenden
  40 synthetischen CSV-Bewegungen im Demo-Seed als Bankquelle) ist
  ausdrücklich NICHT Teil dieses Auftrags und erfordert einen eigenen,
  separat zu beauftragenden und unabhängig zu prüfenden Schritt.
- **Kein echter Bankkontakt in dieser Sitzung.** 80 synthetische
  PHPUnit-Tests decken Konfigurationsvalidierung, Keyring-Guard,
  CAMT-Sicherheitsschicht (DTD/Entity, ZIP-Grenzen/Pfadtraversal/
  Symlink, IBAN-Trennung) und Replay/Konflikt/Prozesssperre ab - das
  ist KEINE Live-Bank-Abnahme.
- **Docker-Image ungetestet** (kein Docker-Daemon in dieser
  Entwicklungssitzung verfügbar) und ein in dieser Sitzung verifiziertes
  echtes Upstream-Problem bei `ebics-api/ebics-client-php` 3.2.1
  (Packagist-Commit-Referenz veraltet, Composer-Fix über einen
  direkten VCS-Repository-Eintrag) - Details in
  `ebics-downloader/README.md`.
- **Restrisiko Dekompressionsbombe** vor dem eigenen `ackClosure`-Hook,
  da die bibliothekseigene Transportdekompression zwingend davor
  läuft - dokumentiert und über einen HTTP-Transportgrößen-Cap
  (`Http\SizeCappedCurlHttpClientFactory`) so weit wie mit der
  öffentlichen Bibliotheks-API möglich begrenzt, aber nicht
  vollständig ausschließbar.
- **Onboarding-Reihenfolge:** bestehende, bereits verschlüsselte
  Teilnehmer-Keyrings (Volksbank/Raiffeisen Wien laut Markus bereits
  vorhanden) haben Vorrang vor jeder Neuanlage; ein tatsächliches
  Onboarding führt ausschließlich ein Mensch durch (Claude hat keinen
  Bankzugriff).

## Paket Indexautomatik (Auftrag 13.09.2026, HV-20260913-INDEXAUTOMATIK)

Neues Modul `src/mietinkasso/indexautomatik/` - monatliche MieWeG-/
MRG-Indexprüfung, Erhöhungsschreiben-Outbox mit Zugangs-/
Zahlungspflicht-Tracking, Vertragsende-Erinnerung (Owner-only). Der
vollständige, wortgetreue Auftrag inkl. der fachlichen Nachträge und des
Modellreviews steht in `RAHMENPROGRAMM.md` (Abschnitt
"HV-20260913-INDEXAUTOMATIK"). Dieser Abschnitt listet, was geliefert
ist und WAS ES BEWUSST NICHT TUT.

### Was funktioniert (mit Tests belegt)

- **Monatlicher, idempotenter Lauf je (Vertrag, Kalendermonat)**
  (`IndexautomatikLaufRepository.claim_periode`/`abschliessen`) - ein
  atomarer Erzeugungsclaim läuft VOR jeder seiteneffektbehafteten
  Berechnung, nicht erst am Ende. Ein begründet BLOCKIERTer oder
  TERMIN_NICHT_ERREICHTer Lauf bleibt im selben Monat erneut versuchbar
  (z. B. nach nachgetragener VPI-Publikation), ein bereits
  abgeschlossener (ERHOEHUNG_ERZEUGT/KEIN_ERHOEHUNGSBEDARF/
  BEREITS_ERFASST) nicht.
- **Zwei Berechnungspfade, beide Wiederverwendung bestehender,
  abgenommener Rechner** - kein paralleler Rohrechner:
  - MieWeG-Wohnungsrechner (`mieweg_vorschau_service.vorschau_erstellen`)
    für MRG-Voll-/Teilanwendung mit bestätigter Wohnungsnutzung. Haupt-
    UND geprüfte Untermiete laufen darüber (MieWeG deckt
    Wohnungsuntermiete ausdrücklich mit ab) - nur eine UNGEKLÄRTE
    Haupt-/Untermiete-Einordnung sperrt.
  - Vertragsklausel-Pfad (`index/service.py::IndexService.
    berechne_vorschlag` über eine versionierte, freigegebene
    `IndexKlauselTable`) für jeden anderen Fall mit geprüfter Klausel,
    insbesondere Geschäftsraum - ohne April-Bindung und ohne
    künstliche Einmal-pro-Jahr-Grenze (partieller Unique-Index nur für
    den MieWeG-Pfad).
- **Rechtsprofil-Versionierung mit automatischer Entwertung**
  (`RechtsprofilService`) - `quelle_hash` bindet die Freigabe an
  Vertrag, referenzierte Komponenten UND eine ggf. referenzierte
  IndexKlausel; jede Änderung entwertet die Freigabe beim nächsten
  Zugriff automatisch. Eine verstrichene `mietzinsobergrenze_gueltig_bis`
  entwertet zusätzlich zeitbasiert. Eine harte Mietzinsobergrenze wirkt
  als Kappung UNABHÄNGIG von `foerderbindung`.
- **Outbox mit echtem Doppelversand-Schutz**: atomarer CAS BEREIT->
  IN_VERSAND (analog Mahnwesen), Recovery für verwaiste IN_VERSAND-
  Fälle, erneute Prüfung von Empfänger/Vertragsstatus/Rechtsprofil-
  Gültigkeit/Wirksamkeitstermin UNMITTELBAR vor jedem Versand. Diese
  erneute Prüfung bindet mittlerweile auch ALLE im Schreiben
  ausgewiesenen Komponenten - auch unveränderte Positionen wie BK/HK
  - gegen ihren aktuellen Stammdatenstand: ändert sich z. B. eine
  NICHT referenzierte BK-Vorauszahlung zwischen Entwurf und Versand,
  blockiert der Versand statt mit einem veralteten Gesamtbetrag zu
  senden (unabhängig reproduziert und behoben). Ein BLOCKIERTes (noch
  nicht versendetes) Erhöhungsschreiben wird nach behobener Quelle IN
  PLACE aktualisiert statt den ganzen April-Zyklus dauerhaft zu
  blockieren. Mehr als eine referenzierte Basis-Komponente blockiert
  bewusst vor Versand (keine erfundene Verteilungsregel). Ohne
  konfigurierten echten Transport-Endpunkt ist der Versand (sowohl
  Backoffice-Route als auch CLI) HART gesperrt - es gibt an keiner
  produktiven Stelle einen Fake-/Test-Transport-Fallback mehr.
- **Zugangs-/Zahlungspflicht-Logik**: eine bloß versendete, unbestätigte
  E-Mail gilt nie automatisch als fristauslösender Zugang; die
  automatische 14-Tage-Berechnung nach § 16 Abs 9 MRG ist auf
  MRG-VOLLANWENDUNG begrenzt (NICHT MRG-Teilanwendung - eine
  Teilanwendung hat kein pauschal geprüftes Fristenprofil). Diese
  Sperre greift bereits beim Versandversuch selbst, nicht erst bei der
  späteren Zugangsbestätigung - ein Mieterschreiben mit ungeklärter
  Fristenlage geht dadurch nie automatisch heraus, auch nicht als
  "GESENDET" mit ungeklärtem Folgezustand.
- **VertragsendeErinnerungService**: Trigger exakt drei KALENDERmonate
  vor Vertragsende (eigene Monatsarithmetik, korrekt für Schaltjahr/
  Monatsende), Empfänger hart `Settings.owner_email`, kein
  Mieter-Fallback/CC. Ein Mieterentwurf entsteht ausschließlich NACH
  einer gespeicherten Entscheidung und wird NIE automatisch versendet.
  Ein deaktivierter Versand markiert nichts als benachrichtigt (bleibt
  OFFEN), damit ein später echt aktivierter Lauf nichts verliert.
- **Nativer Statistik-Austria-OGD-Parser** (`vpi_import.py`) - gegen
  vom Betreiber tatsächlich heruntergeladene, echte Dateien geprüft
  (925 Gesamtindex-Zeilen aller vier Reihen korrekt importiert,
  Atomarität bei Fehlerzeile bestanden). Finalität wird aus der
  Publikationsregel abgeleitet (jüngster Monat vorläufig, frühere
  endgültig; ein Jahresdurchschnitt gilt erst ENDGUELTIG, wenn dieselbe
  Reihe auch den Jänner des Folgejahres enthält), nicht aus einer
  Statusspalte (die gibt es im echten Format nicht). Quell-Hash/-URL/
  Abrufzeit werden je Zeile gespeichert.
- **Gesellschaftsscope-Filter in den Batchläufen**
  (`IndexautomatikService.monatslauf_alle`,
  `VertragsendeErinnerungService.plane_alle`) - ein Vertrag außerhalb
  von `ctx.gesellschaft_ids` wird VOR jedem Zugriff komplett
  übersprungen (kein `CrossTenantError` mehr, das in einer
  BLOCKIERT-Fachlaufzeile landen konnte).
- **Mietzinsobergrenze-Pflicht bei bestätigter MRG-Zinsbeschränkung**:
  eine bestätigte MRG-Voll-Zinsbeschränkung OHNE erfasste
  Mietzinsobergrenze blockiert die Freigabe - keine fiktiv
  unbegrenzte Weitergeltung.
- **MieWeG-Gesetzesspur ist auf VPI20C18 fixiert** (`vpi_reihe` wird
  nur vom MieWeG-Wohnungsrechner-Pfad gelesen); eine vertragliche,
  ggf. abweichende Reihe gehört als eigenständige, versionierte
  IndexKlausel erfasst.
- **StatistikAustriaClient ist in den Monatslauf verdrahtet**: bei
  aktivem `MIETINKASSO_INDEXAUTOMATIK_VPI_AUTOMATISCHER_ABRUF` werden
  die vier amtlichen Reihen VOR der Monatsprüfung abgerufen/importiert;
  schlägt das fehl, scheitert der gesamte Lauf sichtbar (JobLockTable
  FEHLGESCHLAGEN) statt mit veralteten Werten weiterzurechnen.
- **Backoffice zeigt interne Blockiert-Gründe und volle Texte**: eine
  eigene Lauf-Übersicht listet auch Fälle, die nie bis zur Outbox
  kommen (samt Blockiert-Gründen); Erhöhungsschreiben und
  Mieterentwurfstexte sind im Klartext einsehbar, nicht nur als
  Statushinweis. Portaltexte nennen keine Umgebungsvariablen- oder
  Skriptpfade mehr.
- 665 Tests insgesamt für dieses Repository (Stand Abschlusscommit;
  651 im ursprünglichen Übergabecommit, seither um gezielte
  Regressionstests für die oben genannten Korrekturen ergänzt).

### Was ausdrücklich NICHT geliefert ist (bewusste, offen benannte Lücken)

- **Automatisches Nachziehen von Sollstellung/Vorschreibung existiert
  inzwischen** (Auftrag HV-20260913-VERSAND-SOLL, siehe eigener
  Abschnitt weiter unten) - `SOLL_UMSETZUNG_OFFEN` ist damit kein
  dauerhafter Endzustand mehr, sondern wird von
  `indexautomatik/umsetzung_service.py::umsetzen()` (Backoffice-Button
  oder täglicher Worker, beide hinter
  `MIETINKASSO_INDEXAUTOMATIK_SOLL_UMSETZUNG_ENABLED`, Default AUS) in
  eine tatsächliche Komponenten-/Rechtsprofiländerung überführt. Der
  Status heißt weiterhin `SOLL_UMSETZUNG_OFFEN` (statt des im
  ursprünglichen Auftragstext genannten "ausgeführt") - siehe
  Begründung im Abschnitt zum Auftrag HV-20260913-VERSAND-SOLL.
- **Geschäftsraum-/Klausel-Pfad erzeugt aktuell KEIN automatisches
  Erhöhungsschreiben.** Der Rechenweg über `index/service.py` läuft
  real und ist an die fachliche Anpassung gebunden (identische Basis+
  amtlicher VPI-Wert erzeugt keinen zweiten Vorschlag), aber es gibt
  keinen unabhängig verifizierten automatischen Wirksamkeits-/
  Zinstermin für diesen Pfad (keine "Jännervorgabe" oder vergleichbare
  Konvention wurde primärquellenbelegt bestätigt) - jeder Fall mit
  einer echten Erhöhung bleibt sichtbar BLOCKIERT mit Verweis auf die
  konkrete `IndexAnpassungTable`-Zeile, bis ein Termin manuell bestätigt
  wird. Kein erfundenes Datum wird verwendet.
- **Förderrechtliche Mietzinsobergrenze wirkt nur im MieWeG-
  Wohnungsrechner-Pfad**, nicht (noch) im Geschäftsraum-/Klausel-Pfad -
  in der Praxis dürfte Förderbindung überwiegend Wohnungen betreffen,
  aber das ist keine geprüfte Rechtsaussage, nur eine bewusste
  Scope-Begrenzung.
- **Statistik-Austria-URLs für VPI15C18/VPI00/VPI96 sind NICHT
  einzeln bestätigt** - nur das Namensmuster der verbal bestätigten
  VPI20C18-URL wurde übernommen. Codex hat die vier Original-Dateien
  inzwischen unabhängig heruntergeladen und das Schema bestätigt; die
  konkreten URLs für die drei übrigen Reihen sollten vor produktivem
  automatischem Abruf trotzdem gegengeprüft werden.
- **`StatistikAustriaClient` (HTTP-Abruf) wurde von Claude NICHT gegen
  den echten Endpunkt ausgeführt** - Claude hat keinen Internetzugriff.
  Codex hat die vier amtlichen OGD-Original-URLs unabhängig geprüft
  (PUBLIC_CLIENT_REVIEW). Der Client ist jetzt in
  `scripts/indexautomatik_monatslauf.py` verdrahtet (bei aktivem Flag
  vor der Monatsprüfung, mit sichtbarem Scheitern des gesamten Laufs
  bei Fehler) - `MIETINKASSO_INDEXAUTOMATIK_VPI_AUTOMATISCHER_ABRUF`
  bleibt trotzdem Default aus, bis Codex den scharfen Betrieb final
  freigibt.
- **Kein echter Transport-Anbieter angebunden.** `HttpTransportadapter`
  implementiert einen dokumentierten, generischen Request-/Response-
  Vertrag; die tatsächliche JLB-MailOps-Strecke (gestaltete HTML-
  Signatur mit Original-Logo, bestehende Mailstrecke) verlangt laut
  Betreiber eine ECHTE manuelle CLICK_RELEASED-Freigabe und führt
  Hausverwaltung noch NICHT in ihrer Mailbox-Allowlist (Stand
  13.09.2026) - diese Freigabe wird hier an keiner Stelle simuliert.
  Realer Versand bleibt an ZWEI unabhängige Flags
  (`MIETINKASSO_INDEXAUTOMATIK_SEND_ENABLED` UND
  `MIETINKASSO_INDEXAUTOMATIK_MAILOPS_ALLOWLIST_BESTAETIGT`) und einen
  konfigurierten Endpunkt gebunden, alle Default aus/leer. Codex hat
  inzwischen (eb3b126, 81 Tests) den echten MailOps-Serverdienst
  produktiv ergänzt (Unix-Domain-Socket, drei Kanäle weiterhin
  deaktiviert) - die Anbindung DIESES Moduls an genau diesen Dienst
  (`httpx.HTTPTransport(uds=...)`, `POST /v1/send`, `GET /v1/status`,
  `GET /v1/health`) ist als eigener, separater Folge-Commit geplant
  (siehe Abschnitt zum Auftrag HV-20260913-VERSAND-SOLL), NICHT Teil
  dieses Commits.
- **Backoffice-UI deckt keinen CSV-Datei-Upload für den OGD-Import ab**
  - die Bedienoberfläche bietet nur den manuellen Jahreswert-Override
    (mit Beleg); der native Datei-Import läuft ausschließlich über
    `vpi_import.py`/`StatistikAustriaClient`, aktuell nur aus einem
    Skript/Python-Kontext aufrufbar, nicht über eine Backoffice-Route.
- **Kein Phase-2-Abschlussbeleg über `mieweg_vorschau_service`.** Nach
  bestätigtem Zugang wird `ausfuehrbare_erhoehung_cent` nicht durch
  einen zweiten `vorschau_erstellen`-Aufruf mit jetzt vorliegendem
  Zustellnachweis nachgezogen (`ErhoehungsschreibenTable.
  mieweg_vorschau_final_id` existiert als Spalte, wird aber nicht
  befüllt) - der Audit-Trail für "vollständig belegt" besteht
  stattdessen aus `zugang_beleg`/`zugang_bestaetigt_am`/
  `zahlungspflicht_ab` auf dem Erhöhungsschreiben selbst.
- **Keine Migrationsspalten-Löschung/-Umbenennung.** Wie im gesamten
  Repository (kein Migrationswerkzeug, siehe Paket A) sind alle neuen
  Tabellen additiv; `create_all_tables()` legt fehlende Tabellen an
  UND zieht seit HV-20260913-VERSAND-SOLL (`infrastructure/db/
  migrations.py::ensure_additive_columns`, Codex-Rückmeldung: bereits
  befüllte `rechtsprofile`/`erhoehungsschreiben` bekommen sonst keine
  neuen Spalten) auch fehlende SPALTEN bestehender, bereits befüllter
  Tabellen additiv per `ALTER TABLE ... ADD COLUMN` nach - niemals eine
  bestehende Spalte ändert/löscht/umbenennt, niemals eine
  Fremdschlüsselspalte (dafür bricht die Funktion sichtbar ab statt
  eine unsichere inline-REFERENCES-Klausel zu raten).
- **Rundungs-/Berechnungsdetails synthetisch, nicht amtlich
  gegengeprüft**: §1 Abs2 Z1/§1 Abs4 MieWeG und §16 Abs9 MRG wurden in
  einer früheren Sitzung nicht selbst gegen `ris.bka.gv.at` verifiziert
  (Netzwerk-Egress blockiert), sondern auf primärquellenbelegte
  Hinweise übernommen (siehe Paket-C-Abschnitt oben). Die MRG-Teil-
  Formulierung im Erhöhungsschreiben zitiert § 16 Abs 9 deshalb bewusst
  qualifiziert statt zuversichtlich.

## Paket Soll-Umsetzung (Auftrag 13.09.2026, HV-20260913-VERSAND-SOLL)

### Was funktioniert (mit Tests belegt)

- **Atomare, versionierte Soll-Umsetzung** (`indexautomatik/
  umsetzung_service.py::IndexSollUmsetzungService.umsetzen`): der
  bisher fehlende letzte Schritt der Indexautomatik-Pipeline
  (`SOLL_UMSETZUNG_OFFEN` -> tatsächliche Änderung von
  `VertragsKomponenteTable`/Rechtsprofil-Basis). Claim (CAS
  `SOLL_UMSETZUNG_OFFEN`/`SOLL_UMSETZUNG_BLOCKIERT` ->
  `SOLL_UMSETZUNG_IN_PRUEFUNG`), vollständige Re-Validierung gegen den
  AKTUELLEN Stand (Vertragsstatus, Komponenten-Stale-Snapshot,
  Rechtsprofil-Stale-Snapshot über denselben `quelle_hash`-Mechanismus
  wie die Freigabe, bereits gebuchte Folgeperiode), Komponenten-
  Historisierung (alte Zeile per `gueltig_bis` geschlossen, NIE
  in-place geändert), neue Rechtsprofil-Version (alte wird
  `INVALIDIERT`, genau wie bei einer manuellen Freigabe) und
  Ausführungsnachweis (`IndexSollUmsetzungTable`, genau eine Zeile je
  Erhöhungsschreiben) laufen in EINER einzigen DB-Transaktion - ein
  Absturz/Parallelstart ergibt nachweislich genau eine Änderung (10
  dedizierte Tests: Erfolg, Replay, paralleler Claim, zwei Stale-
  Snapshot-Varianten, falscher Mandant, unveränderte BK-Komponente
  bleibt unangetastet, Zugang/Zeitpunkt, bereits gebuchte Folgeperiode,
  Transaktionsabbruch mitten in der Umsetzung).
- **`komponenten_verteilung`** auf `ErhoehungsschreibenTable`: die
  centgenaue Alt-/Neu-Zuordnung auf GENAU EINE Komponente, befüllt von
  `outbox_service.erstellen_aus_mieweg`/`erstellen_aus_index_anpassung`
  immer dann, wenn (wie bei jedem nicht blockierten Schreiben) exakt
  eine Komponente referenziert ist - leer bei Mehrkomponenten-Fällen,
  die `umsetzen()` dann konsequent verweigert statt zu schätzen.
- **Konfigurierbares Fristenprofil** (`RechtsprofilTable.
  frist_tage_zugang_bis_wirksamkeit`/`frist_quellenbeleg`, über
  `RechtsprofilService.entwurf_anlegen` setzbar): hebt die pauschale
  Sperre für nicht unterstützte Rechtsordnungen (Gewerbe-/
  Jännerklausel u. Ä.) GEZIELT für ein einzelnes, belegtes Rechtsprofil
  auf (sowohl in `versenden()` als auch in `zugang_bestaetigen()`),
  OHNE das bisherige pauschale 14-Tage-MRG-Voll-Verhalten für
  unveränderte Fälle zu berühren. Fließt jetzt auch in den
  `quelle_hash` ein (Drift-Erkennung wie jedes andere Profilfeld).
- **Additive Spaltenmigration für bereits produktiv befüllte
  Datenbanken** (`infrastructure/db/migrations.py::
  ensure_additive_columns`, in `create_all_tables()` verdrahtet) - vier
  dedizierte Tests gegen eine handgebaute Altschema-Fixture (volle
  Spaltenliste von `rechtsprofile`/`erhoehungsschreiben` VOR diesem
  Auftrag, mit befüllten Zeilen): Spalten werden nachgezogen, bestehende
  Zeilenwerte bleiben unverändert, Idempotenz bei wiederholtem Lauf.
- **Backoffice-Ansicht** unter `/backoffice/indexautomatik/soll-
  umsetzung` (Liste + Detail/Vorschau + POST `/umsetzen`,
  authentifiziert/CSRF-geschützt/gesellschaftsscope-geprüft, GET rein
  lesend) - zeigt Stand, konkreten Blockiergrund bzw. Vorschau Soll
  alt/neu ab Datum. `MIETINKASSO_INDEXAUTOMATIK_SOLL_UMSETZUNG_ENABLED`
  (Default AUS) sperrt sowohl den Backoffice-Button als auch den
  täglichen Worker uniform (Flag wird in `umsetzen()` selbst geprüft,
  nicht nur im Aufrufer) - ein Integrationstest belegt, dass der Button
  bei deaktiviertem Flag wirkungslos bleibt.
- **Täglicher Worker erweitert**
  (`scripts/indexautomatik_taegliche_pflege.py`): greift bei aktivem
  Flag automatisch NUR frische `SOLL_UMSETZUNG_OFFEN`-Fälle auf (exakt
  wie `versenden()` nur `BEREIT`, nicht `BLOCKIERT`, automatisch
  aufgreift) - ein bereits `SOLL_UMSETZUNG_BLOCKIERT`er Fall braucht
  eine behobene Ursache und wird dann bewusst manuell über die
  Backoffice-Ansicht erneut angestoßen, keine zweite Worker-/
  Schedulerinstanz.

### Was ausdrücklich NICHT geliefert ist (bewusste, offen benannte Lücken)

- **`MIETINKASSO_INDEXAUTOMATIK_SOLL_UMSETZUNG_ENABLED` bleibt Default
  AUS** - wie jede andere neue Automatikschaltung in diesem Repository
  erst nach expliziter Betriebsfreigabe zu aktivieren.
- **Transport/MailOps-Anbindung ist bewusst NICHT Teil dieses Commits**
  (Auftrag: "Liefere zunächst den Soll-Pfad ... danach Transport") -
  Mailversand für Index-Erhöhungsschreiben läuft unverändert über den
  bestehenden generischen `HttpTransportadapter`
  (`indexautomatik_transport_endpoint_url`/`_api_key`), NICHT über den
  von Codex inzwischen produktiv ergänzten MailOps-Unix-Domain-Socket-
  Dienst (eb3b126). Die konkrete Schnittstelle ist bereits bekannt
  (`POST /v1/send` mit `referenz`/`art`/`empfaenger_*`/`betreff`/
  `text`/`freigabe_referenz`, `GET /v1/status`/`GET /v1/health`,
  Antwortstatus `ANGENOMMEN|GESENDET|UNKLAR|IN_BEARBEITUNG|
  DEAKTIVIERT|NICHT_GEFUNDEN|FEHLER` - `FEHLER` terminal, nie erneut
  senden; NUR `GESENDET` mit tatsächlichem `versendet_am` aus einem
  echten SentItems-Beleg zählt als Versandnachweis/löst Mahnstufe 2
  aus; `ANGENOMMEN` NIE als Empfängerzugang werten; `UNKLAR` löst eine
  Statusabfrage, aber NIE einen neuen Send-POST aus; idempotente
  POST-Wiederholung mit geändertem Payload liefert 409; Owner-
  Erinnerungen serverseitig fix `mb@jlb-immo.at`, kein Mieterfallback;
  Sender serverseitig fix `hausverwaltung@jlb-immo.at`; UDS-Pfad
  `/run/jlb-hv-mail/mailops-hv.sock`, Tokenfile
  `/run/secrets/hv-mail-token`) und als eigener, separater Folge-Commit
  geplant - KEIN eigener direkter SMTP-/Graph-Sender im Mietmodul,
  Provider-Abstraktion mit synthetischem Fake zuerst, echte Anbindung
  erst nach dieser Schnittstellenfestlegung.
- **Fachliche Fristenprofil-Werte je Vertragstyp fehlen weiterhin.**
  Die generische, konfigurierbare Infrastruktur (siehe oben) trifft
  KEINE eigene Rechtsentscheidung - welche konkreten Verträge welche
  `frist_tage_zugang_bis_wirksamkeit`/welchen `frist_quellenbeleg`
  bekommen, bleibt Codex' fachlicher Prüfung vorbehalten.
- **Drei von Codex am 13.09. gemeldete, noch NICHT umgesetzte
  Regel-Verfeinerungen** (bewusst in einem separaten Folge-Commit,
  NICHT in diesem):
  1. `IndexKlauselTable` braucht belegte Kalender-/Intervall-/
     Rundungsfelder statt nur eines einmaligen
     `fruehestmoeglicher_termin` (der sonst ab Februar pauschal alles
     erlaubt) - u. a. für den Fall "nur Jänner UND strikt >3 %-
     Schwelle, Grenzwerte auf eine Dezimalstelle" versus "Jänner ohne
     Schwelle" versus "maximal einmal jährlich ohne fixen Monat".
  2. MRG-Zinsbeschränkung/Förderbindung dürfen bei unbekannter Lage
     NICHT stillschweigend `False` werden - ein `ENTWURF` darf
     unbekannt bleiben, eine Freigabe muss echte Tri-State-Angaben
     (bekannt-ja/bekannt-nein/unbekannt) statt eines gerateten Booleans
     verlangen.
  3. Ein `bezugsjahr`/`bezugsmonat` der Rechtsprofil-Quelle kann
     Jahrzehnte vor einer erst kürzlich importierten aktuellen
     Komponentenfassung liegen - die real bestehende aktuelle
     Sollkomponente ist erst ab ihrem Übernahmestichtag im System.
     `umsetzen()`/`_validiere_vollstaendigkeit_fuer_freigabe` dürfen
     `bezugsmonat` NIE mit dem Komponentenbeginn gleichsetzen oder
     zurückdatieren; vor einer Umsetzung muss die referenzierte
     AKTUELLE Fassung nachweislich aktiv sein, eine historische letzte
     Basis braucht einen SEPARATEN Beleg.
- **Backoffice-Ansicht ist bewusst minimal** (Liste + Detail/Vorschau +
  ein Aktions-Button) - keine Massenumsetzung, kein Filter/keine
  Suche, keine Sortierung nach Dringlichkeit.
- **Kein automatischer Differenzkorrektur-Mechanismus** für bereits
  `SOLLGESTELLT`/`ZUGESTELLT`/`EXPORTIERT`e Perioden, die den
  Wirksamkeitszeitraum überlappen - `umsetzen()` BLOCKIERT diesen Fall
  sichtbar mit Nennung der betroffenen Vorschreibung(en) statt eine
  Korrekturbuchung zu erfinden; die tatsächliche Korrektur bleibt ein
  dokumentierter, manueller Folgeschritt.

### Codex-Rückprüfung Commit 499c36f — behoben

Unabhängige Gegenproben gegen den ersten Soll-Umsetzung-Commit fanden
mehrere reale Buchungsfehler, alle in einem Nachfolgecommit auf
demselben Branch behoben, mit dediziertem Regressionstest je Fund:

- **(a) Fehlender Versandbeleg wurde nicht geprüft**: `umsetzen()`
  verlangte bisher nur einen bestätigten Zugang, nicht den tatsächlichen
  Versandnachweis (`versendet_am`/`externe_versandreferenz`) - ein
  (z. B. fehlerhaft) direkt auf `SOLL_UMSETZUNG_OFFEN` gesetzter Fall
  ohne echten Versand wurde umgesetzt. Jetzt Pflichtprüfung.
- **(b) Abgelaufene Mietzinsobergrenze wurde übersehen**: die Stale-
  Snapshot-Prüfung verglich nur einen Content-Hash, nicht die zeitliche
  Gültigkeit (`mietzinsobergrenze_gueltig_bis`) - jetzt über
  `RechtsprofilService.ist_noch_gueltig()` (identisch zu jeder anderen
  Prüfung dieses Rechtsprofils) abgedeckt.
- **(c) Ein geplantes Enddatum der alten Komponente ging verloren**: die
  neue Komponente bekam unabhängig vom Ausgangszustand immer
  `gueltig_bis=None` - ein ursprünglich befristetes Enddatum wird jetzt
  unverändert auf die neue Komponente übertragen.
- **(d) Der gebuchte neue Betrag wurde nicht gegen das versendete
  Schreiben verifiziert**: `komponenten_verteilung.neuer_betrag_cent`
  wurde ungeprüft übernommen - jetzt Pflichtabgleich gegen
  `alter_betrag_cent + erhoehung_cent`.
- **Anspruchsmonat/Fälligkeit/technische Komponentenwirksamkeit waren
  vermischt**: `VorschreibungService.entwurf_erstellen` liest aktive
  Komponenten IMMER zum Monatsersten - eine Komponentenwirksamkeit
  mitten im Monat (taggenaue `zahlungspflicht_ab`, z. B. der 5./15.)
  hätte im Anspruchsmonat selbst noch den ALTEN Betrag verwendet. Die
  technische Wirksamkeit liegt jetzt bewusst auf dem 1. des
  Anspruchsmonats (`_anspruchsmonat_start`); die tatsächliche Fälligkeit
  bleibt separat auf dem Erhöhungsschreiben sichtbar. Ein End-to-End-Test
  über eine echte `VorschreibungService.entwurf_erstellen` belegt das.
- **Wiederholte Erstjahres-Aliquotierung im Folgezyklus drohte**:
  `bezugsjahr` wurde nach einer Umsetzung fortgeschrieben, `bezugsmonat`/
  `letzte_basis_war_jahresdurchschnitt` aber nicht - ein unmittelbar
  folgender zweiter Zyklus hätte die (nur für das allererste,
  unterjährige Bezugsjahr gültige) Aliquotierung
  (`mieweg_vorschau/berechnung.py`) fälschlich ein zweites Mal
  angewendet. Jetzt werden alle drei Felder beim MieWeG-Pfad gemeinsam
  auf die neue Jahresbasis (`bezugsmonat=12`,
  `letzte_basis_war_jahresdurchschnitt=True`) fortgeschrieben, mit einem
  Zwei-Zyklen-End-to-End-Test.
- **`outbox_service._entwurf_speichern` aktualisierte
  `komponenten_verteilung` bei einem Retry (`bestehende_id`) nicht** -
  ein zuvor mehrkomponenten-blockierter, dann behobener Entwurf behielt
  die alte/leere Verteilung. Jetzt mit übernommen.
- **Backoffice-Liste `/indexautomatik/soll-umsetzung` filterte nicht
  nach Gesellschaftsscope** (anders als `monatslauf_alle`/`plane_alle`)
  - im aktuellen Ein-Operator-Pilotmodul (`_ctx()` ist immer
  ADMIN/`gesellschaft_ids=None`) praktisch folgenlos, aber ohne den
  Filter wäre eine künftige Mehrbenutzer-Rolle sofort eine Datenlücke.
  Jetzt mit demselben `ctx.has_zugriff(...)`-Muster gefiltert.

Aus derselben Rückprüfung inzwischen behoben (Commits `8f499c9`,
`3139e19`, `c01ceb2`):

- **Mehrkomponenten-Verteilung war vollständig gesperrt** - ein Fall
  mit z. B. HMZ+Küche beauftragt wurde komplett blockiert. Jetzt
  implementiert: `outbox_service._verteile_erhoehung_centgenau`
  verteilt die Gesamterhöhung proportional zum jeweiligen Anteil jeder
  Komponente am referenzierten Gesamtbetrag (größte-Rest-Verfahren,
  Summe der Deltas ist immer exakt die Gesamterhöhung).
  `komponenten_verteilung` trägt dafür eine Liste (`eintraege`) statt
  eines einzelnen Eintrags; `umsetzung_service.umsetzen()` historisiert
  entsprechend beliebig viele betroffene Komponenten in einer
  Transaktion (`IndexSollUmsetzungTable.neue_komponenten_ids`/
  `beendete_komponenten_ids` additiv ergänzt, die bisherigen
  Einzelfelder bleiben für den Ein-Komponenten-Fall bequem gefüllt). Mit
  E2E-Test (zwei Komponenten, zentgenaue Verteilung, beide historisiert).
- **Geschäftsraum-/Klausel-Pfad schrieb `basis_wert`/`basis_monat`
  der `IndexKlauselTable` nach einer freigegebenen `IndexAnpassung`
  nicht fort.** Jetzt implementiert: `umsetzen()` legt bei einer
  Klausel-basierten Umsetzung eine NEUE `IndexKlauselTable`-Version an
  (append-only, alte `GESPERRT` + `ersetzt_id`), mit fortgeschriebenem
  `basis_wert` (= tatsächlich verwendeter `IndexAnpassung.neuer_wert`).
  Rein mechanische Bestandskorrektur, KEINE neue Rechtsentscheidung zu
  Fristen/Terminen - der automatische Wirksamkeitstermin für diesen
  Pfad bleibt weiterhin bewusst gesperrt (siehe unten, Punkt 1 der
  Codex-Regel-Verfeinerungen ist NICHT Teil davon).
- **Tri-State MRG-Zinsbeschränkung/Förderbindung** ist umgesetzt:
  additive `mrg_zinsbeschraenkung_geprueft`/`foerderbindung_geprueft`-
  Flags (Default `False`, `server_default text("0")`) statt einer
  gelockerten bestehenden Spalte - `_validiere_vollstaendigkeit_fuer_
  freigabe` verlangt jetzt `geprueft=True` für beide Felder, ein
  `ENTWURF` darf weiterhin unbekannt bleiben.
- **Komponenten-Existenzprüfung gegen die historische Bezugsbasis
  (Punkt 3 der Codex-Regel-Verfeinerungen) verwechselte die aktuelle,
  zuletzt historisierte Komponentenzeile mit der ursprünglichen
  Verpflichtung.** Nach einer Umsetzung bekommt die neue Komponentenzeile
  ein neues `gueltig_von` (der Anspruchsmonat) - eine reine
  `gueltig_von`-Prüfung hätte die (korrekt fortgeschriebene) MieWeG-
  Bezugsbasis der Vorperiode fälschlich als "Komponente hat noch nicht
  bestanden" abgelehnt. Neue additive Spalte `VertragsKomponenteTable.
  historisiert_von_id` (explizite, nur von `umsetzen()` gesetzte Kette,
  KEINE ID-String-Heuristik) - `StammdatenRepository.
  ursprungs_gueltig_von()` verfolgt sie bis zur Ursprungszeile zurück;
  von `mieweg_vorschau/service.py` UND `rechtsprofil.py` gemeinsam
  genutzt. Dieselbe Kette schützt auch vor dem umgekehrten, in Punkt 3
  beschriebenen Fall (ein `bezugsjahr` Jahrzehnte vor einer gerade erst
  importierten Komponentenfassung) - eine Komponente ohne
  `historisiert_von_id` (z. B. ein frischer Intake-Import) liefert ihr
  eigenes `gueltig_von` als Ursprung, die Prüfung blockiert dann
  korrekt statt eine unbelegte historische Basis stillschweigend zu
  akzeptieren.
- **Bestehende, noch nicht gebuchte (ENTWURF) Monatsvorschreibungen
  blieben nach einer Umsetzung auf dem alten Komponentenstand stehen**
  (`VorschreibungService.entwurf_erstellen` befüllt eine Vorschreibung
  nur beim allerersten Aufruf, aktualisiert eine bereits bestehende
  nie). `umsetzen()` baut einen solchen offenen Entwurf ab dem
  Wirksamkeitsmonat jetzt in derselben Transaktion aus dem neu
  historisierten Komponentenstand neu auf (inklusive unveränderter
  BK/HK-Positionen) - bereits gebuchte Perioden bleiben weiterhin
  vollständig gesperrt (unverändert).
- **Der Mietertext enthielt eine interne `Rechtsprofil Version N`-
  Referenz** - entfernt; die Vertragsbeleg-/Klauselreferenz (ein
  verständlicher Mietvertragsabschnitt) steht bereits an anderer Stelle
  im Text und bleibt unverändert. Interne Versions-/Hash-Nachweise
  bleiben ausschließlich im `IndexSollUmsetzungTable`-Ausführungsnachweis.

Weiterhin OFFEN, NICHT Teil dieser Korrekturrunde (echte Fachentscheidung
nötig, siehe AGENTS.md - Claude baut keine eigenen Rechtsregeln):

- **Punkt 1 der Codex-Regel-Verfeinerungen (Kalender-/Intervall-/
  Rundungsfelder für `IndexKlauselTable`) ist NICHT umgesetzt.** Ein
  automatischer Wirksamkeitstermin für den Geschäftsraum-/Klausel-Pfad
  bleibt deshalb weiterhin gesperrt (`_monatslauf_klausel` gibt
  unverändert `BLOCKIERT` mit "kein erfundenes Datum" zurück, sobald ein
  rechnerischer Vorschlag vorliegt). Welche konkreten Kalendermonate/
  Mindestintervalle/Rundungsregeln je Vertrag gelten (z. B. "nur Jänner
  UND strikt >3%-Schwelle" versus "Jänner ohne Schwelle" versus
  "maximal einmal jährlich ohne fixen Monat"), ist eine echte, vertrags-
  individuelle Rechtsfrage - das Datenmodell/die Prüflogik dafür sollte
  erst entworfen werden, wenn die konkreten, zu unterstützenden
  Regelformen feststehen, statt ein Schema zu erraten, das an der
  tatsächlichen Anforderung vorbeigeht.

## Paket Dashboard/Variable Monatsabrechnung (Auftrag 13.09.2026, HV-20260913-DASHBOARD)

Neues Modul `src/mietinkasso/variableabrechnung/` (versionierte
Monatsabrechnungen für KURZZEITVERMIETUNG/SELFSTORAGE, CSV-Import,
Monatsübersicht, separate Netto-Mietanteil-Freigabe) plus reine
Anzeige-Präzisierungen im bestehenden Dashboard/Kontoauszug. Der
vollständige, wortgetreue Auftrag inkl. der Quellenpräzisierung steht
in `RAHMENPROGRAMM.md` (Abschnitt "HV-20260913-DASHBOARD").

**Historie der unabhängigen Abnahme:** Der erste Übergabestand
(`ee6bd8b`) wurde in einer unabhängigen Gegenprüfung mit 6 konkret
reproduzierten Fehlern zurückgewiesen (Commit `60eb9c4` behebt sie plus
zwei bei der Prüfung zusätzlich gefundene Datenvertrags-/
Idempotenzlücken); eine zweite Gegenprüfung von `60eb9c4` fand vier
weitere konkrete Fehler (Commit `8a9d036` behebt sie). Der folgende
Abschnitt beschreibt den STAND NACH BEIDEN Korrekturrunden, nicht den
ursprünglichen Übergabestand.

### Was funktioniert (mit Tests belegt)

- **Dashboard/Konto-Relabeling, rein darstellend**: "Saldo" heißt jetzt
  "Kontostand (offen/Guthaben)", "fälliger unstrittiger Rest" heißt
  "Davon mit bekannter Fälligkeit"; ein Rechenweg (Eröffnung +
  Vorschreibungen/Nachbelastungen − Zahlungen − Gutschriften, optional
  + Korrekturen) wird aus denselben `OPSaldo.positionen` angezeigt, die
  `OPService.berechne_saldo` bereits verwendet - `op/service.py` selbst
  wurde NICHT verändert. Aktive Mahnsperren samt Gründen erscheinen
  sowohl in der Objektübersicht als auch am Kontoauszug, mit
  ausdrücklichem Hinweis, dass eine bekannte Fälligkeit KEINE
  Mahnfreigabe ist und eine unbekannte Fälligkeit NICHT automatisch als
  strittig gilt.
- **Versionierte, unveränderliche Monatsabrechnung**
  (`VariableAbrechnungTable`/`VariableAbrechnungService`): jede
  Erfassung/Korrektur ist eine neue Zeile; ein partieller Unique-Index
  (`uq_variable_abrechnung_aktuell`) erzwingt genau eine aktuelle
  Version je (Einheit, Art, Leistungsmonat) auch auf DB-Ebene.
  `korrigieren` bindet sich per optimistic lock an die vom Aufrufer
  zuletzt gesehene Version (`ausgehend_von_id`); die eigentliche
  Durchsetzung ist eine ATOMARE bedingte UPDATE-Anweisung in
  `VariableAbrechnungRepository.neue_version_anlegen`
  (`WHERE id=alte_id AND ist_aktuell=True`) - eine reine
  Prüfen-dann-Schreiben-Logik hätte (wie in der ersten Gegenprüfung
  reproduziert) ein enges Zeitfenster offengelassen, in dem eine
  zwischenzeitliche fremde Korrektur stillschweigend überschrieben
  werden konnte. Ein `erfassen`-Aufruf auf eine bereits bestehende
  Gruppe ist NUR bei identischem Inhalt ein sicherer No-Op
  (Wiederholimport, `vermietete_flaeche_qm` wird dabei kanonisch als
  `Decimal` verglichen, NICHT als String - "10" und "10.00" gelten
  als gleich); bei abweichendem Inhalt verweist
  `VariableAbrechnungKonfliktError` auf die explizite Korrektur-Route.
- **Entwurf/Betragsart-Trennung ohne erfundene USt-Umrechnung**:
  `berichteter_betrag_cent`/`berichteter_betragsart`
  (BRUTTO/NETTO/UNGEKLAERT) halten fest, WIE ein Betrag gemeldet wurde;
  `unser_netto_anteil_cent` (der für die Erlössumme MASSGEBLICHE Wert)
  bleibt davon unabhängig und optional, bis er tatsächlich geprüft ist
  (`status=BESTAETIGT`, das einen erfassten Nettoanteil voraussetzt).
  Kostenfelder (Betriebs-/Reinigungs-/Verwaltungskosten-Hinweis) und
  der tatsächliche Zahlungseingang sind rein informativ und werden an
  KEINER Stelle im Code von `unser_netto_anteil_cent` abgezogen oder
  damit verrechnet.
- **Separate Netto-Mietanteil-Freigabe für Dauermiete-Komponenten**
  (`KomponentenNettoMietFreigabeTable`/`komponenten_freigabe.py`, neu
  in der ersten Korrekturrunde): `VertragsKomponenteTable.betrag_cent`
  wird NIE als Netto-Mietbasis übernommen - der Bestand enthält
  historisch auch BRUTTO gespeicherte Beträge, auch bei
  art=HMZ/KUECHE/PARKPLATZ (die `ust_satz_promille`-Spalte beweist
  allein keine Betragsbasis); eine positive Whitelist plausibler
  Mietarten (`_MOEGLICHE_MIET_ARTEN`) ist NOTWENDIG, aber NICHT
  hinreichend. Nur eine explizit erfasste, belegte Freigabe mit
  eigenem `bestaetigter_netto_betrag_cent` (kann von `betrag_cent`
  ABWEICHEN), Quellenbeleg und eigener Gültigkeit zählt für die
  Monatsübersicht; sie entwertet sich automatisch (`quelle_hash`,
  analog `RechtsprofilTable`), sobald sich die zugrunde liegende
  Komponente ändert. Eine neue Freigabe entwertet NUR zeitlich
  überlappende frühere Freigaben derselben Komponente (atomar per
  CAS-Update mit `rowcount`-Prüfung) - eine nicht überlappende
  historische Freigabe (z. B. ein anderer Monat) bleibt unberührt; eine
  Überschneidung erfordert zusätzlich einen expliziten
  `aenderungsgrund`, sonst wird NICHTS geschrieben (in der zweiten
  Gegenprüfung reproduziert: eine pauschale "entwerte alle" hätte eine
  unabhängige historische Freigabe rückwirkend zerstört).
  `VertragsKomponenteTable`/`OPPositionTable` werden von diesem Modul
  NIE geschrieben.
- **Atomarer, idempotenter CSV-Import** (`variableabrechnung/
  csv_import.py`) - Vorschau (`erstelle_plan`, rein lesend) und
  bewusste Übernahme (`wende_an`, bindet sich an den Plan-Hash der
  geprüften Datei) getrennt, analog zum bestehenden generischen Intake
  (`intake/planner.py`/`intake/apply.py`) und Eröffnungsimport
  (`op/eroeffnung_import.py`). Eine Zeile, die von der aktuellen
  Version abweicht, OHNE `aenderungsgrund` zu tragen, blockiert den
  GESAMTEN Import (KONFLIKT); mit `aenderungsgrund` wird sie erst nach
  expliziter `korrekturen_bestaetigt`-Bestätigung als neue Version
  übernommen. Die gesamte Datei läuft in EINER Transaktion - eine
  gesperrte/konfliktbehaftete Zeile verhindert auch die Übernahme der
  unproblematischen Zeilen derselben Datei. `plan_hash` bindet sich
  zusätzlich an die je Zeile GESEHENE aktuelle Version
  (`aktuelle_version_id`); `wende_an` prüft unmittelbar vor dem
  Schreiben mit einer FRISCHEN Neuprüfung erneut gegen den bestätigten
  Hash - eine zwischenzeitliche fremde Änderung (nicht nur ein
  geänderter Dateiinhalt) lässt den gesamten Import fehlschlagen,
  statt still auf den zwischenzeitlichen Stand umzubasieren (in der
  ersten Gegenprüfung als "CSV-Stale-Preview" reproduziert und über
  einen dedizierten Regressionstest abgesichert). `erstelle_plan`
  verlangt jetzt ebenfalls `ctx` und markiert Zeilen ohne
  Gesellschaftszugriff bereits in der Vorschau als GESPERRT, ohne
  Fachdaten preiszugeben; `wende_an` prüft Gesellschaftszugriff/
  Schreibrecht für JEDE Zeile, auch eine inhaltlich UNVERAENDERTE.
- **Monatsübersicht** (`variableabrechnung/dashboard.py`,
  Backoffice-Route `/dashboard/monatsuebersicht`): Dauermiete-Soll
  netto ist die Summe der oben beschriebenen, geprüften
  Netto-Mietanteil-Freigaben für Vertragskomponenten, die den
  gewählten Monat VOLLSTÄNDIG abdecken (nicht nur zum Monatsersten
  aktiv - ALLE den Monat überlappenden Komponenten werden geprüft,
  damit eine erst untermonatlich beginnende/endende Komponente sichtbar
  bleibt, statt unbemerkt zu verschwinden), plus bestätigte Kurzzeit-/
  Selfstorage-Nettoanteile. Ein Vertrag/eine Komponente, die den Monat
  nur UNTERmonatlich abdeckt, liefert keinen automatisch berechneten
  anteiligen Wert, sondern eine explizite Datenlücke (keine erfundene
  Proration); ein aktiver Vertrag ganz ohne qualifizierende Komponente
  ebenso. Eine Einheit mit Berichten für MEHRERE Arten im selben Monat
  (z. B. gleichzeitig KURZZEITVERMIETUNG und SELFSTORAGE) ist ein
  Artenkonflikt - alle betroffenen Berichte werden von der Summe
  ausgeschlossen und als EIN konsolidierter Hinweis gemeldet, statt
  stillschweigend addiert zu werden. Eine Einheit mit SOWOHL
  Dauermiete-Soll ALS AUCH einem Report für denselben Monat wird als
  Doppelzählungs-Konflikt erkannt und der Report von der Summe
  ausgeschlossen (nicht addiert). ENTWURF-Zeilen und fehlende
  Monatsberichte erscheinen als benannte Datenlücken statt in einer
  scheinbar vollständigen Summe zu verschwinden;
  `tatsaechlicher_zahlungseingang_cent` fließt an keiner Stelle in die
  Summe ein ("nie Bank-Ist behaupten"). Ein Objekt-Ausschluss wird an
  JEDER Stelle berücksichtigt: beim Summieren, bei den
  "fehlender Bericht"-Hinweisen UND bei den zugrunde liegenden
  `variable_service.liste_aktuelle`-Zeilen selbst (siehe nächster
  Punkt) - ein Bericht, dessen Objekt ERST NACH der Erfassung
  ausgeschlossen wird, verschwindet dadurch automatisch aus der Summe.
- **Auth/Scope/CSRF/Objektausschluss konsequent auch auf Lesepfaden**:
  `require_gesellschaft_access`/`require_schreibrecht` auf jeder
  Service-Methode, `pruefe_einheit_nicht_ausgeschlossen` (analog
  `pruefe_vertrag_nicht_ausgeschlossen`) sperrt Objekt-107-artige
  Fälle. Über die reine Gesellschaftsscope-Prüfung hinaus (in der
  zweiten Gegenprüfung als Lücke reproduziert) filtern
  `liste_aktuelle`/`liste_alle` jetzt zusätzlich Berichte
  ausgeschlossener Objekte heraus (auch mit explizit angegebener
  `gesellschaft_id`), und `aktuelle_version`/`liste_versionen` lehnen
  eine ausgeschlossene Einheit aktiv ab; die Backoffice-Routen
  Korrektur-GET, Versionen-GET und die neue
  Komponentenfreigabe-GET/POST fangen `CrossTenantError`/
  `ObjektAusgeschlossenError` in eine verständliche Fehlerseite ab
  statt einer rohen 500-Antwort. Die Komponentenfreigabe-POST-Route
  verifiziert zusätzlich, dass die übergebene `komponente_id`
  tatsächlich zum `vertrag_id` im Pfad gehört. Jede POST-Route
  verlangt ein gültiges `csrf_token`.
- 731 Tests insgesamt (665 vor diesem Paket + 66 neu über beide
  Korrekturrunden: Service, CSV-Import inkl. Stale-Preview-/
  Idempotenz-Regressionstests, Netto-Mietanteil-Freigabe inkl.
  Überlappungs-/Invalidierungs-Regressionstests, Dashboard-Aggregation
  inkl. Ausschluss-/Artenkonflikt-/Untermonatlich-Regressionstests,
  Backoffice-HTTP inkl. Objektsperre, Centgenauigkeit/negative Beträge,
  fehlend-vs-Null bei `berichteter_betragsart`, und ein expliziter
  Test, dass eine variable Monatsabrechnung den bestehenden
  OP-Kontostand NICHT verändert).

### Was ausdrücklich NICHT geliefert ist (bewusste, offen benannte Lücken)

- **Echte Bestandszuordnung bleibt bei Codex.** Dieses Modul ist
  generischer Code über beliebige `EinheitTable`-IDs mit Nutzungsstatus
  KURZZEITVERMIETUNG/SELFSTORAGE - welche konkreten sieben
  Kurzzeit-Einheiten und die eine Selfstorage-Einheit real existieren
  und wie ihre Leistungsmonate tatsächlich zugeordnet werden, ist NICHT
  Teil dieser Sitzung (synthetische Testdaten/Tests ausgenommen).
  Ebenso befüllt Codex die echten Netto-Mietanteil-Freigaben je
  Bestandskomponente (Quellenbeleg/Gültigkeit) außerhalb dieses Repos -
  ohne eine solche Freigabe bleibt jede Dauermiete-Komponente in der
  Monatsübersicht eine Datenlücke, das ist gewolltes Verhalten, kein
  Bug.
- **Kein CSV-Datei-Upload-Assistent/keine Vorlagen-Datei im Repo.** Der
  Import erwartet die Spaltenreihenfolge aus dem Hinweistext der
  Backoffice-Seite (`/backoffice/variable-abrechnung/import`); eine
  separate, herunterladbare CSV-Vorlagendatei existiert (noch) nicht
  (die synthetische `importtemplates/variable_abrechnung.csv` dient nur
  Tests/Doku).
- **Keine Backoffice-seitige Massenkorrektur.** Jede Korrektur läuft
  über GENAU eine Zeile (Formular ODER eine einzelne CSV-Zeile mit
  `aenderungsgrund`) - es gibt keine "alle Zeilen eines Monats auf
  einmal korrigieren"-Funktion. Das gilt analog für die
  Netto-Mietanteil-Freigabe: jede Freigabe wird einzeln je Komponente
  erfasst.
- **Keine Historisierung von `EinheitTable.nutzungsstatus`.** Das Feld
  bleibt (wie im gesamten Repository) ein einzelner aktueller Zustand
  ohne Verlauf; die Monatsübersicht kompensiert das für die
  Dauermiete-Summe über die Vertragsgültigkeit, verwendet den aktuellen
  Status aber weiterhin als (klar gekennzeichnete) Heuristik für den
  "fehlender Monatsbericht"-Hinweis - eine Einheit, deren Nutzungsart
  sich zwischenzeitlich geändert hat, kann hier einen nicht ganz
  treffenden Hinweis erzeugen.
- **Keine automatische Verknüpfung zu `VorschreibungTable`/OP.** Die
  variable Monatsabrechnung UND die Netto-Mietanteil-Freigabe sind
  bewusst EIGENE, von `OPPositionTable` vollständig getrennte
  Datenquellen (Reporting) - kein Code-Pfad dieses Pakets bucht, ändert
  oder storniert einen OP oder eine Vorschreibung, oder schreibt in
  `VertragsKomponenteTable`.
- **Keine anteilige (untermonatliche) Berechnung.** Eine Komponente
  oder ein Vertrag, die/der einen Monat nur teilweise abdeckt, liefert
  bewusst KEINEN automatisch berechneten Teilbetrag, sondern
  ausschließlich eine Datenlücke - eine anteilige Zuordnung wäre ohne
  echte Quelle erfunden.
- **Keine Migrationsspalten-Änderung mehr nötig, aber Schemaerweiterung
  in dieser Sitzung.** `variable_abrechnungen` und
  `komponenten_netto_miet_freigaben` sind rein additiv;
  `komponenten_netto_miet_freigaben.aenderungsgrund` wurde in der
  zweiten Korrekturrunde NOCH VOR jeder Erstbefüllung mit echten Daten
  ergänzt (kein Alter einer produktiv befüllten Tabelle) -
  `create_all_tables()` legt weiterhin nur fehlende Tabellen an, kein
  eigenständiges Migrationswerkzeug.

## Paket Rückstandsübersicht (Auftrag 13.09.2026, HV-20260913-RUECKSTAENDE)

Neues, rein lesendes Modul `src/mietinkasso/rueckstaende/service.py`
(`berechne_rueckstandsuebersicht`) plus komplett überarbeitete zentrale
Backoffice-Route `/backoffice/` (`dashboard()`), die vorher OHNE
gewähltes Objekt leer war. Der vollständige, wortgetreue Auftrag steht
in `RAHMENPROGRAMM.md` (Abschnitt "HV-20260913-RUECKSTAENDE").
`src/invoice_automation/` ist unverändert.

### Was funktioniert (mit Tests belegt)

- **Standard "Alle Objekte", ohne Auswahl nicht mehr leer.** `/backoffice/`
  zeigt sofort eine gefüllte Übersicht über alle erlaubten, nicht
  ausgeschlossenen Objekte; ein gemeinsamer Objektfilter (Dropdown,
  gruppiert je Gesellschaft) schaltet auf genau ein Objekt um - Summen,
  Mietkontentabelle, Einzelpositionsübersicht und "Einheiten ohne
  Mietkonto" stammen alle aus DERSELBEN Berechnung
  (`RueckstandsUebersicht`) und reagieren daher garantiert identisch auf
  denselben Filter (mit einem dedizierten Regressionstest
  `test_filterwirkung_identisch_zu_teilmenge_von_alle_objekte`
  abgesichert).
- **Fünf getrennte Kennzahlen statt einer Summe**: Summe positiver
  Kontostände; Guthaben gesamt (wird NIE gegen positive Kontostände
  anderer Mieter verrechnet - "Soll-/Habensalden je Konto getrennt");
  Summe "fällig/überfällig" (heute fällig ist noch nicht überfällig,
  daher diese Formulierung statt eines pauschalen "überfällig")/noch
  nicht fällige/Fälligkeit-unbekannte Summe der EINZELPOSITIONEN
  (`OPService.offene_forderungen`). Diese Positions-Sicht ist
  ausdrücklich NICHT dasselbe wie `OPSaldo.faelliger_unstrittiger_
  rest_cent` (Konto-Sicht) - **Nachbesserung (Codex-Rückprüfung
  abc4530):** die Mietkontentabelle zeigt jetzt BEIDE Zahlen je Konto
  als eigene Spalten ("Fällig (Kontoberechnung)" und "Fällig
  (Positionen)"/"Rest gesamt (Positionen)") PLUS eine explizite
  numerische `abweichung_saldo_zu_positionen_cent`-Spalte, statt nur
  eines pauschalen Hinweistexts. Ein dedizierter Test
  (`test_kontostand_ohne_entsprechende_einzelposition_zeigt_abweichung`)
  reproduziert exakt den gemeldeten Fall (positive KORREKTUR-Buchung
  777 Cent: Kontostand 7,77 €, Einzelpositionen 0,00 €, Abweichung
  7,77 € sichtbar).
- **Mietkontentabelle** jetzt mit Objekt-Spalte (funktioniert dadurch
  sowohl gefiltert als auch über "Alle Objekte" hinweg), Konto- UND
  Mahnvorschau-Link je Zeile, sowie einer Hinweis-Spalte für aktive
  Mahnsperren (Grund wörtlich aus `SperreTable.grund`, z. B. RATENPLAN/
  RECHTSANWALT). **Nachbesserung:** eine neue, eigene Mahnfälle-Tabelle
  (`RueckstandsUebersicht.mahnfaelle`) zeigt JEDEN gespeicherten
  Mahnfall (OP-Nr., Stufe, Status, ursprünglicher Fallbetrag, Datum) -
  vorher wurde pro Vertrag nur der zuletzt angelegte Fall gezeigt, was
  frühere Stufen/andere Forderungen ausblendete. Der Fallbetrag fließt
  in KEINE OP-Kennzahl ein (Test:
  `test_alle_mahnfaelle_je_vertrag_sichtbar_nicht_nur_der_neueste`).
  Spaltenbezeichnung "Mieter" statt "Debitor"; Erklärungstexte ohne
  interne Begriffe (kein "FIFO"/"Service"/"glattgerechnet").
- **Neue Einzelpositionsübersicht** über alle offenen Forderungen im
  gefilterten Bestand: Objekt, Vertrag/Mieter, **OP-Nr. und
  Belegreferenz** (Nachbesserung - vorher nur Belegdatum, ähnliche
  Forderungen waren nicht unterscheidbar), Art, Zeitraum
  (Leistungsperiode), Belegdatum, Fälligkeit, Rest, Fälligkeitsklasse
  als Badge, Konto-Link. Eine unbekannte Fälligkeit (`faelligkeit_
  bekannt=False`) zeigt IMMER "unbekannt", selbst wenn inkonsistente
  Altdaten trotzdem ein Datum gespeichert hätten - nie ein scheinbar
  bestätigtes Datum.
- **Einheiten ohne Mietkonto** (Leerstand/Kurzzeitvermietung/
  Selfstorage/Eigennutzung) erscheinen in einer eigenen Liste über alle
  gefilterten Objekte - niemals als Mietkonto-Zeile mit erfundenem
  Saldo 0.
- **Serverseitige Zugriffsprüfung konsequent neu**: die Objekt-
  Auswahlliste UND die "Alle Objekte"-Summen entstehen aus GENAU EINER
  Schleife über `ctx.has_zugriff`-geprüfte Gesellschaften und deren
  NICHT ausgeschlossene Objekte. Ein explizit angefordertes `objekt_id`,
  das unbekannt ist, zu keiner zugänglichen Gesellschaft gehört, oder
  ausgeschlossen ist, wird EINHEITLICH (derselbe Fehlertyp,
  `UnbekanntesObjektFilterError`) abgelehnt - kein stiller Wechsel auf
  "Alle Objekte", kein Erkenntnisgewinn für den Aufrufer, welcher der
  drei Fälle vorliegt (HTTP: 400 mit verständlicher Fehlerseite statt
  500 oder stillschweigendem Fallback). **Nachbesserung:** zusätzlich
  wird JEDER Vertrag (und ein davon abgeleitetes Konto) zusätzlich
  gegen sein EIGENES `gesellschaft_id`-Feld geprüft und übersprungen,
  falls das nicht im `ctx`-Zugriff liegt - `list_vertraege_fuer_objekt`
  filtert nur über Einheit->Objekt, nie über dieses Feld, ein
  inkonsistenter Vertrag unter einem sonst erlaubten Objekt hätte sonst
  Finanzdaten einer fremden Gesellschaft durchgelassen (Test:
  `test_inkonsistenter_vertrag_unter_erlaubtem_objekt_wird_ausgeblendet`).
- **Vollständig GET-seiteneffektfrei**: die Übersicht kombiniert
  ausschließlich bereits bestehende Lesepfade
  (`StammdatenRepository`, `OPService.berechne_saldo`/
  `offene_forderungen`, `MahnFallRepository.list_fuer_vertrag`) - NIE
  `MahnwesenService.plane_forderung`/`plane_alle_offenen_forderungen`,
  die neue `MahnFallTable`-Zeilen anlegen würden. Ein dedizierter Test
  (`test_uebersicht_ist_vollstaendig_schreibfrei`) ruft die Berechnung
  mehrfach auf und prüft, dass sich weder OP- noch Mahnfall-Zeilenzahl
  ändert.
- 25 Tests in `test_rueckstaende_service.py` (Scope/Ausschluss,
  Guthaben-ohne-Verrechnung, Fälligkeitsklassen, Storno, historischer
  Vertrag mit Rest, Leerstand/fehlendes Konto, Mahnsperre, ALLE
  Mahnfälle je Vertrag statt nur der neueste, Konto-vs-Position-
  Abweichung inkl. der 777-Cent-KORREKTUR-Gegenprobe, OP-ID/
  Belegreferenz je Einzelposition, inkonsistenter Vertrag/Konto unter
  erlaubtem Objekt, identische Filterwirkung, einheitliche Ablehnung
  fremd/ausgeschlossen/unbekannt, Schreibfreiheit) plus 3 neue
  HTTP-Tests in `test_backoffice.py` (Standardansicht nicht mehr leer,
  Objektfilter wirkt konsistent, OP-Nr./Belegreferenz sichtbar) und 3
  angepasste Bestandstests (Umbenennung "Einheiten ohne aktiven
  Vertrag" → "Einheiten ohne Mietkonto"; Objekt 107 wird auf dieser
  Route jetzt aktiv abgelehnt statt nur-lesend mit Banner gezeigt -
  Kontoauszug-Verhalten für 107 bleibt unverändert; Spaltenbeschriftung
  "Fällig (Kontoberechnung)"/"Fällig (Positionen)" statt der
  mehrdeutigen "Davon mit bekannter Fälligkeit" NUR auf dieser Route -
  der Kontoauszug selbst behält seine Beschriftung). Gesamter
  Mietinkasso-Testsatz: 757 Tests grün.

### Was ausdrücklich NICHT geliefert ist (bewusste, offen benannte Lücken)

- **Kein neuer Export/keine Fremddienst-Integration.** Wie beauftragt -
  die Übersicht ist eine reine HTML-Seite im bestehenden Backoffice,
  kein CSV-/Excel-Export, keine externe API.
- **Keine eigene Verrechnungs-/Mahnlogik.** Alle Zahlen stammen 1:1 aus
  `OPService`/`MahnFallRepository`; dieses Paket ändert an deren
  Berechnung nichts und bucht/plant/versendet selbst nichts.
- **Mahnstatus ist eine Momentaufnahme, keine Live-Berechnung.** Der
  angezeigte Mahnfall-Status ist der zuletzt GESPEICHERTE (durch einen
  früheren, an anderer Stelle ausgelösten Planungslauf), nicht was eine
  Neuplanung JETZT ergäbe - für eine aktuelle Neuberechnung bleibt die
  bestehende Mahnvorschau-Seite (verlinkt) zuständig, die aber selbst
  weiterhin (wie schon vor diesem Paket) beim Aufruf tatsächlich plant.
- **Keine Historisierung von `EinheitTable.nutzungsstatus`** (wie im
  gesamten Repository) - "Einheiten ohne Mietkonto" zeigt den AKTUELLEN
  Status, keine rückwirkende Tatsachenbehauptung für vergangene Monate.
- **Keine Migrationsspalten-Änderung.** Reine Anwendungsschicht über
  bestehenden Tabellen, keine neue Tabelle/Spalte in dieser Sitzung.
