<?php

declare(strict_types=1);

namespace EbicsDownloader\Tests\Config;

/**
 * Baut ein strukturell vollständiges, GÜLTIGES (aber rein synthetisches)
 * Profil-Array als Ausgangspunkt für Config-Tests - jeder Test mutiert
 * gezielt EIN Feld, um eine bestimmte Ablehnung zu provozieren.
 */
final class ProfileFixture
{
    /** @return array<string, mixed> */
    public static function gueltig(): array
    {
        return [
            'profile' => 'test-profil',
            'enabled' => true,
            'ebics_version' => 'H005',
            'host_id' => 'TESTHOST',
            'bank_url' => 'https://bank.example.invalid/ebics',
            'partner_id' => 'PARTNER1',
            'user_id' => 'USER1',
            'bank_fingerprint_sha256' => [
                'signature_x' => str_repeat('a', 64),
                'signature_e' => str_repeat('b', 64),
            ],
            'keyring' => [
                'path' => '/tmp/does-not-matter-keyring.json',
                'passphrase_file' => '/tmp/does-not-matter-passphrase.txt',
            ],
            'btd' => [
                'service_name' => 'SVCNAME',
                'scope' => 'AT',
                'service_option' => null,
                'container_type' => 'ZIP',
                'msg_name' => 'camt.053',
                'msg_name_variant' => null,
                'msg_name_version' => '04',
                'msg_name_format' => 'xml',
            ],
            'fdl_fallback' => [
                'enabled' => false,
                'file_format' => null,
            ],
            'allowed_accounts' => [
                ['objekt' => '601', 'iban' => 'AT000000000000000601'],
                ['objekt' => '616', 'iban' => 'AT000000000000000616'],
                ['objekt' => '617', 'iban' => 'AT000000000000000617'],
            ],
            'blocked_objekte' => ['107'],
            'archive_dir' => '/tmp/ebics-test/archiv',
            'quarantine_dir' => '/tmp/ebics-test/quarantaene',
            'release_dir' => '/tmp/ebics-test/freigegeben',
            'ledger_path' => '/tmp/ebics-test/ledger.json',
            'lock_file' => '/tmp/ebics-test/lock',
            'limits' => [
                'max_response_bytes' => 1000000,
                'max_zip_entries' => 10,
                'max_entry_bytes' => 500000,
                'http_timeout_seconds' => 30,
                'max_retries' => 0,
                'lock_timeout_seconds' => 5,
            ],
            'tls' => [
                'verify_peer' => true,
                'verify_host' => true,
                'allow_redirects' => false,
                'ca_bundle_path' => null,
            ],
        ];
    }
}
