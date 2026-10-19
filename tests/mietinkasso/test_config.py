"""Tests für `infrastructure/config.py::pruefe_produktionskonfiguration`
(Codex-Rückprüfung: production darf nie mit unsicherer Konfiguration
starten - echter Mailversand, offenes Backoffice, Cookies ohne
HTTPS-Bindung)."""

from __future__ import annotations

import pytest

from mietinkasso.infrastructure.config import ProduktionskonfigurationUngueltigError, Settings, pruefe_produktionskonfiguration


def _settings(**overrides) -> Settings:
    basis = dict(
        environment="production",
        send_enabled=False,
        backoffice_password_hash="pbkdf2_sha256$1$aa$bb",
        backoffice_cookie_secure=True,
    )
    basis.update(overrides)
    return Settings(**basis)


def test_produktionskonfiguration_mit_sicheren_werten_ist_ok():
    pruefe_produktionskonfiguration(_settings())  # darf nicht werfen


def test_development_wird_nicht_geprueft():
    pruefe_produktionskonfiguration(_settings(environment="development", send_enabled=True, backoffice_password_hash=None, backoffice_cookie_secure=False))


def test_send_enabled_in_production_wird_abgelehnt():
    with pytest.raises(ProduktionskonfigurationUngueltigError, match="SEND_ENABLED"):
        pruefe_produktionskonfiguration(_settings(send_enabled=True))


def test_brief_fenster_oben_mm_entspricht_der_einfachbrief_spezifikation():
    """Codex-Rückmeldung 19.10.2026: dieser Settings-Default war auf der
    alten DIN-5008-Position (45 mm) stehengeblieben, obwohl
    `brief_pdf.py::Absender.fenster_oben_mm` (die tatsächliche
    EinfachBrief-Spezifikation) bereits 62 mm trug - und wird beim
    tatsächlichen Aufruf (`routes/mahnwesen.py::mahnbrief_pdf`) IMMER an
    die `Absender`-Instanz übergeben, überschreibt deren richtigen
    Default also still. Beide Werte müssen identisch bleiben."""

    from mietinkasso.mahnwesen.brief_pdf import Absender

    einstellungen = Settings()
    referenz_absender = Absender(
        name="x", adresse="x", fn="x", uid="x", telefon="x", website="x", email="x",
        farbe_anthrazit="#000", farbe_gold="#000",
    )
    assert einstellungen.brief_fenster_oben_mm == referenz_absender.fenster_oben_mm == 62.0


def test_fehlender_passwort_hash_in_production_wird_abgelehnt():
    with pytest.raises(ProduktionskonfigurationUngueltigError, match="PASSWORD_HASH"):
        pruefe_produktionskonfiguration(_settings(backoffice_password_hash=None))


def test_unsicheres_cookie_in_production_wird_abgelehnt():
    with pytest.raises(ProduktionskonfigurationUngueltigError, match="COOKIE_SECURE"):
        pruefe_produktionskonfiguration(_settings(backoffice_cookie_secure=False))
