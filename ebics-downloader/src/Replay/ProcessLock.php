<?php

declare(strict_types=1);

namespace EbicsDownloader\Replay;

use EbicsDownloader\Exceptions\HardStopException;

/**
 * Dateibasierte Prozesssperre (`flock`) gegen einen gleichzeitigen
 * zweiten Downloadversuch für dasselbe Profil - z. B. ein manueller
 * Aufruf während ein Cron-/Timer-Lauf noch läuft, oder ein doppelter
 * Wiederanlauf nach einem Absturz. Non-blocking mit kurzer Wartezeit
 * (`limits.lock_timeout_seconds`): ein zweiter Aufruf bricht mit einem
 * klaren Fehler ab, statt endlos zu warten oder unbemerkt parallel zu
 * laufen.
 */
final class ProcessLock
{
    /** @var resource|null */
    private $handle = null;

    public function __construct(private readonly string $pfad, private readonly int $timeoutSekunden)
    {
    }

    public function erlangen(): void
    {
        $verzeichnis = dirname($this->pfad);
        if (!is_dir($verzeichnis) && !mkdir($verzeichnis, 0700, true) && !is_dir($verzeichnis)) {
            throw new HardStopException("Sperrverzeichnis konnte nicht angelegt werden: $verzeichnis");
        }

        $handle = fopen($this->pfad, 'c');
        if ($handle === false) {
            throw new HardStopException("Sperrdatei konnte nicht geöffnet werden: {$this->pfad}");
        }

        $start = time();
        do {
            if (flock($handle, LOCK_EX | LOCK_NB)) {
                $this->handle = $handle;
                ftruncate($handle, 0);
                fwrite($handle, (string) getmypid());
                fflush($handle);
                return;
            }
            usleep(100_000);
        } while (time() - $start < $this->timeoutSekunden);

        fclose($handle);
        throw new HardStopException(
            "Ein anderer Downloadlauf für dieses Profil läuft bereits (Sperre {$this->pfad} nicht erlangbar "
            . "innerhalb {$this->timeoutSekunden}s) - kein paralleler zweiter Abruf."
        );
    }

    public function freigeben(): void
    {
        if ($this->handle !== null) {
            flock($this->handle, LOCK_UN);
            fclose($this->handle);
            $this->handle = null;
        }
    }
}
