<?php

declare(strict_types=1);

namespace EbicsDownloader\Camt;

use EbicsDownloader\Exceptions\DeliveryRejectedException;
use RuntimeException;
use ZipArchive;

/**
 * Ersetzt die bibliothekseigene `ZipArchiveExtractor`
 * (`vendor/ebics-api/ebics-client-php/src/Services/ZipArchiveExtractor.php`)
 * VOLLSTÄNDIG für diesen Downloadpfad: `BTDContext::setParserFormat()`
 * wird immer auf FILE_PARSER_FORMAT_TEXT gesetzt (siehe
 * Download\BtdDownloader), sodass die Bibliothek selbst NIE ein ZIP
 * entpackt. Diese Klasse ist die EINZIGE Stelle, die eine gelieferte
 * Datei als ZIP öffnet, und bietet zwei Stufen:
 *
 * - `pruefeMetadatenOhneEntpacken()`: liest NUR `numFiles`/`statIndex()`
 *   (kein Byte wird entpackt) und wird von `Download\BtdDownloader`
 *   INNERHALB des `ackClosure`-Hooks aufgerufen, also VOR der dauerhaften
 *   Archivierung und VOR der positiven Quittung an die Bank - eine
 *   offensichtlich überdimensionierte/deformierte Lieferung bekommt so
 *   schon an dieser frühen, günstigen Stelle eine klare Ablehnung.
 * - `entpackeSicher()`: wiederholt dieselbe Metadatenprüfung (Verteidigung
 *   in der Tiefe, kein Vertrauen auf einen bereits einmal bestandenen
 *   Check) und liest danach jeden Eintrag tatsächlich in den Speicher.
 *
 * Namen aus dem Archiv werden an KEINER Stelle als Dateisystempfad
 * verwendet (kein ZipSlip möglich); Einträge mit Verzeichnis-
 * Trennzeichen/".."-Segmenten oder einem Symlink-Unix-Modus im
 * external_attr-Feld werden hart abgelehnt.
 *
 * Bekannte, bewusst nicht geschlossene Restlücke (ehrlich dokumentiert
 * in README.md): `statIndex()['size']` ist die im ZIP-Zentralverzeichnis
 * DEKLARIERTE unkomprimierte Größe. Eine absichtlich fehlerhaft
 * konstruierte Archivdatei könnte hier lügen; `ext-zip` bietet keine
 * streamende Inflate-API mit hartem Byte-Cap während der Dekompression
 * selbst. Die Anzahl-/Größen-Vorprüfung wehrt den Normalfall (viele/
 * riesige valide Einträge) sicher ab, aber keine gezielt manipulierte
 * Zentralverzeichnis-Diskrepanz - deshalb die zusätzliche Nachprüfung
 * der TATSÄCHLICHEN Größe nach `getFromIndex()` in `entpackeSicher()`.
 */
final class SafeZipReader
{
    public function __construct(
        private readonly int $maxEntries,
        private readonly int $maxEntryBytes,
        private readonly int $maxTotalBytes,
    ) {
    }

    public function pruefeMetadatenOhneEntpacken(string $zipBytes): void
    {
        $this->mitGeoeffnetemArchiv($zipBytes, function (ZipArchive $zip): void {
            $this->pruefeMetadaten($zip);
        });
    }

    /**
     * @return string[] Roh-Inhalte in Archivreihenfolge - Namen aus dem
     *     Archiv werden bewusst NICHT zurückgegeben, damit ein Aufrufer
     *     sie nicht versehentlich als Dateiname weiterverwendet.
     */
    public function entpackeSicher(string $zipBytes): array
    {
        return $this->mitGeoeffnetemArchiv($zipBytes, function (ZipArchive $zip): array {
            $stats = $this->pruefeMetadaten($zip);

            $inhalte = [];
            foreach ($stats as $i => $stat) {
                $daten = $zip->getFromIndex($i);
                if ($daten === false) {
                    throw new DeliveryRejectedException("ZIP-Eintrag $i konnte nicht gelesen werden.");
                }
                if (strlen($daten) > $this->maxEntryBytes) {
                    // Verteidigung in der Tiefe: tatsächliche Größe nach dem
                    // Entpacken erneut gegen die deklarierte Größe prüfen.
                    throw new DeliveryRejectedException(
                        "ZIP-Eintrag $i: tatsächliche Größe nach Entpacken überschreitet das Limit."
                    );
                }
                $inhalte[] = $daten;
            }
            return $inhalte;
        });
    }

