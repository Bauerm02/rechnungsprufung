<?php

declare(strict_types=1);

namespace EbicsDownloader\Cli;

use EbicsDownloader\Config\ProfileLoader;
use EbicsDownloader\Exceptions\ConfigValidationException;

/**
 * Reiner Konfigurationscheck: lädt ein Profil und ruft AUSSCHLIESSLICH
 * `ProfileConfig::validateStructure()` auf - NICHT `assertRunnable()`.
 * Toleriert damit bewusst `enabled=false` und Platzhalterwerte, damit
 * das generische Beispielprofil (`config/profiles.example.json`)
 * gefahrlos gegengeprüft werden kann, ohne echte Bankdaten zu benötigen.
 * Löst NIEMALS einen echten EBICS-Kontakt aus.
 */
final class CheckConfigCommand
{
    public function __construct(private readonly ProfileLoader $loader = new ProfileLoader())
    {
    }

    /** @return int Exit-Code: 0 = strukturell gültig, 1 = ungültig. */
    public function ausfuehren(string $profilPfad): int
    {
        try {
            $config = $this->loader->load($profilPfad);
            $config->validateStructure();
        } catch (ConfigValidationException $e) {
            fwrite(STDERR, 'Konfiguration ungültig: ' . $e->getMessage() . PHP_EOL);
            return 1;
        }

        $status = $config->enabled ? 'aktiviert' : 'DEAKTIVIERT (enabled=false)';
        $platzhalter = $config->findePlatzhalter();
        fwrite(STDOUT, "Profil '{$config->profileName}' ist strukturell gültig ({$status}).\n");
        if ($platzhalter !== []) {
            fwrite(STDOUT, 'Enthält noch Platzhalterwerte (kein echter Downloadversuch möglich): '
                . implode(', ', $platzhalter) . "\n");
        }
        return 0;
    }
}
