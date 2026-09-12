<?php

declare(strict_types=1);

namespace EbicsDownloader\Tests\Camt;

use EbicsDownloader\Camt\Camt053StatementValidator;
use EbicsDownloader\Camt\DeliveryProcessor;
use EbicsDownloader\Camt\SafeZipReader;
use EbicsDownloader\Camt\StatementSplitter;
use EbicsDownloader\Camt\XmlSafetyGuard;
use EbicsDownloader\Config\ProfileLoader;
use EbicsDownloader\Exceptions\DeliveryRejectedException;
use EbicsDownloader\Tests\Config\ProfileFixture;
use PHPUnit\Framework\TestCase;
use ZipArchive;

final class DeliveryProcessorTest extends TestCase
{
    private DeliveryProcessor $processor;

    protected function setUp(): void
    {
        $this->processor = new DeliveryProcessor(
            new XmlSafetyGuard(),
            new StatementSplitter(new Camt053StatementValidator()),
            new SafeZipReader(maxEntries: 10, maxEntryBytes: 100000, maxTotalBytes: 1000000),
        );
    }

    public function testEinzelneXmlDateiOhneZipContainer(): void
    {
        $config = (new ProfileLoader())->fromArray(ProfileFixture::gueltig());
        $xml = CamtFixture::einStatement('AT000000000000000601');

        $ergebnisse = $this->processor->verarbeite($xml, false, $config);

        self::assertCount(1, $ergebnisse);
        self::assertTrue($ergebnisse[0]->freigegeben);
    }

    public function testZipContainerMitMehrerenStatementDateien(): void
    {
        $config = (new ProfileLoader())->fromArray(ProfileFixture::gueltig());
        $zipBytes = $this->baueZip([
            'a.xml' => CamtFixture::einStatement('AT000000000000000601'),
            'b.xml' => CamtFixture::einStatement('AT000000000000000616'),
        ]);

        $ergebnisse = $this->processor->verarbeite($zipBytes, true, $config);

        self::assertCount(2, $ergebnisse);
        self::assertTrue($ergebnisse[0]->freigegeben);
        self::assertTrue($ergebnisse[1]->freigegeben);
    }

    public function testEinBeschaedigtesDokumentImZipLehntDieGesamteLieferungAb(): void
    {
        $config = (new ProfileLoader())->fromArray(ProfileFixture::gueltig());
        $zipBytes = $this->baueZip([
            'gut.xml' => CamtFixture::einStatement('AT000000000000000601'),
            'boese.xml' => CamtFixture::mitEntity('AT000000000000000616'),
        ]);

        $this->expectException(DeliveryRejectedException::class);
        $this->processor->verarbeite($zipBytes, true, $config);
    }

    /** @param array<string, string> $dateien */
    private function baueZip(array $dateien): string
    {
        $pfad = tempnam(sys_get_temp_dir(), 'ebics-delivery-test-');
        $zip = new ZipArchive();
        $zip->open($pfad, ZipArchive::OVERWRITE);
        foreach ($dateien as $name => $inhalt) {
            $zip->addFromString($name, $inhalt);
        }
        $zip->close();
        $bytes = (string) file_get_contents($pfad);
        unlink($pfad);
        return $bytes;
    }
}
