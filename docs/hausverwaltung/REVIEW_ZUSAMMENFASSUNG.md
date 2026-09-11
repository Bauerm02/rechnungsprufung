# Review-Zusammenfassung — Mietinkasso-Modul

Stand: erster lauffähiger, getesteter Baustand von HV-20260911-MVP1,
zur unabhängigen Prüfung durch Codex. "Erste Lieferung" heißt hier: alle
in RAHMENPROGRAMM.md geforderten Bausteine sind implementiert und
getestet; rechtlich/fachlich heikle Bereiche (Index, BK-Umlageschlüssel,
Mahntexte) sind bewusst als versionierte, geprüft freizugebende
Regelprofile gebaut statt als vermeintlich fertige Wahrheit — Details
dazu in `OFFENE_PUNKTE.md`.

## Repo-Befund (vor Implementierung)

- Stack: Python 3.11, FastAPI, SQLAlchemy 2.0, Pydantic v2, pytest,
  SQLite. Kein AGENTS.md/CLAUDE.md vorhanden (jetzt ergänzt).
- `src/invoice_automation/` ist ein Kreditoren-/Eingangsrechnungs-
  Scaffold (OCR → Validierung → Duplikatserkennung → Zahlungs-XML,
  Migration von Zapier). Es gibt dort **keine** Debitoren-, OP-,
  Bank-, Mahnwesen- oder Vertragslogik — ein anderer Bounded Context
  (Kreditoren/AP) als der beauftragte (Debitoren/AR, Hausverwaltung).
- Entscheidung: eigenständiges, klar getrenntes Modul
  `src/mietinkasso/` im selben Repo/Branch (kein Import in beide
  Richtungen zu `invoice_automation`), damit es bei Bedarf portabel in
  ein eigenes Repo/Service verschoben werden kann.

## Implementiert

| Baustein | Ort | Status |
|---|---|---|
| Domäne (Enums, Money/Rundung inkl. Halbcent) | `domain/` | fertig + getestet |
| Stammdaten (Gesellschaft/Objekt/Einheit/Vertrag/Komponenten/Kaution/Sperre) | `stammdaten/` | fertig + getestet |
| Auth/Rollen (Gesellschafts-Scoping, DB-seitig durchgesetzt) | `auth/` | fertig + getestet |
| Audit-Log | `audit/` | fertig (Basisversion, append-only) |
| Debitoren-Ledger (Eröffnung, Buchen, Storno/Korrektur, Saldo) | `op/` | fertig + getestet |
| Eröffnungs-CSV-Import (Gesamtsaldo/Einzel-OP) | `op/eroeffnung_import.py` | fertig + getestet |
| Mietvorschreibung (Entwurf→Sollstellung→Zustellung→Export) | `vorschreibung/` | fertig + getestet |
| Bankabgleich (CAMT.053/CSV-Import, Zuordnung, Rücklastschrift) | `bank/` | fertig + getestet |
| Indexanpassung (versionierte Klausel, Halbcent-Rundung) | `index/` | fertig + getestet |
| Betriebskostenabrechnung | `bk/` | fertig + getestet |
| Mahnwesen (genau 2 Stufen, Outbox, Sperren, kein Doppelversand) | `mahnwesen/` | fertig + getestet |
| Jobs/Scheduler-Sicherheit (idempotente Läufe) | `jobs/` | fertig + getestet |
| API + Read-Only-Dashboard | `api/` | fertig (Smoke-getestet, siehe unten) |
| Synthetic-Data-Seed | `scripts/seed_synthetic_data.py` | fertig, idempotent geprüft |
| Importvorlagen | `importtemplates/` | fertig, gegen echte Testfälle geprüft |

## Testergebnis

```
python -m pytest tests/mietinkasso -q    # 42 passed
python -m pytest -q                       # 147 passed (105 invoice_automation + 42 mietinkasso)
```

