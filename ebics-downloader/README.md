# EBICS-Downloadclient (Paket D1)

Schmaler, isolierter EBICS-Downloadclient für camt.053-Kontoauszüge -
vollständig getrennt von `src/mietinkasso/` (der Mietinkasso-Datenbank)
und von `src/invoice_automation/`. Kein Zahlungsverkehr, keine
Schlüsselerzeugung, keine automatische Bankzuordnung/OP-Verbuchung.
Auftrag `HV-20260912-ECHTBETRIEB`, Abschnitt Paket D (siehe
`../docs/hausverwaltung/RAHMENPROGRAMM.md`).

Dieses Modul liefert **generische, wiederverwendbare Infrastruktur**
(Intake-Engine, Konfigurationsprofile, Deployment-Vorlagen). Es wird von
Claude mit ausschließlich synthetischen Daten gebaut und getestet;
**Codex befüllt es mit echten Bankdaten und betreibt es** auf dem
bestehenden Hetzner-Server. Claude greift zu keinem Zeitpunkt auf einen
Server, eine echte Bank oder ein echtes Geheimnis zu.

## Umfang D1

- Ausschließlich **Konfigurationsprüfung** (`check-config`) und
  **Download bestätigter camt.053-Formate** (`download`).
- Einmaliger CLI-Aufruf, **keine öffentliche HTTP-Schnittstelle**, kein
  Dauerprozess.
- Whitelist auf genau drei Objekte/Konten (601 Am Corso, 616
  Fockygasse, 617 Primelweg) - Objekt 107 ist strukturell und zusätzlich
  hartkodiert immer gesperrt (`ProfileConfig::IMMER_GESPERRTES_OBJEKT`).
- Automatische Zuordnung zu Mietkonten/Zahlungsaufträge sind **nicht**
  Teil dieses Pakets und bleiben es bis zu einer eigenen, separat
  beauftragten und geprüften Import-Freigabe (siehe „Kein Cutover“
  unten).

## Architektur (ein Downloadlauf, in Reihenfolge)

1. **`Cli\CheckConfigCommand` / `Cli\DownloadCommand`** - einziger
   Einstiegspunkt `bin/ebics-downloader`.
2. **`Config\ProfileLoader`** - lädt und validiert EIN Profil = EIN
   EBICS-Teilnehmer/eine Bankverbindung mit eigenem Schlüssel, Archiv,
   Lock, Fingerprint (`config/profiles/*.example.json`).
   `validateStructure()` prüft immer; `assertRunnable()` verweigert
   zusätzlich `enabled=false` und jeden verbliebenen `PLATZHALTER`-Wert.
3. **`Replay\ProcessLock`** - `flock`-Sperre gegen einen zweiten
   parallelen Lauf für dasselbe Profil.
4. **`Keyring\KeyringGuard`** - lädt den BESTEHENDEN, verschlüsselten
   Keyring; bricht hart ab bei fehlender/leerer Datei, falschen
   Dateirechten (nicht exakt 0600), unvollständigem Keyring oder
   abweichendem Bank-Fingerprint (SHA-256 über die geladenen
   Bank-Signaturschlüssel, verglichen mit dem beim Onboarding einmalig
   verifizierten Wert aus der Profildatei). Erzeugt, registriert oder
   ersetzt **niemals** selbst einen Schlüssel.
5. **`Download\EbicsStatementDownloader`** - EIN
   `EbicsClient::executeDownloadOrder()`-Aufruf (BTD/H005 oder
   FDL/H004-Fallback). Die `ackClosure` (aufgerufen von der Bibliothek
   NACH Transportentschlüsselung/-dekompression, aber VOR dem Senden der
   Quittung an die Bank) prüft die absolute Bytegrenze und archiviert
   dann dauerhaft - siehe „Bank-Quittung vs. Geschäftserfolg“ unten.
6. **`Camt\DeliveryProcessor`** (nur bei akzeptiertem Download) - DTD/
   Entity-Vorprüfung, sicheres Entpacken (nur bei
   `container_type=ZIP`), CAMT-Strukturprüfung, Trennung je Statement
   nach Whitelist-IBAN.
7. **`Replay\StatementLedger`** - Replay-/Konflikterkennung je
   (IBAN, Auszugsnummer) über den kanonischen Inhaltshash.
8. Freigabe (`release_dir`) NUR für neue, whitelisted, konfliktfreie
   Statements; alles andere landet in `quarantine_dir`.
   `Replay\DateWindowStore` schreibt die Watermark NUR nach
   vollständig erfolgreicher Verarbeitung fort.

