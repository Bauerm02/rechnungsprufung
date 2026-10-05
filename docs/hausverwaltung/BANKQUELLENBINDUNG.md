# Bankquellenbindung Objekt -> Bankquelle -> Bankkonto

Stand: Branch `codex/hv-bankimport-20261005`, Basis `77dbae4`. Code:
`src/mietinkasso/bank/{quellen_models,quellenbindung,quellenabruf}.py`,
Integration in `bank/service.py`, UI `backoffice/routes/bankquellen.py` und
Import-Teil von `backoffice/routes/bank.py`. Tests:
`tests/mietinkasso/test_bankquellenbindung.py`,
`tests/mietinkasso/test_bankquellen_review.py`,
`tests/mietinkasso/test_z_bankquellen_routen.py` (nur synthetische Daten,
Fake-Adapter, keine Bank-/Anbieterverbindung).

Dieses Dokument beschreibt, **was** der Code erzwingt und **was nicht**.
Er macht konfigurierte Mietbank-Pfade verbindlich; er verbindet **keine**
echte Bank, aktiviert keinen Anbieter und richtet keine echten Konten ein.

## 1. Datenmodell (additiv)

Zwei neue Tabellen über das bestehende `create_all`; bestehende
Finanztabellen werden weder geändert noch nachbefüllt.

- `bank_quellen`: (Anbieter, Zugangsreferenz, Anbieter-Kontoreferenz) ->
  genau ein internes Bankkonto, normalisierte eigene IBAN, Gesellschaft,
  Kontorolle. UNIQUE auf dem Tupel: dasselbe Tupel kann nie auf ein
  zweites Bankkonto zeigen. Zeilen sind unveränderlich.
- `bank_quellen_bindungen`: append-only Revisionen je Objekt (UNIQUE
  `objekt_id, revision`), Status `AKTIV` oder `WIDERRUFEN`, Nachweis,
  Akteur, Zeitpunkt, SHA-256-Fingerprint über Bindung und Quelle.
  Mehrere Objekte dürfen dieselbe Quelle nutzen.

Referenzen sind **nicht geheime Kennungen** (z. B. Teilnehmer-/Kunden-ID).
Passwörter, Schlüssel und Tokens gehören nie hinein; die Prüfung erlaubt
nur `A-Z a-z 0-9 . _ : / -` ohne Leerraum, kann ein Geheimnis aber nicht
inhaltlich erkennen.

## 2. Konfiguration (`QuellenbindungService`)

- Schreibrecht und Gesellschaftszugriff Pflicht; Lesezugriff sieht nur.
- Objekt muss existieren, darf nicht ausgeschlossen sein (Objekt 107),
  Gesellschaft von Objekt, Bankkonto und Eingabe müssen übereinstimmen.
  Der feste Ausschluss von Objekt 107 gilt auch bei fehlendem Ausschlusskennzeichen.
- Die eingegebene IBAN muss nach Normalisierung exakt der gespeicherten
  IBAN des Bankkontos entsprechen (nur Format, keine Prüfsumme).
- Nur Kontorolle `MIETE`; `KAUTION`, `KREDIT`, `UNBEKANNT` und Unbekanntes
  werden abgelehnt. Kein Konto wird aus Namen, Gesellschaft oder "erstem
  Treffer" abgeleitet; die Oberfläche belegt nichts vor.
- Quelle (falls neu), Bindungsrevision und Audit-Eintrag
  (`audit_events`, IBAN nur maskiert) entstehen in **einer** Transaktion
  (`schreibgesperrte_session`, Bankkontozeile `FOR UPDATE`).
- Exakter Retry (gleiche Quelle, gleicher Nachweis) ist ein No-Op.
- Jede Änderung braucht den angezeigten Stand `erwarteter_stand`
  (`NEU` bzw. `R<revision>-<fingerprint>`); sonst
  `OptimistischerLockKonfliktError`, nichts wird überschrieben.
- Widerruf schreibt einen Grabstein (neue Revision `WIDERRUFEN`);
  Revisionen und Audit bleiben erhalten. Neubindung danach nur gegen den
  Widerrufsstand.

## 3. Drei Zustände und ihre Wirkung

| Zustand | Import auf das Bankkonto | Zuordnung an Mieter des Objekts |
|---|---|---|
| **Nie konfiguriert** (Objekt ohne Revision, Bankkonto ohne Quelle) | unverändert: manueller Import wie bisher (Legacy) | unverändert |
| **Aktiv** | nur mit gültigem `BankQuellenKontext`; ohne Kontext abgelehnt | nur von genau diesem Bankkonto |
| **Widerrufen** (Grabstein) | gesperrt, auch ohne Kontext; kein Rückfall auf Legacy | gesperrt |
| **Ungültig** (Bankkonto-IBAN/Gesellschaft geändert, Objekt umgehängt, Fingerprint verletzt) | gesperrt | gesperrt |

Ein Bankkonto, auf das jemals eine Quelle gezeigt hat, bleibt für
ungebundene Importe dauerhaft gesperrt. Das ist kontrollierte Einführung:
nur ausdrücklich konfigurierte Objekte/Bankkonten werden strikt, alle
übrigen arbeiten unverändert weiter.
Bei gemeinsam genutzten Quellen sperrt der Widerruf den Kontext dieses
Objekts. Andere aktiv gebundene Objekte können dieselbe Quelle weiterhin
verwenden; der ungebundene Import bleibt gesperrt.

