<?php

declare(strict_types=1);

namespace EbicsDownloader\Tests\Config;

use EbicsDownloader\Config\ProfileLoader;
use EbicsDownloader\Exceptions\ConfigValidationException;
use PHPUnit\Framework\TestCase;

final class ProfileLoaderTest extends TestCase
{
    private ProfileLoader $loader;

    protected function setUp(): void
    {
        $this->loader = new ProfileLoader();
    }

    public function testGueltigesProfilWirdAkzeptiert(): void
    {
        $config = $this->loader->fromArray(ProfileFixture::gueltig());
        $config->validateStructure();
        $this->loader->assertRunnable($config);
        self::assertTrue($config->isObjektErlaubt('601'));
        self::assertSame('AT000000000000000601', $config->ibanFuerObjekt('601'));
    }

    public function testDeaktiviertesProfilBestehtValidateStructureAberNichtAssertRunnable(): void
    {
        $daten = ProfileFixture::gueltig();
        $daten['enabled'] = false;
        $config = $this->loader->fromArray($daten);

        $config->validateStructure();

        $this->expectException(ConfigValidationException::class);
        $this->loader->assertRunnable($config);
    }

    public function testPlatzhalterBestehtValidateStructureAberNichtAssertRunnable(): void
    {
        $daten = ProfileFixture::gueltig();
        $daten['host_id'] = 'PLATZHALTER_HOST_ID';
        $config = $this->loader->fromArray($daten);

        $config->validateStructure();
        self::assertNotSame([], $config->findePlatzhalter());

        $this->expectException(ConfigValidationException::class);
        $this->loader->assertRunnable($config);
    }

    public function testObjekt107DarfNichtInAllowedAccountsStehen(): void
    {
        $daten = ProfileFixture::gueltig();
        $daten['allowed_accounts'] = [
            ['objekt' => '107', 'iban' => 'AT000000000000000107'],
            ['objekt' => '616', 'iban' => 'AT000000000000000616'],
            ['objekt' => '617', 'iban' => 'AT000000000000000617'],
        ];
        $config = $this->loader->fromArray($daten);

        $this->expectException(ConfigValidationException::class);
        $this->expectExceptionMessage('107');
        $config->validateStructure();
    }

    public function testBlockedObjekteMussObjekt107Enthalten(): void
    {
        $daten = ProfileFixture::gueltig();
        $daten['blocked_objekte'] = [];
        $config = $this->loader->fromArray($daten);

        $this->expectException(ConfigValidationException::class);
        $config->validateStructure();
    }

    public function testGenauDreiKontenSindPflicht(): void
    {
        $daten = ProfileFixture::gueltig();
        $daten['allowed_accounts'] = [
            ['objekt' => '601', 'iban' => 'AT000000000000000601'],
        ];
        $config = $this->loader->fromArray($daten);

        $this->expectException(ConfigValidationException::class);
        $config->validateStructure();
    }

    public function testFalscheObjektmengeWirdAbgelehnt(): void
    {
        $daten = ProfileFixture::gueltig();
        $daten['allowed_accounts'] = [
            ['objekt' => '601', 'iban' => 'AT000000000000000601'],
            ['objekt' => '616', 'iban' => 'AT000000000000000616'],
            ['objekt' => '999', 'iban' => 'AT000000000000000999'],
        ];
        $config = $this->loader->fromArray($daten);

        $this->expectException(ConfigValidationException::class);
        $config->validateStructure();
    }

    public function testDoppelteIbansWerdenAbgelehnt(): void
    {
        $daten = ProfileFixture::gueltig();
        $daten['allowed_accounts'] = [
            ['objekt' => '601', 'iban' => 'AT000000000000000601'],
            ['objekt' => '616', 'iban' => 'AT000000000000000601'],
            ['objekt' => '617', 'iban' => 'AT000000000000000617'],
        ];
        $config = $this->loader->fromArray($daten);

        $this->expectException(ConfigValidationException::class);
        $config->validateStructure();
    }