## Bank-Quittung vs. Geschäftserfolg (zentrale Designentscheidung)

Eine POSITIVE EBICS-Quittung bestätigt ausschließlich: die Rohbytes
wurden vollständig empfangen und dauerhaft archiviert. Sie sagt NICHTS
über die geschäftliche Verwertbarkeit aus (fremde Konten, beschädigte
Struktur) - diese Prüfung läuft immer ERST NACHHER
(`Camt\DeliveryProcessor`), nachdem die Bank bereits weiß, dass die
Lieferung angekommen ist. Eine später als quarantäne-pflichtig
erkannte Lieferung kann diese Quittung nicht mehr zurücknehmen, wird
aber niemals als Mietimport-„Erfolg“ markiert oder nach `release_dir`
durchgereicht.

## Betriebsgrenzen (ehrlich, für Codex/Markus vor jeder Live-Anbindung)

- **Kein echter Bankkontakt in dieser Sitzung.** Alle Tests sind
  synthetisch (`tests/`); eine bestandene Testsuite ist **keine
  Live-Bank-Abnahme**. Der erste echte Kontakt gehört Codex/Markus.
- **Docker-Image ungetestet.** Kein Docker-Daemon in dieser
  Entwicklungssitzung verfügbar - `docker/Dockerfile` muss vor dem
  produktiven Einsatz tatsächlich gebaut und der Containerstart
  gegen das Zielsystem verifiziert werden.
- **Host-PHP dieser Sitzung ist 8.4, nicht 8.5.** Die Bibliothek
  `ebics-api/ebics-client-php` 3.2.1 verlangt `php: "^8.5"`.
  `composer.json` pinnt `config.platform.php: 8.5.0`, und `composer
  install`/-`update` liefen mit `--ignore-platform-reqs`, weil dieser
  Sandbox-Host tatsächlich 8.4.19 ohne `ext-bcmath` ist. Das reale
  Zielsystem (Docker-Image `php:8.5-cli`, siehe `docker/Dockerfile`)
  hat echtes PHP 8.5 mit allen benötigten Erweiterungen - dieser
  Workaround ist ausschließlich ein Sandbox-Artefakt dieser
  Entwicklungssitzung.
- **Gefundene Packagist/GitHub-Diskrepanz bei Version 3.2.1.** Die von
  Packagist für Version `3.2.1` zurückgelieferte Commit-Referenz
  (`11c2caf264a785c46f6d9de56a11e3cc377af60f`) existiert im
  tatsächlichen Git-Verlauf des Repositories nicht mehr (Composer:
  „is gone (history was rewritten?)“). Unabhängig verifiziert: der
  aktuelle Git-Tag `v3.2.1` zeigt jetzt auf einen ANDEREN Commit
  (`c0cd3d448fa01ea0442e72b2720be0d371070c12`) - gleicher Autor
  (andrew-svirin), identische Commit-Message („Add verification for
  signature“), identischer `composer.json`-Versionsstring `3.2.1`,
  identischer `php: "^8.5"`, identische `ackClosure`-vor-
  `transferReceipt`-Sequenzierung in `EbicsClient.php`. Das ist ein
  echtes Upstream-History-Rewrite (der Tag wurde nachträglich
  umgesetzt), keine Fehlkonfiguration dieser Sitzung. Fix:
  `composer.json` enthält einen expliziten `"repositories"`-Eintrag
  (`type: vcs`, direkte GitHub-URL), der die aktuellen Git-Tags LIVE
  liest statt sich auf die veraltete Packagist-Referenz zu verlassen;
  `composer.lock` ist exakt auf `c0cd3d448f...` gelockt. Vor jedem
  produktiven `composer install` erneut prüfen, ob Packagist
  zwischenzeitlich nachgezogen hat.
- **Restrisiko Dekompressionsbombe VOR der eigenen Prüfung.** Die
  bibliothekseigene zlib-Dekompression (`EbicsClient::downloadTransaction()`)
  läuft immer VOLLSTÄNDIG, BEVOR unser `ackClosure`-Hook (der einzige
  verfügbare Kontrollpunkt) überhaupt aufgerufen wird. Ein absichtlich
  klein komprimierter, aber riesig entpackender Payload kann daher
  bereits vor der eigenen Bytegrenzenprüfung einen Speicherspitzenwert
  verursachen. `Http\SizeCappedCurlHttpClientFactory` grenzt die
  KOMPRIMIERTE Übertragungsgröße per `CURLOPT_XFERINFOFUNCTION` bereits
  während des HTTP-Downloads ein; ein vollständiger Schutz vor einer
  Dekompressionsbombe bei ansonsten unter diesem Transportlimit
  liegender Größe ist mit der öffentlichen API dieser Bibliotheksversion
  nicht erreichbar.
