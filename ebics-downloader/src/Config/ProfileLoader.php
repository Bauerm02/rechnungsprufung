<?php

declare(strict_types=1);

namespace EbicsDownloader\Config;

use EbicsDownloader\Exceptions\ConfigValidationException;

/**
 * Lädt ein Profil aus einer JSON-Datei. Bewusst STRIKT: jedes fehlende
 * Pflichtfeld ist ein sofortiger Fehler (keine stillschweigenden
 * Defaults für Bankparameter). `load()` liefert ein geparstes Profil,
 * das auch bei enabled=false (z. B. das generische Beispiel voller
 * Platzhalter) erfolgreich geparst werden kann - erst `assertRunnable()`
 * entscheidet, ob tatsächlich ein Downloadversuch erlaubt ist.
 */
final class ProfileLoader
{
    public function load(string $path): ProfileConfig
    {
        if (!is_file($path)) {
            throw new ConfigValidationException("Profildatei nicht gefunden: $path");
        }
        $raw = file_get_contents($path);
        if ($raw === false) {
            throw new ConfigValidationException("Profildatei konnte nicht gelesen werden: $path");
        }
        /** @var mixed $data */
        $data = json_decode($raw, true);
        if (!is_array($data)) {
            throw new ConfigValidationException("Profildatei ist kein gültiges JSON-Objekt: $path");
        }
        return $this->fromArray($data);
    }

