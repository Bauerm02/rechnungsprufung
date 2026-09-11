# Importvorlagen — Mietinkasso

Alle Beispieldaten hier sind synthetisch. Spaltennamen sind so gewählt,
dass sie 1:1 zu den Parametern der jeweiligen Importfunktion passen.

## `eroeffnung_gesamtsaldo.csv`

Für Konten, die mit einem **bestätigten Gesamtsaldo** eröffnet werden
(z. B. aus den Deb-/Kred-/Sachkontensalden). Wird geparst mit
`mietinkasso.op.eroeffnung_import.parse_eroeffnung_csv` und über
`importiere_eroeffnung_csv(..., konten_je_id=...)` gebucht. `import_id`
ist optional — fehlt sie, wird sie deterministisch aus dem Zeileninhalt
abgeleitet (Wiederholimport bleibt trotzdem wirkungslos).

**Wichtig:** Ein Konto darf NIE sowohl mit `GESAMTSALDO` als auch mit
`EINZEL_OP` eröffnet werden (siehe `DoppelteEroeffnungsartError`) — pro
Konto ist vorab zu entscheiden, welche Quelle (bestätigter Saldo ODER
Einzelposten-Journal) tatsächlich verwendet wird.

## `eroeffnung_einzel_op.csv`

Für Konten, die mit **Einzel-OPs zum Stichtag** eröffnet werden (z. B.
aus dem Journal). `typ` ist einer von `SOLL`, `GUTSCHRIFT`, `ZAHLUNG`,
`RUECKLASTSCHRIFT`. `faelligkeit` darf leer bleiben, wenn sie aus der
Quelle nicht zweifelsfrei hervorgeht — die Position bleibt dann sichtbar,
wird aber nie automatisch bemahnt (Fachregel 2).

## `bank_csv_import.csv`

Für den konfigurierbaren CSV-Bankimport
(`mietinkasso.bank.importer.parse_csv` mit einer
`CsvSpaltenMapping(betrag="betrag", buchungsdatum="datum", ...)`).
Automatische Zahlungszuordnung greift NUR, wenn die `referenz`-Spalte
eine eindeutige Vertragskennung im Format `VERTRAG:<vertrag_id>` enthält
— Name oder Betrag allein reichen nie aus. Eine Rücklastschrift wird
nicht automatisch aus dem Vorzeichen erkannt, sondern erfordert eine
explizite Zuordnung zur ursprünglichen Zahlung
(`bank.service.BankImportService.verarbeite_ruecklastschrift`).

## CAMT.053

Es gibt hier keine separate Beispieldatei; ein minimales, funktionsfähiges
Beispiel-XML mit genau dieser Struktur (Ntry/Amt/CdtDbtInd/BookgDt/
RmtInf/AcctSvcrRef) steht direkt in
`tests/mietinkasso/test_bank.py` (`CAMT_XML`), damit Vorlage und Test
nie auseinanderlaufen.

## Zinsliste (Vertragskomponenten) — noch kein automatischer Import

Für die Übernahme bestehender Zinslisten in `VertragsKomponenteTable`
(HMZ/Küche/Parkplatz/BK-Vorauszahlung/...) gibt es in diesem Checkpoint
bewusst noch KEINE automatische Parserfunktion: das reale Format (zwei
Zinslisten unterschiedlicher Abdeckung laut Auftrag) muss zuerst über
das Quellenmapping von Codex gegen die tatsächlichen Dateien geklärt
werden. `stammdaten.repository.StammdatenRepository.add_komponente`
nimmt bereits alle nötigen Felder programmatisch entgegen; ein CSV-Layer
darüber ist ein offener Punkt (siehe
`docs/hausverwaltung/OFFENE_PUNKTE.md`).
