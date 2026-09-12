<?php

declare(strict_types=1);

namespace EbicsDownloader\Download;

/**
 * Reines Transportobjekt, damit die `ackClosure` (die die Bibliothek als
 * `callable` aufruft und deren Rückgabewert NUR als bool ausgewertet
 * wird) ihr eigentliches Ergebnis trotzdem an den Aufrufer von
 * `EbicsStatementDownloader::herunterladen()` zurückgeben kann.
 */
final class DownloadOutcomeHolder
{
    public ?DownloadOutcome $outcome = null;
}
