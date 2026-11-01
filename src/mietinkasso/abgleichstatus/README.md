# Zahlungs- und Abgleichstand

Auftrag HV-20261005-ABGLEICHSTATUS. Reine Leseansicht, keine Formulare.
Portal: Karte **Zahlungs- und Abgleichstand** auf der Übersicht (nach den
Kennzahlen) und Detailseite `/backoffice/abgleichstatus` (gleicher Login,
gleicher Objektfilter; der Filter wird beim Link mitgenommen).

## Bedeutung

- **Offene Beträge / Guthaben** stammen unverändert aus
  `berechne_rueckstandsuebersicht` (OPService). Keine zweite Saldorechnung.
  Guthaben werden nie mit Rückständen anderer Mieter verrechnet.
- **Abgleichnachweis** (`abgleich_nachweise`): ausdrücklich erfasste Prüfung
  der Mieteingänge je Objekt und Bankkonto für `geprueft_von`–`geprueft_bis`.
  Maßgeblich ist je Objekt/Bankkonto der zuletzt erfasste Nachweis
  (`geprueft_am`, dann `id`). Eine Korrektur darf ein früheres Prüfende
  setzen. Das jüngste Bankbuchungsdatum gilt nie als geprüft.
  Ohne Nachweis: „Noch kein Abgleichnachweis“. Prüfende vor heute: „Stand vom
  …; neuere Eingänge noch prüfen“.
- Ein Abgleichnachweis ist **keine Bankvollständigkeit** und keine
  Mahnfreigabe. `bank_vollstaendigkeit` bleibt unberührt und wird nur
  getrennt daneben angezeigt.
- Bankkonten erscheinen nur über einen Nachweis. Passt das Bankkonto nicht
  zur Gesellschaft des Objekts/Vertrags, wird der Nachweis beim Lesen
  ignoriert und gezählt. Es gibt kein Ersatzkonto aus der Gesellschaft.
  Angezeigt werden Bankbezeichnung und die letzten vier IBAN-Stellen,
  nie `quelle_ref` (kann ein Serverpfad sein), nur ein Präfix der Prüfsumme.
- **Einzug** (`einzug_nachweise`): `EINGEREICHT` heißt „Eingereicht,
  Bankeingang noch offen“. Ein Einzug mindert keinen Saldo, markiert nichts
  als bezahlt und wird nicht automatisch mit Bankzeilen abgeglichen. Ein
  älterer eingereichter Einzug bleibt offen sichtbar, auch wenn ein anderer
  Eingang den Saldo gedeckt hat.
- **Letzte erfasste Zahlung**: Buchungsdatum der jüngsten aktiven
  ZAHLUNG-Buchung. Das ist keine Vollständigkeitsaussage. Ein „bezahlt
  bis“ wird nicht abgeleitet. Gezeigt werden die offenen Zeiträume der
  gebuchten Vorschreibungen. „Keine offenen Posten“ bezieht sich nur auf
  bereits gebuchte Vorschreibungen.

## Datenlebenszyklus

- Die Nachweistabellen sind additiv (`create_all`) und append-only. Bestehende
  Zeilen und Tabellen werden nicht migriert oder verändert.
- Erfassung nur über `AbgleichNachweisRepository.erfasse_abgleichnachweis`
  bzw. `erfasse_einzugnachweis` aus der privaten Quelle (Operator-Intake).
  Vor dem Schreiben wird geprüft: Zugriff, nicht ausgeschlossen, Bankkonto
  derselben Gesellschaft, Pflichtfelder, Zeitzone und positiver Betrag.
  Gleiche Kennung (`import_id` bzw. `referenz`) mit gleichem Inhalt ist ein
  No-op. Abweichender Inhalt wird mit `ImportConflictError` abgelehnt.
- Belegte Statuswechsel werden mit `erfasse_einzugsstatus` als eigene,
  idempotente Ereignisse in `einzug_status_nachweise` erfasst. Der Originaleinzug
  bleibt unverändert; der jüngste Statusnachweis zählt. Dies bucht keine Zahlung
  und verändert keine OP. Nur ein Original-Bankeingang darf unabhängig davon
  über den bestehenden Buchungsdienst zugeordnet werden.
- Jeder GET liest live. Lesen schreibt nichts. Die Daten liegen in der
  bestehenden Datenbank und sind damit Teil der bestehenden Sicherung.

