<?php

declare(strict_types=1);

namespace EbicsDownloader\Camt;

/**
 * Ergebnis der Prüfung EINES einzelnen `Stmt`-Blocks: entweder freigegeben
 * (whitelisted Objekt/IBAN, strukturell eindeutig) oder quarantiniert
 * (unbekanntes/fremdes/gesperrtes Konto oder strukturell nicht eindeutig
 * zuordenbar). Trägt NIE Rohbuchungstexte oder die volle IBAN in Feldern,
 * die später in einem allgemeinen Status-Manifest landen könnten - siehe
 * `Archive\ManifestWriter` für die Maskierung.
 */
final class StatementOutcome
{
    private function __construct(
        public readonly bool $freigegeben,
        public readonly ?string $objekt,
        public readonly ?string $iban,
        public readonly ?string $auszugsnummer,
        public readonly string $xml,
        public readonly string $kanonischerInhaltsHash,
        public readonly ?string $ablehnungsgrund,
    ) {
    }

    public static function freigegeben(string $objekt, string $iban, ?string $auszugsnummer, string $xml): self
    {
        return new self(true, $objekt, $iban, $auszugsnummer, $xml, self::kanonischerHash($xml), null);
    }

    public static function quarantaene(?string $iban, ?string $auszugsnummer, string $xml, string $grund): self
    {
        return new self(false, null, $iban, $auszugsnummer, $xml, self::kanonischerHash($xml), $grund);
    }

    private static function kanonischerHash(string $xml): string
    {
        return hash('sha256', $xml);
    }
}
