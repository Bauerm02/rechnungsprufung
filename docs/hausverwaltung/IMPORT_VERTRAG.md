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
  Bestätigung. **Wichtig, `paket_hash` ist SEMANTISCH, keine
  Datei-Prüfsumme:** er wird über das bereits geparste `IntakePaket`
  (alle Felder aller Zeilen, kanonisch als JSON serialisiert) gebildet,
  NICHT über die rohen Bytes der Eingabedatei. Eine rein kosmetische
  Änderung an der Quelldatei, die am geparsten Inhalt nichts ändert
  (z. B. andere Einrückung/Zeilenumbrüche in der JSON-Datei, eine
  andere Spaltenreihenfolge im CSV-Bündel, führende/nachgestellte
  Leerzeichen in einem Feld), ändert den Hash NICHT — `apply` würde in
  diesem Fall denselben Hash akzeptieren. Ändert sich dagegen IRGENDEIN
  geparster Feldwert (auch nur einer einzigen Zeile), ändert sich der
  Hash, und `apply` verweigert die Ausführung ("bindet sich an
  identischen semantischen Inhalt", nicht an identische Datei-Bytes).
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
2. **Ein CSV-Bündel** — bis zu 12 einzelne CSV-Dateien in einem
   Verzeichnis, mit exakt diesen Dateinamen:
   `gesellschaften.csv`, `objekte.csv`, `einheiten.csv`,
   `debitoren.csv`, `vertraege.csv`, `eroeffnungen.csv`,
   `nachbuchungen.csv`, `eroeffnungskorrekturen.csv`, `sperren.csv`,
   `komponenten.csv` (siehe "Ergänzung 12.09.2026" unten),
   `kautionen.csv`, `mietvertragsprofile.csv` (siehe "Ergänzung
   13.09.2026" unten). Fehlende Dateien = leere Liste für diesen Typ.
   Spaltennamen entsprechen 1:1 den JSON-Feldnamen unten; Werte sind
   Text (Zahlen/Daten wie im JSON-Beispiel, also `2026-08-31`,
   `150000`, `true`/`false`).

Beide Formen erzeugen intern dasselbe `IntakePaket` und werden
IDENTISCH geplant/geprüft.

## Feldschema

Alle Beträge sind Integer-Cent. Alle Daten sind `YYYY-MM-DD`. Alle
Enum-Werte sind exakt wie in `domain/enums.py` (Großschreibung).

**Explizite Betragssemantik (Codex-Rückprüfung, klargestellt):**

- **`betrag_cent` ist IMMER positiv einzugeben** für `nachbuchungen[]`
  (`SOLL`/`GUTSCHRIFT`/`ZAHLUNG`/`RUECKLASTSCHRIFT`), für
  `eroeffnungen[]` mit `modus: "EINZEL_OP"` und für
  `eroeffnungskorrekturen[]` — das Vorzeichen (mindernd bei
  `GUTSCHRIFT`/`ZAHLUNG`, erhöhend bei `SOLL`/`RUECKLASTSCHRIFT`) wird
  IMMER aus `typ` abgeleitet (`op/service.py::_effect_cent`), niemals
  aus dem Vorzeichen des Eingabewerts. Ein negativer Wert wird technisch
  blockiert (Plan zeigt KONFLIKT) statt ihn ein zweites Mal zu negieren
  und z. B. eine `GUTSCHRIFT` versehentlich zur Schulderhöhung zu
  machen.
- **Ausnahme: `eroeffnungen[]` mit `modus: "GESAMTSALDO"` darf negativ
  sein** — das ist eine Nettosumme (kein `typ`-Vorzeichen), ein
  negativer Wert ist ein legitimes Guthaben zum Eröffnungsstichtag.
- **`komponenten[].betrag_cent` ist BRUTTO** — exakt der Betrag, der bei
  Sollstellung gebucht wird (siehe `domain/money.py::zerlege_brutto_cent`).
  `ust_satz_promille` dient AUSSCHLIESSLICH dem Netto/USt-Ausweis am
  Beleg und verändert NIE den gebuchten Gesamtbetrag. Zulässige Werte:
  `0` (0 %), `10000` (10 %), `20000` (20 %) — jeder andere Wert wird
  abgelehnt (`UStSatzUngueltigError`), nicht stillschweigend gerundet
  oder interpretiert.

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

## Ergänzung 12.09.2026: vier zusätzliche, optionale Entitätstypen

Nach Beginn des Mappings ausdrücklich als generische, weiterhin rein
synthetisch getestete Erweiterung nachgetragen — kein neuer Vertrag,
sondern eine Fortschreibung desselben. Alle vier sind optional (leere/
fehlende Liste = kein Effekt) und laufen über denselben atomaren
`plan`/`apply`-Zyklus wie die bestehenden Entitätstypen.

### `rechtsordnung: "UNGEKLAERT"` (kein neues Feld, ein neuer gültiger Wert)

Die `Rechtsordnung`-Enum hat einen neuen Wert `UNGEKLAERT` — bewusst
STATT eine der bestehenden Kategorien zu erraten, wenn die rechtliche
Einordnung eines Vertrags beim Import noch nicht feststeht. Ein Vertrag
mit `rechtsordnung: "UNGEKLAERT"` wird ganz normal angelegt (kein
Sperrgrund, nur ein Hinweiszähler im Plan), ist aber ab sofort
technisch gesperrt für:

- **Sollstellung** (`vorschreibung/service.py::sollstellen` wirft
  `RechtsordnungUngeklaertError`),
- **Index-Anpassung** (`index/service.py::klausel_anlegen`/
  `berechne_vorschlag` wirft dieselbe Exception),
- **Mahnung** (`mahnwesen/service.py::plane_forderung`/`versenden`
  liefert `BLOCKIERT` — kein Wurf, da diese Methoden generell Ergebnisse
  statt Exceptions liefern).

Sobald die Rechtsordnung geklärt ist, hebt ein erneuter Intake-Lauf mit
demselben `vertrag_id` und der korrekten Rechtsordnung die Sperre auf
(gewöhnlicher Stammdaten-Upsert, kein Sonderpfad nötig).

### `sperren[]` — dauerhafte Prüfhinweise/Sperrgründe je Vertrag

Nutzt die BESTEHENDE `SperreTable`/`StammdatenRepository.aktive_sperren`,
die `mahnwesen/service.py` bereits als harte Mahnsperre auswertet — kein
neuer Sperrmechanismus, nur ein neuer Einspielweg dafür.

| Feld | Typ | Pflicht | Hinweis |
|---|---|---|---|
| `vertrag_id` | str | ja | muss im Paket oder in der DB existieren |
| `grund` | str | ja | ein Wert der `Sperrgrund`-Enum, z. B. `RECHTSANWALT`\|`RATENPLAN`\|`MANUELL`\|`INSOLVENZ`\|... |
| `kommentar` | str\|null | nein | |

Idempotenz: eine bereits AKTIVE Sperre mit identischem `(vertrag_id,
grund, kommentar)` ist ein wirkungsloser Replay (`UNVERAENDERT`); eine
inhaltlich andere Sperre (anderer `grund` oder `kommentar`) ist eine
ZUSÄTZLICHE, eigenständige Sperre (additiver Fakt, kein Ersatz) — kein
Konflikt, da mehrere gleichzeitig aktive Sperren je Vertrag fachlich
normal sind. Ein Aufheben einer Sperre ist NICHT Teil dieses Intakes
(bleibt ein manueller Backoffice-Schritt, `sperre_aufheben`). Im
Backoffice sichtbar im Kontoauszug jedes betroffenen Vertrags.

### `komponenten[]` — optionale Vertragskomponenten (HMZ/Küche/Parkplatz/BK-VZ)

Nutzt die BESTEHENDE `VertragsKomponenteTable`/
`StammdatenRepository.add_komponente` (dieselbe, die auch
`importtemplates/README.md`/die manuelle Zinslisten-Pflege benutzt).

| Feld | Typ | Pflicht | Hinweis |
|---|---|---|---|
| `id` | str | ja | eigener ID-Raum |
| `vertrag_id` | str | ja | |
| `art` | str | ja | z. B. `HMZ`\|`KUECHE`\|`PARKPLATZ`\|`BK_VORAUSZAHLUNG` |
| `bezeichnung` | str | ja | |
| `betrag_cent` | int | ja | |
| `gueltig_von` | Datum | ja | |
| `ust_satz_promille` | int | nein (default `10000`) | |
| `indexierbar` | bool | nein (default `false`) | reines Eignungsflag für `index/service.py` — **löst für sich genommen KEINE Indexklausel/-freigabe aus** |
| `gueltig_bis` | Datum\|null | nein | |

Idempotenz wie bei den Stammdaten-Entitäten (Hash-Vergleich über die
`id`): identischer Inhalt = `UNVERAENDERT`, abweichender Inhalt = harter
Konflikt (gesamter Lauf verweigert) — Komponenten werden nie
stillschweigend überschrieben.

### `eroeffnungskorrekturen[]` — nachweislich im Gesamtsaldo fehlender Posten

Schmaler Sonderfall für GENAU EINE Situation: der bereits bestätigte
Eröffnungs-Gesamtsaldo (`quelle_bestaetigt: true`) enthält nachweislich
einen Posten NICHT, weil dessen tatsächliches Datum vor/auf dem
Eröffnungsstichtag liegt (Beispiel: eine Zahlung wurde beim
Stichtags-Export übersehen). Dies ist AUSDRÜCKLICH kein allgemeines
Altjournal-Tor — eine gewöhnliche `nachbuchungen[]`-Zeile mit Belegdatum
vor/auf dem Stichtag bleibt weiterhin ein harter Konflikt
(`pruefe_kein_altjournal_in_gesamtsaldo`).

| Feld | Typ | Pflicht | Hinweis |
|---|---|---|---|
| `import_id` | str | ja | Quell-ID für Idempotenz |
| `vertrag_id` | str | ja | muss bereits (im Paket oder in der DB) eine bestätigte `GESAMTSALDO`-Eröffnung haben, sonst Konflikt |
| `typ` | str | ja | `SOLL`\|`GUTSCHRIFT`\|`ZAHLUNG`\|`RUECKLASTSCHRIFT` |
| `betrag_cent` | int | ja | |
| `original_belegdatum` | Datum | ja | das ECHTE historische Datum — bleibt bewahrt, wird NICHT auf den Übernahmetag verschoben |
| `grund` | str | ja | Pflichtangabe, kein Platzhalter |
| `quelle_referenz` | str | ja | Pflichtangabe (z. B. Bankbeleg-/Belegnummer), kein Platzhalter |
| `beleg_referenz` | str | nein (default `"Eröffnungskorrektur"`) | |

**`buchungsdatum` steht NICHT in der Datei** — es ist immer der
Übernahmetag (Zeitpunkt des `apply`-Laufs), niemals das historische
Datum (`op_service.eroeffnungskorrektur_buchen`). `quelle_system` der
entstehenden Zeile ist fix `"eroeffnungskorrektur"` — das ist das
geforderte eigene Flag; `grund` landet in `aenderungsgrund`. Original-
Eröffnungssaldo und Korrektur bleiben beide als getrennte, sichtbare
Zeilen im Kontoauszug nachvollziehbar (keine In-Place-Änderung der
Eröffnung). Idempotenz über `import_id` wie bei `nachbuchungen[]`.

## Ergänzung 13.09.2026: zwei weitere, optionale Entitätstypen (Auftrag
## HV-20260913-VERTRAGSANLAGE — Vertragsanlage/-anzeige)

Zusätzlich zu den oben dokumentierten Entitäten erlaubt dieses Format ab
jetzt auch die bereits von Codex ausgelesenen Vertragsfelder, damit
dieselben Daten nicht ein zweites Mal manuell erfasst werden müssen.

### `kautionen[]` — bestätigter, tatsächlich eingegangener Kautionsbetrag

Nutzt die BESTEHENDE `KautionTable`/`StammdatenRepository.set_kaution`
(ID-Konvention `KAU-{vertrag_id}`, 1:1 je Vertrag). AUSDRÜCKLICH der
TATSÄCHLICH eingegangene Betrag — niemals der vertraglich vereinbarte
(siehe `mietvertragsprofile[].vertragliche_kaution_cent` unten). Ein
vereinbarter Betrag ist KEIN Zahlungsbeleg.

| Feld | Typ | Pflicht | Hinweis |
|---|---|---|---|
| `vertrag_id` | str | ja | |
| `betrag_cent` | int | ja | muss positiv sein |
| `stichtag` | Datum | ja | |
| `referenz` | str\|null | nein | |

STRIKTE Idempotenz wie bei den übrigen Stammdaten-Entitäten (nicht wie
`mietvertragsprofile[]` unten): identischer Inhalt = `UNVERAENDERT`,
abweichender Inhalt = harter Konflikt (gesamter Lauf verweigert) — eine
Kaution ist ein bestätigter Zahlungsfakt, keine laufend fortzuschreibende
Angabe.

### `mietvertragsprofile[]` — zusätzliche, VERSIONIERTE Verwaltungs-/Anzeigefelder

Nutzt die NEUE `MietvertragsprofilTable`/
`StammdatenRepository.add_mietvertragsprofil` — bewusst GETRENNT von
`RechtsprofilTable` (Freigabe-pflichtige Rechtsgrundlage der
Indexautomatik) und von `VertragTable.gueltig_von` (bleibt unverändert
die für Sollstellung/OP maßgebliche technische Vertragslaufzeit).

| Feld | Typ | Pflicht | Hinweis |
|---|---|---|---|
| `vertrag_id` | str | ja | |
| `nutzungsart` | str | nein (default `UNGEKLAERT`) | `WOHNUNG`\|`BUERO`\|`GESCHAEFTSLOKAL`\|`SONSTIGE`\|`UNGEKLAERT` — NIEMALS aus `rechtsordnung` abgeleitet (kein "Büro = MRG-frei") |
| `urspruenglicher_mietbeginn` | Datum\|null | nein | tatsächlicher historischer Mietbeginn — bei Altobjekten oft ABWEICHEND von `VertragTable.gueltig_von` (das ist dort häufig die Verwaltungsübernahme) |
| `verwaltungsuebernahme_am` | Datum\|null | nein | |
| `verwaltung_bezeichnung` | str\|null | nein | |
| `vertragliche_kaution_cent` | int\|null | nein | der VEREINBARTE Betrag — KEIN Zahlungsbeleg, strukturell getrennt von `kautionen[].betrag_cent` |
| `vertragliche_kaution_quellenbeleg` | str\|null | nein | |
| `mahngebuehr_cent` | int\|null | nein | `null` = unbekannt/kein Fund, `0` = ausdrücklich belegte "keine Gebühr" — NIE ein erfundener Default bei fehlendem Fund |
| `mahngebuehr_quellenbeleg` | str\|null | nein | |
| `index_reihe` | str\|null | nein | AUSDRÜCKLICH UNVERBINDLICHES Staging-Feld, siehe unten |
| `index_urspruenglicher_basismonat` | str (`JJJJ-MM`)\|null | nein | " |
| `index_urspruenglicher_basiswert` | Decimal\|null | nein | " |
| `index_schwelle_prozent` | Decimal\|null | nein | " |
| `index_schwelle_inklusive` | bool\|null | nein | Tri-State — `null` = im Vertragstext nicht eindeutig festgestellt, NIE geraten |
| `index_anpassungsmonat` | int\|null | nein | " |
| `index_mindestintervall_monate` | int\|null | nein | " |
| `index_klauseltext_auszug` | str\|null | nein | " |
| `index_klauseltext_seite` | int\|null | nein | " |
| `quelle_typ` | str | nein (default `IMPORT_SCHEMA`) | `PDF_EXTRAKTION`\|`MANUELL`\|`IMPORT_SCHEMA` |
| `quelle_referenz` | str\|null | nein | |

**Index-Quellfelder sind reine Gedächtnisstütze/Vorbefüll-Vorschläge**
für das bestehende, eigenständige Formular
`/vertrag/{id}/indexklauseln` (`index/service.py::klausel_anlegen` +
`klausel_freigeben`) — ihre Übernahme erzeugt NIEMALS automatisch eine
`IndexKlauselTable`-Zeile, keine Freigabe, keine Sollstellung. Eine
tatsächlich wirksame Klausel und ein eventuelles Rekonstruktionsmodell
bleiben im Backoffice immer getrennt lesbar, nie vermischt mit diesen
Staging-Feldern.

**Append-only, KEIN In-Place-Update:** eine inhaltliche Änderung legt
eine NEUE Version an (`version` fortlaufend je `vertrag_id`), nie eine
Überschreibung. Abweichend von der sonst strikten Stammdaten-
Konfliktregel ist ein inhaltlich abweichender Import HIER KEIN Konflikt
(Status `AKTUALISIERUNG` statt `KONFLIKT`) — laufende Datenpflege dieser
rein beschreibenden Felder ist gewollt und blockiert den Lauf nicht.
Identischer Inhalt gegenüber der zuletzt gespeicherten Version bleibt
`UNVERAENDERT` (kein wirkungsloser Zusatz-Insert). Bestehende Konten/
OP/Sperren/Vertragskomponenten bleiben davon vollständig unberührt.

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
  ],
  "kautionen": [
    {"vertrag_id": "V-601-TOP1", "betrag_cent": 152000, "stichtag": "2020-01-15", "referenz": "Überweisung Kaution"}
  ],
  "mietvertragsprofile": [
    {"vertrag_id": "V-601-TOP1", "nutzungsart": "WOHNUNG",
     "urspruenglicher_mietbeginn": "2015-06-01", "verwaltungsuebernahme_am": "2020-01-01",
     "vertragliche_kaution_cent": 152000, "mahngebuehr_cent": null,
     "index_reihe": "VPI 2020", "index_urspruenglicher_basismonat": "2015-06",
     "index_urspruenglicher_basiswert": "106.7", "index_schwelle_prozent": "5.0",
     "index_schwelle_inklusive": true, "quelle_typ": "PDF_EXTRAKTION",
     "quelle_referenz": "vertrag_601_top1.pdf#3"}
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
  Gesamtsaldo-Stichtag desselben Kontos, eine `eroeffnungskorrekturen[]`-
  Zeile ohne bereits bestätigte `GESAMTSALDO`-Eröffnung desselben
  Vertrags, eine `sperren[]`-Zeile mit ungültigem `grund` (kein
  `Sperrgrund`-Enumwert), eine `komponenten[]`-Zeile zu einem
  ausgeschlossenen Objekt, ein nicht-positiver `betrag_cent` bei
  `nachbuchungen[]`/`eroeffnungskorrekturen[]`/`EINZEL_OP`-Eröffnungen
  (siehe "Explizite Betragssemantik" oben — nur `GESAMTSALDO` darf
  negativ/ein Guthaben sein).
- **Sichtbare Hinweise (blockieren NICHT, weil das bestehende System sie
  bereits sicher behandelt):** fehlende `faelligkeit` (Position bleibt
  sichtbar, wird nie automatisch gemahnt), fehlende Debitor-`email`
  (Vertrag bleibt anlegbar, wird nie automatisch gemahnt), Vertrag mit
  `rechtsordnung: "UNGEKLAERT"` (anlegbar, aber technisch von Mahnung/
  Index/Sollstellung gesperrt — siehe Ergänzung oben). Der Plan zählt
  alle drei aus und weist sie aus — keine stille Weglassung.

## Status

Dieser Vertrag ist die Grundlage für die Implementierung in derselben
Sitzung (`src/mietinkasso/intake/`, `scripts/intake_import.py`). Codex
kann auf Basis dieses Dokuments bereits mit dem realen Mapping beginnen;
Abweichungen, die sich während der Implementierung als nötig erweisen,
werden hier nachgetragen, nicht stillschweigend geändert.
