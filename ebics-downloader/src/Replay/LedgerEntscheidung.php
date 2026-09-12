<?php

declare(strict_types=1);

namespace EbicsDownloader\Replay;

/**
 * Ergebnis von `StatementLedger::pruefeUndErfasse()`:
 *
 * - NEU: erste bekannte Lieferung dieses (IBAN, Auszugsnummer) -
 *   Statement darf weitergereicht/freigegeben werden.
 * - REPLAY: identischer Inhalt (gleicher kanonischer Hash) wurde für
 *   dieses (IBAN, Auszugsnummer) bereits verarbeitet - NICHT erneut
 *   weiterreichen (keine doppelte Weitergabe bei Wiederanlauf/erneutem
 *   Abruf desselben Datumsfensters).
 * - KONFLIKT: dieselbe Auszugsnummer/IBAN wurde bereits mit einem
 *   ANDEREN Inhalt gesehen - kein stilles Überschreiben, harter Stopp
 *   zur manuellen Klärung.
 */
final class LedgerEntscheidung
{
    private function __construct(
        public readonly string $status,
        public readonly ?string $vorhandenerHash,
    ) {
    }

    public const NEU = 'NEU';
    public const REPLAY = 'REPLAY';
    public const KONFLIKT = 'KONFLIKT';

    public static function neu(): self
    {
        return new self(self::NEU, null);
    }

    public static function replay(): self
    {
        return new self(self::REPLAY, null);
    }

    public static function konflikt(string $vorhandenerHash): self
    {
        return new self(self::KONFLIKT, $vorhandenerHash);
    }
}
