# Import-/Quellenformat Rechtsprofile & Index-Quellenfakten

Auftrag HV-20260919-INDEX-MONATSBERICHT, Umfang C. Dieses Dokument
beschreibt das JSON-Format für Codex' privates Datenmapping (echte
Vertrags-/Indexklauseldaten, außerhalb dieses Repos) auf die zwei neuen
Import-Ziele:

- `RechtsprofilTable`-**ENTWURF**-Zeilen (über das bereits bestehende
  `RechtsprofilService.entwurf_anlegen` — keine Freigabe, keine
  Fachentscheidung durch den Import selbst).
- `IndexQuellenFaktenTable`-Zeilen (rein informativ/versioniert, siehe
  Docstring dort — NIE direkte Eingabe für eine echte Berechnung, siehe
  Abschnitt "Rechenvorschlag" unten).

Dry-run (`plan`) ist Standard; `apply` verlangt den exakten Plan-Hash aus
einem vorherigen `plan`-Lauf. Beide Zeilenarten sind inhaltsversioniert:
ein Re-Import mit unverändertem Inhalt erzeugt KEINE neue Version.
Nichts an Soll/Bank/OP, keine Buchung, kein Versand.

```
python scripts/rechtsprofil_quellenimport.py plan --database-url sqlite:////pfad/produktiv.db --datei paket.json
python scripts/rechtsprofil_quellenimport.py apply --database-url sqlite:////pfad/produktiv.db --datei paket.json \
    --bestaetige-hash <hash aus dem plan-Lauf> --akteur "Codex"
```

`--database-url` verlangt einen Pfad außerhalb des Repos (kein
`:memory:`, kein Pfad im Arbeitsbaum) — identische Prüfung wie beim
bestehenden `scripts/intake_import.py`.

## Gesamtstruktur

```json
{
  "quelle": "Codex-Datenmapping 2026-09-19, Quelle: Mietverträge Ordner X",
  "rechtsprofile": [ { "...": "siehe unten" } ],
  "quellen_fakten": [ { "...": "siehe unten" } ]
}
```

- `quelle` ist Pflicht und darf nicht leer sein (Provenienzpflicht — jede
  angelegte Zeile trägt diesen Text unverändert in `import_quelle`/
  `quelle`).
- `rechtsprofile`/`quellen_fakten` sind je optional, aber mindestens
  eine der beiden Listen muss mindestens einen Eintrag haben.
- Beide Listen können unabhängig voneinander befüllt werden — ein
  Vertrag kann NUR eine `quellen_fakten`-Zeile bekommen (z. B. weil noch
  kein vollständiges Rechtsprofil möglich ist, aber bereits belegte
  Basisdaten für die Berichtsanzeige vorliegen), NUR ein Rechtsprofil,
  oder beides.

## `rechtsprofile[]`

Ein Eintrag erzeugt EIN neues `RechtsprofilTable`-**ENTWURF** (Status
`ENTWURF`, niemals `FREIGEGEBEN`) über `RechtsprofilService.
entwurf_anlegen` — die tatsächliche fachliche Freigabe bleibt ein
separater, manueller Schritt im Backoffice (`/backoffice/vertrag/
{vertrag_id}` → „Rechtsprofil freigeben“), nie automatisch durch diesen
Import. Felder entsprechen 1:1 den Parametern von `entwurf_anlegen`
(`indexautomatik/rechtsprofil.py`):