    public function testTlsVerifyPeerPflicht(): void
    {
        $daten = ProfileFixture::gueltig();
        $daten['tls']['verify_peer'] = false;
        $config = $this->loader->fromArray($daten);

        $this->expectException(ConfigValidationException::class);
        $config->validateStructure();
    }

    public function testTlsVerifyHostPflicht(): void
    {
        $daten = ProfileFixture::gueltig();
        $daten['tls']['verify_host'] = false;
        $config = $this->loader->fromArray($daten);

        $this->expectException(ConfigValidationException::class);
        $config->validateStructure();
    }

    public function testRedirectsMuessenVerbotenSein(): void
    {
        $daten = ProfileFixture::gueltig();
        $daten['tls']['allow_redirects'] = true;
        $config = $this->loader->fromArray($daten);

        $this->expectException(ConfigValidationException::class);
        $config->validateStructure();
    }

    public function testBankUrlMussHttpsSein(): void
    {
        $daten = ProfileFixture::gueltig();
        $daten['bank_url'] = 'http://bank.example.invalid/ebics';
        $config = $this->loader->fromArray($daten);

        $this->expectException(ConfigValidationException::class);
        $config->validateStructure();
    }

    public function testUngueltigeEbicsVersionWirdAbgelehnt(): void
    {
        $daten = ProfileFixture::gueltig();
        $daten['ebics_version'] = 'H003';
        $config = $this->loader->fromArray($daten);

        $this->expectException(ConfigValidationException::class);
        $config->validateStructure();
    }

    public function testH004VerlangtAktivenFdlFallback(): void
    {
        $daten = ProfileFixture::gueltig();
        $daten['ebics_version'] = 'H004';
        $daten['fdl_fallback'] = ['enabled' => false, 'file_format' => null];
        $config = $this->loader->fromArray($daten);

        $this->expectException(ConfigValidationException::class);
        $config->validateStructure();
    }

    public function testFdlFallbackVerlangtFileFormat(): void
    {
        $daten = ProfileFixture::gueltig();
        $daten['ebics_version'] = 'H004';
        $daten['fdl_fallback'] = ['enabled' => true, 'file_format' => null];
        $config = $this->loader->fromArray($daten);

        $this->expectException(ConfigValidationException::class);
        $config->validateStructure();
    }

    public function testH004MitVollstaendigemFdlFallbackIstGueltig(): void
    {
        $daten = ProfileFixture::gueltig();
        $daten['ebics_version'] = 'H004';
        $daten['fdl_fallback'] = ['enabled' => true, 'file_format' => 'camt.053'];
        $config = $this->loader->fromArray($daten);

        $config->validateStructure();
        $this->addToAssertionCount(1);
    }

    public function testNegativeLimitsWerdenAbgelehnt(): void
    {
        $daten = ProfileFixture::gueltig();
        $daten['limits']['max_zip_entries'] = 0;
        $config = $this->loader->fromArray($daten);

        $this->expectException(ConfigValidationException::class);
        $config->validateStructure();
    }

    public function testFehlendesPflichtfeldWirdBeimLadenAbgelehnt(): void
    {
        $daten = ProfileFixture::gueltig();
        unset($daten['host_id']);

        $this->expectException(ConfigValidationException::class);
        $this->loader->fromArray($daten);
    }

    public function testObjektFuerIbanIstDieKehrfunktionZuIbanFuerObjekt(): void
    {
        $config = $this->loader->fromArray(ProfileFixture::gueltig());
        $original = 'AT000000000000000616';
        $mitLeerzeichenUndKleinbuchstaben = strtolower(substr($original, 0, 4) . ' ' . substr($original, 4));

        self::assertSame('616', $config->objektFuerIban($original));
        self::assertSame('616', $config->objektFuerIban($mitLeerzeichenUndKleinbuchstaben));
        self::assertNull($config->objektFuerIban('AT99999999999999999999'));
    }
}
