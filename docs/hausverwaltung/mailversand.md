# Hausverwaltungsversand

Portal und täglicher Worker nutzen denselben privaten MailOps-Client. Der
MailOps-Dienst ergänzt die originale JLB-HTML-Signatur mit eingebettetem Logo.
Das Mietprogramm erhält keinen Microsoft-App-Schlüssel und sendet keine
Bankdaten oder Vertragsdateien an einen öffentlichen Transportendpunkt.

## Aktivierung

Die folgenden Einstellungen sind getrennt und standardmäßig ausgeschaltet:

- `MIETINKASSO_HV_MAIL_ALLOWLIST_BESTAETIGT`: tatsächlich erteilte, auf das
  Hausverwaltungspostfach begrenzte Exchange-Berechtigung.
- `MIETINKASSO_SEND_ENABLED`: Mahnläufe, genau zwei freigegebene Stufen.
- `MIETINKASSO_INDEXAUTOMATIK_SEND_ENABLED`: qualifizierte Indexschreiben.
- `MIETINKASSO_VERTRAGSENDE_ERINNERUNG_SEND_ENABLED`: interne Erinnerung an
  die konfigurierte Eigentümeradresse, drei Kalendermonate vor Mietende.
- `MIETINKASSO_INDEXAUTOMATIK_SOLL_UMSETZUNG_ENABLED`: wirksame, belegte
  Indexänderungen auf Vorschreibungskomponenten übernehmen.

Der private Dienst muss die jeweilige Nachrichtenart ebenfalls zulassen.
Socket und Token werden ausschließlich lokal schreibgeschützt eingebunden:
`MIETINKASSO_HV_MAIL_SOCKET_PATH` und `MIETINKASSO_HV_MAIL_TOKEN_FILE`.
Tokenwerte gehören weder in Git noch in Protokolle oder Cloud-Chats.

## Versandnachweis und Wiederanlauf

`ANGENOMMEN` ist kein belegter Versand. Erst eine gebundene MailOps-Referenz,
Microsoft-Nachrichtenreferenz und tatsächliche Versandzeit führen zum Status
`GESENDET` bzw. `BENACHRICHTIGT`. Fachstatus und Nachweis werden atomar
gespeichert. Die Nachweise sind unter `/backoffice/mailversand` sichtbar.

Bei Timeout, Absturz oder fehlendem Nachweis bleibt der Vorgang unklar. Der
tägliche Worker und der Portalbutton fragen dann ausschließlich den Status
ab; sie versenden die Nachricht nicht erneut. Die zweite Mahnstufe rechnet
ab dem belegten Versanddatum der ersten Stufe nach Wiener Kalenderdatum.
Forderungsstand, Sperren und bestätigte Bankvollständigkeit werden unmittelbar
vor jeder Mahnung erneut aus gespeicherten Fachdaten geprüft.

## Index und Zugang

Ein Versandnachweis ist kein rechtlicher Zugangsnachweis. Ein wirksamer
Vorschreibungswechsel verlangt zusätzlich den belegten Zugang und die
vertraglich/gesetzlich zulässige Frist. Änderungen an Empfänger, Vertrag,
Komponenten oder freigegebenem Rechtsprofil sperren veraltete Schreiben.
Ungeklärte MRG- oder Förderregeln werden nicht als Ausnahme behandelt.

Noch ungebuchte Vorschreibungsentwürfe werden bei der Umsetzung atomar
aktualisiert. Bereits gebuchte Perioden sperren die automatische Umsetzung.
Historische Simulationen erzeugen keine neuen Bestandsforderungen.

Die technische Abnahme verwendet synthetische Daten und simulierte
Providerantworten. Echte Nachrichten, Buchungen, Bankaktionen und
Automationsläufe sind keine Funktionstests.
