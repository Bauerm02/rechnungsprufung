<?php

declare(strict_types=1);

namespace EbicsDownloader\Tests\Cli;

use EbicsDownloader\Cli\DownloadCommand;
use EbicsDownloader\Tests\Config\ProfileFixture;
use PHPUnit\Framework\TestCase;

final class DownloadCommandTest extends TestCase
{
    private string $pfad;

    protected function setUp(): void
    {
        $this->pfad = sys_get_temp_dir() . '/ebics-download-test-' . bin2hex(random_bytes(6)) . '.json';
    }

    protected function tearDown(): void
    {
        @unlink($this->pfad);
    }

    public function testDeaktiviertesProfilWirdOhneJedenDownloadversuchAbgelehnt(): void
    {
        $daten = ProfileFixture::gueltig();
        $daten['enabled'] = false;
        file_put_contents($this->pfad, json_encode($daten));

        $exitCode = (new DownloadCommand())->ausfuehren($this->pfad);

        self::assertSame(1, $exitCode);
    }

    public function testProfilMitPlatzhalternWirdOhneJedenDownloadversuchAbgelehnt(): void
    {
        $daten = ProfileFixture::gueltig();
        $daten['host_id'] = 'PLATZHALTER_HOST_ID';
        file_put_contents($this->pfad, json_encode($daten));

        $exitCode = (new DownloadCommand())->ausfuehren($this->pfad);

        self::assertSame(1, $exitCode);
    }
}
