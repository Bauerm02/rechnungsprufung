<?php

declare(strict_types=1);

namespace EbicsDownloader\Exceptions;

use RuntimeException;

/**
 * Eine tatsächlich vom Server empfangene Lieferung wird verworfen, weil
 * sie beschädigt, strukturell unerwartet, oder ein Sicherheitslimit
 * (Größe/Anzahl/Kontozuordnung) verletzt ist. WICHTIG: das Original wird
 * trotzdem privat archiviert/quarantiert (siehe Archive\RawArchiver) -
 * diese Exception verhindert nur die Weitergabe/Freigabe, nicht die
 * Aufbewahrung des Rohmaterials für die spätere Klärung. Eine so
 * abgelehnte Lieferung wird NIE als vollständig erfolgreich markiert.
 */
final class DeliveryRejectedException extends RuntimeException
{
}
