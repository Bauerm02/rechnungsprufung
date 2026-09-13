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

## Auftrag HV-20260912-ECHTBETRIEB (wortgetreu, 12.09.2026)

> Neuer ausdrücklicher Nutzerauftrag HV-20260912-ECHTBETRIEB: Markus
> erlaubt jetzt, die echten Hausverwaltungs-Stammdaten und Salden in das
> Programm zu übernehmen, zwei Mahnstufen einzustellen und den
> geschützten Onlinebetrieb auf seinem bestehenden Hetzner-Server über
> die JLB-Webseite einzurichten. Automatische Bankabholung und
> automatische Zahlungszuordnung ausdrücklich zurückgestellt bis zu
> seiner EBS/EBICS-Lösung. Du implementierst weiter im bestehenden
> Branch claude/bold-volta-7xovjq mit Sonnet 5, High, Fast AUS; Codex
> macht privates Datenmapping, unabhängige Abnahme und kontrollierten
> Serverbetrieb. Die alte Synthetic-only/kein-Deployment-Grenze ist für
> Codex durch diesen neuen Nutzerauftrag abgelöst. Bei dir bleiben
> Testdaten synthetisch; keine echten Personen, Bankdaten, Dokumente
> oder Secrets in Git oder Cloud-Code. Aktualisiere AGENTS.md/CLAUDE.md
> dateigleich mit der differenzierten neuen Freigabe und
> RAHMENPROGRAMM/OFFENE_PUNKTE.
>
> Implementiere die notwendigen Ergänzungen, ohne das Rechnungsmodul
> anzufassen:
>
> 1. Generischer atomarer, idempotenter Intake für Gesellschaften/
>    Objekte/Einheiten/Debitoren/Verträge sowie bestätigte
>    Eröffnungssalden zum Stichtag und separat datierte Nachbuchungen.
>    Vorhandene Services und IDs nutzen. JSON/CSV-Dry-run erzeugt
>    lesbaren Plan mit Quelle/Hash; Apply bindet sich an identischen
>    Inhalt und schreibt in eine ausdrücklich angegebene private DB
>    außerhalb Repo. Ein Fehler => gesamter Lauf unverändert.
>    Wiederholung keine Duplikate; geänderte gleiche Quell-ID Konflikt;
>    Eröffnungssaldo und enthaltenes Journal niemals doppelt; keine
>    Produktionsdaten aus Demo seed. Codex erzeugt echte Eingabe. Früh
>    Schema/Beispiel und CLI-Befehle nennen, damit Mapping parallel
>    möglich ist. Fehlende Fälligkeit, Kontakt-/Vertragsfreigabe oder
>    ungeprüfte Anfangssalden als sichtbare Sperrgründe, nicht erfundene
>    Werte. Stammdaten/Nutzungsstatus auch ohne aktive Mietforderung
>    erfassbar (Leerstand, KZV, Selfstorage). Objekt107 ausgeschlossen.
>    Keine historische Sollstellung durch Datenimport starten.
> 2. Genau zwei konfigurierbare Mahnstufen im Backoffice sichtbar
>    speichern: Stufe1 7 Tage nach belegter Fälligkeit, Stufe2
>    frühestens14 Tage nach tatsächlich versandter Stufe1 und erst nach
>    deren Zahlungsfrist, keine Zinsen/Gebühren. Bestehende Sperren
>    bleiben, RA/Ratenplan/unklare Salden/fehlende
>    Bankvollständigkeit/unklare Zahlungseingänge. Keine tatsächliche
>    Mietermail aus diesem Entwicklungsauftrag; SEND_ENABLED=false,
>    Vorlagen/Planung konfigurierbar und überprüfbar. Fehlende
>    automatische Bankversorgung klar anzeigen, manuellen geprüften
>    Bankstand zulassen, keine Bankvollständigkeit erfinden.
> 3. Produktionsstart zunächst für Markus als EINEN Operator über HTTPS
>    mit persistenten Daten, ohne Demo-Seed. Sichere Auth/Sitzung/CSRF
>    für Reverse Proxy, sichere Cookies, keine offenen API-Hintertüren.
>    Minimales Deployment-Paket für bestehendes Hetzner mit env
>    außerhalb Git, Health ohne Kundendaten, Backup/Restore-Anleitung
>    und Einzelinstanz/DB-Transaktionsschutz. Bestehende Auth/DB nutzen
>    wo passend; keine zusätzlichen kostenpflichtigen Dienste. Codex
>    prüft vorhandene Serverinfrastruktur und übernimmt Integration, du
>    greifst nicht auf Server zu und deployest nicht.
>
> Akzeptanz: Einspielung technisch möglich ohne Rohdaten in Git;
> Kontosalden centgenau rücklesbar, Dublettentest/Rollback synthetisch;
> keine Mahnung bei gesperrten Konten; zwei Stufen ohne Stufe3;
> Produktionsstart enthält null Demo-Daten; unautorisierter
> Onlinezugriff blockiert. Tests angemessen, im bestehenden Branch
> committen/pushen. Codex editiert deine Implementierungsdateien nicht.
> Erledigt-Nachweise erhalten. Liefere früh Importvertrag und dann
> Umsetzung bis fertig.

### Umsetzung/Konkretisierungen (diese Sitzung)

- **Importvertrag zuerst geliefert:** `docs/hausverwaltung/IMPORT_VERTRAG.md`
  (Schema, Beispiel, CLI-Befehle), damit Codex das reale Mapping
  parallel zur Implementierung beginnen kann.
