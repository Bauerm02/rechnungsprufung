<?php

declare(strict_types=1);

namespace EbicsDownloader\Config;

use EbicsDownloader\Exceptions\ConfigValidationException;

/**
 * Ein vollständig geparstes, aber noch NICHT auf Ausführbarkeit
 * geprüftes Bankprofil (ein Profil = ein EBICS-Teilnehmer/eine Bank-
 * verbindung mit eigenen Schlüsseln, eigenem Archiv, eigener Sperre,
 * eigenem Fingerprint - siehe Nutzerauftrag: "mehrere voneinander
 * getrennte Teilnehmer/Bankprofile"). `enabled=false` ist der Default
 * in jedem Beispiel; ProfileLoader::assertRunnable() erzwingt zusätzlich
 * das Fehlen jeglicher PLATZHALTER-Werte, bevor ein echter Download
 * versucht werden darf."""
 */
final class ProfileConfig
{
    /** @param AllowedAccount[] $allowedAccounts */
    /** @param string[] $blockedObjekte */
    public function __construct(
        public readonly string $profileName,
        public readonly bool $enabled,
        public readonly string $ebicsVersion,
        public readonly string $hostId,
        public readonly string $bankUrl,
        public readonly string $partnerId,
        public readonly string $userId,
        public readonly string $bankFingerprintSignatureX,
        public readonly string $bankFingerprintSignatureE,
        public readonly string $keyringPath,
        public readonly string $keyringPassphraseFile,
        public readonly BtfConfig $btd,
        public readonly bool $fdlFallbackEnabled,
        public readonly ?string $fdlFileFormat,
        public readonly array $allowedAccounts,
        public readonly array $blockedObjekte,
        public readonly string $archiveDir,
        public readonly string $quarantineDir,
        public readonly string $releaseDir,
        public readonly string $ledgerPath,
        public readonly string $lockFile,
        public readonly LimitsConfig $limits,
        public readonly bool $tlsVerifyPeer,
        public readonly bool $tlsVerifyHost,
        public readonly bool $tlsAllowRedirects,
        public readonly ?string $tlsCaBundlePath,
    ) {
    }

    /**
     * Objekt 107 ist IMMER gesperrt - unabhängig davon, was in der Datei
     * steht (Verteidigung in der Tiefe: die Whitelist allein müsste das
     * bereits ausschließen, da 107 nie in `allowed_accounts` stehen darf;
     * dieser zusätzliche, hartkodierte Check verlässt sich nicht allein
     * auf eine korrekt gepflegte Konfigurationsdatei).
     */
    public const IMMER_GESPERRTES_OBJEKT = '107';

    public function isObjektErlaubt(string $objekt): bool
    {
        if ($objekt === self::IMMER_GESPERRTES_OBJEKT) {
            return false;
        }
        foreach ($this->allowedAccounts as $account) {
            if ($account->objekt === $objekt) {
                return true;
            }
        }
        return false;
    }

    public function ibanFuerObjekt(string $objekt): ?string
    {
        if (!$this->isObjektErlaubt($objekt)) {
            return null;
        }
        foreach ($this->allowedAccounts as $account) {
            if ($account->objekt === $objekt) {
                return $account->iban;
            }
        }
        return null;
    }

    /** @return string[] normalisierte (Großschreibung, ohne Leerzeichen) erlaubte IBANs */
    public function erlaubteIbansNormalisiert(): array
    {
        return array_map(
            static fn (AllowedAccount $a): string => self::normalisiereIban($a->iban),
            $this->allowedAccounts,
        );
    }

    /**
     * Kehrfunktion zu `ibanFuerObjekt()`: liefert das Objekt zu einer
     * (bereits oder noch nicht normalisierten) IBAN, oder null, wenn die
     * IBAN zu keinem freigegebenen Objekt gehört. Wird von
     * `Camt\StatementSplitter` verwendet, um ein einzelnes, bereits
     * strukturell eindeutiges Statement der Kundensammeldatei einem
     * Objekt zuzuordnen oder andernfalls zu quarantänieren.
     */
    public function objektFuerIban(string $iban): ?string
    {
        $ibanNorm = self::normalisiereIban($iban);
        foreach ($this->allowedAccounts as $account) {
            if (self::normalisiereIban($account->iban) === $ibanNorm) {
                return $account->objekt;
            }
        }
        return null;
    }

    private static function normalisiereIban(string $iban): string
    {
        return strtoupper(str_replace(' ', '', $iban));
    }

