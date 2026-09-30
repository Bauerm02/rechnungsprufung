"""Schmales, bereichsübergreifendes Hilfsmodul der Routen.

Hier steht ausschließlich, was nachweislich von mehreren Fachbereichen
benutzt wird. Kein Router, keine Route, kein Rückimport aus `app` oder
aus einem anderen Routenmodul."""

from __future__ import annotations

from html import escape as h
from urllib.parse import urlencode


# -- Vertragsprüfung (Auftrag 12.09., Paket B) -------------------------------------


_RECHTSORDNUNGEN_FUER_AUSWAHL = [
    "OESTERREICH_MRG_VOLL", "OESTERREICH_MRG_TEIL", "OESTERREICH_MRG_FREI",
    "OESTERREICH_WGG", "OESTERREICH_GEWERBE", "DEUTSCHLAND", "UNGEKLAERT",
]


# -- Automatische Online-Listen (Auftrag HV-20260930-PORTAL-LISTEN) ----------
# Von der Übersicht, "Mieter & Objekte" und den drei Listen selbst
# verlinkt - reine Verlinkung, keine Datenabfrage.


_LISTEN_LINKS = (
    ("/backoffice/mieterliste", "Mieterliste"),
    ("/backoffice/zinsliste", "Zinsliste"),
    ("/backoffice/salden", "Salden"),
)


def listen_links_html(*, aktiver_pfad: str = "", objekt_id: str | None = None) -> str:
    """Schmale Linkzeile zu den drei Listen; ein gesetzter Objektfilter
    wird beim Wechsel zwischen den Listen mitgenommen."""

    parameter = f"?{urlencode({'objekt_id': objekt_id})}" if objekt_id else ""
    links = "".join(
        f'<a href="{h(pfad + parameter)}">'
        f'{"<strong>" + h(titel) + "</strong>" if pfad == aktiver_pfad else h(titel)}</a>'
        for pfad, titel in _LISTEN_LINKS
    )
    return f'<nav class="tabs"><span class="muted">Automatische Listen:</span> {links}</nav>'
