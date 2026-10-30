"""Rein lesende Route der Kautionsübersicht."""

from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from mietinkasso.backoffice import dependencies as deps
from mietinkasso.backoffice.routes import shared
from mietinkasso.backoffice.auth import _ctx, _current_session, _fehlerseite, _layout
from mietinkasso.backoffice.kautionsliste_ansicht import rendere_kautionsliste
from mietinkasso.backoffice.zinsliste_ansicht import logo_html
from mietinkasso.domain.exceptions import MietinkassoError
from mietinkasso.portallisten.kautionen import berechne_kautionsliste

router = APIRouter()


@router.get("/kautionen", response_class=HTMLResponse)
def kautionen_seite(
    request: Request,
    objekt_id: Optional[str] = None,
    q: Optional[str] = None,
    status: str = "alle",
    session=Depends(_current_session),
):
    try:
        liste = berechne_kautionsliste(
            ctx=_ctx(session),
            objekt_id=objekt_id or None,
            stammdaten_repository=deps._stammdaten_repo,
            status=status,
            suche=q or None,
        )
    except (MietinkassoError, ValueError) as exc:
        return _fehlerseite(session, "Kautionsübersicht", str(exc), "/backoffice/kautionen")
    html = rendere_kautionsliste(
        liste,
        logo=logo_html(getattr(deps._settings, "brief_logo_pfad", None)),
        navigation=shared.listen_links_html(
            aktiver_pfad="/backoffice/kautionen",
            objekt_id=liste.basis.objekt_filter,
        ),
    )
    return _layout(request, session, "Kautionsübersicht", html)
