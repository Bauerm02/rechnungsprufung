<?php

declare(strict_types=1);

namespace EbicsDownloader\Keyring;

use EbicsApi\Ebics\Models\Keyring;
use EbicsApi\Ebics\Services\FileKeyringManager;
use EbicsDownloader\Exceptions\HardStopException;

/**
 * Lädt einen BEREITS BESTEHENDEN, verschlüsselten Keyring und bricht
 * hart ab, statt automatisch etwas zu reparieren oder anzulegen.
 *
 * Konkreter, in dieser Sitzung verifizierter Befund gegen die reale
 * Bibliothek (ebics-api/ebics-client-php 3.2.1,
 * `Services/FileKeyringManager::loadKeyring()`): bei einer FEHLENDEN
 * oder LEEREN Datei liefert die Bibliothek selbst KEINEN Fehler,
 * sondern einen frisch angelegten, leeren `Keyring` zurück ("Bei nicht
 * existierenden oder leeren Dateien wird ein neuer leerer Keyring
 * erstellt"). Ohne die Prüfungen in dieser Klasse würde ein fehlendes
 * Secret-Mount NICHT laut fehlschlagen, sondern still mit einem
 * nutzlosen leeren Keyring weiterlaufen - genau das verbietet der
 * Auftrag ausdrücklich. Diese Klasse prüft deshalb VOR UND NACH dem
 * Laden explizit:
 *
 * 1. Datei existiert, ist nicht leer, hat genau die Rechte 0600.
 * 2. Nach dem Laden sind ALLE fünf Signatur-Slots (Benutzer A/X/E,
 *    Bank X/E) tatsächlich befüllt - eine frisch erzeugte, leere
 *    Instanz hätte hier durchgehend `null`.
 * 3. Der SHA-256-Fingerprint der geladenen Bank-Schlüssel (Signatur X
 *    und E) entspricht exakt dem in der privaten Profildatei
 *    hinterlegten, beim Onboarding einmalig verifizierten Wert - eine
 *    Abweichung (z. B. eine ausgetauschte Keyring-Datei) stoppt den
 *    Prozess hart, statt dem neuen Schlüssel stillschweigend zu
 *    vertrauen.
 *
 * Diese Klasse importiert AUSSCHLIESSLICH `FileKeyringManager`/
 * `Keyring` - keine INI/HIA/HPB/Reset/SPR/HCS-Order-Klasse ist hier
 * (oder sonst irgendwo im Downloadpfad dieses Pakets) referenziert;
 * `tests/Keyring/OrderTypeAllowlistTest.php` prüft das zusätzlich
 * automatisiert gegen den gesamten Quellbaum.
 */
final class KeyringGuard
{
    public function __construct(private readonly FileKeyringManager $manager)
    {
    }

    /**
     * @param array{signature_x: string, signature_e: string} $expectedFingerprints Hex-SHA-256, aus der privaten Profildatei
     */
    public function loadVerified(string $path, string $passphrase, array $expectedFingerprints): Keyring
    {
        $this->assertFileSafeToLoad($path);
        $keyring = $this->manager->loadKeyring($path, $passphrase);
        $this->assertPopulated($keyring);
        $this->assertFingerprint($keyring, $expectedFingerprints);
        return $keyring;
    }

    private function assertFileSafeToLoad(string $path): void
    {
        if (!is_file($path)) {
            throw new HardStopException(
                "Keyring-Datei fehlt: $path - kein automatisches Anlegen. Bitte den bestehenden, "
                . 'verschlüsselten Keyring aus dem Secret-Mount bereitstellen.'
            );
        }
        clearstatcache(true, $path);
        if (filesize($path) === 0) {
            throw new HardStopException("Keyring-Datei ist leer: $path - kein automatisches Anlegen.");
        }
        $perms = fileperms($path);
        if ($perms === false) {
            throw new HardStopException("Dateirechte von $path konnten nicht gelesen werden.");
        }
        $mode = $perms & 0777;
        if ($mode !== 0600) {
            throw new HardStopException(
                sprintf("Keyring-Datei %s hat unsichere Rechte %o (erwartet exakt 0600).", $path, $mode)
            );
        }
    }

    private function assertPopulated(Keyring $keyring): void
    {
        $pflichtfelder = [
            'getUserSignatureA' => 'Benutzer-Signatur A (Authentifizierung)',
            'getUserSignatureX' => 'Benutzer-Signatur X (Signierung)',
            'getUserSignatureE' => 'Benutzer-Signatur E (Verschlüsselung)',
            'getBankSignatureX' => 'Bank-Signatur X (Signierung)',
            'getBankSignatureE' => 'Bank-Signatur E (Verschlüsselung)',
        ];
        foreach ($pflichtfelder as $getter => $bezeichnung) {
            if ($keyring->{$getter}() === null) {
                throw new HardStopException(
                    "Keyring unvollständig - $bezeichnung fehlt ($getter() ist null). Das deutet auf einen "
                    . 'frisch angelegten leeren Keyring hin (z. B. weil die Datei fehlte/leer war) - kein '
                    . 'automatisches Schlüsselanlegen, harter Stopp statt eines unvollständigen Downloadversuchs.'
                );
            }
        }
    }

    /**
     * @param array{signature_x: string, signature_e: string} $expected
     */
    private function assertFingerprint(Keyring $keyring, array $expected): void
    {
        $bankSignatureX = $keyring->getBankSignatureX();
        $bankSignatureE = $keyring->getBankSignatureE();
        // assertPopulated() lief bereits vorher und garantiert Nicht-Null;
        // die erneute Prüfung hier ist bewusst defensiv (keine Annahme
        // über die Aufrufreihenfolge externer Nutzung dieser Klasse).
        if ($bankSignatureX === null || $bankSignatureE === null) {
            throw new HardStopException('Bank-Signaturschlüssel fehlen - Fingerprint nicht prüfbar.');
        }
        $actualX = hash('sha256', (string) $bankSignatureX->getPublicKey()->getKey());
        $actualE = hash('sha256', (string) $bankSignatureE->getPublicKey()->getKey());

        if (!hash_equals(strtolower($expected['signature_x']), $actualX)) {
            throw new HardStopException(
                "Bank-Fingerprint (Signatur X) stimmt NICHT mit dem in der Profildatei hinterlegten, beim "
                . "Onboarding verifizierten Wert überein (erwartet {$expected['signature_x']}, geladen $actualX) "
                . '- harter Stopp. Das kann eine ausgetauschte/manipulierte Keyring-Datei bedeuten oder eine '
                . 'nicht in der Konfiguration nachgezogene, legitime Schlüsseländerung der Bank; beides erfordert '
                . 'menschliche Klärung, kein automatisches Vertrauen.'
            );
        }
        if (!hash_equals(strtolower($expected['signature_e']), $actualE)) {
            throw new HardStopException(
                "Bank-Fingerprint (Signatur E) stimmt NICHT mit dem in der Profildatei hinterlegten, beim "
                . "Onboarding verifizierten Wert überein (erwartet {$expected['signature_e']}, geladen $actualE) "
                . '- harter Stopp.'
            );
        }
    }
}