    public function fromArray(array $d): ProfileConfig
    {
        $req = function (array $d, string $key) {
            if (!array_key_exists($key, $d)) {
                throw new ConfigValidationException("Pflichtfeld '$key' fehlt in der Profilkonfiguration.");
            }
            return $d[$key];
        };
        $reqStr = function (array $d, string $key) use ($req): string {
            $v = $req($d, $key);
            if (!is_string($v) || trim($v) === '') {
                throw new ConfigValidationException("Pflichtfeld '$key' muss ein nicht-leerer String sein.");
            }
            return $v;
        };
        $optStr = static function (array $d, string $key): ?string {
            $v = $d[$key] ?? null;
            if ($v === null) {
                return null;
            }
            if (!is_string($v)) {
                throw new ConfigValidationException("Feld '$key' muss ein String oder null sein.");
            }
            return $v;
        };

        $keyring = $req($d, 'keyring');
        if (!is_array($keyring)) {
            throw new ConfigValidationException("'keyring' muss ein Objekt sein.");
        }

        $btdRaw = $req($d, 'btd');
        if (!is_array($btdRaw)) {
            throw new ConfigValidationException("'btd' muss ein Objekt sein.");
        }
        $btd = new BtfConfig(
            serviceName: $reqStr($btdRaw, 'service_name'),
            scope: $optStr($btdRaw, 'scope'),
            serviceOption: $optStr($btdRaw, 'service_option'),
            containerType: $optStr($btdRaw, 'container_type'),
            msgName: $reqStr($btdRaw, 'msg_name'),
            msgNameVariant: $optStr($btdRaw, 'msg_name_variant'),
            msgNameVersion: $optStr($btdRaw, 'msg_name_version'),
            msgNameFormat: $optStr($btdRaw, 'msg_name_format'),
        );

        $fdlRaw = $d['fdl_fallback'] ?? ['enabled' => false, 'file_format' => null];
        if (!is_array($fdlRaw)) {
            throw new ConfigValidationException("'fdl_fallback' muss ein Objekt sein.");
        }

        $fingerprintRaw = $req($d, 'bank_fingerprint_sha256');
        if (!is_array($fingerprintRaw)) {
            throw new ConfigValidationException("'bank_fingerprint_sha256' muss ein Objekt sein.");
        }

        $accountsRaw = $req($d, 'allowed_accounts');
        if (!is_array($accountsRaw)) {
            throw new ConfigValidationException("'allowed_accounts' muss eine Liste sein.");
        }
        $accounts = [];
        foreach ($accountsRaw as $entry) {
            if (!is_array($entry)) {
                throw new ConfigValidationException("Jeder Eintrag in 'allowed_accounts' muss ein Objekt sein.");
            }
            $accounts[] = new AllowedAccount(
                objekt: $reqStr($entry, 'objekt'),
                iban: $reqStr($entry, 'iban'),
            );
        }

        $blockedRaw = $d['blocked_objekte'] ?? [];
        if (!is_array($blockedRaw)) {
            throw new ConfigValidationException("'blocked_objekte' muss eine Liste sein.");
        }

        $limitsRaw = $req($d, 'limits');
        if (!is_array($limitsRaw)) {
            throw new ConfigValidationException("'limits' muss ein Objekt sein.");
        }
        $reqInt = static function (array $d, string $key) use ($req): int {
            $v = $req($d, $key);
            if (!is_int($v)) {
                throw new ConfigValidationException("Feld '$key' muss eine Ganzzahl sein.");
            }
            return $v;
        };
        $limits = new LimitsConfig(
            maxResponseBytes: $reqInt($limitsRaw, 'max_response_bytes'),
            maxZipEntries: $reqInt($limitsRaw, 'max_zip_entries'),
            maxEntryBytes: $reqInt($limitsRaw, 'max_entry_bytes'),
            httpTimeoutSeconds: $reqInt($limitsRaw, 'http_timeout_seconds'),
            maxRetries: $reqInt($limitsRaw, 'max_retries'),
            lockTimeoutSeconds: $reqInt($limitsRaw, 'lock_timeout_seconds'),
        );

        $tlsRaw = $d['tls'] ?? [];
        if (!is_array($tlsRaw)) {
            throw new ConfigValidationException("'tls' muss ein Objekt sein.");
        }

        return new ProfileConfig(
            profileName: $reqStr($d, 'profile'),
            enabled: (bool) ($d['enabled'] ?? false),
            ebicsVersion: $reqStr($d, 'ebics_version'),
            hostId: $reqStr($d, 'host_id'),
            bankUrl: $reqStr($d, 'bank_url'),
            partnerId: $reqStr($d, 'partner_id'),
            userId: $reqStr($d, 'user_id'),
            bankFingerprintSignatureX: $reqStr($fingerprintRaw, 'signature_x'),
            bankFingerprintSignatureE: $reqStr($fingerprintRaw, 'signature_e'),
            keyringPath: $reqStr($keyring, 'path'),
            keyringPassphraseFile: $reqStr($keyring, 'passphrase_file'),
            btd: $btd,
            fdlFallbackEnabled: (bool) ($fdlRaw['enabled'] ?? false),
            fdlFileFormat: $optStr($fdlRaw, 'file_format'),
            allowedAccounts: $accounts,
            blockedObjekte: array_map('strval', $blockedRaw),
            archiveDir: $reqStr($d, 'archive_dir'),
            quarantineDir: $reqStr($d, 'quarantine_dir'),
            releaseDir: $reqStr($d, 'release_dir'),
            ledgerPath: $reqStr($d, 'ledger_path'),
            lockFile: $reqStr($d, 'lock_file'),
            limits: $limits,
            tlsVerifyPeer: (bool) ($tlsRaw['verify_peer'] ?? false),
            tlsVerifyHost: (bool) ($tlsRaw['verify_host'] ?? false),
            tlsAllowRedirects: (bool) ($tlsRaw['allow_redirects'] ?? true),
            tlsCaBundlePath: $optStr($tlsRaw, 'ca_bundle_path'),
        );
    }

    /**
     * Verweigert einen echten Downloadversuch, solange das Profil
     * deaktiviert ist ODER noch Platzhalterwerte enthält ODER die
     * strukturelle Validierung fehlschlägt. Wird IMMER vor jedem
     * tatsächlichen EBICS-Aufruf durchlaufen (siehe Cli\DownloadCommand) -
     * eine reine `--check-config`-Prüfung ruft NUR `validateStructure()`
     * auf und toleriert enabled=false/Platzhalter, um das generische
     * Beispiel gefahrlos gegenprüfen zu können.
     */
    public function assertRunnable(ProfileConfig $config): void
    {
        if (!$config->enabled) {
            throw new ConfigValidationException(
                "Profil '{$config->profileName}' ist deaktiviert (enabled=false) - kein Downloadversuch. "
                . 'Explizite Aktivierung ist eine bewusste, manuelle Entscheidung des Betreibers.'
            );
        }
        $platzhalter = $config->findePlatzhalter();
        if ($platzhalter !== []) {
            throw new ConfigValidationException(
                "Profil '{$config->profileName}' enthält noch unausgefüllte Platzhalterwerte: "
                . implode(', ', $platzhalter) . ' - kein Downloadversuch mit Beispieldaten.'
            );
        }
        $config->validateStructure();
    }
}
