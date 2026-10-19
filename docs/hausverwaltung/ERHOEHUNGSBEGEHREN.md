# Erhöhungsbegehren – geprüfte Vorlage, Stand 30.09.2026

Owner Codex, Vorgang HV-20260930-ERHOEHUNGSBEGEHREN. Vorlage ist keine pauschale Freigabe einzelner Verträge und keine anwaltliche Bescheinigung.

## Primärquellen und Folgerungen

- MieWeG § 1, BGBl I 114/2025: https://ris.bka.gv.at/eli/bgbl/i/2025/114/P1/NOR40274266
  Wohnungen im Voll- und Teilanwendungsbereich, grundsätzlich jährliche April-Anpassung; Jahresdurchschnitte VPI2020, über 3% nur halber Mehrbetrag. Bei mietzinsbeschränkten Wohnungen Sonderdeckel für die Veränderung 2025/2026 von 1%/2%. Exakter halber Cent ist abzurunden. Die gesetzliche Grenze ersetzt keine wirksame vertragliche Wertsicherungsvereinbarung. Geschäftsräume fallen nicht allein wegen eines MRG-Vertrags unter diese Wohnungsregel.
- MieWeG §4: https://www.ris.bka.gv.at/Dokument.wxe?Abfrage=Bundesnormen&Dokumentnummer=NOR40274269
  §1 erfasst auch Erhöhungen ab 2026 aus älteren Verträgen. Bei bereits valorisierten Altverträgen ist der Bezugsmonat des zuletzt verwendeten Indexwertes zu berücksichtigen. Vertraglicher Mietbeginn und gesetzlicher Vertragsabschluss-/Übergangsbezug sind getrennte Angaben; keine globale Umstellung auf ein Datum.
- MRG §16 Abs9: https://ris.bka.gv.at/eli/bgbl/1981/520/P16/NOR40273694
  Erklärung nach Wirksamwerden der Indexänderung; Bekanntgabe spätestens 14 Tage vor folgendem Zinstermin. Keine pauschale Rückwirkung. Die Regel kann auch für Geschäftsräume im Vollanwendungsbereich gelten. Bei Teilanwendung wird sie nicht pauschal unterstellt; dafür braucht der Automatikpfad ein belegtes Fristenprofil. Längere Vertragsfristen bleiben maßgeblich.
- OGH 5Ob77/98d (26.05.1998), Vergleich mit damaligem §16 Abs6 / heutigem Abs9: https://www.ris.bka.gv.at/Dokumente/Justiz/JJT_19980526_OGH0002_0050OB00077_98D0000_000/JJT_19980526_OGH0002_0050OB00077_98D0000_000.pdf
  Keine allgemeine Pflicht zur eigenhändigen Unterschrift für eine bloße Bekanntgabe ableiten. Daraus folgt keine automatische Zugangsfiktion für normale E-Mails und keine allgemeine Aussage zur Wirksamkeit anderer Vertragserklärungen. Vorhandene belegte Zustellformen bleiben erhalten.

## Umsetzung und Grenzen

`begehren.py` rendert aus bereits berechneten Werten. Vermieter und JLB als Verwaltung werden auseinandergehalten. Vertragsklausel, Indexweg, Einzelbeträge netto/USt/brutto und gesamter Monatsbetrag einschließlich unveränderter Positionen erscheinen im Text. Keine neuen Indexierungen von BK oder Kaution durch die Vorlage. Interne Prüfhinweise sperren die Outbox, statt im versandfähigen Text aufzutauchen.

Ein vorgeschlagener Zinstermin ist an das ausgewiesene späteste Zugangsdatum gebunden. Verspäteter nachgewiesener Zugang verschiebt den Zahlungsbeginn; niemals vor den im Schreiben genannten Termin. Abgelaufene Entwürfe müssen erneuert werden. Version und Texthash schützen den Versandweg. Alte unsendbare Vorlagen werden nicht stillschweigend freigegeben; Quellenprüfung bleibt erforderlich. Gesendete/beanspruchte Schreiben dürfen nicht über die Erneuerungsaktion geändert werden.

Geschützte Druckansicht im Backoffice in JLB-Farben Anthrazit/Gold/Creme, Forum/EB-Garamond mit Cambria/Constantia-Fallback. Nur konfiguriertes freigegebenes PNG/JPEG-Logo. Originale HTML-/CID-Signatur und fester Absender hausverwaltung@jlb-immo.at bleiben im vorhandenen MailOps-Dienst; keine neue Mailintegration und kein Testversand. Papier-/Mailansicht mit Entwurfsstatus ist keine Zustellurkunde.

Einzelvertragsfragen (wirksame Klausel, tatsächliche Nutzungsart, MRG-Regime, Quellenbindung, zuletzt berücksichtigter Index, gegebenenfalls Mietzinsobergrenze) werden weiter über die versionierten Rechtsprofile freigegeben. Eine fehlende oder widersprüchliche Vertragsgrundlage wird nicht durch diese allgemeine Vorlage ersetzt. Insbesondere keine neue Rechtsfreigabe für den offenen Büro-Vertragsfall und keine angenommene Wertsicherung bei mündlichem Gewerbevertrag.

Keine Mieter-Nachrichten, Solländerungen oder Buchungen werden bei der Abnahme ausgelöst. Betriebsflags bleiben unverändert. Die Berechnungsengine wird in diesem Vorlagenauftrag nicht durch neue Rechtsannahmen ersetzt.