- **Sperrgründe vs. sichtbare Hinweise:** "ungeprüfte Anfangssalden"
  wird als hartes Pflichtfeld (`quelle_bestaetigt`) je Eröffnungszeile
  umgesetzt — fehlt/false, blockiert die GESAMTE Einspielung (Konflikt
  mit "Ein Fehler => gesamter Lauf unverändert" wäre sonst nicht
  auflösbar). "Fehlende Fälligkeit" und "fehlender Debitor-Kontakt"
  werden NICHT hart geblockt, weil das bestehende System sie bereits
  sicher als "nicht automatisch mahnfähig" behandelt (siehe
  `mahnwesen/service.py::plane_forderung`) — der Intake-Plan weist sie
  stattdessen als sichtbare Zähler/Hinweise aus, damit nichts erfunden,
  aber auch nichts unnötig blockiert wird, was das System ohnehin schon
  konservativ behandelt. "Vertragsfreigabe" hat keine eigene
  Datenbankspalte in diesem Schema; sie wird über dieselbe
  `quelle_bestaetigt`-Pflicht auf Eröffnungsebene sowie die bereits
  bestehende Sperren-Tabelle abgedeckt — kein neues, unbelegtes
  Fachkonzept erfunden.

## Ergänzung zu HV-20260912-ECHTBETRIEB (wortgetreu, 12.09.2026)

> Generische Schema-Ergänzungen, ausschließlich synthetische
> Anforderungen: 1. Rechtsordnung UNGEKLAERT zulassen, daraus explizite
> Mahn-/Index-/Sollstellungssperre. Keine Pflichtkategorie raten. 2.
> Intake muss optionale Prüfhinweise/Sperren pro Vertrag dauerhaft
> speichern und Backoffice anzeigen, etwa RECHTSANWALT/RATENPLAN/MANUELL
> für ungeklärte Vertrags-/Kontaktfreigabe. 3. Synthetisches Beispiel:
> Original-Gesamtsaldo zu Monatsende enthält eine nachweislich fehlende
> Zahlung nicht; tatsächliches Zahlungsdatum liegt VOR dem
> Eröffnungsstichtag. Eine ausdrücklich belegte Eröffnungskorrektur
> braucht deshalb eigenes Flag/Grund/Quell-ID und muss
> Original-Belegdatum bewahren, Buchungsdatum ist Übernahmetag. Nicht
> Altjournal allgemein freigeben. Originalsaldo plus ergänzende Zahlung
> nachvollziehbar; idempotent, keine Doppelerfassung. 4. Bitte optionale
> Vertragskomponenten über denselben atomaren Intake aufnehmen
> (bestehende Tabelle/Service; keine Indexfreigabe), damit HMZ/Küche/
> Parkplatz/BK auseinander lesbar bleiben. Schemaänderungen
> dokumentieren. Keine echten Daten in dieser Nachricht, keine
> produktiven Funktionstests.

### Umsetzung/Konkretisierungen (diese Ergänzung)

- **Rechtsordnung UNGEKLAERT ist ein neuer Enumwert, kein neues Feld**
  (`domain/enums.py::Rechtsordnung.UNGEKLAERT`). Die drei Sperren
  (Mahnung/Index/Sollstellung) laufen über eine EINZIGE gemeinsame
  Prüffunktion (`domain/enums.py::rechtsordnung_geklaert`), damit sie
  nicht unabhängig voneinander (und potenziell inkonsistent) je
  Aufrufer neu formuliert werden. Mahnung liefert `BLOCKIERT` (bestehende
  Ergebnis-statt-Exception-Konvention von `plane_forderung`/`versenden`);
  Index/Sollstellung werfen `RechtsordnungUngeklaertError`, da diese
  Methoden bereits an anderer Stelle Exceptions werfen.
- **Sperren nutzen die bestehende `SperreTable`, keinen neuen
  Mechanismus:** `mahnwesen/service.py` wertete `aktive_sperren` schon
  vor dieser Ergänzung als harte Mahnsperre aus. Der Intake bekommt nur
  einen neuen, additiven Einspielweg dafür (`sperren[]`) plus
  Sichtbarkeit im Kontoauszug des Backoffice. Eine Sperre AUFHEBEN
  bleibt bewusst ein manueller Backoffice-Schritt, nicht Teil des
  Intakes (sonst könnte ein Re-Import versehentlich eine bewusst
  gesetzte Sperre stillschweigend entfernen).
- **Eröffnungskorrektur reicht KEIN neues Altjournal-Tor:** Sie ist eine
  eigene Methode (`op/service.py::eroeffnungskorrektur_buchen`), nicht
  eine Lockerung von `pruefe_kein_altjournal_in_gesamtsaldo` — eine
  gewöhnliche Nachbuchung mit Belegdatum vor/auf dem Gesamtsaldo-
  Stichtag bleibt weiterhin ein harter Konflikt. Das "eigene Flag"
  wird bewusst NICHT als neue Bool-Spalte umgesetzt, sondern über den
  bereits vorhandenen `quelle_system`-Wert `"eroeffnungskorrektur"`
  (eindeutig unterscheidbar von `"intake_import"`/`"backoffice"`/...);
  `grund` landet im bereits vorhandenen `aenderungsgrund`-Feld,
  `quelle_referenz` wird Teil der `beleg_referenz`. Kein Schema-Migrationsbedarf.
  `belegdatum` bleibt das echte historische Datum, `buchungsdatum` wird
  vom Aufrufer (`intake/apply.py::wende_an`, neuer optionaler
  `heute`-Parameter) auf den Übernahmetag gesetzt, nie aus der Datei
  übernommen.
- **Vertragskomponenten nutzen `add_komponente` unverändert als reines
  Insert**, keine neue Upsert-Semantik am bestehenden Service — die
  Replay-/Konfliktlogik ("gleiche ID = No-Op, geänderte ID = Konflikt")
  läuft komplett im Intake-Planer/-Apply (Hash-Vergleich vor dem
  Schreiben), analog zu den übrigen Stammdaten-Entitäten. `indexierbar`
  bleibt reines Eignungsflag für `index/service.py`; der Intake ruft an
  keiner Stelle `IndexService` auf — "keine Indexfreigabe" ist damit
  strukturell erzwungen, nicht nur dokumentiert.

