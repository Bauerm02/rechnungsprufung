<?php

declare(strict_types=1);

namespace EbicsDownloader\Archive;

use RuntimeException;

/**
 * Legt Rohbytes (die unveränderte, transportentschlüsselte Lieferung -
 * VOR jeder eigenen Camt-Interpretation) dauerhaft, atomar und mit
 * restriktiven Rechten ab, zusammen mit ihrem SHA-256-Fingerprint.
 *
 * "Atomar" heisst hier konkret: in eine temporäre Datei IM SELBEN
 * Verzeichnis schreiben, fsync(), chmod 0600, dann rename() auf den
 * endgültigen Namen - rename() ist auf demselben Dateisystem eine
 * einzelne atomare Operation, ein Absturz mitten im Schreiben kann daher
 * nie eine halb geschriebene Datei unter dem Zielnamen hinterlassen.
 *
 * Diese Klasse trifft KEINE Entscheidung darüber, ob eine Lieferung
 * business-seitig gültig ist (das entscheidet Camt\StatementSplitter
 * danach) - sie archiviert bewusst auch inhaltlich zurückgewiesene
 * Lieferungen (siehe DeliveryRejectedException-Doku), weil das Original
 * für eine spätere menschliche Klärung erhalten bleiben muss.
 */
final class RawArchiver
{
    public function __construct(private readonly string $directory)
    {
    }

    /**
     * @return array{path: string, sha256: string, bytes: int}
     */
    public function archiviere(string $rohinhalt, string $dateiPraefix): array
    {
        if (!is_dir($this->directory) && !mkdir($this->directory, 0700, true) && !is_dir($this->directory)) {
            throw new RuntimeException("Archivverzeichnis konnte nicht angelegt werden: {$this->directory}");
        }

        $sha256 = hash('sha256', $rohinhalt);
        $zielname = sprintf(
            '%s_%s_%s.raw',
            $dateiPraefix,
            gmdate('Ymd\THis\Z'),
            substr($sha256, 0, 16)
        );
        $zielpfad = rtrim($this->directory, '/') . '/' . $zielname;

        if (is_file($zielpfad)) {
            // Identischer Dateiname (gleicher Präfix+Zeitpunkt+Hash-Präfix)
            // existiert bereits - kein stilles Überschreiben irgendeiner
            // bereits archivierten Rohdatei.
            return ['path' => $zielpfad, 'sha256' => $sha256, 'bytes' => strlen($rohinhalt)];
        }

        $tmpPfad = $zielpfad . '.tmp-' . bin2hex(random_bytes(8));
        $handle = fopen($tmpPfad, 'xb');
        if ($handle === false) {
            throw new RuntimeException("Temporäre Archivdatei konnte nicht angelegt werden: {$tmpPfad}");
        }
        try {
            if (fwrite($handle, $rohinhalt) === false) {
                throw new RuntimeException("Schreiben der Archivdatei fehlgeschlagen: {$tmpPfad}");
            }
            fflush($handle);
            fsync($handle);
        } finally {
            fclose($handle);
        }
        chmod($tmpPfad, 0600);

        if (!rename($tmpPfad, $zielpfad)) {
            @unlink($tmpPfad);
            throw new RuntimeException("Archivdatei konnte nicht final abgelegt werden: {$zielpfad}");
        }

        return ['path' => $zielpfad, 'sha256' => $sha256, 'bytes' => strlen($rohinhalt)];
    }
}
