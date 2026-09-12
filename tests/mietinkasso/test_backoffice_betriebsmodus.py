"""Tests für den Betriebsmodus-Banner (`backoffice/views.py`).

Auftrag 12.09. (Paket A): der Backoffice-Banner war bisher fest auf
"PILOT-BETRIEB — nur synthetische Demodaten" verdrahtet, unabhängig von
der tatsächlichen Umgebung. `betriebsmodus_banner`/`seite` müssen
anhand der bereits vorhandenen `environment`-Konfiguration korrekt
zwischen Echtbestand und Demo unterscheiden - ohne ein neues
Datenmodell und ohne irgendetwas automatisch freizugeben (reine
Anzeigeentscheidung, `produktiv`/`send_enabled` werden vom Aufrufer
übergeben, nie hier gelesen/geändert).

Hinweis zur Testabdeckung: `test_backoffice.py` importiert
`mietinkasso.api.app` (und damit `backoffice.app`) genau einmal pro
Testprozess mit `MIETINKASSO_ENVIRONMENT` unausgesprochen auf dem
Default "development" - ein zweiter Import mit
`MIETINKASSO_ENVIRONMENT=production` im selben Prozess würde wegen
Python-Modul-Caching NICHT erneut ausgeführt und `_PRODUKTIV` daher
nicht auf True setzen. Der produktive Zweig wird deshalb hier auf der
reinen Anzeigefunktion getestet (unabhängig von der App/den
Settings), der demo-Zweig zusätzlich End-to-End in
`test_backoffice.py`."""

from __future__ import annotations

from mietinkasso.backoffice.views import betriebsmodus_banner, seite


def test_banner_zeigt_pilot_text_wenn_nicht_produktiv():
    text = betriebsmodus_banner(produktiv=False, send_enabled=False)
    assert "PILOT-BETRIEB" in text
    assert "synthetische Demodaten" in text


def test_banner_zeigt_echtbetrieb_text_und_send_enabled_status():
    text_aus = betriebsmodus_banner(produktiv=True, send_enabled=False)
    assert "ECHTBETRIEB" in text_aus
    assert "reale Hausverwaltungsdaten" in text_aus
    assert "AUS (SEND_ENABLED=false)" in text_aus
    assert "PILOT" not in text_aus
    assert "synthetische" not in text_aus

    text_an = betriebsmodus_banner(produktiv=True, send_enabled=True)
    assert "Mailversand: AKTIV" in text_an


def test_seite_default_ist_sicher_pilot_auch_ohne_expliziten_parameter():
    """Sicherer Default: ein Aufrufer, der `produktiv` vergisst, zeigt
    NIE versehentlich den Echtbetrieb-Banner an."""

    html = seite(titel="Test", inhalt="<p>x</p>")
    assert "PILOT-BETRIEB" in html
    assert "(PILOT)" in html


def test_seite_zeigt_echtbetrieb_banner_und_titel_wenn_explizit_produktiv():
    html = seite(titel="Test", inhalt="<p>x</p>", produktiv=True, send_enabled=False)
    assert "ECHTBETRIEB" in html
    assert "(ECHTBETRIEB)" in html
    assert "PILOT-BETRIEB" not in html
