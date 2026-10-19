from datetime import date
from types import SimpleNamespace
from mietinkasso.backoffice.indexklausel_form import klausel_formular


def test_mietbeginn_und_buchhaltungsbeginn_werden_getrennt_angezeigt():
    vertrag = SimpleNamespace(id="SYNTHETISCH", gueltig_von=date(2026, 8, 1))
    profil = SimpleNamespace(urspruenglicher_mietbeginn=date(2025, 12, 1))
    html = klausel_formular(vertrag, [], [], "test", mietprofil=profil)
    assert "Ursprünglicher Mietbeginn: 01.12.2025" in html
    assert "Buchhaltung im System ab: 01.08.2026" in html
    assert 'name="abschlussdatum" required' in html
    assert 'name="basis_monat" required' in html


def test_fehlender_mietbeginn_wird_nicht_als_buchhaltungsbeginn_ausgegeben():
    vertrag = SimpleNamespace(id="SYNTHETISCH", gueltig_von=date(2026, 8, 1))
    html = klausel_formular(vertrag, [], [], "test")
    assert "Ursprünglicher Mietbeginn: noch nicht gesondert belegt" in html
