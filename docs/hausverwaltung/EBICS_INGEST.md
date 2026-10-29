# EBICS-CAMT.053-Übernahme (HV-20261029-EBICS-INTEGRATION)

Kontrollierte Übernahme vorab abgeholter, je IBAN getrennter CAMT.053-
Dateien in den bestehenden SQLite-Bankimport. Der Abruf selbst ist NICHT
Teil dieses Moduls (vorgelagerter, lokaler, lesender Transport, z. B.
`ebics-downloader/`). Kein Scheduler, keine Bankverbindung, keine
OP-Buchung, keine Zuordnung, keine Forderung/Gebühr, kein Schemawechsel.

Code: `src/mietinkasso/bank/ebics_ingest.py`, CLI `scripts/ebics_ingest.py`,
Tests `tests/mietinkasso/test_ebics_ingest.py`.

## Python-API

```python
plan = plan_ebics_camt053(
    session_factory=sf, bank_konto_id="BK-1", erwartete_iban="AT..",
    camt_bytes=xml, ctx=auth_ctx, datum_toleranz_tage=4,
)
ergebnis = apply_ebics_camt053(
    session_factory=sf, bank_konto_id="BK-1", erwartete_iban="AT..",
    camt_bytes=xml, ctx=auth_ctx, erwarteter_plan_hash=plan.plan_hash,
)
```

- `plan_*`: rein lesend (auch über SQLite `mode=ro`), verlangt
  Gesellschaftszugriff auf das Konto.
- `apply_*`: verlangt Gesellschaftszugriff UND Schreibrecht. Läuft über
  `BankImportService.importiere_ausgewaehlt_atomar` (kleine kompatible
  Erweiterung): dieselbe `schreibgesperrte_session` (SQLite
  `BEGIN IMMEDIATE`), dieselbe `_speichere_roh`-Speicherung, eine
  Transaktion. Der Plan wird UNTER der Sperre neu berechnet; weicht der
  Hash ab -> `EbicsPlanHashAbweichungError`, enthält er KONFLIKT ->
  `EbicsPlanNichtAnwendbarError`; in beiden Fällen wird nichts
  geschrieben. Geschrieben werden ausschließlich NEU-Zeilen mit
  `quelle_typ="CAMT053"` (gleiche `import_id` wie ein manueller
  CAMT-Upload derselben Datei).

## Prüfungen vor jeder Klassifizierung (Ablehnung der gesamten Datei)

- Bankkonto existiert; erwartete IBAN (Transport) = IBAN des DB-Kontos.
- Namespace `urn:iso:std:iso:20022:tech:xsd:camt.053.*`, genau ein
  `BkToCstmrStmt`; kein DOCTYPE/ENTITY.
- Aktueller `parse_camt053`: jedes `Stmt/Acct` = ausgewähltes Konto
  (Fremd-/Mischkonten -> `CamtKontoMismatchError`), Pflichtfelder,
  Sammelbuchungssummen, Währung.
- Jede `Ntry` mit `Sts` = `BOOK` (final) und ausdrücklich `Amt/@Ccy="EUR"`;
  `Acct/Ccy` falls vorhanden EUR.

## Klassifizierung je Zeile

| Status | Bedeutung | Apply |
|---|---|---|
| `NEU` | kein übereinstimmender Bestand | geschrieben |
| `VORHANDEN` | gleiche native ID mit übereinstimmendem Inhalt (quellenübergreifend), ausdrücklicher Exact-Match oder identisches Duplikat in der Datei | nicht geschrieben |
| `UNKLAR` | Gleichbetrag ohne ausreichende Identität, mehrdeutiger Exact-Match, mögliche Sammelbuchung/Zerlegung, dateiintern ununterscheidbar | zurückgehalten, vollständig berichtet |
| `KONFLIKT` | gleiche native ID mit widersprechendem Inhalt (Bestand oder innerhalb der Datei) | gesamter Apply verweigert |

Regeln im Detail:

