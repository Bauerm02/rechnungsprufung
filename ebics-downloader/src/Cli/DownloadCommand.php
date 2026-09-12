<?php

declare(strict_types=1);

namespace EbicsDownloader\Cli;

use DateTimeImmutable;
use EbicsDownloader\Archive\ManifestWriter;
use EbicsDownloader\Archive\RawArchiver;
use EbicsDownloader\Camt\Camt053StatementValidator;
use EbicsDownloader\Camt\DeliveryProcessor;
use EbicsDownloader\Camt\SafeZipReader;
use EbicsDownloader\Camt\StatementOutcome;
use EbicsDownloader\Camt\StatementSplitter;
use EbicsDownloader\Camt\XmlSafetyGuard;
use EbicsDownloader\Config\ProfileConfig;
use EbicsDownloader\Config\ProfileLoader;
use EbicsDownloader\Download\EbicsStatementDownloader;
use EbicsDownloader\Exceptions\ConfigValidationException;
use EbicsDownloader\Exceptions\DeliveryRejectedException;
use EbicsDownloader\Exceptions\HardStopException;
use EbicsDownloader\Http\SizeCappedCurlHttpClientFactory;
use EbicsDownloader\Keyring\KeyringGuard;
use EbicsDownloader\Replay\DateWindowStore;
use EbicsDownloader\Replay\LedgerEntscheidung;
use EbicsDownloader\Replay\ProcessLock;
use EbicsDownloader\Replay\StatementLedger;
use EbicsApi\Ebics\Services\FileKeyringManager;
use Throwable;

/**
 * Der tatsächliche Downloadpfad: `ProfileLoader::assertRunnable()` MUSS
 * bestehen (deaktiviertes/Platzhalterprofil bricht hart ab), danach
 * Prozesssperre -> EBICS-Download (Transportquittung, siehe
 * `Download\EbicsStatementDownloader`) -> Camt-Sicherheits-/
 * Trennschicht -> Replay-/Konfliktprüfung je Statement -> Freigabe in
 * `release_dir` NUR für whitelisted, konfliktfreie, neue Statements.
 *
 * Trifft KEINE automatische Nachbuchung bei Mehrdeutigkeit: ein
 * KONFLIKT (siehe `Replay\LedgerEntscheidung`) wird IMMER in die
 * Quarantäne gelegt, nie stillschweigend überschrieben oder in
 * `release_dir` gemischt.
 */
final class DownloadCommand
{
    public function __construct(private readonly ProfileLoader $loader = new ProfileLoader())
    {
    }

    /** @param string[] $argv Restliche Argumente nach dem Profilpfad, z. B. ["--von=2026-01-01", "--bis=2026-01-31"]. */
    public function ausfuehren(string $profilPfad, array $argv = []): int
    {
        try {
            $config = $this->loader->load($profilPfad);
            $this->loader->assertRunnable($config);
        } catch (ConfigValidationException $e) {
            fwrite(STDERR, 'Konfiguration nicht ausführbar: ' . $e->getMessage() . PHP_EOL);
            return 1;
        }

        $lock = new ProcessLock($config->lockFile, $config->limits->lockTimeoutSeconds);
        try {
            $lock->erlangen();
        } catch (HardStopException $e) {
            fwrite(STDERR, $e->getMessage() . PHP_EOL);
            return 1;
        }

        try {
            return $this->downloadenUndVerarbeiten($config, $argv);
        } catch (HardStopException|DeliveryRejectedException $e) {
            fwrite(STDERR, get_class($e) . ': ' . $e->getMessage() . PHP_EOL);
            return 1;
        } catch (Throwable $e) {
            fwrite(STDERR, 'Unerwarteter Fehler: ' . $e->getMessage() . PHP_EOL);
            return 1;
        } finally {
            $lock->freigeben();
        }
    }

