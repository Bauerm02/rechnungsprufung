<?php

declare(strict_types=1);

namespace EbicsDownloader\Tests\Replay;

use DateTimeImmutable;
use EbicsDownloader\Replay\DateWindowStore;
use PHPUnit\Framework\TestCase;

final class DateWindowStoreTest extends TestCase
{
    private string $pfad;

    protected function setUp(): void
    {
        $this->pfad = sys_get_temp_dir() . '/ebics-watermark-test-' . bin2hex(random_bytes(6)) . '.txt';
    }

    protected function tearDown(): void
    {
        @unlink($this->pfad);
    }

    public function testOhneVorherigenLaufIstNichtsGespeichert(): void
    {
        $store = new DateWindowStore($this->pfad);
        self::assertNull($store->ladeLetztesBis());
    }

    public function testGespeichertesDatumWirdWiederGeladen(): void
    {
        $store = new DateWindowStore($this->pfad);
        $bis = new DateTimeImmutable('2026-01-31T00:00:00+00:00');
        $store->schreibeBis($bis);

        $geladen = $store->ladeLetztesBis();
        self::assertNotNull($geladen);
        self::assertSame($bis->format('c'), $geladen->format('c'));
    }
}
