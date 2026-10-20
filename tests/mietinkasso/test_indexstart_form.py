from datetime import date
from types import SimpleNamespace
from mietinkasso.backoffice.indexklausel_form import klausel_formular


def _leeres_profil(**ueberschreibungen) -> SimpleNamespace:
    """Stub mit ALLEN Feldern, die `klausel_formular`/`zinsprofil_formular`
    von einem echten `MietvertragsprofilTable`-Row lesen - ein echtes Row
    hat sie immer (auch unbefüllt als `None`), ein unvollständiger Test-
    Stub würde reale Attributzugriffe sonst nur zufällig verdecken."""

    basis = dict(
        urspruenglicher_mietbeginn=None, unterzeichnungsdatum=None,
        index_reihe=None, index_urspruenglicher_basismonat=None, index_urspruenglicher_basiswert=None,
        index_schwelle_prozent=None, index_schwelle_inklusive=None,
        index_anpassungsmonat=None, index_mindestintervall_monate=None, index_klauseltext_auszug=None,
        verzugszins_art="UNGEKLAERT", verzugszins_satz_prozent=None, verzugszins_klauseltext_auszug=None,
        mahngebuehr_cent=None, mahngebuehr_quellenbeleg=None,
    )
    basis.update(ueberschreibungen)
    return SimpleNamespace(**basis)


def test_mietbeginn_und_buchhaltungsbeginn_werden_getrennt_angezeigt():
    vertrag = SimpleNamespace(id="SYNTHETISCH", gueltig_von=date(2026, 8, 1))
    profil = _leeres_profil(urspruenglicher_mietbeginn=date(2025, 12, 1))
    html = klausel_formular(vertrag, [], [], "test", mietprofil=profil)
    assert "Ursprünglicher Mietbeginn: 01.12.2025" in html
    assert "Buchhaltung im System ab: 01.08.2026" in html
    assert 'name="abschlussdatum" value="" required' in html
    assert 'name="basis_monat" value="" required' in html


def test_index_quellfelder_befuellen_formular_vorschlag():
    """Abnahme-Folgefix: belegte Index-Quellwerte aus dem
    Mietvertragsprofil (PDF-Vorschlag oder manuell im Review erfasst)
    befüllen jetzt die tatsächliche Indexklausel-Form vor - nicht mehr
    nur den Mietbeginn."""

    from decimal import Decimal

    vertrag = SimpleNamespace(id="SYNTHETISCH", gueltig_von=date(2026, 8, 1))
    profil = _leeres_profil(
        unterzeichnungsdatum=date(2026, 6, 1),
        index_reihe="VPI 2020", index_urspruenglicher_basismonat="2026-12",
        index_urspruenglicher_basiswert=Decimal("130.5"), index_schwelle_prozent=Decimal("3"),
        index_schwelle_inklusive=False, index_anpassungsmonat=1, index_mindestintervall_monate=12,
        index_klauseltext_auszug="Schwankung um mehr als 3 % löst Anpassung im Jänner aus.",
    )
    html = klausel_formular(vertrag, [], [], "test", mietprofil=profil)
    assert 'name="abschlussdatum" value="2026-06-01"' in html
    assert 'value="VPI20C18" selected' in html
    assert 'name="basis_monat" value="2026-12"' in html
    assert 'name="basis_wert" value="130.5"' in html
    assert 'name="schwelle_prozent" value="3"' in html
    assert 'value="0" selected' in html  # exklusiv vorbefüllt
    assert 'name="anpassungsmonat" min="1" max="12" value="1"' in html
    assert 'name="mindestintervall_monate" min="1" value="12"' in html
    assert "Schwankung um mehr als 3" in html


def test_fehlender_mietbeginn_wird_nicht_als_buchhaltungsbeginn_ausgegeben():
    vertrag = SimpleNamespace(id="SYNTHETISCH", gueltig_von=date(2026, 8, 1))
    html = klausel_formular(vertrag, [], [], "test")
    assert "Ursprünglicher Mietbeginn: noch nicht gesondert belegt" in html
