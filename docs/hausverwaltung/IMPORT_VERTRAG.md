# Importvertrag — generischer Echtbetrieb-Intake (HV-20260912-ECHTBETRIEB)

Dieses Dokument ist der verbindliche Schnittstellenvertrag für den
generischen Intake in `src/mietinkasso/intake/` + `scripts/intake_import.py`.
Es wird VOR der vollständigen Implementierung fertiggestellt, damit Codex
das reale Datenmapping parallel beginnen kann. Änderungen an diesem
Vertrag nach Beginn des Mappings werden hier vermerkt, nicht
stillschweigend vorgenommen.

**Geltungsbereich:** Gesellschaften, Objekte, Einheiten, Debitoren,
Verträge, Eröffnungssalden (Stichtag), Nachbuchungen (separat datiert,
NACH dem Eröffnungsstichtag). **Nicht Teil dieses Intakes:** Kaution,
Vertragskomponenten/Zinslisten (siehe `importtemplates/README.md`),
Bankdaten (siehe `bank/importer.py`), Indexklauseln, BK-Abrechnungen —
diese haben eigene, bestehende Importwege oder sind noch offen (siehe
OFFENE_PUNKTE.md). Das Rechnungsmodul (`src/invoice_automation/`) ist
von diesem Intake nicht betroffen.

## Grundprinzipien

- **Zwei getrennte Schritte:** `plan` (rein lesend, keine DB-Schreibung)
  und `apply` (schreibt, genau EINE DB-Transaktion für die GESAMTE
  Datei). `apply` verlangt den `paket_hash` aus dem `plan`-Lauf als
  Bestätigung — ändert sich die Datei zwischen `plan` und `apply` auch
  nur um ein Byte, ändert sich der Hash, und `apply` verweigert die
  Ausführung ("bindet sich an identischen Inhalt").
- **Ein Fehler => gesamter Lauf unverändert:** `apply` öffnet eine
  einzige DB-Session/Transaktion für alle Zeilen aller Entitätstypen.
  Schlägt irgendeine Zeile fehl (unbekannte Referenz, Konflikt,
  Objekt 107, technischer Fehler), wird die GESAMTE Transaktion
  zurückgerollt — keine Teilbuchung.
- **Wiederholung ist wirkungslos, Änderung ist ein Konflikt:** Für
  Stammdaten (Gesellschaft/Objekt/Einheit/Debitor/Vertrag) ist die
  fachliche `id` selbst die Quell-ID. Existiert eine `id` bereits mit
  EXAKT demselben Inhalt (Hash-Vergleich über alle Felder), ist der
  erneute Lauf ein wirkungsloser No-Op. Existiert dieselbe `id` mit
  ABWEICHENDEM Inhalt, ist das ein Konflikt — der ganze Lauf wird
  verweigert, nichts wird stillschweigend überschrieben. Für
  Eröffnungssalden/Nachbuchungen gilt dieselbe Regel über die
  `import_id` (identischer Mechanismus wie der bestehende
  Eröffnungsimport/`OPRepository.insert_idempotent`).
- **Keine Vermischung mit Demo-Daten:** `scripts/intake_import.py`
  verlangt `--database-url` explizit als Pflichtparameter (kein
  Fallback auf die Repo-eigene Demo-SQLite) und verweigert eine
  SQLite-Datei, deren Pfad innerhalb dieses Repositories liegt, sowie
  `:memory:`.
- **Objekt 107 ist ausgeschlossen:** Jede Zeile, die sich (direkt oder
  über Einheit/Vertrag) auf Objekt `107` bezieht, oder ein Objekt mit
  `ausgeschlossen: true`, blockiert den GESAMTEN Lauf.
- **Keine erfundenen Werte:** Fehlt ein Pflichtfeld, wird die Zeile als
  Konflikt/Sperrgrund sichtbar — nie ein Platzhalter- oder Rateweiter.
- **Keine historische Sollstellung:** Der Intake bucht ausschließlich
  Eröffnung (`op_service.eroeffnen_gesamtsaldo`/`eroeffnen_einzel_op`)
  und Nachbuchungen (`op_service.buchen`) direkt auf das Ledger. Er ruft
  NIEMALS `vorschreibung_service` auf — es entsteht dadurch keine neue
  automatische Monats-Vorschreibung.

