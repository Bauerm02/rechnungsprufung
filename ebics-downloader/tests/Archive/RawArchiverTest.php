<?php

declare(strict_types=1);

namespace EbicsDownloader\Tests\Archive;

use EbicsDownloader\Archive\RawArchiver;
use PHPUnit\Framework\TestCase;

final class RawArchiverTest extends TestCase
{
    private string $verzeichnis;

    protected function setUp(): void
    {
        $this->verzeichnis = sys_get_temp_dir() . '/ebics-archiv-test-' . bin2hex(random_bytes(6));
    }

    protected function tearDown(): void
    {
        if (!is_dir($this->verzeichnis)) {
            return;
        }
        foreach (glob($this->verzeichnis . '/*') ?: [] as $datei) {
            @unlink($datei);
        }
        @rmdir($this->verzeichnis);
    }

    public function testArchiviertMitKorrektemHashUndRestriktivenRechten(): void
    {
        $archiver = new RawArchiver($this->verzeichnis);
        $ergebnis = $archiver->archiviere('inhalt-der-lieferung', 'testprofil');

        self::assertFileExists($ergebnis['path']);
        self::assertSame(hash('sha256', 'inhalt-der-lieferung'), $ergebnis['sha256']);
        self::assertSame('inhalt-der-lieferung', file_get_contents($ergebnis['path']));

        $rechte = fileperms($ergebnis['path']) & 0777;
        self::assertSame(0600, $rechte);
    }

    public function testLegtVerzeichnisAnFallsEsFehlt(): void
    {
        self::assertDirectoryDoesNotExist($this->verzeichnis);
        $archiver = new RawArchiver($this->verzeichnis);
        $archiver->archiviere('x', 'testprofil');

        self::assertDirectoryExists($this->verzeichnis);
    }
}
