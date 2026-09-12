<?php

declare(strict_types=1);

namespace EbicsDownloader\Exceptions;

use RuntimeException;

/**
 * Wird geworfen, wenn eine Vorbedingung fehlt, die niemals automatisch
 * hergestellt werden darf (fehlender/leerer Keyring, falsche Dateirechte,
 * abweichender Bank-Fingerprint, deaktiviertes Profil, unbekanntes
 * Konto). Der Prozess bricht sofort mit einem Exit-Code ungleich 0 ab -
 * es gibt an keiner Stelle im Downloadpfad einen automatischen
 * "repariere das selbst"-Pfad (kein INI/HIA/HPB/Reset/SPR/HCS, keine
 * Schlüsselerzeugung).
 */
final class HardStopException extends RuntimeException
{
}
