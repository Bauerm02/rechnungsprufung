# AGENTS.md — Arbeitsregeln für dieses Repository

Dieses Repository enthält zwei unabhängige, absichtlich getrennte Codebasen:

- `src/invoice_automation/` — Kreditoren-/Eingangsrechnungs-Automatisierung
  (Migration von Zapier). Siehe `README.md`.
- `src/mietinkasso/` — Hausverwaltung & Mietinkasso (Auftrag
  `HV-20260911-MVP1`). Siehe `docs/hausverwaltung/RAHMENPROGRAMM.md`.

Die beiden Module importieren sich gegenseitig nicht. Wer an einem
Modul arbeitet, lässt das andere unangetastet, außer eine Änderung ist
ausdrücklich repository-weit (z. B. `pyproject.toml`, CI).

## Auftrag HV-20260911-MVP1 (Hausverwaltung & Mietinkasso)

### Scope

Geschütztes Online-Modul mit dauerhaftem, deterministischem Regelbetrieb
OHNE laufende KI-Aufrufe: Eröffnung/Salden, Debitoren/OP, Mietvorschreibung,
Bankabgleich, Index- und BK-Nachverrechnung, genau zwei automatische
Mahnstufen. Pilotobjekte: 601 Am Corso, 616 Fockygasse, 617 Primelweg,
619 Gutenberg. Objekt 107 (Sieben Dörfer) ist ausdrücklich ausgeschlossen.
Die vollständigen Fachregeln stehen in
`docs/hausverwaltung/RAHMENPROGRAMM.md` — dieses Dokument ist die
verbindliche Quelle, nicht diese Kurzfassung.

### Owner-Aufteilung

- **Markus Bauer** — Auftraggeber. Erteilt den Auftrag, entscheidet über
  Fachregeln, Freigaben und Produktivbetrieb. Trifft alle Entscheidungen,
  die über die hier beschriebenen Leitplanken hinausgehen.
- **Codex** — verantwortet Rahmen, Quellenmapping (reale Verwaltungsdaten
  außerhalb dieses Repos) und die unabhängige Code-Abnahme. Prüft die von
  Claude gelieferten Dateien gegenlesend in einem separaten lokalen Klon.
  Codex-Hinweise, die während einer laufenden Sitzung eintreffen, sind
  bindende Zwischen-Reviewpunkte, keine bloßen Vorschläge.
- **Claude** — Implementierer. Baut Code, Migration, Tests und Doku in
  `src/mietinkasso/` auf dem freigegebenen Aufgabenbranch, committet und
  pusht dorthin. Nimmt keine eigenständigen Fachentscheidungen zu
  Rechtsfragen (Index, BK, MRG-Einordnung) vor, sondern baut sie als
  versionierte, überprüfbare Regelprofile, die erst nach fachlicher
  Freigabe wirksam werden.

### Synthetic-only

- Es werden ausschließlich synthetische Testdaten verwendet
  (`scripts/seed_synthetic_data.py`, `src/mietinkasso/importtemplates/`).
- Keine echten Mieterdaten, keine echten Kontonummern/IBANs, keine
  Originaldokumente aus den lokalen Dropbox-/Windows-Pfaden werden in
  dieses Repository kopiert oder eingelesen. Falls solche Pfade in einer
  Sitzung erwähnt werden: Zugriff prüfen, niemals Inhalte übernehmen.
- Keine echten Mietermails, keine Produktivbuchungen, keine Bankaktionen,
  kein Deployment.

### Send-off / Übergabeprotokoll

Vor jeder Übergabe an Codex zur Abnahme:

1. `pytest` lokal grün (siehe `Makefile` bzw. `python -m pytest`).
2. Alle geänderten/neuen Dateien in der Antwort benannt.
3. Commit mit beschreibender Nachricht auf dem freigegebenen Aufgabenbranch
   erstellt und **nur dieser Branch** gepusht (kein Force-Push, kein Push
   auf `main`).
4. Offene Punkte (fehlende Integrationen, ungeprüfte Rechtsprofile,
   Platzhalter-Parser) explizit in
   `docs/hausverwaltung/OFFENE_PUNKTE.md` benannt, nicht stillschweigend
   weggelassen.
5. `SEND_ENABLED=false` (bzw. das Äquivalent für den jeweiligen
   Versandkanal) bleibt Standard, bis eine Person das Gegenteil
   ausdrücklich freigibt.

### Fachliche Leitplanken (Kurzfassung — Details in RAHMENPROGRAMM.md)

- Sollsalden und Habensalden werden je Debitor/Konto getrennt geführt.
  Die Nettosumme eines ganzen Objekts ist niemals gleichzusetzen mit der
  Summe mahnbarer Forderungen einzelner Mieter.
- Keine Namens- oder Nullsaldo-Heuristiken (z. B. "Soll ist 0, also
  Leerstand" oder "gleicher Name, also derselbe Mieter/dieselbe Zahlung").
  Zuordnungen laufen über explizite, eindeutige Kennungen.
- Technische Verrechnungskonten des Quellsystems (z. B. ein
  Leerstands-Sammelkonto) sind keine Debitoren/Mieter und dürfen nicht
  wie ein Mietkonto behandelt oder mit einem OP-Saldo bemahnt werden.
- Kaution ist strukturell getrennt vom OP-Saldo und wird nie automatisch
  gegen Mietrückstand verrechnet.
