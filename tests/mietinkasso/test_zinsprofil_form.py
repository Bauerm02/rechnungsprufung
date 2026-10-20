"""Reine Renderer-Tests für `backoffice/zinsprofil_form.py` (Abnahme-
Folgefix: "Zinsprofil-Form noch gar keine Quellwerte" - belegte
Verzugszins-/Mahngebühr-Quellwerte aus dem Mietvertragsprofil befüllen
jetzt die Formular-Standardwerte vor)."""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from mietinkasso.backoffice.zinsprofil_form import zinsprofil_formular


def _leeres_profil(**ueberschreibungen) -> SimpleNamespace:
    basis = dict(
        unterzeichnungsdatum=None, verzugszins_art="UNGEKLAERT", verzugszins_satz_prozent=None,
        verzugszins_klauseltext_auszug=None, mahngebuehr_cent=None, mahngebuehr_quellenbeleg=None,
    )
    basis.update(ueberschreibungen)
    return SimpleNamespace(**basis)


def test_ohne_mietprofil_bleibt_formular_wie_bisher_leer():
    vertrag = SimpleNamespace(id="SYNTHETISCH")
    html = zinsprofil_formular(vertrag, [], "test")
    assert 'name="vertragsdatum" value=""' in html
    assert 'name="vereinbarter_zinssatz_prozent" value=""' in html
    assert 'name="mahnpauschale_kostenbasis_cent" value=""' in html


def test_vertraglich_vereinbarter_verzugszins_und_mahngebuehr_befuellen_vorschlag():
    vertrag = SimpleNamespace(id="SYNTHETISCH")
    profil = _leeres_profil(
        unterzeichnungsdatum=date(2026, 6, 1),
        verzugszins_art="VERTRAGLICH_VEREINBART", verzugszins_satz_prozent=Decimal("8"),
        verzugszins_klauseltext_auszug="Verzugszinsen in Höhe von 8 % p.a.",
        mahngebuehr_cent=2000, mahngebuehr_quellenbeleg="Mahngebühr: 20,00 EUR",
    )
    html = zinsprofil_formular(vertrag, [], "test", mietprofil=profil)
    assert 'name="vertragsdatum" value="2026-06-01"' in html
    assert 'name="vereinbarter_zinssatz_prozent" value="8"' in html
    assert "Verzugszinsen in Höhe von 8" in html
    assert 'name="mahnpauschale_kostenbasis_cent" value="20,00"' in html
    assert "Mahngebühr: 20,00 EUR" in html
    # Prüf-/Freigabe-Checkboxen bleiben UNANGEHAKT - menschliche Prüfung
    # bleibt zwingend, eine Vorbefüllung ersetzt sie nicht.
    assert '<input type="checkbox" name="vereinbarung_geprueft" value="1">' in html
    assert '<input type="checkbox" name="mahnpauschale_geprueft" value="1">' in html


def test_gesetzlicher_fallback_befuellt_keinen_vereinbarten_zinssatz():
    """Ein gesetzlicher Fallback ist KEINE vertragliche Vereinbarung - ein
    trotzdem hinterlegter Satz darf das Zinsfeld nicht täuschend
    vorbefüllen (das würde eine Prüfung/Vereinbarung vortäuschen)."""

    vertrag = SimpleNamespace(id="SYNTHETISCH")
    profil = _leeres_profil(verzugszins_art="GESETZLICHER_FALLBACK", verzugszins_satz_prozent=Decimal("4"))
    html = zinsprofil_formular(vertrag, [], "test", mietprofil=profil)
    assert 'name="vereinbarter_zinssatz_prozent" value=""' in html