## Auftrag HV-20260913-INDEXAUTOMATIK (wortgetreu, 13.09.2026)

> Neuer verbindlicher Nutzerauftrag HV-20260913-INDEXAUTOMATIK. Bitte
> auf dem bestehenden freigegebenen Branch claude/bold-volta-7xovjq ab
> ae3abb4 implementieren, testen, committen und normal pushen. Sonnet
> reicht; Fast bleibt aus. Du implementierst, Codex prüft unabhängig und
> übernimmt reale Stammdaten/Betrieb. Nur synthetische Daten in
> Code/Tests, keine Serverzugriffe oder echten Mails/Buchungen,
> Rechnungsmodul und EBICS-Baum nicht ändern. Keine parallelen
> Code-Writer.
>
> Ziel: dauerhafter deterministischer Betrieb ohne KI: jeden Monat
> vertragliche Indexierung prüfen, rechtlich ausführbare Erhöhung mit
> korrektem Schreiben automatisch zur Zustellung bringen; zusätzlich
> drei Kalendermonate vor vertraglichem Mietende ausschließlich den
> intern konfigurierten Eigentümer/Markus erinnern. Mieter bei
> Vertragsende ERST nach seiner ausdrücklichen gespeicherten
> Entscheidung kontaktieren. Nicht automatisch kündigen, verlängern
> oder Leerstand buchen. Klare UI im bestehenden geschützten
> Backoffice, keine zweite Datenquelle.
>
> Bitte vollständigen zusammenhängenden Workflow im vorhandenen
> Mietmodul bauen (persistente Tabellen/Migration, Service, CLI/
> systemd-Vorlagen, Backoffice, Tests/Doku):
>
> 1. Monatsprüfung je aktivem Vertrag und Monatsperiode idempotent,
>    prozess-/DB-sicher, bei Wiederholung oder Absturz keine
>    Doppelbuchung/Double-Send. Bestehende Quellen/Komponenten, aktuell
>    verrechneter Betrag und letzte tatsächlich verwendete Indexbasis,
>    nicht nur historischer theoretischer Verlauf; rückdatierte
>    Veröffentlichungen und bereits angewandte Erhöhungen beachten.
>    Monatslauf prüft; Ausführung nur zum vertraglich und gesetzlich
>    erlaubten Termin. Kalenderzeit Europe/Vienna, Monatsende/
>    Schaltjahr sauber.
> 2. Wiederverwendung der abgenommenen mieweg_vorschau/index-Rechner,
>    KEIN paralleler ungeschützter Rohrechner. Für jeden Fall
>    versioniertes freigegebenes Profil mit Vertragsbeleg/Klausel/
>    Rechtsordnung, Wohnung versus Geschäft explizit, Haupt-/
>    Untermiete, Förderbindung/Mietzinsobergrenze und Bezug/
>    Letzterhöhung. Ungeklärte Daten blockieren nur den Fall, erzeugen
>    konkrete interne Prüfliste. Nutzer will MRG-Vollanwendung
>    ausdrücklich mit abgedeckt: MieWeG für Wohnungen im Voll-/
>    Teilbereich, Deckel/April-Termin/Altvertragsübergang prüfen; MRG
>    §16 Abs9 Höchstzins und schriftliches Erhöhungsbegehren nach
>    Wirksamwerden, rechtzeitiger ZUGANG mindestens 14 Tage vor
>    maßgeblichem Zinstermin. Keine pauschale Rechtsklassifikation
>    MRG_VOLL=>Wohnung. Eigentümer-Rechtsprofilfreigabe einmal
>    versioniert, danach keine unnötige monatliche Einzelgenehmigung
>    unveränderter Standardfälle. Änderungen an Quelle/Basis/Vertrag/
>    Profil entwerten alte Freigabe. Gesetzesdetails prüft Codex
>    separat und liefert Präzisierungen; keine eigene Rechtsannahme bei
>    Lücke.
> 3. Nur indexfähige Komponenten, Küche/Stellplatz gemäß eigener
>    Vertragsgrundlage; BK/HK/Wasser/Strom etc niemals pauschal
>    indexieren. Netto/USt/Brutto transparent. Gesetzliche Obergrenze
>    ist harte Bedingung (inklusive deren Beleg/Gültigkeit); fehlend=>
>    blockiert statt fiktiv unbegrenzt. Bei MRG muss ein bloß
>    versendetes SMTP-E-Mail nicht als rechtzeitiger Zugang gelten.
>    Formal geeigneten Zustellweg samt Beleg modellieren; bei fehlendem
>    Zugang oder Formnachweis keine erhöhte Sollbuchung und keine
>    rückwirkende Mahnung. Fristversäumnis=>nächsten rechtlich
>    zulässigen Zinstermin bestimmen, nicht starr Versanddatum+14
>    buchen.
> 4. Qualifiziertes deutsches Erhöhungsschreiben: Gesellschaft aus
>    Vertrag, Adresse/Top/Mieter, Klausel, Reihe/Basis-/Vergleichsmonat/
>    -werte, Rechenweg, alter/neuer HMZ sowie Küche/Parkplatz einzeln
>    und USt/Gesamtsumme, Wirksamkeits-/Zahlungstermin, unveränderte BK
>    transparent, Belege/Version, JLB-Signatur aus Konfiguration.
>    Rechts-/Zustellform explizit; HTML allein oder Provider-Acceptance
>    nicht als qualifizierte Schriftform/Zugang ausgeben. Persistente
>    Outbox mit Zuständen Entwurf/blockiert/bereit/gesendet/Zugang
>    bestätigt/ausgeführt/unklar; vor Dispatch Quelle/Empfänger/
>    Vertragsstatus erneut prüfen. Stabile Idempotenz an Transport,
>    unklarer Timeout wird NICHT blind wiederholt. Ein
>    Transportadapter/konfigurierbarer interner Dienst genügt, mit
>    echten implementierten Request-/Responsechecks und isolierten
>    Fakes für Tests; keine erfundene Liveintegration. Versand-/Index-
>    und Owner-Erinnerungsflags getrennt (keine globale Aktivierung von
>    Mahnungen). Produktionsdaten nicht ändern.
> 5. Vertragsende: Trigger end_date minus 3 Kalendermonate (kein 90
>    Tage), fällige verpasste Hinweise einmal nachholen, zukünftige
>    Frist beachten. Unbefristet/kein belastbarer Endtermin=>kein
>    erfundener Leerstand. Stabile Aufgabe je Vertrag+Enddatum, nach
>    Verlängerung alte Aufgabe/Outbox ungültig, neuer Termin neu
>    planbar. Interner Hinweis mit Objekt/Top/Mieter/Ende und
>    Entscheidung im authentifizierten Portal 'verlängern prüfen'/
>    'nicht verlängern prüfen'/'Rückfrage'. Keine Entscheidung über
>    GET-Link, CSRF/Auth/Audit. Empfänger hart Owner-only; kein
>    tenant_email Fallback/CC. Entscheidung erzeugt höchstens
>    freizugebenden Mieterentwurf, niemals automatisches rechtlich
>    bindendes Kündigungsschreiben oder einen neuen Vertrag.
> 6. Produktivplan: monatlich Indexkontrolle (z.B. 1. um 06:00 Wien),
>    täglich fällige Zustellungen/3-Monats-Erinnerungen nachziehen
>    (keine KI-Automation). Ein Writer, Lock/Idempotenz, sichtbarer
>    letzter Lauf/Fehler/konkrete Blocker. Default Versand aus; Codex
>    kann nach Abnahme definierte Features separat aktivieren, ohne
>    bestehende Mahnsperren aufzuheben. Migration muss die bestehende
>    reale DB verlustfrei erweitern.
>
> Abnahmetests: Januarvertrag außerhalb Januar; genau 3% versus >3%;
> Wohnung Voll/Teil, Geschäft unter MRG, unbekanntes Profil; April/
> 14-Tage-Zugang/fehlender Zugang/Fristversäumnis; Deckel/
> Fördergrenze; letzte Basis+bereits angewandte Erhöhung; fehlende
> amtliche VPI-Publikation; Doppel-/Parallelstart/Crash/unklarer
> Versand; abgelaufener Vertrag und geänderter Empfänger; leap-day/
> 31.05 minus 3 Monate; Reminder nur Owner, Änderung des Enddatums,
> unbefristet, tenant action nur nach Entscheidung; CSRF/
> Objektzugriff. Bitte Kern integrieren statt nur TODOs oder isolierte
> Bibliothek. Vor Umsetzung kurz Scope/Ausgangscommit prüfen; danach
> arbeiten bis Übergabecommit mit Testnachweis und ehrlichen Resten.

