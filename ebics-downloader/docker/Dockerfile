# EBICS-Downloadclient (Paket D1) - isolierter PHP-CLI-Container.
#
# UNGETESTET in dieser Sitzung: kein Docker-Daemon verfuegbar (siehe
# README.md, Abschnitt "Betriebsgrenzen"). Codex/Markus muessen dieses
# Image vor dem produktiven Einsatz tatsaechlich bauen und den
# Containerstart gegen das echte Zielsystem (Hetzner) verifizieren.
#
# Bewusst KEIN Composer im Laufzeit-Image (Multi-Stage-Build: Composer
# nur im Build-Stage), KEIN root-Prozess zur Laufzeit, KEINE
# oeffentliche Portfreigabe (der Client hat keine HTTP-Schnittstelle -
# EXPOSE fehlt daher absichtlich).

FROM php:8.5-cli AS build
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        git unzip libzip-dev libonig-dev \
    && docker-php-ext-install bcmath zip \
    && rm -rf /var/lib/apt/lists/*

COPY --from=composer:2 /usr/bin/composer /usr/bin/composer
COPY composer.json composer.lock ./
RUN composer install --no-dev --no-interaction --no-progress --optimize-autoloader

COPY src/ src/
COPY bin/ bin/

FROM php:8.5-cli AS runtime
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        libzip4 \
    && docker-php-ext-install bcmath zip \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system ebics && useradd --system --gid ebics --home-dir /app ebics

COPY --from=build /app/vendor vendor
COPY --from=build /app/src src
COPY --from=build /app/bin bin
COPY composer.json composer.json

# Private Konfiguration/Schluessel/Archive werden NIE ins Image gebacken,
# sondern zur Laufzeit read-only (Konfiguration/Schluessel) bzw.
# read-write (Archiv/Quarantaene/Freigabe/Ledger) mit restriktiven
# Rechten in dieses Verzeichnis gemountet.
RUN mkdir -p /app/privat && chown ebics:ebics /app/privat
VOLUME ["/app/privat"]

USER ebics
ENTRYPOINT ["php", "bin/ebics-downloader"]
CMD ["check-config", "/app/privat/profile.json"]
