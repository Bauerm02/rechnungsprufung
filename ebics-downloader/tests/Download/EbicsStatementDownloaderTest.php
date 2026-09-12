<?php

declare(strict_types=1);

namespace EbicsDownloader\Tests\Download;

use EbicsApi\Ebics\Services\FileKeyringManager;
use EbicsDownloader\Archive\RawArchiver;
use EbicsDownloader\Config\ProfileLoader;
use EbicsDownloader\Download\EbicsStatementDownloader;
use EbicsDownloader\Exceptions\HardStopException;
use EbicsDownloader\Http\SizeCappedCurlHttpClientFactory;
use EbicsDownloader\Keyring\KeyringGuard;
use EbicsDownloader\Tests\Config\ProfileFixture;
use EbicsDownloader\Tests\Keyring\KeyringFixture;
use PHPUnit\Framework\TestCase;

/**
 * Deckt AUSSCHLIESSLICH die Fehlerpfade ab, die VOR jedem tatsächlichen
 * Netzwerkkontakt zur Bank greifen (Keyring-/Passphrase-Prüfung) - ein
 * echter EBICS-Serverkontakt ist in dieser Sitzung weder verfügbar noch
 * beauftragt (siehe README.md: eine bestandene Offline-/Synthetiktest-
 * suite ist KEINE Live-Bank-Abnahme).
 */
final class EbicsStatementDownloaderTest extends TestCase
{
    private string $tmpVerzeichnis;

    protected function setUp(): void
    {
        $this->tmpVerzeichnis = sys_get_temp_dir() . '/ebics-downloader-test-' . bin2hex(random_bytes(6));
        mkdir($this->tmpVerzeichnis, 0700, true);
    }

    protected function tearDown(): void
    {
        foreach (glob($this->tmpVerzeichnis . '/*') ?: [] as $datei) {
            @unlink($datei);
        }
        @rmdir($this->tmpVerzeichnis);
    }

    private function baueDownloader(array $profilUeberschreibungen = []): EbicsStatementDownloader
    {
        $daten = array_merge(ProfileFixture::gueltig(), $profilUeberschreibungen);
        $daten['keyring']['path'] = $this->tmpVerzeichnis . '/keyring.json';
        $daten['keyring']['passphrase_file'] = $this->tmpVerzeichnis . '/passphrase.txt';
        $config = (new ProfileLoader())->fromArray($daten);

        return new EbicsStatementDownloader(
            $config,
            new KeyringGuard(new FileKeyringManager()),
            new RawArchiver($this->tmpVerzeichnis . '/archiv'),
            new SizeCappedCurlHttpClientFactory(),
        );
    }

    public function testFehlendePassphraseDateiBrichtHartAbOhneNetzwerkkontakt(): void
    {
        $downloader = $this->baueDownloader();

        $this->expectException(HardStopException::class);
        $this->expectExceptionMessageMatches('/Passphrase-Datei fehlt/');
        $downloader->herunterladen(null, null);
    }

    public function testLeerePassphraseDateiBrichtHartAb(): void
    {
        $downloader = $this->baueDownloader();
        touch($this->tmpVerzeichnis . '/passphrase.txt');

        $this->expectException(HardStopException::class);
        $this->expectExceptionMessageMatches('/leer/');
        $downloader->herunterladen(null, null);
    }

    public function testKeyringVersionMussZurConfigPassen(): void
    {
        // KeyringFixture::vollstaendig() liefert stets einen
        // Keyring::VERSION_30-Keyring; ein Profil mit ebics_version=H004
        // erwartet VERSION_25 und muss daher (NACH bestandener Fingerprint-
        // Prüfung - die Profil-Fingerprints werden hier bewusst passend zur
        // Fixture gesetzt, um genau diesen nachgelagerten Check isoliert zu
        // treffen) hart abbrechen, statt die Version automatisch anzupassen.
        $downloader = $this->baueDownloader([
            'ebics_version' => 'H004',
            'fdl_fallback' => ['enabled' => true, 'file_format' => 'camt.053'],
            'bank_fingerprint_sha256' => [
                'signature_x' => KeyringFixture::bankXFingerprint(),
                'signature_e' => KeyringFixture::bankEFingerprint(),
            ],
        ]);
        file_put_contents($this->tmpVerzeichnis . '/passphrase.txt', 'geheim');
        KeyringFixture::schreibeDatei($this->tmpVerzeichnis . '/keyring.json', KeyringFixture::vollstaendig(), 0600);

        $this->expectException(HardStopException::class);
        $this->expectExceptionMessageMatches('/passt nicht zur konfigurierten ebics_version/');
        $downloader->herunterladen(null, null);
    }
}
