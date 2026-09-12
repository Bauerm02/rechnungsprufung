<?php

declare(strict_types=1);

namespace EbicsDownloader\Tests\Cli;

use EbicsDownloader\Cli\CheckConfigCommand;
use EbicsDownloader\Tests\Config\ProfileFixture;
use PHPUnit\Framework\TestCase;

final class CheckConfigCommandTest extends TestCase
{
    private string $pfad;

    protected function setUp(): void
    {
        $this->pfad = sys_get_temp_dir() . '/ebics-checkconfig-test-' . bin2hex(random_bytes(6)) . '.json';
    }

    protected function tearDown(): void
    {
        @unlink($this->pfad);
    }

    public function testDeaktiviertesProfilMitPlatzhalternIstStrukturellGueltig(): void
    {
        $daten = ProfileFixture::gueltig();
        $daten['enabled'] = false;
        $daten['host_id'] = 'PLATZHALTER_HOST_ID';
        file_put_contents($this->pfad, json_encode($daten));

        $exitCode = (new CheckConfigCommand())->ausfuehren($this->pfad);

        self::assertSame(0, $exitCode);
    }

    public function testStrukturellUngueltigesProfilLiefertFehlercode(): void
    {
        $daten = ProfileFixture::gueltig();
        $daten['allowed_accounts'] = [];
        file_put_contents($this->pfad, json_encode($daten));

        $exitCode = (new CheckConfigCommand())->ausfuehren($this->pfad);

        self::assertSame(1, $exitCode);
    }

    public function testDenGenerischenBeispielprofilenBestehenDenCheckConfig(): void
    {
        $wurzel = dirname(__DIR__, 2);
        foreach (glob($wurzel . '/config/profiles/*.example.json') ?: [] as $beispielPfad) {
            $exitCode = (new CheckConfigCommand())->ausfuehren($beispielPfad);
            self::assertSame(0, $exitCode, "Beispielprofil $beispielPfad muss strukturell gültig sein.");
        }
    }
}
