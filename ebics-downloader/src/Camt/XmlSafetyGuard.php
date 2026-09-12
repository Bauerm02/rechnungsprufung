<?php

declare(strict_types=1);

namespace EbicsDownloader\Camt;

use DOMDocument;
use EbicsDownloader\Exceptions\DeliveryRejectedException;

/**
 * Erste Verteidigungslinie gegen präparierte XML-Dateien, BEVOR
 * irgendein XML-Parser (unserer oder ein fremder) den Inhalt überhaupt
 * sieht: ein reiner Byte-Scan lehnt jedes Vorkommen von `<!DOCTYPE` oder
 * `<!ENTITY` sofort ab. Das ist bewusst redundant zu den DOMDocument-
 * Ladeoptionen in `parseSicher()` (kein Netzwerk, keine externen DTDs,
 * keine Entity-Substitution) - moderne libxml-Versionen deaktivieren
 * externe Entities zwar standardmäßig, aber diese Klasse verlässt sich
 * NICHT allein auf eine Bibliotheksvoreinstellung, die sich zwischen
 * PHP-/libxml-Builds unterscheiden kann.
 */
final class XmlSafetyGuard
{
    public function pruefeUnsicherenInhaltAb(string $xmlBytes): void
    {
        if (preg_match('/<!\s*DOCTYPE/i', $xmlBytes) === 1) {
            throw new DeliveryRejectedException('XML enthält eine DOCTYPE-Deklaration - abgelehnt vor jedem Parsing.');
        }
        if (preg_match('/<!\s*ENTITY/i', $xmlBytes) === 1) {
            throw new DeliveryRejectedException('XML enthält eine ENTITY-Deklaration - abgelehnt vor jedem Parsing.');
        }
        if (preg_match('/<\?xml-stylesheet/i', $xmlBytes) === 1) {
            throw new DeliveryRejectedException('XML enthält eine xml-stylesheet-Verarbeitungsanweisung - abgelehnt.');
        }
    }

    public function parseSicher(string $xmlBytes): DOMDocument
    {
        $this->pruefeUnsicherenInhaltAb($xmlBytes);

        $vorherigeEinstellung = libxml_use_internal_errors(true);
        try {
            $dom = new DOMDocument();
            $dom->resolveExternals = false;
            $dom->substituteEntities = false;
            $dom->validateOnParse = false;
            $geladen = $dom->loadXML($xmlBytes, LIBXML_NONET | LIBXML_NOCDATA);
            if ($geladen === false) {
                $fehler = array_map(static fn ($e) => trim($e->message), libxml_get_errors());
                throw new DeliveryRejectedException(
                    'XML konnte nicht sicher geparst werden: ' . implode('; ', $fehler)
                );
            }
            return $dom;
        } finally {
            libxml_clear_errors();
            libxml_use_internal_errors($vorherigeEinstellung);
        }
    }
}