### Fachlicher Nachtrag (13.09.2026, während der Umsetzung)

> Fachlicher Nachtrag, Codex hat die amtlichen RIS-PDFs am 13.09.2026
> tatsächlich heruntergeladen/gelesen (NOR40274266 §1, NOR40274269 §4,
> NOR40273694 §16). Wichtig für Implementierung: §1 Abs2 Z1 positiver
> Anteil über 3% halbiert; erste volle Monate nach Bezug und
> Halbcent-ABRUNDUNG; Abs3 Bezugsjahre 2025=1%, 2026=2% nur bei
> Wohnungs-Mietzinsbeschränkung. §4 Abs2 Altvertrag: tatsächlich
> zuletzt angewandter Indexbezugsmonat, nicht automatisch Miet-/
> Verwaltungsbeginn. Abs4 Vertragsdeckel+gesetzlicher Deckel,
> April-Modelltermin. §16 Abs9 bleibt unberührt: Höchstzins+nach
> Wirksamwerden ergehendes Schreiben und 14 Tage Zugang.
> Modell-Wirksamkeit (April) und erste Zahlungspflicht nach
> fristgerechtem Zugang sind GETRENNTE Termine; verspätete Zustellung
> nicht automatisch um ganzes Jahr schieben. Präzisierung: "qualifiziert
> richtig" heißt fachlich korrektes Schreiben, NICHT pauschal
> QES-Pflicht! Nicht jedes E-Mail verbieten; konkreten dokumentierten
> Zustellweg/Formregel als Profil prüfen. Keine automatische
> Gleichsetzung von SMTP/HTTP-accepted mit Zugang. JLB-Versand nutzt
> gestaltete HTML-Signatur mit eingebettetem Original-Logo, bestehende
> Mailstrecke; generischer Transport mit strengem dokumentiertem
> Request/Response-Vertrag, keine Fake-Live-Behauptung. Codex prüft
> gerade vorhandene Betriebsanbindung. Bitte jetzt fokussiert
> implementieren; keine weiteren breiten Architektursuchen oder
> zusätzlichen Agenten. Bestehende Baugruppen wiederverwenden. Erst
> vollständiger Kern inkl. UI/CLI/Tests und Übergabecommit; keine
> fremden Module anfassen. Ist dies in sinnvollen Teilcommits möglich,
> bitte Zwischenergebnisse committen/pushen, damit Codex die
> unabhängige Prüfung parallel beginnen kann, ohne deine Dateien zu
> ändern.

### Umsetzung/Konkretisierungen (diese Sitzung)