0. **Bankidentität je Zeile** (Rückprüfung 2, Regelversion
   `EBICS-INGEST-2`), nie aus Namen/Beträgen abgeleitet:

   | Fall | Identität (`identitaet.art`) | gespeichert als |
   |---|---|---|
   | `AcctSvcrRef` genau einmal in der Datei | `ACCTSVCRREF` | `<konto>:CAMT053:<Ref>` (wie CAMT-Upload) |
   | `AcctSvcrRef` mehrfach (Sammel-/Berichtsreferenz), Statement-Id eindeutig | `ACCTSVCRREF_STMT_POSITION` | `<konto>:CAMT053:<Ref>#S<stmt>N<ntry>[T<tx>]` |
   | keine `AcctSvcrRef`, Statement-Id eindeutig | `STMT_POSITION` | `<konto>:CAMT053:NOID:EBICS#S<stmt>N<ntry>[T<tx>]` (weiter `hat_native_id=false` mit Fingerprint) |
   | `AcctSvcrRef` mehrfach, keine eindeutige Statement-Id | `ACCTSVCRREF_NICHT_EINDEUTIG` | nicht geschrieben (UNKLAR) |
   | keine `AcctSvcrRef`, keine eindeutige Statement-Id | `KEINE` | bisheriger NOID-/Fingerprint-Weg |

   `<stmt>` = erste 12 Hex-Zeichen von SHA-256 der originalen `Stmt/Id`
   (Längengrenze `import_id` 128); `<ntry>` = Position der Ntry im
   Statement, `<tx>` = TxDtls-Position bei Sammelposten. Die originale
   `AcctSvcrRef` bleibt Herkunft: im Plan (`identitaet.original_acct_svcr_ref`,
   `ntry_acct_svcr_ref`, `stmt_id`) und in der unveränderten `roh_zeile`.
   Wiederholung derselben Datei findet dieselbe Identität -> VORHANDEN
   (auch ohne native ID). Anderer Inhalt unter derselben zusammengesetzten
   Identität oder an derselben Originalposition (`KONFLIKT_ORIGINALPOSITION_INHALT`)
   -> KONFLIKT. Eine andere Position desselben Original-Statements ist
   eine eigenständige Buchung (identische Zahlungen bleiben getrennt).
   Zwei unterschiedliche CAMT-`AcctSvcrRef` sind nie dieselbe Zahlung:
   gleich aussehend -> `UNKLAR_ANDERE_BANKIDENTITAET`, sonst kein
   Gleichbetrag-Klärfall.

1. **Native ID** (`AcctSvcrRef`) wird gegen jede Bestandszeile desselben
   Kontos mit native ID verglichen, unabhängig von der Quelle (aus
   `import_id` `<konto>:<quelle>:<id>`). Gleiche Quelle CAMT053: gleicher
   `quelle_hash` oder gleicher Kern (Cent, Währung, Buchungsdatum,
   Valuta) ohne Referenz-/IBAN-Widerspruch -> VORHANDEN. Andere Quelle:
   Datum darf kreuzweise Buchung/Valuta exakt übereinstimmen. Sonst
   KONFLIKT.
2. **Exact-Match** ohne native-ID-Treffer: gleicher Centbetrag (inkl.
   Vorzeichen) und Währung, Datum exakt (Buchung=Buchung oder kreuzweise
   Valuta=Buchung), Identität: gleiche Gegenkonto-IBAN oder gleiche/
   enthaltene Referenz (normalisiert, enthaltener Teil >= 8 Zeichen) und
   kein Widerspruch im anderen Merkmal. Eine gespeicherte Gegenkonto-IBAN
   gleich der eigenen Konto-IBAN (Altparser) zählt als fehlend.
   Dedupliziert wird nur eine eindeutige 1:1-Zuordnung bzw. eine
   vollständig ununterscheidbare Gruppe gleicher Größe; jede andere
   Konstellation -> UNKLAR.
3. **Sammelbuchung**: eine nicht zugeordnete Bestandszeile im
   Datumsfenster, die der Summe von >= 2 Dateizeilen entspricht ->
   betroffene Dateizeilen UNKLAR; eine Dateizeile, die der Summe von >= 2
   Bestandszeilen entspricht -> UNKLAR. Mehr als 18 Kandidaten -> alle
   UNKLAR (keine unsichere Teilprüfung).
4. **Gleichbetrag** im Datumsfenster (Standard ±4 Tage über
   Buchung/Valuta) ohne Exact-Match -> UNKLAR.
5. Zeilen ohne jede belegte Identität (Art `KEINE`) mit identischem
   Fingerprint innerhalb der Datei -> UNKLAR.

Für `STMT_POSITION`-Zeilen übergibt der Adapter den Positionsschlüssel an
`BankImportService._speichere_roh(positions_schluessel=...)`: stabile,
UNIQUE-geschützte `import_id` statt UUID; die Fingerprint-Sperre entfällt
nur auf diesem Weg (Entscheidung bereits unter derselben Schreibsperre
getroffen), `fingerprint_hash` bleibt gespeichert, damit ein späterer
CSV-/Datei-Upload derselben Zeile weiter gesperrt wird.

