# Monatliche Indexprüfung und Vorschreibungswechsel

Unter Vertragsprüfung → Rechtsprofil wird jeder Vertrag einzeln vorbereitet.
Über „Vertragliche Indexklausel erfassen oder prüfen“ werden VPI-Reihe,
tatsächlich verwendete Basis, Schwelle, Kalenderregel und die betroffenen
Mietbestandteile aus dem Vertrag erfasst. Eine neue Version pausiert die
bisherige Regel. Ungeklärte Angaben sind keine bestätigte Ausnahme.

Die Bestätigung einer Vertragsklausel ersetzt das Rechtsprofil nicht. Dieses
enthält Nutzung, Haupt-/Untermiete, MRG-Zinsbeschränkung, Förderung und
gegebenenfalls eine belegte Mietzinsobergrenze. Bei Wohnungen müssen sowohl
die Vertragsgrenze als auch die gesetzlichen Grenzen eingehalten werden.

Ein alter VPI-Bezugsmonat ist vom Datum der letzten Vorschreibung und vom
technischen Importdatum zu unterscheiden. Für erst später importierte
Mietbestandteile kann ein historischer Basisbeleg hinterlegt werden: echter
Belegbetrag, echtes Belegdatum und Fundstelle. Vor Freigabe muss der Betrag
zur verwendeten Rechenbasis passen. Die ursprüngliche Zeile wird nicht
rückdatiert. Küche oder eine ausdrücklich wertgesicherte Gewerbepauschale
werden nur im belegten Umfang erfasst; separate BK-/Heizkostenvorauszahlungen
werden nicht indexiert.

Die Monatsprüfung unterscheidet feste Kalendermonate, reine Schwellenklauseln
und Mindestintervalle. Indexwert und gerundete obere/untere Schwellen-Grenzwerte
sind getrennte Regeln. Eine zusätzliche Wartefrist benötigt ein belegtes
Indexereignis; das eigene Abrufdatum ist kein Veröffentlichungsdatum.

Ein bereites Schreiben wird vor dem Versand erneut gegen aktuelle
Vertragsdaten geprüft. Unter „Mailversand und Nachweise“ steht der tatsächliche
Versandstatus. Ungewisse Antworten werden abgefragt, nicht erneut gesendet.
Unter „Indexautomatik-Outbox“ wird der belegte Zugang mit Datum erfasst.
Im MRG-Vollanwendungsbereich kann eine Vertragsfrist die gesetzliche
Mindestfrist nicht verkürzen.

Nach Eintritt der belegten Zahlungspflicht übernimmt die freigeschaltete
Sollumsetzung die neue Miete in datierte Komponenten und noch ungebuchte
Vorschreibungsentwürfe. Gebuchte Perioden und historische Forderungen werden
nicht überschrieben. Die Indexbasis wird für die nächste Anpassung
fortgeschrieben, damit dieselbe Erhöhung nicht nochmals berechnet wird.
Ein rechnerischer Senkungsbedarf wird gesondert zur Prüfung angezeigt.

Die Timer arbeiten ohne KI. Mailversand und Sollumsetzung benötigen zusätzlich
die in [mailversand.md](mailversand.md) beschriebenen Betriebseinstellungen.
Der tatsächliche Produktionsstand und fallbezogene Nachweise stehen im
führenden Fachprojekt; dieser Text allein belegt keine erfolgte Bereitstellung.