- **Terminmodell §16 Abs9, aus dem Nachtrag abgeleitet:** die
  MieWeG-April-Wirksamkeit (`fruehester_termin_gesamt` aus
  `mieweg_vorschau/service.py`) und die tatsächliche
  Zahlungspflicht-Fälligkeit sind zwei GETRENNTE Daten. Ein
  Erhöhungsschreiben ergeht ERST NACH diesem April-Termin (nie davor);
  die Zahlungspflicht beginnt erst am nächsten monatlichen Zinstermin
  (`VertragTable.faelligkeit_tag`), der mindestens 14 Tage nach
  tatsächlich bestätigtem Zugang liegt. Eine verspätete Zustellung
  verschiebt daher nur den nächsten monatlichen Zinstermin, NIE den
  ganzen April-Zyklus um ein Jahr - siehe `indexautomatik/outbox_service.py`.
- **Zwei-Phasen-Nutzung von `mieweg_vorschau_service.vorschau_erstellen`:**
  Phase 1 (Planung, vor Versand) ruft ihn ohne
  `zustellnachweis_referenz` auf und verwendet ausschließlich
  `rechnerische_differenz_cent` (nicht `ausfuehrbare_erhoehung_cent`,
  das dort bewusst einen bereits vorhandenen Zustellnachweis
  voraussetzt - hier aber zirkulär wäre, da der Nachweis erst NACH
  dem Versand entsteht) für die Entscheidung, ob ein Schreiben
  überhaupt erzeugt wird. Phase 2 (nach bestätigtem Zugang) ruft ihn
  ERNEUT mit dem jetzt vorliegenden Zustellnachweis auf und erzeugt
  damit den finalen, tatsächlich `vollstaendig`en Prüfsatz mit
  gesetztem `ausfuehrbare_erhoehung_cent` als Abschlussbeleg. Die
  bereits abgenommene Paket-C-Datei `mieweg_vorschau/service.py`
  selbst wird dafür NICHT verändert.
- **VPI-Jahresdurchschnittswerte** sind eine neue, rein manuell im
  Backoffice gepflegte Tabelle (`VpiJahreswertTable`) - kein
  automatischer Statistik-Austria-Abruf (kein Serverzugriff durch
  Claude). Eine fehlende Publikation für ein benötigtes Jahr blockiert
  den betroffenen Fall sichtbar, erfindet aber nie einen Wert.
- **Transportadapter:** `indexautomatik/transport.py` definiert einen
  generischen HTTP-Vertrag (Request/Response-Felder, Timeout->
  `TransportFehlerUngewissError`, kein Blind-Retry) plus
  `FakeTransportadapter` für Tests. Die tatsächliche JLB-Mailstrecke
  (HTML-Signatur, Original-Logo) verdrahtet Codex serverseitig; dieses
  Modul erfindet keine Live-Integration.
- **Keine automatische Sollstellung/Vorschreibungsänderung:** dieser
  Auftrag deckt Prüfung, Schreiben-Generierung und Zustellung/
  Zugangs-Tracking ab, NICHT das automatische Nachziehen von
  `VertragsKomponenteTable.betrag_cent`/einer neuen Vorschreibung -
  das bleibt ein bewusst offener, in `OFFENE_PUNKTE.md` benannter
  manueller Folgeschritt (entspricht der bestehenden
  "keine Produktivbuchungen durch Claude"-Grenze).

## Auftrag HV-20260913-DASHBOARD (wortgetreu, 13.09.2026)

Neuer klar begrenzter Nutzerauftrag HV-20260913-DASHBOARD, auf
bestehendem Branch claude/bold-volta-7xovjq, aufbauend auf 1da3843.
Bitte implementieren, testen, committen und nur diesen Branch pushen.
Bestehende Index-/EBICS-/Mail-Restarbeiten bleiben separat. Codex
übernimmt Quellen und Deployment; du ausschließlich generischer Code
und synthetische Daten, keine Serverzugriffe oder Produktivdaten.
Sonnet 5 High, Fast aus beibehalten.

1) Dashboard/Konto: "Saldo" verständlich als "Kontostand (offen /
   Guthaben)", "fälliger unstrittiger Rest" als "Davon mit bekannter
   Fälligkeit" beschriften. Rechenweg Eröffnung + Vorschreibungen -
   Zahlungen/Gutschriften zeigen. Unbekannte Fälligkeit ist nicht
   automatisch strittig. Aktive Mahnsperren einschließlich Gründe
   sichtbar darstellen; bekannte Fälligkeit ist keine Mahnfreigabe.
   Reine Darstellung, keine Änderung bestehender OP.

2) Bestehendes Backoffice um monatliche Abrechnungen für
   KURZZEITVERMIETUNG und SELFSTORAGE erweitern. Eingabe: bestehende
   Objekt-/Einheit-ID, Leistungsmonat, Belegdatum, Quellenreferenz/
   Hash, berichteter Nettoumsatz, unser Nettoanteil (maßgeblich),
   optionale Abzüge, vermietete Einheiten/Fläche. Bericht/Anteil und
   tatsächlicher Zahlungseingang getrennt. Keine pauschale
   USt-Umrechnung; fehlende Werte bleiben unbekannt. Keine OP-/
   Bankbuchungen aus Reporting.

3) Bedienbare Erfassung und Korrektur: unveränderliche Versionen,
   Änderungsgrund, optimistic lock. Genau ein aktiver Datensatz pro
   Einheit/Art/Monat, keine Summierung desselben Reports aus Import
   und manueller Erfassung. Atomarer idempotenter CSV-Import mit
   Vorschau und bewusster Übernahme als deterministische Schnittstelle,
   synthetische Vorlage/Doku. Korrigierte Quelle nur explizit als neue
   Version. Keine eigene Mailpipeline, externe API oder Secrets.
   Bestehende Auth/Gesellschaftsscope/Rollen/CSRF auf allen Routen und
   Services. Fremde/ausgeschlossene Objekte sperren.

