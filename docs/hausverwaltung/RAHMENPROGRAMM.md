# RAHMENPROGRAMM — HV-20260911-MVP1 (Hausverwaltung & Mietinkasso)

Dieses Dokument ist der wortgetreu gespeicherte Auftrag, wie er der
Implementierungssitzung erteilt wurde. Er ist die verbindliche Quelle für
alle Fachregeln in `src/mietinkasso/`. Nachträgliche Klarstellungen aus
derselben Sitzung sind am Ende unter "Ergänzende Klarstellungen"
angehängt, mit Datum/Kontext, statt den Originaltext zu verändern.

## Originalauftrag

> HV-20260911-MVP1 — Hausverwaltung & Mietinkasso. Markus beauftragt dich
> als Implementierer, Codex verantwortet Rahmen und unabhängige
> Code-Abnahme. Markus hat den Zugriff auf das private Repository
> Bauerm02/rechnungsprufung und die Umsetzung in einem eigenen
> Entwicklungsbranch ausdrücklich erlaubt. Nutze Sonnet 5 / High, Fast
> aus. Arbeite bis zu einem lauffähigen, getesteten ersten
> Buchhaltungsmodul. Keine echten Mietermails, Produktivbuchungen,
> Bankaktionen, Deployment oder Änderungen am Default-Branch. Du darfst
> in deinem Aufgabenbranch Code und Dokumentation schreiben, testen und
> diesen Branch zur Review bereitstellen. Keine zusätzlichen
> kostenpflichtigen Services.
>
> Zuerst README, AGENTS.md/CLAUDE.md, Projektarchitektur und Tests des
> Repositories lesen; vorhandene Arbeit erhalten. Berichte früh kurz den
> tatsächlichen Stack, Branch und Datenzugriff. Prüfe die bestehende
> Implementierung auf bereits vorhandene Debitoren, OP, Rechnung, Bank,
> Auth/Rollen, Jobs und Outbox. Nutze diese statt Parallelbaus. Wenn
> dieses Repo ungeeignet ist, begründe es quellenbezogen und liefere
> einen klar getrennten portablen Modulbaustein samt Tests und
> Integrationsvorschlag im eigenen Branch. Nicht nur einen Plan oder
> UI-Mockup liefern.
>
> ZIEL: Geschütztes Online-Modul mit dauerhaftem, deterministischem
> Regelbetrieb OHNE KI-Aufrufe. Nach Einrichtung: Salden übernehmen,
> Debitoren/fehlende Buchungen erfassen, Mietvorschreibung, Bankabgleich,
> offene Posten, Indexanpassung und BK-Nachverrechnung sowie GENAU zwei
> automatische Mahnstufen. KI nur bei Entwicklung/unterstützter
> Erstextraktion. Keine laufende LLM-Abhängigkeit. Bestehende
> Hauptbuchhaltung bleibt führend; Miet-Nebenbuch liefert idempotente
> Exportvorschläge/Status. Keine zweite DATEV-Schreibstrecke.
>
> BESTEHENDE INFRASTRUKTUR: Codex hat Company OS auf jlb-hetzner unter
> /home/mabau/workspaces/company-os lesend geprüft: Python/FastAPI,
> PostgreSQL, Rollen/Policies/Audit, separate Repos 7d-invoice und
> jlb-cockpit. Die Beziehung dieses GitHub-Repos dazu ist NICHT belegt;
> nicht erfinden. Lokale führende Fachmappe
> D:\AI-Knowledge\10_PROJEKTE\10_Finanzen\14_Hausverwaltung_Mietinkasso;
> tägliche KI-Basis E:\7 Dörfer Immobilien Dropbox\_AI-Kontext. Originale
> E:\7 Dörfer Immobilien Dropbox\Immobilien\Hausverwaltung\
> Verwaltungsuebernahme_2026-09-11. Diese Pfade sind in Cloud-Code
> vermutlich nicht gemountet: prüfe, behaupte keinen Zugriff, kopiere
> keine Secrets oder Rohdaten. Entwickle mit synthetischen Daten.
> Vorhandene Windows-Dokumente: Deb-/Kred-/Sachkontensalden, Journal,
> Stammblätter, Kaution 31.08.2026, zwei Zinslisten unterschiedlicher
> Abdeckung. Noch KEIN vollständiger aktueller Bank-/OP-Abgleich. Codex
> erstellt Quellenmapping separat.
>
> FACHREGELN (für dich und Codex identisch verbindlich):
>
> 1. Pilotobjekte 601 Am Corso, 616 Fockygasse, 617 Primelweg, 619
>    Gutenberg; 107 Sieben Dörfer ausgeschlossen. JLB führt Verwaltung;
>    Vermieter/Forderungsinhaber ist je Vertrag die richtige
>    Gesellschaft. IDs für Gesellschaft, Objekt, Einheit, Vertrag,
>    Debitor, Konto getrennt; 617 ist kein eindeutiger Bankbezeichner.
>    Konten, Freigaben und Kaution intern. Kaution niemals automatisch
>    mit Mietrückstand verrechnen. Nutzungsstatus Dauervermietung,
>    Kurzzeitvermietung, Eigennutzung, Leerstand, Selfstorage erhalten;
>    Soll 0 bedeutet NICHT automatisch Leerstand.
> 2. Eröffnung: entweder Einzel-OP zum Stichtag ODER bestätigter
>    Gesamtsaldo, niemals Saldo plus enthaltenes altes Journal doppelt
>    buchen. Saldo ohne bekannte Fälligkeit/Anspruch sichtbar, aber
>    nicht automatisch mahnen. Nachbuchungen mit Beleg,
>    Leistungsperiode, Buchungsdatum, Fälligkeit und Änderungsgrund.
>    Korrektur/Storno statt Historie überschreiben. Quellzeile/Hash und
>    idempotente Import-IDs. Gleiche ID mit verändertem Inhalt =
>    Konflikt.
> 3. Vorschreibung: zeitlich gültige Vertragsversionen,
>    Einzelkomponenten HMZ, Küche, Parkplatz, Keller/Garten etc.,
>    BK/Heiz-/WW-Vorauszahlungen, USt/Steuercode getrennt. Pro
>    Vertrag/Monat genau einmal. Untermonate/Nachlass nach bestätigter
>    Regel. Fälligkeit aus Vertrag/Profil. Entwurf, Sollstellung,
>    Dokumentzustellung, Hauptbuch-Export, Zahlungsabgleich getrennt.
>    Mietforderung kann aus wirksamem Vertrag entstehen; keine
>    pauschale Übernahme der AR-Regel, dass nur eine versendete Rechnung
>    eine Mietforderung sei.
> 4. Bank: CAMT.053 und konfigurierbarer CSV-Import, Originale privat,
>    Source Hash, Zeitraum/Konto/Währung prüfen, Wiederholimporte
>    abfangen. Zahlung und Zuordnung getrennt. Automatisch nur
>    eindeutige Referenz und Konto; Name/gleicher Betrag allein reichen
>    nicht. Teil-/Sammelzahlungen, Guthaben, Gutschriften,
>    Rücklastschrift, Klärkonto. Nie gesellschaftsübergreifend
>    ausgleichen. OP = Eröffnung + Soll/Nachbelastung - Gutschrift -
>    zugeordnete Zahlung + Rücklastschrift. Fälliger unstrittiger Rest
>    separat. Bankvollständigkeit bis Stichtag sichtbar, nicht bloß
>    File-mtime.
> 5. Index: Rechtsordnung, MRG/Mietzinsbeschränkung/WGG/Vertragsart,
>    gültige Klausel, Abschlussdatum, Basisreihe/-wert/-monat, letzte
>    Anpassung, Schwelle und indexierbare Komponenten versioniert.
>    Fehlende Klassifizierung sperrt wirksame Erhöhung. Österreich 2026:
>    MieWeG §§1,4 plus Vertrag; April-Termine, Jahresdurchschnitt,
>    3%-Dämpfung, gegebenenfalls 1%-/2%-Grenze, anteilige
>    Erstvalorisierung und Altvertragsübergang. Kein pauschaler
>    Monats-VPI-Quotient und keine Indexierung von BK/gesamter
>    Bruttomiete. Exakt halber Cent nach §1 Abs2 Z3 abwärts runden,
>    separat vom üblichen Runden anderer Geldposten. Vertragliche
>    niedrigere Grenze beachten. Gewerbe/WGG/Deutschland eigene
>    Profile. Zunächst überprüfbare Vorschläge; nur fachlich
>    freigegebene versionierte Regelprofile erzeugen spätere
>    Sollstellungen. Keine rückwirkenden Forderungen aus ungeprüften
>    Klauseln. Quellen:
>    https://www.ris.bka.gv.at/Dokument.wxe?Abfrage=Bundesnormen&Dokumentnummer=NOR40274266
>    und
>    https://www.ris.bka.gv.at/Dokument.wxe?Abfrage=Bundesnormen&Dokumentnummer=NOR40274269
>    ; Statistik Austria Indeximport. Kein Rechtsprofil blind als
>    universell zertifizieren.
> 6. BK: WEG-Eigentümerabrechnung ist keine Mieterabrechnung. Rücklage,
>    Finanzierung, Sonderumlage, Reparatur, Verwaltungs-/
>    Abrechnungskosten nicht pauschal umlegen. Je Position
>    umlagefähig/Eigentümer/ungeklärt mit Quelle und Profil.
>    Abrechnungsjahr, Flächen/Anteile/Verbrauch, Mieterwechsel,
>    Vorauszahlungen und gesetzliche Stichtagsregeln berücksichtigen.
>    MRG §§21–24/HeizKG/Vertrag. Nachbelastung oder Gutschrift erst
>    nach geprüfter Abrechnung und gültiger Fälligkeit.
> 7. Automatische Mahnungen: Implementierungsauftrag ausdrücklich
>    erteilt; nach fachlicher Abnahme einer versionierten Policy laufen
>    Standardfälle ohne erneute KI-/Markus-Freigabe. Entwicklung/Pilot
>    standardmäßig SEND_ENABLED=false, nur Preview/Outbox.
>    Konfigurationsvorschlag: Stufe1 7 Tage nach Fälligkeit, Stufe2
>    mindestens14 Tage nach erfolgreichem Erstversand UND nach
>    dortiger Zahlungsfrist. Pro Forderung max2 Stufen; keine dritte
>    Mahnung, Kündigung, Inkasso-/RA-Nachricht. Zinsen/Gebühren
>    Default0. Nur geprüftes Profil/Vertrag/Empfänger, bestätigter
>    fälliger Rest, aktueller vollständiger Bankstand (Default
>    maximal2 Tage) und keine Sperre. Sperren: Streit,
>    Mietminderung/Schimmel, Ratenplan, Insolvenz, RA, ungeklärter
>    Eingang, unklarer Eröffnungssaldo, Bounce, manuelle Sperre.
>    Ausnahme -> interner Bearbeitungsantrag. Vor Versand Bank/OP
>    unter Transaktion erneut prüfen, Zahlung nach Planung verhindert
>    Versand. Eindeutige Outbox-Schlüssel nach
>    Gesellschaft/Vertrag/Forderungsumfang/Stufe, Locks, Audit,
>    Snapshot/Template/Regelversion. Stage2 nur nach gesendetem
>    Stage1. Provider-Timeout nach möglicher Annahme -> uncertain, KEIN
>    blinder Retry. Kein fiktives exactly-once Versprechen für SMTP;
>    Provider-Idempotenz oder Abklärung.
> 8. Betrieb: DB/Worker/Scheduler unabhängig vom Browser/Chat.
>    Start/Stop, Auth/Rollen, Gesellschaftstrennung serverseitig,
>    Fehlerqueue, Bankfrische, Backups/Restore, Wiederanlauf.
>    Credentials außerhalb Git; Bank/Mail-Refresh wo unterstützt, Ablauf
>    mit Warnung. Keine ewige Auth garantieren. CAMT/CSV-Dropfolder
>    zuerst, bestehende API/EBICS nur später belegt. Versionierte
>    API/Domain-Events für späteren Mail-/Dokumentversand, KEINE
>    allgemeine Antwort-KI in MVP1. Geld Decimal/Integer-Cent, UTC und
>    Europe/Vienna-Fälligkeiten.
>
> LIEFERUNG: echte Implementierung + Migration + API + schlanke
> Bedienoberfläche passend zum Repo, Importvorlagen, synthetische
> Daten, Tests, Betriebsanleitung, klare offene Integrationspunkte.
> Speichere diesen Auftrag als docs/hausverwaltung/RAHMENPROGRAMM.md und
> eine Review-Zusammenfassung. Teile zum Schluss Branch, Commit,
> Testbefehle, geänderte Dateien, Start-/Demoanleitung und offene
> Punkte mit.
>
> MINDESTTESTS: Eröffnung100 + Soll600 - Zahlung200 =500;
> Doppelimport wirkungslos; geänderte gleiche ID Konflikt; Saldo+
> enthaltenes Journal nicht doppelt; Cross-Tenant-Zugriff/Zuordnung
> blockiert; unklarer Eröffnungssaldo nie Mahnung; Teilzahlung700-300
> =400; Überzahlung800auf700=100Guthaben; Rücklastschrift macht OP
> wieder offen; Küche/Parkplatz enthalten; BK nicht indexiert; fehlende
> Klausel/Profil blockiert; gesetzlicher Halbcent; WEG-Rücklage kein
> automatischer Mieter-OP; BK-Entwurf nicht mahnen; veraltete
> Bank/Streit/Ratenplan sperren; Zahlung zwischen Planung/Versand
> stoppt; zwei Worker/Neustart keine Doppelvorschreibung/Mail; Stage2
> ohne Stage1 verboten; Provider-Timeout kein Doppelversand; nach
> Stufe2 nur interner Fall; Status Selfstorage/Kurzzeit bleibt; Kaution
> getrennt; 107 ausgeschlossen; Änderungen nach Freigabe invalidieren
> sie; Restore wiederholbar; alle Standardfunktionen OHNE KI-Key.
>
> Beginne jetzt mit Repo-Prüfung und Implementierung. Codex liest deinen
> Fortschritt und prüft anschließend die Dateien unabhängig. Nicht nur
> delegieren oder mit einem Plan enden.

