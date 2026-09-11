# Review-Zusammenfassung — Mietinkasso-Modul (Checkpoint)

Dieser Stand ist ein **Zwischen-Checkpoint** zur parallelen, unabhängigen
Prüfung durch Codex — kein finaler Abschluss. Er wird bis zur
Endlieferung aktualisiert (finale Version enthält Mahnwesen, Jobs/API,
Synthetic-Data-Seed, Betriebsanleitung vollständig).

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

## Bisher implementiert (dieser Checkpoint)

| Baustein | Ort | Status |
|---|---|---|
| Domäne (Enums, Money/Rundung) | `domain/` | fertig |
| Stammdaten (Gesellschaft/Objekt/Einheit/Vertrag/Komponenten/Kaution/Sperre) | `stammdaten/` | fertig |
| Auth/Rollen (Gesellschafts-Scoping) | `auth/` | fertig |
| Audit-Log | `audit/` | fertig (Basisversion) |
| Debitoren-Ledger (Eröffnung, Buchen, Storno/Korrektur, Saldo) | `op/` | fertig + getestet |
| Mietvorschreibung (Entwurf→Sollstellung→Zustellung→Export) | `vorschreibung/` | fertig + getestet |
| Bankabgleich (CAMT.053/CSV-Import, Zuordnung, Rücklastschrift) | `bank/` | fertig + getestet |
| Indexanpassung (versionierte Klausel, Halbcent-Rundung) | `index/` | fertig + getestet |
| Betriebskostenabrechnung | `bk/` | Service fertig, Tests folgen |
| Mahnwesen (2 Stufen, Outbox, Sperren) | `mahnwesen/` | in Arbeit |
| Jobs/Scheduler-Sicherheit | `jobs/` | in Arbeit |
| API/UI | `api/` | in Arbeit |
| Synthetic-Data-Seed | `scripts/` | in Arbeit |

## Testergebnis (dieser Checkpoint)

```
python -m pytest tests/mietinkasso -q
```

Siehe Commit-Historie für den genauen Zählerstand zum jeweiligen
Commit; die bestehende `invoice_automation`-Testsuite (`pytest`, ohne
Pfadeinschränkung) bleibt unverändert grün.

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
