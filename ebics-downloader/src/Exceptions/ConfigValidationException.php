<?php

declare(strict_types=1);

namespace EbicsDownloader\Exceptions;

use InvalidArgumentException;

/**
 * Das Profil enthält fehlende, unvollständige oder als PLATZHALTER
 * erkennbare Angaben - der Prozess weigert sich, mit unvollständiger
 * Konfiguration zu starten, statt österreichische Bankparameter (BTF-
 * Felder, Host/URL, Kontonummern) zu erraten.
 */
final class ConfigValidationException extends InvalidArgumentException
{
}
