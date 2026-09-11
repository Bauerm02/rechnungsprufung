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

**`buchungs_id` (→ `CsvSpaltenMapping.eindeutige_referenz`) ist die
bankseitig eindeutige Transaktionskennung** und sollte, wenn die Bank so
eine Spalte liefert, IMMER gemappt werden. Ohne sie behandelt der Import
jede Zeile als "ohne eindeutige Kennung": ein zweiter Import mit
identischem Konto/Datum/Betrag/Referenz (z. B. aus einem überlappenden
Tagesexport) wird dann NICHT still zusammengelegt und NICHT still ein
zweites Mal gebucht, sondern mit `MehrfachbuchungsKonfliktError`
abgelehnt, weil eine echte zweite, zufällig identisch aussehende Zahlung
nicht automatisch von einer Wiederholung unterschieden werden kann.

## CAMT.053

Es gibt hier keine separate Beispieldatei; ein minimales, funktionsfähiges
Beispiel-XML mit genau dieser Struktur (Ntry/Amt/CdtDbtInd/BookgDt/
RmtInf/AcctSvcrRef) steht direkt in
`tests/mietinkasso/test_bank.py` (`CAMT_XML`), damit Vorlage und Test
nie auseinanderlaufen.

## George-Business-CSV (nur Vorschau, kein Import)

`mietinkasso.bank.george_business_csv.erstelle_preview` liest den
CSV-Kontoumsatz-Export von George Business rein lesend und liefert eine
auditierbare Vorschau (`GeorgeZeilenErgebnis` je Datenzeile). Eigenes,
unabhängiges CLI-Skript: `scripts/george_business_preview.py`. **Kein
Datenbankimport, keine Bankverbindung, kein Versand, keine Buchung** —
und bewusst getrennt vom bestehenden `bank.importer`/`bank.service`
(CAMT.053/konfigurierbares CSV), das unverändert bleibt.

Erwartete Spalten (exakt, siehe `ERWARTETE_SPALTEN`): `(Sammel-)
Überweisung ID`, `Enthaltene Überweisung ID`, `Eigene IBAN`, `Eigener
Kontoname`, `Buchungsdatum`, `Durchführungsdatum`, `Durchführungszeit`,
`Kontoauszug / Rechnung`, `Partner Name`, `Partner IBAN`, `Partner BIC`,
`Partner Kontonummer`, `Partner Bankleitzahl`, `Betrag`, `Währung`,
`Buchungs-Details`, `Buchungsreferenz`, `Valutadatum`,
`Zahlungsreferenz`, `Auftraggeber-Referenz`. UTF-8 mit optionalem BOM,
Komma-getrennt, gequotete Felder. Beträge österreichisch (`1.234,56`,
`-123,45`, immer genau zwei Nachkommastellen), `Buchungsdatum`/
`Valutadatum` als `TT.MM.JJJJ`, nur `EUR`. Eine abweichende Kopfzeile
oder eine XLSX-Datei (ZIP-Signatur) wird abgelehnt statt spekulativ als
CSV mit Lücken gelesen.

**Sammel-Summenzeilen (wichtigste Einschränkung, nach unabhängiger
Codeprüfung korrigiert):** Der Export kann sowohl die Summenzeile einer
Sammelüberweisung als auch deren Einzelposten enthalten. Die sichtbare
Spalte `(Sammel-) Überweisung ID` ist dabei **kein zuverlässiger
Gruppenschlüssel** — die Summenzeile trägt laut Fachprüfung dieselbe ID
wie NUR der erste Einzelposten, die übrigen Detailzeilen tragen jeweils
eigene IDs. Weder diese Spalte noch `Buchungsreferenz` sind daher allein
ein eindeutiger Transaktions-/Gruppenschlüssel und werden nie pauschal
zur Deduplizierung verwendet.

Eine Sammelgruppe wird stattdessen über `(Eigene IBAN, Währung,
Buchungsdatum, eine ECHTE/brauchbare Buchungsreferenz)` zusammengeführt
und nur dann als vollständig aufgelöst behandelt, wenn zusätzlich:

1. jede beteiligte Zeile über `Enthaltene Überweisung ID` eindeutig als
   Detail (`D`) oder Summe (`S`) erkennbar ist — nach einem unabhängig
   strukturell bestätigten, aber ohne echte Produktions-IDs verifizierten
   107-Zeichen-Profil: `Eigene IBAN(20) + 14 Nullen + Währung(3) +
   numerisches Präfix(9, NICHT als Datum interpretiert) + Jahr(4) +
   Marker(S/D) + Hex-Hash(56)`,
