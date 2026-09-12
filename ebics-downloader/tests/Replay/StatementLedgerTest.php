<?php

declare(strict_types=1);

namespace EbicsDownloader\Tests\Replay;

use EbicsDownloader\Replay\LedgerEntscheidung;
use EbicsDownloader\Replay\StatementLedger;
use PHPUnit\Framework\TestCase;

final class StatementLedgerTest extends TestCase
{
    private string $pfad;

    protected function setUp(): void
    {
        $this->pfad = sys_get_temp_dir() . '/ebics-ledger-test-' . bin2hex(random_bytes(6)) . '.json';
    }

    protected function tearDown(): void
    {
        @unlink($this->pfad);
    }

    public function testErsteLieferungIstNeu(): void
    {
        $ledger = new StatementLedger($this->pfad);
        $entscheidung = $ledger->pruefeUndErfasse('AT601', 'STMT-1', 'hash-a');

        self::assertSame(LedgerEntscheidung::NEU, $entscheidung->status);
    }

    public function testIdentischeErneuteLieferungIstReplay(): void
    {
        $ledger = new StatementLedger($this->pfad);
        $ledger->pruefeUndErfasse('AT601', 'STMT-1', 'hash-a');

        $zweiteEntscheidung = $ledger->pruefeUndErfasse('AT601', 'STMT-1', 'hash-a');

        self::assertSame(LedgerEntscheidung::REPLAY, $zweiteEntscheidung->status);
    }

    public function testAbweichenderInhaltGleicherAuszugsnummerIstKonflikt(): void
    {
        $ledger = new StatementLedger($this->pfad);
        $ledger->pruefeUndErfasse('AT601', 'STMT-1', 'hash-a');

        $entscheidung = $ledger->pruefeUndErfasse('AT601', 'STMT-1', 'hash-b-anders');

        self::assertSame(LedgerEntscheidung::KONFLIKT, $entscheidung->status);
        self::assertSame('hash-a', $entscheidung->vorhandenerHash);
    }

    public function testUnterschiedlicheIbansTeilenSichKeineAuszugsnummer(): void
    {
        $ledger = new StatementLedger($this->pfad);
        $ledger->pruefeUndErfasse('AT601', 'STMT-1', 'hash-a');

        $entscheidung = $ledger->pruefeUndErfasse('AT616', 'STMT-1', 'hash-a');

        self::assertSame(LedgerEntscheidung::NEU, $entscheidung->status);
    }

    public function testFehlendeAuszugsnummerDedupliziertNurBeiExaktGleichemInhalt(): void
    {
        $ledger = new StatementLedger($this->pfad);
        $erste = $ledger->pruefeUndErfasse('AT601', null, 'hash-x');
        $identisch = $ledger->pruefeUndErfasse('AT601', null, 'hash-x');
        $anders = $ledger->pruefeUndErfasse('AT601', null, 'hash-y');

        self::assertSame(LedgerEntscheidung::NEU, $erste->status);
        self::assertSame(LedgerEntscheidung::REPLAY, $identisch->status);
        self::assertSame(LedgerEntscheidung::NEU, $anders->status, 'Ohne Auszugsnummer nie als Konflikt gegeneinander werten.');
    }

    public function testPersistiertUeberMehrereLedgerInstanzenHinweg(): void
    {
        (new StatementLedger($this->pfad))->pruefeUndErfasse('AT601', 'STMT-1', 'hash-a');

        $neueInstanz = new StatementLedger($this->pfad);
        $entscheidung = $neueInstanz->pruefeUndErfasse('AT601', 'STMT-1', 'hash-a');

        self::assertSame(LedgerEntscheidung::REPLAY, $entscheidung->status);
    }
}