    private function downloadenUndVerarbeiten(ProfileConfig $config, array $argv): int
    {
        [$start, $ende] = $this->ermittleDatumsfenster($config, $argv);

        $keyringGuard = new KeyringGuard(new FileKeyringManager());
        $archiver = new RawArchiver($config->archiveDir);
        $httpFactory = new SizeCappedCurlHttpClientFactory();
        $downloader = new EbicsStatementDownloader($config, $keyringGuard, $archiver, $httpFactory);

        $ergebnis = $downloader->herunterladen($start, $ende);

        $manifest = new ManifestWriter($config->archiveDir . '/manifest.jsonl');

        if (!$ergebnis->akzeptiert) {
            $manifest->anhaengen([
                'ereignis' => 'download_abgelehnt',
                'profil' => $config->profileName,
                'grund' => $ergebnis->ablehnungsgrund,
            ]);
            fwrite(STDERR, 'Download abgelehnt: ' . $ergebnis->ablehnungsgrund . PHP_EOL);
            return 1;
        }

        $manifest->anhaengen([
            'ereignis' => 'download_archiviert',
            'profil' => $config->profileName,
            'sha256' => $ergebnis->sha256,
            'bytes' => $ergebnis->bytes,
            'archiv_pfad' => $ergebnis->archivPfad,
        ]);
        fwrite(STDOUT, "Rohlieferung archiviert: {$ergebnis->archivPfad} (SHA-256 {$ergebnis->sha256}).\n");

        $istZip = $config->btd->containerType !== null && strtoupper($config->btd->containerType) === 'ZIP';
        $processor = new DeliveryProcessor(
            new XmlSafetyGuard(),
            new StatementSplitter(new Camt053StatementValidator()),
            new SafeZipReader($config->limits->maxZipEntries, $config->limits->maxEntryBytes, $config->limits->maxResponseBytes),
        );

        try {
            $statements = $processor->verarbeite($ergebnis->rohinhalt, $istZip, $config);
        } catch (DeliveryRejectedException $e) {
            $manifest->anhaengen([
                'ereignis' => 'lieferung_vollstaendig_quarantaeniert',
                'profil' => $config->profileName,
                'grund' => $e->getMessage(),
            ]);
            fwrite(STDERR, 'Gesamte Lieferung quarantäniert (strukturell unsicher): ' . $e->getMessage() . PHP_EOL);
            return 1;
        }

        $ledger = new StatementLedger($config->ledgerPath);
        $konflikte = 0;
        $freigegeben = 0;
        $quarantaeniert = 0;

        foreach ($statements as $outcome) {
            $konflikte += $this->verarbeiteEinStatement($outcome, $config, $ledger, $manifest, $freigegeben, $quarantaeniert);
        }

        fwrite(STDOUT, "Statements: $freigegeben freigegeben, $quarantaeniert quarantäniert, $konflikte Konflikt(e).\n");

        if ($konflikte === 0) {
            $watermark = new DateWindowStore($config->archiveDir . '/watermark.txt');
            $watermark->schreibeBis($ende ?? new DateTimeImmutable());
        } else {
            fwrite(STDERR, "Watermark NICHT fortgeschrieben - mindestens ein Konflikt erfordert manuelle Klärung.\n");
        }

        return $konflikte === 0 ? 0 : 2;
    }