2. alle beteiligten Zeilen dasselbe eingebettete 9-stellige
   Gruppenpräfix tragen (Konsistenzprüfung),
3. die Summenzeile eine `(Sammel-) Überweisung ID` trägt, die mit der ID
   mindestens einer Detailzeile übereinstimmt,
4. die Detailbeträge sich centgenau exakt auf die Summenzeile addieren.

Summen können vor oder nach ihren Details in der Datei stehen — die
Reihenfolge spielt keine Rolle. Jede Abweichung (fehlende Gegenstücke,
uneindeutige Markierung, inkonsistentes Gruppenpräfix, abweichende
Summe, eine isolierte S- oder D-Zeile ohne vollständige Gegengruppe)
führt NICHT zu einer geratenen Klassifizierung, sondern die GESAMTE
betroffene Gruppe erscheint als `PRUEFFALL`. Eigenständige normale
Einzelumsätze (andere ID-Struktur) bleiben über eine inhaltsbasierte,
stabile Kandidatenkennung (`kandidaten_id`) plus eigene Dublettenprüfung
erhalten — identische `Enthaltene Überweisung ID` auf demselben Konto
oder vollständig identische Rohdatensätze ohne brauchbare ID werden nie
stillschweigend zusammengelegt, sondern ebenfalls als `PRUEFFALL`
ausgewiesen.

Zeilen auf einem anderen als dem angefragten Konto werden `ABGELEHNT`;
Zeilen auf dem richtigen Konto, aber außerhalb des angefragten
Zeitraums, werden `PRUEFFALL` — beide zählen nie als `KANDIDAT` und
fließen nie in die Summenbildung oder Saldenkontrolle ein. Eine Datei
ganz ohne Datenzeile (nur Kopfzeile) gilt nicht als bestätigt
vollständig und wird abgelehnt.

Eine Zeile ohne S/D-Sammelmarkierung braucht trotzdem MINDESTENS eine
brauchbare Kennung — `Enthaltene Überweisung ID` ODER `(Sammel-)
Überweisung ID` — um automatisch `KANDIDAT` zu werden; sind BEIDE
leer/`NOTPROVIDED`, wird die Zeile ein `PRUEFFALL` (eine
`Buchungsreferenz` allein ist kein eindeutiger Schlüssel), unabhängig
davon, ob die Datei eine oder mehrere Zeilen enthält. Eine ID, die lang
genug für einen Versuch des 107-Zeichen-S/D-Profils ist, aber inhaltlich
davon abweicht (falsches Konto/Währung/Padding/Jahr/Hex, abgeschnittener
Suffix), wird NIE als gewöhnlicher Einzelumsatz durchgereicht, sondern
selbst ein `PRUEFFALL` und "poisoned" jede sonst zufällig valide
erscheinende Sammelgruppe mit gleichem Konto/Währung/Datum/Referenz —
AUSSER sie entspricht exakt einem separat bestätigten anderen
Einzelumsatz-ID-Format (aktuell: 118 Zeichen = IBAN+14 Nullen+Währung+
17-stelliges opakes Präfix+64-stelliger Hex-Hash, KEIN S/D-Profil).

`felder`/`sha256_zeile` je Ergebniszeile bewahren die EXAKTEN dekodierten
CSV-Werte (kein `.strip()`) — Normalisierung für Konto-/Datums-/
Betragsvergleiche passiert getrennt nur für die fachliche Auswertung.
`datei_sha256` ist der SHA256 der rohen Dateibytes.

**Weitere bewusste Grenzen:** `Zahlungsreferenz`/`Buchungs-Details`/
`Auftraggeber-Referenz` werden ausschließlich als Anzeigetext behandelt,
nie als Anweisung ausgeführt — keine automatische Mieterlös-/
Mieterkonto-Ableitung, keine Umbuchungs-/Darlehens-/Drittzahler-Logik.
`erwartetes_konto_iban`/`von`/`bis` sind explizite Prüfparameter: jede
Zeile wird dagegen geprüft, aber keine Zeile wird deshalb aus dem
Ergebnis entfernt — nur Summenbildung und die optionale Saldenkontrolle
(nur bei explizit angegebenem Anfangs- UND Endsaldo) berücksichtigen
ausschließlich Zeilen, die auf beide Parameter passen. Das jüngste
Buchungsdatum in der Datei ist kein Beweis für eine bis zum Abrufdatum
lückenlose Bankanbindung (siehe `docs/hausverwaltung/OFFENE_PUNKTE.md`).

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