## Plan-/Ergebnisinhalt (JSON)

Plan: `regelversion`, `bank_konto_id`, `gesellschaft_id`, `iban`,
`quelle_sha256`, `quelle_bytes`, `datum_toleranz_tage`, `dokument`
(Namespace, MsgId, je Statement Id/Sequenzen/`*_laut_dokument`,
Buchungsdatum min/max, Hinweis „Abdeckung und Salden nicht geprüft“),
`eintraege` (Status, Grundcode, Text, betroffene Bestands-IDs, Felder,
Quelle: Ntry-Index, TxDtls-Index, NtryRef, Ntry-SHA256, Rohzeile),
`bankbestand_snapshot` (Anzahl, Summe, max ID, Inhalts-SHA256),
`plan_hash`, `zusammenfassung` (`anwendbar`, `abgleich_vollstaendig`).

`abgleich_vollstaendig` ist nur `true`, wenn nichts zurückgehalten wurde,
und sagt NICHTS über Auszugsabdeckung oder Salden. Geld in Cent, Datum
ISO, fehlende Werte `null`.

Der Planhash umfasst Datei-Hash, Konto, Regelversion, Toleranz, jede
Klassifizierung und den Bankbestands-Snapshot - jede Änderung an Datei,
Bestand des Kontos oder Regeln erzwingt einen neuen Plan.

## CLI

```
python scripts/ebics_ingest.py plan  --database-url sqlite:////privat/bank.db \
    --bank-id BK-1 --erwartete-iban AT.. --datei auszug.xml --kontext-datei /privat/operator.json
python scripts/ebics_ingest.py apply ... --expected-plan-hash <plan_hash>
```

Kontextdatei (oder `MIETINKASSO_EBICS_KONTEXT`), außerhalb des Repos:
`{"user_id": "...", "rolle": "BUCHHALTUNG", "gesellschaft_ids": ["..."]}` -
ausdrückliche Gesellschaftsliste Pflicht. Nur bestehende SQLite-Datei
außerhalb des Repos; kein Schema-Anlegen. Exit: 0 vollständig, 1 mit
zurückgehaltenen Einträgen, 2 Konflikt/Planhash abweichend, 3 Eingabe-,
Format-, Konto- oder Berechtigungsfehler.

## Grenzen

- Keine Salden-/Abdeckungsprüfung (Lücken zwischen Auszügen, Saldo-
  kontrolle `Bal`) - separate Transportdaten, später ergänzbar.
- Kein Audit-Log-Eintrag je Apply (wie bisheriger CAMT-Upload);
  Nachweis über Plan-/Ergebnis-JSON.
- UNKLAR-Zeilen haben noch keinen Klärungsweg im Backoffice; Klärung
  erfolgt manuell (z. B. Upload nach Bereinigung) - kein Abgleich-
  Vollständigkeitsstatus solange offen.
- Referenzvergleich bewusst streng: abweichend formatierte
  Verwendungszwecke zwischen Altexporten und CAMT werden UNKLAR statt
  VORHANDEN.
- Sammelbuchungsprüfung nur im Datumsfenster und bis 18 Kandidaten.
- Eine in der Datei eindeutige `AcctSvcrRef` bleibt unqualifiziert:
  verwendet die Bank dieselbe Referenz in einer SPÄTEREN Datei für eine
  andere Buchung, ist das weiterhin KONFLIKT (keine Statement-Info am
  Altbestand). Identitätsqualifizierung setzt eine eindeutige, stabile
  `Stmt/Id` voraus; liefert die Bank denselben Auszug mit anderer Id oder
  umsortierten Positionen, greift nur der wirtschaftliche Abgleich bzw.
  KONFLIKT an gleicher Position.
- Ein manueller CAMT-Upload (Backoffice) einer bereits per EBICS
  übernommenen Datei mit mehrfacher `AcctSvcrRef` kennt die
  qualifizierten Schlüssel nicht; solche Dateien nur über diesen Adapter
  übernehmen.
- Gleiche native ID aus einer anderen Quelle wird als dieselbe Kennung
  behandelt (Auftrag); zufällig gleiche Fremd-IDs führen zu VORHANDEN
  (bei gleichem Kern) oder KONFLIKT.
- Nicht gegen echte Bankdateien gehärtet; Abnahme mit realen Dateien
  durch Codex außerhalb des Repos.
