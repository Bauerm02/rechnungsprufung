<?php

declare(strict_types=1);

namespace EbicsDownloader\Tests\Archive;

use EbicsDownloader\Archive\ManifestWriter;
use PHPUnit\Framework\TestCase;

final class ManifestWriterTest extends TestCase
{
    private string $pfad;

    protected function setUp(): void
    {
        $this->pfad = sys_get_temp_dir() . '/ebics-manifest-test-' . bin2hex(random_bytes(6)) . '/manifest.jsonl';
    }

    protected function tearDown(): void
    {
        @unlink($this->pfad);
        @rmdir(dirname($this->pfad));
    }

    public function testEintragEnthaeltNieDieVolleIban(): void
    {
        $writer = new ManifestWriter($this->pfad);
        $writer->anhaengen(['ereignis' => 'test', 'iban_maskiert' => ManifestWriter::maskiereIban('AT611234567890123456')]);

        $inhalt = (string) file_get_contents($this->pfad);
        self::assertStringNotContainsString('1234567890', $inhalt);
        self::assertStringContainsString('AT61', $inhalt);
        self::assertStringContainsString('3456', $inhalt);
    }

    public function testMaskierungKurzerIbanErgibtNurSterne(): void
    {
        self::assertSame('********', ManifestWriter::maskiereIban('AT611234'));
    }

    public function testMehrereEintraegeWerdenAlsJsonLinesAngehaengt(): void
    {
        $writer = new ManifestWriter($this->pfad);
        $writer->anhaengen(['ereignis' => 'eins']);
        $writer->anhaengen(['ereignis' => 'zwei']);

        $zeilen = array_filter(explode("\n", (string) file_get_contents($this->pfad)));
        self::assertCount(2, $zeilen);
        self::assertSame('eins', json_decode($zeilen[0], true)['ereignis']);
        self::assertSame('zwei', json_decode($zeilen[1], true)['ereignis']);
    }
}