4) Dashboard mit Monatsauswahl: Dauermiet-Soll netto ohne BK/HK/USt
   etc., Kurzzeit-Nettoanteil und Selfstorage-Nettoanteil sowie
   "Nettomieterlös laut Vorschreibung und Monatsabrechnungen". Nie
   Bank-Ist behaupten. Unklare Komponenten/Pauschalaufteilung und
   fehlende Monatsreports als Datenlücken, keine scheinbar vollständige
   Summe. Küchen-/Parkplatzmiete soweit explizite Mietkomponenten
   berücksichtigen. Doppelzählung Dauermiete plus variabler Report
   derselben Einheit/Periode verhindern; Nutzungsstatus und
   Vertragsgültigkeit beachten.

5) Additive Migration, kein Seed. Tests für Mandantentrennung, CSRF/
   Leserechte, Idempotenz/abweichende Importe, Korrekturkonflikt,
   falsche Periode/Status, fehlend vs Null, Centgenauigkeit,
   Mahnsperranzeige und unveränderte OP. Regressionen des bestehenden
   Moduls ausführen. Ein sauberer vollständiger Implementierungsstand
   mit Commit und tatsächlichen offenen Grenzen; kein neues
   Architekturprojekt. Keine echten Namen, Beträge, Quellen oder
   Kontodaten verwenden.

### Quellenbedingte Präzisierung (13.09.2026, während der Umsetzung)

Monatsberichte enthalten häufig zunächst nur Buchungsumsatz brutto und
noch KEIN bestätigtes Eigentümer-Netto. Bitte ENTWURF/UNGEKLAERT mit
optional leerem nettoanteil_cent ermöglichen; berichteter
Originalbetrag plus Betragsart BRUTTO/NETTO/UNGEKLAERT separat,
unbekannt bleibt None. Keine Division durch erfundene USt. Nur
geprüfte/eindeutig netto bestätigte eigene Anteile in die Erlössumme;
Entwürfe sichtbar mit fehlendem Netto-/Abschlussnachweis. Betriebs-,
Reinigungs- und Verwaltungskosten können bereits im ausgewiesenen
Anteil enthalten sein: optionale Kostenfelder rein erläuternd, nicht
nochmals abziehen. Überweisung an Eigentümer kann Kostenersatz
enthalten und ist nicht gleich Nettomieterlös. Bei historischer
Periode keine aus aktuellem Status erfundene Vertrags-/
Nutzungsverteilung. Die schon angelegten sieben Kurzzeit-Einheiten
plus eine Selfstorage-Einheit bleiben der Bestandsumfang; generischer
Code, echte Zuordnung durch Codex.

### Umsetzung/Konkretisierungen (diese Sitzung)

- `status` (ENTWURF/BESTAETIGT) ist von `berichteter_betragsart`
  (BRUTTO/NETTO/UNGEKLAERT) UNABHÄNGIG - ein BESTAETIGT verlangt nur
  einen erfassten `unser_netto_anteil_cent`, unabhängig davon, in
  welcher Betragsart der ursprünglich gemeldete Betrag vorlag.
- Kostenfelder (`betriebskosten_hinweis_cent`/
  `reinigungskosten_hinweis_cent`/`verwaltungskosten_hinweis_cent`)
  sind ausdrücklich in KEINEM Code-Pfad an einer Berechnung beteiligt
  - reine Anzeige-/Dokumentationsfelder.
- Optimistic Lock: `korrigieren` bindet sich an `ausgehend_von_id`
  (die vom Aufrufer zuletzt gesehene aktuelle Version); weicht die
  tatsächlich aktuelle Version zum Zeitpunkt der Korrektur davon ab,
  wird `OptimistischerLockKonfliktError` geworfen statt die
  zwischenzeitliche fremde Korrektur stillschweigend zu überschreiben.
- CSV-Import: `erstelle_plan`/`wende_an` teilen sich dieselbe
  Prüffunktion (`_pruefe_paket`/`_pruefe_einzelzeile`) wie der
  bestehende generische Intake (`intake/planner.py`) - kein separater
  Prüfpfad. Eine Zeile, die von der aktuellen Version abweicht, ohne
  `aenderungsgrund` zu tragen, gilt als KONFLIKT (blockiert den
  gesamten Import); mit `aenderungsgrund` als KORREKTUR (nur nach
  explizitem `korrekturen_bestaetigt=True` anwendbar). Die GESAMTE
  Datei läuft in einer Transaktion.
- Monatsübersicht (`variableabrechnung/dashboard.py`) verwendet für
  das Dauermiete-Soll die VERTRAGSGÜLTIGKEIT (`gueltig_von`/
  `gueltig_bis`), nicht `EinheitTable.nutzungsstatus` (das hat keine
  Historie) - historische Monate werden dadurch nicht anhand des
  heutigen Status verzerrt. Ein Hinweis auf einen fehlenden
  Monatsbericht nutzt dagegen bewusst den AKTUELLEN Nutzungsstatus als
  Heuristik und ist als solche gekennzeichnet, keine rückwirkende
  Tatsachenbehauptung.

### Konkretisierungen aus zwei Runden unabhängiger Abnahme (13.09.2026)

