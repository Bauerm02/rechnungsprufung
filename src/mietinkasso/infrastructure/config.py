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

    # Owner-Monatsbericht (Auftrag HV-20260919-INDEX-MONATSBERICHT): eigener,
    # eng begrenzter Schalter - unabhängig von `send_enabled`/
    # `indexautomatik_send_enabled`/`vertragsende_erinnerung_send_enabled`
    # (Codex: "unabhängig von ausgeschaltetem Mieter-/Mahnsenden aktivierbar,
    # ohne allgemeines SEND_ENABLED zu öffnen"). Läuft trotzdem weiterhin
    # zusätzlich über `hv_mail_allowlist_bestaetigt` + den echten Transport-
    # Client (kein paralleler, ungeprüfter Versandweg). Empfänger ist
    # IMMER `owner_email`, serverseitig zusätzlich gegen die feste
    # MailOps-Empfängergrenze für den Kind INDEX_MONATSBERICHT geprüft
    # (siehe `indexautomatik/mailops_client.py`).
    index_monatsbericht_send_enabled: bool = False
    # Aktivierungsperiode "YYYY-MM" (Betriebsdetail Codex: "erstes reguläres
    # automatisches Mailing soll 01.10.2026 ... ältere September-Vorschau
    # darf nicht unbeabsichtigt nachgesendet werden"). `None` (Default) -
    # "closed by default": KEIN automatischer Versand irgendeiner Periode,
    # selbst wenn `index_monatsbericht_send_enabled=True` gesetzt ist. Ein
    # bereits erzeugter Bericht für eine Periode VOR dieser Aktivierung
    # bleibt dauerhaft nur im Portal sichtbar (Status BEREIT), wird aber
    # NIE automatisch versendet.
    index_monatsbericht_send_ab: str | None = None
    # Absolute Basis-URL für den Portal-Link in der Owner-Mail (Codex:
    # "Mail-Link absolut, nicht relativ" - eine Mail landet im
    # E-Mail-Client, nicht im Browser-Kontext des Backoffice, ein
    # relativer Pfad wäre dort nicht klickbar). Zeigt auf die reale,
    # bestehende Hetzner-/JLB-Domain - Claude selbst greift NIE auf
    # diesen Server zu, dies ist nur der Textbaustein für die Mail.
    backoffice_basis_url: str = "https://verwaltung.jlb-immo.at"
    # Auftrag HV-20260913-VERSAND-SOLL: eigenes, GETRENNTES Flag für die
    # tatsächliche Soll-Umsetzung (Vertragskomponenten-/Rechtsprofil-
    # änderung) - bewusst UNABHÄNGIG von `indexautomatik_send_enabled`
    # (Mailversand), weil die Umsetzung selbst KEINEN Mailversand macht,
    # aber trotzdem eine erstmals scharf zu schaltende, echte
    # Buchhaltungs-/Vertragsänderung ist (gleiche "erst explizit
    # freigeben"-Vorsicht wie bei jedem anderen neuen Automatikschritt
    # in diesem Repository). Default false.
    indexautomatik_soll_umsetzung_enabled: bool = False

    # Vertragsanlage-Aufnahme (Auftrag HV-20260913-VERTRAGSANLAGE): der
    # hochgeladene Original-PDF-Vertrag wird NIE ins Repository/DB-BLOB
    # geschrieben, sondern in dieses private Verzeichnis AUSSERHALB des
    # Repos gelegt - ohne konfiguriertes Verzeichnis bleibt der Upload
    # blockiert (kein Fallback auf einen Pfad INNERHALB des Repos, kein
    # /tmp-Verzeichnis, das mit anderen Prozessen geteilt sein könnte).
    vertragsanlage_upload_verzeichnis: str | None = None
    vertragsanlage_max_upload_bytes: int = 15 * 1024 * 1024
    vertragsanlage_max_seiten: int = 60

    # Mahnbrief-PDF (Auftrag HV-20260914-MAHNUNG-BRIEF): Absenderdaten
    # sind öffentliche CI-Angaben (kein Secret). Logo/Fonts bleiben
    # konfigurierbare Dateipfade - Codex legt die freigegebenen Assets
    # beim Deployment privat ab; ohne sie bleibt der Brief ein
    # textbasierter Brief ohne Signet/mit Ersatzschrift.
    brief_absender_name: str = "JLB Projects GmbH"
    brief_absender_adresse: str = "Marc-Aurel-Straße 4/16, 1010 Wien"
    brief_absender_fn: str = "FN 631126b, Handelsgericht Wien"
    brief_absender_uid: str = "ATU81269707"
    brief_absender_telefon: str = "+43 1 435 10 11"
    brief_absender_website: str = "jlb-immo.at"
    brief_absender_email: str = "hausverwaltung@jlb-immo.at"
    brief_farbe_anthrazit: str = "#1F2125"
    brief_farbe_gold: str = "#C9A86A"
    brief_logo_pfad: str | None = None
    brief_font_regular_pfad: str | None = None
    brief_font_bold_pfad: str | None = None
    # Optionaler, vom Fließtext GETRENNTER Headline-Font (z. B. "Forum")
    # nur für die Betreffzeile - Fließtext bleibt regular/bold
    # (z. B. "EB Garamond"). Ohne konfigurierten Pfad fällt die
    # Betreffzeile auf den normalen Fett-Font zurück (unverändertes
    # Verhalten).
    brief_font_headline_pfad: str | None = None
    # DIN-5008-Standardposition für ein Fensterkuvert - Codex kann sie
    # bei Bedarf auf die tatsächliche EinfachBrief-Fensterspezifikation
    # nachjustieren, ohne Codeänderung.
    brief_fenster_links_mm: float = 20.0
    brief_fenster_oben_mm: float = 45.0
    brief_fenster_breite_mm: float = 90.0
    brief_fenster_hoehe_mm: float = 45.0


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
