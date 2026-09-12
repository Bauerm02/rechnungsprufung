"""Tests für den Betriebsmodus-Banner (`backoffice/views.py`).

Auftrag 12.09. (Paket A/B): der Backoffice-Banner war bisher fest auf
"PILOT-BETRIEB — nur synthetische Demodaten" verdrahtet, unabhängig von
der tatsächlichen Umgebung. `betriebsmodus_banner`/`seite` müssen
anhand der bereits vorhandenen `environment`-Konfiguration korrekt
zwischen Echtbestand und Demo unterscheiden - ohne ein neues
Datenmodell und ohne irgendetwas automatisch freizugeben (reine
Anzeigeentscheidung, `environment`/`send_enabled` werden vom Aufrufer
übergeben, nie hier gelesen/geändert).

Codex-Rückprüfung (Paket A, unabhängig geprüft): die ursprüngliche
Logik prüfte "Echtbetrieb nur bei environment == 'production'" - ein
Zwischenschritt mit bereits echten, gestagten Daten (z. B.
"local_realdata_staged") wäre damit fälschlich als "nur synthetische
Demodaten" ausgewiesen worden. Jetzt gilt umgekehrt: NUR bekannte
Demo-Umgebungen (development/test/ci) behaupten synthetische Daten,
jede andere (auch ein unbekannter Wert) zeigt den Echtbetrieb-Banner.

Hinweis zur Testabdeckung: `test_backoffice.py` importiert
`mietinkasso.api.app` (und damit `backoffice.app`) genau einmal pro
Testprozess mit `MIETINKASSO_ENVIRONMENT` unausgesprochen auf dem
Default "development" - ein zweiter Import mit einem anderen Wert im
selben Prozess würde wegen Python-Modul-Caching NICHT erneut
ausgeführt. Der Echtbetrieb-Zweig wird deshalb hier auf der reinen
Anzeigefunktion getestet (unabhängig von der App/den Settings), der
Demo-Zweig zusätzlich End-to-End in `test_backoffice.py`."""

from __future__ import annotations

from mietinkasso.backoffice.views import betriebsmodus_banner, ist_bekannte_demo_umgebung, seite


def test_ist_bekannte_demo_umgebung_erkennt_nur_die_zugelassenen_werte():
    assert ist_bekannte_demo_umgebung("development") is True
    assert ist_bekannte_demo_umgebung("test") is True
    assert ist_bekannte_demo_umgebung("ci") is True
    assert ist_bekannte_demo_umgebung("production") is False
    assert ist_bekannte_demo_umgebung("staging") is False
    # Ein Zwischenschritt mit bereits echten, gestagten Daten - darf NIE
    # als "sicher synthetisch" durchgehen, nur weil er nicht exakt
    # "production" heißt.
    assert ist_bekannte_demo_umgebung("local_realdata_staged") is False
    assert ist_bekannte_demo_umgebung("irgendein-unbekannter-wert") is False


def test_banner_zeigt_pilot_text_fuer_bekannte_demo_umgebung():
    text = betriebsmodus_banner(environment="development", send_enabled=False)
    assert "PILOT-BETRIEB" in text
    assert "synthetische Demodaten" in text


def test_banner_zeigt_echtbetrieb_fuer_production():
    text = betriebsmodus_banner(environment="production", send_enabled=False)
    assert "ECHTBETRIEB" in text
    assert "reale Hausverwaltungsdaten" in text
    assert "AUS (SEND_ENABLED=false)" in text
    assert "PILOT" not in text
    assert "synthetische" not in text


def test_banner_zeigt_echtbetrieb_fuer_unbekannten_zwischenschritt_niemals_synthetisch():
    """Der konkrete Codex-Befund: ein Umgebungswert wie
    'local_realdata_staged' darf NIE 'nur synthetische Demodaten'
    behaupten."""

    text = betriebsmodus_banner(environment="local_realdata_staged", send_enabled=False)
    assert "synthetische" not in text
    assert "ECHTBETRIEB" in text
    assert "local_realdata_staged" in text  # Umgebung sichtbar, nicht verschleiert


def test_banner_zeigt_mailversand_status():
    text_an = betriebsmodus_banner(environment="production", send_enabled=True)
    assert "Mailversand: AKTIV" in text_an


def test_seite_default_ist_sicher_pilot_auch_ohne_expliziten_parameter():
    """Sicherer Default: ein Aufrufer, der `environment` vergisst, zeigt
    NIE versehentlich den Echtbetrieb-Banner an."""

    html = seite(titel="Test", inhalt="<p>x</p>")
    assert "PILOT-BETRIEB" in html
    assert "(PILOT)" in html


def test_seite_zeigt_echtbetrieb_banner_und_titel_bei_production():
    html = seite(titel="Test", inhalt="<p>x</p>", environment="production", send_enabled=False)
    assert "ECHTBETRIEB" in html
    assert "(ECHTBETRIEB)" in html
    assert "PILOT-BETRIEB" not in html
