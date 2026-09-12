<?php

declare(strict_types=1);

namespace EbicsDownloader\Tests\Camt;

use EbicsDownloader\Camt\Camt053StatementValidator;
use EbicsDownloader\Camt\StatementSplitter;
use EbicsDownloader\Camt\XmlSafetyGuard;
use EbicsDownloader\Config\ProfileLoader;
use EbicsDownloader\Tests\Config\ProfileFixture;
use PHPUnit\Framework\TestCase;

final class StatementSplitterTest extends TestCase
{
    private StatementSplitter $splitter;
    private XmlSafetyGuard $xmlGuard;

    protected function setUp(): void
    {
        $this->splitter = new StatementSplitter(new Camt053StatementValidator());
        $this->xmlGuard = new XmlSafetyGuard();
    }

    public function testWhitelistedKontoWirdFreigegeben(): void
    {
        $config = (new ProfileLoader())->fromArray(ProfileFixture::gueltig());
        $dom = $this->xmlGuard->parseSicher(CamtFixture::einStatement('AT000000000000000601'));

        $ergebnisse = $this->splitter->trenne($dom, $config);

        self::assertCount(1, $ergebnisse);
        self::assertTrue($ergebnisse[0]->freigegeben);
        self::assertSame('601', $ergebnisse[0]->objekt);
        self::assertSame('AT000000000000000601', $ergebnisse[0]->iban);
        self::assertStringContainsString('AT000000000000000601', $ergebnisse[0]->xml);
    }

    public function testFremdesKontoWirdQuarantaeniertNichtFreigegeben(): void
    {
        $config = (new ProfileLoader())->fromArray(ProfileFixture::gueltig());
        $dom = $this->xmlGuard->parseSicher(CamtFixture::einStatement('AT000000000000009999'));

        $ergebnisse = $this->splitter->trenne($dom, $config);

        self::assertCount(1, $ergebnisse);
        self::assertFalse($ergebnisse[0]->freigegeben);
        self::assertNull($ergebnisse[0]->objekt);
        self::assertStringContainsString('zu keinem der freigegebenen', $ergebnisse[0]->ablehnungsgrund ?? '');
    }

    public function testKundensammeldateiTrenntFreigegebenesVonFremdemKonto(): void
    {
        $config = (new ProfileLoader())->fromArray(ProfileFixture::gueltig());
        $dom = $this->xmlGuard->parseSicher(
            CamtFixture::zweiStatements('AT000000000000000601', 'AT000000000000009999')
        );

        $ergebnisse = $this->splitter->trenne($dom, $config);

        self::assertCount(2, $ergebnisse);
        self::assertTrue($ergebnisse[0]->freigegeben);
        self::assertFalse($ergebnisse[1]->freigegeben);
    }

    public function testObjekt107IstNieImWhitelistProfilEnthaltenUndWirdDaherQuarantaeniert(): void
    {
        $config = (new ProfileLoader())->fromArray(ProfileFixture::gueltig());
        self::assertNull($config->objektFuerIban('AT-107-KONTO-WAERE-NIE-ERLAUBT'));

        $dom = $this->xmlGuard->parseSicher(CamtFixture::einStatement('AT-107-KONTO-WAERE-NIE-ERLAUBT'));
        $ergebnisse = $this->splitter->trenne($dom, $config);

        self::assertFalse($ergebnisse[0]->freigegeben);
    }

    public function testMehrdeutigesStatementWirdQuarantaeniertMitEigenemGrund(): void
    {
        $config = (new ProfileLoader())->fromArray(ProfileFixture::gueltig());
        $dom = $this->xmlGuard->parseSicher(
            CamtFixture::stmtMitMehrerenAcct('AT000000000000000601', 'AT000000000000000616')
        );

        $ergebnisse = $this->splitter->trenne($dom, $config);

        self::assertCount(1, $ergebnisse);
        self::assertFalse($ergebnisse[0]->freigegeben);
        self::assertStringContainsString('nicht eindeutig', $ergebnisse[0]->ablehnungsgrund ?? '');
    }
}
