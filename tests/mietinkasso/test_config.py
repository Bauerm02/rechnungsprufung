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


def test_fehlender_passwort_hash_in_production_wird_abgelehnt():
    with pytest.raises(ProduktionskonfigurationUngueltigError, match="PASSWORD_HASH"):
        pruefe_produktionskonfiguration(_settings(backoffice_password_hash=None))


def test_unsicheres_cookie_in_production_wird_abgelehnt():
    with pytest.raises(ProduktionskonfigurationUngueltigError, match="COOKIE_SECURE"):
        pruefe_produktionskonfiguration(_settings(backoffice_cookie_secure=False))
