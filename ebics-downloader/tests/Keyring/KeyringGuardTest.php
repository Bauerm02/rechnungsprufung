<?php

declare(strict_types=1);

namespace EbicsDownloader\Tests\Keyring;

use EbicsApi\Ebics\Services\FileKeyringManager;
use EbicsDownloader\Exceptions\HardStopException;
use EbicsDownloader\Keyring\KeyringGuard;
use PHPUnit\Framework\TestCase;

final class KeyringGuardTest extends TestCase
{
    private string $tmpVerzeichnis;
    private KeyringGuard $guard;

    protected function setUp(): void
    {
        $this->tmpVerzeichnis = sys_get_temp_dir() . '/ebics-keyring-test-' . bin2hex(random_bytes(6));
        mkdir($this->tmpVerzeichnis, 0700, true);
        $this->guard = new KeyringGuard(new FileKeyringManager());
    }

    protected function tearDown(): void
    {
        $this->rekursivLoeschen($this->tmpVerzeichnis);
    }

    private function rekursivLoeschen(string $pfad): void
    {
        if (!is_dir($pfad)) {
            return;
        }
        foreach (scandir($pfad) ?: [] as $eintrag) {
            if ($eintrag === '.' || $eintrag === '..') {
                continue;
            }
            $ziel = $pfad . '/' . $eintrag;
            is_dir($ziel) ? $this->rekursivLoeschen($ziel) : unlink($ziel);
        }
        rmdir($pfad);
    }

    private function erwarteteFingerprints(): array
    {
        return [
            'signature_x' => KeyringFixture::bankXFingerprint(),
            'signature_e' => KeyringFixture::bankEFingerprint(),
        ];
    }

    public function testFehlendeDateiBrichtHartAb(): void
    {
        $this->expectException(HardStopException::class);
        $this->expectExceptionMessageMatches('/fehlt/');
        $this->guard->loadVerified($this->tmpVerzeichnis . '/nicht-vorhanden.json', 'pw', $this->erwarteteFingerprints());
    }

    public function testLeereDateiBrichtHartAb(): void
    {
        $pfad = $this->tmpVerzeichnis . '/leer.json';
        touch($pfad);
        chmod($pfad, 0600);

        $this->expectException(HardStopException::class);
        $this->expectExceptionMessageMatches('/leer/');
        $this->guard->loadVerified($pfad, 'pw', $this->erwarteteFingerprints());
    }

    public function testFalscheDateirechteBrechenHartAb(): void
    {
        $pfad = $this->tmpVerzeichnis . '/unsicher.json';
        KeyringFixture::schreibeDatei($pfad, KeyringFixture::vollstaendig(), 0644);

        $this->expectException(HardStopException::class);
        $this->expectExceptionMessageMatches('/Rechte/');
        $this->guard->loadVerified($pfad, 'pw', $this->erwarteteFingerprints());
    }

    public function testUnvollstaendigerKeyringBrichtHartAb(): void
    {
        $pfad = $this->tmpVerzeichnis . '/unvollstaendig.json';
        KeyringFixture::schreibeDatei($pfad, KeyringFixture::unvollstaendigOhneBankSignaturX(), 0600);

        $this->expectException(HardStopException::class);
        $this->expectExceptionMessageMatches('/Bank-Signatur X/');
        $this->guard->loadVerified($pfad, 'pw', $this->erwarteteFingerprints());
    }

    public function testFingerprintAbweichungBrichtHartAb(): void
    {
        $pfad = $this->tmpVerzeichnis . '/gueltig.json';
        KeyringFixture::schreibeDatei($pfad, KeyringFixture::vollstaendig(), 0600);

        $falscheFingerprints = [
            'signature_x' => str_repeat('0', 64),
            'signature_e' => KeyringFixture::bankEFingerprint(),
        ];

        $this->expectException(HardStopException::class);
        $this->expectExceptionMessageMatches('/Fingerprint/');
        $this->guard->loadVerified($pfad, 'pw', $falscheFingerprints);
    }

    public function testGueltigerKeyringMitPassendemFingerprintWirdGeladen(): void
    {
        $pfad = $this->tmpVerzeichnis . '/gueltig.json';
        KeyringFixture::schreibeDatei($pfad, KeyringFixture::vollstaendig(), 0600);

        $keyring = $this->guard->loadVerified($pfad, 'pw', $this->erwarteteFingerprints());

        self::assertNotNull($keyring->getBankSignatureX());
        self::assertNotNull($keyring->getBankSignatureE());
        self::assertNotNull($keyring->getUserSignatureA());
    }
}