    /**
     * @template T
     * @param callable(ZipArchive): T $aktion
     * @return T
     */
    private function mitGeoeffnetemArchiv(string $zipBytes, callable $aktion)
    {
        $tmpDatei = tempnam(sys_get_temp_dir(), 'ebics-camt-zip-');
        if ($tmpDatei === false) {
            throw new RuntimeException('Temporäre Datei für ZIP-Prüfung konnte nicht angelegt werden.');
        }
        file_put_contents($tmpDatei, $zipBytes);
        chmod($tmpDatei, 0600);

        $zip = new ZipArchive();
        try {
            $status = $zip->open($tmpDatei);
            if ($status !== true) {
                throw new DeliveryRejectedException("ZIP-Archiv konnte nicht geöffnet werden (Code $status).");
            }
            return $aktion($zip);
        } finally {
            @$zip->close();
            @unlink($tmpDatei);
        }
    }

    /** @return array<int, array<string, mixed>> */
    private function pruefeMetadaten(ZipArchive $zip): array
    {
        $anzahl = $zip->numFiles;
        if ($anzahl > $this->maxEntries) {
            throw new DeliveryRejectedException(
                "ZIP-Archiv enthält $anzahl Einträge, erlaubt sind maximal {$this->maxEntries}."
            );
        }

        $stats = [];
        $gesamtgroesse = 0;
        for ($i = 0; $i < $anzahl; $i++) {
            $stat = $zip->statIndex($i);
            if ($stat === false) {
                throw new DeliveryRejectedException("ZIP-Eintrag $i: Metadaten konnten nicht gelesen werden.");
            }
            $name = (string) $stat['name'];
            if ($this->istUnsicherePfadangabe($name)) {
                throw new DeliveryRejectedException(
                    "ZIP-Eintrag mit unsicherem Namen abgelehnt: " . $this->maskiereName($name)
                );
            }
            // ZipArchive::statIndex() liefert in dieser ext-zip-Version
            // KEINEN 'external_attr'-Schlüssel (in dieser Sitzung geprüft:
            // ext-zip 1.22.7) - die Unix-Modusbits müssen separat über
            // getExternalAttributesIndex() gelesen werden.
            if ($zip->getExternalAttributesIndex($i, $opsys, $externalAttr) && $this->istSymlinkEintrag($externalAttr)) {
                throw new DeliveryRejectedException('ZIP-Eintrag ist ein Symlink - abgelehnt.');
            }
            $groesse = (int) $stat['size'];
            if ($groesse > $this->maxEntryBytes) {
                throw new DeliveryRejectedException(
                    "ZIP-Eintrag zu groß ($groesse Bytes), erlaubt sind maximal {$this->maxEntryBytes}."
                );
            }
            $gesamtgroesse += $groesse;
            if ($gesamtgroesse > $this->maxTotalBytes) {
                throw new DeliveryRejectedException(
                    "ZIP-Archiv überschreitet in Summe die erlaubte Gesamtgröße von {$this->maxTotalBytes} Bytes."
                );
            }
            $stats[] = $stat;
        }
        return $stats;
    }

    private function istUnsicherePfadangabe(string $name): bool
    {
        if ($name === '' || str_starts_with($name, '/') || str_starts_with($name, '\\')) {
            return true;
        }
        if (str_contains($name, '..')) {
            return true;
        }
        if (str_contains($name, '\\')) {
            return true;
        }
        return false;
    }

    private function istSymlinkEintrag(int $externalAttr): bool
    {
        $unixMode = ($externalAttr >> 16) & 0xFFFF;
        $s_iflnk = 0xA000;
        return $unixMode !== 0 && ($unixMode & 0xF000) === $s_iflnk;
    }

    private function maskiereName(string $name): string
    {
        return substr(preg_replace('/[^\x20-\x7E]/', '?', $name) ?? '', 0, 80);
    }
}