- **ZIP-Zentralverzeichnis kann lügen.** `Camt\SafeZipReader` prüft
  Anzahl/Größe jedes Eintrags über `ZipArchive::statIndex()`, BEVOR
  irgendetwas entpackt wird - das ist die im Zentralverzeichnis
  DEKLARIERTE Größe. `ext-zip` bietet keine streamende Inflate-API mit
  hartem Byte-Cap während der Dekompression selbst; eine gezielt
  fehlerhaft konstruierte Archivdatei mit abweichender tatsächlicher
  Größe wird zusätzlich NACH dem Entpacken nachgeprüft (Verteidigung in
  der Tiefe), aber ein exotischer Central-Directory-Manipulationsangriff
  ist damit nicht zu 100 % ausgeschlossen.
- **Auszugsnummer optional im ISO-20022-Schema.** Fehlt einem Statement
  `Stmt/Id`, schlüsselt `Replay\StatementLedger` stattdessen über den
  Inhaltshash (`iban|NOID|<hash>`) - eine exakt identische erneute
  Lieferung wird weiterhin als Replay erkannt, zwei inhaltlich
  verschiedene Statements ohne Auszugsnummer für dasselbe Konto aber
  NICHT als Konflikt gegeneinander (sie erhalten unterschiedliche
  Schlüssel statt fälschlich gleichgesetzt zu werden - sicher, aber kein
  vollständiger Ersatz für eine fehlende Auszugsnummer).
- **Keine automatische Bankabholung/-zuordnung, kein Cutover.** Dieses
  Paket liefert ausschließlich das Übergabeverzeichnis
  (`release_dir`); ein automatischer Import der dort abgelegten
  Statements in `src/mietinkasso/` ist NICHT Teil dieses Auftrags. Die
  bestehenden 40 synthetischen CSV-Bewegungen im Mietinkasso-Demo-Seed
  bleiben unberührt; ein Cutover auf EBICS als Quelle erfordert einen
  eigenen, separat beauftragten und geprüften Schritt.
- **`max_retries` ist aktuell reine Konfiguration ohne Wiederholungslogik**
  in `Download\EbicsStatementDownloader` - ein einzelner Downloadversuch
  pro CLI-Aufruf; automatische Wiederholungen (mit korrektem
  Zurücksetzen von Transaktionszustand) sind nicht Teil von D1.

## Onboarding (Kurzfassung - Details in `docs/ONBOARDING_EBICS.md`)

**Vorrang hat immer die sichere Übernahme eines bereits bestehenden
Teilnehmers** (Volksbank-Unterlagen liegen laut Markus bereits vor,
Raiffeisen Wien wird von ihm selbst nachgereicht) - eine
Neuanlage/Reinitialisierung ist nur ein gezielter, von einem Menschen
nach Klärung der bisherigen Nutzung getroffener Ausweichweg, niemals
ein automatischer Standardpfad. Dieses Paket führt kein tatsächliches
Onboarding durch.

## Tests

```
composer install
vendor/bin/phpunit
```

80 synthetische Tests (keine echten Bankdaten, keine echten
Kontonummern/IBANs, keine echten Schlüssel) decken ab: die
Order-Type-Allowlist (kein INI/HIA/HPB/SPR/HCS-Klassenzugriff im
`src/`-Baum), Konfigurationsvalidierung, Keyring-Guard-Szenarien
(fehlend/leer/falsche Rechte/unvollständig/Fingerprint-Abweichung),
CAMT-Sicherheitsschicht (DTD/Entity, ZIP-Bombe/Pfadtraversal/Symlink,
IBAN-Trennung, dokumentweite Ntry-Verschachtelung), Replay/Konflikt,
Prozesssperre und die Download-Fehlerpfade vor jedem Netzwerkkontakt.

## Nicht Teil dieses Auftrags

Mieter-Mail, Zahlungen, ein weiteres Modell/API, tatsächliches
Deployment, tatsächlicher Bankzugriff. Alle produktiven Schalter
(`enabled`) bleiben auf `false`, bis ein Mensch sie bewusst umlegt.
