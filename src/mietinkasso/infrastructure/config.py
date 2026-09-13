from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. No AI/LLM settings exist here on purpose:

    MVP1 runs as a deterministic rule engine with zero LLM dependency at
    runtime. KI is only used offline during development for first-draft
    data extraction, never invoked from this service.
    """

    model_config = SettingsConfigDict(
        env_prefix="MIETINKASSO_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "development"
    database_url: str = "sqlite:///./data/mietinkasso.db"
    default_timezone: str = Field(default="Europe/Vienna")

    # Hard safety defaults for the pilot phase. Never flip these true
    # outside an explicit, reviewed production cutover.
    send_enabled: bool = False
    bank_stand_max_age_days: int = 2
    mahn_stufe1_tage_nach_faelligkeit: int = 7
    mahn_stufe2_mindesttage_nach_stufe1: int = 14

    ausgeschlossene_objekte: tuple[str, ...] = ("107",)
    pilot_objekte: tuple[str, ...] = ("601", "616", "617", "619")

    # Es gibt in MVP1 kein echtes Login/Session-Handling (siehe
    # OFFENE_PUNKTE.md). Bis das existiert, ist api/app.py trotzdem NICHT
    # offen: ohne konfigurierten Token antworten alle Datenendpunkte mit
    # 503 statt Daten preiszugeben ("closed by default" statt "geöffnet,
    # bis jemand Auth einbaut").
    api_token: str | None = None

    # Server-gerendertes Backoffice (bedienbarer Pilot-Arbeitsablauf, siehe
    # backoffice/app.py): EIN lokaler Login für den Piloten (kein
    # Mehrbenutzer-/Rollen-Management in MVP1 - das ist explizit eine
    # spätere Inbetriebnahme, siehe OFFENE_PUNKTE.md). Ohne konfigurierten
    # Passwort-Hash bleibt das Backoffice komplett geschlossen (503),
    # exakt wie `api_token` oben - "closed by default". Der Hash wird mit
    # `python -m mietinkasso.backoffice.security <passwort>` erzeugt und
    # NIE als Klartext-Passwort hier abgelegt.
    backoffice_user: str = "markus"
    backoffice_password_hash: str | None = None
    backoffice_session_ttl_minuten: int = 480
    # Sicher per Default (HV-20260912-ECHTBETRIEB: Produktionsstart hinter
    # HTTPS-Reverse-Proxy) - das Session-Cookie wird dann nur über TLS
    # übertragen. Lokale Entwicklung/Tests über reines HTTP (kein TLS)
    # müssen dies ausdrücklich auf false setzen, sonst sendet der Browser
    # das Cookie nicht zurück; ein vergessenes "false" in Produktion ist
    # der sicherere Fehler als ein vergessenes "true".
    backoffice_cookie_secure: bool = True

    # Indexautomatik (Auftrag 13.09.2026, HV-20260913-INDEXAUTOMATIK):
    # interner Empfänger für Vertragsende-Erinnerungen - IMMER Owner,
    # NIE ein Mieter-Fallback/CC (siehe VertragsendeErinnerungTable-
    # Docstring). Ohne konfigurierte Adresse bleibt die
    # Vertragsende-Erinnerung geblockt statt an niemanden/eine geratene
    # Adresse zu gehen.
    owner_email: str | None = None
    # Bewusst GETRENNT vom bestehenden `send_enabled` (Mahnwesen) -
    # "Versand-/Index- und Owner-Erinnerungsflags getrennt (keine
    # globale Aktivierung von Mahnungen)". Beide Default false.
    indexautomatik_send_enabled: bool = False
    vertragsende_erinnerung_send_enabled: bool = False
    # Betriebspräzisierung 13.09.2026: die bestehende JLB-MailOps-Strecke
    # verlangt eine ECHTE manuelle CLICK_RELEASED-Freigabe und führt
    # Hausverwaltung noch NICHT in ihrer Mailbox-Allowlist - diese
    # Freigabe darf nie simuliert werden. Realer Versand setzt deshalb
    # ZUSÄTZLICH zu `indexautomatik_send_enabled` diese von Codex nach
    # echter Prüfung zu setzende Bestätigung voraus; beide bleiben in
    # dieser Sitzung false.
    indexautomatik_mailops_allowlist_bestaetigt: bool = False
    indexautomatik_jlb_signatur: str = "JLB Projects GmbH - Hausverwaltung"
    # Default AUS - `statistik_austria_client.py` konnte in dieser
    # Sitzung mangels Internetzugriff nicht selbst gegen den echten
    # Endpunkt ausgeführt werden. Codex hat die vier amtlichen
    # OGD-Original-URLs unabhängig erfolgreich geprüft
    # (PUBLIC_CLIENT_REVIEW) - die Aktivierung bleibt trotzdem eine
    # bewusste, separate Betriebsentscheidung.
    indexautomatik_vpi_automatischer_abruf: bool = False
    # Zielverzeichnis für die vier heruntergeladenen OGD-Rohdateien -
    # AUSSERHALB des Repos, keine echten/produktiven Daten in Git.
    # Pflicht, sobald `indexautomatik_vpi_automatischer_abruf=True`.
    indexautomatik_vpi_ablage_verzeichnis: str | None = None
    # Generischer HTTP-Transport (indexautomatik/transport.py::
    # HttpTransportadapter) - ohne konfigurierten Endpunkt bleibt der
    # tägliche Versandlauf strukturell blockiert (kein Fallback auf
    # einen erfundenen Endpunkt).
    indexautomatik_transport_endpoint_url: str | None = None
    indexautomatik_transport_api_key: str | None = None
    # Private existing JLB MailOps process, credentials stay on the server.
    hv_mail_socket_path: str | None = None
    hv_mail_token_file: str | None = None
    hv_mail_allowlist_bestaetigt: bool = False
    # Auftrag HV-20260913-VERSAND-SOLL: eigenes, GETRENNTES Flag für die
    # tatsächliche Soll-Umsetzung (Vertragskomponenten-/Rechtsprofil-
    # änderung) - bewusst UNABHÄNGIG von `indexautomatik_send_enabled`
    # (Mailversand), weil die Umsetzung selbst KEINEN Mailversand macht,
    # aber trotzdem eine erstmals scharf zu schaltende, echte
    # Buchhaltungs-/Vertragsänderung ist (gleiche "erst explizit
    # freigeben"-Vorsicht wie bei jedem anderen neuen Automatikschritt
    # in diesem Repository). Default false.
    indexautomatik_soll_umsetzung_enabled: bool = False


class ProduktionskonfigurationUngueltigError(RuntimeError):
    """Wird beim Prozessstart geworfen, wenn `environment == "production"`
    gesetzt ist, aber sicherheitsrelevante Einstellungen nicht dem
    erwarteten Produktionsstand entsprechen - ein sofortiger, lauter
    Startfehler ist der sicherere Fehler als ein still laufender Prozess
    mit unsicherer Konfiguration (echter Mailversand, offenes Backoffice,
    Cookies ohne HTTPS-Bindung)."""


def pruefe_produktionskonfiguration(settings: Settings) -> None:
    """Nur in `environment == "production"` scharf - lokale
    Entwicklung/Tests (Default `"development"`) bleiben unangetastet."""

    if settings.environment != "production":
        return
    fehler = []
    if settings.send_enabled and not (settings.hv_mail_allowlist_bestaetigt and
            settings.hv_mail_socket_path and settings.hv_mail_token_file):
        fehler.append(
            "MIETINKASSO_SEND_ENABLED verlangt den ausdrücklich freigegebenen privaten Hausverwaltungs-Mailweg."
        )
    if not settings.backoffice_password_hash:
        fehler.append(
            "MIETINKASSO_BACKOFFICE_PASSWORD_HASH ist in production Pflicht - kein offenes Backoffice."
        )
    if not settings.backoffice_cookie_secure:
        fehler.append(
            "MIETINKASSO_BACKOFFICE_COOKIE_SECURE muss in production 'true' bleiben (Session-Cookie nur über HTTPS)."
        )
    if fehler:
        raise ProduktionskonfigurationUngueltigError(
            "Unsichere Produktionskonfiguration, Prozess wird NICHT gestartet: " + " ".join(fehler)
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
