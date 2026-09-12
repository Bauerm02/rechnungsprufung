<?php

declare(strict_types=1);

namespace EbicsDownloader\Config;

/**
 * BTF-Felder (EBICS 3.0/H005, ServiceContext) - AUSNAHMSLOS aus der
 * privaten Profildatei, NIE mit einem Default für eine österreichische
 * Bank vorbelegt. `service_name`/`msg_name` sind Pflichtfelder; alle
 * anderen bleiben optional, weil nicht jede Bank jedes Feld verlangt.
 */
final class BtfConfig
{
    public function __construct(
        public readonly string $serviceName,
        public readonly ?string $scope,
        public readonly ?string $serviceOption,
        public readonly ?string $containerType,
        public readonly string $msgName,
        public readonly ?string $msgNameVariant,
        public readonly ?string $msgNameVersion,
        public readonly ?string $msgNameFormat,
    ) {
    }
}
