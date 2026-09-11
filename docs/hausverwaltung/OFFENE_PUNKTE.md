# Offene Punkte — Mietinkasso-Modul

Stand: erste Lieferung (siehe REVIEW_ZUSAMMENFASSUNG.md für Baustand).
Alles hier ist bewusst offen gelassen bzw. bewusst konservativ gebaut —
keine stillschweigend übersprungenen Punkte.

## Integration / Betrieb

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
- **Dokumentzustellung Vorschreibung:** `dokument_zustellen()` markiert
  nur den Status; ein PDF-Rendering/Versand ist nicht Teil von MVP1.
- **Auth/Login:** `auth/service.py` bildet Rollen- und
  Gesellschafts-Scoping als Autorisierungsschicht ab; ein echtes
  Login/Session-Handling (z. B. OAuth, Passwort-Reset) ist nicht Teil
  dieser Lieferung und muss vor Internet-Exposition ergänzt werden.
- **Company OS / 7d-invoice / jlb-cockpit:** Die Beziehung dieses
  Repos zu den auf jlb-hetzner geprüften Systemen ist laut Auftrag
  NICHT belegt und wurde hier nicht angenommen. Eine etwaige spätere
  Anbindung (z. B. Hauptbuch-Export-Zielsystem) ist ein offener
  Architekturpunkt für Codex/Markus.

## Fachlich (bewusst konservativ gehalten)

- **Index-Rechtsprofile:** `index/service.py` rechnet ausschließlich
  auf Basis einer versionierten, manuell freigegebenen `IndexKlausel`
  (Basisreihe/-wert/-monat, Schwelle, Dämpfung, vertragliche Grenze).
  Die MieWeG-2026-spezifischen Detailregeln (April-Termine,
  Jahresdurchschnittsbildung, 1 %-/2 %-Schwellenlogik, anteilige
  Erstvalorisierung, Altvertragsübergang) sind als Parameter der
  Klausel abbildbar, aber NICHT als hartkodierte "immer richtige"
  Formel hinterlegt — jede Klausel muss von einer fachkundigen Person
  gegen die tatsächliche Vertragslage und die RIS-Quellen
  (NOR40274266, NOR40274269) freigegeben werden, bevor sie wirksame
  Vorschläge erzeugt.
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

- Kein REST/HTML-Frontend mit Authentifizierung, nur ein minimales
  Read-Only-Dashboard (siehe `api/app.py`). Schreibende HTTP-Endpunkte
  existieren absichtlich nicht.
- `api/app.py` hat keine automatisierten HTTP-Tests: `fastapi.testclient.TestClient`
  bräuchte `httpx` als zusätzliche Abhängigkeit, die hier nicht
  eingeführt wurde. Stattdessen manuell smoke-getestet (`/health`, `/`,
  Routing-Tabelle) gegen eine echte SQLite-DB. Sollte vor einem
  produktiven Einsatz um echte HTTP-Tests ergänzt werden.
- Keine Observability (Metriken/Alerting) über structlog hinaus.
- SQLite als Default-URL für Entwicklung/Tests; PostgreSQL wird über
  `MIETINKASSO_DATABASE_URL` unterstützt (SQLAlchemy-Engine ist
  DB-agnostisch), aber nicht gegen ein echtes Postgres getestet.
- `JobRunner` (generische Job-Sperre) ist ein Zusatzbaustein für Jobs
  ohne natürliche Eindeutigkeit; die eigentliche Doppel-Buchungs-/
  Doppel-Versand-Sicherheit kommt aus den DB-Unique-Constraints in
  `vorschreibungen` (Vertrag+Monat) und `mahn_faelle` (outbox_key) plus
  dem atomaren Compare-and-Swap in `MahnFallRepository.claim_fuer_versand`.
