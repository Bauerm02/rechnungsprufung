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

from mietinkasso.domain.money import round_index_half_cent_down
from mietinkasso.index.service import IndexService
from mietinkasso.infrastructure.db.tables import IndexKlauselTable


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

    neuer_betrag_decimal = Decimal(basis_betrag_cent) * (Decimal("1") + effektive_veraenderung / Decimal("100"))
    return int(round_index_half_cent_down(neuer_betrag_decimal))
