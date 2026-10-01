# Monatliche Eigentümer-Betriebskosten

Auftrag HV-20261001-BK-AUTOMATIK. Ein eigener Bereich im bestehenden Programm,
keine zweite Mieterbuchhaltung. Portal: **Abrechnungen → Betriebskosten zahlen**.

Der bestehende tägliche Fristendienst prüft den aktuellen Monat. Er erzeugt
je Konto/Ausführungstag eine unveränderliche SEPA-Überweisungsdatei
`pain.001.001.03` für noch nicht reservierte, bestätigte Positionen. Keine KI,
kein zweiter Timer, kein Bankversand, keine Signatur und keine bezahlte
API. Erster regulärer Lauf im Monat erzeugt die Dateien; spätere Läufe können
vor Fälligkeit geklärte Positionen ergänzen. Unique(Monat, Vorschreibungskennung)
und BEGIN IMMEDIATE verhindern konkurrierende Doppeldateien.

Eigentümervorschreibung enthält auch Rücklage/Darlehen. Das ist ausdrücklich
keine Aussage über die Umlagefähigkeit auf Mieter. Jahresnachverrechnungen,
Altmahnungen, unklare Guthaben und Tilgungen werden nicht wiederholt.

## Voraussetzungen und Grenzen

- Versionierte Vorschreibung mit belegtem Betrag, Gültigkeitsbeginn/-ende,
  Einheit, Gesellschaftskonto, Empfängerkonto und eindeutiger Referenz.
- Neue Vorschreibung einmal anhand des Originalbelegs bestätigen. Im Portal
  Betrag/Zeitraum ändern und PDF hochladen; Hash und geschützte Ablage erfolgen
  intern. Keine KI-Auslegung unstrukturierter E-Mails oder Kaufverträge.
- Bestätigte Kostenenden schließen Folgemonate aus. Übergangsmonate und
  ungeklärte Übergaben werden zurückgestellt, nicht pauschal anteilig bezahlt.
- Ablaufende Vorschreibungen laufen nicht ungeprüft ins nächste Jahr weiter.
  Eine im Voraus erfasste neue Fassung ersetzt die alte erst ab Gültigkeitsbeginn.
- Eigene Konten müssen derselben Gesellschaft wie das zugelassene Objekt
  gehören. Objekt107 ist ausgeschlossen. Kontowechsel bleibt belegte Migration.
- Eindeutig bezeichnete native Bankbelastungen mit Empfänger-IBAN,
  Einzelreferenz und Leistungsmonat reduzieren den Dateibetrag. Mehrdeutige
  Belastungen bzw. Rückbuchungen werden zurückgestellt. Mieteinnahmen werden
  nicht verrechnet. Fehlende Bankimporte sind kein Zahlungsnachweis.
- EBICS noch nicht verfügbar: aktuelle Bankdaten und Deckung vor Signatur
  prüfen. Das Programm meldet nie einen Export als ausgeführte Überweisung.
- Bereits erzeugte Dateien bleiben unverändert. Änderungen, spätere Zahlungen
  oder neue Sperren blockieren deren Download; eine Bankdatei, die bereits bei
  George liegt, muss dort ausdrücklich geprüft/storniert werden. Kein blinder
  automatischer Ersatz- oder Differenzauftrag.
- Originaldateien mit dokumentiertem George-Import können über den streng
  validierten Operator-Intake übernommen werden. Sie reservieren dieselben
  Monatspositionen und werden nicht als neue Download-Datei angeboten.
- Überfällige noch nicht exportierte Positionen bleiben zur Terminprüfung
  zurückgestellt. Keine stillschweigende Verschiebung des Ausführungstags.

Ausführung am Vorschreibungstag bzw. dem nächsten TARGET-Betriebstag.
Kalendergrundlage: [EZB TARGET-Schließtage](https://www.ecb.europa.eu/ecb/contacts/working-hours/html/index.en.html),
geprüft 01.10.2026. Bankeigene Einreichfristen sind zusätzlich bei George zu
beachten; Dateiherstellung allein garantiert keinen fristgerechten Eingang.

## Betrieb / Wiederanlauf

Keine neue Automation installieren. Das vorhandene
`mietinkasso-fristen-tag.timer` startet das bestehende Python-Skript. Der
Eigentümerlauf hat eigene Tabellen und funktioniert unabhängig vom Index-
Tagesjob-Lock und Mailtransport. Fehler lassen den Dienst fehlschlagen;
die nächste Anzeige weist den tatsächlichen letzten Prüfzeitpunkt aus.
Nach Ausfall wird nur der aktuelle Monat geprüft, keine unbekannten Altmonate
nachgeholt. Backup umfasst dieselbe SQLite-DB und Belege im Runtime-Ordner.

Tabellen: eigentuemer_vorschriften, eigentuemer_zahlungsdateien,
eigentuemer_zahlungspositionen, eigentuemer_zahlungslaeufe. Additiv.
Keine Änderung von OP-, Miet-, Bank- oder Kautionstabellen durch diesen Dienst.
