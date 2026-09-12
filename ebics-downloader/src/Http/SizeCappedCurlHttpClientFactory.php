<?php

declare(strict_types=1);

namespace EbicsDownloader\Http;

use EbicsApi\Ebics\Services\CurlHttpClient;
use EbicsDownloader\Config\ProfileConfig;
use EbicsDownloader\Exceptions\DeliveryRejectedException;

/**
 * Baut den `CurlHttpClient` der Bibliothek MIT allen sicherheitsrelevanten
 * cURL-Optionen aus dem Profil - die Bibliothek selbst setzt in ihrem
 * `CurlHttpClient` (`vendor/.../Services/CurlHttpClient.php`) NUR die
 * Optionen, die ihr explizit übergeben werden; TLS-Prüfung/Redirects sind
 * dort KEIN Default, den man aktiv abschalten müsste, sondern schlicht
 * unkonfiguriert, wenn man leer aufruft. Diese Fabrik erzwingt deshalb:
 *
 * - CURLOPT_SSL_VERIFYPEER / CURLOPT_SSL_VERIFYHOST je nach Profil.
 * - CURLOPT_FOLLOWLOCATION = false, wenn tls.allow_redirects = false
 *   (Standard/Pflicht laut Auftrag: "keine Redirects/unsichere URLs").
 * - CURLOPT_CAINFO nur, wenn explizit konfiguriert (kein Rätselraten).
 *
 * Zusätzlich schließt sie eine in dieser Sitzung identifizierte Lücke:
 * die bibliothekseigene Transportentschlüsselung/-dekomprimierung
 * (Base64-Decode -> AES-Decrypt -> zlib-Uncompress) läuft VOLLSTÄNDIG,
 * BEVOR unser eigener `ackClosure`-Hook (siehe Download\BtdDownloader)
 * je aufgerufen wird - ein Ressourcenlimit dort allein kommt zu spät für
 * eine überdimensionierte HTTP-Antwort. `CURLOPT_NOPROGRESS=false` mit
 * einem `CURLOPT_XFERINFOFUNCTION`-Callback, der den Transfer abbricht,
 * sobald mehr als `limits.max_response_bytes` empfangen wurden, wirkt
 * dagegen bereits WÄHREND des HTTP-Downloads - deutlich früher als jede
 * Prüfung nach `curl_exec()`. Ein alleiniges `CURLOPT_MAXFILESIZE` wäre
 * hier nicht zuverlässig, weil es sich auf einen angekündigten
 * Content-Length-Header verlässt, den eine Bank nicht zwingend sendet.
 */
final class SizeCappedCurlHttpClientFactory
{
    public function build(ProfileConfig $config): CurlHttpClient
    {
        $maxBytes = $config->limits->maxResponseBytes;

        $options = [
            CURLOPT_SSL_VERIFYPEER => $config->tlsVerifyPeer,
            CURLOPT_SSL_VERIFYHOST => $config->tlsVerifyHost ? 2 : 0,
            CURLOPT_FOLLOWLOCATION => $config->tlsAllowRedirects,
            CURLOPT_MAXREDIRS => 0,
            CURLOPT_TIMEOUT => $config->limits->httpTimeoutSeconds,
            CURLOPT_CONNECTTIMEOUT => $config->limits->httpTimeoutSeconds,
            CURLOPT_PROTOCOLS => defined('CURLPROTO_HTTPS') ? CURLPROTO_HTTPS : null,
            CURLOPT_REDIR_PROTOCOLS => defined('CURLPROTO_HTTPS') ? CURLPROTO_HTTPS : null,
            CURLOPT_NOPROGRESS => false,
            CURLOPT_XFERINFOFUNCTION => static function ($resource, int $downloadTotal, int $downloadedNow) use ($maxBytes): int {
                if ($downloadedNow > $maxBytes) {
                    // Rückgabewert != 0 lässt libcurl den Transfer sofort mit
                    // CURLE_ABORTED_BY_CALLBACK abbrechen (siehe post() unten).
                    return 1;
                }
                return 0;
            },
        ];

        if ($config->tlsCaBundlePath !== null) {
            $options[CURLOPT_CAINFO] = $config->tlsCaBundlePath;
        }

        return new CurlHttpClient(array_filter($options, static fn ($v): bool => $v !== null));
    }
}