## Format

Zwei gleichwertige Eingabeformen:

1. **Eine JSON-Datei** mit den unten stehenden Feldern (empfohlen für
   Codex' Mapping-Skript — ein Objekt, keine Zeilenstreams).
2. **Ein CSV-Bündel** — bis zu 7 einzelne CSV-Dateien in einem
   Verzeichnis, mit exakt diesen Dateinamen:
   `gesellschaften.csv`, `objekte.csv`, `einheiten.csv`,
   `debitoren.csv`, `vertraege.csv`, `eroeffnungen.csv`,
   `nachbuchungen.csv`. Fehlende Dateien = leere Liste für diesen Typ.
   Spaltennamen entsprechen 1:1 den JSON-Feldnamen unten; Werte sind
   Text (Zahlen/Daten wie im JSON-Beispiel, also `2026-08-31`,
   `150000`, `true`/`false`).

Beide Formen erzeugen intern dasselbe `IntakePaket` und werden
IDENTISCH geplant/geprüft.

## Feldschema

Alle Beträge sind Integer-Cent. Alle Daten sind `YYYY-MM-DD`. Alle
Enum-Werte sind exakt wie in `domain/enums.py` (Großschreibung).

### `gesellschaften[]`
| Feld | Typ | Pflicht | Hinweis |
|---|---|---|---|
| `id` | str | ja | z. B. `JLB`, `7DI` — eigener ID-Raum, kein Objekt-/Bankbezeichner |
| `name` | str | ja | |

### `objekte[]`
| Feld | Typ | Pflicht | Hinweis |
|---|---|---|---|
| `id` | str | ja | Pilotobjekte `601`/`616`/`617`/`619`; `107` blockiert den GESAMTEN Lauf |
| `gesellschaft_id` | str | ja | muss in `gesellschaften[]` oder DB existieren |
| `bezeichnung` | str | ja | |
| `adresse` | str\|null | nein | |
| `ausgeschlossen` | bool | nein (default `false`) | `true` blockiert wie `id == "107"` |

### `einheiten[]`
| Feld | Typ | Pflicht | Hinweis |
|---|---|---|---|
| `id` | str | ja | eigener ID-Raum |
| `objekt_id` | str | ja | |
| `bezeichnung` | str | ja | |
| `nutzungsstatus` | str | ja | `DAUERVERMIETUNG`\|`KURZZEITVERMIETUNG`\|`EIGENNUTZUNG`\|`LEERSTAND`\|`SELFSTORAGE` — **ohne Vertrag erfassbar**, keine Namens-/Nullsaldo-Heuristik |
| `flaeche_qm` | Decimal-String\|null | nein | |
| `miteigentumsanteile` | Decimal-String\|null | nein | |

### `debitoren[]`
| Feld | Typ | Pflicht | Hinweis |
|---|---|---|---|
| `id` | str | ja | eigener ID-Raum |
| `name` | str | ja | |
| `email` | str\|null | nein | fehlt sie: Vertrag bleibt anlegbar, aber laut bestehendem `mahnwesen/service.py` nie automatisch mahnfähig — im Plan als Hinweiszähler sichtbar |
| `adresse` | str\|null | nein | |

### `vertraege[]`
| Feld | Typ | Pflicht | Hinweis |
|---|---|---|---|
| `id` | str | ja | eigener ID-Raum |
| `einheit_id` | str | ja | |
| `debitor_id` | str | ja | |
| `gesellschaft_id` | str | ja | Forderungsinhaber-Gesellschaft |
| `rechtsordnung` | str | ja | siehe `Rechtsordnung`-Enum |
| `gueltig_von` | Datum | ja | |
| `gueltig_bis` | Datum\|null | nein | |
| `faelligkeit_tag` | int | nein (default `5`) | |
| `zahlungsfrist_tage` | int | nein (default `14`) | |

### `eroeffnungen[]` (Stichtagssaldo, EIN Eintrag pro Konto/Vertrag)
| Feld | Typ | Pflicht | Hinweis |
|---|---|---|---|
| `import_id` | str | ja | Quell-ID für Idempotenz (z. B. `EROEFF-<vertrag_id>`) |
| `vertrag_id` | str | ja | Konto wird daraus automatisch abgeleitet (`get_or_create_konto`) |
| `modus` | str | ja | `GESAMTSALDO` oder `EINZEL_OP` |
| `betrag_cent` | int | ja | bei `EINZEL_OP`: Betrag DIESER Zeile, nicht der Kontosumme |
| `stichtag` | Datum | ja | |
| `quelle_bestaetigt` | bool | **ja, muss `true` sein** | fehlt/`false` => GESAMTER Lauf blockiert ("ungeprüfte Anfangssalden") |
| `typ` | str\|null | nur bei `EINZEL_OP` | `SOLL`\|`GUTSCHRIFT`\|`ZAHLUNG`\|`RUECKLASTSCHRIFT` |
| `belegdatum` | Datum\|null | nein (default = `stichtag`) | |
| `faelligkeit` | Datum\|null | nein | fehlt sie: Position bleibt sichtbar, aber `faelligkeit_bekannt=false` — nie automatisch gemahnt (bestehendes Verhalten, kein neuer Mechanismus) |
| `beleg_referenz` | str | nein (default `"Eröffnungsimport"`) | |

**Ein Konto darf pro Lauf nur EINEN Eröffnungsmodus haben** (Fachregel
2: Gesamtsaldo und Einzel-OP schließen sich aus — bereits in
`op_service._pruefe_und_setze_eroeffnungsmodus` erzwungen). Mehrere
`EINZEL_OP`-Zeilen für dasselbe `vertrag_id` sind erlaubt (ein OP je
Zeile); mehrere `GESAMTSALDO`-Zeilen für dasselbe `vertrag_id` mit
abweichendem Betrag/Stichtag sind ein Konflikt.

### `nachbuchungen[]` (separat datiert, NACH dem Eröffnungsstichtag)
| Feld | Typ | Pflicht | Hinweis |
|---|---|---|---|
| `import_id` | str | ja | Quell-ID für Idempotenz |
| `vertrag_id` | str | ja | |
| `typ` | str | ja | `SOLL`\|`GUTSCHRIFT`\|`ZAHLUNG`\|`RUECKLASTSCHRIFT` |
| `betrag_cent` | int | ja | |
| `belegdatum` | Datum | ja | bei `SOLL`/`GUTSCHRIFT`: muss NACH dem Gesamtsaldo-Stichtag des Kontos liegen (`pruefe_kein_altjournal_in_gesamtsaldo`), sonst Konflikt |
| `buchungsdatum` | Datum | ja | |
| `faelligkeit` | Datum\|null | nein | siehe oben |
| `beleg_referenz` | str | nein (default leer) | |
| `aenderungsgrund` | str\|null | nein | |
| `leistungsperiode` | str\|null (`YYYY-MM`) | nein | |

## JSON-Beispiel (rein synthetisch)

```json
{
  "quelle": "codex-mapping-2026-09-12",
  "gesellschaften": [
    {"id": "JLB", "name": "JLB Projects GmbH"}
  ],
  "objekte": [
    {"id": "601", "gesellschaft_id": "JLB", "bezeichnung": "Am Corso", "adresse": "Am Corso 1, Wien", "ausgeschlossen": false}
  ],
  "einheiten": [
    {"id": "601-TOP1", "objekt_id": "601", "bezeichnung": "Top 1", "nutzungsstatus": "DAUERVERMIETUNG", "flaeche_qm": "65.30"},
    {"id": "601-TOP2", "objekt_id": "601", "bezeichnung": "Top 2 (Keller-Selfstorage)", "nutzungsstatus": "SELFSTORAGE"}
  ],
  "debitoren": [
    {"id": "DEB-1001", "name": "Erika Musterfrau", "email": "erika@example.at"}
  ],
  "vertraege": [
    {"id": "V-601-TOP1", "einheit_id": "601-TOP1", "debitor_id": "DEB-1001", "gesellschaft_id": "JLB",
     "rechtsordnung": "OESTERREICH_MRG_VOLL", "gueltig_von": "2020-01-01"}
  ],
  "eroeffnungen": [
    {"import_id": "EROEFF-V-601-TOP1", "vertrag_id": "V-601-TOP1", "modus": "GESAMTSALDO",
     "betrag_cent": 150000, "stichtag": "2026-08-31", "quelle_bestaetigt": true}
  ],
  "nachbuchungen": [
    {"import_id": "NACH-V-601-TOP1-202609", "vertrag_id": "V-601-TOP1", "typ": "SOLL",
     "betrag_cent": 76000, "belegdatum": "2026-09-01", "buchungsdatum": "2026-09-01",
     "faelligkeit": "2026-09-05", "beleg_referenz": "Miete September 2026"}
  ]
}
```

## CLI-Befehle

```bash
# 1. Dry-run: rein lesend, keine DB-Schreibung. Liest optional gegen eine
#    DB, um NEU/UNVERAENDERT/KONFLIKT zu bestimmen (--database-url), oder
#    rein strukturell ohne DB-Vergleich (--ohne-datenbankvergleich).
python scripts/intake_import.py plan \
    --datei echtdaten.json \
    --database-url sqlite:////pfad/ausserhalb/repo/produktiv.db

# CSV-Bündel statt JSON:
python scripts/intake_import.py plan \
    --verzeichnis /pfad/zum/csv-buendel/ \
    --database-url sqlite:////pfad/ausserhalb/repo/produktiv.db

# 2. Apply: verlangt den paket_hash aus Schritt 1 als Bestätigung.
python scripts/intake_import.py apply \
    --datei echtdaten.json \
    --database-url sqlite:////pfad/ausserhalb/repo/produktiv.db \
    --bestaetige-hash <paket_hash aus dem plan-Lauf> \
    --akteur "codex-intake"
```

- `--database-url` ist IMMER Pflicht (keine Fallbacks). SQLite-Pfade
  innerhalb dieses Repositories oder `:memory:` werden verweigert.
- `plan` gibt Exit-Code `0` nur zurück, wenn der Plan anwendbar ist
  (keine Konflikte/Sperren) — sonst `1` mit lesbarer Begründung je
  Zeile, nichts wird geraten.
- `apply` gibt Exit-Code `0` nur bei vollständigem Erfolg zurück; jeder
  Fehler beendet mit `1` und lässt die DB unverändert (Rollback).

## Sperrgründe (sichtbar im Plan, nicht erfunden)

- **Hart blockierend (gesamter Lauf verweigert):** Objekt 107 (direkt
  oder über `ausgeschlossen: true`), unbekannte Referenz (`einheit_id`/
  `objekt_id`/`debitor_id`/`gesellschaft_id`/`vertrag_id` löst sich
  weder im Paket noch in der DB auf), Konflikt (gleiche `id`/
  `import_id`, abweichender Inhalt), `quelle_bestaetigt` fehlt oder
  `false` bei einer Eröffnungszeile, widersprüchlicher
  Eröffnungsmodus/Doppelbuchung, Nachbuchung mit Belegdatum vor/auf dem
  Gesamtsaldo-Stichtag desselben Kontos.
- **Sichtbare Hinweise (blockieren NICHT, weil das bestehende System sie
  bereits sicher behandelt):** fehlende `faelligkeit` (Position bleibt
  sichtbar, wird nie automatisch gemahnt), fehlende Debitor-`email`
  (Vertrag bleibt anlegbar, wird nie automatisch gemahnt). Der Plan
  zählt beides aus und weist es aus — keine stille Weglassung.

## Status

Dieser Vertrag ist die Grundlage für die Implementierung in derselben
Sitzung (`src/mietinkasso/intake/`, `scripts/intake_import.py`). Codex
kann auf Basis dieses Dokuments bereits mit dem realen Mapping beginnen;
Abweichungen, die sich während der Implementierung als nötig erweisen,
werden hier nachgetragen, nicht stillschweigend geändert.