## Ergänzende Klarstellungen (während der Umsetzungssitzung)

- **Quellenmapping/Cutoff:** Das Originaljournal
  `20260831 Buchungsjournal.pdf` enthält trotz Dateiname tatsächlich
  Buchungszeilen mit Datum 01.09.2026 (u. a. Seiten 8, 27, 28, 73,
  77–79, 83). Der Dateiname darf beim späteren Quellenmapping NICHT als
  harter Stichtags-Cutoff verwendet werden. Das lokale
  12-PDF-Quellinventar war zum Zeitpunkt dieser Sitzung erstellt, aber
  noch nichts davon importiert.
- **Granularität von Salden/Mahnung:** Salden und Mahnfähigkeit sind
  pro Forderung (OP-Position mit eigener Fälligkeit) zu berechnen, nicht
  pauschal pro Konto/Objekt aggregiert. Soll- und Habensalden bleiben je
  Debitor getrennt; die Nettosumme eines ganzen Objekts ist niemals
  gleichzusetzen mit der Summe mahnbarer Einzelforderungen.
- **Tenant-Scope:** Die Mandantentrennung (Gesellschaft) muss
  DB-seitig geprüft werden, nicht nur als Konvention in der
  Service-Schicht. Siehe `bank/repository.py::create_zuordnung` als
  Referenzimplementierung (Prüfung anhand frisch aus der DB gelesener
  Zeilen, nicht anhand von Aufrufer-Parametern).
