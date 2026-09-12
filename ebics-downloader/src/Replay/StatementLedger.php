<?php

declare(strict_types=1);

namespace EbicsDownloader\Replay;

use RuntimeException;

/**
 * Persistentes Register gegen Replay/Konflikt, geschlüsselt über
 * (IBAN + gesetzliche Auszugsnummer) mit dem kanonischen Inhaltshash als
 * Wert - genau wie im Auftrag verlangt ("Dateihash + gesetzliche
 * Auszugsnummer/IBAN + kanonischer Bewegungs-/Saldeninhalt").
 *
 * Bekannte, dokumentierte Einschränkung: fehlt einem Statement die
 * `Stmt/Id` (Auszugsnummer) - laut ISO-20022-Schema optional, in der
 * Praxis aber immer von Banken gesetzt -, kann Replay/Konflikt für
 * dieses eine Statement NICHT über die Auszugsnummer erkannt werden.
 * In diesem Fall schlüsselt das Ledger stattdessen direkt über den
 * kanonischen Inhaltshash (`iban|NOID|<hash>`): eine exakt identische
 * erneute Lieferung wird weiterhin korrekt als Replay erkannt, aber
 * zwei INHALTLICH VERSCHIEDENE Statements ohne Auszugsnummer für
 * dasselbe Konto können nicht als Konflikt gegeneinander erkannt werden
 * (sie erhalten schlicht unterschiedliche Schlüssel). Das ist eine
 * bewusste, sichere Verschärfung (nie stillschweigend zusammenlegen),
 * aber keine vollständige Ersatzlösung für eine fehlende Auszugsnummer.
 *
 * Speicherung als einzelne JSON-Datei mit exklusiver Dateisperre
 * (`flock`) um die komplette Lese-Änder-Schreib-Sequenz - ausreichend
 * für einen Einzeloperator-Batch-Abruf (kein Mehrbenutzer-Datenbank-
 * betrieb vorgesehen).
 */
final class StatementLedger
{
    public function __construct(private readonly string $pfad)
    {
    }

    public function pruefeUndErfasse(string $iban, ?string $auszugsnummer, string $kanonischerHash): LedgerEntscheidung
    {
        $verzeichnis = dirname($this->pfad);
        if (!is_dir($verzeichnis) && !mkdir($verzeichnis, 0700, true) && !is_dir($verzeichnis)) {
            throw new RuntimeException("Ledger-Verzeichnis konnte nicht angelegt werden: $verzeichnis");
        }

        $handle = fopen($this->pfad, 'c+b');
        if ($handle === false) {
            throw new RuntimeException("Ledger-Datei konnte nicht geöffnet werden: {$this->pfad}");
        }

        try {
            if (!flock($handle, LOCK_EX)) {
                throw new RuntimeException("Ledger-Sperre konnte nicht erlangt werden: {$this->pfad}");
            }

            $daten = $this->lade($handle);
            $schluessel = $this->schluessel($iban, $auszugsnummer, $kanonischerHash);

            if (isset($daten[$schluessel])) {
                $vorhandenerHash = (string) $daten[$schluessel]['hash'];
                if (hash_equals($vorhandenerHash, $kanonischerHash)) {
                    return LedgerEntscheidung::replay();
                }
                return LedgerEntscheidung::konflikt($vorhandenerHash);
            }

            $daten[$schluessel] = [
                'iban' => $iban,
                'auszugsnummer' => $auszugsnummer,
                'hash' => $kanonischerHash,
                'erster_kontakt_utc' => gmdate('c'),
            ];
            $this->schreibe($handle, $daten);

            return LedgerEntscheidung::neu();
        } finally {
            flock($handle, LOCK_UN);
            fclose($handle);
            @chmod($this->pfad, 0600);
        }
    }

    private function schluessel(string $iban, ?string $auszugsnummer, string $kanonischerHash): string
    {
        if ($auszugsnummer === null || $auszugsnummer === '') {
            return $iban . '|NOID|' . $kanonischerHash;
        }
        return $iban . '|' . $auszugsnummer;
    }

    /** @return array<string, array{iban: string, auszugsnummer: ?string, hash: string, erster_kontakt_utc: string}> */
    private function lade($handle): array
    {
        $groesse = fstat($handle)['size'] ?? 0;
        if ($groesse === 0) {
            return [];
        }
        rewind($handle);
        $inhalt = stream_get_contents($handle);
        if ($inhalt === false || trim($inhalt) === '') {
            return [];
        }
        $daten = json_decode($inhalt, true);
        return is_array($daten) ? $daten : [];
    }

    private function schreibe($handle, array $daten): void
    {
        $json = json_encode($daten, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES);
        rewind($handle);
        ftruncate($handle, 0);
        fwrite($handle, (string) $json);
        fflush($handle);
    }
}
