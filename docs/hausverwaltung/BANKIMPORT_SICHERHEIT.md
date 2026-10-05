# Bankimport-Sicherheit (CAMT.053 / konfigurierbares CSV)

Stand: Branch `codex/hv-bankimport-20261005`, Basis `cc89c2f`. Code:
`src/mietinkasso/bank/{importer,repository,service}.py`, Import-/Vorschau-
Teil von `src/mietinkasso/backoffice/routes/bank.py`. Tests:
`tests/mietinkasso/test_bankimport_sicherheit.py`,
`tests/mietinkasso/test_bankimport_concurrency.py`,
`tests/mietinkasso/test_z_bankimport_routen.py` (nur synthetische Daten).

Dieses Dokument beschreibt, **was** garantiert ist und **was nicht**.

## 1. Bindung an das persistierte Bankkonto

`BankImportService.importiere_camt053`/`importiere_csv` vertrauen dem
übergebenen `BankKontoTable`-Objekt nur noch als Behauptung:

- Vor dem Parsen wird das Konto per ID aus der DB geladen. Unbekannte ID,
  abweichende Gesellschaft oder abweichende normalisierte IBAN
  (Whitespace entfernt, Großschreibung) -> `BankkontoBindungError`
  (Unterklasse von `BindungInkonsistentError`). Veraltete oder
  manipulierte, vom Repository abgelöste Objekte werden so abgelehnt.
- Zugriffs- und Schreibrecht (`require_gesellschaft_access`,
  `require_schreibrecht`) werden gegen die **gespeicherte** Gesellschaft
  geprüft, nicht gegen die behauptete.
- Geparst wird gegen die geprüfte (= persistierte) IBAN.
- Als erstes Statement der bestehenden Schreibtransaktion
  (`schreibgesperrte_session`; Datei-SQLite `BEGIN IMMEDIATE`, sonst
  `SELECT ... FOR UPDATE` auf der Bankkontozeile) wird dieselbe Identität
  samt Rechten erneut geprüft. Eine zwischenzeitliche Änderung von
  Gesellschaft/IBAN lässt den gesamten Import ohne Schreibzugriff scheitern.
- Es werden keine Konto- oder Ledgerdaten verändert und kein Konto
  abgeleitet oder erraten.
- Bankquellenbindung: an derselben Stelle (vor dem Parsen und in der
  Schreibtransaktion) wird die Objekt-Bankquellenbindung geprüft, siehe
  [BANKQUELLENBINDUNG.md](BANKQUELLENBINDUNG.md).

## 2. Eigene Kontospalte im CSV (optional)

`CsvSpaltenMapping.eigene_iban` (als letztes Feld angehängt, positionale
Aufrufe bleiben gültig) benennt die Spalte mit der IBAN des **eigenen,
exportierten** Kontos - nicht die Gegenkonto-Spalte (`gegenkonto_iban`).
`parse_csv(..., erwartete_iban=...)` (keyword-only) lehnt mit
`CsvKontoMismatchError` die **gesamte** Datei vor jeder DB-Änderung ab,
wenn bei gesetzter Spalte:

- die erwartete IBAN leer ist,
- die Spalte im Kopf fehlt oder (auch nach Trim/Groß-/Kleinschreibung)
  mehrfach vorkommt,
- die Spalte zugleich als Gegenkonto-Spalte gemappt ist,
- irgendeine Zeile einen leeren oder (nach derselben Normalisierung wie
  CAMT) abweichenden Wert trägt - auch gemischt mit korrekten Zeilen.

`BankImportService` übergibt die persistierte Konto-IBAN. Das Backoffice
reicht das optionale Feld `spalte_eigene_iban` über Formular -> Vorschau
-> Hidden-Feld -> Import durch und prüft **in Vorschau und Import**
(der Import vertraut der Vorschau nicht). Fehler erscheinen als
Fehlerseite (HTTP 400), nicht als 500. Die Vorschau zeigt eine kurze,
ehrliche Aussage zum Kontonachweis (CAMT geprüft / CSV-Spalte geprüft /
kein Kontonachweis aus der Datei).

**Grenzen:**

