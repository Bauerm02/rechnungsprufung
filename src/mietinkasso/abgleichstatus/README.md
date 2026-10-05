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
