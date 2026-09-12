<?php

declare(strict_types=1);

namespace EbicsDownloader\Archive;

/**
 * Schreibt EINEN Statuseintrag pro verarbeitetem Statement in ein
 * append-only JSON-Lines-Manifest im ALLGEMEINEN (nicht privaten)
 * Statusverzeichnis. Enthält bewusst NIE Rohbuchungstexte oder die
 * volle IBAN - nur maskierte Kennungen, damit dieses Manifest gefahrlos
 * für einen Statusüberblick gelesen werden kann, ohne selbst wie eine
 * private Kontoauskunft geschützt werden zu müssen. Die vollständigen
 * Daten liegen ausschließlich im privaten Roharchiv/der Quarantäne
 * (0600, außerhalb dieses Manifests).
 */
final class ManifestWriter
{
    public function __construct(private readonly string $manifestPfad)
    {
    }

    /** @param array<string, scalar|null> $eintrag */
    public function anhaengen(array $eintrag): void
    {
        $verzeichnis = dirname($this->manifestPfad);
        if (!is_dir($verzeichnis) && !mkdir($verzeichnis, 0700, true) && !is_dir($verzeichnis)) {
            throw new \RuntimeException("Manifestverzeichnis konnte nicht angelegt werden: $verzeichnis");
        }

        $eintrag['zeitpunkt_utc'] = gmdate('c');
        $zeile = json_encode($eintrag, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE) . "\n";

        $handle = fopen($this->manifestPfad, 'ab');
        if ($handle === false) {
            throw new \RuntimeException("Manifest konnte nicht geöffnet werden: {$this->manifestPfad}");
        }
        try {
            if (flock($handle, LOCK_EX)) {
                fwrite($handle, $zeile);
                fflush($handle);
                flock($handle, LOCK_UN);
            }
        } finally {
            fclose($handle);
        }
        @chmod($this->manifestPfad, 0600);
    }

    public static function maskiereIban(?string $iban): ?string
    {
        if ($iban === null || $iban === '') {
            return $iban;
        }
        $normalisiert = strtoupper(str_replace(' ', '', $iban));
        $laenge = strlen($normalisiert);
        if ($laenge <= 8) {
            return str_repeat('*', $laenge);
        }
        return substr($normalisiert, 0, 4) . str_repeat('*', $laenge - 8) . substr($normalisiert, -4);
    }
}
