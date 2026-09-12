<?php

declare(strict_types=1);

namespace EbicsDownloader\Tests\Camt;

use EbicsDownloader\Camt\SafeZipReader;
use EbicsDownloader\Exceptions\DeliveryRejectedException;
use PHPUnit\Framework\TestCase;
use ZipArchive;

final class SafeZipReaderTest extends TestCase
{
    /** @var string[] */
    private array $tmpDateien = [];

    protected function tearDown(): void
    {
        foreach ($this->tmpDateien as $pfad) {
            @unlink($pfad);
        }
        $this->tmpDateien = [];
    }

    private function baueZip(callable $befuellen): string
    {
        $pfad = tempnam(sys_get_temp_dir(), 'ebics-zip-test-');
        $this->tmpDateien[] = $pfad;
        $zip = new ZipArchive();
        $zip->open($pfad, ZipArchive::OVERWRITE);
        $befuellen($zip);
        $zip->close();
        return (string) file_get_contents($pfad);
    }

    public function testNormalesArchivWirdVollstaendigGelesen(): void
    {
        $zipBytes = $this->baueZip(function (ZipArchive $zip): void {
            $zip->addFromString('statement1.xml', 'INHALT-1');
            $zip->addFromString('statement2.xml', 'INHALT-2');
        });

        $reader = new SafeZipReader(maxEntries: 10, maxEntryBytes: 1000, maxTotalBytes: 10000);
        $inhalte = $reader->entpackeSicher($zipBytes);

        self::assertCount(2, $inhalte);
        self::assertContains('INHALT-1', $inhalte);
        self::assertContains('INHALT-2', $inhalte);
    }

    public function testZuVieleEintraegeWerdenAbgelehntOhneEntpacken(): void
    {
        $zipBytes = $this->baueZip(function (ZipArchive $zip): void {
            for ($i = 0; $i < 5; $i++) {
                $zip->addFromString("datei-$i.xml", "inhalt-$i");
            }
        });

        $reader = new SafeZipReader(maxEntries: 3, maxEntryBytes: 1000, maxTotalBytes: 10000);

        $this->expectException(DeliveryRejectedException::class);
        $this->expectExceptionMessageMatches('/Einträge/');
        $reader->entpackeSicher($zipBytes);
    }

    public function testZuGrosserEinzelnerEintragWirdAbgelehnt(): void
    {
        $zipBytes = $this->baueZip(function (ZipArchive $zip): void {
            $zip->addFromString('gross.xml', str_repeat('A', 2000));
        });

        $reader = new SafeZipReader(maxEntries: 10, maxEntryBytes: 100, maxTotalBytes: 10000);

        $this->expectException(DeliveryRejectedException::class);
        $this->expectExceptionMessageMatches('/zu groß/');
        $reader->entpackeSicher($zipBytes);
    }

    public function testZuGrosseGesamtsummeWirdAbgelehnt(): void
    {
        $zipBytes = $this->baueZip(function (ZipArchive $zip): void {
            $zip->addFromString('a.xml', str_repeat('A', 600));
            $zip->addFromString('b.xml', str_repeat('B', 600));
        });

        $reader = new SafeZipReader(maxEntries: 10, maxEntryBytes: 1000, maxTotalBytes: 1000);

        $this->expectException(DeliveryRejectedException::class);
        $this->expectExceptionMessageMatches('/Gesamtgröße/');
        $reader->entpackeSicher($zipBytes);
    }

    public function testPfadTraversalNameWirdAbgelehnt(): void
    {
        $zipBytes = $this->baueZip(function (ZipArchive $zip): void {
            $zip->addFromString('../../etc/passwd', 'boese');
        });

        $reader = new SafeZipReader(maxEntries: 10, maxEntryBytes: 1000, maxTotalBytes: 10000);

        $this->expectException(DeliveryRejectedException::class);
        $this->expectExceptionMessageMatches('/unsicherem Namen/');
        $reader->entpackeSicher($zipBytes);
    }

    public function testAbsoluterPfadNameWirdAbgelehnt(): void
    {
        $zipBytes = $this->baueZip(function (ZipArchive $zip): void {
            $zip->addFromString('regulaer.xml', 'harmlos');
            $index = $zip->locateName('regulaer.xml');
            self::assertNotFalse($index);
            $zip->renameIndex($index, '/etc/passwd');
        });

        $reader = new SafeZipReader(maxEntries: 10, maxEntryBytes: 1000, maxTotalBytes: 10000);

        $this->expectException(DeliveryRejectedException::class);
        $reader->entpackeSicher($zipBytes);
    }

    public function testSymlinkEintragWirdAbgelehnt(): void
    {
        $zipBytes = $this->baueZip(function (ZipArchive $zip): void {
            $zip->addFromString('link', '/etc/passwd');
            $index = $zip->locateName('link');
            self::assertNotFalse($index);
            $symlinkModus = 0xA000 | 0777;
            $zip->setExternalAttributesIndex($index, ZipArchive::OPSYS_UNIX, $symlinkModus << 16);
        });

        $reader = new SafeZipReader(maxEntries: 10, maxEntryBytes: 1000, maxTotalBytes: 10000);

        $this->expectException(DeliveryRejectedException::class);
        $this->expectExceptionMessageMatches('/Symlink/');
        $reader->entpackeSicher($zipBytes);
    }

    public function testMetadatenPruefungOhneEntpackenWirftBeiVerletzungOhneRueckgabewert(): void
    {
        $zipBytes = $this->baueZip(function (ZipArchive $zip): void {
            for ($i = 0; $i < 5; $i++) {
                $zip->addFromString("datei-$i.xml", "inhalt-$i");
            }
        });

        $reader = new SafeZipReader(maxEntries: 3, maxEntryBytes: 1000, maxTotalBytes: 10000);

        $this->expectException(DeliveryRejectedException::class);
        $reader->pruefeMetadatenOhneEntpacken($zipBytes);
    }

    public function testMetadatenPruefungBeiGueltigemArchivWirftNicht(): void
    {
        $zipBytes = $this->baueZip(function (ZipArchive $zip): void {
            $zip->addFromString('a.xml', 'harmlos');
        });

        $reader = new SafeZipReader(maxEntries: 10, maxEntryBytes: 1000, maxTotalBytes: 10000);
        $reader->pruefeMetadatenOhneEntpacken($zipBytes);
        $this->addToAssertionCount(1);
    }
}
