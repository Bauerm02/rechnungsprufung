<?php

declare(strict_types=1);

namespace EbicsDownloader\Config;

final class LimitsConfig
{
    public function __construct(
        public readonly int $maxResponseBytes,
        public readonly int $maxZipEntries,
        public readonly int $maxEntryBytes,
        public readonly int $httpTimeoutSeconds,
        public readonly int $maxRetries,
        public readonly int $lockTimeoutSeconds,
    ) {
    }
}