## Kontoabdeckung und Vertrags-Prüfseite

`kontoabdeckung.py`, Seite `/backoffice/vertrag/{id}/kontoabdeckung` (Link in der
Mieterakte, optional `von`/`bis`, Standard technischer Vertragsbeginn bis heute).
Rein lesend: keine Aufgabe, keine Mahnfreigabe, keine Buchung.

- `pruefe_kontoabdeckung` vereinigt `BelegtePeriode`-Einträge genau einer Konto-ID
  (exakter Vergleich) und nennt konkrete Lücken. Überlappende und direkt
  anschließende Perioden werden vereinigt. Nachweis ist nur eine Periode mit
  Beleg-ID, gültigem SHA-256 und `vollstaendig_erfolgreich is True`. Alles andere
  wird mit Grund verworfen. Unbelegt heißt `UNBEKANNT`, nie "keine Zahlung".
- **Es gibt derzeit keine gespeicherte Periodenquelle.** Die Seite übergibt keine
  Perioden und zeigt deshalb „Belegte Perioden fehlen“. `bank_vollstaendigkeit`
  (nur „bis“, ohne Beleg), `abgleich_nachweise` (Objektprüfung ohne Erfolgsangabe),
  letzte Bankbuchung und Dateiname werden nicht in Perioden umgedeutet.
- Saldo, offener Betrag und Guthaben kommen unverändert aus
  `berechne_abgleichstatus`/`OPService`. Getrennt daneben stehen Teilzahlungen,
  Zahlungen mit Bankbezug, nicht zugeordnete Bankzeilen mit exakter
  `VERTRAG:<id>`-Referenz, Rücklastschriften, Kaution, Parteien und weitere Verträge
  derselben Debitor-ID.
- Fremdzahler werden nicht beurteilt: der Zahlername laut Bank wird ungeprüft
  angezeigt, nie mit dem Mieter verglichen. Das Bankkonto stammt aus der
  bestehenden eindeutigen Auflösung; sonst bleibt die Abdeckung unbekannt.
- Hinweisschlüssel sind berechnete Hashes zur Anzeige ohne Doppelte und werden
  nicht gespeichert.

## Mieterantwort-Entwürfe

`antwortentwurf.py`, Seite `/backoffice/vertrag/{id}/antwortentwurf`. Deterministisch,
ohne LLM, ohne Speicherung, kein Maildraft, kein Transport; der Versand bleibt beim
Mail-Verantwortlichen.

- Ohne bestätigten Bezug auf genau diesen Vertrag geht jede Frage in die manuelle
  Prüfung. Namen werden nie ausgewertet. Eine genannte andere Vertragskennung,
  Einheit oder ein anderes Objekt führt ebenfalls in die manuelle Prüfung.
- Jede Zeile, Teilfrage und jeder Fragesatz bleibt erhalten und endet mit Beleg oder
  als offen. Nur vollständige, begrenzte Frageformen erlauben eine automatische Faktenantwort.
  Unbekannte Zusatzklauseln und Adressen bleiben manuell; das ersetzt keine Prüfung.
- Nie beantwortet: Anweisungsversuche, Rechtsfragen, Fristen/Zusagen, Bankdaten und
  Fragen zu weiteren Personen. Unbekanntes bleibt offen, nie 0. Technischer
  `gueltig_von` und Default-`faelligkeit_tag` gelten nicht als Beleg.
- Finanzfragen zeigen nur den gebuchten Stand samt Abdeckungsgrenze und
  ausdrücklich keine Aussage „bezahlt“.


### Geprüfte Grenzen dieser Erweiterung
- Kein neuer Importer: der Bankowner muss verifizierte Perioden an die vorhandene
  `BelegtePeriode`-Schnittstelle liefern. Bis dahin bleibt die sichtbare Abdeckung unbekannt.
- Querverweise filtern Objekt-Ausschluss und Gesellschaft erneut; fehlerhafte
  Bankverknüpfungen geben keine fremden Metadaten aus.
- Mehr als 50 Fragen behalten jeden Originalpunkt als offen; über 20.000 Zeichen
  wird die gesamte Eingabe abgelehnt. Keine gekürzte fertige Antwort.
- Codeprüfung und synthetische Tests sind kein Deployment, keine Aktivierung und
  keine Abnahme echter Vertragsvorlagen oder produktiver Bankperioden.
