"""Öffentlicher HTTP-Einstieg des Backoffice für ``api.app``.

Fachliche Routen liegen unter ``routes/``, der gemeinsame Dienst- und
Sitzungszustand in ``dependencies.py`` und Zugriffsschutz in ``auth.py``.
Das Prefix und die Einbindungsreihenfolge werden ausschließlich hier gesetzt.
Details: ``docs/hausverwaltung/BACKOFFICE_ARCHITEKTUR.md``.
"""

from __future__ import annotations

from fastapi import APIRouter

from mietinkasso.backoffice.routes import (
    auth as auth_routes,
    bank as bank_routes,
    dashboard as dashboard_routes,
    indexbetrieb as indexbetrieb_routes,
    indexprofile as indexprofile_routes,
    konten as konten_routes,
    mahnwesen as mahnwesen_routes,
    mailversand as mailversand_routes,
    mieweg as mieweg_routes,
    monatsbericht as monatsbericht_routes,
    variableabrechnung as variableabrechnung_routes,
    vertragsanlage as vertragsanlage_routes,
    vertragspruefung as vertragspruefung_routes,
    zinsprofile as zinsprofile_routes,
)


router = APIRouter(prefix="/backoffice", tags=["backoffice"])

# Fachlich gruppierte Router. Bei überlappenden Pfaden ist die Reihenfolge
# bindend; die HTTP-Vertragstests prüfen die Auflösung gegen den Altstand.
router.include_router(auth_routes.router)
router.include_router(dashboard_routes.router)
router.include_router(konten_routes.router)
router.include_router(bank_routes.router)
router.include_router(mahnwesen_routes.router)
router.include_router(zinsprofile_routes.router)
router.include_router(mailversand_routes.router)
router.include_router(vertragspruefung_routes.router)
router.include_router(mieweg_routes.router)
router.include_router(indexprofile_routes.router)
router.include_router(indexbetrieb_routes.router)
router.include_router(monatsbericht_routes.router)
router.include_router(variableabrechnung_routes.router)
# Bewusst zuletzt: `GET /vertrag/{vertrag_id}` ist der einzige
# zweisegmentige Platzhalter unter /vertrag/ und steht modulintern NACH
# `GET /vertrag/weiterleiten`. Diese beiden Routen dürfen weder getrennt
# noch umsortiert werden.
router.include_router(vertragsanlage_routes.router)
