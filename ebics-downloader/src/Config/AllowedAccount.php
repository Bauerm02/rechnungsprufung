<?php

declare(strict_types=1);

namespace EbicsDownloader\Config;

/**
 * Ein einzelnes, explizit freigegebenes Konto (Whitelist-Eintrag).
 * `objekt` ist nur eine Anzeige-/Zuordnungshilfe (z. B. "601") - die
 * tatsächliche, sicherheitsrelevante Prüfung läuft ausschließlich über
 * die normalisierte IBAN (siehe Camt\StatementSplitter).
 */
final class AllowedAccount
{
    public function __construct(
        public readonly string $objekt,
        public readonly string $iban,
    ) {
    }
}
