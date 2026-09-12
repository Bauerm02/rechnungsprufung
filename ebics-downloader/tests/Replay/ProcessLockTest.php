<?php

declare(strict_types=1);

namespace EbicsDownloader\Tests\Replay;

use EbicsDownloader\Exceptions\HardStopException;
use EbicsDownloader\Replay\ProcessLock;
use PHPUnit\Framework\TestCase;

final class ProcessLockTest extends TestCase
{
    private string $pfad;

    protected function setUp(): void
    {
        $this->pfad = sys_get_temp_dir() . '/ebics-lock-test-' . bin2hex(random_bytes(6)) . '.lock';
    }

    protected function tearDown(): void
    {
        @unlink($this->pfad);
    }

    public function testZweiterParalleleAbrufversuchWirdAbgelehnt(): void
    {
        $erste = new ProcessLock($this->pfad, timeoutSekunden: 1);
        $erste->erlangen();

        $zweite = new ProcessLock($this->pfad, timeoutSekunden: 1);
        $this->expectException(HardStopException::class);
        $this->expectExceptionMessageMatches('/läuft bereits/');
        try {
            $zweite->erlangen();
        } finally {
            $erste->freigeben();
        }
    }

    public function testNachFreigabeKannEinNeuerLaufDieSperreErlangen(): void
    {
        $erste = new ProcessLock($this->pfad, timeoutSekunden: 1);
        $erste->erlangen();
        $erste->freigeben();

        $zweite = new ProcessLock($this->pfad, timeoutSekunden: 1);
        $zweite->erlangen();
        $zweite->freigeben();

        $this->addToAssertionCount(1);
    }
}
