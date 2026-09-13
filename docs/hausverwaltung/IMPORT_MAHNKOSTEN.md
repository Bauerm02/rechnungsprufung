# Import-/Profilformat Mahnkosten (Zinsprofil, OeNB-Basiszinssatz)

Auftrag Markus 13.09.2026 ("pro Mahnlauf Mahngebühren, und die Zinsen
dazu, soviel wie gesetzlich erlaubt ist"). Dieses Dokument beschreibt das
Format für Markus' privates Mapping (echte Vertragsdaten, außerhalb
dieses Repos) auf die beiden neuen Tabellen `ZinsprofilTable` und
`OenbBasiszinssatzTable` (`src/mietinkasso/infrastructure/db/tables.py`).

**Wichtige Abgrenzung zum Stammdaten-Intake:** Anders als
`kautionen[]`/`mietvertragsprofile[]` (siehe `IMPORT_VERTRAG.md`) ist
dieses Format **nicht** Teil des atomaren, dry-run-geprüften
`intake/`-Pakets. Es wird direkt über
`mahnwesen/kosten_repository.py::MahnkostenRepository` angewendet (per
Skript oder über die Backoffice-Formulare unter
`/backoffice/vertrag/{vertrag_id}/zinsprofil` und
`/backoffice/basiszinssatz`). Das ist ein bewusst offener Punkt, siehe
`OFFENE_PUNKTE.md`.

## `zinsprofile[]` — je Vertrag, versioniert

Jeder Eintrag erzeugt EIN neues `ZinsprofilTable`-ENTWURF (nie eine
Überschreibung); die Freigabe (`GEPRUEFT`) erfolgt danach ausdrücklich
über `zinsprofil_freigeben(...)` bzw. den "Zinsprofil bestätigen"-Button
im Backoffice — nie automatisch durch den Import selbst.

```json
{
  "zinsprofile": [
    {
      "vertrag_id": "V-601-3",
      "ist_b2b": false,
      "vertragsdatum": "2024-01-01",
      "vereinbarter_zinssatz_prozent": null,
      "vereinbarung_geprueft": false,
      "vereinbarung_beleg": null,
      "mahngebuehr_kostenbasis_cent": 2000,
      "mahngebuehr_kostenbasis_beleg": "Portokosten-/Personalaufwandsnachweis 2026, Beilage 3"
    }
  ]
}
```

Feldbedeutung (siehe `mahnwesen/kosten.py` für die fachliche
Herleitung):

| Feld | Pflicht | Bedeutung |
|---|---|---|
| `vertrag_id` | ja | Zielvertrag (muss existieren). |
| `ist_b2b` | ja | Beiderseits unternehmensbezogenes Geschäft — Voraussetzung für §456 UGB. |
| `vertragsdatum` | nein | Nötig für den §456-UGB-Stichtag 16.03.2013. Fehlt es, bleibt B2B blockiert statt geraten. |
| `vereinbarter_zinssatz_prozent` | nein | Nur wirksam, wenn `vereinbarung_geprueft=true` — eine gelesene Klausel ist noch KEINE Wirksamkeitsfreigabe (KSchG §6 Abs 1 Z 13/OGH 7Ob111/25m). Ohne menschliche Prüfung ignoriert das System diesen Wert und verwendet die gesetzliche/UGB-Basis. |
| `vereinbarung_geprueft` | nein (Default `false`) | Muss NACH menschlicher Prüfung der Klausel explizit auf `true` gesetzt werden — niemals pauschal aus dem Import heraus. |
| `vereinbarung_beleg` | nein | Fundstelle/Nachweis der Vereinbarung. |
| `mahngebuehr_kostenbasis_cent` | nein | Notwendige, zweckmäßige tatsächliche Betreibungskosten (§1333 Abs 2 ABGB, §458 UGB) — NIE eine Pauschale ohne Beleg. `null`/fehlend bedeutet "ungeklärt", NICHT "keine Gebühr" — es wird dann schlicht keine Gebühr angesetzt, bis eine Kostenbasis nachgetragen ist. |
| `mahngebuehr_kostenbasis_beleg` | nein | Nachweis der Kostenbasis. |

## `basiszinssaetze[]` — global, je Halbjahr

```json
{
  "basiszinssaetze": [
    {
      "id": "2026-1",
      "gueltig_von": "2026-01-01",
      "gueltig_bis": "2026-06-30",
      "basiszinssatz_prozent": "1.53",
      "quelle_referenz": "OeNB-Kundmachung 01.01.2026"
    },
    {
      "id": "2026-2",
      "gueltig_von": "2026-07-01",
      "gueltig_bis": "2026-12-31",
      "basiszinssatz_prozent": "1.53",
      "quelle_referenz": "OeNB-Kundmachung 01.07.2026"
    }
  ]
}
```

Ein Halbjahreswert ist **unveränderlich** — `basiszinssatz_erfassen(...)`
lehnt eine bereits vorhandene `id` mit einem Fehler ab
(`ValueError: ... bereits erfasst - Halbjahreswerte sind
unveränderlich, ggf. neuen Zeitraum verwenden.`). Für einen neuen Wert
im selben Zeitraum ist eine neue `id` (z. B. mit Datumsstempel) statt
eines Überschreibens nötig. Ein zukünftiges Halbjahr OHNE erfassten
Wert führt NICHT zur stillschweigenden Fortschreibung des letzten
bekannten Werts — die B2B-Berechnung bleibt für diesen Zeitraum
ausdrücklich "Basis ungeklärt" blockiert, bis der Wert nachgetragen
wird (siehe `kosten.py::bestimme_zinssatz`).

## Anwendungsreihenfolge

1. `basiszinssaetze[]` zuerst erfassen (unabhängig von Verträgen).
2. `zinsprofile[]` je Vertrag als ENTWURF anlegen.
3. Jedes ENTWURF-Zinsprofil MENSCHLICH prüfen und erst dann über
   `/backoffice/zinsprofil/{id}/freigeben` freigeben — dieser Schritt
   ist bewusst nicht Teil des Imports.

Ohne freigegebenes Zinsprofil gilt automatisch die gesetzliche Basis
(§1000 ABGB, 4 % p.a.) — die Hauptforderung selbst wird davon nie
blockiert, nur der Zins-/Gebührenanteil bleibt bis zur Freigabe auf der
gesetzlichen Basis bzw. "Klärung erforderlich" (Gebühr).