    private function verarbeiteEinStatement(
        StatementOutcome $outcome,
        ProfileConfig $config,
        StatementLedger $ledger,
        ManifestWriter $manifest,
        int &$freigegeben,
        int &$quarantaeniert,
    ): int {
        if (!$outcome->freigegeben) {
            $this->schreibeDatei($config->quarantineDir, $this->sichererDateiname('quarantaene', $outcome), $outcome->xml);
            $manifest->anhaengen([
                'ereignis' => 'statement_quarantaeniert',
                'iban_maskiert' => ManifestWriter::maskiereIban($outcome->iban),
                'auszugsnummer' => $outcome->auszugsnummer,
                'grund' => $outcome->ablehnungsgrund,
            ]);
            $quarantaeniert++;
            return 0;
        }

        $entscheidung = $ledger->pruefeUndErfasse($outcome->iban, $outcome->auszugsnummer, $outcome->kanonischerInhaltsHash);

        if ($entscheidung->status === LedgerEntscheidung::REPLAY) {
            $manifest->anhaengen([
                'ereignis' => 'statement_replay_uebersprungen',
                'objekt' => $outcome->objekt,
                'iban_maskiert' => ManifestWriter::maskiereIban($outcome->iban),
                'auszugsnummer' => $outcome->auszugsnummer,
            ]);
            return 0;
        }

        if ($entscheidung->status === LedgerEntscheidung::KONFLIKT) {
            $this->schreibeDatei($config->quarantineDir, $this->sichererDateiname('konflikt', $outcome), $outcome->xml);
            $manifest->anhaengen([
                'ereignis' => 'statement_konflikt',
                'objekt' => $outcome->objekt,
                'iban_maskiert' => ManifestWriter::maskiereIban($outcome->iban),
                'auszugsnummer' => $outcome->auszugsnummer,
                'vorhandener_hash' => $entscheidung->vorhandenerHash,
                'neuer_hash' => $outcome->kanonischerInhaltsHash,
            ]);
            return 1;
        }

        $this->schreibeDatei($config->releaseDir, $this->sichererDateiname('freigegeben', $outcome), $outcome->xml);
        $manifest->anhaengen([
            'ereignis' => 'statement_freigegeben',
            'objekt' => $outcome->objekt,
            'iban_maskiert' => ManifestWriter::maskiereIban($outcome->iban),
            'auszugsnummer' => $outcome->auszugsnummer,
        ]);
        $freigegeben++;
        return 0;
    }

    private function sichererDateiname(string $praefix, StatementOutcome $outcome): string
    {
        $objektTeil = $outcome->objekt ?? 'unbekannt';
        return sprintf('%s_%s_%s.xml', $praefix, preg_replace('/[^A-Za-z0-9_-]/', '_', $objektTeil), substr($outcome->kanonischerInhaltsHash, 0, 16));
    }

    private function schreibeDatei(string $verzeichnis, string $dateiname, string $inhalt): void
    {
        if (!is_dir($verzeichnis) && !mkdir($verzeichnis, 0700, true) && !is_dir($verzeichnis)) {
            throw new HardStopException("Verzeichnis konnte nicht angelegt werden: $verzeichnis");
        }
        $pfad = rtrim($verzeichnis, '/') . '/' . $dateiname;
        if (is_file($pfad)) {
            return;
        }
        $tmp = $pfad . '.tmp-' . bin2hex(random_bytes(8));
        file_put_contents($tmp, $inhalt);
        chmod($tmp, 0600);
        rename($tmp, $pfad);
    }

    /** @return array{0: ?DateTimeImmutable, 1: ?DateTimeImmutable} */
    private function ermittleDatumsfenster(ProfileConfig $config, array $argv): array
    {
        $von = $this->leseArgument($argv, '--von');
        $bis = $this->leseArgument($argv, '--bis');

        if ($von === null) {
            $watermark = new DateWindowStore($config->archiveDir . '/watermark.txt');
            $letztesBis = $watermark->ladeLetztesBis();
            if ($letztesBis !== null) {
                $von = $letztesBis->format('Y-m-d');
            }
        }

        if ($von === null || $bis === null) {
            // Kein vollständiges Fenster ermittelbar - kein Raten eines
            // Startdatums; die Bank liefert dann ihren eigenen Standard
            // (i. d. R. der jüngste verfügbare Kontoauszug).
            return [null, null];
        }

        return [new DateTimeImmutable($von), new DateTimeImmutable($bis)];
    }

    private function leseArgument(array $argv, string $name): ?string
    {
        foreach ($argv as $arg) {
            if (str_starts_with($arg, $name . '=')) {
                return substr($arg, strlen($name) + 1);
            }
        }
        return null;
    }
}
