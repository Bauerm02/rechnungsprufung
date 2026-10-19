"""Anmeldung und Abmeldung (Formular, Origin-Prüfung, Cookie).

Die eigentliche Sitzungs-/CSRF-Mechanik liegt in `backoffice/auth.py`
bzw. `backoffice/security.py`; hier stehen nur die drei HTTP-Routen."""

from __future__ import annotations

from fastapi import APIRouter, Cookie, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from mietinkasso.backoffice.security import pruefe_passwort
from mietinkasso.backoffice.views import flash_error, seite

from mietinkasso.backoffice import dependencies as deps
from mietinkasso.backoffice.auth import _pruefe_login_origin, _require_enabled, _verify_csrf


#: Ohne eigenes Prefix - das gemeinsame `/backoffice`-Prefix wird GENAU
#: EINMAL in `backoffice/app.py` gesetzt.
router = APIRouter()


# -- Login/Logout -----------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
def login_formular(request: Request, fehler: str | None = None) -> HTMLResponse:
    _require_enabled()
    fehler_html = flash_error(fehler) if fehler else ""
    inhalt = f"""
    {fehler_html}
    <div class="card" style="max-width:380px;margin:2rem auto;">
      <h1>Anmeldung</h1>
      <form method="post" action="/backoffice/login">
        <label>Benutzername</label>
        <input type="text" name="username" required autofocus>
        <label>Passwort</label>
        <input type="password" name="password" required>
        <button type="submit">Anmelden</button>
      </form>
    </div>"""
    return HTMLResponse(seite(titel="Anmeldung", inhalt=inhalt, environment=deps._settings.environment, send_enabled=deps._settings.send_enabled))


@router.post("/login")
def login_absenden(request: Request, username: str = Form(...), password: str = Form(...)) -> RedirectResponse:
    _require_enabled()
    _pruefe_login_origin(request)
    if deps._login_rate_limiter.gesperrt():
        return RedirectResponse(
            url="/backoffice/login?fehler=Zu+viele+Fehlversuche.+Bitte+in+einigen+Minuten+erneut+versuchen.",
            status_code=303,
        )
    gueltig = username == deps._settings.backoffice_user and pruefe_passwort(password, deps._settings.backoffice_password_hash)
    if not gueltig:
        deps._login_rate_limiter.fehlversuch_melden()
        return RedirectResponse(url="/backoffice/login?fehler=Benutzername+oder+Passwort+falsch.", status_code=303)
    deps._login_rate_limiter.erfolgreich_angemeldet()
    session_id, _csrf = deps._sessions.erstellen(username)
    response = RedirectResponse(url="/backoffice/", status_code=303)
    response.set_cookie(
        deps._COOKIE_NAME, session_id, httponly=True, samesite="lax", secure=deps._settings.backoffice_cookie_secure,
        max_age=deps._settings.backoffice_session_ttl_minuten * 60,
    )
    return response


@router.post("/logout")
def logout(csrf_token: str = Form(...), session_cookie: str | None = Cookie(default=None, alias=deps._COOKIE_NAME)) -> RedirectResponse:
    session = deps._sessions.holen(session_cookie)
    if session is not None:
        _verify_csrf(session, csrf_token)
        deps._sessions.loeschen(session_cookie)
    response = RedirectResponse(url="/backoffice/login", status_code=303)
    response.delete_cookie(deps._COOKIE_NAME)
    return response
