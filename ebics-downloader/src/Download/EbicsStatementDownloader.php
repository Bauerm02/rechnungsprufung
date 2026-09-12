<?php

declare(strict_types=1);

namespace EbicsDownloader\Download;

use DateTimeInterface;
use EbicsApi\Ebics\Contexts\BTDContext;
use EbicsApi\Ebics\Contexts\FDLContext;
use EbicsApi\Ebics\Contexts\RequestContext;
use EbicsApi\Ebics\Contracts\EbicsClientInterface;
use EbicsApi\Ebics\EbicsClient;
use EbicsApi\Ebics\Models\Bank;
use EbicsApi\Ebics\Models\EbicsClientOptions;
use EbicsApi\Ebics\Models\Keyring;
use EbicsApi\Ebics\Models\User;
use EbicsApi\Ebics\Orders\BTD;
use EbicsApi\Ebics\Orders\FDL;
use EbicsDownloader\Archive\RawArchiver;
use EbicsDownloader\Config\ProfileConfig;
use EbicsDownloader\Exceptions\HardStopException;
use EbicsDownloader\Http\SizeCappedCurlHttpClientFactory;
use EbicsDownloader\Keyring\KeyringGuard;

/**
 * Führt GENAU EINEN EBICS-Downloadversuch (BTD/H005 oder FDL/H004-
 * Fallback) durch. Ruft ausschließlich `EbicsClient::executeDownloadOrder()`
 * auf - keine INI/HIA/HPB/Reset/SPR/HCS-Order wird an irgendeiner Stelle
 * dieser Klasse (oder sonst im `src/`-Baum, siehe
 * `tests/Ordertype/OrderTypeAllowlistTest.php`) referenziert.
 *
 * Kernentscheidung (siehe Klassendoku von `DownloadOutcome`): die
 * `ackClosure` (aufgerufen von der Bibliothek NACH Transportentschlüsselung/
 * -dekompression, aber VOR dem Senden der Quittung an die Bank - siehe
 * `vendor/ebics-api/ebics-client-php/src/EbicsClient.php::downloadTransaction()`)
 * prüft NUR die absolute Bytegrenze der entschlüsselten Nutzlast und
 * archiviert dann dauerhaft; jede feinere Prüfung (ZIP-Struktur, Stmt/
 * Acct/IBAN-Trennung) läuft bewusst ERST NACHHER in
 * `Camt\DeliveryProcessor`, nachdem die Bank bereits eine positive
 * Quittung erhalten hat. Diese Trennung ist beabsichtigt: die Quittung
 * bestätigt ausschließlich einen vollständigen, sicher gespeicherten
 * Transportempfang, NICHT die geschäftliche Verwertbarkeit.
 *
 * Bekannte, in README.md offen dokumentierte Restlücke: die
 * bibliothekseigene zlib-Dekompression (`downloadTransaction()`, VOR
 * `ackClosure`) läuft immer vollständig, bevor diese Klasse überhaupt
 * die Kontrolle bekommt. Ein absichtlich riesig komprimierter Payload
 * (Dekompressionsbombe) kann daher bereits vor der hier greifenden
 * Bytegrenzenprüfung einen Speicherspitzenwert verursachen. Die
 * `Http\SizeCappedCurlHttpClientFactory` grenzt die KOMPRIMIERTE
 * Übertragungsgröße vorgelagert ein; ein vollständiger Schutz vor einer
 * Dekompressionsbombe bei ansonsten unter dem Transportlimit liegender
 * Übertragungsgröße ist mit der öffentlichen API dieser Bibliotheksversion
 * nicht erreichbar.
 */
final class EbicsStatementDownloader
{
    public function __construct(
        private readonly ProfileConfig $config,
        private readonly KeyringGuard $keyringGuard,
        private readonly RawArchiver $archiver,
        private readonly SizeCappedCurlHttpClientFactory $httpFactory,
    ) {
    }

    public function herunterladen(?DateTimeInterface $start, ?DateTimeInterface $end): DownloadOutcome
    {
        $keyring = $this->ladeUndPruefeKeyring();

        $bank = new Bank($this->config->hostId, $this->config->bankUrl);
        $user = new User($this->config->partnerId, $this->config->userId);

        $options = new EbicsClientOptions();
        $options->setHttpClient($this->httpFactory->build($this->config));

        $client = new EbicsClient($bank, $user, $keyring, $options);

        $ergebnisHolder = new DownloadOutcomeHolder();
        $context = (new RequestContext())->setAckClosure($this->baueAckClosure($ergebnisHolder));

        $order = $this->baueOrder($context, $start, $end);

        $client->executeDownloadOrder($order);

        return $ergebnisHolder->outcome ?? DownloadOutcome::abgelehnt(
            'ackClosure wurde von der Bibliothek nicht aufgerufen - unerwarteter Zustand, keine Freigabe.'
        );
    }