## 4. Import

`importiere_camt053`/`importiere_csv` nehmen optional `quellen_kontext`.
Der Kontext wird nur aus der DB erzeugt (`kontext_fuer_objekt`) und vor
dem Parsen **und** erneut als erste Prüfung in der gesperrten
Schreibtransaktion (`_importiere_atomar`) mit einem frisch geladenen
Kontext verglichen (alle Felder inkl. Revision/Fingerprint). Widerruf
oder Neubindung zwischen beiden Prüfungen -> Abbruch ohne Zeile.

- CAMT: bestehende Pflicht-IBAN-Prüfung je Statement.
- CSV mit Kontext: eigene Kontospalte (`eigene_iban`) Pflicht, jede Zeile
  wird gegen die gebundene IBAN geprüft.

**Herkunft beim Dateiupload:** Konto aus der gespeicherten Bindung, IBAN
der Datei geprüft - die Datei selbst ist **nicht** anbieterseitig
authentifiziert (Selbstauskunft der Exportdatei). Die Vorschau sagt das.

## 5. Zahlungszuordnung

`_zuordnen_atomar` (manuell und automatisch),
`verknuepfe_mit_bestehender_zahlung` und `verarbeite_ruecklastschrift`
lösen in ihrer Schreibtransaktion Konto -> Vertrag -> Einheit -> Objekt
aus der DB auf und prüfen die aktuelle Bindung gegen das Bankkonto der
persistierten Transaktion, bevor gebucht wird. Ein anderes Bankkonto
derselben Gesellschaft wird abgelehnt; ein widerrufenes/ungültiges
Objekt ebenfalls. Geteilte Quellen (mehrere Objekte, ein Bankkonto)
bleiben zuordenbar. Mahnwesen, Mahnsperren, Hashes und Idempotenz bleiben
unverändert.
Auch direkte Aufrufe von `BankRepository.create_zuordnung` prüfen diese
Bindung, einschließlich Wiederholaufrufen. Ohne übergebene Session nutzt
das Repository selbst die SQLite-Schreibsperre. Wer eine Session übergibt,
muss sie wie bisher als gemeinsame gesperrte Schreibtransaktion führen.

Grenze: Ein Rücklastschrift-Vorgang nach einer Neubindung auf ein anderes
Bankkonto wird abgelehnt (fail-closed) und braucht manuelle Klärung.

## 6. Geschützter Abruf (`GeschuetzterBankabruf`)

Anbieterneutraler Einstieg mit injiziertem, nur lesendem
`BankAnbieterAdapter`. Tupel stammt ausschließlich aus der aktiven
Bindung. Kontoinfo (Tupel, IBAN, Rolle `MIETE`) wird **vor** dem
Umsatzabruf geprüft; eine falsche Antwort (fremdes Konto, `KREDIT`,
`KAUTION`, andere IBAN) führt zu `AnbieterAntwortError` ohne Umsatzabruf.
Die Lieferung muss dieselbe Identität tragen und CAMT.053 sein; Import
nur über `importiere_camt053` mit demselben Kontext (kein zweiter
Schreibpfad). Es gibt **keinen** produktiven Adapter, keinen Scheduler
und keine Zugangsdaten; das PHP-Modul `ebics-downloader` ist unberührt.
Nach der Kontoinfo wird die Bindung erneut gelesen: ein inzwischen
widerrufener oder ersetzter Stand verhindert bereits den Umsatzabruf.

## 7. Backoffice

- `GET /backoffice/bank/quellen`: Status je zugänglichem, nicht
  ausgeschlossenem Objekt (nicht konfiguriert / aktiv / widerrufen /
  ungültig), Anbieter/Referenzen, Rolle, maskierte IBAN, Revision,
  Nachweis. "Nicht konfiguriert" ist neutral, nie grün.
- `POST /backoffice/bank/quellen/binden` und `/widerrufen`: Sitzung +
  CSRF, Nachweis Pflicht, `erwarteter_stand` aus der Seite.
- Bankdatei-Import: Auswahl "Objekt mit gebundener Bankquelle"; das
  Bankkonto wird aus der Bindung abgeleitet. Vorschau und Import tragen
  `objekt_id`, `bindung_token`, `quelle_id`, `bank_konto_id`; der Import
  lädt den Kontext neu und lehnt jede Abweichung ab. Ohne Objekt bleibt
  das bisherige Formular für nie konfigurierte Bankkonten nutzbar.

## 8. Nicht geliefert

- Keine echte Einrichtung von Quellen/Bindungen, kein Anbieter, kein
  Live-Abruf, keine automatische Zuordnung.
- Mieter nie konfigurierter Objekte bleiben von jedem Bankkonto ihrer
  Gesellschaft zuordenbar (Legacy), auch von einem gebundenen.
- Keine Prüfsummenvalidierung der IBAN.
- Parallelität ist für den bestehenden Datei-SQLite-Betrieb getestet;
  eine PostgreSQL-Betriebsabnahme dieser Erweiterung ist nicht erfolgt.
- Die Rollen-/Mehrbenutzerprüfung ist auf Service-Ebene getestet; das
  Backoffice kennt weiterhin einen ADMIN-Operator.