| Feld | Pflicht | Bedeutung |
|---|---|---|
| `vertrag_id` | ja | Zielvertrag (muss existieren). |
| `rechtsordnung` | ja | `OESTERREICH_MRG_VOLL`/`_MRG_TEIL`/`_MRG_FREI`/`_WGG`/`_GEWERBE`/`DEUTSCHLAND`/`UNGEKLAERT`. |
| `ist_wohnungsnutzung` | ja | Tri-State (`true`/`false`/`null`). Nur `false` erlaubt die begrenzte Gewerbe-Rechenvorschau (siehe unten); `null`/`true` sperren sie identisch. |
| `mrg_zinsbeschraenkung` | ja | Bool. |
| `mrg_zinsbeschraenkung_geprueft` | nein (Default `false`) | Erst nach menschlicher Prüfung `true` setzen. |
| `ist_altvertrag` | ja | Bool. |
| `ist_hauptmiete` | ja | Tri-State — `null` sperrt wie bei `ist_wohnungsnutzung`. |
| `foerderbindung` | ja | Bool. |
| `foerderbindung_geprueft` | nein (Default `false`) | Wie `mrg_zinsbeschraenkung_geprueft`. |
| `mietzinsobergrenze_cent`/`_quellenbeleg`/`_gueltig_bis` | nein | Nur wirksam mit Beleg. |
| `bezugsjahr`/`bezugsmonat` | ja | Letzte tatsächlich dokumentierte Basis (siehe Abgrenzung unten). |
| `letzte_basis_war_jahresdurchschnitt` | ja | Bool. |
| `basis_komponenten_ids` | ja | Liste — MUSS auf real existierende, zum Vertrag gehörende Komponenten verweisen. Ein Vertrag mit bestätigtem Gesamtbetrag OHNE Komponenten (siehe `quellen_fakten` unten) kann daher KEIN Rechtsprofil bekommen, solange keine Komponenten importiert sind. |
| `vertrag_beleg_referenz`, `klausel_referenz` | ja | Nachweis-Fundstellen. |
| `vertraglich_zulaessiger_betrag_cent`, `vertraglicher_quellenbeleg`, `vertraglicher_fruehestmoeglicher_termin`, `vertragsklausel_id`, `frist_tage_zugang_bis_wirksamkeit`, `frist_quellenbeleg` | nein | Siehe `entwurf_anlegen`-Docstring; `vertragsklausel_id` und `vertraglich_zulaessiger_betrag_cent` schließen sich gegenseitig aus. |
| `historische_basis_belege` | nein (Default `{}`) | Siehe `indexbetrieb.md`. |
| `vpi_reihe` | nein (Default `VPI20C18`) | |

## `quellen_fakten[]`