    private function ladeUndPruefeKeyring(): Keyring
    {
        $passphrasePfad = $this->config->keyringPassphraseFile;
        if (!is_file($passphrasePfad)) {
            throw new HardStopException("Passphrase-Datei fehlt: $passphrasePfad - kein automatisches Anlegen.");
        }
        $passphrase = trim((string) file_get_contents($passphrasePfad));
        if ($passphrase === '') {
            throw new HardStopException("Passphrase-Datei ist leer: $passphrasePfad.");
        }

        $keyring = $this->keyringGuard->loadVerified($this->config->keyringPath, $passphrase, [
            'signature_x' => $this->config->bankFingerprintSignatureX,
            'signature_e' => $this->config->bankFingerprintSignatureE,
        ]);

        $erwarteteVersion = $this->config->ebicsVersion === 'H005' ? Keyring::VERSION_30 : Keyring::VERSION_25;
        if ($keyring->getVersion() !== $erwarteteVersion) {
            throw new HardStopException(
                "Keyring-Version ({$keyring->getVersion()}) passt nicht zur konfigurierten ebics_version "
                . "({$this->config->ebicsVersion}) - kein automatisches Anpassen, Klärung erforderlich."
            );
        }

        return $keyring;
    }

    private function baueAckClosure(DownloadOutcomeHolder $holder): callable
    {
        $maxBytes = $this->config->limits->maxResponseBytes;
        $archiver = $this->archiver;
        $profilName = $this->config->profileName;

        return function ($transaction) use ($holder, $maxBytes, $archiver, $profilName): bool {
            $rohinhalt = $transaction->getOrderData();
            $bytes = strlen($rohinhalt);

            if ($bytes > $maxBytes) {
                $holder->outcome = DownloadOutcome::abgelehnt(
                    "Entschlüsselte Nutzlast ($bytes Bytes) überschreitet das konfigurierte Limit "
                    . "($maxBytes Bytes) - NICHT archiviert, NICHT quittiert."
                );
                return false;
            }

            $archivErgebnis = $archiver->archiviere($rohinhalt, $profilName);
            $holder->outcome = DownloadOutcome::akzeptiert(
                $archivErgebnis['path'],
                $archivErgebnis['sha256'],
                $archivErgebnis['bytes'],
                $rohinhalt,
            );
            return true;
        };
    }

    private function baueOrder(RequestContext $context, ?DateTimeInterface $start, ?DateTimeInterface $end): BTD|FDL
    {
        if ($this->config->ebicsVersion === 'H005') {
            $btdContext = (new BTDContext())
                ->setServiceName($this->config->btd->serviceName)
                ->setMsgName($this->config->btd->msgName)
                ->setParserFormat(EbicsClientInterface::FILE_PARSER_FORMAT_TEXT);
            if ($this->config->btd->scope !== null) {
                $btdContext->setScope($this->config->btd->scope);
            }
            if ($this->config->btd->serviceOption !== null) {
                $btdContext->setServiceOption($this->config->btd->serviceOption);
            }
            if ($this->config->btd->containerType !== null) {
                $btdContext->setContainerType($this->config->btd->containerType);
            }
            if ($this->config->btd->msgNameVariant !== null) {
                $btdContext->setMsgNameVariant($this->config->btd->msgNameVariant);
            }
            if ($this->config->btd->msgNameVersion !== null) {
                $btdContext->setMsgNameVersion($this->config->btd->msgNameVersion);
            }
            if ($this->config->btd->msgNameFormat !== null) {
                $btdContext->setMsgNameFormat($this->config->btd->msgNameFormat);
            }
            return new BTD($btdContext, $start, $end, $context);
        }

        if (!$this->config->fdlFallbackEnabled || $this->config->fdlFileFormat === null) {
            throw new HardStopException(
                'ebics_version=H004 verlangt einen aktivierten und vollständig konfigurierten fdl_fallback '
                . '- kein impliziter Ausweichweg.'
            );
        }
        $fdlContext = (new FDLContext())
            ->setFileFormat($this->config->fdlFileFormat)
            ->setParserFormat(EbicsClientInterface::FILE_PARSER_FORMAT_TEXT);

        return new FDL($fdlContext, $start, $end, $context);
    }
}
