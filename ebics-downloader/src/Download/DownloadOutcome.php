<?php

declare(strict_types=1);

namespace EbicsDownloader\Download;

/**
 * Ergebnis EINES EBICS-Downloadversuchs. `akzeptiert=true` bedeutet
 * ausschließlich: die Rohbytes wurden dauerhaft archiviert und der Bank
 * wurde eine POSITIVE Quittung gesendet (Transportebene) - das sagt
 * NICHTS darüber aus, ob der Inhalt später geschäftlich verwertbar ist
 * (fremde Konten, beschädigte Struktur). Diese bewusste Trennung
 * zwischen "Bank-Ebene: erfolgreich empfangen" und "Fachebene:
 * verwertbar" ist zentral (siehe Camt\DeliveryProcessor für die
 * Fachprüfung, die IMMER erst NACH einem akzeptierten Download läuft).
 */
final class DownloadOutcome
{
    private function __construct(
        public readonly bool $akzeptiert,
        public readonly ?string $archivPfad,
        public readonly ?string $sha256,
        public readonly ?int $bytes,
        public readonly ?string $rohinhalt,
        public readonly ?string $ablehnungsgrund,
    ) {
    }

    public static function akzeptiert(string $archivPfad, string $sha256, int $bytes, string $rohinhalt): self
    {
        return new self(true, $archivPfad, $sha256, $bytes, $rohinhalt, null);
    }

    public static function abgelehnt(string $grund): self
    {
        return new self(false, null, null, null, null, $grund);
    }
}
