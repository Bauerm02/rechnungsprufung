<?php

declare(strict_types=1);

namespace EbicsDownloader\Replay;

use DateTimeImmutable;

/**
 * Persistiert das zuletzt erfolgreich verarbeitete Datumsfenster-Ende
 * ("Watermark") als einfache ISO-8601-Datei. Wird von
 * `Cli\DownloadCommand` NUR dann fortgeschrieben, wenn die komplette
 * Lieferung dauerhaft archiviert UND geschäftlich validiert wurde (kein
 * Fortschreiben bei Ablehnung/Konflikt) - siehe Auftrag: "Datumsfenster
 * und Watermark nur bei vollständig persistierter/validierter Lieferung".
 */
final class DateWindowStore
{
    public function __construct(private readonly string $pfad)
    {
    }

    public function ladeLetztesBis(): ?DateTimeImmutable
    {
        if (!is_file($this->pfad)) {
            return null;
        }
        $inhalt = trim((string) file_get_contents($this->pfad));
        if ($inhalt === '') {
            return null;
        }
        $datum = DateTimeImmutable::createFromFormat(DateTimeImmutable::ATOM, $inhalt);
        return $datum !== false ? $datum : null;
    }

    public function schreibeBis(DateTimeImmutable $bis): void
    {
        $verzeichnis = dirname($this->pfad);
        if (!is_dir($verzeichnis) && !mkdir($verzeichnis, 0700, true) && !is_dir($verzeichnis)) {
            throw new \RuntimeException("Watermark-Verzeichnis konnte nicht angelegt werden: $verzeichnis");
        }
        $tmp = $this->pfad . '.tmp-' . bin2hex(random_bytes(8));
        file_put_contents($tmp, $bis->format(DateTimeImmutable::ATOM));
        chmod($tmp, 0600);
        rename($tmp, $this->pfad);
    }
}
