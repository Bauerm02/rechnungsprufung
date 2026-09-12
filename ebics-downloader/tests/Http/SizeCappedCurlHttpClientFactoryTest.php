<?php

declare(strict_types=1);

namespace EbicsDownloader\Tests\Http;

use EbicsDownloader\Config\ProfileLoader;
use EbicsDownloader\Http\SizeCappedCurlHttpClientFactory;
use EbicsDownloader\Tests\Config\ProfileFixture;
use PHPUnit\Framework\TestCase;

final class SizeCappedCurlHttpClientFactoryTest extends TestCase
{
    public function testErzwingtTlsPruefungUndVerbietetRedirects(): void
    {
        $config = (new ProfileLoader())->fromArray(ProfileFixture::gueltig());
        $client = (new SizeCappedCurlHttpClientFactory())->build($config);

        $options = $this->leseOptionen($client);

        self::assertTrue($options[CURLOPT_SSL_VERIFYPEER]);
        self::assertSame(2, $options[CURLOPT_SSL_VERIFYHOST]);
        self::assertFalse($options[CURLOPT_FOLLOWLOCATION]);
        self::assertSame(0, $options[CURLOPT_MAXREDIRS]);
        self::assertArrayHasKey(CURLOPT_XFERINFOFUNCTION, $options);
        self::assertFalse($options[CURLOPT_NOPROGRESS]);
    }

    public function testProgressCallbackBrichtBeiUeberschreitungDesLimitsAb(): void
    {
        $config = (new ProfileLoader())->fromArray(ProfileFixture::gueltig());
        $client = (new SizeCappedCurlHttpClientFactory())->build($config);
        $options = $this->leseOptionen($client);
        $callback = $options[CURLOPT_XFERINFOFUNCTION];

        $limit = $config->limits->maxResponseBytes;
        self::assertSame(0, $callback(null, 0, $limit));
        self::assertSame(1, $callback(null, 0, $limit + 1));
    }

    public function testCaBundlePfadWirdNurGesetztWennKonfiguriert(): void
    {
        $config = (new ProfileLoader())->fromArray(ProfileFixture::gueltig());
        $client = (new SizeCappedCurlHttpClientFactory())->build($config);
        $options = $this->leseOptionen($client);

        self::assertArrayNotHasKey(CURLOPT_CAINFO, $options);
    }

    /** @return array<int, mixed> */
    private function leseOptionen(object $client): array
    {
        $reflection = new \ReflectionProperty($client, 'options');
        $reflection->setAccessible(true);
        return $reflection->getValue($client);
    }
}
