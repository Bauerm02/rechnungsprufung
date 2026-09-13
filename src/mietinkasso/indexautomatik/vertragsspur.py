"""Reine, seiteneffektfreie Berechnung der VERTRAGLICHEN Spur aus einer
versionierten, bereits freigegebenen `IndexKlauselTable` (Auftrag
13.09., Modellreview: "ein fixer manuell eingetragener
vertraglich_zulaessiger_betrag_cent ist keine dauerhafte
Vertragsformel... Vertragsmodell bitte aus versionierter Klausel und
amtlichen Werten rechnen").

Reine Wiederverwendung der bereits abgenommenen Schwellen-/Dämpfungs-/
Deckel-Formel aus `index/service.py::IndexService._daempfe` (KEIN
paralleler Rohrechner) - liefert hier aber einen ABSOLUTEN neuen
Zielbetrag (für `mieweg_vorschau_service.vorschau_erstellen(
vertraglich_zulaessiger_betrag_cent=...)`) statt wie dort eine reine
Delta-Anpassung mit Nebenwirkung (`IndexAnpassungTable`-Persistenz)."""

from __future__ import annotations

from decimal import Decimal

from mietinkasso.index.service import IndexService
from mietinkasso.infrastructure.db.tables import IndexKlauselTable
from mietinkasso.mieweg_vorschau.berechnung import runde_halbcent


def berechne_vertragliche_spur_cent(
    *, klausel: IndexKlauselTable, aktueller_vpi_wert: Decimal, basis_betrag_cent: int
) -> int:
    if klausel.basis_wert == 0:
        raise ValueError(f"IndexKlausel {klausel.id}: Basiswert ist 0 - Veränderung nicht berechenbar.")

    rohe_veraenderung = (aktueller_vpi_wert - klausel.basis_wert) / klausel.basis_wert * 100
    effektive_veraenderung = IndexService._daempfe(rohe_veraenderung, klausel.daempfung_prozent)

    if klausel.vertragliche_grenze_prozent is not None and effektive_veraenderung > klausel.vertragliche_grenze_prozent:
        effektive_veraenderung = klausel.vertragliche_grenze_prozent

    if klausel.schwelle_inklusive:
        unterhalb_schwelle = abs(effektive_veraenderung) < klausel.schwelle_prozent
    else:
        unterhalb_schwelle = abs(effektive_veraenderung) <= klausel.schwelle_prozent
    if unterhalb_schwelle:
        effektive_veraenderung = Decimal("0")

    # Unabhängiger Review (fd8c2b2-Folgereview, synthetisch reproduziert:
    # Basis 12345 Cent, VPI 100->102, erwartet 12592, tatsächlich 12591):
    # `neuer_betrag_decimal` ist bereits CENT-denominiert - `domain/
    # money.py::round_index_half_cent_down` erwartet dagegen einen
    # EURO-Betrag (quantisiert auf 0.01 = 1 Cent) und hätte einen
    # gebrochenen Cent-Rest (12591.9) nur auf "12591.90" gerundet, den
    # anschließende `int()`-Aufruf dann Richtung Null ABGESCHNITTEN statt
    # gerundet. `berechnung.py::runde_halbcent` ist die für bereits
    # Cent-denominierte Werte korrekte Funktion (quantisiert auf ganze
    # Cent, exakter Halbcent rundet ab) - exakt dieselbe Wiederverwendung
    # wie in `mieweg_vorschau/berechnung.py` selbst.
    neuer_betrag_decimal = Decimal(basis_betrag_cent) * (Decimal("1") + effektive_veraenderung / Decimal("100"))
    return int(runde_halbcent(neuer_betrag_decimal))