Die bestehende `invoice_automation`-Testsuite ist unverändert grün
geblieben. `api/app.py` hat keine automatisierten HTTP-Tests (kein
`httpx` als zusätzliche Abhängigkeit eingeführt), wurde aber manuell
gegen eine echte SQLite-DB smoke-getestet (`/health`, `/`, Routing).
Das ist in `OFFENE_PUNKTE.md` als Lücke vermerkt.

## Beim Bauen selbst gefundene und behobene Fehler (Beispiele)

Diese Punkte lohnen eine gezielte Gegenprüfung durch Codex, weil sie
zeigen, wo die Fachregeln leicht falsch zu implementieren sind:

- **Skalierungsfehler bei der BK-Umlage:** `to_cents()` erwartet einen
  Euro-Betrag und multipliziert intern mit 100; die erste Version von
  `bk/service.py` hat das versehentlich auf einen bereits in Cent
  vorliegenden Wert angewendet (Faktor-100-Fehler). Durch den Test
  `test_weg_ruecklage_erzeugt_keinen_automatischen_mieter_op` sofort
  aufgefallen und behoben.
- **"Fälliger unstrittiger Rest" schloss Zahlungen fälschlich aus:**
  `faelligkeit_bekannt` wurde ursprünglich für ALLE OP-Typen als
  Ausschlusskriterium behandelt; da Zahlungen typischerweise kein
  eigenes Fälligkeitsdatum haben, wurden sie komplett aus dem mahnbaren
  Betrag herausgefiltert, statt die Forderung zu mindern. Fachlich
  korrekt: Zahlungen/Gutschriften mindern den mahnbaren Betrag immer,
  nur Forderungen (Eröffnung/Soll/Rücklastschrift) brauchen eine
  bekannte, verstrichene Fälligkeit. Gefunden über
  `test_zahlung_zwischen_planung_und_versand_stoppt_versand`.
- **Wall-Clock statt fachlichem Datum:** mehrere Stellen (`sollstellen`,
  `storniere_und_korrigiere`, `ergebnisse_buchen`) haben intern
  `date.today()` für `buchungsdatum` verwendet, statt das vom Aufrufer
  übergebene fachliche `heute` durchzureichen. In Produktion meist
  unsichtbar (beides ist "heute"), aber ein Determinismus-/Testbarkeits-
  problem und potenziell ein Bug bei Nachbuchungen mit Rückdatum. Alle
  drei Stellen nehmen jetzt ein explizites `heute`-Argument.

## Wichtige Konstruktionsentscheidungen

## Wichtige Konstruktionsentscheidungen

- **Geld:** integer Cent überall im Ledger; `Decimal` nur für die
  Index-Rechenformel und die gesetzliche Halbcent-Rundung
  (`domain/money.py`), die ausdrücklich von normaler kaufmännischer
  Rundung getrennt implementiert ist.
- **Idempotenz:** jede importierte Zeile (OP, Bank) trägt eine
  `import_id` + `quelle_hash`. Wiederholung mit gleichem Inhalt ist ein
  No-Op; gleiche ID mit anderem Inhalt ist ein `ImportConflictError`.
- **Korrektur statt Überschreiben:** `op/service.py::storniere_und_korrigiere`
  markiert die Originalzeile `STORNIERT` und fügt bei Bedarf eine neue
  aktive Zeile hinzu; nichts wird in-place verändert.
- **Tenant-Trennung DB-seitig:** `bank/repository.py::create_zuordnung`
  vergleicht die Gesellschaft von Banktransaktion und Zielkonto anhand
  frisch aus der DB gelesener Zeilen und lehnt sonst mit
  `CrossTenantError` ab — nicht nur eine Service-Konvention.
- **Keine Heuristiken:** automatische Zahlungszuordnung nur über eine
  eindeutige, explizite Vertragsreferenz; Nutzungsstatus wird nie aus
  Soll=0 abgeleitet; Namens-/Betragsgleichheit allein reicht nirgends.