    /**
     * Findet jeden String-Wert in der Rohkonfiguration, der noch ein
     * unausgefüllter Platzhalter ist (Konvention: enthält "PLATZHALTER"
     * oder "PLACEHOLDER", Groß-/Kleinschreibung egal). Wird von
     * ProfileLoader::assertRunnable() vor jedem echten Downloadversuch
     * aufgerufen - niemals bei einer bloßen Konfigurationsprüfung mit
     * enabled=false, da das generische Beispiel absichtlich nur aus
     * Platzhaltern besteht.
     */
    public function findePlatzhalter(): array
    {
        $gefunden = [];
        $pruefen = function (string $pfad, $wert) use (&$gefunden, &$pruefen): void {
            if (is_string($wert)) {
                if (preg_match('/PLATZHALTER|PLACEHOLDER/i', $wert) === 1) {
                    $gefunden[] = $pfad;
                }
                return;
            }
            if (is_array($wert)) {
                foreach ($wert as $key => $unterwert) {
                    $pruefen($pfad . '.' . $key, $unterwert);
                }
            }
        };
        foreach (get_object_vars($this) as $feld => $wert) {
            if ($wert instanceof BtfConfig || $wert instanceof LimitsConfig) {
                foreach (get_object_vars($wert) as $unterfeld => $unterwert) {
                    $pruefen("$feld.$unterfeld", $unterwert);
                }
                continue;
            }
            if (is_array($wert)) {
                foreach ($wert as $index => $eintrag) {
                    if ($eintrag instanceof AllowedAccount) {
                        $pruefen("$feld.$index.objekt", $eintrag->objekt);
                        $pruefen("$feld.$index.iban", $eintrag->iban);
                        continue;
                    }
                    $pruefen("$feld.$index", $eintrag);
                }
                continue;
            }
            $pruefen($feld, $wert);
        }
        return $gefunden;
    }

    public function validateStructure(): void
    {
        if (!in_array($this->ebicsVersion, ['H005', 'H004'], true)) {
            throw new ConfigValidationException(
                "ebics_version muss 'H005' (EBICS 3.0/BTD, Standardweg) oder 'H004' (EBICS 2.5/FDL, nur wenn "
                . "real unterstützt und bankbestätigt) sein, war '{$this->ebicsVersion}'."
            );
        }
        if ($this->ebicsVersion === 'H004' && !$this->fdlFallbackEnabled) {
            throw new ConfigValidationException(
                'ebics_version=H004 verlangt fdl_fallback.enabled=true (EBICS 2.5 ist nur ein explizit '
                . 'bestätigter Ausweichweg, kein impliziter Default).'
            );
        }
        if ($this->fdlFallbackEnabled && ($this->fdlFileFormat === null || trim($this->fdlFileFormat) === '')) {
            throw new ConfigValidationException('fdl_fallback.enabled=true verlangt ein gesetztes file_format.');
        }
        if (count($this->allowedAccounts) !== 3) {
            throw new ConfigValidationException(
                'allowed_accounts muss genau drei Konten enthalten (601, 616, 617) - war ' . count($this->allowedAccounts) . '.'
            );
        }
        $objekte = array_map(static fn (AllowedAccount $a): string => $a->objekt, $this->allowedAccounts);
        sort($objekte);
        if ($objekte !== ['601', '616', '617']) {
            throw new ConfigValidationException(
                "allowed_accounts muss exakt die Objekte 601, 616, 617 enthalten - war " . implode(', ', $objekte) . '.'
            );
        }
        if (in_array(self::IMMER_GESPERRTES_OBJEKT, $objekte, true)) {
            throw new ConfigValidationException('Objekt 107 darf niemals in allowed_accounts stehen.');
        }
        if (!in_array(self::IMMER_GESPERRTES_OBJEKT, $this->blockedObjekte, true)) {
            throw new ConfigValidationException('blocked_objekte muss Objekt 107 explizit enthalten.');
        }
        $ibans = $this->erlaubteIbansNormalisiert();
        if (count($ibans) !== count(array_unique($ibans))) {
            throw new ConfigValidationException('allowed_accounts enthält doppelte IBANs - nicht eindeutig zuordenbar.');
        }
        if (!$this->tlsVerifyPeer || !$this->tlsVerifyHost) {
            throw new ConfigValidationException('tls.verify_peer und tls.verify_host müssen beide true sein (TLS-Prüfung ist Pflicht).');
        }
        if ($this->tlsAllowRedirects) {
            throw new ConfigValidationException('tls.allow_redirects muss false sein - keine Redirects/unsicheren URLs.');
        }
        if (!str_starts_with(strtolower($this->bankUrl), 'https://')) {
            throw new ConfigValidationException('bank_url muss mit https:// beginnen.');
        }
        if ($this->limits->maxZipEntries < 1 || $this->limits->maxEntryBytes < 1 || $this->limits->maxResponseBytes < 1) {
            throw new ConfigValidationException('limits.* müssen positive Werte sein.');
        }
    }
}