- Legacy-CSV ohne eigene Kontospalte bleibt unverändert importierbar. Die
  Kontozuordnung beruht dann **allein auf der Auswahl** des Operators;
  die Datei liefert keinerlei Kontonachweis.
- Auch mit eigener Kontospalte ist das eine **Selbstauskunft der
  Exportdatei**, kein bankseitiger Herkunftsnachweis (keine Signatur,
  keine Bankverbindung). Wer die Datei bearbeiten kann, kann die Spalte
  setzen.
- Die Bindung eines Provider-/Bankzugangs und seiner Kontorolle an Objekt
  und Bankkonto ist inzwischen im Code umgesetzt, siehe
  [BANKQUELLENBINDUNG.md](BANKQUELLENBINDUNG.md): für konfigurierte
  Bankkonten ist die eigene CSV-Kontospalte Pflicht und ein ungebundener
  Import abgelehnt. Nie konfigurierte Bankkonten behalten das hier
  beschriebene Verhalten. Echte Einrichtung und Anbieteranbindung stehen
  aus.
- Quellen ohne jede IBAN-Angabe bleiben ohne Kontonachweis; dafür gibt es
  hier keine Lösung.

## 3. Native Bank-IDs über Formatgrenzen (CSV <-> CAMT053)

Vertrag: `CsvSpaltenMapping.eindeutige_referenz` darf **nur** eine von
der Bank vergebene, je Bankkonto eindeutige Buchungs-ID sein (dieselbe
Kennung wie z. B. CAMT `AcctSvcrRef`). Zeilennummern, Laufindizes oder
selbst gebaute Schlüssel sind keine native ID.

Ohne Schemaänderung (bestehendes `import_id`-Schema
`<bank_konto_id>:<quelle_typ>:<native_id>` und bestehender
`quelle_hash`) prüft `_speichere_roh` innerhalb der Schreibtransaktion
für jede Zeile mit nativer ID **alle** Formate:

- Kandidaten werden exakt per `IN` auf die je Format gebildeten
  `import_id`s gesucht - kein `LIKE`/Präfix, `:`/`%`/`_` bleiben wörtlich.
  Ein Kandidat zählt nur, wenn Bankkonto, Format und native ID ihn exakt
  erzeugen. Eine reine Zeichenkettenkollision (z. B. Konto `A:CSV`/ID `x`
  vs. Konto `A`/ID `CSV:x`) ist nie ein Replay; landet ein Insert auf
  einer fremd belegten `import_id`, wird er als `ImportConflictError`
  abgelehnt.
- Genau ein Treffer mit identischem Inhalts-Hash -> dieselbe Zeile wird
  zurückgegeben (Replay, unabhängig vom Format).
- Abweichender Hash -> `ImportConflictError`; die ganze Datei rollt
  zurück, auch bereits davor eingefügte neue Zeilen.
- Mehr als ein Treffer (historische Doppelzeilen aus beiden Formaten) ->
  `ImportConflictError` zur manuellen Klärung, auch wenn bereits eine
  Zeile im selben Format existiert. Bestehende Zeilen werden **nie**
  automatisch gelöscht, zusammengelegt oder geändert.
- Gleiche native ID auf einem anderen Bankkonto ist eine eigene Zeile.
- Verschiedene native IDs werden nie über Betrag/Name/Referenz
  zusammengelegt.
- Zeilen ohne native ID: unverändert konservativer Fingerprint-Konflikt
  (`MehrfachbuchungsKonfliktError`).

**Grenzen:** Der Inhalts-Hash umfasst Betrag, Währung, Buchungs-/
Valutadatum, Referenz, Gegenkonto-IBAN/-Name und native ID. Liefern CSV-
Export und CAMT für dieselbe Buchung abweichende Texte (z. B. andere
Referenzaufbereitung), entsteht bewusst ein Konflikt zur manuellen
Klärung statt einer stillen Zusammenlegung. Die formatübergreifende
Prüfung ist eine Lese-dann-Schreib-Entscheidung; sie ist unter
Datei-SQLite durch `BEGIN IMMEDIATE` und unter PostgreSQL durch die
Zeilensperre auf dem Bankkonto serialisiert, hat aber (anders als die
gleiche-Format-`import_id`) keinen eigenen DB-UNIQUE-Backstop.
