"""Schmales, bereichsübergreifendes Hilfsmodul der Routen.

Hier steht ausschließlich, was nachweislich von mehreren Fachbereichen
benutzt wird. Kein Router, keine Route, kein Rückimport aus `app` oder
aus einem anderen Routenmodul."""

from __future__ import annotations


# -- Vertragsprüfung (Auftrag 12.09., Paket B) -------------------------------------


_RECHTSORDNUNGEN_FUER_AUSWAHL = [
    "OESTERREICH_MRG_VOLL", "OESTERREICH_MRG_TEIL", "OESTERREICH_MRG_FREI",
    "OESTERREICH_WGG", "OESTERREICH_GEWERBE", "DEUTSCHLAND", "UNGEKLAERT",
]
