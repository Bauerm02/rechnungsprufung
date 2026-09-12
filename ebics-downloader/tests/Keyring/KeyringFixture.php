<?php

declare(strict_types=1);

namespace EbicsDownloader\Tests\Keyring;

use EbicsApi\Ebics\Models\Keyring;

/**
 * Baut ein synthetisches, aber STRUKTURELL vollständiges Keyring-JSON
 * für Tests. "Schlüssel" sind absichtlich keine echten RSA-Schlüssel -
 * die Bibliothek validiert beim reinen Laden/Deserialisieren keine
 * kryptografische Gültigkeit (siehe StringKeyStorage::readPublicKey():
 * ein einfaches base64_decode), und `KeyringGuard` prüft ausschließlich
 * Vollständigkeit (alle fünf Signatur-Slots gesetzt) und den SHA-256-
 * Fingerprint der rohen Schlüsselbytes - beides mit beliebigen Bytes
 * testbar.
 */
final class KeyringFixture
{
    public const BANK_X_ROHBYTES = 'synthetische-bank-signatur-x-fuer-tests';
    public const BANK_E_ROHBYTES = 'synthetische-bank-signatur-e-fuer-tests';

    public static function bankXFingerprint(): string
    {
        return hash('sha256', self::BANK_X_ROHBYTES);
    }

    public static function bankEFingerprint(): string
    {
        return hash('sha256', self::BANK_E_ROHBYTES);
    }

    /** @return array<string, mixed> */
    public static function vollstaendig(): array
    {
        $signaturBlock = static fn (string $rohbytes, ?string $privat = 'synthetisch-privat') => [
            'CERTIFICATE' => null,
            'PUBLIC_KEY' => base64_encode($rohbytes),
            'PRIVATE_KEY' => $privat !== null ? base64_encode($privat) : null,
        ];

        return [
            Keyring::VERSION_PREFIX => Keyring::VERSION_30,
            Keyring::USER_PREFIX => [
                Keyring::SIGNATURE_PREFIX_A => array_merge(
                    $signaturBlock('user-signatur-a'),
                    [Keyring::VERSION_PREFIX => 'A006']
                ),
                Keyring::SIGNATURE_PREFIX_E => $signaturBlock('user-signatur-e'),
                Keyring::SIGNATURE_PREFIX_X => $signaturBlock('user-signatur-x'),
            ],
            Keyring::BANK_PREFIX => [
                Keyring::SIGNATURE_PREFIX_E => $signaturBlock(self::BANK_E_ROHBYTES, null),
                Keyring::SIGNATURE_PREFIX_X => $signaturBlock(self::BANK_X_ROHBYTES, null),
            ],
        ];
    }

    /** @return array<string, mixed> */
    public static function unvollstaendigOhneBankSignaturX(): array
    {
        $daten = self::vollstaendig();
        $daten[Keyring::BANK_PREFIX][Keyring::SIGNATURE_PREFIX_X]['PUBLIC_KEY'] = null;
        return $daten;
    }

    public static function schreibeDatei(string $pfad, array $daten, int $modus = 0600): void
    {
        $verzeichnis = dirname($pfad);
        if (!is_dir($verzeichnis)) {
            mkdir($verzeichnis, 0700, true);
        }
        file_put_contents($pfad, json_encode($daten, JSON_PRETTY_PRINT));
        chmod($pfad, $modus);
    }
}