Ein Eintrag erzeugt EINE neue, versionierte `IndexQuellenFaktenTable`-
Zeile. Diese Tabelle ist **absichtlich lockerer** als `RechtsprofilTable`
— sie verlangt KEINE existierenden Komponenten und KEINE vollständige
Rechtsprofil-Vollständigkeit, weil ihr einziger Zweck die
**Anzeige-Anreicherung des Monatsberichts** ist (Codex: "bereits 34
Drafts vorhanden ... die neuen Quellenfakten müssen im Bericht
tatsächlich nutzbar/sichtbar sein, nicht für alle 17 nur 'kein
freigegebenes Profil'"). Eine `quellen_fakten`-Zeile ist NIE eine
Ausführungsfreigabe — sie wird im Bericht konsequent als
"Quellenfakten — noch keine Ausführungsfreigabe" gekennzeichnet.

```json
{
  "quellen_fakten": [
    {
      "vertrag_id": "V-601-7",
      "ist_wohnungsnutzung": false,
      "urspruengliche_klauselbasis": { "reihe": "VPI20C18", "monat": "2024-01", "wert": 130.0 },
      "urspruenglicher_indexbetrag_cent": 50000,
      "aktueller_indexbetrag_cent": 50000,
      "betrag_basisbindung_belegt": true,
      "schwelle_prozent": 3,
      "schwelle_inklusive": false,
      "daempfung_prozent": null,
      "vertragliche_grenze_prozent": null,
      "klauselregel_text": "Wertsicherung nach VPI 2020, Schwelle 3%, jährliche Prüfung im Juni.",
      "bestaetigte_gesamtmiete_cent": 200000,
      "bestaetigte_gesamtmiete_quelle": "Kontoauszug September 2026",
      "bestaetigte_gesamtmiete_stichtag": "2026-09-01",
      "letzte_tatsaechliche_basis_jahr": 2024,
      "letzte_tatsaechliche_basis_monat": 1,
      "letzte_tatsaechliche_basis_hinweis": "Letzte tatsächlich umgesetzte Anpassung laut Altsystem.",
      "bereits_enthaltene_erhoehungen_hinweis": null,
      "pruefhinweis": "Vertragsklausel noch nicht im Rechtsprofil freigegeben.",
      "bedingter_naechster_monat": 6,
      "bedingter_fruehester_termin_hinweis": "Jährliche Prüfung jeweils im Juni laut Klausel Punkt 7.",
      "quellenreferenzen": ["Mietvertrag V-601-7, Punkt 7", "Kontoauszug 09/2026"]
    }
  ]
}
```

| Feld | Pflicht | Typ/Validierung | Bedeutung |
|---|---|---|---|
| `vertrag_id` | ja | nicht-leerer String | Zielvertrag (muss existieren). |
| `ist_wohnungsnutzung` | nein (Default `null`) | JSON-Bool oder `null` — **niemals** ein String wie `"false"` (wird hart abgelehnt, kein `bool("false")`-Fehlschluss) | Tri-State, fail-closed: NUR `false` erlaubt die Gewerbe-Rechenvorschau unten; `null` (ungeklärt) sperrt genau wie `true` (tatsächliche Wohnung). |
| `urspruengliche_klauselbasis` | nein | Objekt `{reihe, monat, wert}` oder `null` | `wert` MUSS eine endliche, positive Zahl sein (`> 0`) — NaN/Infinity/0/negativ werden hart abgelehnt (Division-durch-0/sinnlose Referenzbasis in jeder späteren Veränderungsberechnung). |
| `urspruenglicher_indexbetrag_cent` | nein | `int` (Cent) oder `null`, `>= 0` | Der ursprünglich indexierte Teilbetrag zur Basis oben — NIE der heute bereits erhöhte Betrag. |
| `aktueller_indexbetrag_cent` | nein | `int` (Cent) oder `null`, `>= 0` | Der HEUTE tatsächlich verrechnete Indexanteil — bei einem Vertrag OHNE jede zwischenzeitliche (Teil-)Erhöhung identisch zu `urspruenglicher_indexbetrag_cent`, sonst höher. PFLICHT für jeden Gesamtvorschlag der Gewerbe-Rechenvorschau (siehe unten) — fehlt er, bleibt bei überschrittener Schwelle sowohl das Delta als auch die neue Gesamtsumme fail-closed `null` (die reine Prozentinformation bleibt trotzdem sichtbar). |
| `betrag_basisbindung_belegt` | nein (Default `false`) | JSON-Bool, strikt | Muss `true` sein, damit die Rechenvorschau überhaupt startet. |
| `schwelle_prozent` | nein | endliche Zahl oder `null` | |
| `schwelle_inklusive` | nein | JSON-Bool oder `null`, strikt | |
| `daempfung_prozent`, `vertragliche_grenze_prozent` | nein | endliche Zahl oder `null` | Direkt an `IndexService.effektive_veraenderung_prozent` weitergereicht. |
| `klauselregel_text` | nein | Text | |
| `bestaetigte_gesamtmiete_cent` | nein | `int` (Cent) oder `null`, `>= 0` | Bestätigter AKTUELLER Gesamtbetrag — ermöglicht erst eine neue Gesamtvorschreibung statt nur des isolierten Indexanteils (siehe unten). |
| `bestaetigte_gesamtmiete_quelle`, `bestaetigte_gesamtmiete_stichtag` | nein | Text/ISO-Datum | |
| `letzte_tatsaechliche_basis_jahr`/`_monat`/`_hinweis` | nein | | Nur Anzeige — die echte "bereits umgesetzt"-Wahrheit kommt weiterhin aus `IndexSollUmsetzungTable`, nie aus diesen Feldern. |
| `bereits_enthaltene_erhoehungen_hinweis` | nein | Text | Verhindert Doppelzählung bereits eingepreister Erhöhungen im Anzeigetext. |
| `pruefhinweis` | nein | Text | Fällt zurück, wenn KEINE Rechenvorschau möglich ist. |
| `bedingter_naechster_monat` | nein | `1..12` oder `null` | Wiederkehrender Kalendermonat (z. B. eine jährliche Juni-Prüfklausel) — wird bei jedem Berichtslauf dynamisch auf den nächsten zukünftigen Termin aufgelöst, NIE als statisches Datum gespeichert. |
| `bedingter_fruehester_termin_hinweis` | nein | Text | |
| `quellenreferenzen` | nein (Default `[]`) | Liste von Strings | |

### Explizite Validierung (Codex-Korrektur, alle beim `plan`-Lauf geprüft)

- **Bool-Felder** (`ist_wohnungsnutzung`, `betrag_basisbindung_belegt`,
  `schwelle_inklusive`): NUR ein tatsächlicher JSON-Bool oder `null` wird
  akzeptiert. Ein String, eine Zahl (`0`/`1`) oder irgendein anderer Typ
  wird hart abgelehnt — es gibt keine automatische Wahrheitswert-
  Umwandlung, die z. B. den String `"false"` fälschlich als `True`
  behandeln würde.
- **Cent-Felder** (`urspruenglicher_indexbetrag_cent`,
  `aktueller_indexbetrag_cent`, `bestaetigte_gesamtmiete_cent`): NUR ein
  `int` oder `null` wird akzeptiert (kein `float`, kein `bool` — `bool`
  ist in Python technisch eine `int`-Unterklasse und wird deshalb
  EXPLIZIT ausgeschlossen), und zusätzlich `>= 0`.
- **Prozent-/Basis-Felder** (`schwelle_prozent`, `daempfung_prozent`,
  `vertragliche_grenze_prozent`, `urspruengliche_klauselbasis.wert`):
  müssen zu einer ENDLICHEN `Decimal` parsbar sein — NaN/Infinity werden
  abgelehnt; `urspruengliche_klauselbasis.wert` muss zusätzlich `> 0`
  sein.
- Ein Fehler bei irgendeinem Feld einer Zeile lehnt den GESAMTEN `plan`-
  Lauf ab (kein Teilimport eines fehlerhaften Pakets).

### Atomarität

Die `rechtsprofile`-Zeilen werden je Zeile über das bestehende
`RechtsprofilService.entwurf_anlegen` angelegt (dieser Service committet
selbst je Aufruf — kein Vollrollback über den gesamten `rechtsprofile`-
Batch, dokumentierte Grenze, siehe `OFFENE_PUNKTE.md`). Die
`quellen_fakten`-Zeilen werden dagegen ALLE zusammen in EINER einzigen
Transaktion geschrieben (`IndexQuellenFaktenRepository.anlegen_batch`) —
ein Fehler beim Bauen irgendeiner Zeile lässt KEINE einzige davon in der
Datenbank landen.

## Begrenzte Gewerbe-Rechenvorschau

Für einen Geschäftsraumvertrag OHNE freigegebenes Rechtsprofil, aber MIT
vollständig belegten `quellen_fakten` (`betrag_basisbindung_belegt`,
`urspruengliche_klauselbasis.reihe`/`.wert`, `urspruenglicher_
indexbetrag_cent`, `schwelle_prozent`, `schwelle_inklusive`, alle
gesetzt, UND `ist_wohnungsnutzung: false` explizit verifiziert), zeigt
der Monatsbericht eine rein lesende Vorschau — AUSSCHLIESSLICH über die
bereits bestehenden, reinen `IndexService`-Bausteine
(`effektive_veraenderung_prozent`/`ueberschreitet_schwelle`), KEINE
zweite/parallele Formel. Die Vorschau:

- vergleicht den zuletzt ENDGUELTIGen amtlichen VPI-Wert (nie
  `VORLAEUFIG`) gegen die dokumentierte Basis,
- zeigt Rohveränderung UND wirksame (gedämpfte/geschwellte) Veränderung
  GETRENNT (eine unterschrittene Schwelle erscheint nie als
  irreführendes "0%", sondern als "Rohveränderung X%, wirksame
  Veränderung 0%, Schwelle NICHT überschritten"),
- rechnet das Delta IMMER gegen `aktueller_indexbetrag_cent` (den HEUTE
  tatsächlich verrechneten Indexanteil), NIE gegen
  `urspruenglicher_indexbetrag_cent` — sonst würden zwischenzeitlich
  bereits umgesetzte (Teil-)Erhöhungen ein zweites Mal mitgezählt. Ist
  die Schwelle NICHT überschritten, bleibt das Delta IMMER `0`
  ("aktuellenTeil unverändert lassen" — keine fiktive Rücknahme einer
  bereits enthaltenen früheren Erhöhung). Ist die Schwelle überschritten
  UND fehlt `aktueller_indexbetrag_cent`, bleiben Delta und
  Gesamtvorschlag fail-closed `null` (siehe Feldtabelle oben),
- liefert eine neue GESAMTvorschreibung NUR, wenn zusätzlich eine
  bestätigte aktuelle Gesamtmiete (`bestaetigte_gesamtmiete_cent`)
  vorliegt — sonst bleibt nur der isolierte neue Indexanteil bekannt,
  NIE eine unterstellte Gesamtsumme,
- ist im Text unmissverständlich als "Rechenvorschlag (unverbindliche
  Vorschau aus Quellenfakten) — Ausführung noch nicht freigegeben"
  gekennzeichnet, mit VPI-Werten und Prozentangaben in normaler
  (nicht-wissenschaftlicher), gerundeter Darstellung (VPI 1–4, Prozent
  fix 4 Nachkommastellen) — die zugrunde liegende Rechnung selbst bleibt
  ungerundet,
- schreibt NIE eine `IndexAnpassungTable`/ein `ErhoehungsschreibenTable`/
  eine Soll-Umsetzung — reine Anzeige.

Fehlt IRGENDEINE der übrigen Voraussetzungen (Basisbindung, Klauselbasis,
`urspruenglicher_indexbetrag_cent`, Schwellenparameter, auflösbarer VPI),
bleibt der bisherige `pruefhinweis`-Text die einzige Anreicherung, ohne
eine Zahl vorzutäuschen.
