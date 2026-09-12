<?php

declare(strict_types=1);

namespace EbicsDownloader\Camt;

use EbicsDownloader\Config\ProfileConfig;

/**
 * Verarbeitet EINE vom Downloader bereits dauerhaft archivierte
 * Rohlieferung (siehe Archive\RawArchiver - die Archivierung passiert
 * IMMER, unabhängig vom Ergebnis dieser Klasse) zu einzelnen,
 * freigegebenen oder quarantänierten Statements.
 *
 * Wenn IRGENDEIN in einem ZIP-Container enthaltenes Dokument
 * strukturell beschädigt/unsicher ist (DTD/Entity, kaputtes XML,
 * dokumentweite Ntry-Verschachtelungsverletzung), wird die GESAMTE
 * Lieferung verworfen - bewusst kein Teilerfolg für die anderen,
 * vielleicht unauffälligen Dokumente derselben Sammellieferung ("eine
 * beschädigte Lieferung nicht als vollständig erfolgreich markieren").
 * Das Original bleibt trotzdem im Roharchiv erhalten.
 */
final class DeliveryProcessor
{
    public function __construct(
        private readonly XmlSafetyGuard $xmlGuard,
        private readonly StatementSplitter $splitter,
        private readonly SafeZipReader $zipReader,
    ) {
    }

    /** @return StatementOutcome[] */
    public function verarbeite(string $rohnutzlast, bool $istZipContainer, ProfileConfig $config): array
    {
        $dokumente = $istZipContainer
            ? $this->zipReader->entpackeSicher($rohnutzlast)
            : [$rohnutzlast];

        $ergebnisse = [];
        foreach ($dokumente as $xmlBytes) {
            $dom = $this->xmlGuard->parseSicher($xmlBytes);
            foreach ($this->splitter->trenne($dom, $config) as $outcome) {
                $ergebnisse[] = $outcome;
            }
        }
        return $ergebnisse;
    }
}
