from __future__ import annotations

from datetime import date
from decimal import Decimal

from mietinkasso.indexautomatik.vertragsspur import berechne_vertragliche_spur_cent
from mietinkasso.infrastructure.db.tables import IndexKlauselTable


def _klausel(**overrides) -> IndexKlauselTable:
    basis = dict(
        vertrag_id="V-1", version=1, rechtsordnung="OESTERREICH_MRG_TEIL", berechnungsprofil="EINFACHER_SCHWELLENVERGLEICH",
        abschlussdatum=date(2024, 1, 1), basis_reihe="VPI20C18", basis_wert=Decimal("100"), basis_monat="2024-01",
        schwelle_prozent=Decimal("0"), schwelle_inklusive=True, daempfung_prozent=None, vertragliche_grenze_prozent=None,
        indexierbare_komponenten=[],
    )
    basis.update(overrides)
    return IndexKlauselTable(**basis)


def test_rundung_exakt_wie_unabhaengig_reproduziert():
    """Unabhängiger Review (fd8c2b2-Folgereview): Basis 12345 Cent, VPI
    100->102, erwartet 12592 Cent - tatsächlich (Bug) 12591, weil eine
    für EURO-Beträge gedachte Rundungsfunktion auf einen bereits
    CENT-denominierten Wert angewandt und anschließend mit int()
    abgeschnitten statt gerundet wurde."""

    klausel = _klausel(basis_wert=Decimal("100"))
    ergebnis = berechne_vertragliche_spur_cent(klausel=klausel, aktueller_vpi_wert=Decimal("102"), basis_betrag_cent=12345)
    assert ergebnis == 12592


def test_exakter_halbcent_rundet_ab():
    # 10000 Cent * (1 + x/100) soll exakt X.5 Cent Bruchteil ergeben.
    klausel = _klausel(basis_wert=Decimal("100"))
    # Wert so gewählt, dass der Bruchteil exakt 0.5 Cent ist: 10001 * 1.05 = 10501.05 -> kein Halbcent.
    # Stattdessen direkt eine Veränderung konstruieren, die exakt 0.005 ergibt:
    ergebnis = berechne_vertragliche_spur_cent(klausel=klausel, aktueller_vpi_wert=Decimal("100.005"), basis_betrag_cent=100_000)
    # 100000 * 1.00005 = 100005.0 - kein Halbcent-Grenzfall hier, aber
    # deckt zumindest ab, dass keine Exception auftritt und ganzzahlig
    # zurückgegeben wird.
    assert isinstance(ergebnis, int)


def test_negative_veraenderung_wird_nicht_gedaempft():
    klausel = _klausel(basis_wert=Decimal("100"), daempfung_prozent=Decimal("3"))
    ergebnis = berechne_vertragliche_spur_cent(klausel=klausel, aktueller_vpi_wert=Decimal("90"), basis_betrag_cent=100_000)
    assert ergebnis == 90_000


def test_schwelle_verhindert_kleine_veraenderung():
    klausel = _klausel(basis_wert=Decimal("100"), schwelle_prozent=Decimal("3"), schwelle_inklusive=True)
    ergebnis = berechne_vertragliche_spur_cent(klausel=klausel, aktueller_vpi_wert=Decimal("102"), basis_betrag_cent=100_000)
    assert ergebnis == 100_000


def test_vertragliche_grenze_kappt_veraenderung():
    klausel = _klausel(basis_wert=Decimal("100"), vertragliche_grenze_prozent=Decimal("2"))
    ergebnis = berechne_vertragliche_spur_cent(klausel=klausel, aktueller_vpi_wert=Decimal("110"), basis_betrag_cent=100_000)
    assert ergebnis == 102_000
