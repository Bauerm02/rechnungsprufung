<?php

declare(strict_types=1);

namespace EbicsDownloader\Tests\Camt;

use EbicsDownloader\Camt\XmlSafetyGuard;
use EbicsDownloader\Exceptions\DeliveryRejectedException;
use PHPUnit\Framework\TestCase;

final class XmlSafetyGuardTest extends TestCase
{
    private XmlSafetyGuard $guard;

    protected function setUp(): void
    {
        $this->guard = new XmlSafetyGuard();
    }

    public function testDoctypeWirdVorJedemParsingAbgelehnt(): void
    {
        $this->expectException(DeliveryRejectedException::class);
        $this->expectExceptionMessageMatches('/DOCTYPE/');
        $this->guard->parseSicher(CamtFixture::mitDoctype('AT000000000000000601'));
    }

    public function testEntityWirdVorJedemParsingAbgelehnt(): void
    {
        $this->expectException(DeliveryRejectedException::class);
        $this->expectExceptionMessageMatches('/ENTITY/');
        $this->guard->parseSicher(CamtFixture::mitEntity('AT000000000000000601'));
    }

    public function testKaputtesXmlWirdAbgelehnt(): void
    {
        $this->expectException(DeliveryRejectedException::class);
        $this->guard->parseSicher(CamtFixture::kaputtesXml());
    }

    public function testGueltigesXmlWirdGeparst(): void
    {
        $dom = $this->guard->parseSicher(CamtFixture::einStatement('AT000000000000000601'));
        self::assertSame('Document', $dom->documentElement?->localName);
    }
}