- **Technische Konten:** Im Quellsystem existieren technische
  Verrechnungskonten (z. B. ein Leerstands-Sammelkonto), die keine
  Mieter/Debitoren sind. Solche Konten dürfen nicht wie ein Mietkonto
  behandelt oder bemahnt werden; ihre Erkennung ist Teil des
  Quellenmappings (Codex), nicht dieser Codebasis.
- **Paketierung:** Das gebaute Wheel muss `src/mietinkasso` enthalten
  (nicht nur `src/invoice_automation`).
- **Arbeitsregeln:** `AGENTS.md` und `CLAUDE.md` (bytegleich) im
  Repo-Root beschreiben Scope, Owner-Aufteilung, Synthetic-only-Regel
  und Send-off-Protokoll für diesen Auftrag.

- **Unabhängige Abnahme (Codex, HEAD `38e8895`):** die 147 vorhandenen
  Tests liefen grün, aber 8 zusätzliche fachliche Gegenproben von Codex
  deckten reale Fehler auf (u. a. Mahnwesen-Doppelversand, ignorierte
  Mahnfristen, doppelte Eröffnung, ungeprüfte Zuordnungsbeträge,
  Cross-Tenant-Phantom-Buchung, Bank-Doppelimport bei überlappenden
  Exports, falsche Index-Schwellen-/Dämpfungsformel) plus weitere
  Abnahmesperren (Mahnstufen pro Vertrag statt pro Forderung, fehlende
  Neuprüfung bei `versenden`, offene HTTP-Endpunkte, erfundene
  Zustellungs-/Exportnachweise, fehlende Auth in Index/BK). Alle sind
  behoben und mit Regressionstests belegt; Details in
  `docs/hausverwaltung/REVIEW_ZUSAMMENFASSUNG.md`.

Die vollständige, laufend aktualisierte Statusübersicht steht in
`docs/hausverwaltung/REVIEW_ZUSAMMENFASSUNG.md`; offene technische
Punkte in `docs/hausverwaltung/OFFENE_PUNKTE.md`.
