# EBICS-Onboarding für einen neuen technischen Leseteilnehmer

**Für Codex/Markus - kein Schritt hier wird von Claude ausgeführt.**
Claude hat keinen Bankzugriff, keine privaten Schlüssel und führt kein
tatsächliches Onboarding durch. Dieses Dokument beschreibt den
Prozess, den ein Mensch (Codex im kontrollierten Serverbetrieb, oder
Markus persönlich) durchläuft.

## Reihenfolge: erst Übernahme, dann - nur falls nötig - Neuanlage

Nach Markus' Auskunft gibt es bereits **zwei EBICS-Teilnehmer**
(gemeint: zwei technische Teilnehmer/Bankverbindungen, nicht zwei
Konten): Volksbank (Unterlagen liegen vor) und Raiffeisen Wien
(Unterlagen inzwischen gefunden, technische Teilnehmer-Einstufung/
Aktivierung bei der Bank aber noch offen). Der Auftrag ist deshalb
ausdrücklich: **bestehende, bereits erstellte und verschlüsselte
Schlüssel sicher übernehmen**, bevor irgendetwas neu angelegt wird.

1. **Prüfen, ob ein verschlüsselter Keyring bereits existiert** (aus
   einer bisherigen Nutzung, z. B. George Business, oder einem
   früheren technischen Setup). Falls ja: NICHT neu initialisieren.
   Stattdessen die bestehende, verschlüsselte Keyring-Datei plus ihre
   Passphrase sicher (z. B. über einen bereits etablierten
   Secret-Kanal, niemals per Klartext-E-Mail) auf den Zielserver
   übertragen und dort mit exakt `chmod 0600` und dem Besitzer des
   Downloadprozesses ablegen.
2. **Fingerprint-Verifikation, auch bei übernommenem Keyring.** Vor der
   ersten produktiven Nutzung den SHA-256-Fingerprint der geladenen
   Bank-Signaturschlüssel (Bank-Signatur X und E) gegen einen
   UNABHÄNGIG von der Bank bestätigten Wert prüfen (Telefon/Portal/
   INI-Brief-Gegenstelle - nicht nur gegen das, was die Datei selbst
   behauptet). Erst danach den Wert in die private Profildatei
   (`bank_fingerprint_sha256`) eintragen. `Keyring\KeyringGuard` bricht
   danach bei jeder späteren Abweichung hart ab.
3. **Nur falls kein bestehender Keyring übernehmbar ist** (z. B. weil
   die bisherige Nutzung ungeklärt ist oder ein komplett neuer
   technischer Teilnehmer bei der Bank beantragt wird): gezielte
   Neuinitialisierung nach Klärung mit der Bank. Dieser Schritt läuft
   NICHT über dieses Paket (`ebics-downloader` referenziert an keiner
   Stelle INI/HIA/HPB/SPR/HCS, siehe `README.md` und
   `tests/Ordertype/OrderTypeAllowlistTest.php`) - er erfordert ein
   separates, bewusst dafür vorgesehenes Werkzeug/Verfahren
   (üblicherweise: `ebics-api/ebics-client-php`s eigene INI/HIA-Order-
   Klassen in einem eigenständigen, einmaligen Skript, INI-Brief-Druck,
   Aktivierung im Bank-Portal, HPB-Abholung des Bankschlüssels, danach
   erst produktiver Betrieb).
4. **Ein Profil pro Teilnehmer.** Volksbank und Raiffeisen Wien
   erhalten JEWEILS eine eigene Profildatei
   (`config/profiles/volksbank.json`, `config/profiles/raiffeisen-wien.json`,
   niemals im Repository, nur auf dem Server) mit eigenem Keyring-Pfad,
   eigenem Archiv-/Quarantäne-/Freigabeverzeichnis, eigener Sperrdatei
   und eigenem Ledger - siehe `config/profiles/*.example.json` für die
   generische Struktur.
5. **Erst nach all dem:** `enabled: true` setzen und
   `bin/ebics-downloader check-config <profil>` gegen die echte,
   befüllte Datei laufen lassen (rein strukturell, kein Bankkontakt),
   bevor ein tatsächlicher `download`-Lauf versucht wird.

## Was dieses Paket NICHT tut

- Keine automatische Schlüsselerzeugung, keine automatische
  INI/HIA/HPB/Reset/SPR/HCS-Ausführung im Downloadpfad.
- Keine Vermutung, dass ein Onboarding "schon erledigt" ist, nur weil
  eine Datei irgendwo existiert - `KeyringGuard` verlangt exakte
  Dateirechte (0600), einen vollständig befüllten Keyring (alle fünf
  Signatur-Slots) und einen bestätigten Fingerprint, sonst harter
  Abbruch.
- Kein Ausweichen auf einen Passwort-Standardwert oder eine
  automatisch generierte Passphrase.
