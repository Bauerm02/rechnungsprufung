# Offene Punkte — Mietinkasso-Modul

Stand: lokaler synthetischer Backoffice-Pilot, 11.09.2026; keine Produktionsfreigabe.
Alles hier ist bewusst offen gelassen bzw. bewusst konservativ gebaut —
keine stillschweigend übersprungenen Punkte.

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
  Die Erkennung von Sammel-Summenzeilen stützt sich auf ein
  BEOBACHTETES, NICHT gegen eine echte George-Exportdatei verifiziertes
  Muster (`S`/`D` am Ende von "Enthaltene Überweisung ID"); jede Gruppe,
  die sich damit nicht eindeutig und centgenau auflösen lässt, wird
  bewusst als Prüffall ausgewiesen statt geraten. Bevor dieser Adapter
  für irgendetwas über die manuelle Sichtprüfung hinaus verwendet wird,
  muss das Muster gegen mindestens eine echte Exportdatei bestätigt
  werden.
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
