<?php

declare(strict_types=1);

namespace EbicsDownloader\Tests\Ordertype;

use PHPUnit\Framework\TestCase;

/**
 * Automatisierte Grep-Prüfung über den GESAMTEN `src/`-Baum: keine
 * Schlüsselerzeugungs-/Registrierungs-Order der Bibliothek
 * (`EbicsApi\Ebics\Orders\{INI,HIA,HPB,SPR,HCS}`) darf hier referenziert
 * werden - Auftrag: "niemals im Abruf automatisch Schlüssel erzeugen/
 * INI/HIA/HPB/Reset/SPR/HCS ausführen". Eine eigenständige "Reset"-
 * Order-Klasse existiert in der tatsächlichen Bibliothek 3.2.1 nicht
 * (geprüft: `vendor/ebics-api/ebics-client-php/src/Orders/` enthält
 * ausschließlich BTD/BTU/FDL/FUL/H3K/HAA/HAC/HCS/HEV/HIA/HKD/HPB/HPD/
 * HTD/INI/PTK/SPR) - ein Schlüssel-Reset läuft, falls die Bibliothek
 * ihn überhaupt unterstützt, über dieselben INI/HIA-Order-Klassen bzw.
 * `EbicsClient::changeKeyringPassword()`/`createUserSignatures()`
 * (siehe zweiter Test unten). Ein Fund hier ist IMMER ein Fehler,
 * unabhängig davon, ob er absichtlich oder versehentlich (z. B. über
 * eine IDE-Autovervollständigung) eingefügt wurde.
 */
final class OrderTypeAllowlistTest extends TestCase
{
    private const VERBOTENE_KLASSEN = [
        'EbicsApi\\Ebics\\Orders\\INI',
        'EbicsApi\\Ebics\\Orders\\HIA',
        'EbicsApi\\Ebics\\Orders\\HPB',
        'EbicsApi\\Ebics\\Orders\\SPR',
        'EbicsApi\\Ebics\\Orders\\HCS',
    ];

    public function testKeineVerbotenenOrderKlassenImQuellbaum(): void
    {
        $srcVerzeichnis = dirname(__DIR__, 2) . '/src';
        $treffer = [];

        $iterator = new \RecursiveIteratorIterator(new \RecursiveDirectoryIterator($srcVerzeichnis, \FilesystemIterator::SKIP_DOTS));
        foreach ($iterator as $datei) {
            if (!$datei->isFile() || $datei->getExtension() !== 'php') {
                continue;
            }
            $inhalt = file_get_contents($datei->getPathname());
            self::assertIsString($inhalt);
            foreach (self::VERBOTENE_KLASSEN as $verboten) {
                if (str_contains($inhalt, $verboten)) {
                    $treffer[] = $datei->getPathname() . ' enthält "' . $verboten . '"';
                }
            }
        }

        self::assertSame([], $treffer, 'Verbotene Order-/Schlüsselerzeugungsreferenz(en) gefunden: ' . implode('; ', $treffer));
    }

    public function testKeineSchluesselerzeugendenBibliotheksmethodenReferenziert(): void
    {
        // Reale gefährliche API-Oberfläche der Bibliothek (geprüft gegen
        // vendor/ebics-api/ebics-client-php/src/Contracts/EbicsClientInterface.php):
        // INI/HIA/HPB/SPR/HCS laufen NICHT über eigene "sendXYZ"-Methoden,
        // sondern als InitializationOrder über executeInitializationOrder().
        $srcVerzeichnis = dirname(__DIR__, 2) . '/src';
        $verboteneMethoden = [
            'createUserSignatures',
            'generateIssuerCertificate',
            'executeInitializationOrder',
            'changeKeyringPassword',
            'InitializationOrderInterface',
        ];
        $treffer = [];

        $iterator = new \RecursiveIteratorIterator(new \RecursiveDirectoryIterator($srcVerzeichnis, \FilesystemIterator::SKIP_DOTS));
        foreach ($iterator as $datei) {
            if (!$datei->isFile() || $datei->getExtension() !== 'php') {
                continue;
            }
            $inhalt = file_get_contents($datei->getPathname());
            self::assertIsString($inhalt);
            foreach ($verboteneMethoden as $verboten) {
                if (str_contains($inhalt, $verboten)) {
                    $treffer[] = $datei->getPathname() . ' enthält "' . $verboten . '"';
                }
            }
        }

        self::assertSame([], $treffer, 'Verbotene schlüsselerzeugende Methode(n) referenziert: ' . implode('; ', $treffer));
    }
}
