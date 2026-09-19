"""Zinsprofil je Vertrag und OeNB-Basiszinssatz (Mahnkostengrundlagen).

Entwurf und Freigabe sind getrennt; erst ein freigegebenes Profil wirkt
auf die Mahnkostenberechnung."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from mietinkasso.auth.service import require_gesellschaft_access, require_schreibrecht
from mietinkasso.backoffice.zinsprofil_form import basiszinssatz_formular, zinsprofil_form_werte, zinsprofil_formular
from mietinkasso.domain.exceptions import MietinkassoError
from mietinkasso.infrastructure.db.tables import ZinsprofilTable as _ZinsprofilTable

from mietinkasso.backoffice import dependencies as deps
from mietinkasso.backoffice.auth import _ctx, _current_session, _fehlerseite, _layout, _objekt_fuer_vertrag_gesperrt, _verify_csrf


#: Ohne eigenes Prefix - das gemeinsame `/backoffice`-Prefix wird GENAU
#: EINMAL in `backoffice/app.py` gesetzt.
router = APIRouter()


# -- Zinsprofil / OeNB-Basiszinssatz (Mahnkosten, Auftrag Markus 13.09.2026) -


@router.get("/vertrag/{vertrag_id}/zinsprofil", response_class=HTMLResponse)
def zinsprofil_uebersicht(request: Request, vertrag_id: str, session=Depends(_current_session)) -> HTMLResponse:
    vertrag = deps._stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None or _objekt_fuer_vertrag_gesperrt(vertrag_id):
        return _fehlerseite(session, "Zinsprofil", "Vertrag nicht verfügbar.")
    require_gesellschaft_access(_ctx(session), vertrag.gesellschaft_id)
    profile = deps._hv_mail.mahnkosten_repo.liste_fuer_vertrag(vertrag_id)
    return _layout(request, session, "Zinsprofil", zinsprofil_formular(vertrag, profile, session.csrf_token))


@router.post("/vertrag/{vertrag_id}/zinsprofil/erstellen")
async def zinsprofil_erstellen(request: Request, vertrag_id: str, session=Depends(_current_session)):
    form = await request.form()
    _verify_csrf(session, str(form.get("csrf_token", "")))
    vertrag = deps._stammdaten_repo.get_vertrag(vertrag_id)
    if vertrag is None or _objekt_fuer_vertrag_gesperrt(vertrag_id):
        return _fehlerseite(session, "Zinsprofil", "Vertrag nicht verfügbar.")
    require_gesellschaft_access(_ctx(session), vertrag.gesellschaft_id)
    require_schreibrecht(_ctx(session))
    try:
        werte = zinsprofil_form_werte(form)
        profil = deps._hv_mail.mahnkosten_repo.zinsprofil_anlegen(vertrag_id=vertrag_id, erstellt_von=session.user_id, **werte)
        deps._audit_service.log(entity_typ="zinsprofil", entity_id=str(profil.id), aktion="ENTWURF_ERFASST",
                           akteur=session.user_id, payload={"vertrag_id": vertrag_id, "version": profil.version})
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Zinsprofil", str(exc), f"/backoffice/vertrag/{vertrag_id}/zinsprofil")
    return RedirectResponse(f"/backoffice/vertrag/{vertrag_id}/zinsprofil", status_code=303)


@router.post("/zinsprofil/{zinsprofil_id}/freigeben")
def zinsprofil_freigeben(request: Request, zinsprofil_id: int, csrf_token: str = Form(...), session=Depends(_current_session)):
    _verify_csrf(session, csrf_token)
    with deps._session_factory() as db:
        row = db.get(_ZinsprofilTable, zinsprofil_id)
        vertrag_id = row.vertrag_id if row is not None else None
    if row is None or row.status != "ENTWURF" or _objekt_fuer_vertrag_gesperrt(vertrag_id):
        return _fehlerseite(session, "Zinsprofil", "Kein bestätigbarer Zinsprofil-Entwurf vorhanden.")
    vertrag = deps._stammdaten_repo.get_vertrag(vertrag_id)
    require_gesellschaft_access(_ctx(session), vertrag.gesellschaft_id)
    require_schreibrecht(_ctx(session))
    try:
        deps._hv_mail.mahnkosten_repo.zinsprofil_freigeben(zinsprofil_id, freigegeben_von=session.user_id)
        deps._audit_service.log(entity_typ="zinsprofil", entity_id=str(zinsprofil_id), aktion="ZINSPROFIL_BESTAETIGT",
                           akteur=session.user_id, payload={"vertrag_id": vertrag_id})
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Zinsprofil", str(exc))
    return RedirectResponse(f"/backoffice/vertrag/{vertrag_id}/zinsprofil", status_code=303)


@router.get("/basiszinssatz", response_class=HTMLResponse)
def basiszinssatz_uebersicht(request: Request, session=Depends(_current_session)) -> HTMLResponse:
    basiszinssaetze = deps._hv_mail.mahnkosten_repo.liste_basiszinssaetze()
    return _layout(request, session, "OeNB-Basiszinssatz", basiszinssatz_formular(basiszinssaetze, session.csrf_token))


@router.post("/basiszinssatz/erfassen")
async def basiszinssatz_erfassen(request: Request, session=Depends(_current_session)):
    form = await request.form()
    _verify_csrf(session, str(form.get("csrf_token", "")))
    require_schreibrecht(_ctx(session))
    try:
        from decimal import Decimal, InvalidOperation
        try:
            satz = Decimal(str(form.get("basiszinssatz_prozent", "")).strip().replace(",", "."))
        except InvalidOperation as exc:
            raise ValueError("Ungültiger Basiszinssatz.") from exc
        deps._hv_mail.mahnkosten_repo.basiszinssatz_erfassen(
            id=str(form.get("id", "")).strip(),
            gueltig_von=date.fromisoformat(str(form.get("gueltig_von", "")).strip()),
            gueltig_bis=date.fromisoformat(str(form.get("gueltig_bis", "")).strip()),
            basiszinssatz_prozent=satz, erfasst_von=session.user_id,
            quelle_referenz=str(form.get("quelle_referenz", "")).strip(),
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "OeNB-Basiszinssatz", str(exc), "/backoffice/basiszinssatz")
    return RedirectResponse("/backoffice/basiszinssatz", status_code=303)
