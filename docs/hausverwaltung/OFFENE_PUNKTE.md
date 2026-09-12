# Offene Punkte — Mietinkasso-Modul

Stand: lokaler synthetischer Backoffice-Pilot, 11.09.2026; keine Produktionsfreigabe.
Alles hier ist bewusst offen gelassen bzw. bewusst konservativ gebaut —
keine stillschweigend übersprungenen Punkte.

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
  gebaut.
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
  Offen bleibt: der Restbetrags-Check in `_create_zuordnung` ist ein
  SELECT-dann-INSERT innerhalb einer Transaktion, kein `SELECT ... FOR
  UPDATE`/echtes Serializable-Locking. Unter SQLite (Dev/Tests) sind
  Schreibtransaktionen ohnehin serialisiert, ein Doppelbuchungsfenster
  ist damit nicht beobachtbar; unter einer produktiven Mehrbenutzer-DB
  (z. B. PostgreSQL) mit Standard-Isolationsstufe (READ COMMITTED)
  könnten zwei ECHT GLEICHZEITIGE Zuordnungsversuche mit
  UNTERSCHIEDLICHER `vorgang_id` auf denselben Restbetrag derselben
  Transaktion theoretisch beide den (noch nicht committeten) alten
  Restbetrag sehen und ihn gemeinsam überschreiten. Ein Row-Lock auf der
  Banktransaktion (`SELECT ... FOR UPDATE`) beim Zuordnungscheck ist der
  nächste Schritt, falls Mehrbenutzerbetrieb mit hoher Nebenläufigkeit
  auf demselben Bankkonto produktiv relevant wird — technisch noch NICHT
  gebaut.
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