Der Übergabestand wurde zweimal mit konkret reproduzierten Fehlern
zurückgewiesen, bevor er freigegeben wurde (Details/Historie siehe
`OFFENE_PUNKTE.md`, Abschnitt "Paket Dashboard/Variable
Monatsabrechnung"). Die dabei entstandenen fachlichen Konkretisierungen
gelten ab sofort als Teil dieses Auftrags:

- **`VertragsKomponenteTable.betrag_cent` ist NIE automatisch die
  Netto-Mietbasis.** Weder Art (auch HMZ/KUECHE/PARKPLATZ) noch
  `ust_satz_promille` allein belegen, dass ein gespeicherter Betrag
  netto ist - der Bestand enthält nachweislich auch BRUTTO gespeicherte
  Beträge und Pauschalen unter diesen Arten. Eine separate,
  eigenständige Netto-Mietanteil-Freigabe (`komponenten_freigabe.py`)
  mit eigenem Betrag, Quellenbeleg und Gültigkeit ist Pflichtvoraus-
  setzung für jede Dauermiete-Zählung; ihre Erfassung ist Sache von
  Codex (echte Nachweise außerhalb des Repos), nicht dieses generischen
  Codes.
- **Untermonatliche Gültigkeit (Vertrag, Komponente UND Freigabe) zählt
  nie als voller Monatsbetrag**, auch nicht anteilig - nur eine
  Datenlücke. Alle den gewählten Monat überlappenden Komponenten werden
  geprüft (nicht nur die zum Monatsersten aktiven), damit eine erst
  untermonatlich beginnende/endende Komponente sichtbar bleibt.
- **Artenkonflikt-Erkennung**: hat eine Einheit im selben Monat
  bestätigte Berichte für mehr als eine Art (z. B. gleichzeitig
  KURZZEITVERMIETUNG und SELFSTORAGE), werden ALLE betroffenen Berichte
  von der Summe ausgeschlossen statt addiert.
- **Optimistic Lock wird atomar auf Repository-Ebene durchgesetzt**
  (bedingte UPDATE-Anweisung mit `rowcount`-Prüfung), nicht nur durch
  eine vorgelagerte Prüfung - sowohl bei `VariableAbrechnungTable`-
  Korrekturen als auch bei der Invalidierung überlappender
  Netto-Mietanteil-Freigaben. Eine neue Freigabe entwertet dabei
  ausschließlich ZEITLICH ÜBERLAPPENDE frühere Freigaben derselben
  Komponente, nie pauschal alle - eine Überschneidung erfordert
  zusätzlich einen Änderungsgrund.
- **CSV-Plan-Hash bindet sich an den beim Planen gesehenen Zustand**
  (`aktuelle_version_id` je Zeile), nicht nur an den Dateiinhalt;
  `wende_an` prüft unmittelbar vor dem Schreiben mit einer frischen
  Neuprüfung erneut dagegen.
- **Ctx-/Gesellschaftsscope- und Objektausschluss-Prüfung gilt auch für
  Lesepfade**, nicht nur für Schreibaktionen: Listen-/Versions-/
  Vorschau-Routen und -Services filtern bzw. lehnen konsequent ab,
  Objektausschluss wird dabei zusätzlich zum reinen Gesellschaftsscope
  geprüft (ein nachträglich ausgeschlossenes Objekt verschwindet damit
  automatisch aus alten Berichten/Summen).

## Auftrag HV-20260913-RUECKSTAENDE (wortgetreu, 13.09.2026)

Neuer beauftragter Schritt HV-20260913-RUECKSTAENDE, auf bestehendem
Branch claude/bold-volta-7xovjq weiterimplementieren, nur generischer
Code/synthetische Tests, keine Live-Daten/Server/Mails/Buchungen und
`src/invoice_automation` unverändert. Sonnet 5 High, Fast aus reicht.

Nutzerwunsch: Im Dashboard sollen alle Rückstandsübersichten liegen und
je Objekt filterbar sein. Aktuell ist /backoffice/ ohne Objekt leer.
Implementiere eine verständliche zentrale Übersicht: Standard Alle
Objekte (nur erlaubte, nicht ausgeschlossene), gemeinsamer Objektfilter;
sämtliche Summen, Mietkontentabelle, offene Einzelpositionen,
Mahnsperren/Anwalt/Ratenplan und vorhandene Mahnfallstatus müssen auf
exakt denselben gefilterten Bestand reagieren. Übersichtslinks allein
reichen nicht: Beträge und zugehörige Mieter/Objekte direkt sichtbar,
Detailkonto/Mahnvorschau verlinken. Einheiten ohne Mietkonto (Leerstand/
KZV/Selfstorage/Eigennutzung) als Bestand, niemals als erfundener
Nullsaldo/Rückstand.

Kennzahlen klar trennen: Summe positiver Kontostände; Guthaben separat
ohne Verrechnung zwischen Mietern; tatsächlich überfällige offene
Einzelpositionen (bekannte Fälligkeit); noch nicht fällige; Fälligkeit
unbekannt. Zeige offene Positionen nach vorhandenem OP-Service/
Verrechnung, keine neue Buchungs- oder Mahnlogik. Mahnsperren und
bestehende Mahnstufen/Fallstatus anzeigen, aber bekannte Fälligkeit nie
als automatische Mahnfreigabe darstellen. Keine Mahnplanung als
GET-Seiteneffekt. Kein Name-/Saldo-Heurismus für strittig; nur belegte
Sperr-/Fallinformationen. Bestehende Service-Unterschiede Kontosaldo vs
offene Positionen erkennen und als Abweichung sichtbar halten, nicht
glattrechnen.

UX kompakt, deutsch, mobil lesbar, bestehende Gestaltung/Nutzungstypen
erhalten. Standard alle Objekte und Option zum einzelnen Objekt.
Kontoübersicht mit Objektspalte und klarer Sortierung, zusätzliche
Einzelpositionsübersicht mit Zeitraum/Beleg/Fälligkeit/Rest und
passenden Details. Vorhandene Mahnstatus können in passender Spalte/
kleiner Tabelle eingebunden werden. Kein neuer Export oder Fremddienst
nötig.

Zugriffsprüfung serverseitig: Der bisherige Dashboard-Code filtert nicht
sauber nach AuthContext. Gesellschaftsscope auf Objektoptionen, Daten,
Summen und direkt manipulierte objekt_id anwenden; fremde/unbekannte IDs
ohne Offenlegung/500 abweisen. Ausgeschlossene Objekte nicht in
Alle-Summen oder Auswahl, kein stiller Scopewechsel.

Teste aussagekräftig: zwei erlaubte Objekte plus fremde Gesellschaft und
ausgeschlossenes Objekt; positive/negative/Nullsalden ohne
Gegenverrechnung, Teilzahlung/Gutschrift/Storno, unbekannte/künftige
Fälligkeit, historische Verträge mit Rest, leere Objekte und fehlendes
Mietkonto, Mahnsperren/Stufe1/2, identische Filterwirkung und
vollständig schreibfreie GETs. Bitte vollständigen Mietinkasso-Testsatz
einmal am finalen Code ausführen, relevante Doku aktualisieren, Commit
in denselben Branch pushen und genaue Revision/Prüfresultate
zurückmelden. Codex bearbeitet parallel nur unabhängige Prüfungen und
Betriebsskripte außerhalb des Repos. Keine unnötigen Mehrfachtests bei
reinen Dokuänderungen.

### Umsetzung/Konkretisierungen (diese Sitzung)

- Neues, rein lesendes Modul `src/mietinkasso/rueckstaende/service.py`
  (`berechne_rueckstandsuebersicht`) kombiniert AUSSCHLIESSLICH bereits
  bestehende Services (`StammdatenRepository`, `OPService.
  berechne_saldo`/`offene_forderungen`, `MahnFallRepository.
  list_fuer_vertrag`) - keine eigene Saldo-/Verrechnungs-/Mahnlogik,
  niemals `MahnwesenService.plane_forderung`/
  `plane_alle_offenen_forderungen` (die neue `MahnFallTable`-Zeilen
  anlegen würden) - die Übersicht ist ein GET-seiteneffektfreier
  Lesepfad.
- Erlaubter Bestand (Filter-Auswahlliste UND "Alle Objekte"-Summen)
  entsteht aus GENAU EINER Schleife über `ctx.has_zugriff`-geprüfte
  Gesellschaften und deren NICHT ausgeschlossene Objekte - beide Sichten
  sehen dadurch garantiert denselben Bestand.
- Ein explizit angefordertes `objekt_id`, das unbekannt ist, zu keiner
  zugänglichen Gesellschaft gehört, oder ausgeschlossen ist, wird
  EINHEITLICH über denselben Fehlertyp
  (`UnbekanntesObjektFilterError`, eine `ValueError`-Unterklasse)
  abgelehnt - kein stiller Wechsel auf "Alle Objekte", kein
  Erkenntnisgewinn für den Aufrufer, welcher der drei Fälle vorliegt.
- Fünf Kennzahlen bewusst GETRENNT geführt (`RueckstandsKennzahlen`):
  Summe positiver Kontostände, Guthabensumme (nie gegen positive Salden
  verrechnet), überfällige/noch nicht fällige/Fälligkeit-unbekannte
  Summe der EINZELPOSITIONEN (`OPService.offene_forderungen`, FIFO-
  Zuordnung je Forderung) - letztere drei sind ausdrücklich NICHT
  dasselbe wie `OPSaldo.faelliger_unstrittiger_rest_cent` (Konto-Ebene);
  beide Werte werden nebeneinander gezeigt, nie glattgerechnet.
- Einheiten ohne (aktiven) Vertrag/Mietkonto erscheinen in einer
  eigenen Liste (`EinheitOhneKontoZeile`), NIE als Mietkonto-Zeile mit
  Saldo 0.
- Mahnstatus stammt ausschließlich aus bereits gespeicherten
  `MahnFallTable`-Einträgen (reiner Read); Sperrgründe kommen
  unverändert aus `StammdatenRepository.aktive_sperren`. Kein Namens-/
  Saldo-Heurismus für "strittig" - das Datenmodell kennt bewusst kein
  solches Feld.
- Backoffice-Route `/backoffice/` (`dashboard()`) wurde komplett auf
  diese eine Berechnung umgestellt: Objektfilter, Kennzahlen,
  Mietkontentabelle, Einzelpositionsübersicht, Mahnfälle-Übersicht und
  "Einheiten ohne Mietkonto" stammen alle aus DERSELBEN
  `RueckstandsUebersicht` - keine der Ansichten kann dadurch aus dem
  gefilterten Bestand herausfallen.

### Nachbesserung nach Codex-Rückprüfung `abc4530` (13.09.2026)

Kleine gezielte Korrektur, kein Neuentwurf: (1) jede Einzelposition
zeigt jetzt OP-Nr. UND Belegreferenz der zugrunde liegenden OP-Zeile;
(2) eine neue, eigene Mahnfälle-Tabelle zeigt JEDEN gespeicherten
Mahnfall je Forderung (vorher nur der zuletzt angelegte pro Vertrag),
Fallbetrag bleibt informativ und fließt in keine OP-Kennzahl ein; (3)
je Mietkonto-Zeile eine explizite numerische Abweichung zwischen
Kontostand und Summe der Einzelpositionen, plus getrennte "Fällig
(Kontoberechnung)"/"Fällig (Positionen)"-Spalten statt eines pauschalen
Hinweistexts; (4) Anzeige verständlicher (Mieter statt Debitor, keine
FIFO-/Service-/glattgerechnet-Begriffe, "fällig/überfällig" statt
pauschal "überfällig", `faelligkeit_bekannt` statt bloßer Datums-
Truthiness); (5) ein Vertrag/Konto, dessen eigenes `gesellschaft_id`
nicht zum ctx-Zugriff passt, wird jetzt auch unter einem sonst
erlaubten Objekt übersprungen (Dateninkonsistenz-Schutz). Zusätzlich:
Dashboard-Tabellen liegen jetzt in einem horizontal scrollbaren
Container (`.tabelle-scroll`, nur diese Tabellen betroffen), damit die
zusätzlichen Spalten die Seite bei schmaler Anzeige nicht verbreitern.
